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
import math
import random
import threading
import time
from collections.abc import Callable, Sequence
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
from claude_swap.usage_store import due_candidate, plan_oversleeps_interval

STATE_FILENAME = "autoswitch_state.json"
STATE_SCHEMA_VERSION = 1

_logger = logging.getLogger("claude-swap")

# Systemic freshen refusals, MOST ACTIONABLE FIRST. Deterministic conditions
# that every candidate hits identically, so the tick reports one of them —
# and the order decides which, because reporting the wrong one is how a cause
# needing a human hides behind one that clears itself. store-unmirrored and
# invalid_client stay until somebody unsets an env var or fixes a client
# registration, and stash-unreadable until they unlock a keychain, fix a mode,
# or purge the row; consume-busy is gone by the next pass. stash-unreadable is
# the one that is per-SLOT rather than global, which costs nothing here: this
# message is only ever emitted when NO candidate freshened, so naming the real
# cause of the only slot that had one beats "(network?)".
_SYSTEMIC_MESSAGES = {
    "store-unmirrored": "CLAUDE_SECURESTORAGE_CONFIG_DIR is set — unset it or "
                        "run cswap from a normal shell",
    "invalid_client": "cswap's OAuth client was rejected — systemic, not this "
                      "account",
    "stash-unreadable": "a stashed successor is unreadable — unlock the "
                        "keychain or fix the file, then retry; "
                        "`cswap unclaimed` inspects it",
    "consume-busy": "another cswap surface holds the slot — retries next pass",
}
# Insertion order IS the precedence order, so the remedy and its rank cannot
# drift apart.
_SYSTEMIC_STATUSES = tuple(_SYSTEMIC_MESSAGES)

# Freshen targets whose access token expires within this window: twice Claude
# Code's own 5-minute refresh buffer, so its post-lock "abort refresh if not
# expired" re-read holds with margin after our swap.
FRESHEN_BUFFER_MS = 10 * 60 * 1000

# Sleep caps around a known quota reset (RESET_SLACK_S lives in poll_policy
# with the rest of the cadence numbers). Recheck at the exhausted-account poll
# cadence: providers can grant quota before the previously reported reset, and
# a long engine sleep must not suppress the fetch that discovers it.
MAX_SLEEP_S = poll_policy.EXHAUSTED_INTERVAL_S
NO_RESET_FALLBACK_S = 300.0

# Idle-hold cap (elapsed, not ticks — the hold itself slows the cadence to
# NO_RESET_FALLBACK_S): an owned-and-expired token normally means Claude Code
# is idle and will self-heal on next use, but a *dead* refresh token with an
# active user would look identical forever, so after this long the engine
# falls back to normal unhealthy counting.
IDLE_HOLD_MAX_S = 30 * 60.0

# Anti-flap margin for the every-account-above-threshold escape, measured on
# the axis that escape ranks by: a target must come back at least this much
# sooner than the account we are leaving. Five minutes is comfortably longer
# than one poll cycle, so two accounts whose windows roll over close together
# cannot trade places on measurement jitter — the reverse move never clears
# the margin. The percentage-point hysteresis is unmeetable in this state by
# construction (everything is within a few points of its limit), which is why
# it needs its own unit rather than a reused one.
RECOVERY_HYSTERESIS_S = 300.0

# Horizon past which a sooner reset stops being worth real headroom. The escape
# above was measured on minutes-scale resets; days-scale is the opposite trade,
# since neither account returns within the session. 4h keeps most of a 5-hour
# cycle on the recovery ranking: a 5h window can be up to 5h from resetting, so
# a peer bound by one that is 4h-5h out falls back to headroom ranking instead.
# Deliberately the conservative side of that boundary -- ranking by headroom
# where the reset is still an hour away costs at most one extra move, while a
# wider horizon would rank by a reset the session may never see.
RECOVERY_HORIZON_S = 4 * 3600.0

# Anti-flap margin on the headroom axis, as a RATIO rather than percentage
# points: strictly-more is no margin at all — one point moves the engine, the
# target burns it back, and it ping-pongs. A ratio makes the move one-way.
HORIZON_HEADROOM_RATIO = 2.0

# The two anti-flap thresholds are both anchored to `active_headroom`, which
# leaves a band where a peer is VISIBLE (the spent clause goes false, so the
# reset axis switches off) yet UNCHOOSABLE (it misses the margin). Measured,
# active 3.00 pts took a 0.10-pt peer over a 5.99-pt one, discarding 60x the
# runway; and inside the band monotonicity inverts, so adding headroom flips a
# move into a refusal.
#
# No single constant removes both ends — the two band widths sum to
# `active x HORIZON_HEADROOM_RATIO - SPENT_HEADROOM_PCT`, so shrinking one
# grows the other. Measured, not argued. The fix is in the ranking instead:
# `best_candidate_headroom` carries no floor, and margin failures are
# re-admitted through a one-way fallback used only when nothing else
# qualifies.

# Below this an account is spent, and headroom comparisons between two spent
# accounts compare noise (a point is under ten minutes of work, less than two
# poll intervals). When EVERY candidate is down here, rank by reset instead —
# sit where quota returns first, however far out — rather than parking on
# whichever account we happen to hold.
SPENT_HEADROOM_PCT = 3.0


def _recovery_is_useful(
    candidate_recovery_ts: float,
    active_recovery_ts: float,
    active_headroom: float,
    best_candidate_headroom: float,
    now: float,
) -> bool:
    """Rank THIS candidate by soonest reset, rather than by headroom?

    Two clauses, one place. Deciding it from four scattered gates left
    reachable holes in four of the sixteen combinations.

    Reset wins when everything worth having is spent — below
    ``SPENT_HEADROOM_PCT`` a headroom edge is under two poll intervals, so the
    only real question is which account returns first. Asked of the active and
    the BEST candidate, not of every account: an unknown headroom is not
    evidence of an empty one, and requiring all of them let a single sentinel
    row veto the check for everybody.

    It also wins when THIS candidate is back soon. Asked per candidate, not
    once on the active: an active bound by its weekly window sits days out
    while a peer's five-hour window returns in minutes.

    Past the horizon we rank by headroom, and when no candidate meets the ratio
    nobody qualifies — an active holding more quota than any peer can offer
    should keep the work.

    THE AXIS IS A PROPERTY OF THE PAIR, not of the candidate alone, and that
    is what stops the two guards leaking into each other. Each anti-flap gate
    is one-way on its OWN axis — hysteresis on recovery, a ratio on headroom —
    but a switch swaps which account is "active". Keying the choice on the
    candidate alone flipped the axis with it, so a pair straddling the horizon
    took one gate going out and the OTHER coming back, and neither guard ever
    saw both legs:

        acct 1   8 points, reset 109h out      acct 2   3 points, reset 3.5h out
        active=1 -> candidate inside  -> recovery: 3.5h < 109h      moves
        active=2 -> candidate outside -> headroom: 8 >= 3*2         moves back

    Measured: 47 credential rewrites over 3.9h on frozen inputs, ending only
    when the sooner reset landed. ``either side inside`` is symmetric under
    that swap, so the axis survives the move and the gate that permitted the
    outbound leg is the one asked about the return — where hysteresis refuses
    it, because the account we just left is now the distant one.

    Why EITHER and not BOTH: requiring both would refuse the #202 case this
    horizon exists to preserve — a weekly-bound active sitting days out while
    a peer's five-hour window returns in eight minutes. Measured across
    multiple ticks, both rules were checked against both shapes:

        rule    oscillating pair        #202 pair
        cand    moves, moves back       moves, moves back   (the bug)
        both    never moves             never moves         (breaks #202)
        either  moves, then holds       moves, then holds   (wanted)

    The #202 case oscillated on the original code too — its test ticks once,
    so it only ever observed the outbound leg.

    The step between the two axes is NOT monotone, and an earlier version of
    this docstring claimed it was ("0 inversions"). Re-swept directly over the
    predicate — active 1..12 pts against a peer walked 0.5..40 at 0.05, four
    reset shapes — a strictly-better peer does flip a move into a refusal, and
    every case sits on one point: `peer_h` crossing SPENT_HEADROOM_PCT, where
    the spent clause goes false and the axis changes underneath the comparison.

    That is the axis boundary, not a leak, and it is NOT reachable through
    `tick()`: the fallback at the bottom of the ranking loop re-admits exactly
    those candidates. A reviewer re-took the same sweep and got a different
    count from mine, which is the point — the count depends on the sweep's step
    and reset shapes, so it is stated as WHERE rather than HOW MANY.
    """
    if (
        active_headroom <= SPENT_HEADROOM_PCT
        and best_candidate_headroom <= SPENT_HEADROOM_PCT
    ):
        # The axis CAN change as the fleet burns, and that is not a leak:
        #
        #     out    active 2.0 / peer 4.0   headroom axis, 4.0 >= 2.0x2
        #     back   active 3.0 / peer 2.0   recovery  axis, 10h vs 80h
        #
        # Each leg is legitimate on the axis its own state selects, and the
        # transition is in the DATA rather than in the gates: constraining
        # either gate does not remove it.
        return True
    return (
        candidate_recovery_ts - now <= RECOVERY_HORIZON_S
        or active_recovery_ts - now <= RECOVERY_HORIZON_S
    )


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
    # The account number the engine would move to if the active account
    # crossed the threshold on this tick's snapshot; None when nothing
    # qualifies. A prediction for watch screens, computed every tick —
    # see `_predict_next_target`. Additive field.
    next_target: str | None = None

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
        if self.next_target is not None:
            fields["nextTarget"] = self.next_target
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


def _binding_recovery_ts(
    usage: dict | str | None, models: Sequence[str], now: float
) -> float:
    """When this account's *binding* window comes back, as a sort key.

    The binding window is the one holding the account back — the highest
    utilization among the windows that gate it (the same set
    ``account_headroom`` measures, so ranking and headroom can never disagree
    about which window matters). Its reset is the moment the account becomes
    useful again.

    Not the weekly window: with every account in the 90s the thing that
    decides where to go is which 5-hour window rolls over first, and that is
    routinely minutes away while the weekly one is days away.

    Returns ``inf`` when unknown or already past, so such accounts sort last
    rather than masquerading as "back immediately" — a stale ``resets_at``
    would otherwise rank a snapshot nobody has refreshed above a measured,
    genuinely imminent one.
    """
    # Pick the BINDING window first, then ask for its reset. Filtering on the
    # reset before the max lets a lower window win whenever the binding one's
    # reset is unknown or past — measured: 7d at 95% with no resets_at and 5h
    # at 40% resetting in an hour returned "back in an hour", which is the
    # opposite of what binds. An account whose binding window has no usable
    # reset is one we cannot schedule around, and inf sorts it last.
    windows = list(oauth.relevant_windows(usage, models))
    if not windows:
        return float("inf")
    _label, _pct, resets_at = max(windows, key=lambda w: w[1])
    ts = _parse_reset_ts(resets_at)
    return ts if ts is not None and ts > now else float("inf")


def _every_account_above_threshold(
    candidates: Sequence[str],
    headroom: dict[str, float | None],
    active_headroom: float | None,
    threshold: float,
) -> bool:
    """Whether the active account AND every measured candidate are at or over
    the threshold — the state where "land somewhere healthy" has no answer.

    Requires the active account's own headroom to be known: without it we do
    not know we are in this state, and guessing here would relax the landing
    rule on an ordinary tick. An unmeasured candidate does not block the
    verdict (it may be healthy, but it cannot be *chosen* either — the caller
    skips ``None`` headroom) as long as at least one candidate was measured.
    """
    if active_headroom is None or (100.0 - active_headroom) < threshold:
        return False
    measured = [headroom.get(n) for n in candidates if headroom.get(n) is not None]
    if not measured:
        return False
    return all((100.0 - h) >= threshold for h in measured)


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
        # Held for the whole switch transaction in `_perform`; `stop()` takes
        # it after signalling, so it cannot return while a switch is under
        # way on another thread. Re-entrant: a stop() issued from the tick
        # thread itself (SIGTERM handler) must never wait on its own switch.
        self._switch_lock = threading.RLock()
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
        # The consume gate serializes every backup-rt POST (the recovery
        # branch in `_fetch_active_usage` is a second call site, under the
        # same per-slot consume lock):
        # it re-reads under the slot lock (our snapshot may be superseded),
        # consults the session profile for a newer generation, and persists
        # via fingerprint CAS — so a freshen racing the collector (or a
        # sibling surface) can no longer double-consume one grant.
        outcome = self.switcher.consume_backup_grant(number, email, creds)
        if outcome.error is None and outcome.credentials:
            # The gate already persisted the successor (or adopted a racing
            # writer's newer lineage) under its own lock.
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
        if outcome.error in _SYSTEMIC_STATUSES:
            # Deterministic conditions, not network trouble: every candidate
            # refuses identically and keeps refusing until something outside
            # this process changes — the shell for store-unmirrored (an
            # inherited CLAUDE_SECURESTORAGE_CONFIG_DIR), our OAuth client
            # registration for invalid_client. Reported distinctly so the tick
            # error names the real cause instead of "(network?)", which would
            # send the user to check a connection that is fine.
            return outcome.error
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
                next_target=self._predict_next_target(
                    state, current, quarantined, usage, headroom, settings
                ),
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
                # Expired and the refresh could not complete this pass (lock
                # contention, unattributable lineage, failed persist, or the
                # row's failure backoff gating the fetch). The locked-refresh
                # path retries on later passes — no quota burn, nothing to
                # switch for yet; crawl slowly instead of burning failover
                # ticks (Finding 2 of the usage-lapse investigation).
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
        # The no-return bar itself lives in `_rank` below: it is a statement
        # about the CHOICE, so it belongs where the choice is made rather than
        # in this census of what exists. See `_no_return_account` for the
        # incident, the scoping, and the release.
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


        decided_now = self.clock()
        ordered, any_known, active_reset_ts = self._rank_with_bar(
            state,
            trigger=trigger,
            consume_first=consume_first,
            oauth_candidates=oauth_candidates,
            usage=usage,
            headroom=headroom,
            current=current,
            active_headroom=active_headroom,
            settings=settings,
            now=decided_now,
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
            decided_now = self.clock()
            ordered, any_known, active_reset_ts = self._rank_with_bar(
                state,
                trigger=trigger,
                consume_first=consume_first,
                oauth_candidates=oauth_candidates,
                usage=usage,
                headroom=headroom,
                current=current,
                active_headroom=active_headroom,
                settings=settings,
                now=decided_now,
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
            # "All exhausted" (and its bounded reset-aware sleep) only when it's
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
        # The departure snapshot of the account we are leaving, taken from the
        # SAME `usage`/`headroom` the ranking just decided on — for
        # consume-first that is the phase-2 refetch, not the stale one.
        left_snapshot = (
            active_headroom,
            _binding_recovery_ts(usage.get(current), self._models, decided_now),
        )
        transient_failure = False
        systemic = ""
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
                return self._perform(num, email, trigger, left_snapshot)
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
            if status in _SYSTEMIC_STATUSES:
                # ONE cause is reported, so it must be the one worth acting
                # on. Assigning unconditionally made it the LAST candidate's,
                # and `consume-busy` clears itself on the next pass while the
                # other two need a human — unset an env var, chase a rejected
                # client_id. So a busy slot sorting after an unmirrored one
                # named the harmless cause and hid the real one: exactly the
                # "reads as intermittent, nothing names it" trap these kinds
                # were split out of "transient" to escape.
                if not systemic or _SYSTEMIC_STATUSES.index(
                    status
                ) < _SYSTEMIC_STATUSES.index(systemic):
                    systemic = status
                continue
            if status == "skip-live-session":
                continue
            return self._perform(num, email, trigger, left_snapshot)

        if systemic or transient_failure:
            self._emit(
                ErrorEvent(
                    message="could not freshen: " + _SYSTEMIC_MESSAGES[systemic]
                    if systemic
                    else "could not freshen any candidate (network?)",
                    transient=True,
                )
            )
            return TickOutcome.ERROR
        self._emit(NoSwitchEvent(reason="no-viable-target"))
        return TickOutcome.BLOCKED

    def _no_return_account(
        self,
        trigger: str,
        state: dict,
        headroom: dict[str, float | None],
        active_headroom: float | None,
        recovered: bool,
        settings: AutoSwitchSettings,
        current: str | None = None,
    ) -> str | None:
        """The account this engine most recently left, while it is still barred.

        NEVER UNDO THE PREVIOUS MOVE. Each anti-flap gate is one-way on its own
        axis, but the axis is a property of the pair's STATE and burn changes
        that state: the ratio gate is relative (`h >= active x 2`) and the
        spent gate absolute (`active <= 3.0`), so a burning pair crosses the
        boundary repeatedly and each crossing re-opens a move. Measured, both
        resets past the horizon, only the active burning: `[1, 2, 1, 2]` where
        base makes one move.

        SCOPED like every sibling gate — `at-limit` and `failover` skip the
        anti-flap gates by design. Unscoped this stranded a 2-account fleet on
        an exhausted active with the peer at 0%.

        AND SCOPED TO THE ENGINE'S OWN LANDING (`lastSwitchTo == current`).
        The bar refuses to undo THIS ENGINE'S last move; once the user
        switches by hand the engine is no longer sitting where it put itself
        and that move is already undone, so the bar protects nothing and
        merely withholds the fleet's best account. Reproduced: engine 1 -> 2,
        user 2 -> 3 by hand, account 1 on 4 pts still barred against an
        active on 2, every reset far out, no release leg reachable — the
        `not recovered` return below fires before the ratio leg is read, and
        `recovered` is False because account 1 was 4 pts at departure too.
        The engine then holds the worse account until the at-limit escape.
        Both sides are `str`-normalised: `lastSwitchTo` is a `str` from
        `_perform` and `lastSwitchFrom` an `int` from `account_ref`. A state
        record written before `lastSwitchTo` existed has no such key and
        cannot prove the engine moved away, so it KEEPS the bar — the same
        conservative reading this module gives every other missing field, and
        the only one that does not silently drop the anti-flap bound for an
        upgrade cycle.

        RELEASED when the account we left now beats us by the same ratio the
        anti-flap margin uses: that is not the flip this bars, it is a move the
        outbound leg would have made on its own merits — BUT ONLY IF IT HAS
        ACTUALLY RECOVERED. The ratio compares against the ACTIVE, and the
        active burns, so ungated it comes true on a target that has done
        nothing. Measured on the cited walk, the barred account held 4.0 pts at
        departure and 4.0 pts at every return; the ratio fired purely because
        the active fell to 2.0, which is the flap arriving through the release
        instead of through the ranking. `recovered` is that gate, computed once
        per snapshot by `_left_account_recovered` and shared with the
        leaves-nothing retry in `_rank` so the two cannot disagree.

        THE LEAVES-NOTHING RELEASE IS NOT HERE. It used to be, and it was
        rewritten twice for the same reason both times: this function cannot
        see the gates that decide. `lastSwitchFrom` is rewritten only by a
        successful switch — the one thing the bar prevents — so a bar that
        empties the ranking is permanent, and each attempt to predict emptiness
        landed one gate short of the ranking loop:

            all(n == barred ...)              "does another account EXIST"
            (headroom.get(n) or 0.0) > 0.0    "is another account not at its limit"

        The loop also applies the threshold, `h >= active x HORIZON_HEADROOM_
        RATIO`, the spent fallback's `h >= active` plus a sooner reset, and the
        recovery hysteresis. A third account on ONE point clears both
        predicates above and none of those, so the n=2 stall came back at n>=3
        and then again one point up. Measured: barred peer 3.5 pts / back in
        10h, active 2 pts / 500h out, third 1 pt — 30 ticks all BLOCKED, and
        the same fleet with `lastSwitchFrom` popped switches on the first.

        `_rank` now ASKS the ranking instead: it ranks with the bar, and
        re-ranks without it when the result is empty. That is exact by
        construction, covers the recovery axis this predicate never could, and
        cannot be one gate behind because it is not a separate copy of the
        gates.

        EMPTINESS ALONE IS NOT THE RELEASE, though — see `_rank`. At n=2 the
        barred ranking is always empty, so an emptiness-only retry is a no-op
        at exactly the fleet size the flap was reported on. Both the retry and
        the ratio above are gated on `recovered`, which is the one question the
        ranking cannot answer: is this a different account from the one we
        left, or only a different active?
        """
        came_from = state.get("lastSwitchFrom")
        if trigger not in ("proactive", "consume-first") or came_from is None:
            return None
        # Only while we are still standing where that switch put us. A manual
        # switch away already undid the move, so there is nothing left to
        # refuse to undo. `str` on both sides: `lastSwitchTo` is written from
        # `_perform`'s `number: str`, `lastSwitchFrom` from `account_ref`'s
        # `number: int`, and an int/str mismatch here would disarm the bar
        # everywhere rather than only after a hand switch.
        landed_on = state.get("lastSwitchTo")
        if landed_on is not None and current is not None:
            if str(landed_on) != str(current):
                return None
        # No membership check on `oauth_candidates`: the loop compares
        # `num == no_return` while iterating that same list, so naming an
        # account that is not in it bars nothing. The check was a no-op and
        # nothing killed it under mutation.
        barred = str(came_from)
        if not recovered:
            return barred        # the ratio below burns true on its own; see above
        left_headroom = headroom.get(barred)
        if left_headroom is not None:
            if active_headroom is not None:
                if left_headroom >= active_headroom * HORIZON_HEADROOM_RATIO:
                    return None               # beats us outright; not a flip
            elif (
                settings is not None
                and left_headroom > 100.0 - settings.threshold
            ):
                # An unreadable active must not be silently scored as "the
                # peer does not beat it" -- same landing-eligible fallback
                # `_left_account_recovered` uses when it, too, has no active
                # to compare against.
                return None
        return barred

    def _left_account_recovered(
        self,
        state: dict,
        usage: dict[str, dict | str | None],
        headroom: dict[str, float | None],
        active_headroom: float | None,
        settings: AutoSwitchSettings,
        now: float,
        current: str | None = None,
    ) -> bool:
        """Is the account we left a better proposition than when we left it?

        This is the release the bar needs and the ranking cannot supply. A bar
        that leaves nothing is a stall, but "leaves nothing" is also what every
        flap looks like on two accounts, so lifting on emptiness alone lifts
        always. The distinction is not in the present state — it is between the
        present and the moment of departure, which is why `_perform` records
        that moment (`leftHeadroom` / `leftRecoveryAt`) alongside
        `lastSwitchFrom`.

        Measured on the walk this guard exists for, the barred account was
        IDENTICAL at every return — same headroom, same reset — and only the
        active had changed. That is the flap: the ranking flipped underneath a
        target that did nothing.

        FAILOVER FIRST, before any leg reads the active — checked ahead of
        dominance, because dominance-first starves this branch:
        `test_a_failover_departure_does_not_disarm_the_bar` broke on its
        FIRST tick the instant the active fell far enough for `4.0 > active
        x 2` to go true (measured at active=1.8, one fifth of a point past
        the boundary this branch's own sibling test already sits on).
        A `(None, None)` snapshot means severity was genuinely unmeasured at
        departure — there is no `leftHeadroom` to diff against and never was
        — so the two signals that do not depend on the active's LIVE state
        are (1) whether the peer, right now, would itself be a healthy place
        to land: `h > 100 - settings.threshold`, the same "would the ranking
        accept this as a landing spot" test `_rank_candidates` already runs
        (`:1617`) on every candidate, reused rather than inventing a fresh
        constant; and (2), when the landing floor cannot answer, whether the
        peer's own binding reset is meaningfully sooner than the active's.
        The landing floor is the exact complement of
        `_every_account_above_threshold`, so it is UNSATISFIABLE whenever
        the
        fleet is all-spent — the recovery leg is what keeps the hold from
        becoming unconditional in exactly that regime. Neither leg can tell
        "genuinely recovered" from "was already this good" — there is
        nothing recorded to tell them apart. Measured which side the landing
        floor should land on: sweeping mutations of this same leg for the
        ordinary path showed an absolute floor is silently reintroducible
        with a green suite, so this is not free of that risk either — the
        difference is this constant is `settings.threshold`, not a
        hardcoded number, so a
        user's OWN policy decides how conservative the hold is, and it moves
        when they change it (pinned directly, below). Deliberately MORE
        conservative than the ordinary path below: a peer sitting at 4 points
        held through the whole walk that broke a bare dominance leg, with no
        upper bound short of the peer crossing the threshold itself OR its
        binding reset pulling meaningfully ahead of the active's. Bounded,
        not permanent — at-limit still escapes untouched
        (`_no_return_account` scopes this trigger out entirely), and the
        recovery leg means the bound is no longer just "the active reaches
        its own hard limit": a peer that resets first releases the hold on
        its own schedule, without the active ever needing to burn down to it.

        THE ORDINARY PATH HAS A REAL BASELINE (`leftHeadroom` is a number,
        not null), so it gets three legs, checked in this order, each with
        the margin its axis already uses. Burn cannot manufacture any of
        them: headroom rises only when a window rolls over, the ratio needs
        the active to lose more than half its remaining headroom AND clear
        an extra `SPENT_HEADROOM_PCT` on top, and the binding reset moves
        nearer only when a nearer window starts binding.

          dominance   `> active x HORIZON_HEADROOM_RATIO + SPENT_HEADROOM_PCT`
                      against the ACTIVE. A peer moved AWAY from for a reason
                      other than headroom (e.g. consume-first's reset
                      ordering) can dominate the active from the moment it
                      was left, and self-improvement against its own
                      departure baseline never fires for an account that had
                      nothing to improve on. The `+SPENT_HEADROOM_PCT` on top
                      of the bare ratio is what the bare ratio misses:
                      measured on this branch's own flap fleet (peer frozen
                      4.0 pts, active burning 98.0% -> 98.4%), it flips
                      true at active=1.8 pts purely because the active kept
                      burning; the same walk with the margin added stays
                      false through active=1.6 and only opens once the active
                      is down to a genuine sliver (`x < 0.5`), which is the
                      at-limit escape's territory, not a return worth calling
                      anti-flap. `test_a_burn_walk_never_returns_to_what_it_
                      left` still settles with the margin in place — it makes
                      the boundary harder to cross, not impossible.
          headroom    `+SPENT_HEADROOM_PCT` against the DEPARTURE baseline
                      — below that an edge is under two poll intervals, the
                      same reason the spent band exists.
          recovery    `-RECOVERY_HYSTERESIS_S` against the DEPARTURE
                      baseline — the same margin the recovery axis ranks by
                      one gate later.

        NO SNAPSHOT MEANS RELEASE. State written before this field existed, or
        by a switch that never recorded one, carries no evidence either way —
        and of the two failure modes the permanent proactive lockout is the
        worse one, because it is persisted and survives a restart and a week of
        wall clock. Absence of evidence releases. This only gates the
        proactive/consume-first return; `_no_return_account` scopes at-limit
        and failover out of the bar by design, so either trigger still
        escapes the account untouched, and the next successful switch
        overwrites the snapshot outright.
        """
        came_from = state.get("lastSwitchFrom")
        if came_from is None:
            # Unreachable through `_no_return_account`, the only caller: it
            # returns `None` at its own `came_from is None` check before
            # `recovered` is ever read. Kept `True` (not load-bearing) so a
            # future direct caller gets "no evidence, release" rather than a
            # silent hold.
            return True
        barred = str(came_from)
        if "leftHeadroom" not in state:
            return True          # pre-upgrade record: genuinely no evidence
        h = headroom.get(barred)
        left_headroom = state.get("leftHeadroom")
        left_recovery = state.get("leftRecoveryAt")
        # A `consume-first` departure can ALSO write (None, None) -- the
        # same shape a real failover writes -- whenever the phase-2 refetch's
        # active row is unmeasurable for headroom but still has a known
        # weekly reset (the split shape `oauth.
        # build_usage_result` emits for `utilization: null` plus a
        # `resets_at`). Inferring "failover" from the two nulls then ran the
        # more permissive failover legs (landing floor + recovery-only) on
        # what was really an ordinary departure -- reachable on 32%+ of
        # swept fleets, both directions. `leftTrigger` records the actual
        # trigger so this never has to guess again; a record written before
        # this field existed has no such key, so fall back to the old
        # two-null inference for it (unchanged behaviour for pre-upgrade
        # state).
        left_trigger = state.get("leftTrigger")
        is_failover_snapshot = (
            left_trigger == "failover"
            if left_trigger is not None
            else (left_headroom is None and left_recovery is None)
        )
        if is_failover_snapshot:
            # Failover: real departure, severity unmeasured at the time --
            # not absence of evidence, and there is no baseline to diff
            # against (that is exactly what "unmeasured" means), so this
            # cannot use the active's HEADROOM at all -- see docstring for
            # why reading the active's headroom here is wrong, and the walk
            # that proved it. Two legs, both read-only against CURRENT state
            # (no departure baseline exists to diff against):
            #
            #   landing   `h > 100 - settings.threshold` -- would the
            #             ranking accept this peer as a landing spot right
            #             now (`_rank_candidates`, :1636)?
            #   recovery  the peer's binding reset is meaningfully sooner
            #             than the ACTIVE's binding reset -- the same axis
            #             `_recovery_is_useful` switches to once headroom
            #             stops being informative. Needed because `landing`
            #             is the exact complement of `_every_account_above_
            #             threshold`: whenever the fleet is all-spent,
            #             `landing` is unsatisfiable by construction, no
            #             matter how soon the peer's own
            #             window resets, and that regime is precisely where
            #             the recovery axis is the one the engine trusts.
            #
            # Burn cannot fake the recovery leg: a reset moves nearer only
            # when a nearer window starts binding, never as a side effect
            # of the active spending down -- the failure mode a bare
            # dominance leg has, guarded directly in the mutation table.
            if h is not None and h > 100.0 - settings.threshold:
                return True
            peer_recovery_ts = _binding_recovery_ts(usage.get(barred), self._models, now)
            active_recovery_ts = _binding_recovery_ts(usage.get(current), self._models, now)
            # The active's recovery must be a REAL measurement, not merely
            # "larger" -- `_binding_recovery_ts` returns `inf` for both
            # "never resets" and "we do not know" (no windows, no
            # `resets_at`, or a stale/past `resets_at`). Reading `inf` as
            # "never" here made `peer < inf - HYST` true for ANY finite
            # peer reset, releasing onto a peer arbitrarily far out on no
            # evidence. `math.isfinite` requires the active to have a
            # genuine, known reset before the comparison even runs --
            # unknown holds, exactly like unreadable already does on the
            # headroom axis.
            #
            # But two of the five `inf` states are ordinary shapes for an
            # active that is plainly alive and burning -- a `pct` reported
            # with no `resets_at`, or a `resets_at` already elapsed -- not
            # unknowns. Reading all five as "unknown, hold" pins the engine
            # on a near-spent active for up to a full window even when the
            # peer is back within `RECOVERY_HORIZON_S`, the
            # same constant this PR already uses for "near enough to
            # matter" (`_recovery_is_useful`). Requiring EITHER a known
            # active reset OR a peer inside that horizon keeps `isfinite`'s
            # intended release (a known active vs. an arbitrarily-far peer
            # still needs `isfinite`) while letting a near peer through
            # regardless of why the active's own reset reads `inf`.
            return (
                (
                    math.isfinite(active_recovery_ts)
                    or peer_recovery_ts - now <= RECOVERY_HORIZON_S
                )
                and peer_recovery_ts < active_recovery_ts - RECOVERY_HYSTERESIS_S
            )
        # Dominance over the ACTIVE, only reached once a real baseline is
        # confirmed to exist above -- a peer that was already miles ahead of
        # the active at departure (moved for a DIFFERENT reason -- e.g.
        # consume-first's reset ordering, not headroom) never "improves" on
        # its own baseline and would stall on self-improvement alone despite
        # dominating throughout.
        #
        # `active_headroom is None` means "we could not read the active this
        # tick" -- reachable through the consume-first two-phase commit,
        # which reassigns `active_headroom` from a fresh
        # refetch without re-classifying the trigger. That is a DIFFERENT
        # state from "readable, but does not dominate" and must not answer
        # the same way: fall back to the landing-eligible test the failover
        # branch above uses when IT has no baseline to compare against
        # either, rather than silently treating "unreadable" as "no
        # dominance".
        #
        # DEFENSIVE, not currently outcome-changing: measured exhaustively,
        # `active_headroom=None` on this path closes
        # BOTH of `_rank_candidates`'s gates before this leg is ever asked
        # (`_every_account_above_threshold` is False on a None active, and
        # the consume-first `active_reset_ts` gate is None too), so no
        # fleet shape has been found where this branch changes a `tick()`
        # outcome. Kept because the call IS reachable (confirmed via the
        # phase-2 refetch) and a future change to those gates could make it
        # live without anyone revisiting this function -- silently reading
        # None as "no dominance" would then be exactly the bug the
        # unreadable-active fallback was added for.
        if h is not None:
            if active_headroom is not None:
                if h > active_headroom * HORIZON_HEADROOM_RATIO + SPENT_HEADROOM_PCT:
                    return True
            elif h > 100.0 - settings.threshold:
                return True
        if (
            isinstance(left_headroom, (int, float))
            and h is not None
            and h >= min(left_headroom + SPENT_HEADROOM_PCT, 100.0)
        ):
            return True
        # `None` is the JSON-safe spelling of "unknown or already past", which
        # `_binding_recovery_ts` returns as `inf`: an account nobody can
        # schedule around. Moving off it onto a real reset IS the improvement.
        was = left_recovery if isinstance(left_recovery, (int, float)) else float("inf")
        return (
            _binding_recovery_ts(usage.get(barred), self._models, now)
            < was - RECOVERY_HYSTERESIS_S
        )

    def _predict_next_target(
        self,
        state: dict,
        current: str,
        quarantined: set[str],
        usage: dict[str, dict | str | None],
        headroom: dict[str, float | None],
        settings: AutoSwitchSettings,
    ) -> str | None:
        """The account the engine would move to if the active account crossed
        the threshold on this snapshot, or None when nothing qualifies.

        `best` never ranks while the active account is healthy, so below the
        threshold the ranking runs as though the active account sat exactly at
        it — the state a real proactive decision is made in. consume-first and
        over-threshold ticks are ranked on their real trigger and headroom.
        Same ranking and no-return bar as a real decision; consume-first's
        phase-2 refetch can still land elsewhere, which is fine for a
        prediction. Pure — no emits, no state writes.
        """
        if (
            self.switcher.account_kind_for(current) == "api_key"
            and not settings.include_api_key_accounts
        ):
            return None
        active_headroom = headroom.get(current)
        if active_headroom is None:
            return None
        candidates = [
            num
            for num in self.switcher.switchable_account_numbers()
            if num != current and num not in quarantined
        ]
        oauth_candidates = [
            n for n in candidates if self.switcher.account_kind_for(n) != "api_key"
        ]
        consume_first = settings.strategy == "consume-first"
        if 100.0 - active_headroom < settings.threshold:
            if consume_first:
                trigger, decided_headroom = "consume-first", active_headroom
            else:
                trigger, decided_headroom = "proactive", 100.0 - settings.threshold
        else:
            trigger = "at-limit" if active_headroom <= 0 else "proactive"
            decided_headroom = active_headroom
        if trigger in ("proactive", "consume-first") and self._in_cooldown(state):
            return None  # the decision path holds here too
        if not oauth_candidates:
            ordered: list[str] = []
        else:
            ordered, _, _ = self._rank_with_bar(
                state,
                trigger=trigger,
                consume_first=consume_first,
                oauth_candidates=oauth_candidates,
                usage=usage,
                headroom=headroom,
                current=current,
                active_headroom=decided_headroom,
                settings=settings,
                now=self.clock(),
            )
        if not ordered and settings.include_api_key_accounts and trigger != "consume-first":
            # Same last resort as the decision: a metered API-key account.
            ordered = [
                n for n in candidates if self.switcher.account_kind_for(n) == "api_key"
            ]
        return ordered[0] if ordered else None

    def _rank_with_bar(
        self, state: dict, **kw
    ) -> tuple[list[str], bool, float | None]:
        """Rank with the no-return bar, and WITHOUT it if that empties AND
        the barred account is a different proposition from the one we left.

        Emptiness alone cannot be the release. On two accounts there is
        exactly one candidate, so barring it ALWAYS empties the list —
        measured, sweeping active x barred headroom x both reset shapes,
        `n=2 barred-rank EMPTY=320 NONEMPTY=0`. An emptiness-only release
        therefore fires every tick and the bar is inert at the fleet size
        the flap was reported on: pcts 92/92, resets 500h/400h, 60 ticks
        gave `[1, 2, 1, 2]` with the bar on and the identical `[1, 2, 1, 2]`
        with `lastSwitchFrom` popped every tick.

        "BARRING LEAVES NOTHING" AND "WE ARE FLAPPING" ARE DIFFERENT
        STATES, and at n=2 they are always the same state — which is how
        one swallowed the other. The ranking cannot separate them: it sees
        only the present, and both look like an empty list. What separates
        them is WHY the ranking flipped. Traced at each leg of that walk:

            t8   1->2   left 1 holding 4.0 pts, 500h out
            t20  2->1   account 1 still 4.0 pts, still 500h out
            t22  1->2   account 2 still 2.0 pts, still 400h out

        Every return won because the ACTIVE burned down, never because the
        target recovered. So the release asks the one question the ranking
        cannot: is the account we left better than when we left it?

        ON BOTH AXES THE RANKING USES, and with the margins it already
        uses — ``SPENT_HEADROOM_PCT`` of headroom (below that an edge is
        under two poll intervals of work) or ``RECOVERY_HYSTERESIS_S``
        sooner. An account's headroom rises only when a window rolls over
        and its binding reset only moves nearer when a nearer window
        starts binding, so both are real events rather than the boundary
        crossings burn manufactures for free.

        Emptiness still decides whether to ASK. Where the bar leaves a
        real alternative it simply applies, so a fleet with somewhere else
        to go is untouched by any of this.

        Cheap: the retry runs only when the barred list came back empty,
        which is the tick that was about to do nothing anyway.
        """
        # Recomputed per snapshot, never once per tick: the consume-first
        # two-phase commit replaces `headroom` and `active_headroom` and
        # re-ranks, and the ratio release consumes exactly those two
        # values. Computed once, the bar answered from a snapshot the
        # ranking had already thrown away — `left=20 active=30` bars,
        # `left=90 active=10` releases, and phase 2 is where that flips.
        recovered = self._left_account_recovered(
            state,
            kw["usage"],
            kw["headroom"],
            kw["active_headroom"],
            kw["settings"],
            kw["now"],
            kw["current"],
        )
        no_return = self._no_return_account(
            kw["trigger"],
            state,
            kw["headroom"],
            kw["active_headroom"],
            recovered,
            kw["settings"],
            kw["current"],
        )
        ranked = self._rank_candidates(no_return=no_return, **kw)
        if no_return is not None and not ranked[0] and recovered:
            unbarred = self._rank_candidates(no_return=None, **kw)
            if unbarred[0]:
                return unbarred
        return ranked

    def _rank_candidates(
        self,
        *,
        trigger: str,
        consume_first: bool,
        oauth_candidates: list[str],
        no_return: str | None,
        usage: dict[str, dict | str | None],
        headroom: dict[str, float | None],
        current: str,
        active_headroom: float | None,
        settings: AutoSwitchSettings,
        now: float,
    ) -> tuple[list[str], bool, float | None]:
        """Filter and rank OAuth candidates for this tick's trigger.

        Returns ``(ordered, any_known, active_reset_ts)``. Pure — no emits,
        no state writes — so the consume-first two-phase commit can run it
        twice per tick: on the stored snapshot to decide provisionally, then
        on the escalated refetch to re-verify before switching.
        """
        # consume-first ranks by soonest weekly reset; a proactive (below-
        # threshold) target must reset strictly sooner than where we are.
        active_reset_ts = (
            _seven_day_reset_ts(usage.get(current), now) if consume_first else None
        )
        # When NOTHING is below the threshold — the active account and every
        # candidate all in the 90s — "land somewhere healthy" has no answer,
        # and holding out for one costs the user the session. Sitting still
        # means burning the active account to 100% and taking a hard limit,
        # with the peer that resets in 8 minutes never tried. So in that state
        # the goal changes from "most headroom" to "soonest back": move to
        # whichever account recovers first and keep working through its reset.
        #
        # Deliberately narrow. It engages only when every measured OAuth
        # account is at/over the threshold, so a single healthy peer still
        # wins the normal way, and RECOVERY_HYSTERESIS_S below replaces the
        # percentage-point margin so two accounts in the 90s cannot ping-pong.
        all_above = _every_account_above_threshold(
            oauth_candidates, headroom, active_headroom, settings.threshold
        )
        # "Is anything worth having?" — the most headroom any candidate with a
        # READABLE row offers. Two exclusions and no others:
        #
        # Unknown headrooms are skipped rather than counted as zero. A row we
        # cannot read is not evidence of an empty account — measured, one
        # sentinel row (expired token, locked keychain) made `all(...)` False
        # forever and parked the engine on the account resetting LAST.
        #
        # Nothing else is filtered, INCLUDING the no-return bar. An earlier
        # version of this comment claimed it was "scoped to choosable
        # candidates"; the code below has never done that and the two
        # paragraphs contradicted each other. Leaving the barred account in is
        # deliberate: this answers whether the FLEET has quota, and the bar is
        # about which account to move to, not about what exists. A peer just
        # above SPENT_HEADROOM_PCT can therefore turn the spent check off while
        # being unchoosable itself — the band is (SPENT_HEADROOM_PCT, active x
        # RATIO], up to 3 points wide at the defaults, and the one-way fallback
        # below is what stops that band parking the engine. A ratio floor used
        # to sit here too and inverted monotonicity; removing it is what let
        # the fallback do the job.
        best_candidate_headroom = max(
            (h for h in map(headroom.get, oauth_candidates) if h is not None),
            default=0.0,
        )
        active_recovery_ts = (
            _binding_recovery_ts(usage.get(current), self._models, now)
            if all_above
            else 0.0  # unread unless all_above; never a live sentinel
        )

        qualifying: list[tuple[tuple, str]] = []
        fallback: list[tuple[tuple, str]] = []
        any_known = False
        for num in oauth_candidates:
            h = headroom.get(num)
            if h is None:
                continue
            any_known = True          # it EXISTS and is readable either way
            if h <= 0:
                continue  # itself at its limit — never a target
            if num == no_return:
                continue  # the account we just left; see _no_return_account
            reset_ts = (
                _seven_day_reset_ts(usage.get(num), now) if consume_first else None
            )
            recovery_ts = (
                _binding_recovery_ts(usage.get(num), self._models, now)
                if all_above
                else 0.0
            )
            if trigger in ("proactive", "consume-first"):
                # Landing must be healthy: an account at/over the threshold
                # would re-trigger on the very next tick. At-limit and failover
                # are escapes that skip this whole block — any account with real
                # headroom beats a blocked or dead one.
                if (100.0 - h) >= settings.threshold and not all_above:
                    continue
                if all_above:
                    # Checked before the strategies, because with nothing below
                    # the threshold the strategy question is moot: consume-first
                    # exists to spend perishable WEEKLY quota, and every account
                    # here is blocked on a window that returns in minutes. Both
                    # strategies want the same thing — the account that can work
                    # again first — so both take this gate and the matching key
                    # below. (Ordering matters: `if consume_first` catching
                    # first filtered on weekly ordering while the key sorted on
                    # binding recovery, two different axes, and left
                    # consume-first users with no anti-flap guard at all.)
                    #
                    # WHICH AXIS is decided per candidate, in one place — see
                    # _recovery_is_useful for the four holes that came from
                    # deciding it once, globally, from four scattered gates.
                    # Set and read under the same `all_above and trigger`
                    # condition, so it is always assigned before the key below.
                    by_recovery = _recovery_is_useful(
                        recovery_ts,
                        active_recovery_ts,
                        active_headroom or 0.0,
                        best_candidate_headroom,
                        now,
                    )
                    if by_recovery:
                        # Hysteresis on the axis we actually rank by. It bounds
                        # the flap RATE rather than making a reverse move
                        # impossible: the target must come back meaningfully
                        # sooner than where we are.
                        if recovery_ts >= active_recovery_ts - RECOVERY_HYSTERESIS_S:
                            continue
                    else:
                        # Headroom axis, with a RATIO margin. Also a rate bound,
                        # not impossibility — headroom moves, so a target that
                        # burns down to a quarter of what it beat can qualify
                        # in reverse. That takes a 4x relative burn instead of
                        # the one point a strictly-greater test would need.
                        if h < (active_headroom or 0.0) * HORIZON_HEADROOM_RATIO:
                            if (
                                (active_headroom or 0.0) <= SPENT_HEADROOM_PCT
                                and h >= (active_headroom or 0.0)
                                and recovery_ts
                                < active_recovery_ts - RECOVERY_HYSTERESIS_S
                            ):
                                fallback.append(((0, recovery_ts, -h), num))
                            continue
                elif consume_first:
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
                elif active_headroom is not None:
                    # best: the candidate must beat the active account by the
                    # full hysteresis margin (a one-way move like 99%→89%
                    # qualifies; near-line pairs can't flap back).
                    if h - active_headroom < settings.hysteresis_pct:
                        continue
            if all_above and trigger in ("proactive", "consume-first"):
                # Ranked on the axis its own gate decided, and TIERED so the two
                # stay comparable: a candidate returning inside the horizon
                # beats one that does not, whatever its headroom. Untiered, the
                # two key shapes were compared elementwise — a raw headroom
                # against an epoch timestamp — and headroom won on magnitude
                # alone. Falling through to the weekly key instead split the
                # filter and the sort across two axes, picking the candidate
                # with LESS headroom whenever its weekly reset was sooner.
                #
                # Scoped to the SAME triggers as the gate above: at-limit and
                # failover skip that gate deliberately, because there we are
                # escaping a dead account rather than optimising a return time.
                # `recovery_ts` in BOTH tiers. Tier 1 hard-coded 0.0 there,
                # which threw away a fact already in hand: two peers with equal
                # headroom past the horizon then tied, and the tie fell through
                # to sequence order. Measured — active 4 pts/300h, two peers
                # 8 pts each, one returning in 5h and one in 500h: base picks
                # the 5h account whichever slot it occupies, this branch picked
                # whichever came first in the list. Headroom still decides
                # first within the tier; the reset only breaks its ties, where
                # sooner is plainly better than lower slot number.
                key: tuple = (
                    (0, recovery_ts, -h) if by_recovery else (1, -h, recovery_ts)
                )
            elif consume_first:
                # Soonest weekly reset first (unknown resets sort last), most
                # headroom breaks ties, then sequence order.
                key = (reset_ts if reset_ts is not None else float("inf"), -h)
            else:
                key = (-h,)
            qualifying.append((key, num))
        # Ascending by the strategy's key; list order (sequence order) breaks ties.
        qualifying = qualifying or fallback
        qualifying.sort(key=lambda t: t[0])
        return [num for _, num in qualifying], any_known, active_reset_ts

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
        # login) is overridden past the active age cap. Exhausted accounts
        # carry their own bounded plan and become due normally.
        stale_candidate_plan = (
            active_pre is not None
            and active_pre.age_s is not None
            and active_pre.age_s >= poll_policy.ACTIVE_MAX_INTERVAL_S
            and (active_pre.poll_interval_s or 0.0)
            > poll_policy.ACTIVE_MAX_INTERVAL_S
            and (binding_pct(active_pre.last_good, self._models) or 0.0) < 100.0
        )
        overslept_plan = (
            active_pre is not None
            and plan_oversleeps_interval(active_pre, now)
        )
        if (
            active_pre is None
            or active_pre.age_s is None
            or stale_candidate_plan
            or overslept_plan
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
        entries = self.switcher.usage_entries_by_account(
            fetch=plan,
            # A candidate-style plan on the active slot is deliberately
            # overridden after the active age cap; every other baseline
            # nomination preserves a valid future plan under the store lock.
            scheduled=not stale_candidate_plan,
        )
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
            escalation_fetch = {current, *candidates}
            # Escalation may beat ordinary candidate plans to obtain a fresh
            # switch decision, but a decision-trusted exhausted row cannot be
            # a target. Preserve any wider post-429 plan instead of refetching
            # that token at the bounded all-exhausted wake cadence.
            for num in tuple(escalation_fetch):
                entry = entries.get(num)
                value = usage.get(num)
                planned_headroom = oauth.account_headroom(
                    value if isinstance(value, dict) else None, self._models
                )
                if (
                    entry is not None
                    and entry.next_poll_at is not None
                    and now < entry.next_poll_at
                    and (entry.poll_interval_s or 0.0)
                    > poll_policy.EXHAUSTED_INTERVAL_S
                    and planned_headroom is not None
                    and planned_headroom <= 0
                ):
                    escalation_fetch.remove(num)
            entries = self.switcher.usage_entries_by_account(
                fetch=escalation_fetch
            )
            usage = {num: entry.decision_value() for num, entry in entries.items()}

        headroom = _headroom_by_account(usage, self._models)
        return entries, usage, headroom

    def _perform(
        self,
        number: str,
        email: str,
        trigger: str,
        left: tuple[float | None, float],
    ) -> TickOutcome:
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
        #
        # Nothing is emitted while `_switch_lock` is held: the TUI delivers
        # events synchronously onto the thread that calls `stop()`, and
        # `stop()` waits for this lock — an emit in here would have the two
        # threads waiting on each other.
        held: AutoSwitchEvent | None = None
        with self._switch_lock, self._state_lock():
            if self._stop.is_set():
                # stop() landed while this tick was deciding: the caller no
                # longer wants switches, so the last one must not slip out.
                held = NoSwitchEvent(reason="stopped")
            else:
                state = self._read_state()
                if trigger in ("proactive", "consume-first") and self._in_cooldown(
                    state
                ):
                    held = NoSwitchEvent(reason="cooldown")
                else:
                    result = self.switcher.switch_to(number, json_output=True)
                    if not result or not result.get("switched"):
                        held = NoSwitchEvent(
                            reason="already-active",
                            detail=(result or {}).get("reason", ""),
                        )
                    else:
                        self._record_switch(state, number, trigger, result, left)
        if held is not None:
            self._emit(held)
            return TickOutcome.NO_ACTION

        self._emit(
            SwitchEvent(
                trigger=trigger,
                from_ref=result.get("from"),
                to_ref=result.get("to"),
                warnings=result.get("warnings", []),
            )
        )
        return TickOutcome.SWITCHED

    def _record_switch(
        self,
        state: dict,
        number: str,
        trigger: str,
        result: dict,
        left: tuple[float | None, float],
    ) -> None:
        """Persist a completed switch into the state file (caller holds the
        state lock)."""
        state["schemaVersion"] = STATE_SCHEMA_VERSION
        state["lastSwitchAt"] = self.clock()
        state["lastSwitchTo"] = number
        # WHERE we came from, so the next tick can refuse to undo this,
        # and WHAT IT LOOKED LIKE, so that refusal has a release that burn
        # cannot fake. See `_left_account_recovered` for why the present
        # state alone cannot supply one. `inf` is stored as null: it is not
        # portable JSON, and every other reader of this file would have to
        # learn about it.
        state["lastSwitchFrom"] = (result.get("from") or {}).get("number")
        state["leftHeadroom"], recovery = left
        state["leftRecoveryAt"] = None if recovery == float("inf") else recovery
        # A `consume-first` phase-2 refetch can write the SAME (None,
        # None) shape a `failover` departure writes, whenever the
        # refetched active row has a `pct` but is otherwise unmeasurable
        # in the same tick its weekly reset is known --
        # `account_headroom` needs a numeric `pct`, `_seven_day_reset_ts`
        # needs only `resets_at`. Inferring the trigger from the two
        # nulls then runs the wrong legs. Record it directly so the
        # reader never has to guess.
        state["leftTrigger"] = trigger
        atomic_write_json(self.state_path, state)

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
        now = self.clock()
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
            if usable_at is None or usable_at <= now:
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
        exits immediately (engines are single-use).

        Called from another thread, returns only once no switch is in
        flight: a tick that has not yet reached its switch transaction will
        refuse it, and one already inside it is allowed to finish first, so
        after this returns the active account no longer changes. Called from
        the tick thread itself (the SIGTERM handler in ``cswap auto``) it
        returns at once and the in-flight switch, if any, completes."""
        self._stop.set()
        self._wake.set()
        with self._switch_lock:
            pass

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
        return self._respect_poll_plan(interval * (0.9 + 0.2 * random.random()))

    def _respect_poll_plan(self, delay: float) -> float:
        """Shorten a normal-cadence sleep to the store's own next-poll time.

        The planner tightens the active row to URGENT_INTERVAL_S while it
        burns toward the threshold, but the loop always slept
        ``interval_seconds`` — so the plan ran late. Measured mid-episode: the
        row was due 112s ago while the engine still had minutes of sleep left.

        Only ever shortens, never below the planner's floor: the 429 budget
        lives in the plan, and this makes the loop obey it rather than
        override it. Best-effort — the unshortened delay is always safe.
        """
        try:
            current = self.switcher.current_account_number()
            if current is None:
                return delay
            entry = self.switcher.usage_entries_by_account(fetch=set()).get(current)
            if entry is None or entry.next_poll_at is None:
                return delay
            due_in = entry.next_poll_at - self.clock()
            # Clamp the DEADLINE, not the result. max(min(delay, due_in), U)
            # raises a delay that was ALREADY below U: at the configurable
            # floor of 15s it turns a 13.5s jittered sleep into 60s, and at
            # the 60s default it flattens the entire lower jitter half.
            # Bounding due_in instead keeps "only ever shortens" true at every
            # configured interval, and still refuses to poll faster than the
            # planner's own floor when the row is overdue.
            return min(delay, max(due_in, poll_policy.URGENT_INTERVAL_S))
        except Exception:
            return delay

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
