"""Opt-in keeper for Claude subscription five-hour usage windows.

The usage endpoint is checked first. A real, minimal Haiku request is sent when
the five-hour window is absent or expired. If usage remains unavailable after a
fresh probe, one guarded request is still attempted: successful and ambiguous
outcomes are protected in state so the fallback cannot become a request every
poll. Disabled, non-OAuth, non-switchable, and known-weekly-exhausted accounts
still fail closed.

Warm requests run through persistent per-account session profiles, never by
switching the globally active login. A small state file prevents duplicate
spend when a just-warmed usage snapshot remains briefly cached or the process
is restarted.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from claude_swap.exceptions import ClaudeSwitchError, PromptOutcomeUnknown, WarmupError
from claude_swap.locking import FileLock
from claude_swap.models import AccountSnapshot
from claude_swap.poll_policy import parse_reset_ts
from claude_swap.session import SessionManager
from claude_swap.settings import atomic_write_json
from claude_swap.usage_store import STALE_OK_S

if TYPE_CHECKING:
    from claude_swap.switcher import ClaudeAccountSwitcher


DEFAULT_INTERVAL_SECONDS = 600.0
MIN_INTERVAL_SECONDS = 300.0
DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_MODEL = "claude-haiku-4-5"
FIVE_HOUR_SECONDS = 5 * 60 * 60
PENDING_GUARD_SECONDS = FIVE_HOUR_SECONDS
STATE_SCHEMA_VERSION = 2
LEGACY_STATE_SCHEMA_VERSION = 1
WARMUP_PROMPT = "Reply only: OK"


@dataclass(frozen=True)
class WarmupEvent:
    """One account-level decision made during a warm-up tick."""

    kind: str
    account_number: str
    email: str
    detail: str


@dataclass(frozen=True)
class WarmupSummary:
    """Aggregate result of one warm-up tick."""

    warmed: int = 0
    would_warm: int = 0
    skipped: int = 0
    failed: int = 0


class WarmupEngine:
    """Inspect managed accounts and warm cold or unconfirmed five-hour windows."""

    def __init__(
        self,
        switcher: ClaudeAccountSwitcher,
        *,
        emit: Callable[[WarmupEvent], None],
        session_manager: SessionManager | None = None,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        model: str = DEFAULT_MODEL,
        dry_run: bool = False,
        clock: Callable[[], float] = time.time,
    ):
        self.switcher = switcher
        self.emit = emit
        self.sessions = session_manager or SessionManager(switcher)
        self.interval_seconds = interval_seconds
        self.timeout_seconds = timeout_seconds
        self.model = model
        self.dry_run = dry_run
        self.clock = clock
        self.state_path = switcher.backup_dir / "warmup_state.json"
        self.lock_path = switcher.backup_dir / ".warmup.lock"
        self._stopped = threading.Event()

    def stop(self) -> None:
        """Request a foreground loop shutdown."""
        self._stopped.set()

    def run_loop(self) -> int:
        """Run ticks until stopped; return nonzero if the final tick failed."""
        exit_code = 0
        while not self._stopped.is_set():
            summary = self.tick()
            exit_code = 1 if summary.failed else 0
            if self._stopped.wait(self.interval_seconds):
                break
        return exit_code

    def tick(self) -> WarmupSummary:
        """Run one serialized usage-check and warm pass."""
        if self._stopped.is_set():
            return WarmupSummary()
        with FileLock(self.lock_path, timeout=1.0):
            if self._stopped.is_set():
                return WarmupSummary()
            return self._tick_locked()

    def _tick_locked(self) -> WarmupSummary:
        now = self.clock()
        state = self._load_state()
        snapshot = self.switcher.accounts_snapshot(fetch=None)
        freshen = {
            account.number
            for account in snapshot.accounts
            if self._needs_fresh_probe(account, state, now)
        }
        if freshen:
            # An explicit fetch set may bypass an idle account's long poll plan,
            # while UsageStore still enforces its serve TTL, claims, and backoff.
            # Only stale accounts that look cold are escalated, and a persisted
            # successful warm suppresses further probes for the whole window.
            snapshot = self.switcher.accounts_snapshot(fetch=freshen)
        warmed = would_warm = skipped = failed = 0

        for account in snapshot.accounts:
            if self._stopped.is_set():
                break
            account_now = self.clock()
            reason = self._skip_reason(account, state, account_now)
            if reason is not None:
                skipped += 1
                self._emit(account, reason, self._reason_detail(reason))
                continue

            if self.dry_run:
                would_warm += 1
                self._emit(
                    account,
                    "would-warm",
                    f"would send one minimal {self.model} request",
                )
                continue

            attempt_at = self.clock()
            # Persist the latest possible acceptance time before launch. If
            # this process dies inside run_prompt, the on-disk guard still
            # covers a request accepted at the end of the timeout window.
            self._mark_pending(state, account, attempt_at + self.timeout_seconds)
            self._save_state(state)
            if self._stopped.is_set():
                self._clear_pending(state, account)
                self._save_state(state)
                break
            try:
                result = self.sessions.run_prompt(
                    account.number,
                    self._claude_args(),
                    timeout=self.timeout_seconds,
                    expected_identity=(account.email, account.org_uuid),
                )
            except PromptOutcomeUnknown as exc:
                # Anthropic may have accepted the request before the local
                # timeout. Keep pendingAt for a full window so unavailable
                # usage cannot turn ambiguity into one request every poll.
                self._mark_pending(state, account, self.clock())
                self._save_state(state)
                failed += 1
                self._emit(account, "failed", self._clean_detail(str(exc)))
                continue
            except (ClaudeSwitchError, OSError) as exc:
                self._clear_pending(state, account)
                self._save_state(state)
                failed += 1
                self._emit(account, "failed", self._clean_detail(str(exc)))
                continue

            if result.returncode != 0:
                # A child can exit nonzero after the service accepted its
                # message. Preserve pendingAt and require a later fresh probe.
                self._mark_pending(state, account, self.clock())
                self._save_state(state)
                failed += 1
                self._emit(
                    account,
                    "failed",
                    f"Claude exited with code {result.returncode}; retry protected",
                )
                continue

            self._mark_warmed(state, account, self.clock())
            self._save_state(state)
            warmed += 1
            self._emit(
                account,
                "warmed",
                f"started a five-hour window with one minimal {self.model} request",
            )

        return WarmupSummary(
            warmed=warmed,
            would_warm=would_warm,
            skipped=skipped,
            failed=failed,
        )

    def _needs_fresh_probe(
        self, account: AccountSnapshot, state: dict, now: float
    ) -> bool:
        if (
            account.disabled
            or account.kind != "oauth"
            or not account.switchable
            or self._state_is_recent(state, account, now)
        ):
            return False
        entry = account.usage
        if entry.last_error is None and entry.age_s is not None and entry.age_s <= STALE_OK_S:
            return False
        usage = entry.last_good
        if not isinstance(usage, dict):
            return True
        if self._stale_weekly_is_exhausted(usage, now):
            return False

        weekly = usage.get("seven_day")
        if isinstance(weekly, dict) and isinstance(weekly.get("pct"), (int, float)):
            if float(weekly["pct"]) >= 100.0:
                reset = parse_reset_ts(weekly.get("resets_at"))
                # A known future weekly reset makes a prompt pointless. Any
                # less complete stale shape should still get a fresh probe.
                return reset is None or reset <= now

        five_hour = usage.get("five_hour")
        if not isinstance(five_hour, dict):
            return True
        reset = parse_reset_ts(five_hour.get("resets_at"))
        # A stale percentage without an absolute deadline cannot prove that
        # its five-hour window is still live.
        return reset is None or reset <= now

    def _skip_reason(self, account: AccountSnapshot, state: dict, now: float) -> str | None:
        if account.disabled:
            return "disabled"
        if account.kind != "oauth":
            return "not-oauth"
        if not account.switchable:
            return "unavailable"
        if self._state_is_recent(state, account, now):
            return "recently-warmed"

        entry = account.usage
        usage = entry.decision_value()
        usage_is_fresh = (
            isinstance(usage, dict)
            and entry.last_error is None
            and entry.age_s is not None
            and entry.age_s <= STALE_OK_S
        )
        if not usage_is_fresh:
            # A stale last-good row can still prove the window live when its
            # absolute reset is in the future. Otherwise the user's opt-in
            # favors one state-guarded attempt over leaving the window cold.
            stale = entry.last_good
            if isinstance(stale, dict):
                if self._stale_weekly_is_exhausted(stale, now):
                    return "weekly-exhausted"
                if self._stale_five_hour_is_live(stale, now):
                    return "live"
            return None

        weekly = usage.get("seven_day")
        if (
            isinstance(weekly, dict)
            and isinstance(weekly.get("pct"), (int, float))
            and float(weekly["pct"]) >= 100.0
        ) or self._model_weekly_exhausted(usage):
            return "weekly-exhausted"

        five_hour = usage.get("five_hour")
        if not isinstance(five_hour, dict):
            return None
        resets_at = five_hour.get("resets_at")
        if resets_at:
            reset_ts = parse_reset_ts(resets_at)
            if reset_ts is not None:
                return "live" if reset_ts > now else None
        pct = five_hour.get("pct")
        if isinstance(pct, (int, float)) and float(pct) > 0.0:
            # Non-zero utilization itself proves the window was started, even
            # if a partial response omitted its reset timestamp.
            return "live"
        return None

    @staticmethod
    def _stale_five_hour_is_live(usage: dict, now: float) -> bool:
        five_hour = usage.get("five_hour")
        if not isinstance(five_hour, dict):
            return False
        reset_ts = parse_reset_ts(five_hour.get("resets_at"))
        return reset_ts is not None and reset_ts > now

    def _stale_weekly_is_exhausted(self, usage: dict, now: float) -> bool:
        weekly = usage.get("seven_day")
        if (
            isinstance(weekly, dict)
            and isinstance(weekly.get("pct"), (int, float))
            and float(weekly["pct"]) >= 100.0
        ):
            reset_ts = parse_reset_ts(weekly.get("resets_at"))
            if reset_ts is not None and reset_ts > now:
                return True

        scoped = usage.get("scoped")
        if not isinstance(scoped, list):
            return False
        wanted = self.model.casefold()
        family = next(
            (name for name in ("haiku", "sonnet", "opus", "fable") if name in wanted),
            wanted,
        )
        for window in scoped:
            if not isinstance(window, dict):
                continue
            name = window.get("name")
            pct = window.get("pct")
            reset_ts = parse_reset_ts(window.get("resets_at"))
            if (
                isinstance(name, str)
                and family in name.casefold()
                and isinstance(pct, (int, float))
                and float(pct) >= 100.0
                and reset_ts is not None
                and reset_ts > now
            ):
                return True
        return False

    def _model_weekly_exhausted(self, usage: dict) -> bool:
        scoped = usage.get("scoped")
        if not isinstance(scoped, list):
            return False
        wanted = self.model.casefold()
        family = next(
            (
                name
                for name in ("haiku", "sonnet", "opus", "fable")
                if name in wanted
            ),
            wanted,
        )
        for window in scoped:
            if not isinstance(window, dict):
                continue
            name = window.get("name")
            pct = window.get("pct")
            if (
                isinstance(name, str)
                and family in name.casefold()
                and isinstance(pct, (int, float))
                and float(pct) >= 100.0
            ):
                return True
        return False

    def _claude_args(self) -> list[str]:
        return [
            "--print",
            "--model",
            self.model,
            "--effort",
            "low",
            "--safe-mode",
            "--tools",
            "",
            "--no-session-persistence",
            WARMUP_PROMPT,
        ]

    def _load_state(self) -> dict:
        if not self.state_path.exists():
            return {"schemaVersion": STATE_SCHEMA_VERSION, "accounts": {}}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise WarmupError(
                f"Could not read {self.state_path}; refusing to risk duplicate "
                f"warm-up requests: {exc}"
            ) from exc
        if not isinstance(data, dict) or not isinstance(data.get("accounts"), dict):
            raise WarmupError(
                f"Invalid warm-up state in {self.state_path}; refusing to risk "
                "duplicate requests."
            )
        if data.get("schemaVersion") == LEGACY_STATE_SCHEMA_VERSION:
            return self._migrate_legacy_state(data)
        if data.get("schemaVersion") != STATE_SCHEMA_VERSION:
            raise WarmupError(
                f"Invalid warm-up state in {self.state_path}; refusing to risk "
                "duplicate requests."
            )
        return data

    @classmethod
    def _migrate_legacy_state(cls, state: dict) -> dict:
        """Convert slot-keyed v1 rows to stable identity keys in memory."""
        migrated: dict[str, dict] = {}
        for row in state["accounts"].values():
            if not isinstance(row, dict):
                continue
            email = row.get("email")
            org_uuid = row.get("orgUuid", "")
            if not isinstance(email, str) or not isinstance(org_uuid, str):
                continue
            key = cls._identity_key(email, org_uuid)
            existing = migrated.get(key)
            if existing is None:
                migrated[key] = dict(row)
                continue
            for field in ("lastWarmAt", "pendingAt"):
                old = existing.get(field)
                new = row.get(field)
                if isinstance(new, (int, float)) and (
                    not isinstance(old, (int, float)) or new > old
                ):
                    existing[field] = new
        return {"schemaVersion": STATE_SCHEMA_VERSION, "accounts": migrated}

    def _save_state(self, state: dict) -> None:
        try:
            atomic_write_json(self.state_path, state)
        except (OSError, ValueError) as exc:
            raise WarmupError(
                f"Could not persist warm-up state to {self.state_path}: {exc}"
            ) from exc

    @staticmethod
    def _identity_key(email: str, org_uuid: str) -> str:
        # Organization ids are shared by multiple members; the composite is
        # the same exact identity pair SessionManager verifies before launch.
        return f"org:{org_uuid}|email:{email.casefold()}"

    @classmethod
    def _state_row(cls, state: dict, account: AccountSnapshot) -> dict | None:
        row = state["accounts"].get(cls._identity_key(account.email, account.org_uuid))
        if not isinstance(row, dict):
            return None
        if row.get("email") != account.email or row.get("orgUuid", "") != account.org_uuid:
            return None
        return row

    def _state_is_recent(self, state: dict, account: AccountSnapshot, now: float) -> bool:
        row = self._state_row(state, account)
        if row is None:
            return False
        last_warm = row.get("lastWarmAt")
        if isinstance(last_warm, (int, float)) and now < float(last_warm) + FIVE_HOUR_SECONDS:
            return True
        pending = row.get("pendingAt")
        return isinstance(pending, (int, float)) and now < float(pending) + PENDING_GUARD_SECONDS

    @staticmethod
    def _row_for(account: AccountSnapshot) -> dict:
        return {"email": account.email, "orgUuid": account.org_uuid}

    def _mark_pending(self, state: dict, account: AccountSnapshot, now: float) -> None:
        row = self._state_row(state, account) or self._row_for(account)
        row["pendingAt"] = now
        state["accounts"][self._identity_key(account.email, account.org_uuid)] = row

    def _clear_pending(self, state: dict, account: AccountSnapshot) -> None:
        row = self._state_row(state, account)
        if row is not None:
            row.pop("pendingAt", None)

    def _mark_warmed(self, state: dict, account: AccountSnapshot, now: float) -> None:
        row = self._state_row(state, account) or self._row_for(account)
        row.pop("pendingAt", None)
        row["lastWarmAt"] = now
        state["accounts"][self._identity_key(account.email, account.org_uuid)] = row

    @staticmethod
    def _clean_detail(detail: str) -> str:
        lines = [line.strip() for line in detail.splitlines() if line.strip()]
        clean = lines[-1] if lines else "unknown error"
        return clean if len(clean) <= 240 else clean[:237] + "..."

    @staticmethod
    def _reason_detail(reason: str) -> str:
        return {
            "disabled": "disabled accounts are not warmed",
            "not-oauth": "API-key accounts are not warmed",
            "unavailable": "account is not currently switchable",
            "recently-warmed": "a successful or in-progress warm is still protected",
            "weekly-exhausted": "weekly quota is exhausted; no request sent",
            "live": "five-hour window is already active",
        }[reason]

    def _emit(self, account: AccountSnapshot, kind: str, detail: str) -> None:
        self.emit(WarmupEvent(kind, account.number, account.email, detail))
