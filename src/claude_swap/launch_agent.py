"""Run ``cswap menubar`` as a launchd LaunchAgent instead of a foreground process.

A menu bar item that dies with the terminal that started it, and never comes
back after a logout or reboot, is the wrong shape for a menu bar app. So
``cswap menubar`` does not run the menu bar in the calling process: it makes
sure launchd runs it (:func:`ensure`) and returns. launchd then starts it at
login and restarts it after a crash. The process launchd spawns is
``cswap menubar --foreground``, the one invocation that draws the status item
in place. (The wrapper ``.app`` that process hops into is
``claude_swap.app_bundle``'s business: launchd only ever needs the console
script.)

*The plist's location is the Start-at-login preference.* A plist in
``~/Library/LaunchAgents`` is what launchd scans at login, so that is where
first setup puts it. When the user turns *Start at login* off in the menu, the
same plist moves to the data directory: launchd can still be handed it for the
current session (that is how ``brew services run`` works too), but nothing
scans that directory at login. The file is deliberately kept there rather than
deleted, because a missing plist means "never set up" and would turn Start at
login back on the next time ``cswap menubar`` ran. Both present means the login
copy wins. Nothing here moves a plist *and* touches launchd in one step: the
running job keeps its in-memory definition, and the menu toggle must never
stop the app whose menu was just clicked.

*launchd's own worker is recognised by pid, not by flag.* ``launchctl print``
names the job's pid; when that is this process, launchd started us and
``cswap menubar`` has to run the menu bar here instead of trying to start the
service it already is. That also keeps plists written before the
``--foreground`` flag existed working: they still say plain ``cswap menubar``.

Two older decisions are worth restating, because both differ from the obvious
approach:

*The plist pins the console script, not ``sys.executable``.* A LaunchAgent
outlives upgrades, and the two paths age differently: ``uv tool upgrade`` (and
``cswap upgrade``) rebuilds the tool's virtualenv — ``sys.executable`` points
inside that virtualenv and can be replaced — while the console script keeps its
path across upgrades. Pinning the script means an upgraded cswap needs a
``launchctl kickstart``, not a reinstalled service. ``sys.executable -m
claude_swap`` stays as the fallback for installs that expose no console script.

*Logs go to ``~/Library/Logs``, not ``/tmp``.* ``/tmp`` is world-writable and
periodically purged, so a crash log can vanish before anyone reads it, and a
predictable world-writable path is a poor place to point a long-lived writer.

Everything here is macOS-only; callers guard on ``sys.platform`` and the public
functions refuse rather than half-work elsewhere.
"""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path

from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.fsutil import atomic_write_bytes

LABEL = "com.cswap.menubar"

# The one invocation that runs the menu bar in the calling process. launchd's
# plist uses it; so does a developer who wants the app attached to a terminal.
WORKER_FLAG = "--foreground"

# Where the user switches a LaunchAgent off. The switch governs the start at
# login only: macOS still lets the user's own `launchctl bootstrap` through, so
# a start is allowed to proceed, but it has to say that this is where the
# login start was turned off.
SYSTEM_SETTINGS_HINT = (
    "System Settings → General → Login Items & Extensions → Allow in the Background"
)

# `sfltool dumpbtm` lists every background item with its disposition; it is
# the only non-root view of the System Settings switch and takes ~2 s.
_BTM_DUMP_TIMEOUT_SECONDS = 15.0

# launchd's default PATH is /usr/bin:/bin:/usr/sbin:/sbin, which covers
# `security` (Keychain reads) but not a Homebrew or ~/.local/bin `claude`. The
# menu bar shells out to detect running sessions, so seed a PATH that finds it.
_EXTRA_PATH_DIRS = ("~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin")
_BASE_PATH_DIRS = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")

# launchd does not inherit the installing shell's environment, so a user behind
# a proxy would get an agent that dials direct. Forward only these, never all.
# A key that is set but empty is forwarded too: urllib reads an empty lowercase
# name as cancelling its uppercase twin (`https_proxy=` switches off an
# `HTTPS_PROXY`), so dropping it would hand the agent a proxy the shell had
# turned off. launchd sets an empty string as an empty variable.
_PROFILE_ENV_KEYS = ("CLAUDE_CONFIG_DIR", "CLAUDE_SECURESTORAGE_CONFIG_DIR", "XDG_DATA_HOME")
_FORWARDED_ENV_KEYS = (
    "https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY",
    "no_proxy", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR",
    # The profile the installing shell was in. Without these the service
    # would manage the default profile while the shell that set it up manages
    # another. There is one service per user, and it follows the shell that
    # installed it; `--install-service` from a different profile says so.
    *_PROFILE_ENV_KEYS,
)

# Written by the worker at start: its pid and build. Lets a later `cswap
# menubar` (or `cswap upgrade`) tell a running instance of an old build from
# the installed one, whichever way the upgrade happened.
STAMP_NAME = "menubar_worker.json"

# A worker younger than this may simply not have written its stamp yet (the
# bundle hop and the imports come first); a missing stamp says nothing then.
_STAMP_GRACE_SECONDS = 60.0

# `launchctl bootout` can return before launchd has finished tearing the
# job down, and a `bootstrap` inside that window fails with "Operation
# already in progress". Poll until the job is really gone instead of
# assuming bootout was synchronous (Homebrew's services code does the same).
_UNLOAD_TIMEOUT_SECONDS = 5.0
_UNLOAD_POLL_SECONDS = 0.1

# After a bootstrap or kickstart launchd spawns the job asynchronously; wait
# briefly for a pid so the caller can print one, but never block on it.
_SPAWN_TIMEOUT_SECONDS = 2.0


def _require_macos() -> None:
    if sys.platform != "darwin":
        raise ClaudeSwitchError(
            "The menu bar service is only available on macOS."
        )


def plist_path(label: str = LABEL, home: Path | None = None) -> Path:
    """The plist's path while the menu bar starts at login."""
    return (home or Path.home()) / "Library" / "LaunchAgents" / f"{label}.plist"


def session_plist_path(label: str = LABEL, backup_dir: Path | None = None) -> Path:
    """The plist's path while Start at login is off (see the module docstring)."""
    if backup_dir is None:
        from claude_swap.paths import get_backup_root

        backup_dir = get_backup_root()
    return Path(backup_dir) / f"{label}.plist"


def start_at_login_enabled(label: str = LABEL, home: Path | None = None) -> bool:
    """Whether launchd will start the menu bar at the next login."""
    return plist_path(label, home).exists()


def worker_stamp_path(backup_dir: Path | None = None) -> Path:
    if backup_dir is None:
        from claude_swap.paths import get_backup_root

        backup_dir = get_backup_root()
    return Path(backup_dir) / STAMP_NAME


def write_worker_stamp(backup_dir: Path | None = None) -> Path:
    """Record this process's pid and build; called by the menu bar at start."""
    from claude_swap.update_check import install_identity

    path = worker_stamp_path(backup_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"pid": os.getpid(), "started_at": time.time(), **install_identity()}
    atomic_write_bytes(path, json.dumps(data, indent=2).encode("utf-8"))
    return path


def read_worker_stamp(backup_dir: Path | None = None) -> dict | None:
    try:
        loaded = json.loads(worker_stamp_path(backup_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _parse_etime(text: str) -> float | None:
    """Seconds from ``ps -o etime`` output: ``[[dd-]hh:]mm:ss``."""
    text = text.strip()
    if not text:
        return None
    days = 0
    if "-" in text:
        day_part, text = text.split("-", 1)
        if not day_part.isdigit():
            return None
        days = int(day_part)
    parts = text.split(":")
    if not 2 <= len(parts) <= 3 or not all(part.isdigit() for part in parts):
        return None
    values = [int(part) for part in parts]
    hours, minutes, seconds = (0, *values) if len(values) == 2 else values
    return float(days * 86400 + hours * 3600 + minutes * 60 + seconds)


def _process_age_seconds(pid: int) -> float | None:
    """Seconds since ``pid`` started, or None when ``ps`` cannot say."""
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "etime="],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return _parse_etime(proc.stdout)


def worker_build_is_stale(pid: int, backup_dir: Path | None = None) -> bool:
    """Whether the worker running as ``pid`` should be restarted onto this build.

    With a stamp of its own (pid match) the answer is exact: version and commit
    are compared, so a git install whose version string never moves still
    counts. Without one — a worker from before stamps existed (the 0.30 and
    1.0.0b1 builds), or one that could not write the data directory — the
    build is unknown, and the worker is restarted once so that its successor
    can be told apart. Not while it may still be writing its stamp, and not
    when a stamp cannot be written here either: then every call would restart
    it, which is worse than an unknown build.
    """
    stamp = read_worker_stamp(backup_dir)
    if not stamp or stamp.get("pid") != pid:
        age = _process_age_seconds(pid)
        if age is None or age < _STAMP_GRACE_SECONDS:
            return False
        return os.access(worker_stamp_path(backup_dir).parent, os.W_OK)
    from claude_swap.update_check import install_identity

    current = install_identity()
    return (stamp.get("version"), stamp.get("source")) != (
        current["version"],
        current["source"],
    )


def log_paths(label: str = LABEL, home: Path | None = None) -> tuple[Path, Path]:
    """``(stdout, stderr)`` log destinations for ``label``."""
    logs = (home or Path.home()) / "Library" / "Logs"
    return logs / f"{label}.log", logs / f"{label}.err"


def service_target(label: str = LABEL, uid: int | None = None) -> str:
    """launchd service target, e.g. ``gui/501/com.cswap.menubar``."""
    return f"gui/{os.getuid() if uid is None else uid}/{label}"


def domain_target(uid: int | None = None) -> str:
    """launchd domain target, e.g. ``gui/501``."""
    return f"gui/{os.getuid() if uid is None else uid}"


def resolve_program() -> list[str]:
    """Argv prefix that launchd should run, minus the subcommand.

    Prefers the installed console script (stable across upgrades, see the
    module docstring) and falls back to running the package through the
    interpreter that is executing right now.

    The path is made absolute but deliberately NOT resolved: a `uv tool
    install` puts a symlink at ``~/.local/bin/cswap`` pointing into the tool's
    virtualenv, and resolving it would write that virtualenv-internal path
    into the plist — the very path this module avoids pinning, since a
    reinstall recreates the virtualenv while the symlink keeps its name.
    """
    candidate = sys.argv[0] if sys.argv and sys.argv[0] else None
    if candidate is not None:
        absolute = Path(os.path.abspath(candidate))
        if absolute.name == "cswap" and absolute.is_file():
            return [str(absolute)]

    which = shutil.which("cswap")
    if which:
        return [str(Path(os.path.abspath(which)))]

    return [sys.executable, "-m", "claude_swap"]


def program_arguments(program: list[str] | None = None) -> list[str]:
    """The argv launchd runs: the program plus the worker invocation."""
    return [*(program or resolve_program()), "menubar", WORKER_FLAG]


def _path_env(program: list[str]) -> str:
    """PATH for the agent, with the program's own directory first."""
    dirs: list[str] = []
    first = Path(program[0]).parent
    if str(first) not in ("", "."):
        dirs.append(str(first))
    for extra in (*_EXTRA_PATH_DIRS, *_BASE_PATH_DIRS):
        expanded = os.path.expanduser(extra)
        if expanded not in dirs:
            dirs.append(expanded)
    return ":".join(dirs)


def _plist_dict(
    program: list[str] | None = None,
    label: str = LABEL,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> dict:
    """The LaunchAgent definition, as the dict :func:`build_plist` serializes."""
    program = program or resolve_program()
    out_log, err_log = log_paths(label, home)
    env = os.environ if env is None else env
    forwarded = {k: env[k] for k in _FORWARDED_ENV_KEYS if k in env}
    return {
        "Label": label,
        "ProgramArguments": program_arguments(program),
        "RunAtLoad": True,
        # Restart a crash, but respect a deliberate Quit. The menu bar's
        # quit handler calls rumps.quit_application(), a clean exit(0);
        # under a bare `KeepAlive: True` launchd would relaunch it at once
        # and the Quit item would do nothing the user can see.
        "KeepAlive": {"SuccessfulExit": False},
        # A menu bar owner is a UI process; Background would have launchd
        # apply throttled I/O and CPU bands to it.
        "ProcessType": "Interactive",
        "EnvironmentVariables": {"PATH": _path_env(program), **forwarded},
        "StandardOutPath": str(out_log),
        "StandardErrorPath": str(err_log),
    }


def build_plist(
    program: list[str] | None = None,
    label: str = LABEL,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> bytes:
    """Serialize the LaunchAgent plist.

    Built with :mod:`plistlib` rather than a formatted XML string so paths
    containing ``&`` or ``<`` cannot produce a plist launchd refuses to parse.
    """
    return plistlib.dumps(_plist_dict(program, label, home, env))


def _load_plist(path: Path) -> dict | None:
    """The plist at ``path`` as a dict; None when absent or unreadable.

    An unreadable plist is treated like a missing one so that :func:`ensure`
    rewrites it rather than handing launchd a file it will refuse.
    """
    try:
        loaded = plistlib.loads(path.read_bytes())
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["launchctl", *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as e:  # pragma: no cover - launchctl is in the base OS
        raise ClaudeSwitchError("launchctl not found; is this macOS?") from e


def _wait_until_unloaded(
    label: str = LABEL,
    uid: int | None = None,
    timeout: float = _UNLOAD_TIMEOUT_SECONDS,
) -> bool:
    """Block until launchd has dropped the job. True if it went away in time."""
    deadline = time.monotonic() + timeout
    while is_loaded(label, uid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(_UNLOAD_POLL_SECONDS)
    return True


def _wait_for_pid(
    label: str = LABEL,
    uid: int | None = None,
    timeout: float = _SPAWN_TIMEOUT_SECONDS,
) -> int | None:
    """The job's pid once launchd has spawned it, or None if it took too long."""
    deadline = time.monotonic() + timeout
    while True:
        pid = service_pid(label, uid)
        if pid is not None or time.monotonic() >= deadline:
            return pid
        time.sleep(_UNLOAD_POLL_SECONDS)


def is_loaded(label: str = LABEL, uid: int | None = None) -> bool:
    """Whether launchd currently knows about the service."""
    return _launchctl("print", service_target(label, uid)).returncode == 0


def _launchd_disabled(label: str = LABEL, uid: int | None = None) -> bool:
    """Whether launchd's own override list disables the service.

    ``launchctl print-disabled gui/<uid>`` is what ``launchctl disable`` writes.
    Older launchctl spelled the value ``true``; current ones say ``disabled``.
    A list that cannot be read counts as "not disabled".
    """
    printed = _launchctl("print-disabled", domain_target(uid))
    if printed.returncode != 0:
        return False
    key = f'"{label}"'
    for line in printed.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith(key) and "=>" in stripped:
            value = stripped.split("=>", 1)[1].strip().rstrip(";").lower()
            return value in ("disabled", "true")
    return False


def _btm_record_disallowed(dump: str, label: str) -> bool:
    """Whether ``sfltool dumpbtm`` output lists ``label`` as switched off.

    Records are blocks headed `` #N:``; each has a ``Disposition: [..]`` line
    before its ``Identifier:`` line, which BTM spells as ``<type>.<label>``
    (``8.com.cswap.menubar`` for a user agent). ``disallowed`` in the
    disposition is the "Allow in the Background" switch in the off position;
    an item the user left alone reads ``allowed``.
    """
    matches = disallowed = False
    for raw in dump.splitlines():
        line = raw.strip()
        if line.startswith("#") and line.endswith(":"):
            if matches and disallowed:
                return True
            matches = disallowed = False
        elif line.startswith("Identifier:"):
            identifier = line.split(":", 1)[1].strip()
            matches = identifier == label or identifier.endswith("." + label)
        elif line.startswith("Disposition:") and "[" in line:
            flags = line.split("[", 1)[1].split("]", 1)[0]
            disallowed = "disallowed" in [flag.strip() for flag in flags.split(",")]
    return matches and disallowed


def _btm_disallowed(label: str = LABEL) -> bool:
    """Whether the user switched the item off under Login Items & Extensions.

    That switch is recorded by backgroundtaskmanagementd, not in launchd's
    disabled list, and it governs the start at login only: a manual
    ``launchctl bootstrap`` from the user's own session still goes through
    (verified on macOS 27). A dump that cannot be read — older releases want
    root for it — counts as "not switched off".
    """
    try:
        proc = subprocess.run(
            ["sfltool", "dumpbtm"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_BTM_DUMP_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        return False
    return _btm_record_disallowed(proc.stdout, label)


def switched_off_in_system_settings(label: str = LABEL, uid: int | None = None) -> bool:
    """Whether the user turned the service off, in either place macOS offers.

    Slow (about two seconds, see :func:`_btm_disallowed`), so callers ask only
    when the answer changes what they say: before a start, and for status.
    """
    return _launchd_disabled(label, uid) or _btm_disallowed(label)


def _job_state(
    label: str = LABEL, uid: int | None = None
) -> tuple[bool, str | None, int | None]:
    """``(loaded, state, pid)`` from ``launchctl print``."""
    printed = _launchctl("print", service_target(label, uid))
    loaded = printed.returncode == 0
    state: str | None = None
    pid: int | None = None
    if loaded:
        for line in printed.stdout.splitlines():
            # `launchctl print` nests sub-dictionaries — pid-local endpoints,
            # inherited environment — and those repeat keys the job itself
            # uses, `state` among them. The job's own fields are the ones at a
            # single tab, so anything deeper is a different object's field.
            if not line.startswith("\t") or line.startswith("\t\t"):
                continue
            stripped = line.strip()
            if state is None and stripped.startswith("state = "):
                state = stripped.removeprefix("state = ").strip()
            elif pid is None and stripped.startswith("pid = "):
                raw = stripped.removeprefix("pid = ").strip()
                if raw.isdigit():
                    pid = int(raw)
    return loaded, state, pid


def service_pid(label: str = LABEL, uid: int | None = None) -> int | None:
    """The pid of the running service, or None when it is not running."""
    return _job_state(label, uid)[2]


def running_as_service(label: str = LABEL, uid: int | None = None) -> bool:
    """Whether THIS process is the launchd job for ``label``.

    launchd's ``print`` names the job's pid; a match means launchd started us,
    through whichever plist it holds, so ``cswap menubar`` must run the menu bar
    here rather than try to start the service it already is. Off macOS there is
    no launchd to ask.
    """
    if sys.platform != "darwin":
        return False
    pid = service_pid(label, uid)
    return pid is not None and pid == os.getpid()


def status(
    label: str = LABEL,
    uid: int | None = None,
    home: Path | None = None,
    backup_dir: Path | None = None,
) -> dict:
    """Installed / loaded / running state, plus the pid when there is one.

    ``installed`` and ``start_at_login`` both mean "the plist is in
    ``~/Library/LaunchAgents``"; the first name predates the second and is kept
    for callers that read it. ``session_plist`` is set while Start at login is
    off, ``configured`` is true when either plist exists, and ``switched_off``
    reports the System Settings switch (a slow read, see
    :func:`switched_off_in_system_settings`).
    """
    from claude_swap.update_check import describe_identity, install_identity

    _require_macos()
    loaded, state, pid = _job_state(label, uid)
    login = plist_path(label, home)
    session = session_plist_path(label, backup_dir)
    stamp = read_worker_stamp(backup_dir)
    own_stamp = stamp if (stamp and pid is not None and stamp.get("pid") == pid) else None
    return {
        "label": label,
        "installed": login.exists(),
        "start_at_login": login.exists(),
        "session_plist": str(session) if session.exists() else None,
        "configured": login.exists() or session.exists(),
        "loaded": loaded,
        "state": state,
        "pid": pid,
        "plist": str(login),
        "switched_off": switched_off_in_system_settings(label, uid),
        "running_build": describe_identity(own_stamp) if own_stamp else None,
        "installed_build": describe_identity(install_identity()),
        "stale_build": pid is not None and worker_build_is_stale(pid, backup_dir),
    }


def _bootstrap(target_plist: Path, label: str, uid: int | None, settled: bool) -> None:
    booted = _launchctl("bootstrap", domain_target(uid), str(target_plist))
    if booted.returncode != 0:
        detail = (booted.stderr or booted.stdout or "").strip()
        if not settled:
            detail = f"{detail}; the previous instance was still shutting down".lstrip("; ")
        raise ClaudeSwitchError(
            f"launchctl bootstrap failed (exit {booted.returncode})"
            + (f": {detail}" if detail else "")
        )


def _reload(target_plist: Path, label: str, uid: int | None) -> None:
    """Hand launchd ``target_plist``, replacing a loaded definition if any.

    launchd keeps a bootstrapped job's definition in memory, so a changed
    plist only takes effect through bootout + bootstrap; ``kickstart`` would
    restart the old definition.
    """
    settled = True
    if is_loaded(label, uid):
        _launchctl("bootout", service_target(label, uid))
        settled = _wait_until_unloaded(label, uid)
    _bootstrap(target_plist, label, uid, settled)


def kickstart(
    label: str = LABEL, uid: int | None = None, *, restart: bool = False
) -> None:
    """Start a loaded job now; with ``restart`` also kill a running one first."""
    args = ["kickstart", *(["-k"] if restart else []), service_target(label, uid)]
    kicked = _launchctl(*args)
    if kicked.returncode != 0:
        detail = (kicked.stderr or kicked.stdout or "").strip()
        raise ClaudeSwitchError(
            f"launchctl kickstart failed (exit {kicked.returncode})"
            + (f": {detail}" if detail else "")
        )


def ensure(
    label: str = LABEL,
    home: Path | None = None,
    backup_dir: Path | None = None,
    program: list[str] | None = None,
    uid: int | None = None,
    *,
    restart: bool = False,
) -> dict:
    """Make the menu bar service run; first setup registers it at login.

    What happens depends on what is there (the module docstring has the why):

    * no plist anywhere — first setup: the plist goes to ``~/Library/LaunchAgents``
      and is bootstrapped, so the menu bar starts now and at every login;
    * a plist in ``~/Library/LaunchAgents`` — Start at login is on: a stopped
      job is kickstarted, an unloaded one bootstrapped, a running one left alone;
    * a plist only in the data directory — the user turned Start at login off:
      the same, but bootstrapped from there, so launchd forgets it at logout;
    * both — the login copy wins and the other is removed.

    A plist whose ``ProgramArguments`` no longer match this install (a reinstall
    moved the console script, or the file predates the worker flag) is rewritten
    and the job reloaded, keeping the environment the installing shell
    forwarded — this shell may lack the proxy variables that one had. ``restart``
    restarts a running job even when nothing changed, for a setting the menu bar
    only reads at launch.

    A service the user switched off under Login Items & Extensions is still
    started: macOS itself allows the manual start and reserves that switch for
    the start at login, so refusing would be stricter than the OS. The result's
    ``switched_off`` lets the caller say that it will not come back at login.
    Returns the action taken (``first-setup``, ``started``, ``restarted`` or
    ``already-running``) with the paths and the pid.
    """
    _require_macos()
    program = program or resolve_program()
    login_plist = plist_path(label, home)
    session_plist = session_plist_path(label, backup_dir)
    out_log, err_log = log_paths(label, home)

    first_setup = not login_plist.exists() and not session_plist.exists()
    if first_setup or login_plist.exists():
        target = login_plist
        if session_plist.exists():
            session_plist.unlink()
    else:
        target = session_plist
    target.parent.mkdir(parents=True, exist_ok=True)
    out_log.parent.mkdir(parents=True, exist_ok=True)

    wanted = _plist_dict(program, label, home)
    existing = _load_plist(target)
    repaired = False
    if existing is None:
        # Owner-only and atomic, not the umask's 0644 via write_bytes: a
        # forwarded proxy URL can carry credentials, and a write cut short by
        # Ctrl-C must not leave a truncated plist for the next login to choke on.
        atomic_write_bytes(target, plistlib.dumps(wanted))
        repaired = not first_setup
    elif existing.get("ProgramArguments") != wanted["ProgramArguments"]:
        env = dict(existing.get("EnvironmentVariables") or {})
        env["PATH"] = wanted["EnvironmentVariables"]["PATH"]
        wanted["EnvironmentVariables"] = env
        atomic_write_bytes(target, plistlib.dumps(wanted))
        repaired = True

    loaded, _state, pid = _job_state(label, uid)
    switched_off = False
    # A running worker of an older build is restarted onto this one: that is
    # what makes "ensure" cover an upgrade made by `uv tool upgrade` by hand.
    stale_build = (
        pid is not None and not repaired and not restart
        and worker_build_is_stale(pid, backup_dir)
    )
    if pid is not None and not repaired and not restart and not stale_build:
        action = "already-running"
    else:
        # Asked only when something is about to start: the read takes ~2 s and
        # a running service has nothing new to say about the login start.
        switched_off = switched_off_in_system_settings(label, uid)
        action = "restarted" if pid is not None else ("first-setup" if first_setup else "started")
        if repaired or not loaded:
            _reload(target, label, uid)
        else:
            kickstart(label, uid, restart=pid is not None)
        pid = _wait_for_pid(label, uid)

    return {
        "label": label,
        "action": action,
        "start_at_login": target == login_plist,
        "plist": str(target),
        "program": wanted["ProgramArguments"],
        "stdout_log": str(out_log),
        "stderr_log": str(err_log),
        "pid": pid,
        "switched_off": switched_off,
        "stale_build": stale_build,
    }


def install(
    label: str = LABEL,
    home: Path | None = None,
    program: list[str] | None = None,
    uid: int | None = None,
    backup_dir: Path | None = None,
) -> dict:
    """Write the login plist from this shell's environment and (re)load it.

    The explicit form of first setup, and the way back to starting at login
    after the menu's toggle moved the plist out: a session-only plist is removed
    and the login one written fresh. Idempotent: an already-loaded service is
    booted out first, so running this after an upgrade re-reads the plist
    instead of failing with launchd's "service already loaded" (EEXIST, code 5).
    """
    _require_macos()
    program = program or resolve_program()
    target_plist = plist_path(label, home)
    out_log, err_log = log_paths(label, home)

    target_plist.parent.mkdir(parents=True, exist_ok=True)
    out_log.parent.mkdir(parents=True, exist_ok=True)
    # A reinstall from a shell in another profile silently redirects the
    # service; report what moved so the caller can say so. The previous
    # configuration is in whichever home the plist currently has: the data
    # directory while Start at login is off.
    session_plist = session_plist_path(label, backup_dir)
    wanted = _plist_dict(program, label, home)
    previous = _load_plist(target_plist) or _load_plist(session_plist)
    profile_changes: list[tuple[str, str | None, str | None]] = []
    if previous is not None:
        old_env = previous.get("EnvironmentVariables") or {}
        new_env = wanted["EnvironmentVariables"]
        profile_changes = [
            (key, old_env.get(key), new_env.get(key))
            for key in _PROFILE_ENV_KEYS
            if old_env.get(key) != new_env.get(key)
        ]
    # Owner-only and atomic, not the umask's 0644 via write_bytes: a forwarded
    # proxy URL can carry credentials, and a write cut short by Ctrl-C must
    # not leave a truncated plist for the next login to choke on.
    atomic_write_bytes(target_plist, plistlib.dumps(wanted))
    if session_plist.exists():
        session_plist.unlink()

    _reload(target_plist, label, uid)

    return {
        "label": label,
        "plist": str(target_plist),
        "program": wanted["ProgramArguments"],
        "start_at_login": True,
        "stdout_log": str(out_log),
        "stderr_log": str(err_log),
        "profile_changes": profile_changes,
    }


def uninstall(
    label: str = LABEL,
    home: Path | None = None,
    uid: int | None = None,
    backup_dir: Path | None = None,
) -> dict:
    """Stop the service and remove every trace the menu bar left on the Mac.

    That is the plist in either home, launchd's own record of the service
    (the "Allow in the Background" switch leaves a disabled override that
    would otherwise outlive the plist), the wrapper ``.app`` the menu bar built
    for itself, and the two log files. Tolerates every partial state — loaded
    without a plist, a plist that was never bootstrapped, neither — because
    the point of an uninstall is to arrive at "gone", not to insist on the
    path taken to get there. A bundle that cannot be deleted is reported, not
    fatal: the service itself is already gone by then.
    """
    _require_macos()
    was_loaded = is_loaded(label, uid)
    if was_loaded:
        booted_out = _launchctl("bootout", service_target(label, uid))
        if booted_out.returncode != 0 and is_loaded(label, uid):
            detail = (booted_out.stderr or booted_out.stdout or "").strip()
            raise ClaudeSwitchError(
                f"launchctl bootout failed (exit {booted_out.returncode})"
                + (f": {detail}" if detail else "")
            )

    cleared_override = False
    if _launchd_disabled(label, uid):
        cleared_override = _launchctl("enable", service_target(label, uid)).returncode == 0

    removed_paths: list[Path] = []
    for candidate in (plist_path(label, home), session_plist_path(label, backup_dir)):
        if candidate.exists():
            candidate.unlink()
            removed_paths.append(candidate)
    for log in log_paths(label, home):
        if log.exists():
            log.unlink()
            removed_paths.append(log)
    stamp = worker_stamp_path(backup_dir)
    if stamp.exists():
        stamp.unlink()
        removed_paths.append(stamp)

    from claude_swap import app_bundle

    if backup_dir is None:
        from claude_swap.paths import get_backup_root

        backup_dir = get_backup_root()
    bundle_error: str | None = None
    try:
        removed_paths.extend(app_bundle.remove(backup_dir))
    except OSError as exc:
        bundle_error = str(exc)

    return {
        "label": label,
        "was_loaded": was_loaded,
        "removed_plist": any(p.suffix == ".plist" for p in removed_paths),
        "removed": [str(p) for p in removed_paths],
        "cleared_override": cleared_override,
        "bundle_error": bundle_error,
    }


def set_start_at_login(
    enabled: bool,
    label: str = LABEL,
    home: Path | None = None,
    backup_dir: Path | None = None,
    program: list[str] | None = None,
) -> dict:
    """Record the Start-at-login preference by moving the plist between its homes.

    Only the file moves; launchd is not touched. The running job keeps its
    in-memory definition either way, the next login scans only
    ``~/Library/LaunchAgents``, and the menu calls this from inside the very
    process a bootout would kill. With no plist anywhere (the menu bar was run
    with ``--foreground`` from a shell) a fresh one is written at the chosen
    location, so the choice is remembered.
    """
    _require_macos()
    login_plist = plist_path(label, home)
    session_plist = session_plist_path(label, backup_dir)
    source, destination = (
        (session_plist, login_plist) if enabled else (login_plist, session_plist)
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.exists():
        atomic_write_bytes(destination, source.read_bytes())
        source.unlink()
    elif not destination.exists():
        atomic_write_bytes(destination, build_plist(program, label, home))
    return {"label": label, "start_at_login": enabled, "plist": str(destination)}
