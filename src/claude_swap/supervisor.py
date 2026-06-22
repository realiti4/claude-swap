"""The managed-session launcher + supervisor (``cswap launch``).

Unlike ``cswap run`` (which ``exec``s claude and is replaced), a *managed*
session keeps cswap resident as the parent process so it can: execute migration
intents the balancer enqueues, and — for single-subscription users — pause a
session when its account is exhausted and **auto-resume it** via native
``claude --resume`` once the rate-limit window resets, instead of losing the
in-flight workflow.

This is not a daemon: it is a foreground, terminal-bound parent the user
started and can Ctrl-C, exactly like the existing Windows branch of
``session._exec`` (which already stays resident wrapping claude) — generalized
to every platform because supervision requires a live parent.

Credential-ownership invariant: a managed profile's credentials are written
ONLY here, by the supervisor that owns that profile — never by another session's
statusline. The statusline records a ``migration`` *intent* in the registry; the
owning supervisor consumes its own intent and re-points its own profile via
``switcher.seed_profile_credentials`` (verbatim seed, no refresh side-effects).
"""

from __future__ import annotations

import os
import select
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from claude_swap import balancer, embed, oauth, registry
from claude_swap.exceptions import SessionError
from claude_swap.locking import FileLock
from claude_swap.printer import accent, bold_accent, dimmed, muted, warning
from claude_swap.session import AUTH_OVERRIDE_ENV_VARS, SessionManager

# How long to wait for a SIGTERM'd child to exit before force-killing it.
_TERMINATE_GRACE_S = 5.0
# Supervisor wake cadence: block on the child for at most this long, then check
# the registry for an intent/pause (only re-reads when the file's mtime moved).
_WATCH_TICK_S = 1.0

# How recent an ``auth_recover`` flag must be to act on it. A 401 the statusfailure
# hook flags should be consumed on the very next supervisor tick; a flag older than
# this was left by a crash and is ignored (cleared without recovering).
_AUTH_RECOVER_TTL_S = 300.0

# Idle-5h-window priming (feature #3): the resident supervisor attempts a prime
# sweep at most this often *per process* (a cheap local clock that avoids building
# the world every tick); the cross-process gate (one sweep per interval across ALL
# supervisors) is the registry ``last_primed_sweep_at`` stamp claimed under the lock.
_PRIME_LOCAL_INTERVAL_S = 300.0

# Secondary model managed sessions auto-fall-back to when the primary model is
# overloaded (HTTP 529). Unlike a 429 rate limit (which is account-specific and
# triggers a migration), a 529 is server/model-side — every account hits the same
# overloaded model — so the fix is claude's NATIVE ``--fallback-model``, not an
# account switch. Override per-launch with ``cswap launch -- --fallback-model X``.
_DEFAULT_FALLBACK_MODEL = "sonnet"

# How many times claude retries a turn's API call before giving up. Managed
# sessions raise this well above claude's default so TRANSIENT failures (a
# dropped socket, connection reset, timeout, a brief 502/503/overload) get more
# native retry-with-backoff and are far more likely to self-heal WITHIN the turn
# — rather than failing the turn and bothering the StopFailure safety net (which
# only handles account-level problems like 429/401, not transient network glitches).
# Only applied when the user hasn't set ``CLAUDE_CODE_MAX_RETRIES`` themselves.
_DEFAULT_MAX_RETRIES = "20"

# Bounded auth-failure recovery on a child EXIT (distinct from the in-session
# StopFailure path, which recovers while claude stays alive). When claude hard-
# exits on invalid/expired credentials, the exit branch recovers and relaunches —
# but ``choose_migration_target`` has no auth-health filter, so an unbounded
# relaunch could hop between logged-out accounts forever. Cap total attempts per
# launch, then surface the re-login guidance instead of spinning.
_MAX_AUTH_RECOVER_ATTEMPTS = 2

# Sentinel: the passive-SIGINT install didn't run (e.g. off the main thread), so
# there is nothing to restore in ``finally``.
_NO_SIGNAL = object()

# Injected as the first turn whenever the supervisor AUTO-resumes a session after a
# rate-limit or auth interruption. Without it, ``claude --resume`` reloads the
# transcript and then idles at a blank prompt waiting for the user — so the
# "auto-resume" never actually continues the work. This nudge makes the recovered
# session pick up where it left off. The session's ``/goal``, model, and effort ride
# along automatically (the goal + model live in the resumed transcript; effort is in
# ``CLAUDE_CODE_EFFORT_LEVEL`` on the env), and the launch FLAGS below are re-passed.
_RECOVERY_PROMPT = "Sorry for the interruption, please gracefully recover and continue."

# claude launch flags that consume EXACTLY the next token as their value (from
# ``claude --help``). Used to walk an argv the way claude's own parser does so the
# trailing positional prompt can be told apart from a flag value.
_VALUE_FLAGS = frozenset({
    "--agent", "--agents", "--append-system-prompt", "--append-system-prompt-file",
    "--debug-file", "--effort", "--fallback-model", "--json-schema",
    "--max-budget-usd", "--max-turns", "--model", "-m", "--name", "-n",
    "--output-format", "--input-format", "--permission-mode",
    "--permission-prompt-tool", "--session-id", "--settings", "--setting-sources",
    "--system-prompt", "--system-prompt-file",
})
# Variadic launch flags: greedily consume following non-flag tokens as values
# (matches commander's parsing in claude).
_VARIADIC_FLAGS = frozenset({
    "--add-dir", "--allowedTools", "--allowed-tools", "--disallowedTools",
    "--disallowed-tools", "--betas", "--file", "--mcp-config",
})
# Optional-value flags: a following non-flag token is their value (a session id,
# PR ref, debug filter), but they are also valid bare. They must be recognized so a
# value like ``--resume <sid>`` isn't mistaken for the positional prompt.
_OPTVALUE_FLAGS = frozenset({"--resume", "--from-pr", "--debug", "-d"})
# Resume-control flags stripped from the preserved set when the supervisor supplies
# its OWN ``--resume <sid>`` — re-passing the user's would double-resume / conflict.
# ``--print``/``-p`` is dropped too: an auto-resume MUST be interactive (the injected
# recovery prompt is a first turn the user then continues), and ``-p`` would relaunch
# headless and exit after one response.
_DROP_ON_RESUME_NOVALUE = frozenset({"--continue", "-c", "--fork-session", "--print", "-p"})
_DROP_ON_RESUME_OPTVALUE = frozenset({"--resume", "--from-pr"})  # optional trailing value
_DROP_ON_RESUME_VALUE = frozenset({"--session-id"})  # required trailing value


def _flag_name(tok: str) -> str:
    """The flag name without an inline ``=value`` (``--model=opus`` -> ``--model``)."""
    return tok.split("=", 1)[0]


def _strip_trailing_prompt(args: list[str]) -> list[str]:
    """Return ``args`` with a trailing positional prompt removed.

    Walks left-to-right consuming flags and their values exactly as claude's parser
    does; the first bare token (not a flag and not consumed as a flag value) is the
    positional prompt — it and everything after it are dropped. A flags-only argv
    (the managed-launch norm) is returned unchanged.
    """
    out: list[str] = []
    i, n = 0, len(args)
    while i < n:
        tok = args[i]
        if not tok.startswith("-"):
            break  # first bare positional == the prompt; drop it and the rest
        out.append(tok)
        name = _flag_name(tok)
        if "=" in tok:
            i += 1
            continue
        if name in _VALUE_FLAGS:
            if i + 1 < n:
                out.append(args[i + 1])
                i += 2
            else:
                i += 1
            continue
        if name in _VARIADIC_FLAGS:
            i += 1
            while i < n and not args[i].startswith("-"):
                out.append(args[i])
                i += 1
            continue
        if name in _OPTVALUE_FLAGS:
            if i + 1 < n and not args[i + 1].startswith("-"):
                out.append(args[i + 1])
                i += 2
            else:
                i += 1
            continue
        i += 1
    return out


def _resume_launch_args(base_args: list[str], sid: str, prompt: str) -> list[str]:
    """Build the argv for an auto-resume relaunch.

    ``["--resume", <sid>]`` + the original launch FLAGS (model / settings /
    skip-permissions / fallback-model / add-dir / … so the recovered session keeps
    them) + ``prompt`` as the first turn. The user's original positional prompt is
    dropped (it already lives in the resumed transcript) and any resume-control
    flags they passed are dropped (the supervisor supplies its own ``--resume``).
    Each flag is kept or dropped together with the value tokens it consumes, so a
    dropped flag never strands a value and a kept flag's value is never re-read as a
    flag.
    """
    flags = _strip_trailing_prompt(base_args)
    out: list[str] = []
    i, n = 0, len(flags)
    while i < n:
        tok = flags[i]
        name = _flag_name(tok)
        group = [tok]
        i += 1
        if "=" not in tok:
            if name in _VALUE_FLAGS or name in _DROP_ON_RESUME_VALUE:
                if i < n:
                    group.append(flags[i])
                    i += 1
            elif name in _VARIADIC_FLAGS:
                while i < n and not flags[i].startswith("-"):
                    group.append(flags[i])
                    i += 1
            elif name in _DROP_ON_RESUME_OPTVALUE:
                if i < n and not flags[i].startswith("-"):
                    group.append(flags[i])
                    i += 1
        if (
            name in _DROP_ON_RESUME_NOVALUE
            or name in _DROP_ON_RESUME_OPTVALUE
            or name in _DROP_ON_RESUME_VALUE
        ):
            continue  # drop the flag and any value(s) it consumed
        out.extend(group)
    # Terminate options with ``--`` so the recovery prompt is ALWAYS the positional
    # prompt — never swallowed as the value of a trailing variadic (``--add-dir``…)
    # or optional-value (``--debug``…) flag, which would leave the resumed session
    # idling at a blank prompt instead of continuing its work.
    return ["--resume", sid, *out, "--", prompt]


def launch(
    switcher, claude_args: list[str], *, cwd: str | None = None, share: bool = True
) -> int:
    """Start a balancer-managed Claude Code session in this terminal.

    Picks the best account for the new session (highest-priority with headroom),
    bootstraps a per-session profile, embeds the statusline + QoL, and supervises
    claude until it exits. Returns claude's exit code.
    """
    claude_bin = shutil.which("claude")
    if not claude_bin:
        raise SessionError(
            "'claude' was not found on PATH. Install Claude Code first."
        )

    seq = switcher._get_sequence_data() or {}
    if not seq.get("accounts"):
        raise SessionError(
            "No managed accounts yet. Add one with `cswap --add-account` first."
        )

    # Ensure the managed template exists (first run before any upgrade migration).
    embed.write_managed_template(switcher)

    cwd = cwd or os.getcwd()
    managed_id = uuid.uuid4().hex[:12]
    profile_dir = switcher.managed_dir / managed_id

    # Pick the initial account (outside any lock; may fetch idle usage).
    account = _pick_initial_account(switcher)

    sup = Supervisor(switcher, managed_id, profile_dir, account, cwd=cwd, share=share)
    return sup.run(claude_args, claude_bin)


def _pick_initial_account(switcher) -> str:
    """Highest-priority account with headroom; falls back so launch never refuses."""
    bcfg = switcher.get_auto_balance_config()
    cfg = balancer.config_from_dict(bcfg)
    reg = registry.read_registry(switcher)
    acct_views, _ = registry.build_world(switcher, reg, fetch_idle=True)
    chosen = balancer.assign_new_session(acct_views, 0, time.time(), cfg)
    if chosen:
        return chosen
    # Nothing has clear headroom — fall back to the highest-priority account so
    # the session still starts (the balancer pauses it on the first rising edge
    # if it is genuinely capped).
    if acct_views:
        return sorted(
            acct_views.values(),
            key=lambda a: (-a.priority, balancer._num_key(a.num)),
        )[0].num
    seq = switcher._get_sequence_data() or {}
    return str(sorted((int(n) for n in seq.get("accounts", {})))[0])


class Supervisor:
    """Owns one managed session's profile, registry row, and child process."""

    def __init__(self, switcher, managed_id, profile_dir, account, *, cwd, share):
        self.switcher = switcher
        self.managed_id = managed_id
        self.profile_dir = Path(profile_dir)
        self.account = str(account)
        self.cwd = cwd
        self.share = share
        self._logger = switcher._logger
        self._claude_session_id = ""
        self._auth_recover_attempts = 0

    # -- lifecycle --------------------------------------------------------

    def run(self, claude_args: list[str], claude_bin: str) -> int:
        self._bootstrap_profile()
        self._register()
        num, email, _ = self.switcher.resolve_account(self.account)
        print(
            f"{accent('Launching')} managed session on Account-{num} ({email}) "
            f"{muted('[load balancer]')}"
        )
        env = self._session_env()
        base_args = self._qol_args(claude_args)
        resume = False
        proc = None  # stays None if Ctrl-C lands during the first Popen
        try:
            while True:
                # Re-read claude's own session id from the registry row right
                # before each Popen. The cached ``_claude_session_id`` is only
                # refreshed in ``_set_pids`` (just AFTER the previous Popen,
                # before the new claude has rendered its statusline), so relying
                # on it would launch the first auto-resume fresh and leave later
                # ones off-by-one. The authoritative value lives in the row.
                sid = self._current_claude_session_id()
                # An auto-resume after a rate-limit / auth interruption: resume the
                # session, re-pass the launch flags so model/settings/permissions
                # survive the account switch, and inject the recovery prompt so the
                # session actually continues its work instead of idling at a blank
                # prompt. A fresh launch (no session id recorded yet) falls back to
                # the original args verbatim.
                args = (_resume_launch_args(base_args, sid, _RECOVERY_PROMPT)
                        if resume and sid
                        else list(base_args))
                # Let claude own Ctrl-C — but ONLY while it is the live foreground
                # process. The supervisor shares the foreground process group + TTY
                # with the child, so a kernel-delivered SIGINT (Ctrl-C in a cooked-
                # mode window) would otherwise raise KeyboardInterrupt here and tear
                # the session down, killing claude via SIGTERM before it can run its
                # own interrupt/quit handling and print its resume hint. A no-op
                # handler (NOT SIG_IGN, which the child would inherit across exec)
                # keeps the parent passive while claude is alive — just like the
                # exec'd `cswap run` path. It is restored the instant the child is
                # gone so the pause/auto-resume countdown below stays Ctrl-C-
                # interruptible (no child owns Ctrl-C then).
                prev_sigint = self._install_passive_sigint()
                try:
                    proc = subprocess.Popen([claude_bin, *args], env=env, cwd=self.cwd)
                    self._set_pids(proc.pid)
                    outcome, rc = self._supervise(proc)
                finally:
                    self._restore_sigint(prev_sigint)

                if outcome == "exit":
                    if self._should_handle_limit_exit():
                        # The child launched and RAN (it hit a usage limit), so its
                        # credentials were fine — reset the consecutive-auth-failure
                        # counter so a long-lived session that recovered from earlier
                        # auth blips isn't torn down on a later, unrelated one.
                        self._auth_recover_attempts = 0
                        self._pause_and_resume()  # migrate now, or pause + auto-resume
                    elif self._should_recover_auth_exit(rc):
                        # An auth-failure exit: claude hard-exited on invalid/expired
                        # login credentials (the in-session StopFailure path only
                        # helps while claude stays alive). Recover — refresh+reseed
                        # this account, or migrate to a healthy one — then relaunch.
                        # Bounded so a permanently-dead login can never spin.
                        if self._auth_recover_attempts >= _MAX_AUTH_RECOVER_ATTEMPTS:
                            warning(
                                f"Managed session keeps failing to authenticate "
                                f"(Account-{self.account}). Re-login on an account "
                                f"(e.g. `cswap --add-account`)."
                            )
                            self._deregister()
                            return rc
                        self._auth_recover_attempts += 1
                        if self._recover_auth():
                            resume = True
                            continue
                        self._deregister()  # logged out, no target -> guidance printed
                        return rc
                    else:
                        # Clean user quit on a healthy account.
                        self._print_resume_hint()  # before deregister (reads the row)
                        self._deregister()
                        return rc
                else:
                    # outcome == "pause": child was SIGTERM'd to honour a pause.
                    # It ran fine up to the pause, so its creds were good -> reset.
                    self._auth_recover_attempts = 0
                    self._pause_and_resume()
                resume = True
        except KeyboardInterrupt:
            # Ctrl-C while the child was live (a SIGINT that slipped through the
            # passive handler's install window) OR during the pause/auto-resume
            # countdown (default handler restored). Reap claude if it is still
            # exiting on its own rather than killing it, so its quit/hint path can
            # finish; an already-exited child returns immediately.
            if proc is not None:
                self._reap_or_terminate(proc)
            self._print_resume_hint()
            self._deregister()
            print(f"\n{dimmed('Managed session stopped')}")
            return 130
        except BaseException:
            # Never orphan the child or leave a ghost registry row on an
            # unexpected failure (e.g. a LockError under contention): clean up
            # the child + our row first, then re-raise.
            if proc is not None:
                self._terminate(proc)
            self._deregister()
            raise

    # -- supervise loop ---------------------------------------------------

    def _supervise(self, proc: subprocess.Popen) -> tuple[str, int | None]:
        """Block on the child; wake on registry changes to consume intents.

        Returns ``("exit", rc)`` when the child exits on its own, or
        ``("pause", None)`` when we SIGTERM it to honour a pause decision.
        """
        last_mtime = self._registry_mtime()
        while True:
            try:
                rc = proc.wait(timeout=_WATCH_TICK_S)
                return ("exit", rc)
            except subprocess.TimeoutExpired:
                pass
            # Idle-5h-window priming rides this resident loop (NO new daemon): a
            # best-effort sweep that self-gates to once per interval (locally and
            # cross-process) and does its network I/O outside the lock, so it can't
            # stall the tick on the common path or block any render.
            self._maybe_prime_idle_windows()
            mtime = self._registry_mtime()
            if mtime == last_mtime:
                continue
            last_mtime = mtime
            decision = self._consume_own_state()
            if decision == "pause":
                self._terminate(proc)
                return ("pause", None)
            # "migrate"/"auth" recovery was applied in place (no relaunch); the
            # session recovers on its next turn. Keep watching.

    def _consume_own_state(self) -> str | None:
        """Act on this session's own registry row: auth recovery, migration, pause.

        Degrades gracefully under lock contention — a tick we can't lock is
        simply skipped and retried on the next registry change, never an error
        that tears down the session.
        """
        lock = FileLock(self.switcher.lock_file, timeout=5)
        if not lock.acquire():
            return None
        try:
            reg = registry.read_registry(self.switcher)
            row = reg.get("sessions", {}).get(self.managed_id)
            if row is None:
                return None
            intent = row.get("migration")
            paused_until = row.get("paused_until")
            # A 401 the statusfailure hook flagged: refresh+re-seed this account
            # (or migrate if it's logged out). Only honour a RECENT flag so a
            # stale one left by a crash can't trigger a spurious recovery.
            auth_recover = row.get("auth_recover")
            do_auth_recover = (
                isinstance(auth_recover, (int, float))
                and (time.time() - auth_recover) <= _AUTH_RECOVER_TTL_S
            )
            if auth_recover is not None:
                row["auth_recover"] = None  # clear it whether or not it's recent
            if intent and intent.get("to"):
                to_account = str(intent["to"])
                registry.clear_intent(reg, self.managed_id)
            else:
                to_account = None
            if do_auth_recover or to_account is not None or auth_recover is not None:
                registry.write_registry(self.switcher, reg)
        finally:
            lock.release()
        if do_auth_recover:
            self._recover_auth()
            return "auth"
        if to_account is not None:
            self._migrate(to_account)
            return "migrate"
        if isinstance(paused_until, (int, float)) and paused_until > time.time():
            self._pending_resume_at = int(paused_until)
            return "pause"
        return None

    def _recover_auth(self) -> bool:
        """Recover this session from a 401 so the NEXT turn re-authenticates.

        First try to refresh + re-seed the SAME account's credentials in place
        (``refresh_account_and_reseed`` owns the credential work). If that fails
        the account is logged out: build the world (OUTSIDE the lock — it may do
        network I/O), pick a healthy migration target, and re-point to it via the
        existing credential-owning ``_migrate``. If no account has headroom either,
        warn the user that every account needs a re-login and leave the session as
        is — no crash, no relaunch loop.

        Returns whether the profile now holds (or has been pointed at) usable
        credentials — ``True`` after a successful refresh/migrate, ``False`` when
        the account is logged out and no healthy target exists. The exit-driven
        caller uses this to decide between relaunching and giving up.
        """
        if self.switcher.refresh_account_and_reseed(self.account, self.profile_dir):
            print(f"\n{accent('Re-authenticated')} Account-{self.account}")
            return True
        # Same-account refresh impossible (refresh token dead / logged out) ->
        # migrate to a healthy account.
        cfg = balancer.config_from_dict(self.switcher.get_auto_balance_config())
        reg = registry.read_registry(self.switcher)
        # A logged-out account's session benefits from a probe-confirmed idle
        # target: spend one credit to land on a runnable account rather than fail.
        acct_views, _ = registry.build_world(
            self.switcher, reg, fetch_idle=True, probe_unavailable=True
        )
        sv = self._self_session_view()
        target = balancer.choose_migration_target(sv, acct_views, {}, cfg)
        if target:
            self._migrate(target)
            return True
        warning(
            "Managed session's account is logged out and no other account has "
            "headroom. Re-login on one account (e.g. `cswap --add-account`)."
        )
        return False

    def _should_recover_auth_exit(self, rc: int | None) -> bool:
        """Whether a child EXIT looks like an auth failure we should recover from.

        Claude Code hard-exits when its credentials are invalid/expired and it
        cannot start a turn (e.g. a revoked/logged-out login at launch). That exit
        is NOT a rate-limit exit (a 401 doesn't raise usage), so without this it
        would be treated as a clean user quit and the session would silently end.

        Detection, in order (the per-launch attempt cap is enforced by the
        caller in :meth:`run`, which prints re-login guidance once exhausted):

        * a clean (rc 0/None) exit, or a signal-killed child (negative rc, e.g.
          ``-11`` SIGSEGV / ``-9`` SIGKILL), is never an auth failure;
        * a RECENT ``auth_recover`` flag the StopFailure hook left (precise for an
          in-session 401 that then exited) — read-and-cleared under the lock;
        * otherwise, only when claude exited BEFORE its statusline ever recorded a
          session id (a launch-time / pre-render failure), pay for a credit-free
          local auth probe. A normal interactive session has a session id and is
          covered by the flag path above, so clean quits never pay the probe cost.
        """
        if not rc or rc < 0:  # None/0 (clean) or signal-killed -> not auth
            return False
        if self._consume_recent_auth_flag():
            return True
        if self._current_claude_session_id():
            return False
        return self._profile_auth_invalid()

    def _consume_recent_auth_flag(self) -> bool:
        """Read-and-clear this session's ``auth_recover`` flag; True if it was recent.

        Mirrors the in-session consumption in :meth:`_consume_own_state` so the exit
        path can act on a 401 the StopFailure hook flagged just before claude died.
        Double-consumption with the alive-loop is benign (whichever clears it first
        wins; the other branch falls through to the probe, which passes post-reseed).
        """
        lock = FileLock(self.switcher.lock_file, timeout=5)
        if not lock.acquire():
            return False
        try:
            reg = registry.read_registry(self.switcher)
            row = reg.get("sessions", {}).get(self.managed_id)
            if row is None:
                return False
            flag = row.get("auth_recover")
            if flag is None:
                return False
            row["auth_recover"] = None
            registry.write_registry(self.switcher, reg)
            return (
                isinstance(flag, (int, float))
                and (time.time() - flag) <= _AUTH_RECOVER_TTL_S
            )
        finally:
            lock.release()

    def _profile_auth_invalid(self) -> bool:
        """Credit-free probe: does claude see this managed profile as logged-out?

        Uses ``claude auth status --json`` (a local check that makes NO API call,
        so it costs no credits) against the managed profile. Best-effort: a server-
        revoked-but-unexpired token can still pass the probe, so this may miss —
        the bounded attempt cap keeps that safe. Never raises.
        """
        try:
            _, email, org = self.switcher.resolve_account(self.account)
        except Exception:
            return False
        try:
            return not SessionManager(self.switcher)._is_session_valid(
                self.profile_dir, email, org
            )
        except Exception:
            self._logger.debug("auth probe failed", exc_info=True)
            return False

    # -- idle-5h-window priming (feature #3) ------------------------------

    def _maybe_prime_idle_windows(self) -> None:
        """Best-effort prime sweep of idle managed accounts; never raises/blocks long.

        Rides the resident watch loop (no new daemon). Self-gates three ways so it
        is cheap and safe:

        * a per-process local clock (skip most ticks without any I/O);
        * the balancer must be enabled (priming only matters while balancing);
        * a cross-process registry claim (``claim_prime_sweep`` under the lock) so
          exactly ONE supervisor sweeps per interval — no thundering herd.

        The decision of WHICH accounts to prime is the pure
        ``balancer.accounts_needing_prime`` (built from a world snapshot OUTSIDE
        the lock); each prime POST also runs OUTSIDE the lock. Only the sweep
        claim and the per-account guard stamps touch the registry, under the lock.
        Active accounts are never primed — only idle/inactive ones (we never touch
        an in-use account's credentials).
        """
        try:
            now = time.time()
            if (now - getattr(self, "_last_prime_attempt", 0.0)) < _PRIME_LOCAL_INTERVAL_S:
                return
            self._last_prime_attempt = now
            # Priming requires BOTH the balancer being enabled AND the dedicated,
            # default-OFF ``primeIdleWindows`` opt-in: it spends real credits on the
            # as-yet-unverified fixed-from-first-use 5h-window premise, so it must
            # never run by default. (Kept after the cheap local-interval check so a
            # disabled flag still costs ~nothing per tick.)
            bcfg = self.switcher.get_auto_balance_config()
            if not (bcfg["enabled"] and bcfg.get("primeIdleWindows")):
                return

            # (1) Claim the cross-process sweep under the lock (cheap, no network).
            lock = FileLock(self.switcher.lock_file, timeout=5)
            if not lock.acquire():
                return
            try:
                reg = registry.read_registry(self.switcher)
                if not registry.claim_prime_sweep(reg, now):
                    return  # another supervisor owns this interval
                registry.prune_primed(reg, now)
                guarded = {
                    num for num in reg.get("primed", {})
                    if registry.prime_guarded(reg, num, now)
                }
                registry.write_registry(self.switcher, reg)
            finally:
                lock.release()

            # (2) Build the world + decide candidates OUTSIDE the lock (network I/O).
            cfg = balancer.config_from_dict(self.switcher.get_auto_balance_config())
            reg = registry.read_registry(self.switcher)
            acct_views, _ = registry.build_world(self.switcher, reg, fetch_idle=True)
            candidates = [
                num for num in balancer.accounts_needing_prime(acct_views, cfg)
                if num != self.account and num not in guarded
            ]
            if not candidates:
                return

            # (3) Prime each candidate OUTSIDE the lock; stamp successes under it.
            for num in candidates:
                if self._prime_one_account(num):
                    self._stamp_primed(num)
        except Exception:  # noqa: BLE001 - priming is strictly best-effort
            self._logger.debug("prime sweep failed", exc_info=True)

    def _prime_one_account(self, num: str) -> bool:
        """Send one minimal credit-consuming call to start ``num``'s 5h window.

        Best-effort, never raises. Reads the INACTIVE account's stored credentials,
        refreshes an expired token (inactive accounts only — never an active one,
        whose creds Claude Code owns) persisting under the lock, then performs the
        prime POST OUTSIDE the lock. Returns whether the clock was started.
        """
        try:
            num, email, _ = self.switcher.resolve_account(num)
        except Exception:
            return False
        # Never prime/refresh the active default login: rotating its single-use
        # refresh token would force a re-login (same invariant build_world now
        # enforces). Its 5h window is anyway warmed by the user's own usage.
        if num == str(self.switcher.active_account_num() or ""):
            self._logger.debug("prime: skipping active-default account %s", num)
            return False
        try:
            creds = self.switcher.read_account_credentials(num, email)
        except Exception:
            self._logger.debug("prime: cannot read creds for %s", num, exc_info=True)
            return False
        if not creds:
            return False
        # Never prime a pay-as-you-go API / console account (or an unrecognized
        # auth type): priming bills real money and there is no 5h subscription
        # window to warm. Only recognized Claude subscription tiers qualify.
        if not oauth.is_primable_subscription(oauth.extract_subscription_type(creds)):
            self._logger.debug(
                "prime: skipping %s — not a primeable subscription account", num
            )
            return False
        token = oauth.extract_access_token(creds)
        if not token:
            return False

        # Refresh an expired token for this INACTIVE account through the locked,
        # double-checked chokepoint so two concurrent supervisors/renders never both
        # POST the single-use refresh token (which would revoke the family and log the
        # account out). A live session on this account => treat as active (claude owns
        # its credentials; don't rotate its refresh token).
        if not self.switcher._live_session_pids(num, email):
            creds = self.switcher.ensure_fresh_inactive_credentials(num, email, creds)
            token = oauth.extract_access_token(creds) or token
        return oauth.prime_account(token)

    def _stamp_primed(self, num: str) -> None:
        lock = FileLock(self.switcher.lock_file, timeout=5)
        if not lock.acquire():
            return
        try:
            reg = registry.read_registry(self.switcher)
            registry.stamp_primed(reg, num, time.time())
            registry.write_registry(self.switcher, reg)
        finally:
            lock.release()

    # -- actions ----------------------------------------------------------

    def _migrate(self, to_account: str) -> None:
        """Re-point this session's own profile to ``to_account`` (no relaunch).

        claude re-reads the seeded credentials on its keychain-cache / 401 cycle,
        the same contract the global switch relies on.
        """
        try:
            num, email, _ = self.switcher.resolve_account(to_account)
        except Exception:
            self._logger.warning("migrate: cannot resolve account %s", to_account)
            return
        self.switcher.seed_profile_credentials(
            self.profile_dir, num, email, cwd=self.cwd
        )
        lock = FileLock(self.switcher.lock_file, timeout=10)
        if lock.acquire():
            try:
                reg = registry.read_registry(self.switcher)
                row = reg.get("sessions", {}).get(self.managed_id)
                if row is not None:
                    now = time.time()
                    row["account_num"] = num
                    row["last_migrated_at"] = now
                    row["migration_count"] = int(row.get("migration_count", 0)) + 1
                    row["rate_limits"] = None    # don't judge the new account by old numbers
                    # Re-arm the cross-process headroom reservation on the NEW account
                    # (BUG-003): a just-migrated session is not yet reporting real
                    # usage, so without this it would contribute ZERO synthetic load
                    # and a second, independently-stranded session's separate
                    # build_world pass would see this target as ~empty and stack onto
                    # it, co-exhausting it. Stamping reserved_at makes
                    # _reserve_load_by_account attribute _pct_cost(ctx_tokens) of load
                    # to the new account for _RESERVE_TTL_S (exactly like a freshly
                    # launched session) until the real rate_limits land — so the
                    # second session sees the target as full and pauses/spreads
                    # instead of stacking. Closes the co-exhaust race with no timer.
                    row["reserved_at"] = now
                    row["_prev_max_pct"] = None   # fresh rising-edge basis on next tick
                    row["paused_until"] = None    # re-pointed to a fresh account -> running, not paused
                    row["migration"] = None
                    registry.write_registry(self.switcher, reg)
            finally:
                lock.release()
        self.account = num
        print(f"\n{accent('Migrated')} managed session to Account-{num} ({email})")

    def _should_handle_limit_exit(self) -> bool:
        """Whether a child exit looks like a rate-limit exit we should recover from.

        Claude Code hard-exits when a subscription limit is hit. We treat an exit
        as limit-driven when the balancer is enabled and this session's account is
        currently at/over the exhaust threshold. (A clean user quit on a healthy
        account just ends the session.)
        """
        if not self.switcher.get_auto_balance_config()["enabled"]:
            return False
        cfg = balancer.config_from_dict(self.switcher.get_auto_balance_config())
        av = self._account_view()
        return av is not None and av.max_pct is not None and av.max_pct >= cfg.exhaust_threshold

    def _resumable(self, acct_views: dict, sv, cfg) -> bool:
        """Whether the session can run again right now.

        True when its current account has recovered below the hysteresis line OR
        a fitting migration target exists. This matches the *placement* gate
        (``choose_migration_target`` / ``_fits``), so we never resume onto an
        account that is below the loose ``_usable`` line but still too full to
        actually host the session — the asymmetry that would otherwise cause a
        relaunch livelock.

        When the API-rate last-resort tier is enabled (``not only_subscription``),
        the current account staying runnable via its own pay-as-you-go capacity
        also counts — ``choose_migration_target`` returns ``None`` in that case
        (it keeps the session put rather than thrashing), so check it explicitly.
        """
        cur = acct_views.get(self.account)
        return (
            balancer._usable(cur, cfg)
            or (not cfg.only_subscription and balancer._api_capable(cur, cfg))
            or balancer.choose_migration_target(sv, acct_views, {}, cfg) is not None
        )

    def _pause_and_resume(self) -> None:
        """Get the session onto a runnable account, pausing as long as needed.

        If a fitting account exists now, re-point and return immediately (the
        migrate-on-exhaustion fast path). Otherwise pause to the soonest real
        reset, wait, and re-check on wake — only returning once the session is
        genuinely resumable. Never resumes into a still-capped account, so there
        is no tight relaunch loop. Interruptible via Ctrl-C.
        """
        cfg = balancer.config_from_dict(self.switcher.get_auto_balance_config())
        sv = self._self_session_view()
        while True:
            reg = registry.read_registry(self.switcher)
            # THE incident fix: probe a usage-429-backed-off idle account so a
            # stranded session discovers it has headroom and migrates instead of
            # pausing an hour. A credit is worth avoiding the stall.
            acct_views, _ = registry.build_world(
                self.switcher, reg, fetch_idle=True, probe_unavailable=True
            )
            if self._resumable(acct_views, sv, cfg):
                break
            action = balancer.pause_decision(sv, acct_views, time.time(), cfg)
            self._pending_resume_at = action.resume_at or int(time.time() + cfg.pause_fallback_s)
            self._mark_paused(self._pending_resume_at)
            self._wait_for_reset()
        self._reassign_for_resume()

    def _pause_status_lines(self, acct_views: dict, cfg) -> list[str]:
        """Compact per-account usage lines shown beneath the pause countdown.

        Uses the best-available signal already in ``acct_views`` (live / cache /
        probe / stale) and, for an account whose usage endpoint is 429-backed-off so
        it has NO signal (``max_pct is None``), falls back to the last-known cached
        reading — so an idle account that is really at ~27% shows that number rather
        than a bare "unknown" while the usage API is rate-limited. Tags the session's
        current account and any account usable as a resume target right now, so when
        the balancer pauses despite a healthy account the discrepancy is visible (and
        the user can press Enter to re-check). Best-effort: never raises.
        """
        from claude_swap.cache import (
            MISSING,
            STALE_USAGE_MAX_AGE_S,
            last_known_usage,
            read_cache,
        )

        try:
            data = self.switcher._get_sequence_data() or {}
            accounts = data.get("accounts", {})
            order = [str(n) for n in data.get("sequence", [])]
            for num in accounts:
                if str(num) not in order:
                    order.append(str(num))
            persisted = read_cache(
                self.switcher.backup_dir / "cache" / "usage.json", float("inf")
            )
            persisted = persisted if (persisted is not MISSING and isinstance(persisted, dict)) else {}

            lines: list[str] = []
            for num in order:
                av = acct_views.get(num)
                email = accounts.get(num, {}).get("email", "") or "?"
                # Usage / status text from the best signal available.
                if av is not None and av.signal == "logged_out":
                    usage = dimmed("logged out — re-login")
                elif av is not None and av.max_pct is not None:
                    h5, d7 = av.five_hour_pct, av.seven_day_pct
                    if h5 is not None or d7 is not None:
                        parts = []
                        if h5 is not None:
                            parts.append(f"5h {h5:>3.0f}%")
                        if d7 is not None:
                            parts.append(f"7d {d7:>3.0f}%")
                        usage = muted("  ".join(parts))
                    else:
                        usage = muted(f"~{av.max_pct:>3.0f}%")
                    if av.signal == "probe":
                        usage += dimmed(" (probe-ok)")
                    elif av.signal == "stale":
                        usage += dimmed(" (cached)")
                else:
                    lk = last_known_usage(persisted.get(num), STALE_USAGE_MAX_AGE_S)
                    pcts = [
                        w["pct"]
                        for w in (lk.values() if isinstance(lk, dict) else [])
                        if isinstance(w, dict) and isinstance(w.get("pct"), (int, float))
                    ]
                    usage = (
                        muted(f"~{max(pcts):>3.0f}% (cached)") if pcts else dimmed("checking…")
                    )
                # Tag: current session account, else a usable resume target.
                if str(num) == str(self.account):
                    tag = "  " + bold_accent("(current)")
                elif balancer._usable(av, cfg):
                    tag = "  " + muted("available")
                else:
                    tag = ""
                lines.append(f"  {dimmed('a' + str(num))} {email:<28} {usage}{tag}")
            return lines
        except Exception:  # noqa: BLE001 - the pause display is best-effort
            self._logger.debug("pause status render failed", exc_info=True)
            return []

    def _wait_for_reset(self) -> None:
        """Foreground wait until the pause horizon OR the session becomes resumable.

        Interruptible (Ctrl-C propagates). Renders a live countdown PLUS a compact
        per-account usage list (so the user can see which account has headroom), and
        lets the user press **Enter** to force an immediate re-check instead of
        waiting out the up-to-30s tick. The early break uses the same ``_resumable``
        gate as placement, so it can't wake into a still-full account.

        Rendering degrades gracefully off a TTY (printed once, no ANSI, no per-tick
        spam); the Enter shortcut is POSIX-tty only (falls back to a plain sleep on
        Windows / piped stdin). Ctrl-C is never swallowed — it propagates to
        :meth:`run`'s handler.
        """
        resume_at = getattr(self, "_pending_resume_at", int(time.time()))
        cfg = balancer.config_from_dict(self.switcher.get_auto_balance_config())
        sv = self._self_session_view()
        is_tty = self._stdout_isatty()
        # Enter-to-recheck only when we can poll a real stdin tty (not Windows, not a
        # pipe). EOF on stdin disables it mid-wait to avoid a busy-loop (see below).
        can_select = (
            sys.platform != "win32"
            and hasattr(sys.stdin, "isatty")
            and self._safe_isatty(sys.stdin)
        )
        prev_lines = 0
        try:
            while True:
                now = time.time()
                remaining = int(resume_at - now)
                if remaining <= 0:
                    break
                reg = registry.read_registry(self.switcher)
                # Probe a usage-backed-off idle target so the wait wakes the instant a
                # probe confirms headroom, rather than sleeping out the whole timer.
                acct_views, _ = registry.build_world(
                    self.switcher, reg, fetch_idle=True, probe_unavailable=True
                )
                if self._resumable(acct_views, sv, cfg):
                    break
                mins, secs = divmod(remaining, 60)
                hours, mins = divmod(mins, 60)
                cd = f"{hours}h{mins:02d}m" if hours else f"{mins}m{secs:02d}s"
                hint = "  (press Enter to re-check now)" if (can_select and is_tty) else ""
                head = dimmed(f"Usage limit reached — auto-resuming in {cd}{hint}")
                block = self._pause_status_lines(acct_views, cfg)
                prev_lines = self._render_pause_frame(head, block, prev_lines, is_tty)

                timeout = min(30.0, max(1.0, remaining))
                if can_select:
                    try:
                        readable, _, _ = select.select([sys.stdin], [], [], timeout)
                    except (OSError, ValueError):
                        # stdin not selectable after all -> stop trying, sleep instead.
                        can_select = False
                        time.sleep(timeout)
                    else:
                        if readable:
                            line = self._consume_stdin_line()
                            if line == "":
                                # EOF (Ctrl-D / closed pipe): select would report it
                                # readable forever -> disable to avoid a busy-loop.
                                can_select = False
                            # Enter (or any input) pressed -> loop now for an
                            # immediate re-check, skipping the rest of the wait.
                else:
                    time.sleep(timeout)
        finally:
            self._clear_pause_frame(prev_lines, is_tty)

    @staticmethod
    def _stdout_isatty() -> bool:
        try:
            return bool(sys.stdout.isatty())
        except Exception:  # noqa: BLE001 - non-tty / closed stream
            return False

    @staticmethod
    def _safe_isatty(stream) -> bool:
        try:
            return bool(stream.isatty())
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _consume_stdin_line() -> str:
        """Read one pending line from stdin (the Enter press). ``""`` on EOF/error."""
        try:
            return sys.stdin.readline()
        except (OSError, ValueError):
            return ""

    def _render_pause_frame(
        self, head: str, block: list[str], prev_lines: int, is_tty: bool
    ) -> int:
        """Draw the countdown + account block, returning the line count drawn.

        On a TTY each tick moves the cursor back to the top of the previous frame
        and clears to end-of-screen (``\\033[J``) before redrawing, so a frame that
        grows or shrinks (an account logging out / a probe landing) never smears.
        Off a TTY the frame is printed exactly ONCE (when ``prev_lines == 0``) with no
        ANSI, so a piped/redirected stdout isn't spammed every tick.
        """
        lines = [head, *block]
        if not is_tty:
            if prev_lines == 0:
                sys.stdout.write("\n".join(lines) + "\n")
                sys.stdout.flush()
                return len(lines)
            return prev_lines
        out = []
        if prev_lines:
            out.append(f"\033[{prev_lines}A")  # cursor up to the top of the frame
        out.append("\r\033[J")  # clear from here to end of screen
        # Trailing newline leaves the cursor on the line BELOW the block, so the
        # next tick's cursor-up count is exactly the line count drawn here.
        out.append("\n".join(lines) + "\n")
        sys.stdout.write("".join(out))
        sys.stdout.flush()
        return len(lines)

    def _clear_pause_frame(self, prev_lines: int, is_tty: bool) -> None:
        """Erase the rendered pause frame so the next message starts clean."""
        if not prev_lines:
            return
        if is_tty:
            sys.stdout.write(f"\033[{prev_lines}A\r\033[J")
        else:
            sys.stdout.write("\n")
        sys.stdout.flush()

    def _reassign_for_resume(self) -> None:
        """Point the profile at the best runnable account, then clear the pause.

        Only called once :meth:`_resumable` holds, so either the current account
        recovered (keep it) or a fitting target exists (migrate to it).
        """
        cfg = balancer.config_from_dict(self.switcher.get_auto_balance_config())
        reg = registry.read_registry(self.switcher)
        # Probe on wake so this pass agrees with the _resumable gate that let it
        # proceed (it must see the same probe-confirmed account to migrate onto).
        acct_views, _ = registry.build_world(
            self.switcher, reg, fetch_idle=True, probe_unavailable=True
        )
        sv = self._self_session_view()
        if not balancer._usable(acct_views.get(self.account), cfg):
            target = balancer.choose_migration_target(sv, acct_views, {}, cfg)
            if target and target != self.account:
                self._migrate(target)
        self._mark_paused(None)

    # -- profile + registry helpers --------------------------------------

    # Session-history items symlinked from the default ~/.claude into a managed
    # profile so a balanced session is continuous with the user's normal claude
    # history: `cswap launch -- --resume <id>` / `--continue` can resume sessions
    # started with plain claude, and a managed session's own transcript shows up
    # in the user's regular history. (Unlike `cswap run`, a balanced session is
    # account-agnostic — it migrates across accounts — so per-account history is
    # not wanted here. Credentials stay isolated; only history is shared.)
    _SHARED_HISTORY = ("projects", "todos", "shell-snapshots")

    def _bootstrap_profile(self) -> None:
        self.switcher.managed_dir.mkdir(parents=True, exist_ok=True)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            os.chmod(self.profile_dir, 0o700)
        num, email, _ = self.switcher.resolve_account(self.account)
        # Verbatim seed (no refresh side-effects), share ~/.claude items, then
        # install the non-shared statusline + effort layer over the symlink.
        # Pre-trust the launch cwd so claude skips the folder trust dialog.
        self.switcher.seed_profile_credentials(
            self.profile_dir, num, email, cwd=self.cwd
        )
        SessionManager(self.switcher)._sync_sharing(self.profile_dir, self.share)
        embed.install_into_profile(self.switcher, self.profile_dir)
        if self.share:
            self._share_history()

    def _share_history(self) -> None:
        """Symlink the default profile's session history into this managed profile.

        Makes ``--resume``/``--continue`` of externally-started sessions work and
        unifies history. POSIX only (symlinks); on Windows a managed profile keeps
        isolated history, so resuming a session started outside cswap is
        unsupported there. Best-effort: a failure just leaves that item unshared.
        """
        if sys.platform == "win32":
            return
        source_root = Path.home() / ".claude"
        for name in self._SHARED_HISTORY:
            src = source_root / name
            dest = self.profile_dir / name
            if not src.exists() or dest.exists() or dest.is_symlink():
                continue
            try:
                dest.symlink_to(src)
            except OSError:
                self._logger.debug(
                    "could not share %s into managed profile", name, exc_info=True
                )

    def _register(self) -> None:
        with FileLock(self.switcher.lock_file):
            reg = registry.read_registry(self.switcher)
            registry.reap_dead(reg)
            registry.upsert_session(
                reg,
                self.managed_id,
                account_num=self.account,
                profile_dir=str(self.profile_dir),
                cwd=self.cwd,
                supervisor_pid=os.getpid(),
                last_seen=time.time(),
                # Mark the moment this session was placed so concurrent launchers
                # count it as load until its real usage lands (BUG 003): a
                # freshly-registered row has ``rate_limits=None`` (zero apparent
                # load), so without a reservation every parallel `cswap launch`
                # picks the same account and breaks the `cmux N` spread.
                reserved_at=time.time(),
            )
            registry.write_registry(self.switcher, reg)

    def _deregister(self) -> None:
        try:
            with FileLock(self.switcher.lock_file, timeout=5):
                reg = registry.read_registry(self.switcher)
                reg.get("sessions", {}).pop(self.managed_id, None)
                registry.write_registry(self.switcher, reg)
        except Exception:
            self._logger.debug("deregister failed", exc_info=True)

    def _set_pids(self, claude_pid: int) -> None:
        lock = FileLock(self.switcher.lock_file, timeout=5)
        if not lock.acquire():
            return
        try:
            reg = registry.read_registry(self.switcher)
            row = reg.get("sessions", {}).get(self.managed_id)
            if row is not None:
                row["claude_pid"] = claude_pid
                row["supervisor_pid"] = os.getpid()
                # Capture claude's own session id for --resume from the registry
                # (the statusline records it once it has rendered at least once).
                self._claude_session_id = row.get("claude_session_id", "") or self._claude_session_id
                registry.write_registry(self.switcher, reg)
        finally:
            lock.release()

    def _mark_paused(self, until: int | None) -> None:
        lock = FileLock(self.switcher.lock_file, timeout=5)
        if not lock.acquire():
            return
        try:
            reg = registry.read_registry(self.switcher)
            row = reg.get("sessions", {}).get(self.managed_id)
            if row is not None:
                row["paused_until"] = until
                registry.write_registry(self.switcher, reg)
        finally:
            lock.release()

    def _account_view(self) -> balancer.AccountView | None:
        reg = registry.read_registry(self.switcher)
        acct_views, _ = registry.build_world(self.switcher, reg, fetch_idle=True)
        return acct_views.get(self.account)

    def _current_claude_session_id(self) -> str:
        """Read claude's own session id for ``--resume`` from this session's row.

        The statusline records ``claude_session_id`` once claude has rendered at
        least once, so this is the authoritative value for an auto-resume — read
        it fresh under the lock right before each (re)launch rather than trusting
        the cached ``_claude_session_id`` (which lags by one Popen). Degrades to
        ``""`` (launch fresh) when the lock can't be acquired or the row is gone.
        """
        lock = FileLock(self.switcher.lock_file, timeout=5)
        if not lock.acquire():
            return ""
        try:
            reg = registry.read_registry(self.switcher)
            row = reg.get("sessions", {}).get(self.managed_id)
            if row is None:
                return ""
            return row.get("claude_session_id", "") or ""
        finally:
            lock.release()

    def _self_session_view(self) -> balancer.SessionView:
        """Build this session's :class:`balancer.SessionView` from its registry row.

        The recovery paths (pause/resume/reassign) feed the pure balancer a view
        of *this* session. Reading the live row keeps ``ctx_tokens`` (and the
        pause/migration/pin fields) accurate so the per-session cost reserve
        (``_pct_cost``) and the placement gate aren't undersized by defaulting
        everything to zero. Falls back to a bare view when the row is absent.
        """
        reg = registry.read_registry(self.switcher)
        row = reg.get("sessions", {}).get(self.managed_id) or {}
        return balancer.SessionView(
            self.managed_id,
            self.account,
            ctx_tokens=int(row.get("ctx_tokens") or 0),
            paused_until=row.get("paused_until"),
            last_migrated_at=float(row.get("last_migrated_at") or 0.0),
            pinned_account=row.get("pinned_account"),
        )

    def _registry_mtime(self) -> float:
        try:
            return registry.registry_path(self.switcher).stat().st_mtime
        except OSError:
            return 0.0

    # -- env / args / process --------------------------------------------

    def _session_env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in AUTH_OVERRIDE_ENV_VARS}
        env["CLAUDE_CONFIG_DIR"] = str(self.profile_dir)
        effort = self.switcher.get_auto_balance_config()["effortLevel"]
        if effort:
            env["CLAUDE_CODE_EFFORT_LEVEL"] = effort
        # Transient resilience: give managed turns more native retry-with-backoff
        # so a dropped socket / connection reset / timeout / brief 5xx self-heals
        # within the turn instead of failing it. Respect an explicit user override.
        if "CLAUDE_CODE_MAX_RETRIES" not in env:
            env["CLAUDE_CODE_MAX_RETRIES"] = _DEFAULT_MAX_RETRIES
        # Force main-transcript persistence. cmux sets ``CLAUDE_CODE_CHILD_SESSION=1``
        # in the surface env a managed launch inherits; Claude Code treats that — for
        # an INTERACTIVE, non-subagent session with no ``TMUX`` marker — as a throwaway
        # child session and SKIPS writing the ``<sessionId>.jsonl`` transcript (the file
        # ``claude --resume`` loads), while still writing aux artifacts. A managed
        # session is first-class and its transcript MUST persist so the auto-resume /
        # ``cswap launch -- --resume <id>`` contract works and no chat history is lost.
        # ``CLAUDE_CODE_FORCE_SESSION_PERSISTENCE`` re-enables it (verified against
        # Claude Code 2.1.178; the cmux wrapper passes this key through untouched).
        # Respect an explicit user override.
        if "CLAUDE_CODE_FORCE_SESSION_PERSISTENCE" not in env:
            env["CLAUDE_CODE_FORCE_SESSION_PERSISTENCE"] = "1"
        # When launched inside a cmux workspace, the cmux-claude-wrapper strips
        # auth-selection env before exec'ing the real claude — which would drop
        # our CLAUDE_CONFIG_DIR profile pin. cmux honours an allow-list env key;
        # ensure CLAUDE_CONFIG_DIR is on it (appending, never clobbering).
        if env.get("CMUX_SURFACE_ID"):
            from claude_swap.cmux import PRESERVE_KEYS_ENV, merge_preserve_keys

            env[PRESERVE_KEYS_ENV] = merge_preserve_keys(env.get(PRESERVE_KEYS_ENV))
        return env

    def _qol_args(self, claude_args: list[str]) -> list[str]:
        """Prepend QoL launch flags, skipping any the user already passed."""
        bcfg = self.switcher.get_auto_balance_config()
        args = list(claude_args)
        joined = " ".join(args)
        prefix: list[str] = []
        if "--model" not in args and "-m" not in args:
            prefix += ["--model", bcfg["model"]]
        if "--dangerously-skip-permissions" not in joined and "--permission-mode" not in joined:
            prefix += ["--dangerously-skip-permissions"]
        # Auto-fall-back to a secondary model on a 529 Overloaded (server/model-
        # side; an account switch wouldn't help). User-supplied --fallback-model
        # wins, so only add ours when they didn't pass one.
        if "--fallback-model" not in args:
            prefix += ["--fallback-model", _DEFAULT_FALLBACK_MODEL]
        return prefix + args

    def _terminate(self, proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=_TERMINATE_GRACE_S)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception:
            self._logger.debug("terminate failed", exc_info=True)

    def _reap_or_terminate(self, proc: subprocess.Popen) -> None:
        """Wait briefly for a child exiting on its own, force-terminate if wedged.

        On Ctrl-C the child received the same SIGINT and is running its own quit
        path (printing its resume hint); give it the grace period to finish before
        escalating, so we don't SIGTERM it out from under its own clean exit.
        """
        try:
            proc.wait(timeout=_TERMINATE_GRACE_S)
        except subprocess.TimeoutExpired:
            self._terminate(proc)
        except Exception:
            self._terminate(proc)

    def _install_passive_sigint(self):
        """Make the parent ignore SIGINT so the child (claude) owns Ctrl-C.

        A no-op *function* handler — deliberately not ``SIG_IGN``: a caught handler
        is reset to the default on the child's ``exec`` (POSIX), so claude still
        receives SIGINT and handles it itself, whereas ``SIG_IGN`` would be
        inherited and leave claude unable to react to Ctrl-C. Returns the previous
        handler to restore, or ``_NO_SIGNAL`` when signals can't be set here (e.g.
        not the main thread).
        """
        try:
            return signal.signal(signal.SIGINT, lambda *_: None)
        except (ValueError, OSError):
            return _NO_SIGNAL

    def _restore_sigint(self, prev) -> None:
        if prev is _NO_SIGNAL or prev is None:
            return
        try:
            signal.signal(signal.SIGINT, prev)
        except (ValueError, OSError, TypeError):
            self._logger.debug("restore SIGINT failed", exc_info=True)

    def _print_resume_hint(self) -> None:
        """Print a balancer-aware resume hint on a clean user quit.

        claude prints its own ``claude --resume <id>`` hint, but resuming that way
        escapes the balancer (no managed profile, no migration). Point the user at
        the managed resume instead. Only meaningful when history is shared (the
        default): with ``--no-share`` the managed transcript is isolated, so the
        hint is suppressed. Must be called BEFORE ``_deregister`` (it reads the row).
        """
        if not self.share:
            return
        sid = self._current_claude_session_id()
        if not sid:
            return
        print(
            f"{dimmed('Resume this balanced session with:')} "
            f"{accent(f'cswap launch -- --resume {sid}')}"
        )
