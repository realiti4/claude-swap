"""Auto-switching for Codex, run alongside the Claude engine by ``cswap auto``.

Deliberately **not** a genericized ``AutoSwitchEngine``. That engine is ~1700
lines of tick built around Claude specifics — per-model scoped windows, setup
tokens, session profiles, credential quarantine, the consume-gate — and
abstracting it to fit a second provider would be a rewrite of the most
load-bearing code in the project, for a provider that needs almost none of it.
Two small engines in one process give the user what they asked for (one
``cswap auto``, both providers) at a fraction of the risk.

What Codex actually needs is small: read every account's usage, and if the
active one is at or over the threshold, move to whichever candidate has the most
headroom. Cadence, backoff and freshness are already handled by the usage cache.

**The honest caveat, and why it is surfaced rather than hidden.** A Codex switch
rewrites ``~/.codex/auth.json``, but a codex session already running holds its
tokens in memory and keeps using the old account until restarted. So an
automatic switch here only helps the *next* session. When running codex
processes are detected the engine says so in its event stream rather than
switching silently and letting the user believe they moved.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from claude_swap.codex.switcher import CodexSwitcher
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.models import AccountSnapshot

_logger = logging.getLogger(__name__)


def binding_pct(account: AccountSnapshot) -> float | None:
    """The utilization that decides this account's fate: the worst window.

    Codex has no per-model windows, so this is just max(5h, weekly) over
    whichever windows the account actually reports.
    """
    usage = account.usage.last_good
    if not isinstance(usage, dict):
        return None
    pcts = [
        float(window["pct"])
        for key in ("five_hour", "seven_day")
        if isinstance(window := usage.get(key), dict)
        and isinstance(window.get("pct"), (int, float))
    ]
    return max(pcts) if pcts else None


@dataclass(frozen=True)
class CodexTick:
    """What one Codex auto-switch tick decided, and why."""

    outcome: str  # "ok" | "switched" | "blocked" | "no-accounts" | "error"
    detail: str = ""
    switched_to: str | None = None
    running_pids: tuple[int, ...] = ()

    def human(self) -> str:
        base = f"codex: {self.detail}" if self.detail else f"codex: {self.outcome}"
        if self.running_pids:
            pids = ", ".join(str(p) for p in self.running_pids)
            base += f" — restart codex (pid {pids}) for it to take effect"
        return base


class CodexAutoSwitcher:
    """Threshold rotation for Codex accounts."""

    def __init__(
        self,
        switcher: CodexSwitcher | None = None,
        *,
        threshold: float = 90.0,
        hysteresis_pct: float = 10.0,
    ) -> None:
        self.switcher = switcher or CodexSwitcher()
        self.threshold = threshold
        self.hysteresis_pct = hysteresis_pct

    def tick(self, *, dry_run: bool = False) -> CodexTick:
        """One decision pass. Never raises."""
        try:
            snapshot = self.switcher.accounts_snapshot(fetch=None)
        except Exception as e:
            _logger.debug("codex auto tick failed: %s", type(e).__name__)
            return CodexTick("error", f"snapshot failed ({type(e).__name__})")

        rotatable = set(self.switcher.switchable_account_numbers())
        accounts = [a for a in snapshot.accounts if a.number in rotatable]
        if not accounts:
            return CodexTick("no-accounts", "no rotatable accounts")

        active = next((a for a in accounts if a.is_active), None)
        if active is None:
            return CodexTick("ok", "no managed account active")

        active_pct = binding_pct(active)
        if active_pct is None:
            # No measurement is not the same as no usage: switching on unknown
            # data would move the user for no established reason.
            return CodexTick("ok", f"account {active.number} usage unknown")

        if active_pct < self.threshold:
            return CodexTick(
                "ok", f"account {active.number} at {active_pct:.0f}% (below threshold)"
            )

        best, best_pct = None, None
        for candidate in accounts:
            if candidate.number == active.number:
                continue
            pct = binding_pct(candidate)
            if pct is None or pct >= self.threshold:
                continue
            # Must beat the active account by the hysteresis margin, or two
            # accounts hovering at the line would ping-pong every tick.
            if pct > active_pct - self.hysteresis_pct:
                continue
            if best_pct is None or pct < best_pct:
                best, best_pct = candidate, pct

        if best is None:
            return CodexTick(
                "blocked",
                f"account {active.number} at {active_pct:.0f}% and no better candidate",
            )

        if dry_run:
            return CodexTick(
                "ok",
                f"would switch {active.number} ({active_pct:.0f}%) -> "
                f"{best.number} ({best_pct:.0f}%)",
            )

        try:
            result = self.switcher.switch_to(best.number)
        except ClaudeSwitchError as e:
            return CodexTick("error", f"switch failed: {e}")

        return CodexTick(
            "switched",
            f"switched {active.number} ({active_pct:.0f}%) -> "
            f"{best.number} ({best_pct:.0f}%)",
            switched_to=best.number,
            # The switch already detected them; an EMPTY list means nothing was
            # running, not that nobody looked. An `or` fallback here re-ran the
            # real process table — inside unit tests, too.
            running_pids=tuple(result.running_pids),
        )
