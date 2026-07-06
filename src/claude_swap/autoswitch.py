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
import hashlib
import json
import logging
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import ClassVar

from claude_swap import oauth
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.json_output import SCHEMA_VERSION, USAGE_TOKEN_EXPIRED
from claude_swap.locking import FileLock
from claude_swap.settings import AutoSwitchSettings, atomic_write_json
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.usage_store import due_candidate

STATE_FILENAME = "autoswitch_state.json"
STATE_SCHEMA_VERSION = 1

_logger = logging.getLogger("claude-swap")

# Freshen targets whose access token expires within this window: twice Claude
# Code's own 5-minute refresh buffer, so its post-lock "abort refresh if not
# expired" re-read holds with margin after our swap.
FRESHEN_BUFFER_MS = 10 * 60 * 1000

# Sleep caps around a known quota reset: a little slack past the reset, and
# never trust one long sleep (laptops suspend, clocks drift) — cap and
# re-evaluate.
RESET_SLACK_S = 60.0
MAX_SLEEP_S = 6 * 3600.0
NO_RESET_FALLBACK_S = 300.0

# Idle-hold cap (elapsed, not ticks — the hold itself slows the cadence to
# NO_RESET_FALLBACK_S): an owned-and-expired token normally means Claude Code
# is idle and will self-heal on next use, but a *dead* refresh token with an
# active user would look identical forever, so after this long the engine
# falls back to normal unhealthy counting.
IDLE_HOLD_MAX_S = 30 * 60.0

# Adaptive scheduler: the baseline request volume is O(1) per tick — the
# active account plus ONE due candidate (stalest data first) — instead of
# every account in parallel. Candidates far from mattering are served stale
# from the usage store. The engine escalates to a full refresh only when a
# switch could actually be near: active utilization within this margin of the
# threshold, or active usage unknown (failover needs fresh candidate data).
ESCALATION_MARGIN_PCT = 15.0
# A candidate whose binding pct moved at least this much between polls is
# being used elsewhere (another PC / session mode) → poll it more closely;
# an unmoved one backs off, up to the cap.
MOVEMENT_DELTA_PCT = 1.0
CANDIDATE_MAX_INTERVAL_S = 600.0
# The active account's cadence relaxes with distance (in utilization points)
# to the escalation band, because burn rate is absolute: >50 pts below the
# band → this hard cap (never slower); >25 pts → 2× the interval; else every
# tick. Distance-based rather than absolute pct so an adjusted threshold
# keeps the same safety margin (at threshold 50 the cap is simply never
# reachable). The cap stays under STALE_OK_S so a relaxed active never loses
# decision trust, and the worst plausible burn (~5%/min, heavy subagent use)
# moves +15 pts between capped polls — still ≥35 pts from the band, and band
# entry is seen at most one poll late, absorbed by ESCALATION_MARGIN_PCT +
# hysteresis.
ACTIVE_MAX_INTERVAL_S = 180.0
ACTIVE_RELAX_DISTANCE_PCT = 25.0  # 2× interval beyond this; cap beyond 2× this


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


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

    def _fields(self) -> dict:
        fields = {
            "active": self.active,
            "headroomPct": self.headroom,
            "threshold": self.threshold,
        }
        if self.fetch_errors:
            fields["fetchErrors"] = self.fetch_errors
        return fields

    def _describe(self, num: str) -> str:
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
            f"(switch at {self.threshold:.0f}%){tail}"
        )


@dataclass(frozen=True)
class SwitchEvent(AutoSwitchEvent):
    kind: ClassVar[str] = "switch"
    trigger: str  # "proactive" | "at-limit" | "failover"
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


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class TickOutcome(enum.Enum):
    """Outcome of one evaluation tick; values double as --once exit codes."""

    SWITCHED = 0
    ERROR = 1
    NO_ACTION = 2
    BLOCKED = 3  # wanted to switch but no viable target / all exhausted


def _refresh_fingerprint(credentials: str) -> str | None:
    data = oauth.extract_oauth_data(credentials)
    token = data.get("refreshToken") if data else None
    if not isinstance(token, str) or not token:
        return None
    return "sha256:" + hashlib.sha256(token.encode()).hexdigest()


def binding_pct(usage: dict | None) -> float | None:
    """Utilization of the binding (higher) 5h/7d window, or None."""
    headroom = oauth.account_headroom(usage)
    return None if headroom is None else 100.0 - headroom


def _limiting_reset_ts(usage: dict | None) -> float | None:
    """Epoch when the last of the ≥100% windows resets (account usable again)."""
    if not isinstance(usage, dict):
        return None
    latest: float | None = None
    for key in ("five_hour", "seven_day"):
        window = usage.get(key)
        if not isinstance(window, dict):
            continue
        pct = window.get("pct")
        if not isinstance(pct, (int, float)) or pct < 100.0:
            continue
        ts = _window_reset_ts(window)
        if ts is not None and (latest is None or ts > latest):
            latest = ts
    return latest


def _earliest_future_reset_ts(usage: dict | None, now: float) -> float | None:
    """Epoch of the next window reset still ahead of ``now``, any utilization."""
    if not isinstance(usage, dict):
        return None
    earliest: float | None = None
    for key in ("five_hour", "seven_day"):
        window = usage.get(key)
        if not isinstance(window, dict):
            continue
        ts = _window_reset_ts(window)
        if ts is not None and ts > now and (earliest is None or ts < earliest):
            earliest = ts
    return earliest


def _window_reset_ts(window: dict) -> float | None:
    resets_at = window.get("resets_at")
    if not resets_at:
        return None
    try:
        return datetime.fromisoformat(
            str(resets_at).replace("Z", "+00:00")
        ).timestamp()
    except ValueError:
        return None


def _ref(number: str, email: str) -> dict:
    return {"number": int(number), "email": email}


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
        self.on_event = on_event
        self.dry_run = dry_run
        self.state_path = state_path or (switcher.backup_dir / STATE_FILENAME)
        self.clock = clock
        self._stop = threading.Event()
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
        ``"transient"`` (network trouble — try again next tick) or
        ``"skip-live-session"``. Only ever touches the slot's *backup* store;
        the active credential belongs to Claude Code.
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
            self.switcher.persist_backup_credentials(
                number, email, outcome.credentials
            )
            return "ok"
        if outcome.error in ("invalid_grant", "no_refresh_token"):
            return "invalid_grant"
        return "transient"

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

        entries, usage, headroom = self._collect_scheduled_usage(current, quarantined)
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
            )
        )

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
                self._emit(
                    NoSwitchEvent(
                        reason="below-threshold",
                        detail=f"{utilization:.0f}% < {settings.threshold:.0f}%",
                    )
                )
                return TickOutcome.NO_ACTION
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

        if trigger == "proactive" and self._in_cooldown(state):
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
        if not oauth_candidates and not api_key_candidates:
            # Won't change until the user adds/recovers an account — no point
            # re-polling at full cadence.
            self._blocked_wait_long = True
            self._emit(NoSwitchEvent(reason="no-candidates"))
            return TickOutcome.BLOCKED

        hysteresis_bar = settings.threshold - settings.hysteresis_pct
        qualifying: list[tuple[float, str]] = []
        any_known = False
        for num in oauth_candidates:
            h = headroom.get(num)
            if h is None:
                continue
            any_known = True
            if h <= 0:
                continue  # itself at its limit — never a target
            if trigger == "proactive":
                # Hysteresis guards only the proactive case: two accounts
                # hovering at the line must not ping-pong. At-limit and
                # failover are escapes — any account with real headroom
                # beats a blocked or dead one (and you can't flap back onto
                # an account at 100%).
                if (100.0 - h) > hysteresis_bar:
                    continue
                if active_headroom is not None and h <= active_headroom:
                    continue  # not provably better than where we are
            qualifying.append((h, num))
        # Best headroom first; list order (sequence order) breaks ties.
        qualifying.sort(key=lambda t: -t[0])
        ordered = [num for _, num in qualifying]
        if not ordered and api_key_candidates:
            # Last resort: metered API-key accounts (unmeasurable headroom).
            ordered = api_key_candidates

        if not ordered:
            if not any_known:
                self._emit(
                    NoSwitchEvent(
                        reason="no-comparison",
                        detail="no candidate has readable usage",
                    )
                )
                return TickOutcome.BLOCKED
            # "All exhausted" (and its hours-long reset sleep) only when it's
            # literally true: every candidate's usage is known and at its
            # limit. A candidate that merely failed the proactive hysteresis
            # bar, or one whose usage is unreadable this tick, can become
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
                            "candidates are too close to the line or their "
                            "usage is unreadable this tick"
                        ),
                    )
                )
                return TickOutcome.BLOCKED
            self._blocked_wait_long = True
            earliest = self._earliest_reset(usage)
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
            if self.dry_run:
                # Dry-run stops at the decision: no token refresh, no
                # quarantine writes — freshening is a mutation.
                return self._perform(num, email, trigger)
            status = self._freshen_target(num, email)
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

    # -- adaptive usage scheduling ---------------------------------------------

    def _collect_scheduled_usage(
        self, current: str, quarantined: set[str] = frozenset()
    ) -> tuple[dict, dict[str, dict | str | None], dict[str, float | None]]:
        """Two-phase usage collection with an O(1) baseline.

        Phase A fetches the active account (at its distance-to-threshold
        cadence — every tick only near the band, see
        :meth:`_active_poll_interval_s`) plus ONE due candidate (the one
        with the stalest data — never-fetched first, then oldest fetch);
        everyone else is served from the usage store. Phase B refetches ALL
        candidates and recomputes before any switch decision when a switch
        could be near: active utilization within ``ESCALATION_MARGIN_PCT`` of
        the threshold, or active usage unknown (failover must not run on
        stale candidate data). Candidate selection never runs on the
        pre-escalation snapshot.

        Stalest-first needs no rotation cursor: it reads the persisted store,
        so the loop and cron-driven ``--once`` runs schedule identically.
        Backoff (``backoffUntil``) is enforced by the collector even for the
        active account — a Retry-After must never be defeated — and during an
        idle-hold no candidate is polled at all (slow crawl for everything).

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
        if (
            active_pre is None
            or active_pre.age_s is None
            or active_pre.age_s >= self._active_poll_interval_s(active_pre)
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
            active_value if isinstance(active_value, dict) else None
        )
        escalate = bool(candidates) and (
            (active_headroom is None and active_value != USAGE_TOKEN_EXPIRED)
            or (
                active_headroom is not None
                and 100.0 - active_headroom
                >= self.settings.threshold - ESCALATION_MARGIN_PCT
            )
        )
        if escalate:
            entries = self.switcher.usage_entries_by_account(
                fetch={current, *candidates}
            )
            usage = {num: entry.decision_value() for num, entry in entries.items()}

        headroom = {
            num: oauth.account_headroom(value if isinstance(value, dict) else None)
            for num, value in usage.items()
        }
        if not self.dry_run:
            self._update_poll_plans(candidates, pre, entries, now)
        return entries, usage, headroom

    def _active_poll_interval_s(self, entry) -> float:
        """Stepwise active cadence by distance to the escalation band.

        Unknown utilization or in-band → every tick (the at-limit escape and
        failover paths must never wait); more than ``ACTIVE_RELAX_DISTANCE_PCT``
        points below the band → 2× the engine interval; more than twice that
        → ``ACTIVE_MAX_INTERVAL_S``. (At the default threshold 90 these are
        the <50% / <25% utilization steps.) Derived per tick from the stored
        entry — no persisted state, and failure backoff / idle-hold /
        escalation are enforced elsewhere and unaffected.
        """
        interval = float(self.settings.interval_seconds)
        pct = binding_pct(entry.last_good)
        if pct is None:
            return interval
        distance = (self.settings.threshold - ESCALATION_MARGIN_PCT) - pct
        if distance > 2 * ACTIVE_RELAX_DISTANCE_PCT:
            tier = ACTIVE_MAX_INTERVAL_S
        elif distance > ACTIVE_RELAX_DISTANCE_PCT:
            tier = 2.0 * interval
        else:
            return interval
        return min(tier, ACTIVE_MAX_INTERVAL_S)

    def _update_poll_plans(
        self, candidates: list[str], pre: dict, post: dict, now: float
    ) -> None:
        """Adapt each just-fetched candidate's poll cadence, persisted in the
        store (survives ``--once`` engine restarts).

        Movement (binding pct changed ≥ ``MOVEMENT_DELTA_PCT`` since its
        previous poll — someone is using it elsewhere) halves the interval,
        floored at the engine interval; no movement backs it off ×1.5 up to
        ``CANDIDATE_MAX_INTERVAL_S``. A poll is never scheduled later than the
        candidate's next window reset (stored usage is obsolete the moment a
        window rolls over — a 95% candidate is really ~0% after its reset).
        A candidate at its limit skips straight to its window reset
        (``nextPollAt`` only — the learned interval is kept for when it
        comes back).
        """
        plans: dict[str, tuple[float | None, float | None]] = {}
        for num in candidates:
            before, after = pre.get(num), post.get(num)
            if before is None or after is None or after.sentinel is not None:
                continue
            if after.fetched_at is None or after.fetched_at == before.fetched_at:
                continue  # not fetched this pass
            base = before.poll_interval_s or self.settings.interval_seconds
            prev_pct = binding_pct(before.last_good)
            new_pct = binding_pct(after.last_good)
            if prev_pct is None or new_pct is None:
                interval = self.settings.interval_seconds
            elif abs(new_pct - prev_pct) >= MOVEMENT_DELTA_PCT:
                interval = max(self.settings.interval_seconds, base / 2)
            else:
                interval = min(CANDIDATE_MAX_INTERVAL_S, base * 1.5)
            next_poll = now + interval
            headroom = oauth.account_headroom(after.last_good)
            if headroom is not None and headroom <= 0:
                reset_ts = _limiting_reset_ts(after.last_good)
                if reset_ts is not None and reset_ts > next_poll:
                    next_poll = reset_ts
            else:
                reset_ts = _earliest_future_reset_ts(after.last_good, now)
                if reset_ts is not None:
                    next_poll = min(next_poll, reset_ts + RESET_SLACK_S)
            plans[num] = (next_poll, interval)
        if plans:
            self.switcher.set_usage_poll_plan(plans)

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
            if trigger == "proactive" and self._in_cooldown(state):
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

    @staticmethod
    def _earliest_reset(usage: dict[str, dict | str | None]) -> datetime | None:
        """Earliest known window reset across all accounts (UTC)."""
        earliest: datetime | None = None
        for entry in usage.values():
            if not isinstance(entry, dict):
                continue
            for window in ("five_hour", "seven_day"):
                resets_at = (entry.get(window) or {}).get("resets_at")
                if not resets_at:
                    continue
                try:
                    when = datetime.fromisoformat(str(resets_at).replace("Z", "+00:00"))
                except ValueError:
                    continue
                if earliest is None or when < earliest:
                    earliest = when
        return earliest

    def _emit(self, event: AutoSwitchEvent) -> None:
        self.on_event(event)

    # -- loop -------------------------------------------------------------------

    def stop(self) -> None:
        """Ask ``run_loop`` to exit; wakes it from any sleep."""
        self._stop.set()

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
        self._stop.clear()
        while not self._stop.is_set():
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
            self._stop.wait(delay)
        return 0
