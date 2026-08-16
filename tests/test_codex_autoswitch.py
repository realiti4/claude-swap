"""Codex auto-switching: when it moves, when it refuses, and what it admits."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_swap.codex.autoswitch import CodexAutoSwitcher, CodexTick, binding_pct
from claude_swap.models import AccountSnapshot, AccountsSnapshot
from claude_swap.usage_store import UsageEntry


def _acc(number: str, pct: float | None, *, active=False, disabled=False, weekly=None):
    usage: dict = {}
    if pct is not None:
        usage["five_hour"] = {"pct": pct}
    if weekly is not None:
        usage["seven_day"] = {"pct": weekly}
    return AccountSnapshot(
        number=number,
        email=f"{number}@x",
        org_name="",
        org_uuid="",
        is_active=active,
        kind="oauth",
        switchable=True,
        usage=UsageEntry(last_good=usage or None),
        disabled=disabled,
        provider="codex",
    )


class FakeSwitcher:
    provider_id = "codex"

    def __init__(self, accounts, rotatable=None, running=()):
        self._accounts = accounts
        self._rotatable = rotatable
        self.switched: list[str] = []
        self._running = running

    def accounts_snapshot(self, fetch=None):
        return AccountsSnapshot(
            active_number=next((a.number for a in self._accounts if a.is_active), None),
            accounts=tuple(self._accounts),
            taken_at=0.0,
            provider="codex",
        )

    def switchable_account_numbers(self):
        if self._rotatable is not None:
            return self._rotatable
        return [a.number for a in self._accounts if not a.disabled]

    def switch_to(self, number):
        from claude_swap.codex.switcher import CodexSwitchResult

        self.switched.append(number)
        return CodexSwitchResult(number=number, email=f"{number}@x", running_pids=list(self._running))


def _auto(accounts, **kw) -> tuple[CodexAutoSwitcher, FakeSwitcher]:
    fake = FakeSwitcher(accounts, **{k: v for k, v in kw.items() if k in ("rotatable", "running")})
    opts = {k: v for k, v in kw.items() if k not in ("rotatable", "running")}
    return CodexAutoSwitcher(fake, **opts), fake


# ---- the binding window ------------------------------------------------


def test_binding_pct_is_the_worst_window():
    assert binding_pct(_acc("1", 20, weekly=80)) == 80


def test_binding_pct_is_none_without_a_measurement():
    assert binding_pct(_acc("1", None)) is None


# ---- when NOT to switch ------------------------------------------------


def test_an_active_account_below_threshold_is_left_alone():
    auto, fake = _auto([_acc("1", 40, active=True), _acc("2", 5)], threshold=90)
    assert auto.tick().outcome == "ok"
    assert fake.switched == []


def test_an_unmeasured_active_account_is_never_switched_away_from():
    """No measurement is not the same as no usage; moving on unknown data means
    moving the user for no established reason."""
    auto, fake = _auto([_acc("1", None, active=True), _acc("2", 1)], threshold=90)
    assert "unknown" in auto.tick().detail
    assert fake.switched == []


def test_no_candidate_below_threshold_blocks_rather_than_moving():
    auto, fake = _auto([_acc("1", 95, active=True), _acc("2", 97)], threshold=90)
    tick = auto.tick()
    assert tick.outcome == "blocked"
    assert fake.switched == []


def test_a_candidate_inside_the_hysteresis_margin_is_refused():
    """Two accounts hovering at the line must never ping-pong."""
    auto, fake = _auto(
        [_acc("1", 92, active=True), _acc("2", 89)], threshold=90, hysteresis_pct=10
    )
    assert auto.tick().outcome == "blocked"
    assert fake.switched == []


def test_a_disabled_account_is_not_a_candidate():
    auto, fake = _auto(
        [_acc("1", 95, active=True), _acc("2", 2, disabled=True)], threshold=90
    )
    assert auto.tick().outcome == "blocked"


def test_no_rotatable_accounts_is_reported_not_crashed():
    auto, _fake = _auto([_acc("1", 95, active=True)], rotatable=[], threshold=90)
    assert auto.tick().outcome == "no-accounts"


def test_no_active_account_is_a_quiet_no_op():
    auto, fake = _auto([_acc("1", 10), _acc("2", 20)], threshold=90)
    assert auto.tick().outcome == "ok"
    assert fake.switched == []


def test_a_snapshot_failure_is_reported_never_raised():
    class Broken(FakeSwitcher):
        def accounts_snapshot(self, fetch=None):
            raise RuntimeError("store gone")

    auto = CodexAutoSwitcher(Broken([]))
    assert auto.tick().outcome == "error"


# ---- when to switch ----------------------------------------------------


def test_an_exhausted_active_account_moves_to_the_roomiest_candidate():
    auto, fake = _auto(
        [_acc("1", 95, active=True), _acc("2", 40), _acc("3", 10)], threshold=90
    )
    tick = auto.tick()
    assert tick.outcome == "switched"
    assert fake.switched == ["3"]  # most headroom
    assert tick.switched_to == "3"


def test_dry_run_decides_without_switching():
    auto, fake = _auto([_acc("1", 95, active=True), _acc("2", 5)], threshold=90)
    tick = auto.tick(dry_run=True)
    assert tick.outcome == "ok" and "would switch" in tick.detail
    assert fake.switched == []


def test_the_weekly_window_can_be_what_triggers_the_switch():
    auto, fake = _auto(
        [_acc("1", 10, weekly=98, active=True), _acc("2", 10, weekly=5)], threshold=90
    )
    assert auto.tick().outcome == "switched"


def test_a_switch_failure_is_reported_not_raised():
    class Failing(FakeSwitcher):
        def switch_to(self, number):
            from claude_swap.exceptions import ClaudeSwitchError

            raise ClaudeSwitchError("no credentials")

    auto = CodexAutoSwitcher(Failing([_acc("1", 95, active=True), _acc("2", 5)]), threshold=90)
    assert auto.tick().outcome == "error"


# ---- the restart caveat, stated rather than hidden ---------------------


def test_a_switch_under_a_running_session_says_so():
    """An automatic switch only reaches the NEXT codex session. Saying nothing
    would let the user believe they had moved."""
    auto, _fake = _auto(
        [_acc("1", 95, active=True), _acc("2", 5)], threshold=90, running=(4242,)
    )
    tick = auto.tick()
    assert tick.running_pids == (4242,)
    assert "restart codex" in tick.human()
    assert "4242" in tick.human()


def test_a_switch_with_nothing_running_stays_quiet():
    auto, _fake = _auto([_acc("1", 95, active=True), _acc("2", 5)], threshold=90, running=())
    assert "restart" not in auto.tick().human()


def test_the_human_line_names_the_provider():
    """Two engines share one event stream; a line that does not say which
    provider it is about is noise."""
    assert CodexTick("ok", "nothing to do").human().startswith("codex:")
