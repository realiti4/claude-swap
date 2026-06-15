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
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from claude_swap import balancer, embed, registry
from claude_swap.exceptions import SessionError
from claude_swap.locking import FileLock
from claude_swap.printer import accent, dimmed, muted, warning
from claude_swap.session import AUTH_OVERRIDE_ENV_VARS, SessionManager

# How long to wait for a SIGTERM'd child to exit before force-killing it.
_TERMINATE_GRACE_S = 5.0
# Supervisor wake cadence: block on the child for at most this long, then check
# the registry for an intent/pause (only re-reads when the file's mtime moved).
_WATCH_TICK_S = 1.0


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
                args = (["--resume", self._claude_session_id] if resume and self._claude_session_id
                        else list(base_args))
                proc = subprocess.Popen([claude_bin, *args], env=env, cwd=self.cwd)
                self._set_pids(proc.pid)
                outcome, rc = self._supervise(proc)

                if outcome == "exit":
                    if not self._should_handle_limit_exit():
                        self._deregister()  # clean user quit on a healthy account
                        return rc
                    self._pause_and_resume()  # migrate now, or pause + auto-resume
                else:
                    # outcome == "pause": child was SIGTERM'd to honour a pause.
                    self._pause_and_resume()
                resume = True
        except KeyboardInterrupt:
            if proc is not None:
                self._terminate(proc)
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
            mtime = self._registry_mtime()
            if mtime == last_mtime:
                continue
            last_mtime = mtime
            decision = self._consume_own_state()
            if decision == "pause":
                self._terminate(proc)
                return ("pause", None)
            # "migrate" was applied in place (no relaunch); keep watching.

    def _consume_own_state(self) -> str | None:
        """Act on this session's own registry row: migration intent or pause.

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
            if intent and intent.get("to"):
                to_account = str(intent["to"])
                registry.clear_intent(reg, self.managed_id)
                registry.write_registry(self.switcher, reg)
            else:
                to_account = None
        finally:
            lock.release()
        if to_account is not None:
            self._migrate(to_account)
            return "migrate"
        if isinstance(paused_until, (int, float)) and paused_until > time.time():
            self._pending_resume_at = int(paused_until)
            return "pause"
        return None

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
        self.switcher.seed_profile_credentials(self.profile_dir, num, email)
        lock = FileLock(self.switcher.lock_file, timeout=10)
        if lock.acquire():
            try:
                reg = registry.read_registry(self.switcher)
                row = reg.get("sessions", {}).get(self.managed_id)
                if row is not None:
                    row["account_num"] = num
                    row["last_migrated_at"] = time.time()
                    row["migration_count"] = int(row.get("migration_count", 0)) + 1
                    row["rate_limits"] = None    # don't judge the new account by old numbers
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
        """
        return (
            balancer._usable(acct_views.get(self.account), cfg)
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
        sv = balancer.SessionView(self.managed_id, self.account)
        while True:
            reg = registry.read_registry(self.switcher)
            acct_views, _ = registry.build_world(self.switcher, reg, fetch_idle=True)
            if self._resumable(acct_views, sv, cfg):
                break
            action = balancer.pause_decision(sv, acct_views, time.time(), cfg)
            self._pending_resume_at = action.resume_at or int(time.time() + cfg.pause_fallback_s)
            self._mark_paused(self._pending_resume_at)
            self._wait_for_reset()
        self._reassign_for_resume()

    def _wait_for_reset(self) -> None:
        """Foreground wait until the pause horizon OR the session becomes resumable.

        Interruptible (Ctrl-C propagates). Renders a countdown so the wait is
        always visible — never a hidden background sleep. The early break uses the
        same ``_resumable`` gate as placement, so it can't wake into a still-full
        account.
        """
        resume_at = getattr(self, "_pending_resume_at", int(time.time()))
        cfg = balancer.config_from_dict(self.switcher.get_auto_balance_config())
        sv = balancer.SessionView(self.managed_id, self.account)
        while True:
            now = time.time()
            remaining = int(resume_at - now)
            if remaining <= 0:
                break
            reg = registry.read_registry(self.switcher)
            acct_views, _ = registry.build_world(self.switcher, reg, fetch_idle=True)
            if self._resumable(acct_views, sv, cfg):
                break
            mins, secs = divmod(remaining, 60)
            hours, mins = divmod(mins, 60)
            cd = f"{hours}h{mins:02d}m" if hours else f"{mins}m{secs:02d}s"
            sys.stdout.write(
                f"\r{dimmed('Usage limit reached — auto-resuming in ' + cd + '  ')}"
            )
            sys.stdout.flush()
            time.sleep(min(30.0, max(1.0, remaining)))
        sys.stdout.write("\r" + " " * 60 + "\r")
        sys.stdout.flush()

    def _reassign_for_resume(self) -> None:
        """Point the profile at the best runnable account, then clear the pause.

        Only called once :meth:`_resumable` holds, so either the current account
        recovered (keep it) or a fitting target exists (migrate to it).
        """
        cfg = balancer.config_from_dict(self.switcher.get_auto_balance_config())
        reg = registry.read_registry(self.switcher)
        acct_views, _ = registry.build_world(self.switcher, reg, fetch_idle=True)
        sv = balancer.SessionView(self.managed_id, self.account)
        if not balancer._usable(acct_views.get(self.account), cfg):
            target = balancer.choose_migration_target(sv, acct_views, {}, cfg)
            if target and target != self.account:
                self._migrate(target)
        self._mark_paused(None)

    # -- profile + registry helpers --------------------------------------

    def _bootstrap_profile(self) -> None:
        self.switcher.managed_dir.mkdir(parents=True, exist_ok=True)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            os.chmod(self.profile_dir, 0o700)
        num, email, _ = self.switcher.resolve_account(self.account)
        # Verbatim seed (no refresh side-effects), share ~/.claude items, then
        # install the non-shared statusline + effort layer over the symlink.
        self.switcher.seed_profile_credentials(self.profile_dir, num, email)
        SessionManager(self.switcher)._sync_sharing(self.profile_dir, self.share)
        embed.install_into_profile(self.switcher, self.profile_dir)

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
