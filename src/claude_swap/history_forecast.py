"""Weekly room left across every account, and where it is heading.

The unit throughout is *accounts' worth*: 1.0 is one account's whole 7-day
limit, so fifteen accounts at 40% used have 9.0 left. Pure functions over a
:class:`~claude_swap.history.History`; nothing here reads a file or a clock.

**Room now** counts an account while its latest reading is fresh (no older
than ``GAP_S``: a healthy account gets one at least every ``HEARTBEAT_S``,
so past that cswap cannot check it) and its weekly window has not reset
since (after the reset the figure no longer means anything). Removed and
disabled accounts are left out: cswap auto never rotates onto them, so their
room is not room the fleet can use. So are accounts with no 7-day limit (API
keys): they have no weekly room to add.

**Room in the past** applies the same rule to the recorded rows at each
moment, and draws nothing where fewer than half the accounts were reporting
(cswap itself was not running, so a total would mislead).

**Paces.** A pace is the 7-day limit all accounts used over a recent stretch
(the last 30 minutes, 6 hours, 24 hours), per second. The stretch ends at
cswap's latest check of the active account, since use after it has not been
seen yet. Each rise in an account's reading is spread evenly from the check
before it (``prev``) to the reading, so a reading that lands late after a gap
in checks does not pile the whole gap's use into the last 30 minutes.

**Forecast.** :func:`simulate_room` steps each counted account forward at a
pace, moving between accounts the way cswap auto's strategy does, with every
weekly limit dropping back to 0% at its reset and again every 7 days after.
It leaves out the 5-hour limit (which mostly changes *which* account the use
lands on, not how much is used) and the per-model weekly limits.

**Refill.** With no more use, room only comes back: each counted account's
used share returns at its weekly reset.

**Sustainable pace** is counted accounts / 7 a day: each limit comes back
once a week, so that is the most the limits keep up with in the long run.
Use faster than it runs out sooner or later; use below it can still run low
for a while when room is short now. Turned around, a pace needs pace x 7
accounts to keep up with it: :func:`extra_accounts` is how many more than
are counted now.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Sequence
from dataclasses import dataclass, field

from claude_swap.history import GAP_S, WEEK_S, History, Reading

HOUR_S = 3600.0
DAY_S = 86400.0
FORECAST_AHEAD_S = WEEK_S  # every weekly limit resets once in view
# Bound on simulate_room's steps. A step ends at a reset or when an account
# fills, so a week of 50 accounts is a few hundred; the bound only stops a
# pathological input (a rate of 1e6) from spinning.
SIMULATION_GUARD = 10_000

# ---------------------------------------------------------------------------
# forecast core: pure, unit-tested functions


Rise = tuple[float, int, float, float]  # (t, account, points, from)


def spread_used(rises: Sequence[Rise], a: float, b: float) -> float:
    """7-day limit used in ``(a, b]``, in accounts' worth.

    ``rises`` are ``(t, account, points, from)``: an account's 7-day figure
    rose by ``points`` between cswap's check of it at ``from`` and the next
    one, at ``t``. The rise is spread evenly over that stretch. Rows recorded
    without a previous check have ``from == t`` and count at the reading.
    """
    total = 0.0
    for t, _n, pts, start in rises:
        if start >= t:
            if a < t <= b:
                total += pts
            continue
        lo, hi = max(a, start), min(b, t)
        if hi > lo:
            total += pts * (hi - lo) / (t - start)
    return total / 100.0


@dataclass
class SimAccount:
    """An account as :func:`simulate_room` steps it: ``u`` = % of its week
    used, ``r`` = its next weekly reset (``math.inf`` when unknown)."""

    n: int
    u: float
    r: float
    priority: int = 0


@dataclass
class Simulation:
    """Room over time as ``(t, room)`` points (two at one ``t`` where a reset
    steps it up), when it first runs out (None if it doesn't), and its low."""

    path: list[tuple[float, float]]
    out: float | None
    low: tuple[float, float]  # (t, room)


def simulate_room(
    accounts: Sequence[SimAccount],
    active: int | None,
    rate: float,
    now: float,
    until: float,
    strategy: str = "best",
) -> Simulation:
    """Step the accounts' weeks from ``now`` to ``until`` at ``rate``
    (accounts' worth a second).

    ``strategy`` is cswap auto's: ``best`` (its default) keeps use on the
    active account until its week is used up, then moves to the one with the
    most room; ``consume-first`` always uses the account with room whose week
    resets soonest (most room breaks ties), so quota about to come back anyway
    goes first; ``priority-consume-first`` does the same within the highest
    priority tier that has room. The caller's accounts are not modified.
    """
    accts = [
        SimAccount(a.n, min(100.0, max(0.0, a.u)), a.r, a.priority) for a in accounts
    ]

    def room() -> float:
        return sum(100.0 - a.u for a in accts) / 100.0

    def most() -> SimAccount | None:
        free = [a for a in accts if a.u < 100]
        return min(free, key=lambda a: (a.u, a.r, a.n), default=None)

    def soonest() -> SimAccount | None:
        free = [a for a in accts if a.u < 100]
        if strategy == "priority-consume-first":
            return min(free, key=lambda a: (-a.priority, a.r, a.u, a.n), default=None)
        return min(free, key=lambda a: (a.r, a.u, a.n), default=None)

    consume = strategy in ("consume-first", "priority-consume-first")
    pick = soonest if consume else most
    per = rate * 100.0  # points of one weekly limit a second
    t = now
    if consume:
        cur = soonest()
    else:
        cur = next((a for a in accts if a.n == active and a.u < 100), None) or most()
    out: float | None = None if cur else now
    low = (now, room())
    path = [(now, low[1])]
    guard = 0
    while t < until and guard < SIMULATION_GUARD:
        guard += 1
        reset = min((a.r for a in accts), default=math.inf)
        full = t + (100.0 - cur.u) / per if cur and per > 0 else math.inf
        t2 = min(until, reset, full)
        if cur and per > 0:
            cur.u = 100.0 if t2 == full else min(100.0, cur.u + per * (t2 - t))
        t = t2
        v = room()
        path.append((t, v))
        if v < low[1]:
            low = (t, v)
        freed = False
        for a in accts:
            if a.r <= t:
                a.u = 0.0
                while a.r <= t:
                    a.r += WEEK_S
                freed = True
        if freed:
            path.append((t, room()))
        if cur and cur.u >= 100:
            cur = None
        if freed and consume:
            cur = None  # the week that just reset went to the back of the queue
        if cur is None:
            cur = pick()
            if cur is None and out is None:
                out = t
    return Simulation(path=path, out=out, low=low)


def path_at(path: Sequence[tuple[float, float]], t: float) -> float:
    """The room at time ``t`` on a :func:`simulate_room` path (after the step,
    at a reset)."""
    k = 0
    while k + 1 < len(path) and path[k + 1][0] <= t:
        k += 1
    a = path[k]
    b = path[k + 1] if k + 1 < len(path) else None
    if b is None or b[0] == a[0]:
        return a[1]
    return a[1] + (b[1] - a[1]) * (t - a[0]) / (b[0] - a[0])


def extra_accounts(per_day: float, have: int) -> int:
    """How many more accounts it would take to keep up with ``per_day``
    (accounts' worth a day) in the long run: the pace times 7 days, less the
    accounts there already, rounded up. A hair over a whole number needs one
    more; float noise on an exact one does not."""
    need = per_day * 7 - have
    return math.ceil(need - 1e-9) if need > 1e-9 else 0


# ---------------------------------------------------------------------------
# the analysis a view draws


@dataclass(frozen=True)
class Pace:
    key: str  # "30m" | "6h" | "24h"
    span: float  # seconds
    name: str


PACES = (
    Pace("30m", 30 * 60.0, "Last 30 minutes"),
    Pace("6h", 6 * HOUR_S, "Last 6 hours"),
    Pace("24h", DAY_S, "Last 24 hours"),
)


@dataclass
class Forecast:
    """One pace, and the room ahead if use carries on at it.

    ``reason`` says why it is unavailable (``ok`` False):
    ``no-room-now`` (no current readings to start from), ``no-active``
    (no reading of the active account yet), ``stale-active`` (none since
    ``since``) or ``warming-up`` (history needs ``needs`` more seconds).
    """

    pace: Pace
    ok: bool
    reason: str | None = None
    since: float | None = None
    needs: float | None = None
    used: float = 0.0  # accounts' worth used in the stretch
    rate: float = 0.0  # accounts' worth a second
    sim: Simulation | None = None

    @property
    def per_day(self) -> float:
        return self.rate * DAY_S


@dataclass(frozen=True)
class ResetEvent:
    t: float
    number: int
    frees: float  # accounts' worth that comes back


@dataclass(frozen=True)
class RoomNow:
    room: float | None  # None when no account is counted
    counted: int


@dataclass(frozen=True)
class AccountNow:
    """One managed account's figures now, as the per-account list shows them."""

    number: int
    active: bool
    disabled: bool
    weekly: bool  # False: no 7-day limit (an API key), so nothing to count
    counted: bool
    d7: float | None
    h5: float | None
    model: float | None
    d7_reset: float | None
    h5_reset: float | None
    fetched_at: float | None
    stale_reset: bool  # its reading is from before its week reset (counted as 0% if fresh)
    error: str | None


@dataclass
class Analysis:
    history: History
    fleet: list[int]
    room_now: RoomNow
    resets: list[ResetEvent]
    forecasts: list[Forecast]
    rises: list[Rise]
    accounts_now: list[AccountNow] = field(default_factory=list)

    @property
    def now(self) -> float:
        return self.history.now

    @property
    def fleet_size(self) -> int:
        return len(self.fleet)

    @property
    def scale(self) -> float:
        """The top of the room axis: the whole fleet's worth (at least 1)."""
        return float(max(1, len(self.fleet)))

    @property
    def sustainable_per_day(self) -> float | None:
        k = self.room_now.counted
        return k / 7.0 if k else None

    @property
    def forecast_end(self) -> float:
        return self.now + FORECAST_AHEAD_S

    def refill_at(self, t: float) -> float | None:
        """The room at a future ``t`` with no more use: today's, plus every
        reset by then."""
        if self.room_now.room is None:
            return None
        back = sum(e.frees for e in self.resets if e.t <= t)
        return min(self.scale, self.room_now.room + back)


def _outdated(value: float | None, reset: float | None, now: float) -> bool:
    return value is not None and reset is not None and reset <= now


def _live(value: float | None, reset: float | None, now: float) -> float | None:
    """A figure taken before its window reset no longer says how much of the
    limit is used."""
    return None if _outdated(value, reset, now) else value


def _week_now(value: float | None, reset: float | None, now: float) -> tuple[float | None, float | None]:
    """A weekly figure and its next reset, as they stand ``now``. Once the
    window has reset, the week is known to have started over: it counts as
    0% used (as :func:`_used_at` does for the past line), and its next reset
    is a week on."""
    if not _outdated(value, reset, now):
        return value, reset
    assert reset is not None
    while reset <= now:
        reset += WEEK_S
    return 0.0, reset


def fleet_of(history: History) -> list[int]:
    """The accounts the totals cover: managed now, not disabled, and with a
    weekly limit."""
    return [
        a.number for a in history.accounts
        if not a.removed and not a.disabled and a.weekly
    ]


def _fresh_reading(rows: list[Reading], times: list[float], t: float) -> Reading | None:
    k = bisect.bisect_right(times, t) - 1
    if k < 0 or t - rows[k].t > GAP_S:
        return None
    return rows[k]


def _used_at(r: Reading | None, t: float) -> float | None:
    """A recorded 7-day figure as it stood at ``t``. Once its window has reset
    the week is known to have started over, so it counts as 0% used from the
    reset until the next reading says otherwise: the line steps up at the
    reset instead of dipping while the next check is minutes away."""
    if r is None or r.d7 is None:
        return None
    return 0.0 if _outdated(r.d7, r.d7_reset, t) else r.d7


def room_at(history: History, fleet: Sequence[int], t: float) -> float | None:
    """Room left across ``fleet`` at a past ``t``, or None where fewer than
    half the accounts were reporting."""
    room, k = 0.0, 0
    for n in fleet:
        rows = history.readings.get(n) or []
        if not rows:
            continue
        r = _fresh_reading(rows, [x.t for x in rows], t)
        used = _used_at(r, t)
        if used is None:
            continue
        room += max(0.0, 100.0 - used) / 100.0
        k += 1
    return room if k > 0 and k >= len(fleet) / 2 else None


def room_series(
    history: History, fleet: Sequence[int], t0: float, t1: float, samples: int
) -> list[tuple[float, float | None]]:
    """``room_at`` at ``samples`` evenly spaced times from ``t0`` to ``t1``
    (both ends included), without re-scanning every account's rows per
    sample."""
    samples = max(2, samples)
    times = [t0 + (t1 - t0) * i / (samples - 1) for i in range(samples)]
    cols = {n: (history.readings.get(n) or []) for n in fleet}
    keys = {n: [r.t for r in rows] for n, rows in cols.items()}
    out = []
    for t in times:
        room, k = 0.0, 0
        for n in fleet:
            rows = cols[n]
            if not rows:
                continue
            used = _used_at(_fresh_reading(rows, keys[n], t), t)
            if used is None:
                continue
            room += max(0.0, 100.0 - used) / 100.0
            k += 1
        out.append((t, room if k > 0 and k >= len(fleet) / 2 else None))
    return out


def rises_of(history: History, fleet: Sequence[int]) -> list[Rise]:
    """Every rise in a fleet account's 7-day figure, oldest first."""
    out: list[Rise] = []
    for n in fleet:
        prev_v: float | None = None
        prev_t: float | None = None
        for r in history.readings.get(n) or []:
            if r.d7 is None:
                continue
            if prev_v is not None and prev_t is not None and r.d7 > prev_v:
                start = max(prev_t, r.prev) if r.prev is not None and r.prev < r.t else r.t
                out.append((r.t, n, r.d7 - prev_v, start))
            prev_v, prev_t = r.d7, r.t
    out.sort(key=lambda x: x[0])
    return out


def _counted_now(history: History, fleet: Sequence[int]) -> list[SimAccount]:
    now = history.now
    out = []
    for n in fleet:
        c = history.current.get(n)
        if c is None or c.t is None or now - c.t > GAP_S:
            continue
        v, reset = _week_now(c.d7, c.d7_reset, now)
        if v is None:
            continue
        acct = history.account(n)
        r = reset if reset is not None and reset > now else math.inf
        out.append(SimAccount(n, v, r, acct.priority if acct else 0))
    return out


def _resets_ahead(history: History, counted: Sequence[SimAccount], until: float) -> list[ResetEvent]:
    out = [
        ResetEvent(a.r, a.n, a.u / 100.0)
        for a in counted
        if a.u > 0 and history.now < a.r <= until
    ]
    return sorted(out, key=lambda e: (e.t, e.number))


def _pace(history: History, rises: Sequence[Rise], counted_room: float | None, pace: Pace) -> Forecast:
    if counted_room is None:
        return Forecast(pace, ok=False, reason="no-room-now")
    now = history.now
    act = history.current.get(history.active) if history.active is not None else None
    end = min(now, act.t) if act is not None and act.t is not None else None
    if end is None:
        return Forecast(pace, ok=False, reason="no-active")
    if now - end > min(30 * 60.0, pace.span / 2):
        return Forecast(pace, ok=False, reason="stale-active", since=end)
    start = end - pace.span
    since = history.recording_since
    if since is None or since > start + max(120.0, pace.span * 0.02):
        needs = pace.span if since is None else since - start
        return Forecast(pace, ok=False, reason="warming-up", needs=needs)
    used = spread_used(rises, start, end)
    return Forecast(pace, ok=True, used=used, rate=used / pace.span)


def analyse(history: History) -> Analysis:
    """Room now, the reset schedule, and a forecast at each pace."""
    fleet = fleet_of(history)
    counted = _counted_now(history, fleet)
    room = RoomNow(
        room=sum(100.0 - a.u for a in counted) / 100.0 if counted else None,
        counted=len(counted),
    )
    rises = rises_of(history, fleet)
    end = history.now + FORECAST_AHEAD_S
    forecasts = []
    for pace in PACES:
        f = _pace(history, rises, room.room, pace)
        if f.ok:
            f.sim = simulate_room(
                counted, history.active, f.rate, history.now, end, history.strategy
            )
        forecasts.append(f)
    analysis = Analysis(
        history=history,
        fleet=fleet,
        room_now=room,
        resets=_resets_ahead(history, counted, end),
        forecasts=forecasts,
        rises=rises,
    )
    analysis.accounts_now = _accounts_now(history, {a.n for a in counted})
    return analysis


def _accounts_now(history: History, counted: set[int]) -> list[AccountNow]:
    """Every managed account, most room first; uncounted ones last."""
    now = history.now
    rows = []
    for acct in history.accounts:
        if acct.removed:
            continue
        c = history.current.get(acct.number)
        fresh = c is not None and c.t is not None and now - c.t <= GAP_S
        d7, d7_reset = _week_now(c.d7, c.d7_reset, now) if c else (None, None)
        model, _ = _week_now(c.model, c.model_reset, now) if c else (None, None)
        rows.append(AccountNow(
            number=acct.number,
            active=acct.number == history.active,
            disabled=acct.disabled,
            weekly=acct.weekly,
            counted=acct.number in counted,
            d7=d7 if fresh else None,
            h5=_live(c.h5, c.h5_reset, now) if fresh and c else None,
            model=model if fresh else None,
            d7_reset=d7_reset,
            h5_reset=c.h5_reset if c else None,
            fetched_at=c.t if c else None,
            stale_reset=bool(c and _outdated(c.d7, c.d7_reset, now)),
            error=c.error if c else None,
        ))
    rows.sort(key=lambda r: (r.d7 is None, r.d7 if r.d7 is not None else 0.0, r.number))
    return rows
