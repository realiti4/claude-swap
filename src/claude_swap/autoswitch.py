"""Auto-switch engine: poll usage, switch accounts before they hit rate limits.

``AutoSwitchEngine`` is UI-agnostic — no printing, no argparse, no TUI
imports. It composes a :class:`ClaudeAccountSwitcher`, evaluates a threshold
policy each :meth:`~AutoSwitchEngine.tick`, and reports everything through
typed events handed to an ``on_event`` callback; the CLI renders them as
human lines or JSONL, and any future frontend (TUI dashboard, menubar) can
consume the same stream.

Policy in one paragraph: when the active account's *binding window* (the
higher of its 5h/7d utilization) crosses ``settings.threshold``, switch to
the candidate with the most headroom — proactively, so the old account is
still valid while a running Claude Code picks the new one up (this is what
makes the macOS ~30s Keychain cache latency harmless). Candidates must sit
``hysteresis_pct`` below the threshold so two accounts hovering at the line
never ping-pong, and a ``cooldown_seconds`` floor bounds the switch rate
(bypassed only when the active account is hard at its limit). Before
activation the target's token is *freshened* (refreshed if it expires within
10 minutes — twice Claude Code's refresh buffer, so a running Claude Code's
under-lock re-read sees a fresh token and aborts its own refresh); a target
whose refresh token is dead gets quarantined instead of activated. When the
active account's own usage becomes unreadable for ``unhealthy_ticks``
consecutive ticks, the engine fails over to any healthy candidate.

Cooldown and quarantine persist in ``<backup_root>/autoswitch_state.json``
(so cron-driven ``cswap auto --once`` ticks behave across processes), mutated
read-modify-write under a dedicated file lock.
"""

from __future__ import annotations

import enum
import json
import logging
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import ClassVar

from claude_swap import oauth, poll_policy
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.json_output import SCHEMA_VERSION, USAGE_TOKEN_EXPIRED
from claude_swap.locking import FileLock
from claude_swap.poll_policy import (
    ESCALATION_MARGIN_PCT,
    RESET_SLACK_S,
    binding_pct,
)
from claude_swap.settings import AutoSwitchSettings, atomic_write_json, parse_model_names
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.usage_store import UsageEntry, due_candidate, seven_day_pct

STATE_FILENAME = "autoswitch_state.json"
STATE_SCHEMA_VERSION = 1

_logger = logging.getLogger("claude-swap")

# Freshen targets whose access token expires within this window: twice Claude
# Code's own 5-minute refresh buffer, so its post-lock "abort refresh if not
# expired" re-read holds with margin after our swap.
FRESHEN_BUFFER_MS = 10 * 60 * 1000

# Sleep caps around a known quota reset (RESET_SLACK_S lives in poll_policy
# with the rest of the cadence numbers): never trust one long sleep (laptops
# suspend, clocks drift) — cap and re-evaluate.
MAX_SLEEP_S = 6 * 3600.0
NO_RESET_FALLBACK_S = 300.0

# Idle-hold cap (elapsed, not ticks — the hold itself slows the cadence to
# NO_RESET_FALLBACK_S): an owned-and-expired token normally means Claude Code
# is idle and will self-heal on next use, but a *dead* refresh token with an
# active user would look identical forever, so after this long the engine
# falls back to normal unhealthy counting.
IDLE_HOLD_MAX_S = 30 * 60.0

# consume-first burn-rate projection. Plain soonest-first parks on the
# account whose weekly window resets soonest, but weekly quota can only be
# drained *through* the 5-hour window, so an account can reset soonest yet
# hold more perishable quota than is reachable before it resets. When the
# active account's burn rate has been measured, a proactive move onto a
# sooner-resetting account is gated on how much of its quota is actually
# reachable: ``reachable = min(remaining_pct, burn_rate * hours_to_reset)``.
# Below MIN_REACHABLE_PCT the move spends a cooldown to realize almost
# nothing (the soonest window is too imminent to exploit / structurally
# stranded), so the engine holds and lets the next-soonest, drainable
# account be considered instead. With no measured rate the gate is inert and
# the strategy degrades to plain soonest-first — the merged behavior.
MIN_REACHABLE_PCT = 5.0
# The two samples behind a burn rate must straddle a sane interval: shorter
# is sampling noise; longer predates the current active stint (only the
# active account burns quota) and would understate the rate. Outside the
# band the rate is treated as unmeasured (gate inert).
BURN_RATE_MIN_DT_S = 30.0
BURN_RATE_MAX_DT_S = 3 * 60 * 60.0

# Adaptive scheduling: the baseline request volume is O(1) per tick — the
# active account plus ONE due candidate (stalest data first) — instead of
# every account in parallel, and the per-account cadence itself (movement,
# threshold distance, urgent mode, 429 recovery) lives in poll_policy, is
# persisted in the usage store by whichever collector fetched, and is shared
# by every surface. The engine escalates to a full candidate refresh only
# when a switch could actually be near: active utilization within
# ESCALATION_MARGIN_PCT of the threshold, or active usage unknown (failover
# needs fresh candidate data). The consume-first trigger can fire outside
# that escalation band; there it decides provisionally on the stored
# snapshot and escalates at commit time, when a switch would actually fire
# (the two-phase commit in _tick_inner).


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def pct_label(value: float) -> str:
    """A percentage for display, as configured: 85.555555 stays itself
    (never a rounded "85.5556") and 99.9 never becomes a lying "100" the
    way ``.0f`` renders it. Ten significant digits still absorb IEEE float
    noise (~15th digit) in computed utilizations (100.0 - headroom).
    Displayed comparisons must format BOTH sides with this helper — mixing
    formatters can render an impossible "85.5556% < 85.555555%"."""
    return f"{value:.10g}"


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AutoSwitchEvent:
    """Base event. ``to_json()`` payloads are additive: consumers must ignore
    unknown ``event`` kinds and unknown fields."""

    kind: ClassVar[str] = "event"
    ts: str = field(default_factory=_now_iso, kw_only=True)

    def _fields(self) -> dict:
        return {}

    def to_json(self) -> dict:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "event": self.kind,
            "ts": self.ts,
            **self._fields(),
        }

    def human(self) -> str:  # pragma: no cover - overridden
        return self.kind


@dataclass(frozen=True)
class PollEvent(AutoSwitchEvent):
    kind: ClassVar[str] = "poll"
    active: dict | None  # account_ref shape, or None
    headroom: dict[str, float | None]  # account number → headroom pct (None=unknown)
    threshold: float
    # account number → last fetch-error cause ("http-429", "timeout", ...) for
    # accounts whose usage is unknown this tick. Additive field.
    fetch_errors: dict[str, str] = field(default_factory=dict)
    # account number → ordered window label → utilization pct ("5h", "7d",
    # then scoped model display names). Additive field: the binding pct alone
    # (e.g. "89%") hides which window binds — #115 was reported off that
    # ambiguity.
    windows: dict[str, dict[str, float]] = field(default_factory=dict)

    def _fields(self) -> dict:
        fields = {
            "active": self.active,
            "headroomPct": self.headroom,
            "threshold": self.threshold,
        }
        if self.fetch_errors:
            fields["fetchErrors"] = self.fetch_errors
        if self.windows:
            fields["windowsPct"] = self.windows
        return fields

    def _describe(self, num: str) -> str:
        wins = self.windows.get(num)
        if wins:
            return " · ".join(f"{name} {pct:.0f}%" for name, pct in wins.items())
        h = self.headroom.get(num)
        if h is not None:
            return f"{100 - h:.0f}%"
        err = self.fetch_errors.get(num)
        return f"? ({err})" if err else "?"

    def human(self) -> str:
        if self.active is None:
            return "poll: no active account"
        num = self.active.get("number")
        h = self.headroom.get(str(num))
        if h is not None:
            used = f"{100 - h:.0f}% used"
        else:
            err = self.fetch_errors.get(str(num))
            used = f"usage unknown ({err})" if err else "usage unknown"
        others = ", ".join(
            f"#{n}: {self._describe(n)}"
            for n in self.headroom
            if n != str(num)
        )
        tail = f" | others: {others}" if others else ""
        return (
            f"Account-{num} ({self.active.get('email')}): {used} "
            f"(switch at {pct_label(self.threshold)}%){tail}"
        )


@dataclass(frozen=True)
class SwitchEvent(AutoSwitchEvent):
    kind: ClassVar[str] = "switch"
    trigger: str  # "proactive" | "at-limit" | "failover" | "consume-first"
    from_ref: dict | None
    to_ref: dict | None
    warnings: list[str] = field(default_factory=list)
    dry_run: bool = False

    def _fields(self) -> dict:
        return {
            "trigger": self.trigger,
            "from": self.from_ref,
            "to": self.to_ref,
            "warnings": self.warnings,
            "dryRun": self.dry_run,
        }

    def human(self) -> str:
        src = (
            f"Account-{self.from_ref.get('number')}" if self.from_ref else "(none)"
        )
        dst = (
            f"Account-{self.to_ref.get('number')} ({self.to_ref.get('email')})"
            if self.to_ref
            else "?"
        )
        prefix = "[dry-run] would switch" if self.dry_run else "Switched"
        return f"{prefix} {src} -> {dst} ({self.trigger})"


@dataclass(frozen=True)
class NoSwitchEvent(AutoSwitchEvent):
    kind: ClassVar[str] = "no-switch"
    reason: str
    detail: str = ""

    def _fields(self) -> dict:
        return {"reason": self.reason, "detail": self.detail}

    def human(self) -> str:
        return f"no switch: {self.reason}" + (f" ({self.detail})" if self.detail else "")


@dataclass(frozen=True)
class QuarantineEvent(AutoSwitchEvent):
    kind: ClassVar[str] = "account-quarantined"
    number: str
    email: str
    reason: str

    def _fields(self) -> dict:
        return {"number": self.number, "email": self.email, "reason": self.reason}

    def human(self) -> str:
        return (
            f"Account-{self.number} ({self.email}) quarantined: {self.reason}. "
            f"Log in with it and run 'cswap --add-account --slot {self.number}' "
            "to recover."
        )


@dataclass(frozen=True)
class UnquarantineEvent(AutoSwitchEvent):
    kind: ClassVar[str] = "account-unquarantined"
    number: str
    email: str
    reason: str = "credentials-replaced"

    def _fields(self) -> dict:
        return {"number": self.number, "email": self.email, "reason": self.reason}

    def human(self) -> str:
        return f"Account-{self.number} ({self.email}) back in rotation ({self.reason})"


@dataclass(frozen=True)
class AllExhaustedEvent(AutoSwitchEvent):
    kind: ClassVar[str] = "all-exhausted"
    earliest_reset_at: str | None

    def _fields(self) -> dict:
        return {"earliestResetAt": self.earliest_reset_at}

    def human(self) -> str:
        if self.earliest_reset_at:
            return f"all accounts exhausted; earliest reset {self.earliest_reset_at}"
        return "all accounts exhausted; no reset time known"


@dataclass(frozen=True)
class SleepEvent(AutoSwitchEvent):
    kind: ClassVar[str] = "sleep"
    seconds: float
    until: str

    def _fields(self) -> dict:
        return {"seconds": round(self.seconds, 1), "until": self.until}

    def human(self) -> str:
        return f"sleeping {self.seconds / 60:.0f}m (until {self.until})"


@dataclass(frozen=True)
class ErrorEvent(AutoSwitchEvent):
    kind: ClassVar[str] = "error"
    message: str
    transient: bool = True

    def _fields(self) -> dict:
        return {"message": self.message, "transient": self.transient}

    def human(self) -> str:
        return f"error: {self.message}" + (" (will retry)" if self.transient else "")


@dataclass(frozen=True)
class ConfigWarningEvent(AutoSwitchEvent):
    """A configuration value is syntactically fine but provably inert (e.g.
    an ``autoswitch.model`` name no account reports). Not an error: the
    engine keeps running on the axes that do exist."""

    kind: ClassVar[str] = "config-warning"
    message: str

    def _fields(self) -> dict:
        return {"message": self.message}

    def human(self) -> str:
        return f"warning: {self.message}"


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class TickOutcome(enum.Enum):
    """Outcome of one evaluation tick; values double as --once exit codes."""

    SWITCHED = 0
    ERROR = 1
    NO_ACTION = 2
    BLOCKED = 3  # wanted to switch but no viable target / all exhausted


# Quarantine state persisted fingerprints from a local refresh-token-only
# helper; oauth.credential_fingerprint is identical for refresh-token creds.
# Setup-token quarantines stored None where the shared helper now yields a
# full-content hash — those release once on first recheck and re-quarantine on
# the next dead freshen (one harmless extra cycle, migration only).
_refresh_fingerprint = oauth.credential_fingerprint


def _window_pcts(
    usage: dict | None, models: tuple[str, ...] = ()
) -> dict[str, float]:
    """Ordered window label → pct: "5h", "7d", then configured scoped names.

    Deliberately restricted to the windows the *decision* reads (same
    ``models`` filter): showing an unconfigured scoped window at 100% next
    to a switch onto that account would look like a bug, when the engine
    correctly ignored it. Full per-model usage lives in ``cswap list``.
    """
    return {
        name: pct for name, pct, _ in oauth.relevant_windows(usage, models)
    }


# Reset math moved to poll_policy with the cadence numbers; aliased for the
# engine's sleep scheduling and the test suite.
_limiting_reset_ts = poll_policy.limiting_reset_ts
_earliest_future_reset_ts = poll_policy.earliest_future_reset_ts
_parse_reset_ts = poll_policy.parse_reset_ts


def _seven_day_reset_ts(usage: dict | str | None, now: float) -> float | None:
    """Epoch of an account's 7-day (weekly) window reset, or None if unknown
    or already past.

    The consume-first strategy ranks by this — the weekly window is the
    perishable quota (the 5-hour one recycles too fast to be worth planning
    around). A stale snapshot can carry a ``resets_at`` that has since
    elapsed; treated as a real instant it would sort the *just-rolled-over*
    account (the least perishable quota of all) as "soonest", so past ==
    unknown. Plain ``ts <= now``: RESET_SLACK_S is poll-scheduling lag
    tolerance, not ranking input — padding here would turn a genuinely
    imminent reset into a false reset-unknown hold.
    """
    if isinstance(usage, dict):
        window = usage.get("seven_day")
        if isinstance(window, dict):
            ts = _parse_reset_ts(window.get("resets_at"))
            if ts is not None and ts > now:
                return ts
    return None


def _ref(number: str, email: str) -> dict:
    return {"number": int(number), "email": email}


def _headroom_by_account(
    usage: dict[str, dict | str | None], models: tuple[str, ...]
) -> dict[str, float | None]:
    """Per-account headroom derived from decision values."""
    return {
        num: oauth.account_headroom(
            value if isinstance(value, dict) else None, models
        )
        for num, value in usage.items()
    }


class AutoSwitchEngine:
    """Threshold-policy auto-switcher over a :class:`ClaudeAccountSwitcher`.

    ``on_event`` receives every :class:`AutoSwitchEvent`; exceptions it raises
    are not caught (a broken frontend should fail loudly in tests). ``clock``
    is wall time (persisted cooldown timestamps must survive processes).
    """

    def __init__(
        self,
        switcher: ClaudeAccountSwitcher,
        settings: AutoSwitchSettings,
        on_event: Callable[[AutoSwitchEvent], None],
        *,
        dry_run: bool = False,
        state_path: Path | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.switcher = switcher
        self.settings = settings
        # Model(s) whose per-model weekly limit also binds the switch decision
        # (empty = account-wide 5h/7d only). ``settings.model`` is a comma-
        # separated list ("Fable", "Opus,Sonnet", "all"); parse once here and
        # pass everywhere usage windows are read — decisions, cadence, and
        # reset scheduling must all see the same axes.
        self._models = parse_model_names(settings.model)
        # Poll plans written by the collector must key on the same threshold/
        # models the engine decides with (CLI overrides included), not on
        # whatever the settings file happens to say.
        switcher.set_poll_policy_inputs(settings.threshold, self._models)
        self.on_event = on_event
        self.dry_run = dry_run
        self.state_path = state_path or (switcher.backup_dir / STATE_FILENAME)
        self.clock = clock
        self._stop = threading.Event()
        # Cuts the current inter-tick sleep short (a session threshold change
        # from the TUI should show a fresh decision now, not next interval).
        self._wake = threading.Event()
        self._unhealthy_ticks = 0
        # Both set per tick: a known-reset sleep target, and whether a BLOCKED
        # outcome is static enough (truly exhausted / no candidates) to wait
        # longer than the normal interval.
        self._sleep_until_ts: float | None = None
        self._blocked_wait_long = False
        # Idle-hold: when the active token expired while Claude Code owns it
        # (and is therefore idle), crawl instead of counting unhealthy ticks.
        # ``_idle_hold_since`` survives across ticks (elapsed-time cap);
        # ``_idle_hold_slow`` is per-tick like ``_blocked_wait_long``.
        self._idle_hold_since: float | None = None
        self._idle_hold_slow = False
        # One-shot typo guard for ``autoswitch.model``: resolved (and possibly
        # warned) on the first tick where every relevant account has readable
        # usage — adaptive polling legitimately leaves gaps before that.
        self._model_check_done = not self._models

    # -- state file ---------------------------------------------------------

    def _state_lock(self) -> FileLock:
        return FileLock(self.state_path.parent / ".autoswitch_state.lock")

    def _read_state(self) -> dict:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _mutate_state(self, mutator: Callable[[dict], None]) -> dict:
        """Read-modify-write the state file under its lock; returns new state.

        The lock prevents two concurrent engines (loop + cron ``--once``) from
        overwriting each other's quarantine/cooldown updates. Never called
        while any other lock is held.
        """
        with self._state_lock():
            state = self._read_state()
            state["schemaVersion"] = STATE_SCHEMA_VERSION
            mutator(state)
            atomic_write_json(self.state_path, state)
            return state

    # -- quarantine -----------------------------------------------------------

    def _quarantine(self, number: str, email: str, reason: str) -> None:
        creds = self.switcher.read_account_credentials(number, email)
        fingerprint = _refresh_fingerprint(creds) if creds else None

        def add(state: dict) -> None:
            state.setdefault("quarantine", {})[number] = {
                "email": email,
                "reason": reason,
                "at": _now_iso(),
                "refreshTokenFingerprint": fingerprint,
            }

        self._mutate_state(add)
        self._emit(QuarantineEvent(number=number, email=email, reason=reason))

    def _release_recovered_quarantines(self, state: dict) -> dict:
        """Drop quarantine entries whose credential was replaced since.

        A changed refresh-token fingerprint (or a removed/re-added slot) means
        the user re-logged in and re-captured the account — the dead lineage
        is gone, so it re-enters rotation.
        """
        quarantine = state.get("quarantine")
        if not isinstance(quarantine, dict) or not quarantine:
            return state
        to_release: list[tuple[str, str, str]] = []
        for number, entry in quarantine.items():
            email_now = self.switcher.account_email(number)
            if not email_now or email_now != entry.get("email"):
                to_release.append(
                    (number, entry.get("email", ""), "account-replaced")
                )
                continue
            creds = self.switcher.read_account_credentials(number, email_now)
            fingerprint = _refresh_fingerprint(creds) if creds else None
            if fingerprint != entry.get("refreshTokenFingerprint"):
                to_release.append((number, email_now, "credentials-replaced"))
        if not to_release:
            return state

        def drop(s: dict) -> None:
            q = s.get("quarantine")
            if isinstance(q, dict):
                for number, _, _ in to_release:
                    q.pop(number, None)

        state = self._mutate_state(drop)
        for number, email, reason in to_release:
            self._emit(UnquarantineEvent(number=number, email=email, reason=reason))
        return state

    # -- freshening -----------------------------------------------------------

    def _freshen_target(self, number: str, email: str) -> str:
        """Ensure a candidate's stored token outlives Claude Code's 5-min
        refresh buffer before it gets activated.

        Returns ``"ok"``, ``"invalid_grant"`` (dead lineage — quarantine),
        ``"identity-conflict"`` (alive but authenticates as a different
        account — quarantine, do not activate), ``"transient"`` (network
        trouble — try again next tick) or ``"skip-live-session"``. Only ever
        touches the slot's *backup* store; the active credential belongs to
        Claude Code.
        """
        if self.switcher.account_kind_for(number) == "api_key":
            return "ok"  # API keys don't expire/refresh
        if self.switcher.live_session_pids_for(number, email):
            # A live `cswap run` session owns this account's token in its own
            # profile. Auto-activating it as the default login too would put
            # one rotating refresh token in two config dirs (the stale-copy
            # failure class) with nobody reading the warning — and its quota
            # is already being consumed by that session anyway. Manual
            # switch_to keeps its warn-and-proceed behavior; auto skips.
            return "skip-live-session"
        creds = self.switcher.read_account_credentials(number, email)
        if not creds:
            return "transient"
        data = oauth.extract_oauth_data(creds)
        if not data:
            return "invalid_grant"
        expires_at = data.get("expiresAt")
        now_ms = self.clock() * 1000
        near_expiry = (
            isinstance(expires_at, (int, float))
            and now_ms + FRESHEN_BUFFER_MS >= expires_at
        )
        if not near_expiry:
            return "ok"
        outcome = oauth.try_refresh_oauth_credentials(creds)
        if outcome.error is None and outcome.credentials:
            # Persist first, unconditionally: the grant consumed a generation,
            # and not writing the successor would kill the lineage regardless
            # of whose it turns out to be.
            self.switcher.persist_backup_credentials(
                number, email, outcome.credentials
            )
            if self._note_token_identity(number, outcome.token_account):
                # The slot's stored credential authenticates as a *different*
                # account — activating it would put the user on the wrong
                # account with every gauge reading normal. Not a viable
                # target; the caller quarantines it (released automatically
                # once the credential is replaced by a re-add).
                return "identity-conflict"
            return "ok"
        if outcome.error in ("invalid_grant", "no_refresh_token"):
            return "invalid_grant"
        return "transient"

    def _note_token_identity(
        self, number: str, token_account: dict | None
    ) -> bool:
        """Use the token endpoint's free identity to verify/backfill a slot.

        The refresh grant just ran against the slot's own stored credential,
        so ``token_account`` (when the server includes it) names who that
        credential really is. Returns True on a *conflict*: the credential
        authenticates under a different organization than the slot records
        (org compared first, whenever both sides record one), or as a
        different account uuid. An empty slot uuid (blank-uuid records from
        older versions, add-token placeholders) is backfilled — but only
        when no org conflict exists: a wrong-org credential is evidence the
        slot holds the wrong account, and backfilling *its* uuid would
        poison the slot's identity record (backfill never rewrites a
        non-empty uuid, so that corruption would be sticky).

        ``_parse_token_account`` already enforces a strict boundary, but this
        identity is opportunistic — re-check types here so malformed data can
        never break the freshen that carried it (the successor credential is
        already persisted by the time this runs).
        """
        if not isinstance(token_account, dict):
            return False
        ta_uuid = token_account.get("uuid")
        if not isinstance(ta_uuid, str) or not ta_uuid.strip():
            return False
        ta_uuid = ta_uuid.strip()
        slot_identity = self.switcher.account_identity(number)
        ta_org = token_account.get("organizationUuid")
        slot_org = slot_identity.get("organizationUuid") or ""
        if isinstance(ta_org, str) and ta_org and slot_org and ta_org != slot_org:
            return True
        if not slot_identity.get("uuid"):
            try:
                self.switcher.backfill_account_uuid(number, ta_uuid)
            except Exception as e:  # never let bookkeeping break a freshen
                _logger.debug("uuid backfill failed for account %s: %r", number, e)
            return False
        return slot_identity["uuid"] != ta_uuid

    # -- tick -----------------------------------------------------------------

    def tick(self) -> TickOutcome:
        """Evaluate once: poll usage, maybe switch. Never raises."""
        try:
            return self._tick_inner()
        except ClaudeSwitchError as e:
            self._emit(ErrorEvent(message=str(e), transient=True))
            return TickOutcome.ERROR
        except Exception as e:  # pragma: no cover - safety net
            self._emit(
                ErrorEvent(message=f"{type(e).__name__}: {e}", transient=True)
            )
            return TickOutcome.ERROR

    def _tick_inner(self) -> TickOutcome:
        self._sleep_until_ts = None
        self._blocked_wait_long = False
        self._idle_hold_slow = False
        settings = self.settings
        state = self._read_state()
        if not self.dry_run:
            # Dry-run must not write anything, so recovered quarantines are
            # only released (state mutation) on real ticks.
            state = self._release_recovered_quarantines(state)
        quarantined = set(
            state.get("quarantine", {})
            if isinstance(state.get("quarantine"), dict)
            else {}
        )

        current = self.switcher.current_account_number()
        if current is None:
            self._emit(
                PollEvent(active=None, headroom={}, threshold=settings.threshold)
            )
            if self.switcher.has_live_login():
                # Live login exists but cswap doesn't manage it: never act —
                # a switch would overwrite it without a backup.
                self._emit(
                    NoSwitchEvent(
                        reason="unmanaged-active-account",
                        detail="run 'cswap --add-account' to include it in rotation",
                    )
                )
            else:
                self._emit(
                    NoSwitchEvent(
                        reason="no-active-account",
                        detail="log in and run 'cswap --add-account' first",
                    )
                )
            return TickOutcome.NO_ACTION

        current_email = self.switcher.account_email(current)
        active_ref = _ref(current, current_email) if current_email else {
            "number": int(current),
            "email": "",
        }

        entries, usage, headroom = self._collect_scheduled_usage(
            current, quarantined, threshold=settings.threshold
        )
        self._emit(
            PollEvent(
                active=active_ref,
                headroom=headroom,
                threshold=settings.threshold,
                fetch_errors={
                    num: entry.last_error
                    for num, entry in entries.items()
                    if usage.get(num) is None and entry.last_error
                },
                windows={
                    num: pcts
                    for num, value in usage.items()
                    if (pcts := _window_pcts(
                        value if isinstance(value, dict) else None, self._models
                    ))
                },
            )
        )

        if not self._model_check_done:
            self._check_model_names(quarantined, usage)

        if (
            self.switcher.account_kind_for(current) == "api_key"
            and not settings.include_api_key_accounts
        ):
            self._emit(
                NoSwitchEvent(
                    reason="active-api-key",
                    detail="API-key accounts have no quota to watch",
                )
            )
            return TickOutcome.NO_ACTION

        active_headroom = headroom.get(current)
        if active_headroom is not None:
            self._unhealthy_ticks = 0
            self._idle_hold_since = None
            utilization = 100.0 - active_headroom
            if utilization < settings.threshold:
                if settings.strategy != "consume-first":
                    self._emit(
                        NoSwitchEvent(
                            reason="below-threshold",
                            # Both sides through pct_label: .0f utilization could
                            # display an impossible "100% < 99.9%".
                            detail=(
                                f"{pct_label(utilization)}% < "
                                f"{pct_label(settings.threshold)}%"
                            ),
                        )
                    )
                    return TickOutcome.NO_ACTION
                # consume-first: below the threshold we still proactively move to
                # whichever account's weekly window resets soonest, to burn the
                # most-perishable quota first. Candidate selection decides whether
                # a sooner-resetting account with room actually exists.
                trigger = "consume-first"
            else:
                trigger = "at-limit" if active_headroom <= 0 else "proactive"
        else:
            if usage.get(current) == USAGE_TOKEN_EXPIRED:
                # Expired while an owner (Claude Code / live session) holds the
                # credential: CC refreshes on every API request, so expired +
                # owner present proves Claude has been idle since expiry — no
                # quota burn, nothing to switch for. Self-heals on next use;
                # crawl slowly instead of burning failover ticks (Finding 2 of
                # the usage-lapse investigation).
                now = self.clock()
                if self._idle_hold_since is None:
                    self._idle_hold_since = now
                if now - self._idle_hold_since <= IDLE_HOLD_MAX_S:
                    self._unhealthy_ticks = 0
                    self._idle_hold_slow = True
                    self._emit(
                        NoSwitchEvent(
                            reason="active-idle",
                            detail=(
                                "token expired while Claude Code is idle; "
                                "resumes on next use"
                            ),
                        )
                    )
                    return TickOutcome.NO_ACTION
                # Held far longer than any idle nap should need — likely a
                # dead refresh token with an *active* user. Fall through to
                # normal unhealthy counting so failover can still happen.
                _logger.warning(
                    "Active token expired and owned for over %.0f minutes; "
                    "resuming unhealthy counting (dead refresh token?)",
                    IDLE_HOLD_MAX_S / 60,
                )
            else:
                self._idle_hold_since = None
            self._unhealthy_ticks += 1
            if self._unhealthy_ticks < settings.unhealthy_ticks:
                self._emit(
                    NoSwitchEvent(
                        reason="active-usage-unknown",
                        detail=(
                            f"{self._unhealthy_ticks}/{settings.unhealthy_ticks} "
                            "before failover"
                        ),
                    )
                )
                return TickOutcome.NO_ACTION
            trigger = "failover"

        if trigger in ("proactive", "consume-first") and self._in_cooldown(state):
            self._emit(NoSwitchEvent(reason="cooldown"))
            return TickOutcome.NO_ACTION

        # -- candidate selection ------------------------------------------
        candidates = [
            num
            for num in self.switcher.switchable_account_numbers()
            if num != current and num not in quarantined
        ]
        oauth_candidates = [
            n for n in candidates if self.switcher.account_kind_for(n) != "api_key"
        ]
        api_key_candidates = (
            [n for n in candidates if self.switcher.account_kind_for(n) == "api_key"]
            if settings.include_api_key_accounts
            else []
        )
        if (
            trigger == "consume-first"
            and not oauth_candidates
            and active_headroom is not None
        ):
            # Healthy below-threshold account with no OAuth peer to compare
            # against — the same state `best` reports as below-threshold
            # NO_ACTION before ever reaching candidate selection. API-key
            # candidates don't change the outcome: they have no weekly window
            # to consume, so a consume-first nudge never targets them. Keep
            # the exit-code contract identical across strategies: cron
            # wrappers keying on BLOCKED must not see false "blocked" from
            # the flag alone.
            self._emit(
                NoSwitchEvent(
                    reason="below-threshold",
                    detail=(
                        f"{pct_label(100.0 - active_headroom)}% < "
                        f"{pct_label(settings.threshold)}%"
                    ),
                )
            )
            return TickOutcome.NO_ACTION
        if not oauth_candidates and not api_key_candidates:
            # Won't change until the user adds/recovers an account — no point
            # re-polling at full cadence.
            self._blocked_wait_long = True
            self._emit(NoSwitchEvent(reason="no-candidates"))
            return TickOutcome.BLOCKED

        consume_first = settings.strategy == "consume-first"
        burn_rate = self._active_burn_rate(entries.get(current))
        ordered, any_known, active_reset_ts, stranded = self._rank_candidates(
            trigger=trigger,
            consume_first=consume_first,
            oauth_candidates=oauth_candidates,
            usage=usage,
            headroom=headroom,
            current=current,
            active_headroom=active_headroom,
            settings=settings,
            now=self.clock(),
            burn_rate=burn_rate,
        )

        if trigger == "consume-first" and ordered:
            # Two-phase commit: the provisional pick may have ridden a
            # snapshot up to CANDIDATE_MAX_INTERVAL_S stale — consume-first
            # decides below the threshold, where the collector only escalates
            # inside the ESCALATION_MARGIN_PCT band (flat-traffic invariant).
            # A switch is imminent, so spend the fetches now and re-decide on
            # fresh data.
            # reserve() serves just-fetched accounts from the store, so this
            # is cheap in-tick and plan-bounded across ticks. The trigger is
            # deliberately NOT re-classified if the fresh active crossed the
            # threshold: a still-qualifying sooner target switches anyway,
            # and otherwise the next tick escalates normally and escapes.
            entries = self.switcher.usage_entries_by_account(
                fetch={current, *candidates}
            )
            usage = {num: entry.decision_value() for num, entry in entries.items()}
            headroom = _headroom_by_account(usage, self._models)
            active_headroom = headroom.get(current)
            burn_rate = self._active_burn_rate(entries.get(current))
            ordered, any_known, active_reset_ts, stranded = self._rank_candidates(
                trigger=trigger,
                consume_first=consume_first,
                oauth_candidates=oauth_candidates,
                usage=usage,
                headroom=headroom,
                current=current,
                active_headroom=active_headroom,
                settings=settings,
                now=self.clock(),
                burn_rate=burn_rate,
            )

        if not ordered and api_key_candidates and trigger != "consume-first":
            # Last resort when we must move: metered API-key accounts
            # (unmeasurable headroom). Never for a below-threshold consume-first
            # nudge — those API-key accounts have no weekly window to consume.
            ordered = api_key_candidates

        if not ordered:
            if not any_known:
                # No candidate readable this tick — true for every strategy,
                # and must not be dressed up as a consume-first hold.
                self._emit(
                    NoSwitchEvent(
                        reason="no-comparison",
                        detail="no candidate has readable usage",
                    )
                )
                return TickOutcome.BLOCKED
            if trigger == "consume-first":
                # Below the threshold and healthy: staying put is a correct
                # outcome, never a block. Distinguish *why* nothing qualified
                # so an opted-in user can see the strategy working (or inert).
                if active_reset_ts is None:
                    # The strictly-sooner filter skips every candidate when the
                    # active account's weekly reset is unknown — without this
                    # reason the strategy would look enabled while doing
                    # nothing, with no way to tell.
                    self._emit(
                        NoSwitchEvent(
                            reason="reset-unknown",
                            detail=(
                                "active account's weekly reset time is "
                                "unknown; consume-first is idle until it "
                                "is reported"
                            ),
                        )
                    )
                    return TickOutcome.NO_ACTION
                if stranded:
                    # A sooner-resetting account exists, but at the active
                    # account's measured burn rate too little of its perishable
                    # quota is reachable before it resets to be worth a switch —
                    # keep consuming here rather than chase a window we can't
                    # meaningfully drain.
                    self._emit(
                        NoSwitchEvent(
                            reason="reset-too-imminent",
                            detail=(
                                "the sooner-resetting account can't be "
                                "meaningfully drained before it resets at the "
                                "current burn rate; holding"
                            ),
                        )
                    )
                    return TickOutcome.NO_ACTION
                # Covers both "everyone resets later" and "sooner ones have no
                # room" — don't claim the active account resets first when the
                # real story may be exhausted candidates.
                self._emit(
                    NoSwitchEvent(
                        reason="already-consuming-soonest",
                        detail="no sooner-resetting account with room to spare",
                    )
                )
                return TickOutcome.NO_ACTION
            # "All exhausted" (and its hours-long reset sleep) only when it's
            # literally true: every candidate's usage is known and at its
            # limit. A candidate that merely failed the proactive hysteresis
            # gate, or one whose usage is unreadable this tick, can become
            # viable at any moment — and the active account can hit 100% and
            # need the at-limit escape — so those keep the normal cadence.
            candidate_headrooms = [headroom.get(n) for n in oauth_candidates]
            truly_exhausted = all(
                h is not None and h <= 0 for h in candidate_headrooms
            )
            if not truly_exhausted:
                self._emit(
                    NoSwitchEvent(
                        reason="no-qualifying-candidate",
                        detail=(
                            "no candidate is below the threshold and better "
                            "than the active account by the hysteresis "
                            "margin, or usage is unreadable this tick"
                        ),
                    )
                )
                return TickOutcome.BLOCKED
            self._blocked_wait_long = True
            earliest = self._earliest_recovery(usage)
            if earliest is not None:
                self._sleep_until_ts = earliest.timestamp() + RESET_SLACK_S
            self._emit(
                AllExhaustedEvent(
                    earliest_reset_at=(
                        earliest.isoformat().replace("+00:00", "Z")
                        if earliest
                        else None
                    )
                )
            )
            return TickOutcome.BLOCKED

        # -- freshen + switch ----------------------------------------------
        transient_failure = False
        for num in ordered:
            email = self.switcher.account_email(num)
            if trigger == "consume-first":
                # The phase-2 refetch is best-effort: the collector refuses
                # accounts in failure backoff or claimed by a concurrent
                # poller, which then serve their stored entries. Consume-first
                # is opportunistic, not an escape — never act on stale data
                # or slide to a worse-ranked target; hold and retry next tick.
                entry = entries.get(num)
                if entry is None or not entry.fresh(self.clock()):
                    self._emit(
                        NoSwitchEvent(
                            reason="stale-usage",
                            detail=(
                                f"account {num} usage could not be refreshed "
                                "this tick (backoff or a concurrent poller); "
                                "retrying"
                            ),
                        )
                    )
                    return TickOutcome.NO_ACTION
            if self.dry_run:
                # Dry-run stops at the decision: no token refresh, no
                # quarantine writes — freshening is a mutation.
                return self._perform(num, email, trigger)
            status = self._freshen_target(num, email)
            if status == "identity-conflict":
                # The slot's credential is alive but belongs to a different
                # account — switching onto it would silently run the wrong
                # account. Quarantine (auto-released once a re-add replaces
                # the credential).
                self._quarantine(num, email, "identity-conflict")
                continue
            if status == "invalid_grant":
                self._quarantine(num, email, "invalid_grant")
                continue
            if status == "transient":
                transient_failure = True
                continue
            if status == "skip-live-session":
                continue
            return self._perform(num, email, trigger)

        if transient_failure:
            self._emit(
                ErrorEvent(
                    message="could not freshen any candidate (network?)",
                    transient=True,
                )
            )
            return TickOutcome.ERROR
        self._emit(NoSwitchEvent(reason="no-viable-target"))
        return TickOutcome.BLOCKED

    def _active_burn_rate(self, entry: UsageEntry | None) -> float | None:
        """The active account's weekly burn rate in pct-per-hour, or None.

        Only the active account burns quota, so its own two most recent
        successful samples measure the workload throughput consume-first uses
        to project how much of a candidate's perishable quota is reachable
        before reset. Returns None — leaving the burn-rate gate inert, so the
        strategy stays plain soonest-first — unless the samples straddle a sane
        interval (see ``BURN_RATE_MIN_DT_S``/``BURN_RATE_MAX_DT_S``) and show
        real forward burn (a non-positive delta is idle time or a window that
        just reset, neither a usable rate).
        """
        if entry is None:
            return None
        cur_pct = seven_day_pct(entry.last_good)
        if cur_pct is None or entry.fetched_at is None:
            return None
        if entry.prev_seven_day_pct is None or entry.prev_fetched_at is None:
            return None
        dt = entry.fetched_at - entry.prev_fetched_at
        if not (BURN_RATE_MIN_DT_S <= dt <= BURN_RATE_MAX_DT_S):
            return None
        delta = cur_pct - entry.prev_seven_day_pct
        if delta <= 0:
            return None
        return delta / (dt / 3600.0)

    def _rank_candidates(
        self,
        *,
        trigger: str,
        consume_first: bool,
        oauth_candidates: list[str],
        usage: dict[str, dict | str | None],
        headroom: dict[str, float | None],
        current: str,
        active_headroom: float | None,
        settings: AutoSwitchSettings,
        now: float,
        burn_rate: float | None = None,
    ) -> tuple[list[str], bool, float | None, bool]:
        """Filter and rank OAuth candidates for this tick's trigger.

        Returns ``(ordered, any_known, active_reset_ts, stranded)``. Pure — no
        emits, no state writes — so the consume-first two-phase commit can run
        it twice per tick: on the stored snapshot to decide provisionally, then
        on the escalated refetch to re-verify before switching. Running it on
        the fresh phase-2 data also re-derives ``reachable`` from the just-
        fetched utilizations, so the burn-rate gate re-checks for free.

        ``stranded`` is True when the burn-rate gate held back every otherwise-
        qualifying sooner-resetting candidate (see ``burn_rate``): the caller
        distinguishes that hold from "already resets soonest".
        """
        # consume-first ranks by soonest weekly reset; a proactive (below-
        # threshold) target must reset strictly sooner than where we are.
        active_reset_ts = (
            _seven_day_reset_ts(usage.get(current), now) if consume_first else None
        )
        qualifying: list[tuple[tuple, str]] = []
        any_known = False
        stranded = False
        for num in oauth_candidates:
            h = headroom.get(num)
            if h is None:
                continue
            any_known = True
            if h <= 0:
                continue  # itself at its limit — never a target
            reset_ts = (
                _seven_day_reset_ts(usage.get(num), now) if consume_first else None
            )
            if trigger in ("proactive", "consume-first"):
                # Landing must be healthy: an account at/over the threshold
                # would re-trigger on the very next tick. At-limit and failover
                # are escapes that skip this whole block — any account with real
                # headroom beats a blocked or dead one.
                if (100.0 - h) >= settings.threshold:
                    continue
                if consume_first:
                    # Purely proactive on reset ordering: below the threshold,
                    # only move to accounts whose weekly window resets sooner
                    # than the active one (above the threshold we must move, so
                    # any healthy account qualifies and the sort picks soonest).
                    if trigger == "consume-first" and (
                        reset_ts is None
                        or active_reset_ts is None
                        or reset_ts >= active_reset_ts
                    ):
                        continue
                    # Burn-rate gate: reset_ts is known and future here. If the
                    # active account's burn rate has been measured, only move
                    # when enough of this candidate's perishable quota is
                    # reachable before it resets — otherwise the switch spends a
                    # cooldown to realize almost nothing (imminent / stranded).
                    # No rate → gate inert → plain soonest-first (merged path).
                    if (
                        trigger == "consume-first"
                        and burn_rate is not None
                        and reset_ts is not None
                    ):
                        hours_to_reset = (reset_ts - now) / 3600.0
                        remaining = 100.0 - (seven_day_pct(usage.get(num)) or 0.0)
                        reachable = min(remaining, burn_rate * hours_to_reset)
                        if reachable < MIN_REACHABLE_PCT:
                            stranded = True
                            continue
                elif active_headroom is not None:
                    # best: the candidate must beat the active account by the
                    # full hysteresis margin (a one-way move like 99%→89%
                    # qualifies; near-line pairs can't flap back).
                    if h - active_headroom < settings.hysteresis_pct:
                        continue
            if consume_first:
                # Soonest weekly reset first (unknown resets sort last), most
                # headroom breaks ties, then sequence order.
                key: tuple = (reset_ts if reset_ts is not None else float("inf"), -h)
            else:
                key = (-h,)
            qualifying.append((key, num))
        # Ascending by the strategy's key; list order (sequence order) breaks ties.
        qualifying.sort(key=lambda t: t[0])
        # A candidate that qualified overrides the stranded hold: the gate only
        # matters when it held back *every* sooner option.
        stranded = stranded and not qualifying
        return [num for _, num in qualifying], any_known, active_reset_ts, stranded

    # -- adaptive usage scheduling ---------------------------------------------

    def _collect_scheduled_usage(
        self,
        current: str,
        quarantined: set[str] = frozenset(),
        *,
        threshold: float | None = None,
    ) -> tuple[dict, dict[str, dict | str | None], dict[str, float | None]]:
        """Two-phase usage collection with an O(1) baseline.

        Phase A fetches the active account (when its persisted poll plan says
        it is due — poll_policy's urgent mode is what tightens that cadence
        near the band) plus ONE due candidate (the one with the stalest data
        — never-fetched first, then oldest fetch); everyone else is served
        from the usage store. Phase B refetches ALL candidates and recomputes
        before any switch decision when a switch could be near: active
        utilization within ``ESCALATION_MARGIN_PCT`` of the threshold, or
        active usage unknown (failover must not run on stale candidate data).
        At-limit, proactive, and ordinary unknown-usage failover selection
        never runs on the pre-escalation snapshot — those triggers imply the
        escalation condition (the deliberate exception: an owned-and-expired
        active is excluded above, so a post-idle-hold failover can run
        without escalating). The consume-first trigger can fire outside the
        escalation band, so it instead decides *provisionally* on the stored
        snapshot and, only when a switch would fire, re-runs an escalated
        collection and re-verifies the choice in ``_tick_inner`` (two-phase
        commit), plus a per-target ``UsageEntry.fresh`` gate before
        performing.

        Stalest-first needs no rotation cursor: it reads the persisted store,
        so the loop and cron-driven ``--once`` runs schedule identically.
        Backoff (``backoffUntil``) is enforced by the collector even for the
        active account — a Retry-After must never be defeated — and during an
        idle-hold no candidate is polled at all (slow crawl for everything).
        Adapted cadences are persisted by the collector itself after each
        fetch (shared with every other surface), not by the engine.

        Returns ``(entries, usage, headroom)`` where ``usage`` carries
        decision values and ``headroom`` the derived headroom per account.
        """
        now = self.clock()
        # Quarantined accounts can never be switch targets, so spending the
        # single alternate poll slot (or an escalation fetch) on one is wasted.
        candidates = [
            n
            for n in self.switcher.switchable_account_numbers()
            if n != current and n not in quarantined
        ]

        pre = self.switcher.usage_entries_by_account(fetch=set())
        plan: set[str] = set()
        active_pre = pre.get(current)
        # The active account is nominated when never fetched, poll-due per its
        # persisted plan, or (no plan yet) past the normal cadence floor. The
        # collector's reserve() honors due-ness even inside the serve TTL, so
        # an urgent plan (60s while burning near the band) actually fetches.
        # A candidate-style plan (slower than any active plan can be) left
        # over from a role change the switcher never saw (e.g. a manual
        # login) is overridden past the active age cap — but an exhausted
        # account stays parked at its reset: its numbers cannot move until
        # then, and the passed reset itself makes the plan due.
        stale_candidate_plan = (
            active_pre is not None
            and active_pre.age_s is not None
            and active_pre.age_s >= poll_policy.ACTIVE_MAX_INTERVAL_S
            and (active_pre.poll_interval_s or 0.0)
            > poll_policy.ACTIVE_MAX_INTERVAL_S
            and (binding_pct(active_pre.last_good, self._models) or 0.0) < 100.0
        )
        if (
            active_pre is None
            or active_pre.age_s is None
            or stale_candidate_plan
            or (
                active_pre.next_poll_at is not None
                and now >= active_pre.next_poll_at
            )
            or (
                active_pre.next_poll_at is None
                and active_pre.age_s >= poll_policy.MIN_INTERVAL_S
            )
        ):
            plan.add(current)
        if self._idle_hold_since is None:
            pick = due_candidate(candidates, pre, now)
            if pick is not None:
                plan.add(pick)
        entries = self.switcher.usage_entries_by_account(fetch=plan)
        usage = {num: entry.decision_value() for num, entry in entries.items()}

        active_value = usage.get(current)
        active_headroom = oauth.account_headroom(
            active_value if isinstance(active_value, dict) else None, self._models
        )
        # The caller's tick-snapshotted threshold, so one tick fetches and
        # decides on the same value even if apply_threshold() lands mid-tick.
        if threshold is None:
            threshold = self.settings.threshold
        escalate = bool(candidates) and (
            (active_headroom is None and active_value != USAGE_TOKEN_EXPIRED)
            or (
                active_headroom is not None
                and 100.0 - active_headroom >= threshold - ESCALATION_MARGIN_PCT
            )
        )
        if escalate:
            entries = self.switcher.usage_entries_by_account(
                fetch={current, *candidates}
            )
            usage = {num: entry.decision_value() for num, entry in entries.items()}

        headroom = _headroom_by_account(usage, self._models)
        return entries, usage, headroom

    def _perform(self, number: str, email: str, trigger: str) -> TickOutcome:
        if self.dry_run:
            current = self.switcher.current_account_number()
            current_email = self.switcher.account_email(current) if current else ""
            self._emit(
                SwitchEvent(
                    trigger=trigger,
                    from_ref=_ref(current, current_email) if current else None,
                    to_ref=_ref(number, email),
                    dry_run=True,
                )
            )
            return TickOutcome.SWITCHED

        # Hold the state lock across the whole recheck -> switch -> record
        # sequence so two concurrent engines (loop + cron --once) make one
        # serialized decision: the loser re-reads the winner's lastSwitchAt
        # and backs off instead of double-switching. No deadlock cycle: the
        # switch path (cswap FileLock + Claude Code locks) never takes the
        # state lock.
        with self._state_lock():
            state = self._read_state()
            if trigger in ("proactive", "consume-first") and self._in_cooldown(state):
                self._emit(NoSwitchEvent(reason="cooldown"))
                return TickOutcome.NO_ACTION

            result = self.switcher.switch_to(number, json_output=True)
            if not result or not result.get("switched"):
                self._emit(
                    NoSwitchEvent(
                        reason="already-active",
                        detail=(result or {}).get("reason", ""),
                    )
                )
                return TickOutcome.NO_ACTION

            state["schemaVersion"] = STATE_SCHEMA_VERSION
            state["lastSwitchAt"] = self.clock()
            state["lastSwitchTo"] = number
            atomic_write_json(self.state_path, state)

        self._emit(
            SwitchEvent(
                trigger=trigger,
                from_ref=result.get("from"),
                to_ref=result.get("to"),
                warnings=result.get("warnings", []),
            )
        )
        return TickOutcome.SWITCHED

    # -- helpers --------------------------------------------------------------

    def _in_cooldown(self, state: dict) -> bool:
        last = state.get("lastSwitchAt")
        if not isinstance(last, (int, float)):
            return False
        return (self.clock() - last) < self.settings.cooldown_seconds

    def _check_model_names(
        self, quarantined: set[str], usage: dict[str, dict | str | None]
    ) -> None:
        """One-shot ``autoswitch.model`` typo guard.

        A configured name that no account reports means the filter looks
        active while gating nothing. That's only provable once every
        relevant oauth account has readable usage this tick — adaptive
        polling legitimately leaves gaps before that — and never worth a
        forced refresh of its own.
        """
        wanted = {m.lower(): m for m in self._models if m.lower() != "all"}
        if not wanted:
            self._model_check_done = True  # bare "all" needs no name match
            return
        relevant = [
            n
            for n in self.switcher.switchable_account_numbers()
            if n not in quarantined
            and self.switcher.account_kind_for(n) != "api_key"
        ]
        values = [usage.get(n) for n in relevant]
        readable = [v for v in values if isinstance(v, dict)]
        if not readable or len(readable) != len(values):
            return  # not every account observed yet — re-check next tick
        seen = {
            s["name"].lower()
            for v in readable
            for s in (v.get("scoped") or [])
            if isinstance(s, dict) and isinstance(s.get("name"), str)
        }
        self._model_check_done = True
        missing = [name for low, name in wanted.items() if low not in seen]
        if missing:
            self._emit(
                ConfigWarningEvent(
                    message=(
                        f"autoswitch.model: {', '.join(missing)} matches no "
                        "account's usage windows — only the 5h/7d limits are "
                        "being watched for it (typo?)"
                    )
                )
            )

    def _earliest_recovery(
        self, usage: dict[str, dict | str | None]
    ) -> datetime | None:
        """Earliest moment any account becomes usable again (UTC), or None
        when that moment can't be proven.

        Per account that's the *latest* reset among its ≥100% relevant
        windows — an account blocked on both 5h and a scoped weekly limit
        isn't usable when the 5h rolls over — then the minimum across
        accounts, the active one included (its recovery also ends the
        blocked state). A blocked account whose exhausted windows carry no
        reset time at all could recover at any moment, so it makes the whole
        answer unprovable: return None and let the bounded blocked-cadence
        fallback re-check, rather than sleeping toward another account's
        later known reset."""
        earliest: float | None = None
        for value in usage.values():
            if not isinstance(value, dict):
                continue
            blocked = [
                resets_at
                for _, pct, resets_at in oauth.relevant_windows(value, self._models)
                if pct >= 100.0
            ]
            if not blocked:
                continue  # not exhausted — doesn't gate the blocked state
            usable_at = _limiting_reset_ts(value, self._models)
            if usable_at is None:
                return None  # blocked with unprovable recovery — don't oversleep
            if earliest is None or usable_at < earliest:
                earliest = usable_at
        if earliest is None:
            return None
        return datetime.fromtimestamp(earliest, tz=timezone.utc)

    def _emit(self, event: AutoSwitchEvent) -> None:
        self.on_event(event)

    # -- loop -------------------------------------------------------------------

    def stop(self) -> None:
        """Ask ``run_loop`` to exit; wakes it from any sleep. Safe to call
        before the loop starts — the stop is never cleared, so the loop
        exits immediately (engines are single-use)."""
        self._stop.set()
        self._wake.set()

    def wake(self) -> None:
        """Cut the current inter-tick sleep short and tick now."""
        self._wake.set()

    def apply_threshold(self, threshold: float) -> None:
        """Session override from the TUI: retarget the trigger and poll
        cadence mid-run. Threshold only — the model axes (and their derived
        state) are fixed at construction. The frozen-settings swap is atomic
        and each tick snapshots ``self.settings`` once, so no locking."""
        self.settings = replace(self.settings, threshold=threshold)
        self.switcher.set_poll_policy_inputs(threshold, self._models)

    def _next_delay(self, outcome: TickOutcome) -> float:
        interval = self.settings.interval_seconds
        if outcome is TickOutcome.BLOCKED:
            if self._sleep_until_ts is not None:
                delay = self._sleep_until_ts - self.clock()
                return min(max(delay, interval), MAX_SLEEP_S)
            if self._blocked_wait_long:
                # Truly exhausted with no reset time known / no candidates.
                return max(interval, NO_RESET_FALLBACK_S)
            # Blocked on something that can resolve any tick (hysteresis,
            # unreadable usage) — keep the normal cadence so the at-limit
            # escape isn't missed.
        elif outcome is TickOutcome.NO_ACTION and self._idle_hold_slow:
            # Idle-hold: Claude is idle on an expired token — nothing changes
            # until the user comes back, so crawl. Worst case protection
            # resumes one slow tick after they do.
            return max(interval, NO_RESET_FALLBACK_S)
        # ±10% jitter so multiple machines don't synchronize their API hits.
        return interval * (0.9 + 0.2 * random.random())

    def run_loop(self) -> int:
        """Tick forever (until :meth:`stop`); a failing tick never kills it."""
        while True:
            # Clear at the top, not after the wait: a wake() racing a wait
            # timeout is then never lost — the tick right after this clear
            # already sees whatever settings that wake announced.
            self._wake.clear()
            if self._stop.is_set():
                return 0
            try:
                outcome = self.tick()
            except Exception as e:  # pragma: no cover - tick() already guards
                self._emit(
                    ErrorEvent(message=f"{type(e).__name__}: {e}", transient=True)
                )
                outcome = TickOutcome.ERROR
            delay = self._next_delay(outcome)
            if delay > self.settings.interval_seconds * 1.5:
                until = datetime.now(timezone.utc) + timedelta(seconds=delay)
                self._emit(
                    SleepEvent(
                        seconds=delay,
                        until=until.isoformat(timespec="seconds").replace(
                            "+00:00", "Z"
                        ),
                    )
                )
            self._wake.wait(delay)
