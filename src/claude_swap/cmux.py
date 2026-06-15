"""cmux integration for cswap (Beta, macOS).

`cmux <https://github.com/manaflow-ai/cmux>`_ is a native macOS terminal that can
open workspaces (tabs) each running a command. This module wires the cswap load
balancer into cmux two ways:

* **S1 — a reusable surface.** :func:`setup` merges a named custom command
  ("Balanced Claude (cswap)") into ``~/.config/cmux/cmux.json`` so the user can
  spawn a balancer-managed Claude Code session from cmux's plus-button / command
  palette at any time. It backs the config up to a timestamped ``.bak`` first,
  merges idempotently (preserving all existing user content), then validates and
  reloads via the cmux CLI.

* **S2 — a one-shot fanout.** :func:`fanout` opens ``n`` cmux workspaces, each
  running ``cswap launch`` in the current directory. Because the balancer's
  online reservation spreads concurrent launches across accounts, the panes land
  on *different* accounts automatically. Uses the documented, robust
  ``cmux new-workspace --cwd … --command …`` path with create→verify→retry to
  defend against the known "typed command silently dropped when unfocused" bug.

The crucial correctness detail is that cmux ships a ``cmux-claude-wrapper`` that
intercepts ``claude`` and *clears* most auth-selection env before exec'ing the
real binary — but it honours ``CMUX_PRESERVE_CLAUDE_AUTH_SELECTION_ENV_KEYS`` (a
comma/space-separated allow-list of env keys to keep). cswap pins each session to
its per-session profile via ``CLAUDE_CONFIG_DIR``; so every surface/command we
emit sets that preserve-key to include ``CLAUDE_CONFIG_DIR``, and the supervisor
(:meth:`supervisor.Supervisor._session_env`) appends it for the child too. Claude
Code merges the wrapper's ``--settings`` additively with the profile's
``settings.json``, so cswap's statusLine and cmux's hooks coexist.

Stdlib only; cmux.json writes go through ``switcher._write_json`` (atomic rename,
0600). macOS-only: every entry point raises a clear error elsewhere.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from claude_swap import embed, registry
from claude_swap.exceptions import SessionError
from claude_swap.models import Platform

# The cmux CLI is not on PATH by default; it lives inside the app bundle.
_APP_BUNDLE_CLI = Path(
    "/Applications/cmux.app/Contents/Resources/bin/cmux"
)

# The named custom-command surface we install into cmux.json. Idempotency keys on
# this name, so renaming it would orphan a previously-installed entry.
SURFACE_NAME = "Balanced Claude (cswap)"

# The cmux.json env key that lists auth-selection env vars the cmux-claude-wrapper
# must NOT strip before exec'ing the real claude. We add CLAUDE_CONFIG_DIR so the
# per-session profile pin survives.
PRESERVE_KEYS_ENV = "CMUX_PRESERVE_CLAUDE_AUTH_SELECTION_ENV_KEYS"
CLAUDE_CONFIG_DIR_KEY = "CLAUDE_CONFIG_DIR"

# How long to let the supervisors register their rows before reading them back in
# :func:`fanout` (each `cswap launch` writes a registry row on startup).
_FANOUT_SETTLE_S = 8.0
# new-workspace create -> verify the workspace appeared -> retry budget.
_CREATE_VERIFY_RETRIES = 3
_CREATE_VERIFY_DELAY_S = 1.5


# --------------------------------------------------------------------------- #
# Availability / discovery
# --------------------------------------------------------------------------- #


def find_cmux() -> str | None:
    """Return the cmux CLI path, or ``None`` if cmux is not installed.

    Prefers a ``cmux`` resolvable on ``PATH`` (so a user who symlinked it wins),
    then falls back to the standard app-bundle location.
    """
    on_path = shutil.which("cmux")
    if on_path:
        return on_path
    if _APP_BUNDLE_CLI.exists():
        return str(_APP_BUNDLE_CLI)
    return None


def is_available() -> bool:
    """True only on macOS with the cmux CLI present."""
    return Platform.detect() == Platform.MACOS and find_cmux() is not None


def _require_macos_cmux() -> str:
    """Return the cmux CLI path or raise a clear, actionable error.

    Guards every public entry point so a non-macOS host or a missing cmux
    install fails with one tidy message instead of a traceback.
    """
    if Platform.detect() != Platform.MACOS:
        raise SessionError("cmux integration is macOS-only.")
    cli = find_cmux()
    if not cli:
        raise SessionError(
            "cmux was not found. Install the cmux app from https://cmux.com "
            "(its CLI lives at /Applications/cmux.app/Contents/Resources/bin/cmux)."
        )
    return cli


def config_path() -> Path:
    """Path to cmux's primary config file (``~/.config/cmux/cmux.json``)."""
    return Path.home() / ".config" / "cmux" / "cmux.json"


# --------------------------------------------------------------------------- #
# Surface (custom command) construction
# --------------------------------------------------------------------------- #


def cswap_launch_command() -> str:
    """The shell command a cmux surface runs to start a managed session.

    Mirrors :func:`embed.cswap_statusline_command`: prefer a resolvable ``cswap``
    on PATH, else invoke the package with the current interpreter so it always
    works for the installed environment.
    """
    if shutil.which("cswap"):
        return "cswap launch"
    return f'"{sys.executable}" -m claude_swap launch'


def build_surface() -> dict:
    """The cswap custom-command entry merged into cmux.json's ``commands`` array.

    A cmux custom command is a named terminal launcher surfaced in the command
    palette / plus-button. ``env`` sets the wrapper's preserve-key allow-list so
    ``CLAUDE_CONFIG_DIR`` (cswap's per-session profile pin) survives the
    cmux-claude-wrapper's auth-env clearing.
    """
    return {
        "name": SURFACE_NAME,
        "type": "terminal",
        "command": cswap_launch_command(),
        "cwd": ".",
        "env": {PRESERVE_KEYS_ENV: CLAUDE_CONFIG_DIR_KEY},
    }


def merge_preserve_keys(existing: str | None) -> str:
    """Return a preserve-key list that includes ``CLAUDE_CONFIG_DIR``.

    cmux accepts a comma- or space-separated list. We append our key to any
    existing value (de-duplicated, order-preserving) rather than clobbering it,
    so a user (or cmux) preserve list keeps working. Shared with the supervisor's
    env logic so both sides agree on the format.
    """
    keys: list[str] = []
    if existing:
        for tok in re.split(r"[,\s]+", existing.strip()):
            if tok and tok not in keys:
                keys.append(tok)
    if CLAUDE_CONFIG_DIR_KEY not in keys:
        keys.append(CLAUDE_CONFIG_DIR_KEY)
    return ",".join(keys)


# --------------------------------------------------------------------------- #
# cmux.json read/merge (JSONC-tolerant)
# --------------------------------------------------------------------------- #


def _strip_jsonc(text: str) -> str:
    """Strip ``//`` line and ``/* */`` block comments outside of strings.

    cmux.json is JSONC (the on-launch template is entirely commented-out keys).
    A real parser is overkill; this scanner respects string literals and escape
    sequences so comment markers inside strings are left intact.
    """
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    escaped = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _strip_trailing_commas(text: str) -> str:
    """Remove trailing commas before ``}``/``]`` (legal in JSONC, not JSON)."""
    return re.sub(r",(\s*[}\]])", r"\1", text)


def load_config(path: Path | None = None) -> dict:
    """Parse cmux.json (JSONC-tolerant). Missing/empty/corrupt -> ``{}``.

    Returns a plain dict so :func:`merge_surface` can layer our command in.
    Comments are dropped on parse; the writer re-emits strict JSON, which cmux
    accepts (verified via ``cmux config validate``).
    """
    path = path or config_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    cleaned = _strip_trailing_commas(_strip_jsonc(raw)).strip()
    if not cleaned:
        return {}
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def merge_surface(config: dict) -> tuple[dict, bool]:
    """Merge our custom command into ``config`` idempotently.

    Returns ``(new_config, changed)``. Preserves every existing key (including a
    user's other custom commands) and only touches the ``commands`` array entry
    whose ``name`` is :data:`SURFACE_NAME`. ``changed`` is False when the entry
    already matches, so callers can skip a needless write/reload.
    """
    new = dict(config)
    new.setdefault(
        "$schema",
        "https://raw.githubusercontent.com/manaflow-ai/cmux/main/web/data/cmux.schema.json",
    )
    new.setdefault("schemaVersion", 1)

    commands = new.get("commands")
    if not isinstance(commands, list):
        commands = []
    else:
        commands = list(commands)  # don't mutate the caller's list in place

    surface = build_surface()
    idx = next(
        (
            i
            for i, c in enumerate(commands)
            if isinstance(c, dict) and c.get("name") == SURFACE_NAME
        ),
        None,
    )
    if idx is None:
        commands.append(surface)
        changed = True
    elif commands[idx] != surface:
        commands[idx] = surface
        changed = True
    else:
        changed = False

    new["commands"] = commands
    return new, changed


# --------------------------------------------------------------------------- #
# cmux CLI helpers
# --------------------------------------------------------------------------- #


def _run_cli(cli: str, *args: str, timeout: float = 30.0) -> subprocess.CompletedProcess:
    """Run a cmux CLI subcommand, capturing output. Never raises on nonzero."""
    return subprocess.run(
        [cli, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _validate(cli: str) -> tuple[bool, str]:
    """Run ``cmux config validate``; return ``(ok, message)``."""
    try:
        cp = _run_cli(cli, "config", "validate")
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    out = (cp.stdout or "") + (cp.stderr or "")
    ok = cp.returncode == 0 and "JSONC syntax is valid" in out
    return ok, out.strip()


# --------------------------------------------------------------------------- #
# S1 — install the reusable surface
# --------------------------------------------------------------------------- #


def setup(switcher) -> dict:
    """Install the "Balanced Claude (cswap)" surface into cmux.json.

    Backs up an existing cmux.json to a timestamped ``.bak``, merges our command
    idempotently (preserving all user content), validates, and reloads via the
    cmux CLI. Returns a status dict:

        ``{"ok", "changed", "config_path", "backup_path", "validated",
           "reloaded", "messages"}``

    Resilient to an absent/empty cmux.json (cmux creates the template on launch;
    a fresh strict-JSON file with just our command is equally valid). Raises a
    clear :class:`SessionError` on non-macOS or when cmux isn't installed.
    """
    cli = _require_macos_cmux()
    # Ensure managed-template exists so a session launched from the surface is
    # immediately embeddable (mirrors `cswap --install`).
    embed.write_managed_template(switcher)

    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    messages: list[str] = []
    backup_path: str | None = None
    if path.exists() and path.stat().st_size > 0:
        backup = path.with_name(f"cmux.json.{time.strftime('%Y%m%d-%H%M%S')}.bak")
        try:
            shutil.copy2(path, backup)
            backup_path = str(backup)
        except OSError as exc:
            messages.append(f"backup failed: {exc}")

    config = load_config(path)
    merged, changed = merge_surface(config)

    if changed:
        # Atomic write via the switcher (0600). cmux accepts strict JSON.
        switcher._write_json(path, merged)
    else:
        messages.append("surface already present and current")

    validated, vmsg = _validate(cli)
    if not validated:
        messages.append(f"validation: {vmsg}")

    reloaded = False
    if validated:
        try:
            cp = _run_cli(cli, "reload-config")
            reloaded = cp.returncode == 0
            if not reloaded:
                messages.append(
                    f"reload-config exit {cp.returncode}: "
                    f"{((cp.stdout or '') + (cp.stderr or '')).strip()[:200]}"
                )
        except (OSError, subprocess.SubprocessError) as exc:
            messages.append(f"reload-config failed: {exc}")

    return {
        "ok": validated,
        "changed": changed,
        "config_path": str(path),
        "backup_path": backup_path,
        "validated": validated,
        "reloaded": reloaded,
        "messages": messages,
    }


# --------------------------------------------------------------------------- #
# S2 — fanout: open N workspaces each running `cswap launch`
# --------------------------------------------------------------------------- #


def _shell_quote(arg: str) -> str:
    """Single-quote-escape an arg for the shell command cmux types/runs."""
    return "'" + arg.replace("'", "'\\''") + "'"


def _build_launch_command(claude_args: list[str]) -> str:
    """The shell command line a fanout workspace runs.

    ``cswap launch`` (PATH or interpreter fallback), forwarding any claude args
    after ``--``. Args are shell-quoted so paths/flags with spaces survive.
    """
    cmd = cswap_launch_command()
    if claude_args:
        cmd += " -- " + " ".join(_shell_quote(a) for a in claude_args)
    return cmd


def _list_workspace_count(cli: str) -> int:
    """Best-effort count of current workspaces (for create→verify)."""
    try:
        cp = _run_cli(cli, "list-workspaces")
    except (OSError, subprocess.SubprocessError):
        return -1
    if cp.returncode != 0:
        return -1
    lines = [ln for ln in (cp.stdout or "").splitlines() if ln.strip()]
    return len(lines)


def _open_one_workspace(cli: str, cwd: str, command: str, name: str) -> bool:
    """Create one workspace running ``command``, verifying it appeared.

    Uses ``cmux new-workspace --cwd … --command …`` — the documented headless
    path (the command is sent to the new workspace's terminal). To defend against
    the known "typed command silently dropped when unfocused" race, we verify the
    workspace count grew and retry the create on failure.
    """
    for attempt in range(1, _CREATE_VERIFY_RETRIES + 1):
        before = _list_workspace_count(cli)
        try:
            cp = _run_cli(
                cli,
                "new-workspace",
                "--name",
                name,
                "--cwd",
                cwd,
                "--command",
                command,
                "--focus",
                "true",  # focus avoids the unfocused-drop race for the typed cmd
            )
        except (OSError, subprocess.SubprocessError):
            time.sleep(_CREATE_VERIFY_DELAY_S)
            continue
        if cp.returncode == 0:
            # Verify a new workspace actually appeared before declaring success.
            time.sleep(_CREATE_VERIFY_DELAY_S)
            after = _list_workspace_count(cli)
            if before < 0 or after < 0 or after > before:
                return True
        time.sleep(_CREATE_VERIFY_DELAY_S)
    return False


def fanout(switcher, n: int, claude_args: list[str] | None = None) -> dict:
    """Open ``n`` cmux workspaces each running a balancer-managed session.

    Each workspace runs ``cswap launch [-- <claude_args>]`` in the current
    directory; the balancer's online reservation spreads them across accounts.
    After a short settle, reads the registry back to report which account each
    landed on. Returns a status dict::

        {"ok", "requested", "opened", "command", "cwd",
         "accounts": [<num>, ...], "distinct_accounts": <int>, "messages"}

    Raises :class:`SessionError` on non-macOS / missing cmux, or when there are
    no managed accounts to balance across.
    """
    cli = _require_macos_cmux()
    if n < 1:
        raise SessionError("fanout count must be >= 1.")

    seq = switcher._get_sequence_data() or {}
    if not seq.get("accounts"):
        raise SessionError(
            "No managed accounts yet. Add one with `cswap --add-account` first."
        )
    if not shutil.which("claude"):
        raise SessionError(
            "'claude' was not found on PATH. Install Claude Code first."
        )

    # Ensure the managed template exists before sessions spawn.
    embed.write_managed_template(switcher)

    cwd = str(Path.cwd())
    command = _build_launch_command(list(claude_args or []))

    messages: list[str] = []
    opened = 0
    for i in range(n):
        name = f"cswap {i + 1}/{n}"
        if _open_one_workspace(cli, cwd, command, name):
            opened += 1
        else:
            messages.append(f"workspace {i + 1} failed to open (after retries)")

    # Let the supervisors register their rows, then read which account each got.
    if opened:
        time.sleep(_FANOUT_SETTLE_S)
    accounts = _recent_session_accounts(switcher, opened)
    distinct = len({a for a in accounts if a})

    return {
        "ok": opened == n,
        "requested": n,
        "opened": opened,
        "command": command,
        "cwd": cwd,
        "accounts": accounts,
        "distinct_accounts": distinct,
        "messages": messages,
    }


def _recent_session_accounts(switcher, limit: int) -> list[str]:
    """Account numbers of the ``limit`` most-recently-started live sessions.

    Reads the registry (no lock needed — atomic writes) and returns the newest
    rows' accounts, so the caller can confirm the fanout spread across accounts.
    """
    reg = registry.read_registry(switcher)
    rows = registry.live_sessions(reg)  # oldest-first
    newest = rows[-limit:] if limit and limit < len(rows) else rows
    return [str(r.get("account_num", "")) for r in newest]
