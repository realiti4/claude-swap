"""Session mode: run Claude Code as a stored account in one terminal.

``cswap run NUM|EMAIL`` launches Claude Code with ``CLAUDE_CONFIG_DIR``
pointing at a persistent per-account profile under
``<backup_dir>/sessions/<num>-<email-slug>/``, leaving the default
``~/.claude/`` login (and every other terminal, plus the VS Code extension)
untouched. ``CLAUDE_CONFIG_DIR`` fully isolates Claude Code's config and
credential lookup; on macOS, Claude hashes the (NFC-normalized) env var value
into its keychain service name, so each profile gets its own keychain entry.

Profiles are seeded with a plaintext ``.credentials.json`` — deliberate,
including on macOS: the plaintext fallback is Claude's only credential
mechanism on Linux (a stable contract), and Claude migrates it into its
hashed keychain entry on first write. Writing that keychain entry ourselves
would couple us to Claude's internal storage format and naming, where a
mismatch is a hard "logged out" failure instead of a harmless stale entry.

Sharing: by default the user's ``settings.json``, ``keybindings.json``,
``CLAUDE.md``, ``skills/``, ``commands/``, and ``agents/`` follow them into
the session profile — symlinks on macOS/Linux (Claude's settings writer
detects symlinks and writes through to the target, so in-session ``/config``
changes land in ``~/.claude``), copies re-synced on every launch on Windows.
A manifest records what cswap created so removal never touches user data.

This module must not import ``switcher`` (switcher imports us for the
session-aware guards); it receives a ``ClaudeAccountSwitcher`` instance.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from claude_swap import macos_keychain
from claude_swap.exceptions import SessionError
from claude_swap.macos_keychain import KeychainError
from claude_swap.locking import FileLock
from claude_swap.models import Platform
from claude_swap.oauth import refresh_oauth_credentials
from claude_swap.printer import accent, dimmed, muted, warning
from claude_swap.process_detection import ClaudeSession, list_sessions

if TYPE_CHECKING:
    from claude_swap.switcher import ClaudeAccountSwitcher

# Items mirrored from ~/.claude into session profiles when sharing is on.
# Deliberately excludes anything account- or instance-scoped: plugins/,
# projects/ (per-account history is a feature), sessions/, ide/,
# .claude.json, .credentials.json, statsig/ and other telemetry.
SHARED_ITEMS = (
    "settings.json",
    "keybindings.json",
    "CLAUDE.md",
    "skills",
    "commands",
    "agents",
)

# Records which entries in a session profile cswap created (so --no-share and
# re-syncs only ever remove cswap-managed links/copies, never user data).
SHARE_MANIFEST = ".cswap-shared.json"

# Deferred-invalidation marker: backup credentials changed while a session was
# live (we never pull credentials out from under a running claude), so the
# profile must be re-bootstrapped on the next non-live `cswap run` even if it
# still passes the local reuse check.
STALE_MARKER = ".cswap-stale-credentials"


def mark_session_stale(session_dir: Path) -> None:
    """Flag a live session profile for re-bootstrap once it exits."""
    try:
        (session_dir / STALE_MARKER).touch()
    except OSError:
        pass  # best-effort; worst case the old reuse behavior applies

# Env vars that make claude bypass account OAuth entirely (verified against
# claude 2.1.175). Dropped from the auth-status probe (they'd fake "logged in"
# for the wrong reason) AND scrubbed from the session launch env with a
# warning: `cswap run N` is an explicit request for account N, so letting an
# exported API key silently hijack the session would defeat the command. The
# same-account fast path (plain claude, untouched env) does not scrub.
AUTH_OVERRIDE_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
    "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR",
)

# `claude auth status` is a local check (no API call) but spawns the full CLI.
_AUTH_STATUS_TIMEOUT = 10.0

# Bootstrap holds the backup-dir lock across one token refresh (10s network
# timeout) plus auth-status probes, so it needs more headroom than the
# default 10s acquire used by the switch paths.
_BOOTSTRAP_LOCK_TIMEOUT = 30.0


def slugify_email(email: str) -> str:
    """Filesystem-safe slug for an email address.

    Uniqueness comes from the ``<num>-`` slot prefix on the session dir, so
    this only needs to be safe (incl. Windows-forbidden chars), not injective.
    """
    normalized = unicodedata.normalize("NFC", email)
    return "".join(
        ch if (ch.isascii() and (ch.isalnum() or ch in "._-")) else "_"
        for ch in normalized
    )


def session_dir_for(backup_dir: Path, account_num: str, email: str) -> Path:
    """Session profile directory for an account.

    Note: the profile itself contains Claude's own ``sessions/<pid>.json``
    PID files, so full paths look like
    ``<backup>/sessions/2-user_x.com/sessions/1234.json`` — intentional.
    """
    return backup_dir / "sessions" / f"{account_num}-{slugify_email(email)}"


def keychain_service_name(session_dir: Path) -> str:
    """Keychain service name Claude Code derives for this config dir.

    Claude hashes the raw ``CLAUDE_CONFIG_DIR`` env var value, NFC-normalized
    and unresolved (claude src ``envUtils.ts``/``macOsKeychainHelpers.ts``).
    Hash exactly the string we export — never a resolved/realpath variant.
    """
    normalized = unicodedata.normalize("NFC", str(session_dir))
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:8]
    return f"Claude Code-credentials-{digest}"


def _keychain_account_name() -> str:
    """Keychain account name, mirroring Claude's ``getUsername()``.

    Delegates to :func:`macos_keychain.keychain_account_name` so session profiles
    and the active store derive the account name identically.
    """
    return macos_keychain.keychain_account_name()


def delete_macos_keychain_entry(session_dir: Path) -> None:
    """Best-effort delete of a session profile's hashed keychain entry.

    No-op off macOS. Needed before seeding (Claude reads the keychain before
    the plaintext file, so a stale entry would shadow a fresh seed) and on
    profile removal (once the dir is gone the hashed name is unrecoverable).
    """
    if Platform.detect() != Platform.MACOS:
        return
    try:
        macos_keychain.delete_password(
            keychain_service_name(session_dir), _keychain_account_name()
        )
    except KeychainError:
        pass  # best-effort; absent entry is already success (rc 44)


def live_sessions_for(session_dir: Path) -> list[ClaudeSession]:
    """Live Claude instances running against a session profile."""
    if not session_dir.exists():
        return []
    return list_sessions(claude_dir=session_dir)


def _probe_env(session_dir: Path) -> dict[str, str]:
    """Env for the auth-status probe: session config dir, auth overrides dropped."""
    env = {k: v for k, v in os.environ.items() if k not in AUTH_OVERRIDE_ENV_VARS}
    env["CLAUDE_CONFIG_DIR"] = str(session_dir)
    return env


class SessionManager:
    """Bootstraps per-account session profiles and launches Claude into them."""

    def __init__(self, switcher: ClaudeAccountSwitcher):
        self.switcher = switcher
        self.sessions_dir = switcher.backup_dir / "sessions"
        self._logger = switcher._logger

    # -- launch ----------------------------------------------------------

    def run(self, identifier: str, claude_args: list[str], share: bool = True) -> NoReturn:
        """Launch Claude Code as the given account in the current terminal."""
        claude_bin = shutil.which("claude")
        if not claude_bin:
            raise SessionError(
                "'claude' was not found on PATH. Install Claude Code first."
            )

        account_num, email, org_uuid = self.switcher.resolve_account(identifier)
        # Guard before the same-account direct-launch fast path below (which
        # _exec's claude and never returns) — and before setup_session.
        self._ensure_not_api_key(account_num, email)

        config_dir_preset = os.environ.get("CLAUDE_CONFIG_DIR")
        if config_dir_preset:
            # With CLAUDE_CONFIG_DIR set, "current default account" is
            # meaningless (we may already be inside a session terminal), so
            # the same-account fast path below must not trigger.
            warning(
                f"CLAUDE_CONFIG_DIR is already set ({config_dir_preset}); "
                "overriding it for this launch."
            )
        else:
            # Same-account fast path: never create a second credential copy
            # for the account that is already the active default login —
            # two copies of one account can drift if the server rotates the
            # refresh token.
            current = self.switcher._get_current_account()
            if current is not None and current == (email, org_uuid):
                print(
                    dimmed(
                        f"Account-{account_num} ({email}) is already the active "
                        "default login — launching claude directly."
                    )
                )
                self._exec(claude_bin, claude_args, env=dict(os.environ))

        scrubbed = [v for v in AUTH_OVERRIDE_ENV_VARS if os.environ.get(v)]
        if scrubbed:
            warning(
                f"Ignoring {', '.join(scrubbed)} for this session — it would "
                f"override the selected account inside Claude Code."
            )

        session_dir, account_num, email = self.setup_session(identifier, share)

        print(
            f"{accent('Launching')} Account-{account_num} ({email}) "
            f"{muted('[session mode]')}"
        )
        env = {
            k: v for k, v in os.environ.items() if k not in AUTH_OVERRIDE_ENV_VARS
        }
        env["CLAUDE_CONFIG_DIR"] = str(session_dir)
        self._exec(claude_bin, claude_args, env=env)

    def _exec(self, claude_bin: str, claude_args: list[str], env: dict[str, str]) -> NoReturn:
        """Hand the terminal over to claude. Never returns.

        POSIX: ``execvpe`` replaces the cswap process entirely (the lock is
        already released — an exec'd claude must never inherit a held flock).
        Windows: ``os.exec*`` detaches from the console confusingly, so stay
        resident as a thin wrapper and mirror claude's exit code.
        """
        argv = [claude_bin, *claude_args]
        if sys.platform == "win32":
            try:
                rc = subprocess.run(argv, env=env).returncode
            except KeyboardInterrupt:
                rc = 130  # Ctrl+C went to claude; just mirror the exit
            sys.exit(rc)
        os.execvpe(claude_bin, argv, env)
        raise AssertionError("unreachable")  # pragma: no cover

    def _ensure_not_api_key(self, account_num: str, email: str) -> None:
        """Reject API-key accounts in session mode (not supported yet).

        Session bootstrap is OAuth-shaped — it seeds ``.credentials.json`` and
        ``_is_session_valid`` requires ``authMethod == "claude.ai"`` — so an API-key
        account would otherwise fail validation opaquely. Raise early with guidance.
        """
        if self.switcher._account_kind(account_num) == "api_key":
            raise SessionError(
                f"Account-{account_num} ({email}) is an API-key account; "
                "'cswap run' (session mode) does not support API-key accounts yet. "
                "Use 'cswap --switch-to' to make it your default login instead."
            )

    # -- bootstrap -------------------------------------------------------

    def setup_session(self, identifier: str, share: bool) -> tuple[Path, str, str]:
        """Ensure a valid session profile exists; returns (dir, num, email)."""
        account_num, email, org_uuid = self.switcher.resolve_account(identifier)
        # Defense-in-depth: also guard here (run() guards before its fast path).
        self._ensure_not_api_key(account_num, email)
        session_dir = session_dir_for(self.switcher.backup_dir, account_num, email)

        # Deferred invalidation: backup credentials changed while this profile
        # was live, so its credentials are presumed stale even if they still
        # pass the local reuse check. Honored only when no session is live —
        # a second `cswap run` joining a live session must not invalidate
        # under the running claude (the marker survives for later).
        stale = (session_dir / STALE_MARKER).exists() and not live_sessions_for(
            session_dir
        )

        # Cheap reuse check without the lock: most launches hit this.
        if not stale and self._is_session_valid(session_dir, email, org_uuid):
            self._sync_sharing(session_dir, share)
            return session_dir, account_num, email

        with FileLock(self.switcher.lock_file, timeout=_BOOTSTRAP_LOCK_TIMEOUT):
            # Re-evaluate the marker under the lock, then re-check validity:
            # another `cswap run` may have bootstrapped while we waited.
            if (session_dir / STALE_MARKER).exists() and not live_sessions_for(
                session_dir
            ):
                self.switcher._invalidate_session_credentials(account_num, email)
                (session_dir / STALE_MARKER).unlink(missing_ok=True)
            if self._is_session_valid(session_dir, email, org_uuid):
                self._sync_sharing(session_dir, share)
                return session_dir, account_num, email

            self._bootstrap(session_dir, account_num, email, org_uuid)
            self._sync_sharing(session_dir, share)

            if not self._is_session_valid(session_dir, email, org_uuid):
                self._cleanup_failed_session(session_dir)
                raise SessionError(
                    f"Session profile for Account-{account_num} ({email}) failed "
                    f"validation. Log in with that account and re-add it: "
                    f"cswap --add-account --slot {account_num}"
                )
        # Lock released here, before any exec.

        return session_dir, account_num, email

    def _bootstrap(
        self, session_dir: Path, account_num: str, email: str, org_uuid: str
    ) -> None:
        """Seed the session profile from backup storage. Caller holds the lock."""
        # Claude reads the keychain before the plaintext file — a stale hashed
        # entry from an earlier profile at this path would shadow the seed.
        delete_macos_keychain_entry(session_dir)

        creds = self.switcher.read_account_credentials(account_num, email)
        if not creds:
            raise SessionError(
                f"Account-{account_num} has no stored credentials. "
                f"Re-add with: cswap --add-account --slot {account_num}"
            )

        # One refresh so the profile starts with a fresh access token; persist
        # a possibly-rotated refresh token back to backup so future switches
        # and runs see the latest. Failure is non-fatal: the stored token may
        # still be valid, and claude refreshes on its own at runtime.
        # Setup-token accounts (--add-token) have no refresh token by design —
        # skip silently instead of warning about a flow that can't happen.
        if self._has_refresh_token(creds):
            refreshed = refresh_oauth_credentials(creds)
            if refreshed:
                creds = refreshed
                self.switcher.write_account_credentials(account_num, email, creds)
            else:
                warning(
                    f"Could not refresh the token for Account-{account_num}; "
                    "continuing with the stored credentials."
                )

        config_text = self.switcher.read_account_config(account_num, email)
        try:
            config_data = json.loads(config_text) if config_text else {}
        except json.JSONDecodeError:
            config_data = {}
        oauth_account = config_data.get("oauthAccount")
        if not oauth_account:
            raise SessionError(
                f"Account-{account_num} has no stored config backup. "
                f"Re-add with: cswap --add-account --slot {account_num}"
            )

        session_dir.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            os.chmod(session_dir, 0o700)

        creds_path = session_dir / ".credentials.json"
        creds_path.write_text(creds, encoding="utf-8")
        if sys.platform != "win32":
            os.chmod(creds_path, 0o600)

        # Merge the identity seed into any existing .claude.json so a
        # re-bootstrap preserves the profile's own projects/history. The
        # `theme` key is load-bearing: claude shows onboarding when
        # `!config.theme || !config.hasCompletedOnboarding`.
        config_path = session_dir / ".claude.json"
        existing: dict = {}
        if config_path.exists():
            try:
                existing = json.loads(config_path.read_text(encoding="utf-8")) or {}
            except (json.JSONDecodeError, OSError):
                existing = {}
        existing["oauthAccount"] = oauth_account
        existing["hasCompletedOnboarding"] = True
        existing.setdefault("theme", config_data.get("theme") or "dark")
        config_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        if sys.platform != "win32":
            os.chmod(config_path, 0o600)

        self._logger.info(
            f"Bootstrapped session profile for account {account_num} at {session_dir}"
        )

    @staticmethod
    def _has_refresh_token(creds: str) -> bool:
        try:
            return bool(json.loads(creds).get("claudeAiOauth", {}).get("refreshToken"))
        except (json.JSONDecodeError, AttributeError):
            return True  # unknown shape — let the refresh attempt decide

    def _cleanup_failed_session(self, session_dir: Path) -> None:
        # Keychain first: claude may have partially migrated the seed, and the
        # hashed service name can't be recomputed once the dir is gone.
        delete_macos_keychain_entry(session_dir)
        shutil.rmtree(session_dir, ignore_errors=True)

    # -- validation ------------------------------------------------------

    def _is_session_valid(self, session_dir: Path, email: str, org_uuid: str) -> bool:
        """Whether claude sees the profile as logged in with the right identity.

        Local check only (`claude auth status` makes no API call): a revoked
        but unexpired token still passes and fails on first real use.
        """
        if not session_dir.is_dir():
            return False
        # On Windows `claude` is a `.cmd` shim, and a bare "claude" passed to
        # subprocess won't resolve it (PATHEXT isn't applied) — it raises
        # FileNotFoundError, which the handler below turns into a false
        # "failed validation". shutil.which finds the shim.
        claude_bin = shutil.which("claude") or "claude"
        try:
            result = subprocess.run(
                [claude_bin, "auth", "status", "--json"],
                env=_probe_env(session_dir),
                capture_output=True,
                text=True,
                timeout=_AUTH_STATUS_TIMEOUT,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        if result.returncode != 0:
            return False
        try:
            status = json.loads(result.stdout)
        except json.JSONDecodeError:
            return False
        if status.get("loggedIn") is not True:
            return False
        # Verified against claude 2.1.175; an env API key reports a different
        # method, and the probe env already drops those vars anyway.
        if status.get("authMethod") != "claude.ai":
            return False
        if status.get("email") != email:
            return False
        # Lenient org check: only when both sides have a value, so schema
        # drift degrades to email-only validation instead of false negatives.
        status_org = status.get("orgId")
        if status_org and org_uuid and status_org != org_uuid:
            return False
        return True

    # -- sharing ---------------------------------------------------------

    def _sync_sharing(self, session_dir: Path, share: bool) -> None:
        """Mirror SHARED_ITEMS from ~/.claude into the profile (or undo it).

        Idempotent; runs on every launch. Deliberately sources from the
        default ``~/.claude`` (not ``get_claude_config_home()``): sharing
        always mirrors the default profile, even when ``CLAUDE_CONFIG_DIR``
        is set in the invoking environment. Lock-free on the reuse path:
        concurrent ``run`` vs ``run --no-share`` is last-writer-wins and
        self-heals on the next launch.
        """
        if not session_dir.is_dir():
            return
        source_root = Path.home() / ".claude"
        manifest_path = session_dir / SHARE_MANIFEST
        managed = self._read_manifest(manifest_path)

        if not share:
            for name in managed:
                self._remove_managed(session_dir / name)
            manifest_path.unlink(missing_ok=True)
            return

        use_symlinks = self.switcher.platform != Platform.WINDOWS
        new_managed: list[str] = []

        for name in SHARED_ITEMS:
            src = source_root / name
            dest = session_dir / name

            if not src.exists():
                # Source vanished (or never existed): prune our own entry.
                if name in managed:
                    self._remove_managed(dest)
                continue

            if dest.is_symlink():
                if name not in managed:
                    managed = [*managed, name]  # adopt: only cswap links here
                if use_symlinks:
                    try:
                        if dest.readlink() != src:
                            dest.unlink()
                            dest.symlink_to(src)
                    except OSError:
                        continue
                    new_managed.append(name)
                    continue
                # Platform moved POSIX → Windows: replace link with a copy.
                dest.unlink()
            elif dest.exists() and name not in managed:
                # Pre-existing user data in the profile — never touch it.
                print(
                    dimmed(
                        f"Not sharing {name}: the session profile already has "
                        "its own copy."
                    )
                )
                continue

            try:
                if use_symlinks:
                    if dest.exists():
                        self._remove_managed(dest)
                    dest.symlink_to(src)
                else:
                    if dest.exists():
                        self._remove_managed(dest)
                    if src.is_dir():
                        shutil.copytree(src, dest)
                    else:
                        shutil.copy2(src, dest)
            except OSError as e:
                self._logger.warning(f"Failed to share {name} into session: {e}")
                continue
            new_managed.append(name)

        # Anything we managed before but no longer created gets removed above;
        # write the manifest atomically so a concurrent reader never sees a
        # truncated file.
        self._write_manifest(manifest_path, new_managed)

    @staticmethod
    def _read_manifest(manifest_path: Path) -> list[str]:
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            items = data.get("items", [])
            # Only ever act on names we could have created.
            return [i for i in items if i in SHARED_ITEMS]
        except (OSError, json.JSONDecodeError, AttributeError):
            return []

    def _write_manifest(self, manifest_path: Path, items: list[str]) -> None:
        mode = "symlink" if self.switcher.platform != Platform.WINDOWS else "copy"
        payload = json.dumps({"items": items, "mode": mode}, indent=2)
        fd, tmp = tempfile.mkstemp(
            dir=str(manifest_path.parent), prefix=".cswap-shared-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, manifest_path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    @staticmethod
    def _remove_managed(dest: Path) -> None:
        """Remove a cswap-created share entry (link or copy), never user data
        beyond it — callers guarantee `dest` is manifest-listed or a symlink."""
        try:
            if dest.is_symlink() or dest.is_file():
                dest.unlink(missing_ok=True)
            elif dest.is_dir():
                shutil.rmtree(dest, ignore_errors=True)
        except OSError:
            pass
