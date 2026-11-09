"""Usage from Claude Code's status line: ``cswap-statusline`` and its setup.

Claude Code runs one status line command after each response and hands it a
JSON payload. Since 2.1.x that payload carries ``rate_limits``: the
subscription's 5-hour and 7-day utilization from the responses it got. So
the account in use can be read at no cost, with no probe and no usage
endpoint request.

``cswap statusline install`` points ``statusLine.command`` in Claude Code's
settings at ``cswap-statusline --then '<your command>'``. On every render it:

1. starts your own command, passes it the payload, and lets its output and
   exit status through unchanged;
2. meanwhile saves the rate limits, with what is needed to tell which
   account they belong to, in ``<cswap data>/statusline/<profile>.json``.

With no command of its own to wrap, it prints a short ``account · 5h · 7d``
line instead. cswap's collectors (dashboard, ``cswap auto``, ``cswap list``)
take those readings on their next pass, for setup-token accounts (see
``ClaudeAccountSwitcher._ingest_statusline``).

The hook runs after every response, so this module imports nothing from
cswap at load time: the whole package takes ~0.1 s to import, and the hook
needs ~0.01 s.

**Which account a reading belongs to.** The payload doesn't say. The hook
records a hash of the profile's access token when the credential is a file
or ``CLAUDE_CODE_OAUTH_TOKEN`` (Linux, WSL, Windows). Otherwise (the macOS
Keychain) it records the live account from ``.claude.json``, and when cswap
last changed accounts. The transcript says when the request behind the
numbers was sent. A reading whose request went out before the credential
last changed, or too soon after it, is dropped, so numbers that arrived just
before a switch are never credited to the account switched to.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

HOOK_NAME = "cswap-statusline"
INBOX_DIRNAME = "statusline"
RECORD_SCHEMA = 1
#: Readings older than this are not taken (the windows may have moved on).
MAX_READING_AGE_S = 6 * 3600.0
#: Inbox files untouched this long are deleted by the collector.
PRUNE_AFTER_S = 24 * 3600.0
#: A request sent this soon after its profile's credential file changed may
#: still have used the previous one.
FILE_GRACE_S = 5.0
#: The same for the macOS Keychain, which Claude Code caches for ~30 s.
KEYCHAIN_GRACE_S = 45.0
#: How much of the transcript's end is read to find the latest response.
TRANSCRIPT_TAIL_BYTES = 512 * 1024
_WINDOWS = ("five_hour", "seven_day")


# ---------------------------------------------------------------------------
# locations (mirror claude_swap.paths without importing it; tests keep them
# equal)
# ---------------------------------------------------------------------------


def backup_root() -> Path:
    """cswap's data folder (``paths.get_backup_root``)."""
    if sys.platform.startswith("linux"):
        xdg = os.environ.get("XDG_DATA_HOME", "")
        if xdg:
            xdg_path = Path(os.path.expanduser(xdg))
            if xdg_path.is_absolute():
                return xdg_path / "claude-swap"
        return Path.home() / ".local" / "share" / "claude-swap"
    return Path.home() / ".claude-swap-backup"


def inbox_dir(root: Path) -> Path:
    return root / INBOX_DIRNAME


def config_home() -> Path:
    """Claude Code's config folder (``paths.get_claude_config_home``)."""
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env) if env else Path.home() / ".claude"


def global_config_path() -> Path:
    """Claude Code's ``.claude.json`` (``paths.get_global_config_path``)."""
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    current = (Path(env) if env else Path.home()) / ".claude.json"
    legacy = config_home() / ".config.json"
    try:
        st = legacy.stat()
    except OSError:
        return current
    if stat.S_ISREG(st.st_mode) and (
        st.st_size > 0 or (st.st_mode & 0o222 and not os.path.exists(current))
    ):
        return legacy
    return current


def _is_default_profile(home: Path) -> bool:
    try:
        return home.resolve() == (Path.home() / ".claude").resolve()
    except OSError:
        return False


def _mtime(path: Path | str | None) -> float | None:
    if not path:
        return None
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


# ---------------------------------------------------------------------------
# reading the payload and the transcript
# ---------------------------------------------------------------------------


def rate_windows(payload: object) -> dict:
    """``{five_hour|seven_day: {used_percentage, resets_at}}`` from a payload."""
    rate = payload.get("rate_limits") if isinstance(payload, dict) else None
    out: dict = {}
    for key in _WINDOWS:
        window = rate.get(key) if isinstance(rate, dict) else None
        pct = _number(window.get("used_percentage")) if isinstance(window, dict) else None
        if pct is None:
            continue
        out[key] = {"used_percentage": pct, "resets_at": _number(window.get("resets_at"))}
    return out


def _entry(line: bytes) -> dict | None:
    try:
        data = json.loads(line)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _timestamp(entry: dict | None) -> float | None:
    from datetime import datetime

    value = entry.get("timestamp") if entry else None
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def last_exchange(transcript: object) -> tuple[float | None, float | None]:
    """``(requestAt, responseAt)`` for the latest main-thread response.

    ``responseAt`` is the newest assistant entry's time. ``requestAt`` is the
    time of the entry that response answers (a prompt or a tool result): the
    request went out after it, so the credential it used was read after it.
    Either is None when the transcript's end doesn't show it.
    """
    if not isinstance(transcript, str) or not transcript:
        return None, None
    try:
        with open(transcript, "rb") as f:
            size = f.seek(0, os.SEEK_END)
            f.seek(max(0, size - TRANSCRIPT_TAIL_BYTES))
            lines = f.read().split(b"\n")
    except OSError:
        return None, None
    if size > TRANSCRIPT_TAIL_BYTES:
        lines = lines[1:]  # cut mid-line
    request_id = response_at = first = None
    i = len(lines) - 1
    while i >= 0:
        line = lines[i]
        i -= 1
        if b'"assistant"' not in line:
            continue
        entry = _entry(line)
        if not entry or entry.get("type") != "assistant" or entry.get("isSidechain"):
            continue
        if request_id is None:
            request_id = entry.get("requestId")
            response_at = _timestamp(entry)
            if not request_id or response_at is None:
                return None, response_at
            first = entry
        elif entry.get("requestId") == request_id:
            first = entry  # an earlier block of the same response
        else:
            break
    if first is None:
        return None, None
    parent = first.get("parentUuid")
    if not isinstance(parent, str) or not parent:
        return None, response_at
    needle = b'"' + parent.encode("utf-8") + b'"'
    for line in reversed(lines):
        if needle in line:
            entry = _entry(line)
            if entry and entry.get("uuid") == parent:
                return _timestamp(entry), response_at
    return None, response_at


def token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _profile_token(home: Path) -> tuple[str | None, float | None]:
    """Hash of the access token Claude Code uses here, and when it last changed.

    ``CLAUDE_CODE_OAUTH_TOKEN`` takes precedence and is fixed for the
    process's life, so it has no change time.
    """
    env_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if env_token:
        return token_sha256(env_token), None
    path = home / ".credentials.json"
    data = _read_json(path)
    oauth = data.get("claudeAiOauth") if data else None
    token = oauth.get("accessToken") if isinstance(oauth, dict) else None
    if not isinstance(token, str) or not token:
        return None, None
    return token_sha256(token), _mtime(path)


def _live_identity() -> dict | None:
    """The account ``.claude.json`` says is signed in: ``{email, organizationUuid}``."""
    account = (_read_json(global_config_path()) or {}).get("oauthAccount")
    email = account.get("emailAddress") if isinstance(account, dict) else None
    if not isinstance(email, str) or not email:
        return None
    org = account.get("organizationUuid")
    return {"email": email, "organizationUuid": org if isinstance(org, str) else ""}


# ---------------------------------------------------------------------------
# the hook
# ---------------------------------------------------------------------------


def observe(payload: object, *, now: float | None = None, root: Path | None = None) -> dict | None:
    """Save this render's rate limits for the collectors and return the record.

    Nothing is written without rate limits, without a response in the
    transcript to date them, or before cswap has a data folder.
    """
    windows = rate_windows(payload)
    root = root or backup_root()
    if not windows or not root.is_dir():
        return None
    request_at, response_at = last_exchange(
        payload.get("transcript_path") if isinstance(payload, dict) else None
    )
    if response_at is None:
        return None
    home = config_home()
    default = _is_default_profile(home)
    token_hash, token_changed = _profile_token(home)
    record: dict = {
        "schemaVersion": RECORD_SCHEMA,
        "observedAt": time.time() if now is None else now,
        "requestAt": request_at,
        "responseAt": response_at,
        "profile": None if default else str(home.resolve()),
        "tokenSha256": token_hash,
        "credentialChangedAt": token_changed,
        "account": None,
        "accountChangedAt": None,
        "rateLimits": windows,
    }
    if token_hash is None:
        record["account"] = _live_identity()
        if default:
            # cswap rewrites sequence.json on every switch.
            record["accountChangedAt"] = _mtime(root / "sequence.json")
    _write_record(root, record)
    return record


def record_name(profile: str | None) -> str:
    if not profile:
        return "default.json"
    return "profile-" + hashlib.sha256(profile.encode("utf-8")).hexdigest()[:16] + ".json"


def _write_record(root: Path, record: dict) -> None:
    folder = inbox_dir(root)
    path = folder / record_name(record["profile"])
    previous = _read_json(path)
    if previous is not None:
        unchanged = {k: v for k, v in previous.items() if k != "observedAt"}
        if unchanged == {k: v for k, v in record.items() if k != "observedAt"}:
            return  # a re-render with nothing new
    folder.mkdir(mode=0o700, exist_ok=True)
    tmp = folder / f".{path.name}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(record, f)
    os.replace(tmp, path)


def countdown(resets_at: float | None, now: float) -> str | None:
    if resets_at is None or resets_at <= now:
        return None
    minutes = int((resets_at - now) // 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


def account_label(root: Path) -> str | None:
    """The live account's cswap alias, else its email."""
    identity = _live_identity()
    if identity is None:
        return None
    accounts = (_read_json(root / "sequence.json") or {}).get("accounts")
    for rec in accounts.values() if isinstance(accounts, dict) else ():
        if (
            isinstance(rec, dict)
            and rec.get("email") == identity["email"]
            and (rec.get("organizationUuid") or "") == identity["organizationUuid"]
        ):
            alias = rec.get("alias")
            if isinstance(alias, str) and alias:
                return alias
            break
    return identity["email"]


def short_line(payload: object, *, now: float | None = None, root: Path | None = None) -> str:
    """``team · 5h 25% (1h 59m) · 7d 61%``, for a machine with no status line."""
    now = time.time() if now is None else now
    parts = [account_label(root or backup_root()) or "Claude"]
    labels = {"five_hour": "5h", "seven_day": "7d"}
    for key, window in rate_windows(payload).items():
        text = f"{labels[key]} {window['used_percentage']:.0f}%"
        left = countdown(window["resets_at"], now) if key == "five_hour" else None
        parts.append(f"{text} ({left})" if left else text)
    return " · ".join(parts)


def _shell_argv(command: str) -> list[str] | None:
    """How to run a status line command the way Claude Code does: with bash
    (Git Bash on Windows). None falls back to the system shell."""
    if sys.platform == "win32":
        git_bash = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH")
        found = git_bash if git_bash and os.path.isfile(git_bash) else shutil.which("bash")
        # System32's bash.exe is WSL's, not the one Claude Code uses.
        if not found or "system32" in found.lower():
            return None
        return [found, "-c", command]
    found = shutil.which("bash")
    return [found, "-c", command] if found else None


def main(argv: list[str] | None = None) -> int:
    """``cswap-statusline [--then COMMAND]``: Claude Code's status line command."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] in (["-h"], ["--help"]):
        print(
            f"usage: {HOOK_NAME} [--then COMMAND]\n\n"
            "Claude Code's status line command, set up by `cswap statusline install`."
        )
        return 0
    then = argv[1] if len(argv) >= 2 and argv[0] == "--then" else None
    try:
        raw = sys.stdin.buffer.read()
    except (OSError, ValueError):
        raw = b""

    child = None
    if then:
        shell_argv = _shell_argv(then)
        try:
            child = subprocess.Popen(
                shell_argv or then, shell=shell_argv is None, stdin=subprocess.PIPE
            )
        except OSError:
            child = None
        else:
            try:
                child.stdin.write(raw)
                child.stdin.close()
            except OSError:  # it exited without reading its input
                pass

    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        payload = {}
    try:
        observe(payload)
    except Exception:  # noqa: BLE001, S110 -- never break the status line
        pass

    if child is not None:
        return child.wait()
    if then is None:
        try:
            line = short_line(payload)
        except Exception:  # noqa: BLE001
            line = "Claude"
        sys.stdout.write(line + "\n")
    return 0


# ---------------------------------------------------------------------------
# collector side: records -> readings (cswap imports happen here, never in
# the hook)
# ---------------------------------------------------------------------------


def read_inbox(root: Path, *, now: float) -> list[dict]:
    """Every current record. Deletes ones untouched for ``PRUNE_AFTER_S``."""
    import contextlib

    try:
        paths = [
            p for p in inbox_dir(root).iterdir()
            if p.suffix == ".json" and not p.name.startswith(".")
        ]
    except OSError:
        return []
    records = []
    for path in sorted(paths):
        record = _read_json(path)
        observed = _number(record.get("observedAt")) if record else None
        if record is None or record.get("schemaVersion") != RECORD_SCHEMA or observed is None:
            continue
        if now - observed > PRUNE_AFTER_S:
            with contextlib.suppress(OSError):
                path.unlink()
            continue
        records.append(record)
    return records


def usage_from_record(record: dict, now: float) -> tuple[dict, float] | None:
    """``(usage, age_s)`` in the usage store's shape; None if unusable or too old."""
    from datetime import UTC, datetime

    from claude_swap import oauth

    taken = _number(record.get("responseAt"))
    if taken is None:
        return None
    age = max(0.0, now - taken)
    if age > MAX_READING_AGE_S:
        return None
    raw: dict = {}
    windows = record.get("rateLimits")
    for key in _WINDOWS:
        window = windows.get(key) if isinstance(windows, dict) else None
        pct = _number(window.get("used_percentage")) if isinstance(window, dict) else None
        if pct is None:
            continue
        entry: dict = {"utilization": pct}
        resets_at = _number(window.get("resets_at"))
        if resets_at:
            try:
                entry["resets_at"] = datetime.fromtimestamp(resets_at, tz=UTC).isoformat()
            except (OverflowError, OSError, ValueError):
                continue
        raw[key] = entry
    if not raw:
        return None
    try:
        usage = oauth.build_usage_result(raw)
    except Exception:  # noqa: BLE001 -- a malformed record is skipped
        return None
    if not usage:
        return None
    # The numbers come from the response's rate-limit headers, as a header
    # probe's do, so the same rules for a partial reading apply.
    usage["source"] = oauth.USAGE_SOURCE_HEADERS
    return usage, age


def attribute(
    record: dict,
    *,
    token_slots: dict[str, list[str]],
    identity_slot,
    profile_slot,
) -> list[str]:
    """The slots a record's reading belongs to, or ``[]`` when that is not certain.

    ``token_slots`` maps a token hash to the setup-token slots holding that
    token; ``identity_slot(email, org)`` and ``profile_slot(path)`` look a
    slot up by live identity or by session profile folder.
    """
    request_at = _number(record.get("requestAt"))
    if request_at is None:
        return []
    token_hash = record.get("tokenSha256")
    if isinstance(token_hash, str) and token_hash:
        slots = list(token_slots.get(token_hash, ()))
        changed, grace = _number(record.get("credentialChangedAt")), FILE_GRACE_S
    else:
        account = record.get("account")
        if not isinstance(account, dict) or not isinstance(account.get("email"), str):
            return []
        profile = record.get("profile")
        slot = (
            profile_slot(profile)
            if isinstance(profile, str) and profile
            else identity_slot(account["email"], account.get("organizationUuid") or "")
        )
        slots = [slot] if slot else []
        changed, grace = _number(record.get("accountChangedAt")), KEYCHAIN_GRACE_S
    if changed is not None and request_at < changed + grace:
        return []  # the request may have gone out on the previous credential
    return slots


def readings(
    records: list[dict],
    *,
    now: float,
    eligible: set[str],
    token_slots: dict[str, list[str]],
    identity_slot,
    profile_slot,
) -> dict[str, tuple[dict, float]]:
    """``{slot: (usage, age_s)}`` for ``UsageStore.adopt``: the newest
    attributable reading per eligible slot."""
    out: dict[str, tuple[dict, float]] = {}
    for record in records:
        reading = usage_from_record(record, now)
        if reading is None:
            continue
        for slot in attribute(
            record, token_slots=token_slots,
            identity_slot=identity_slot, profile_slot=profile_slot,
        ):
            if slot in eligible and (slot not in out or reading[1] < out[slot][1]):
                out[slot] = reading
    return out


# ---------------------------------------------------------------------------
# install / uninstall (`cswap statusline ...`)
# ---------------------------------------------------------------------------


class SetupError(Exception):
    """The status line setting could not be changed."""


def settings_path() -> Path:
    """Claude Code's user settings for the profile in use."""
    return config_home() / "settings.json"


def _load_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise SetupError(f"{path} is not valid JSON ({e}). Fix it first.") from None
    except OSError as e:
        raise SetupError(f"Could not read {path}: {e}") from None
    if not isinstance(data, dict):
        raise SetupError(f"{path} does not hold a JSON object.")
    return data


def _save_settings(path: Path, data: dict) -> Path:
    """Write atomically, through a symlink (session profiles link this file
    to the default one). The first change keeps a copy of the original."""
    target = path.resolve() if path.is_symlink() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    backup = target.with_name(target.name + ".cswap-backup")
    if target.exists() and not backup.exists():
        shutil.copy2(target, backup)
    mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o644
    tmp = target.with_name(f".{target.name}.cswap.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, target)
    return target


def _command_words(status_line: object) -> list[str]:
    command = status_line.get("command") if isinstance(status_line, dict) else None
    try:
        return shlex.split(command) if isinstance(command, str) else []
    except ValueError:
        return []


def is_hook(status_line: object) -> bool:
    words = _command_words(status_line)
    return bool(words) and Path(words[0].replace("\\", "/")).name.removesuffix(".exe") == HOOK_NAME


def wrapped_command(status_line: object) -> str | None:
    """The user's own command inside a hook command, or None."""
    words = _command_words(status_line)
    if len(words) >= 3 and words[1] == "--then":
        return words[2]
    return None


def hook_executable() -> str:
    """Absolute path of ``cswap-statusline``, written for bash."""
    found = shutil.which(HOOK_NAME)
    if not found:
        script = Path(sys.argv[0]).resolve()
        for name in (HOOK_NAME, HOOK_NAME + ".exe"):
            beside = script.parent / name
            if beside.is_file():
                found = str(beside)
                break
    if not found:
        raise SetupError(
            f"`{HOOK_NAME}` is not installed. Reinstall claude-swap to get it "
            "(for example `uv tool install --force claude-swap`)."
        )
    path = str(Path(found).absolute())
    # Claude Code runs the command with Git Bash on Windows, which eats
    # backslashes.
    return path.replace("\\", "/") if sys.platform == "win32" else path


def install(*, path: Path | None = None, executable: str | None = None) -> tuple[str, str | None]:
    """Put the hook in front of the status line.

    Returns ``(state, wrapped)``: ``state`` is ``"installed"`` or
    ``"already"``, and ``wrapped`` is the user's own command it runs.
    """
    path = path or settings_path()
    data = _load_settings(path)
    current = data.get("statusLine")
    if is_hook(current):
        return "already", wrapped_command(current)
    exe = shlex.quote(executable or hook_executable())
    if current is None:
        data["statusLine"] = {"type": "command", "command": exe, "padding": 0}
        wrapped = None
    elif (
        isinstance(current, dict)
        and current.get("type", "command") == "command"
        and current.get("shell") in (None, "bash")
        and isinstance(current.get("command"), str)
        and current["command"].strip()
    ):
        wrapped = current["command"]
        data["statusLine"] = {**current, "command": f"{exe} --then {shlex.quote(wrapped)}"}
    else:
        raise SetupError(
            f"The statusLine setting in {path} is not a bash command cswap can "
            "run in front of. Left unchanged."
        )
    _save_settings(path, data)
    return "installed", wrapped


def uninstall(*, path: Path | None = None) -> tuple[bool, str | None]:
    """Put the status line back as it was. Returns ``(changed, restored command)``."""
    path = path or settings_path()
    if not path.exists():
        return False, None
    data = _load_settings(path)
    current = data.get("statusLine")
    if not is_hook(current):
        return False, None
    wrapped = wrapped_command(current)
    if wrapped is None:
        data.pop("statusLine", None)
    else:
        data["statusLine"] = {**current, "command": wrapped}
    _save_settings(path, data)
    return True, wrapped


def project_overrides(start: Path | None = None) -> list[Path]:
    """Project settings from ``start`` upward that set their own status line.

    These take precedence over the user setting, so the hook doesn't run in
    those projects.
    """
    found = []
    folder = (start or Path.cwd()).resolve()
    home = Path.home().resolve()
    for candidate in (folder, *folder.parents):
        if candidate == home:
            break
        for name in ("settings.json", "settings.local.json"):
            path = candidate / ".claude" / name
            data = _read_json(path) if path.is_file() else None
            if data is not None and "statusLine" in data and not is_hook(data["statusLine"]):
                found.append(path)
    return found


if __name__ == "__main__":
    sys.exit(main())
