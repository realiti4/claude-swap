"""Check PyPI for newer versions of claude-swap."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tomllib
import urllib.request
from pathlib import Path
from typing import Any, NamedTuple

from claude_swap.cache import CACHE_DIR, MISSING, read_cache, write_cache

CACHE_PATH = CACHE_DIR / "update_check.json"
CACHE_TTL = 24 * 3600  # A successful lookup is kept for a day.
FAILURE_TTL = 3600  # A failed one is retried after an hour.
PYPI_URL = "https://pypi.org/pypi/claude-swap/json"

_VERSION_RE = re.compile(
    r"(\d+(?:\.\d+)*)(?:[-_.]?(alpha|beta|preview|pre|rc|a|b|c)[-_.]?(\d+)?)?",
    re.IGNORECASE,
)
_PRE_RANKS = {"alpha": 0, "a": 0, "beta": 1, "b": 1, "preview": 2, "pre": 2, "rc": 2, "c": 2}
_FINAL_RANK = 3


class _Version(NamedTuple):
    """A version as a sort key. NamedTuple so that comparing two of these
    compares the release first and only then the pre-release fields, which is
    what puts 0.27.0b1 below 0.27.0 and both below 0.28.0."""

    release: tuple[int, ...]
    pre_rank: int  # _FINAL_RANK when this is not a pre-release.
    pre_number: int


def _parse_version(v: str) -> _Version:
    """Parse a release number with an optional PEP 440 pre-release suffix.

    Every cycle of claude-swap ships as a pre-release first (0.27.0b1,
    0.26.0b1, ...), and a plain int() over the dotted parts raised ValueError
    on those, which check_for_update swallowed as "no update available".
    Development and post releases are not modeled — the project publishes
    none — so they collapse onto the release they belong to.
    """
    m = _VERSION_RE.match(v)
    if m is None:
        raise ValueError(f"unrecognized version: {v!r}")
    release = tuple(int(x) for x in m.group(1).split("."))
    # PEP 440 makes 0.27 and 0.27.0 the same release, and the pre-release rank
    # only means anything once the release segments line up.
    while len(release) > 1 and release[-1] == 0:
        release = release[:-1]
    if m.group(2) is None:
        return _Version(release, _FINAL_RANK, 0)
    return _Version(release, _PRE_RANKS[m.group(2).lower()], int(m.group(3) or 0))


def _is_newer(latest: str, current: str) -> bool:
    """Whether the latest release is an upgrade worth telling the user about."""
    latest_version = _parse_version(latest)
    current_version = _parse_version(current)
    # Nobody on a final release asked to be moved onto a pre-release, so we
    # stay quiet for them; people already running one still hear about later
    # pre-releases.
    if latest_version.pre_rank != _FINAL_RANK and current_version.pre_rank == _FINAL_RANK:
        return False
    return latest_version > current_version


def _detect_install_method() -> str | None:
    """Return 'uv', 'pipx', or None if we can't tell."""
    prefix = Path(sys.prefix)
    parts = tuple(p.lower() for p in prefix.parts)
    pairs = list(zip(parts, parts[1:]))

    if ("uv", "tools") in pairs:
        return "uv"
    if ("pipx", "venvs") in pairs:
        return "pipx"

    # Env-var override: only trust if sys.prefix is actually under it.
    for env_var, name in (("UV_TOOL_DIR", "uv"), ("PIPX_HOME", "pipx")):
        root = os.environ.get(env_var)
        if root:
            try:
                if prefix.is_relative_to(Path(root)):
                    return name
            except (ValueError, OSError):
                pass
    return None


class _Cooldown(NamedTuple):
    """An exclude-newer setting as uv spells it, and where the user set it."""

    value: str  # "P14D", "14 days", "2026-10-11T00:36:21Z"
    source: str  # 'uv-receipt.toml: exclude-newer-span = "P14D"'
    from_env: bool  # Set through UV_EXCLUDE_NEWER rather than the receipt.


def _read_receipt() -> dict[str, Any]:
    """The ``[tool]`` table of this install's uv tool receipt, or ``{}``."""
    try:
        with (Path(sys.prefix) / "uv-receipt.toml").open("rb") as fh:
            tool = tomllib.load(fh).get("tool", {})
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return tool if isinstance(tool, dict) else {}


def _uv_cooldown(receipt: dict[str, Any]) -> _Cooldown | None:
    """The release cooldown ``uv tool upgrade`` applies to this install.

    uv records ``--exclude-newer`` in the tool receipt and reapplies it on
    every upgrade. A relative setting ("14 days") is stored as
    ``exclude-newer-span`` next to the absolute cutoff of install time, and
    the span is what uv recomputes from. ``UV_EXCLUDE_NEWER`` in the
    environment overrides the receipt, as it does for uv itself.
    """
    env_value = os.environ.get("UV_EXCLUDE_NEWER", "").strip()
    if env_value:
        return _Cooldown(env_value, f"UV_EXCLUDE_NEWER={env_value}", from_env=True)
    options = receipt.get("options", {})
    if not isinstance(options, dict):
        return None
    for key in ("exclude-newer-span", "exclude-newer"):
        value = options.get(key)
        if isinstance(value, str):
            return _Cooldown(value, f'uv-receipt.toml: {key} = "{value}"', from_env=False)
    return None


# A receipt value is pasted into a shell line unquoted, so only an ISO
# duration ("P14D") or an RFC 3339 timestamp may pass.
_CUTOFF_RE = re.compile(r"^[A-Za-z0-9.:+-]+$")


def _uv_latest_version(current_version: str, cooldown: _Cooldown | None) -> str | None:
    """The newest claude-swap release uv would install into this environment.

    ``uv pip list --outdated`` resolves against the index the way ``uv tool
    upgrade`` does once it is handed the same exclude-newer setting, so the
    version it reports is one uv can actually install. Returns the current
    version when nothing newer is visible and None when uv fails. A uv that
    is not on PATH raises FileNotFoundError, since the caller treats that
    differently from a failed resolve. The environment variable is dropped so that the cooldown passed
    here is the only one uv sees, and the command runs from the tool's own
    directory because uv reads the ``uv.toml`` or ``pyproject.toml`` of
    whatever project the shell happens to be in. ``uv tool upgrade`` does
    not, so a project's own cutoff must not hide releases from this check.
    """
    cmd = ["uv", "pip", "list", "--outdated", "--format", "json", "--python", sys.executable]
    if cooldown is not None:
        cmd += ["--exclude-newer", cooldown.value]
    env = {k: v for k, v in os.environ.items() if k != "UV_EXCLUDE_NEWER"}
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
            check=True,
            cwd=sys.prefix,
        )
        rows = json.loads(result.stdout)
    except FileNotFoundError:
        raise
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    for row in rows:
        if row.get("name") == "claude-swap":
            latest = row.get("latest_version")
            return latest if isinstance(latest, str) else None
    return current_version


def _pypi_latest_version() -> str | None:
    """PyPI's latest release, for installs that uv does not manage."""
    try:
        req = urllib.request.Request(PYPI_URL)
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read().decode())
        return data["info"]["version"]
    except Exception:
        return None


class _Lookup(NamedTuple):
    """A version lookup and whether its answer is worth writing to the cache."""

    latest: str | None
    cache: bool


def _latest_version(current_version: str, method: str | None) -> _Lookup:
    """The newest release the user's package manager will actually install.

    uv is often off PATH when cswap runs under cron or launchd. Without a
    cooldown PyPI answers the same question, so it takes over. With one,
    PyPI would advertise releases the cooldown hides, so the run stays quiet
    and leaves the cache alone: the interactive run that follows has uv and
    retries at once. Any other uv failure (a timeout, a failed resolve) is
    cached like a PyPI failure, so a dead network costs one attempt per
    FAILURE_TTL rather than one per command.
    """
    if method != "uv":
        return _Lookup(_pypi_latest_version(), cache=True)
    cooldown = _uv_cooldown(_read_receipt())
    uv_missing = False
    try:
        latest = _uv_latest_version(current_version, cooldown)
    except FileNotFoundError:
        latest = None
        uv_missing = True
    if latest is not None:
        return _Lookup(latest, cache=True)
    if cooldown is None:
        return _Lookup(_pypi_latest_version(), cache=True)
    return _Lookup(None, cache=not uv_missing)


def held_back_release_notice(current_version: str) -> str | None:
    """Explain a release that uv's cooldown keeps ``uv tool upgrade`` from seeing.

    uv prints "Nothing to upgrade" whether the install is current or the
    newest release is simply younger than the cooldown, and the user cannot
    tell the two apart. When a cooldown is set, this asks uv what it would
    install without one and names the setting that is in the way.

    The remedy is ``uv tool upgrade`` itself with a zero cooldown, which
    takes the release and leaves the receipt's extras, ``--with`` packages,
    Python version and index options alone. uv stores the zero as
    ``exclude-newer-span = "PT0S"``, so a second line restores the receipt's
    own value. The flag also wins over ``UV_EXCLUDE_NEWER`` for that one
    run, and since the variable keeps applying afterwards there is nothing
    to restore in that case.
    """
    cooldown = _uv_cooldown(_read_receipt())
    if cooldown is None:
        return None
    try:
        latest = _uv_latest_version(current_version, None)
        if latest is None or not _is_newer(latest, current_version):
            return None
    except (FileNotFoundError, ValueError):
        return None
    take_now = 'uv tool upgrade claude-swap --exclude-newer "0 days"'
    lines = [
        f"claude-swap {latest} is on PyPI, but this install has a release cooldown",
        f"  ({cooldown.source}) that hides it from uv tool upgrade.",
    ]
    if cooldown.from_env:
        lines.append(f"  To take it now (the flag overrides UV_EXCLUDE_NEWER): {take_now}")
    else:
        lines.append(f"  To take it now:          {take_now}")
        if _CUTOFF_RE.match(cooldown.value):
            lines.append(
                "  To restore the cooldown: "
                f"uv tool upgrade claude-swap --exclude-newer {cooldown.value}"
            )
    return "\n".join(lines)


def check_for_update(current_version: str) -> str | None:
    """Return a notification string if a newer version exists, else None."""
    try:
        method = _detect_install_method()
        cached_data = read_cache(CACHE_PATH, CACHE_TTL)
        if cached_data is None:
            # A cached failure. Keep it for an hour, not a day.
            cached_data = read_cache(CACHE_PATH, FAILURE_TTL)
        if cached_data is not MISSING and isinstance(cached_data, str | None):
            latest_version = cached_data
        else:
            lookup = _latest_version(current_version, method)
            latest_version = lookup.latest
            # A failure is cached as None and expires after FAILURE_TTL.
            # The one exception is uv missing under a cooldown, see
            # _latest_version.
            if lookup.cache:
                write_cache(CACHE_PATH, latest_version)

        if latest_version and _is_newer(latest_version, current_version):
            direct = {
                "uv": "uv tool upgrade claude-swap",
                "pipx": "pipx upgrade claude-swap",
            }.get(method or "")
            if direct and sys.platform != "win32":
                # cswap upgrade actually performs the upgrade here.
                hint = "Run `cswap upgrade` to update."
            elif direct:
                # Windows: cswap upgrade only prints, so point at the real command.
                hint = f"Run `{direct}` to update."
            else:
                # Unknown install method: cswap upgrade shows manual instructions.
                hint = "Run `cswap upgrade` for upgrade instructions."
            return (
                f"A newer version of claude-swap is available ({latest_version}). "
                f"You are using {current_version}. {hint}"
            )
        return None
    except Exception:
        return None


def _installed_version_on_disk() -> str | None:
    """The version claude-swap actually has on disk after an upgrade.

    importlib.metadata resolves the running environment, so it still reports
    the pre-upgrade version from inside this process. A fresh interpreter
    started from sys.executable reads the tool directory uv/pipx rewrote,
    giving an honest after-version for both install methods without parsing
    `uv tool list` / `pipx list` output. Best effort: any failure or timeout
    returns None so a verification hiccup never masks the upgrade's own exit
    status.
    """
    code = (
        "import importlib.metadata as m\n"
        "print(m.version('claude-swap'))"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    version = result.stdout.strip()
    return version or None


def run_self_upgrade(current_version: str | None = None) -> int:
    """Run the appropriate upgrade command for the current install method.

    Returns the subprocess exit code, or 1 if detection failed or the package
    manager is missing from PATH. After a successful run it verifies what
    actually landed on disk and reports the before -> after, since a package
    manager can exit 0 without changing anything (uv's "Nothing to upgrade",
    #491). A uv install with a release cooldown also gets told about a release
    the cooldown hides, since uv itself only reports "Nothing to upgrade".
    """
    from claude_swap.printer import accent, error, warning

    if current_version is None:
        from claude_swap import __version__

        current_version = __version__
    before = current_version

    method = _detect_install_method()
    commands = {
        "uv": ["uv", "tool", "upgrade", "claude-swap"],
        "pipx": ["pipx", "upgrade", "claude-swap"],
    }
    cmd = commands.get(method or "")
    if cmd is None:
        error(
            "Could not detect install method (looked for uv tool / pipx).\n"
            f"  sys.prefix:     {sys.prefix}\n"
            f"  sys.executable: {sys.executable}\n"
            "To upgrade manually, run one of:\n"
            "  uv tool upgrade claude-swap\n"
            "  pipx upgrade claude-swap\n"
            f"  {sys.executable} -m pip install --upgrade claude-swap\n"
            "If you installed with `pip install -e .`, use `git pull` instead."
        )
        return 1

    # Windows: the running cswap.exe launcher is locked, so an in-process
    # uv/pipx upgrade fails when it tries to replace the executable even
    # though the package itself updates. cswap exits right after this, which
    # releases the lock, so the user can just run the command themselves.
    if sys.platform == "win32":
        print(f"To upgrade claude-swap on Windows, run:\n  {accent(' '.join(cmd))}")
        if method == "uv":
            notice = held_back_release_notice(current_version)
            if notice:
                warning(notice)
        return 1

    try:
        result = subprocess.run(cmd, check=False)
    except FileNotFoundError:
        error(
            f"Detected {method} install but `{cmd[0]}` is not on PATH. "
            "Run the upgrade manually from a shell where it is available."
        )
        return 1

    if result.returncode != 0:
        return result.returncode

    # The package manager can exit 0 without changing anything, so verify
    # what landed on disk and say so rather than trusting its wording (#491).
    # importlib.metadata in a fresh interpreter reads the directory uv/pipx
    # rewrote, so the report is honest for both install methods. Verification
    # is best effort: a failure or timeout returns None and we stay silent,
    # never masking the upgrade's own exit status.
    after = _installed_version_on_disk()
    if before and after and after != before:
        print(accent(f"claude-swap upgraded: {before} -> {after}"))
    elif before and after:
        print(f"claude-swap is still at {after}.")

    if method == "uv":
        notice = held_back_release_notice(current_version)
        if notice:
            warning(notice)
    return result.returncode
