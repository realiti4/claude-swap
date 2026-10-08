"""Account switcher for OpenAI Codex CLI logins (``cswap codex …``).

``CodexAccountSwitcher`` is the shared orchestration — roster, slot backups,
the switch transaction, usage collection, the consume gate — with Codex's
leaves swapped in through the provider hooks on ``ClaudeAccountSwitcher``:

- The live login is ``$CODEX_HOME/auth.json`` or Codex's Keychain item
  (:class:`~claude_swap.codex_store.CodexCredentialStore`).
- Identity is read offline from that login's id_token. There is no identity
  file beside it, so the ``oauthAccount`` config the shared paths store and
  check is synthesized from the credential itself.
- Codex takes no lock on its auth.json, so there are no CLI locks to hold.
- Codex refreshes (and rotates) its own live login; cswap never refreshes
  the active account, only inactive slot backups through the consume gate.

The store lives under ``<backup root>/codex``. Behavior is cited against the
Codex source (tag rust-v0.160.1).
"""

from __future__ import annotations

import contextlib
import getpass
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from claude_swap import codex_auth, codex_session, codex_store, oauth, process_detection
from claude_swap.codex_store import (
    FILE_STORE_OVERRIDE,
    CodexCredentialStore,
    CodexStoreUnsupported,
)
from claude_swap.exceptions import (
    ClaudeSwitchError,
    ConfigError,
    CredentialReadError,
    SessionError,
    SwitchError,
    ValidationError,
)
from claude_swap.json_output import (
    USAGE_NO_CREDENTIALS,
    USAGE_RELOGIN_REQUIRED,
    USAGE_TOKEN_EXPIRED,
)
from claude_swap.locking import FileLock
from claude_swap.models import get_timestamp
from claude_swap.paths import (
    CODEX_CLI_PREFIX,
    CODEX_DISPLAY_NAME,
    CODEX_SUBDIR,
    CODEX_TOKEN_KIND,
)
from claude_swap.printer import bolded, dimmed, warning
from claude_swap.switcher import ERROR_NOTES, SENTINEL_NOTES, ClaudeAccountSwitcher
from claude_swap.usage_store import FetchRecord

CODEX_SENTINEL_NOTES = {
    **SENTINEL_NOTES,
    USAGE_TOKEN_EXPIRED: (
        "token expired or rejected — open Codex to refresh it "
        f"(if it was revoked: {CODEX_CLI_PREFIX} add --login)"
    ),
    USAGE_RELOGIN_REQUIRED: (
        f"re-login needed — refresh token dead; run: {CODEX_CLI_PREFIX} add --login"
    ),
}

CODEX_ERROR_NOTES = {
    **ERROR_NOTES,
    "stash-unreadable": (
        "this slot's stashed successor is unreadable — unlock the keychain "
        f"or fix the file, then retry; `{CODEX_CLI_PREFIX} unclaimed` inspects it"
    ),
}

# A `.login-*` home older than this belongs to a login that died before its
# cleanup ran (device-code logins time out after 15 minutes; Codex
# login/src/device_code_auth.rs). Younger ones may be another terminal's
# login still in progress.
_STALE_LOGIN_HOME_S = 3600


class CodexAccountSwitcher(ClaudeAccountSwitcher):
    """Multi-account switcher for OpenAI Codex CLI logins."""

    provider_name = "codex"
    display_name = CODEX_DISPLAY_NAME
    short_name = CODEX_DISPLAY_NAME
    cli_prefix = CODEX_CLI_PREFIX
    # `codex login --with-api-key`'s auth.json, carried as one string.
    api_key_format = "Codex API-key auth.json string"
    token_kind = CODEX_TOKEN_KIND
    switch_notice = (
        "Codex processes already running keep the previous account — "
        "restart them to apply it."
    )
    # Identity lives in the login itself (there is no config file).
    live_config_missing = "No live Codex login found"
    # The menu bar title's prefix, so `cswap codex menubar` and Claude's
    # item (plain "⇄") are told apart.
    menubar_icon = "⇄ Codex"
    backup_subdir = CODEX_SUBDIR
    backup_keychain_service = "claude-swap-codex"
    run_legacy_migrations = False
    sentinel_notes = CODEX_SENTINEL_NOTES
    error_notes = CODEX_ERROR_NOTES

    def __init__(self, debug: bool = False):
        # Workspace display names fetched at add time, by account id. The
        # fetch is network, and the config synthesis that uses the name also
        # runs under the switch locks, so it only ever reads this.
        self._workspace_names: dict[str, str] = {}
        # A login captured outside the live store (`add_account_via_login`),
        # pinned for one shared add on this thread so it reads that login
        # wherever it would read auth.json. Thread-local: a TUI collect lane
        # on another thread must keep seeing the real live login.
        self._capture_tls = threading.local()
        # (live-key fingerprint, roster stat) -> API-key identity; see
        # `_api_key_identity`.
        self._api_key_identity_cache: tuple[tuple, dict | None] | None = None
        super().__init__(debug=debug)
        # Inside a `cswap codex run` session's shell CODEX_HOME is the
        # profile; every command (list, status, tui, watch, auto, …) must see
        # the user's real default home instead. The profile path is kept so
        # `cswap codex run` can still say what it replaced.
        self._default_home_unknown = False
        self.session_shell_home, known = codex_session.restore_default_home(
            self.backup_dir / "sessions"
        )
        if not known:
            self._distrust_default_home()

    def _distrust_default_home(self) -> None:
        """The default Codex home could not be determined (a session's shell
        lost the recorded one): ``~/.codex`` is used, and for the rest of this
        process the consume gate refreshes no stored login, since any of them
        may be the real default's live lineage."""
        self._default_home_unknown = True
        msg = (
            "CODEX_HOME pointed at a session profile and the default Codex home "
            f"could not be determined; using {codex_store.codex_home()} and "
            "refreshing no stored login in this run."
        )
        self._logger.warning(msg)
        warning(msg, file=sys.stderr)

    # -- live store, locks, environment ------------------------------------

    def _make_store(self) -> CodexCredentialStore:
        return CodexCredentialStore(self)

    def _live_credentials_lock(self):
        # Codex writes auth.json with no lock of its own (login/src/auth/
        # storage.rs:206-223): there is no protocol to join.
        return contextlib.nullcontext()

    def _live_config_lock(self):
        return contextlib.nullcontext()

    def _store_env_guard(self) -> bool:
        # CLAUDE_SECURESTORAGE_CONFIG_DIR redirects Claude's store; Codex's
        # follows CODEX_HOME on every path, capture and consume alike.
        return False

    def live_login_path(self) -> Path:
        return codex_store.live_auth_path()

    def _live_credentials_path(self) -> Path:
        return codex_store.live_auth_path()

    def _looks_like_api_key(self, creds: str | None) -> bool:
        return bool(creds) and codex_auth.is_api_key_blob(creds)

    def _running_instances(self):
        """Running Codex processes, in the base's ``(sessions, ides)`` shape:
        Codex keeps no session or IDE lock files, so all of them are found by
        one ``ps`` scan and listed as sessions."""
        return process_detection.list_codex_processes(), []

    def _refuse_session_shell(self) -> None:
        """Refuse account mutation from inside a ``cswap codex run`` shell:
        with ``CODEX_HOME`` pointing into this store's session profiles, the
        "live" login is the profile's, not the default one. (``__init__``
        already restores the default home; this catches a CODEX_HOME
        re-pointed after the switcher was built.)"""
        home = os.environ.get("CODEX_HOME")
        if not home:
            return
        try:
            Path(home).resolve().relative_to((self.backup_dir / "sessions").resolve())
        except ValueError:
            return
        raise SwitchError(
            f"This shell is inside a {self.cli_prefix} run session profile "
            "(CODEX_HOME points at it). Mutating accounts here would operate "
            "on the wrong live store — unset CODEX_HOME or run from a normal "
            "shell."
        )

    def relogin_hint(
        self, text: str, slot: object = None, via_login: str = "{cmd}"
    ) -> str:
        """Always ``cswap codex add --login``: a ``codex login`` in the live
        home would revoke the stored account it replaces (codex-spec gotcha
        1), so a Codex hint never says "log in, then add"."""
        cmd = f"{self.cli_prefix} add --login"
        return via_login.format(cmd=cmd if slot is None else f"{cmd} --slot {slot}")

    def _delete_session_keychain_entry(self, session_dir: Path) -> None:
        """Nothing to delete: a Codex session profile keeps its login in its
        own auth.json (file store forced), and the base's entry name lies in
        Claude Code's Keychain namespace."""

    # -- session mode (codex_session) ------------------------------------------
    #
    # A Codex profile keeps its login in <profile>/auth.json; its
    # .credentials.json is Codex's MCP OAuth store and is never touched.
    # `_session_identity_drifted` needs no override: the base compares
    # through `_read_session_identity`.

    def make_session_manager(self) -> codex_session.CodexSessionManager:
        return codex_session.CodexSessionManager(self)

    def _scan_live_sessions(self, session_dir: Path):
        return codex_session.scan_live_sessions(session_dir)

    def _read_session_credentials(self, session_dir: Path) -> str | None:
        return codex_session.read_session_credentials(session_dir)

    def _read_session_identity(self, session_dir: Path) -> tuple[str, str] | None:
        return codex_session.read_session_identity(session_dir)

    def _profile_is_quiescent(self, session_dir: Path) -> bool:
        return codex_session.profile_is_quiescent(session_dir)

    def _ensure_no_live_session(
        self, account_num: str, email: str, action: str
    ) -> None:
        """The base guard in Codex's words, pointing at Codex's records.
        (The base names ``<profile>/sessions``, which in a Codex profile is
        conversation history — not something to "remove or repair".)"""
        session_dir = self._session_dir(account_num, email)
        sessions, unreadable = self._scan_live_sessions(session_dir)
        if sessions:
            raise SessionError(
                f"Account-{account_num} ({email}) has a live session-mode Codex "
                f"process (PID {', '.join(str(s.pid) for s in sessions)}). "
                f"Exit it first, then retry {action}."
            )
        if unreadable:
            raise SessionError(
                f"Account-{account_num} ({email}) has {unreadable} session "
                f"record(s) that could not be read, so whether a Codex process "
                f"is live cannot be determined. Inspect "
                f"{session_dir / codex_session.RUN_RECORDS} and "
                f"{session_dir / 'app-server-daemon'} and remove or repair "
                f"them, then retry {action}."
            )

    def _invalidate_session_credentials(self, account_num: str, email: str) -> None:
        """Drop the profile's login (auth.json) so the next run re-seeds it
        from the backup; history and everything else stay.

        Only from a quiescent profile. ``_post_backup_write`` decides "live"
        with ``_live_session_pids``, which drops the records it could not
        read, so it reaches here for a profile that may be running; that one
        keeps its login and is marked stale instead (re-seeded once it is
        quiescent).
        """
        from claude_swap.session import clear_session_stale, mark_session_stale

        session_dir = self._session_dir(account_num, email)
        if not session_dir.exists():
            return
        if not self._profile_is_quiescent(session_dir):
            if not mark_session_stale(session_dir):
                self._logger.error(
                    "Account %s's session profile may be in use and could not "
                    "be marked stale; it may keep the superseded login.",
                    account_num,
                )
            return
        (session_dir / "auth.json").unlink(missing_ok=True)
        clear_session_stale(session_dir)
        self._logger.info(f"Invalidated session credentials for account {account_num}")

    def _slot_token_dead(self, num: str, email: str) -> bool:
        try:
            return super()._slot_token_dead(num, email)
        except CodexStoreUnsupported:
            # The live store cannot be read, so it cannot be condemned.
            return False

    # -- identity ------------------------------------------------------------

    def _capture_source(self) -> str | None:
        """The login being looked at: a pinned capture, else the live one."""
        pinned = getattr(self._capture_tls, "creds", None)
        return pinned if pinned is not None else self._read_credentials()

    def _identity_of(self, creds: str | None) -> dict | None:
        """``{"email", "uuid", "organizationUuid", "planType"}`` of a Codex
        login, or None. ``organizationUuid`` is the ChatGPT account id, so a
        personal login and a workspace login of one email stay apart."""
        if not creds:
            return None
        ident = codex_auth.identity(creds)
        if ident is not None:
            return ident if ident["email"] else None
        if codex_auth.is_api_key_blob(creds):
            return self._api_key_identity(creds)
        return None

    def _api_key_identity(self, creds: str) -> dict | None:
        """The API-key slot holding this key, as an identity.

        An API-key login names no account (Claude keeps the identity in
        ~/.claude.json; Codex has nothing beside auth.json), so the key is
        matched against the API-key slots' backups. Without this, a slot
        switched to would read as an unmanaged login, and the next switch
        would stash it rather than back it up.

        One operation asks for the live identity many times, and each backup
        read can be a Keychain subprocess, so the answer is cached per live
        key and roster state: every path that re-points a slot at another key
        (add-token, import, remove) rewrites ``sequence.json``. A scan that
        could not read some backup is not cached.
        """
        key = (codex_auth.parse_blob(creds) or {}).get("OPENAI_API_KEY")
        if not key:
            return None
        try:
            st = self.sequence_file.stat()
        except OSError:
            return None  # no roster, no slot to match
        stamp = (oauth.credential_fingerprint(creds), st.st_mtime_ns, st.st_size)
        cached = self._api_key_identity_cache
        if cached is not None and cached[0] == stamp:
            return cached[1]
        data = self._get_sequence_data() or {}
        result, complete = None, True
        for num, acct in data.get("accounts", {}).items():
            if acct.get("kind") != "api_key":
                continue
            backup, unreadable = self._read_account_credentials_ex(
                num, acct.get("email", "")
            )
            complete = complete and not unreadable
            stored = codex_auth.parse_blob(backup or "")
            if stored and stored.get("OPENAI_API_KEY") == key:
                result = {
                    "email": acct.get("email", ""),
                    "uuid": acct.get("uuid") or "",
                    "organizationUuid": acct.get("organizationUuid") or "",
                    "planType": None,
                }
                break
        if result is not None or complete:
            self._api_key_identity_cache = (stamp, result)
        return result

    def _get_current_identity_triple(self) -> tuple[str, str, str] | None:
        """``(email, account_id, chatgpt_user_id)`` of the live login.

        Raises:
            CodexStoreUnsupported: Codex's configured store is one cswap
                cannot read.
        """
        ident = self._identity_of(self._capture_source())
        if ident is None:
            return None
        return (ident["email"], ident["organizationUuid"] or "", ident["uuid"] or "")

    def _resolve_token_identity(self, creds: str) -> dict | None:
        """The ownership oracle, offline: the id_token names its account —
        its own claim, not the stored account_id a hybrid gets wrong."""
        return codex_auth.token_identity(creds)

    # -- synthesized config ----------------------------------------------------

    def _workspace_name(self, identity: dict) -> str | None:
        """Display name of a workspace login (None for a personal plan).
        Offline: the name this process's add just fetched, else the one the
        roster stored for this identity — so a switch in a later process
        keeps it in the slot's config backup — else the plan's label."""
        plan = identity.get("planType")
        if plan not in codex_auth.WORKSPACE_PLANS:
            return None
        org = identity.get("organizationUuid") or ""
        data = self._get_sequence_data() or {}
        slot = self._find_account_slot(data, identity.get("email") or "", org)
        stored = data["accounts"][slot].get("organizationName") if slot else None
        return (
            self._workspace_names.get(org) or stored or f"ChatGPT {plan.title()}"
        )

    def _remember_workspace_name(self, creds: str, identity: dict) -> None:
        """Fetch a workspace login's display name (advisory network call).

        A success also corrects an existing slot's stored name: one added
        while the lookup failed holds the plan label, a workspace can be
        renamed, and the shared add never rewrites an existing record's name.
        """
        org = identity.get("organizationUuid")
        if not org or identity.get("planType") not in codex_auth.WORKSPACE_PLANS:
            return
        name = codex_auth.fetch_workspace_name(
            creds, base_url=codex_store.chatgpt_base_url()
        )
        if not name:
            return
        self._workspace_names[org] = name
        try:
            with FileLock(self.lock_file):
                data = self._get_sequence_data()
                slot = self._find_account_slot(data, identity["email"], org) if data else None
                if slot and data["accounts"][slot].get("organizationName") != name:
                    data["accounts"][slot]["organizationName"] = name
                    data["lastUpdated"] = get_timestamp()
                    self._write_json(self.sequence_file, data)
        except (ClaudeSwitchError, OSError) as e:
            # Advisory: `add --login` calls this after its temp home is gone,
            # so failing here would throw away a completed browser login.
            self._logger.warning(f"Could not store the workspace name {name!r}: {e}")

    def _synth_config_for(self, identity: dict) -> dict:
        return {
            "oauthAccount": {
                "emailAddress": identity["email"],
                "accountUuid": identity.get("uuid") or "",
                "organizationUuid": identity.get("organizationUuid") or "",
                "organizationName": self._workspace_name(identity),
            }
        }

    def _live_config_data(self) -> dict | None:
        ident = self._identity_of(self._capture_source())
        return None if ident is None else self._synth_config_for(ident)

    def _snapshot_live_config(self) -> str | None:
        """Synthesized from the live login. The switch stores it as the
        outgoing slot's config backup, and both switch branches require the
        ``oauthAccount`` it carries."""
        data = self._live_config_data()
        return None if data is None else json.dumps(data, indent=2)

    def _apply_live_config(
        self, target_config: dict, emit_output: bool, warnings_out: list[str]
    ) -> None:
        """No config file to repoint: the identity travels inside auth.json.

        Runs right after the activation write in both switch branches, so it
        is where that write's store warnings (a Keychain fallback, the
        account_id race) reach the user and the JSON ``warnings``.
        """
        for msg in self._store.write_warnings:
            warnings_out.append(msg)
            if emit_output:
                warning(msg)

    def _restore_live_config(self, text: str) -> None:
        """Nothing was applied, so there is nothing to restore."""

    # -- add -------------------------------------------------------------------

    def _read_capture_credentials(self) -> str | None:
        """The login ``add`` captures: a pinned one, else the live store
        (degraded reads refused). Claude's CLAUDE_* variables play no part."""
        pinned = getattr(self._capture_tls, "creds", None)
        return pinned if pinned is not None else self._refuse_degraded_capture()

    def _reject_live_api_key_capture(self, creds: str) -> None:
        if self._looks_like_api_key(creds):
            raise ValidationError(
                "The active Codex login is an API key. Add it with "
                f"'{self.cli_prefix} add-token <key>' instead."
            )

    def _reject_foreign_credential_capture(
        self, creds: str, email: str, org_uuid: str, account_uuid: str
    ) -> str:
        """Exact and offline: the identity was read from a login, so the
        captured bytes must carry that same identity. A mismatch means the
        live login changed between the two reads."""
        ident = codex_auth.identity(creds) or {}
        seen = (
            ident.get("email"),
            ident.get("organizationUuid") or "",
            ident.get("uuid") or "",
        )
        if seen != (email, org_uuid or "", account_uuid or ""):
            raise ConfigError(
                "The live Codex login changed while it was being read. Nothing "
                "was changed. Re-run when no login is in flight."
            )
        return creds

    def add_account(
        self,
        slot: int | None = None,
        assume_yes: bool = False,
        alias: str | None = None,
    ) -> None:
        """Capture the live Codex login into a slot (see the base method).

        Fails with Codex's own wording when there is no ChatGPT login, and
        fetches a workspace's name first: the shared add reads the identity
        through the config synthesis, which must stay offline.
        """
        self._refuse_session_shell()
        creds = self._read_capture_credentials()
        if creds is None:
            raise CredentialReadError("Failed to read the live Codex login")
        self._reject_live_api_key_capture(creds)
        ident = self._identity_of(creds)
        if ident is None:
            raise ConfigError(
                "No active Codex login found. Log in with `codex login`, or "
                f"add an account with: {self.relogin_hint('')}"
            )
        self._remember_workspace_name(creds, ident)
        super().add_account(slot=slot, assume_yes=assume_yes, alias=alias)

    def add_account_from_token(
        self,
        token: str,
        email: str | None = None,
        slot: int | None = None,
        assume_yes: bool = False,
    ) -> None:
        """Register an OpenAI API key as a Codex account.

        Stored as the auth.json ``codex login --with-api-key`` writes
        (``{"auth_mode": "apikey", "OPENAI_API_KEY": …}``), so a switch
        activates it as is. ``token`` is the key, ``"-"`` (one stdin line) or
        ``""`` (prompt).
        """
        self._refuse_session_shell()
        if token == "-":
            token = sys.stdin.readline().rstrip("\n")
        elif not token:
            token = getpass.getpass(f"{self.token_kind}: ")
        token = token.strip()
        if not token:
            raise ValidationError("API key cannot be empty")
        super().add_account_from_token(
            codex_auth.make_api_key_blob(token),
            email=email, slot=slot, assume_yes=assume_yes,
        )

    def add_account_via_login(
        self,
        slot: int | None = None,
        alias: str | None = None,
        device_auth: bool = False,
    ) -> None:
        """Sign in with ``codex login`` in a throwaway CODEX_HOME and store
        the result, leaving the live login alone.

        Browser and device logins first revoke whatever login their home
        holds (cli/src/login.rs:122-168, 319-341), and ``codex logout``
        revokes too. So the login runs in a fresh 0700 directory under the
        store, never in a home holding an account, and that directory is
        deleted afterwards WITHOUT logging out (which would revoke the login
        just captured). The file store is forced so the login lands in that
        directory's auth.json.

        An account that already has a slot gets that slot refreshed — the
        re-login path for a dead refresh token. If it is the live login, the
        live login is replaced too: otherwise the next switch away would read
        the old live lineage as the slot's own rotation and back it up over
        the fresh one. Codex adopts a same-account login changed on disk at
        its next refresh (auth/manager.rs:2487-2520).
        """
        self._refuse_session_shell()
        codex = shutil.which("codex")
        if codex is None:
            raise ConfigError(
                "The codex CLI was not found on PATH. Install Codex, then retry."
            )
        self._setup_directories()
        cutoff = time.time() - _STALE_LOGIN_HOME_S
        for stale in self.backup_dir.glob(".login-*"):
            with contextlib.suppress(OSError):
                if stale.stat().st_mtime < cutoff:
                    self._remove_login_home(stale)
        login_home = Path(tempfile.mkdtemp(prefix=".login-", dir=self.backup_dir))
        try:
            cmd = [codex, "login"]
            if device_auth:
                cmd.append("--device-auth")
            cmd += ["-c", FILE_STORE_OVERRIDE]
            rc = subprocess.run(
                cmd, env={**os.environ, "CODEX_HOME": str(login_home)}
            ).returncode
            if rc != 0:
                raise ConfigError(f"codex login failed (exit {rc}); nothing was added.")
            try:
                creds = (login_home / "auth.json").read_text(encoding="utf-8")
            except OSError as e:
                raise ConfigError(
                    f"codex login left no login to capture ({e}); nothing was added."
                )
        finally:
            self._remove_login_home(login_home)

        ident = codex_auth.identity(creds)
        if ident is None or not ident["email"]:
            raise ConfigError(
                "codex login did not produce a ChatGPT login; nothing was added."
            )
        key = (ident["email"], ident["organizationUuid"] or "")
        self._remember_workspace_name(creds, ident)
        is_live = self._get_current_account() == key
        prior_active = (self._get_sequence_data() or {}).get("activeAccountNumber")

        self._capture_tls.creds = creds
        try:
            super().add_account(slot=slot, alias=alias)
        finally:
            self._capture_tls.creds = None

        data = self._get_sequence_data() or {}
        num = self._find_account_slot(data, *key)
        if num is None or self._read_account_credentials(num, key[0]) != creds:
            return  # cancelled at the overwrite prompt
        if is_live:
            with FileLock(self.lock_file):
                # Re-checked under the lock, like every other live write: the
                # add above can sit at a prompt, and a switch or `codex login`
                # landing meanwhile makes the live login another account's.
                still_live = self._live_identity_matches(*key)
                if still_live:
                    self._write_credentials(creds)
            if still_live:
                for msg in self._store.write_warnings:
                    warning(msg)
                print(dimmed("It is the live Codex login, so that was updated too."))
            else:
                print(dimmed(
                    "The live Codex login changed meanwhile, so it was left alone."
                ))
        elif data.get("activeAccountNumber") != prior_active:
            # The shared add marks what it captured as active; this login
            # is not live.
            data["activeAccountNumber"] = prior_active
            self._write_json(self.sequence_file, data)

    def _remove_login_home(self, path: Path) -> None:
        """Delete a temporary login home — never via ``codex logout``, which
        would revoke the login. A failure leaves an unrevoked login on disk,
        so it is reported with its path."""
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            pass
        except OSError as e:
            msg = (
                f"Could not remove the temporary Codex login at {path} ({e}); "
                "it holds an unrevoked login — delete that directory."
            )
            self._logger.warning(msg)
            warning(msg)

    # -- switch ------------------------------------------------------------------

    def _read_target_credentials(self, account_num: str, email: str) -> str:
        """The switch target's stored login, refused when Codex could not
        run it (``codex_session.login_problem``) or its ``forced_*`` settings
        would delete it at startup.

        Read before any live-store write on both switch branches — on the
        direct branch also before the displaced-live stash. On the normal
        branch the outgoing login has already been backed up (or stashed) by
        then, which is a correct backup whether or not the switch goes on."""
        creds = super()._read_target_credentials(account_num, email)
        if not codex_auth.is_api_key_blob(creds):
            # An imported or hand-edited backup can lack what Codex needs
            # (codex-spec gotcha 3), or name another account.
            accounts = (self._get_sequence_data() or {}).get("accounts") or {}
            org = accounts.get(account_num, {}).get("organizationUuid") or ""
            problem = codex_session.login_problem(creds, email, org)
            if problem:
                raise SwitchError(
                    f"Refusing to switch to Account-{account_num}: its stored "
                    f"login is unusable ({problem}). Re-add it with: "
                    + self.relogin_hint("", account_num)
                )
        reason = codex_auth.forced_login_violation(creds, codex_store.read_codex_config())
        if reason:
            raise SwitchError(
                f"Refusing to switch to Account-{account_num}: {reason}, so "
                f"Codex would delete this login at startup. Change the setting in "
                f"{codex_store.codex_home() / 'config.toml'} or pick another account."
            )
        return creds

    def _prepare_credentials_for_activation(
        self, target_credentials: str, live_credentials: str | None
    ) -> str:
        """Activated as stored: a Codex login shares no machine-wide fields
        with the live one."""
        return target_credentials

    def _print_switch_followup(self) -> None:
        print(dimmed(
            "Codex processes already running keep the previous account until "
            "restarted. Restart the shared daemon with: "
            "codex app-server daemon restart"
        ))
        try:
            sessions, ide_instances = self._running_instances()
        except Exception:
            self._logger.debug("Failed to detect running Codex processes", exc_info=True)
            return
        count = len(sessions) + len(ide_instances)
        if count:
            print(dimmed(f"{count} Codex process{'es' if count != 1 else ''} running now."))

    # -- usage -------------------------------------------------------------------

    def consume_backup_grant(
        self, account_num: str, email: str, snapshot: str
    ) -> oauth.RefreshOutcome:
        """The consume gate, refused while the live login could be this
        slot's lineage.

        Codex refreshes its live login itself (never us — codex-spec gotcha
        2), and "live" is decided by reading that login: when the read fails
        or is degraded, no slot is active, and the live account's slot would
        reach this gate as an inactive one. Spending its grant here strands
        the rotated token in the backup, the running Codex gets
        refresh_token_reused, and the next switch away writes the stale live
        login back over the fresher backup. So: transient (nothing struck,
        retried next pass) while the live login is unreadable, degraded or in
        an unsupported store, or carries this slot's lineage — and while the
        slot's session profile may be in use, since a running session owns
        the lineage it was seeded with. Every consumer (usage collection,
        autoswitch's freshen, sessions) comes through here.
        """
        if self._default_home_unknown or codex_session.in_sessions(
            codex_store.codex_home(), self.backup_dir / "sessions"
        ):
            # The "live" login may not be the real default's (a session
            # profile's, or ~/.codex standing in for an unknown default), so
            # the default login's lineage could look inactive. `__init__`
            # restores the real home when it can; this holds otherwise.
            self._logger.info(
                "The default Codex home is not known here; not consuming "
                "account %s's refresh token.", account_num,
            )
            return oauth.RefreshOutcome(None, "transient")
        if not self._profile_is_quiescent(self._session_dir(account_num, email)):
            # A session may be running on this slot's lineage (the profile is
            # seeded from the backup). The base gate asks
            # `_live_session_pids`, which drops records it could not read,
            # and would then adopt the profile's login and POST its grant
            # under a running Codex.
            self._logger.info(
                "Account %s's session profile may be in use; not consuming "
                "its refresh token.", account_num,
            )
            return oauth.RefreshOutcome(None, "transient")
        try:
            live = self._read_active_credentials()
        except CodexStoreUnsupported:
            live = None
        if live is None or live.value is None or live.degraded:
            self._logger.info(
                "The live Codex login could not be read cleanly; not consuming "
                "account %s's refresh token, which may be the live one.",
                account_num,
            )
            return oauth.RefreshOutcome(None, "transient")
        if live.value:
            # ponytail: checked before the consume/slot locks, and the POST
            # runs later outside any lock — a switch making this slot live in
            # that window still spends the now-live grant. Closing it needs a
            # lock Codex honors around auth.json, and Codex takes none.
            live_fp = oauth.credential_fingerprint(live.value)
            backup = self._read_account_credentials(account_num, email)
            if live_fp in (
                oauth.credential_fingerprint(backup),
                oauth.credential_fingerprint(snapshot),
            ):
                self._logger.info(
                    "Account %s's refresh token is the live Codex login's; "
                    "Codex refreshes it, not cswap.", account_num,
                )
                return oauth.RefreshOutcome(None, "transient")
        return super().consume_backup_grant(account_num, email, snapshot)

    def _fetch_account_usage(
        self,
        account_info: tuple[int, str, str, str, bool, str, str],
        rejected_fp: str | None = None,
    ) -> FetchRecord:
        """The base fetch, with the row's refused-token stamp handed to the
        active account's read-only fetch too."""
        num, email, _org_name, org_uuid, is_active, creds, _alias = account_info
        if is_active:
            return self._fetch_active_usage(
                str(num), email, creds, org_uuid, rejected_fp
            )
        return super()._fetch_account_usage(account_info, rejected_fp)

    def _fetch_active_usage(
        self,
        account_num: str,
        email: str,
        creds: str,
        org_uuid: str = "",
        rejected_fp: str | None = None,
    ) -> FetchRecord:
        """Usage for the live login — read-only, never refreshed.

        Codex refreshes its own login and rotates the refresh token as it
        does; a refresh from here would leave running Codex processes on a
        spent token (codex-spec gotcha 2). An expired access token reports
        "token expired" until Codex renews it, and a 401 likewise
        (``_read_only_fetch``, which stamps the refused token so later passes
        don't ask again until it changes). A rotation Codex made is resynced
        into the slot backup, attributed offline by the id_token.
        """
        view = oauth.extract_oauth_data(creds)
        if not view or not view.get("accessToken"):
            return FetchRecord(sentinel=USAGE_NO_CREDENTIALS)
        if oauth.is_oauth_token_expired(view.get("expiresAt")):
            return FetchRecord(sentinel=USAGE_TOKEN_EXPIRED)
        record = self._read_only_fetch(account_num, email, creds, rejected_fp)
        if record.usage is not None and not self._active_read_degraded:
            self._resync_rotated_backup(account_num, email, org_uuid, creds)
        return record

    # -- output ------------------------------------------------------------------
    #
    # Codex payloads say which provider they describe, and `isOrganization`
    # follows the workspace NAME: `organizationUuid` is always the ChatGPT
    # account id, so the base's bool(org_uuid) would call every login one.

    def list_accounts(
        self,
        show_token_status: bool = False,
        json_output: bool = False,
        fetch: set[str] | None = None,
    ) -> dict | None:
        payload = super().list_accounts(
            show_token_status=show_token_status, json_output=json_output, fetch=fetch
        )
        if payload is not None:
            for row in payload["accounts"]:
                row["isOrganization"] = bool(row["organizationName"])
            payload["provider"] = self.provider_name
        return payload

    def _build_status_payload(self) -> dict:
        payload = super()._build_status_payload()
        active = payload.get("active")
        if active and "isOrganization" in active:
            active["isOrganization"] = bool(active["organizationName"])
        payload["provider"] = self.provider_name
        return payload

    def status(self, json_output: bool = False) -> dict | None:
        # Kept: the base says "No active Codex account"; Codex's wording
        # (and its tests, the menu bar's alert) say "login".
        if not json_output and self._get_current_account() is None:
            print(f"{bolded('Status:')} {dimmed('No active Codex login')}")
            return None
        return super().status(json_output=json_output)

    def _switch_result_from_op(
        self, op: dict, strategy: str, extra_warnings: list[str] | None = None
    ) -> dict:
        result = super()._switch_result_from_op(op, strategy, extra_warnings)
        result["provider"] = self.provider_name
        return result

    def _switch_noop(self, **kwargs) -> dict:
        result = super()._switch_noop(**kwargs)
        result["provider"] = self.provider_name
        return result
