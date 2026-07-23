"""Explicit recovery for an expired credential owned by a live Claude profile.

Recovery never rotates OAuth credentials itself. It asks Claude Code to make one
small noninteractive request in the profile that already owns the token, then
re-reads that profile. ``recovered`` means that same identity-matched owner now
has a non-expired OAuth credential; normal list/status collection observes
usage through its own claim protocol. Backups are intentionally absent from this
module: a session profile may hold a newer refresh-token generation than its
slot backup.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Protocol

from claude_swap import oauth
from claude_swap.credentials import ActiveCredentials, looks_like_api_key
from claude_swap.json_output import SCHEMA_VERSION
from claude_swap.locking import FileLock
from claude_swap.paths import (
    get_default_claude_config_home,
    get_default_global_config_path,
)
from claude_swap.process_detection import get_running_instances
from claude_swap.session import (
    AUTH_OVERRIDE_ENV_VARS,
    live_sessions_for,
    read_session_identity,
    read_session_owner_credentials,
)

RecoveryStatus = Literal[
    "recovered",
    "not_needed",
    "retry_later",
    "human_required",
]

CANARY_PROMPT = "Reply with exactly OK."
CANARY_TIMEOUT_S = 60.0
CANARY_TERMINATE_GRACE_S = 2.0

# Only process-launch essentials cross the boundary. In particular, proxy,
# provider, model, and auth variables do not reach the canary.
_POSIX_ENV_ALLOWLIST = frozenset({
    "HOME",
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
})
_WINDOWS_ENV_ALLOWLIST = frozenset({
    "APPDATA",
    "COMSPEC",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "LOCALAPPDATA",
    "PATH",
    "PATHEXT",
    "PROGRAMDATA",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "WINDIR",
})


class _UsageStore(Protocol):
    def entries(self, identities: dict[str, tuple[str, str]], models=()): ...


class _Switcher(Protocol):
    backup_dir: Path
    _usage_store: _UsageStore

    def _account_kind(self, account_num: str | None) -> str: ...

    def _get_sequence_data(self) -> dict | None: ...

    def _read_default_profile_credentials(self) -> ActiveCredentials: ...


@dataclass(frozen=True)
class _Owner:
    kind: Literal["default", "session"]
    config_dir: Path | None


@dataclass(frozen=True)
class _OwnerSnapshot:
    owner: _Owner
    credential_fingerprint: str
    credential_state: CredentialState


CanaryStatus = Literal["exited", "timed_out", "start_failed"]
CredentialState = Literal["expired", "not_needed", "human_required"]


def _envelope(account_num: str, status: RecoveryStatus) -> dict:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "operation": "recover",
        "accountNumber": int(account_num),
        "recoveryStatus": status,
    }


def _read_default_identity() -> tuple[str, str] | None:
    """Read the default profile identity without honoring config-dir overrides."""
    try:
        data = json.loads(get_default_global_config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    account = data.get("oauthAccount")
    if not isinstance(account, dict):
        return None
    email = account.get("emailAddress")
    if not isinstance(email, str) or not email:
        return None
    org_uuid = account.get("organizationUuid") or ""
    if not isinstance(org_uuid, str):
        return None
    return email, org_uuid


def _same_identity(
    actual: tuple[str, str] | None, email: str, org_uuid: str
) -> bool:
    return actual == (email, org_uuid or "")


def _find_owner(
    switcher: _Switcher,
    account_num: str,
    email: str,
    org_uuid: str,
) -> tuple[_Owner | None, RecoveryStatus | None]:
    """Return the one live profile allowed to own recovery, failing closed."""
    try:
        default_sessions, default_ides = get_running_instances(
            get_default_claude_config_home()
        )
        default_live = bool(default_sessions or default_ides)
        default_identity = _read_default_identity()

        # Keep path construction centralized with session mode, including its
        # platform-safe email slug.
        from claude_swap.session import session_dir_for

        session_dir = session_dir_for(switcher.backup_dir, account_num, email)
        session_live = bool(live_sessions_for(session_dir))
        session_identity = read_session_identity(session_dir) if session_live else None
    except Exception:
        return None, "retry_later"

    # A live profile with unreadable identity cannot be safely attributed.
    if default_live and default_identity is None:
        return None, "human_required"
    if session_live and not _same_identity(session_identity, email, org_uuid):
        return None, "human_required"

    candidates: list[_Owner] = []
    if default_live and _same_identity(default_identity, email, org_uuid):
        candidates.append(_Owner("default", None))
    if session_live:
        candidates.append(_Owner("session", session_dir))

    if len(candidates) != 1:
        return None, "human_required"

    owner = candidates[0]
    if owner.kind == "default":
        # A default owner is valid only for the account currently named by the
        # default profile; the equality above is deliberately strict on org.
        return owner, None

    # Session recovery is only for an inactive slot. If default identity is
    # unreadable, inactivity cannot be proven; if it names the target, this is
    # an active slot with the wrong owner shape.
    if default_identity is None or _same_identity(default_identity, email, org_uuid):
        return None, "human_required"
    return owner, None


def _read_owner_credentials(
    switcher: _Switcher, owner: _Owner
) -> ActiveCredentials:
    if owner.kind == "default":
        return switcher._read_default_profile_credentials()
    assert owner.config_dir is not None
    return read_session_owner_credentials(owner.config_dir)


def _credential_state(
    switcher: _Switcher, account_num: str, credentials: str
) -> CredentialState:
    """Classify local bytes without calling OAuth endpoints."""
    if switcher._account_kind(account_num) == "api_key" or looks_like_api_key(
        credentials
    ):
        return "human_required"
    data = oauth.extract_oauth_data(credentials)
    if not data:
        return "human_required"
    scopes = data.get("scopes")
    if (
        isinstance(scopes, list)
        and {scope for scope in scopes if isinstance(scope, str)} == {"user:inference"}
        and len(scopes) == 1
    ):
        return "human_required"
    if not isinstance(data.get("refreshToken"), str) or not data.get("refreshToken"):
        return "human_required"
    if not isinstance(data.get("accessToken"), str) or not data.get("accessToken"):
        return "human_required"
    expires_at = data.get("expiresAt")
    if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
        return "human_required"
    if not oauth.is_oauth_token_expired(expires_at):
        return "not_needed"
    return "expired"


def _canary_environment(config_dir: Path | None) -> dict[str, str]:
    allowlist = _WINDOWS_ENV_ALLOWLIST if sys.platform == "win32" else _POSIX_ENV_ALLOWLIST
    env = {key: value for key, value in os.environ.items() if key in allowlist}
    for key in AUTH_OVERRIDE_ENV_VARS:
        env.pop(key, None)
    # Absence selects the default profile. A session owner gets exactly its
    # canonical cswap profile path.
    if config_dir is None:
        env.pop("CLAUDE_CONFIG_DIR", None)
    else:
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    return env


def _canary_argv(claude_bin: str) -> list[str]:
    return [
        claude_bin,
        "-p",
        CANARY_PROMPT,
        "--safe-mode",
        "--no-session-persistence",
        "--output-format",
        "json",
        "--max-turns",
        "1",
        "--tools",
        "",
        "--permission-mode",
        "bypassPermissions",
        "--model",
        "haiku",
        "--effort",
        "low",
    ]


def _wait_quietly(process: subprocess.Popen, timeout: float) -> bool:
    try:
        process.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False
    except Exception:
        try:
            return process.poll() is not None
        except Exception:
            return False


def _terminate_posix_tree(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (OSError, ValueError):
        try:
            process.terminate()
        except OSError:
            pass
    if _wait_quietly(process, CANARY_TERMINATE_GRACE_S):
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ValueError):
        try:
            process.kill()
        except OSError:
            pass
    _wait_quietly(process, CANARY_TERMINATE_GRACE_S)


def _taskkill_tree(pid: int, *, force: bool) -> None:
    argv = ["taskkill", "/PID", str(pid), "/T"]
    if force:
        argv.append("/F")
    try:
        subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=CANARY_TERMINATE_GRACE_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _terminate_windows_tree(process: subprocess.Popen) -> None:
    try:
        process.send_signal(signal.CTRL_BREAK_EVENT)
    except (AttributeError, OSError, ValueError):
        _taskkill_tree(process.pid, force=False)
    if _wait_quietly(process, CANARY_TERMINATE_GRACE_S):
        return
    _taskkill_tree(process.pid, force=True)
    if not _wait_quietly(process, CANARY_TERMINATE_GRACE_S):
        try:
            process.kill()
        except OSError:
            pass
        _wait_quietly(process, CANARY_TERMINATE_GRACE_S)


def _terminate_process_tree(process: subprocess.Popen) -> None:
    if sys.platform == "win32":
        _terminate_windows_tree(process)
    else:
        _terminate_posix_tree(process)


class _CanarySignal(BaseException):
    def __init__(self, signum: int):
        self.signum = signum


def _can_install_canary_signal_guard() -> bool:
    return (
        sys.platform != "win32"
        and hasattr(signal, "SIGTERM")
        and threading.current_thread() is threading.main_thread()
    )


@contextmanager
def _canary_signal_guard(cleanup: Callable[[], None]):
    if not _can_install_canary_signal_guard():
        yield
        return

    installed: dict[int, object] = {}
    watched = [signal.SIGTERM]
    if hasattr(signal, "SIGINT"):
        watched.append(signal.SIGINT)

    def handler(signum, _frame):
        cleanup()
        raise _CanarySignal(signum)

    try:
        for signum in watched:
            installed[signum] = signal.getsignal(signum)
            signal.signal(signum, handler)
    except (AttributeError, OSError, RuntimeError, ValueError):
        for signum, previous in installed.items():
            signal.signal(signum, previous)
        yield
        return

    try:
        yield
    finally:
        for signum, previous in installed.items():
            signal.signal(signum, previous)


def _propagate_canary_signal(exc: _CanarySignal) -> None:
    if exc.signum == getattr(signal, "SIGINT", None):
        raise KeyboardInterrupt
    raise SystemExit(128 + exc.signum)


def run_canary(config_dir: Path | None) -> CanaryStatus:
    """Run the fixed Claude canary with no terminal and bounded tree cleanup."""
    claude_bin = shutil.which("claude")
    if not claude_bin:
        return "start_failed"

    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "env": _canary_environment(config_dir),
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
        )
    else:
        kwargs["start_new_session"] = True

    process: subprocess.Popen | None = None
    terminated = False

    def cleanup_process() -> None:
        nonlocal terminated
        if terminated or process is None:
            return
        terminated = True
        try:
            if process.poll() is not None:
                return
        except Exception:
            pass
        _terminate_process_tree(process)

    with tempfile.TemporaryDirectory(prefix="cswap-recover-") as cwd:
        kwargs["cwd"] = cwd
        try:
            process = subprocess.Popen(_canary_argv(claude_bin), **kwargs)
            with _canary_signal_guard(cleanup_process):
                process.wait(timeout=CANARY_TIMEOUT_S)
            return "exited"
        except subprocess.TimeoutExpired:
            cleanup_process()
            return "timed_out"
        except _CanarySignal as exc:
            _propagate_canary_signal(exc)
        except KeyboardInterrupt:
            cleanup_process()
            raise
        except Exception:
            cleanup_process()
            return "start_failed"


def _slot_still_matches(
    switcher: _Switcher, account_num: str, email: str, org_uuid: str
) -> bool:
    data = switcher._get_sequence_data() or {}
    account = data.get("accounts", {}).get(account_num)
    return bool(
        isinstance(account, dict)
        and account.get("email") == email
        and (account.get("organizationUuid") or "") == (org_uuid or "")
    )


def _owner_identity(owner: _Owner) -> tuple[str, str] | None:
    if owner.kind == "default":
        return _read_default_identity()
    assert owner.config_dir is not None
    return read_session_identity(owner.config_dir)


def _credential_fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _recovery_lock(switcher: _Switcher, account_num: str) -> FileLock:
    """A nonblocking, per-slot command lock distinct from the state lock.

    The lock file intentionally remains after release: deleting a flock path can
    create a second inode for a waiter and break exclusion. It contains no
    credential data and lives in claude-swap's private state directory.
    """
    lock_dir = switcher.backup_dir / "recovery-locks"
    lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if sys.platform != "win32":
        os.chmod(lock_dir, 0o700)
    return FileLock(lock_dir / f"{account_num}.lock", timeout=0.0)


def _snapshot_owner(
    switcher: _Switcher,
    account_num: str,
    email: str,
    org_uuid: str,
) -> tuple[_OwnerSnapshot | None, RecoveryStatus | None]:
    """Read exactly the owner state that must remain stable through launch."""
    if not _slot_still_matches(switcher, account_num, email, org_uuid):
        return None, "human_required"
    owner, owner_status = _find_owner(switcher, account_num, email, org_uuid)
    if owner is None:
        return None, owner_status or "human_required"
    if not _same_identity(_owner_identity(owner), email, org_uuid):
        return None, "human_required"
    credentials = _read_owner_credentials(switcher, owner)
    if credentials.keychain_unavailable or credentials.value is None:
        return None, "retry_later"
    state = _credential_state(switcher, account_num, credentials.value)
    return _OwnerSnapshot(owner, _credential_fingerprint(credentials.value), state), None


def _recover(
    switcher: _Switcher,
    account_num: str,
    email: str,
    org_uuid: str,
) -> RecoveryStatus:
    identity = {account_num: (email, org_uuid or "")}
    # Keep a known-dead credential out of Claude Code even if it is still in a
    # live owner profile. This is a cache read only; recovery never observes or
    # writes usage because that bypasses the collector's claim/fencing protocol.
    if switcher._usage_store.entries(identity)[account_num].token_dead():
        return "human_required"

    before, status = _snapshot_owner(switcher, account_num, email, org_uuid)
    if before is None:
        return status or "human_required"
    if before.credential_state != "expired":
        return before.credential_state

    # Re-read immediately before Popen. The command lock serializes recoveries
    # for this stable slot, but it cannot lock Claude Code's /login or another
    # external profile change. One after this final read is the narrow residual
    # race; post-canary identity verification below remains mandatory.
    launch, status = _snapshot_owner(switcher, account_num, email, org_uuid)
    if launch is None:
        return status or "human_required"
    if launch.owner != before.owner:
        return "human_required"
    if (
        launch.credential_fingerprint != before.credential_fingerprint
        or launch.credential_state != before.credential_state
    ):
        return (
            "human_required"
            if launch.credential_state == "human_required"
            else "retry_later"
        )

    canary_status = run_canary(launch.owner.config_dir)
    if canary_status == "start_failed":
        return "retry_later"

    # A canary may refresh before later timing out, so inspect after every
    # launched process. ``recovered`` is credential/identity proof only; list
    # and status perform the subsequent usage observation under their claims.
    after, status = _snapshot_owner(switcher, account_num, email, org_uuid)
    if after is None:
        return status or "human_required"
    if after.owner != launch.owner:
        return "human_required"
    if after.credential_state != "not_needed":
        return "retry_later"
    return "recovered"


def recover_account(
    switcher: _Switcher,
    account_num: str,
    email: str,
    org_uuid: str,
) -> dict:
    """Recover one resolved slot and return the stable, PII-free envelope."""
    if not account_num.isdigit() or int(account_num) <= 0:
        raise ValueError("account number must be positive")
    lock: FileLock | None = None
    try:
        lock = _recovery_lock(switcher, account_num)
        if not lock.acquire():
            status = "retry_later"
        else:
            if sys.platform != "win32":
                os.chmod(lock.lock_path, 0o600)
            status = _recover(switcher, account_num, email, org_uuid)
    except KeyboardInterrupt:
        raise
    except Exception:
        status = "retry_later"
    finally:
        if lock is not None:
            lock.release()
    return _envelope(account_num, status)
