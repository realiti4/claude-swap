"""Made-up usage history for ``cswap history --demo``.

Four weeks of 15 accounts, generated the way the recorder writes real data
(a row when a reading changes, one every 6 hours regardless, ``prev`` on
each), with cswap auto switching between them: proactive and at-limit
moves, a consume-first move now and then, an occasional manual switch, and
one account that stops answering usage checks three and a half days before
the end. Deterministic for a given ``now`` and ``seed``, and in UTC, so a
test renders the same picture on every machine; nothing here touches disk.
"""

from __future__ import annotations

import random

from claude_swap.history import (
    HEARTBEAT_S,
    Account,
    Current,
    History,
    Reading,
)

DAY = 86400.0
HOUR = 3600.0
STEP = 600.0  # one simulated poll every 10 minutes
N_ACCOUNTS = 15
FAILING = 6  # the account whose usage checks start failing
# Chance that Claude Code is busy in each UTC hour of the day.
BUSY = (.55, .4, .22, .1, .04, .03, .05, .15, .4, .7, .85, .9,
        .8, .85, .9, .9, .85, .75, .6, .55, .65, .75, .7, .65)


def demo_history(now: float, *, seed: int = 20261025, strategy: str = "best") -> History:
    rnd = random.Random(seed)
    now = float(int(now))
    start = (now - 28 * DAY) // STEP * STEP
    fail_start = now - round(3.6 * DAY)

    class A:
        def __init__(self, n: int):
            self.n = n
            reset = -(-(start + rnd.random() * 7 * DAY) // HOUR) * HOUR
            if n == FAILING:
                # its week resets while it can't be checked, so its last
                # reading goes out of date
                reset = -(-(fail_start + 1.6 * DAY) // HOUR) * HOUR
                while reset - 7 * DAY > start:
                    reset -= 7 * DAY
            self.reset = reset
            self.fable = 0.0 if rnd.random() < .25 else .08 + rnd.random() * .3
            self.d7 = (1 - (reset - start) / (7 * DAY)) * (35 + rnd.random() * 55)
            self.m = self.d7 * self.fable
            self.h5 = 0.0
            self.h5_reset: float | None = None
            self.rows: list[Reading] = []
            self.sig: tuple | None = None
            self.row_t = float("-inf")
            self.seen: dict | None = None
            self.last_seen: float | None = None
            self.failures = 0

    accts = [A(n) for n in range(1, N_ACCOUNTS + 1)]
    state = {"active": 4}

    def usable(a: A, t: float) -> bool:
        return not (a.n == FAILING and t >= fail_start)

    def peak(a: A) -> float:
        return max(a.h5, a.d7)

    def pick(t: float, manual: bool = False) -> A | None:
        others = [a for a in accts if a.n != state["active"] and usable(a, t)]
        fresh = [a for a in others if a.h5 < 60 and a.d7 < 85]
        if not fresh:
            cur = accts[state["active"] - 1]
            cur_peak = peak(cur) if usable(cur, t) else 200.0
            best = min(others, key=peak, default=None)
            return best if best and peak(best) < min(95.0, cur_peak - 10) else None
        if manual:
            return fresh[int(rnd.random() * len(fresh))]
        if rnd.random() < .9:
            return min(fresh, key=peak)
        return min(fresh, key=lambda a: a.reset)

    def move(to: A | None) -> None:
        """Switch to ``to`` (cswap auto's move, or a manual one)."""
        if to is not None and to.n != state["active"]:
            state["active"] = to.n

    day_key, mood = -1, 1.0
    t = start
    while t <= now:
        hour = int(t // HOUR) % 24
        key = int(t // DAY)
        if key != day_key:
            day_key = key
            weekend = (int(t // DAY) + 4) % 7 in (5, 6)  # 1970-01-01 was a Thursday
            mood = (.55 + rnd.random() * .7) * (.65 if weekend else 1)
        for a in accts:
            if t >= a.reset:
                a.d7, a.m = 0.0, 0.0
                a.reset += 7 * DAY
            if a.h5_reset is not None and t >= a.h5_reset:
                a.h5, a.h5_reset = 0.0, None
        if not usable(accts[state["active"] - 1], t):
            move(pick(t))
        if rnd.random() < BUSY[hour] * min(1.15, mood):
            a = accts[state["active"] - 1]
            if a.h5_reset is None:
                a.h5_reset = t // HOUR * HOUR + 5 * HOUR
            used = 2.6 * mood * (.5 + rnd.random())  # points of the week in 10 minutes
            a.d7 = min(100.0, a.d7 + used)
            a.h5 = min(100.0, a.h5 + used / .6)
            a.m = min(100.0, a.m + used * a.fable)
            p = peak(a)
            if p >= 100:
                move(pick(t))
            elif p >= 90 and rnd.random() < .85:
                move(pick(t))
            elif rnd.random() < .01:
                soon = [b for b in accts if b.n != state["active"] and usable(b, t)
                        and b.reset - t < 20 * HOUR and b.d7 < 75 and b.h5 < 50
                        and a.reset - b.reset > DAY]
                if soon:
                    move(soon[0])
            elif rnd.random() < .006:
                move(pick(t, manual=True))
        # what the recorder writes: a row when a reading changes, and a
        # heartbeat every 6 hours regardless, each with the check before it
        for a in accts:
            at = min(now - 5, t + 20 + int(rnd.random() * 200))
            if not usable(a, t) or rnd.random() < .0015:
                a.failures += 1
                continue
            a.failures = 0
            a.seen = {"t": at, "h5": round(a.h5), "d7": round(a.d7), "m": round(a.m),
                      "h5_reset": a.h5_reset, "d7_reset": a.reset}
            sig = (a.seen["h5"], a.seen["d7"], a.seen["m"])
            if sig != a.sig or at - a.row_t >= HEARTBEAT_S:
                a.rows.append(Reading(at, a.seen["h5"], a.seen["d7"], a.seen["m"],
                                      prev=a.last_seen, d7_reset=a.reset))
                a.sig, a.row_t = sig, at
            a.last_seen = at
        t += STEP

    readings: dict[int, list[Reading]] = {}
    current: dict[int, Current] = {}
    for a in accts:
        c = a.seen or {}
        readings[a.n] = a.rows
        current[a.n] = Current(
            t=c.get("t"),
            h5=c.get("h5"), h5_reset=c.get("h5_reset"),
            d7=c.get("d7"), d7_reset=c.get("d7_reset"),
            model=c.get("m"), model_reset=c.get("d7_reset"),
            error="http-403" if a.n == FAILING else None,
            failures=a.failures if a.n == FAILING else 0,
        )
    return History(
        now=now,
        accounts=[Account(a.n) for a in accts],
        readings=readings,
        current=current,
        active=state["active"],
        model_name="Fable",
        recording_since=start,
        strategy=strategy,
        demo=True,
    )
