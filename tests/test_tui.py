"""Tests for the Textual TUI: data service units + Pilot-driven app tests.

The Pilot tests run the real app headlessly against a ``FakeSwitcher`` that
implements exactly the structured surface the TUI consumes
(``accounts_snapshot``, ``switch_to``/``switch``/``remove_account``/add
flows) — no scraping, no real credentials, no network.
"""

from __future__ import annotations

import dataclasses
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from claude_swap.autoswitch import NoSwitchEvent, SwitchEvent
from claude_swap.json_output import USAGE_API_KEY, USAGE_TOKEN_EXPIRED
from claude_swap.models import AccountSnapshot, AccountsSnapshot
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.tui import data as tui_data
from claude_swap.usage_store import UsageEntry


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _iso_in(seconds: float) -> str:
    return (
        (datetime.now(timezone.utc) + timedelta(seconds=seconds))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def make_entry(
    pct5: float | None = 25.0,
    pct7: float | None = 10.0,
    *,
    sentinel: str | None = None,
    age_s: float = 5.0,
    scoped: list[tuple[str, float]] | None = None,
    spend: dict | None = None,
) -> UsageEntry:
    """``pct5``/``pct7`` of None omit that window (e.g. annual plans lack 7d)."""
    if sentinel is not None:
        return UsageEntry(sentinel=sentinel)
    last_good: dict = {}
    if pct5 is not None:
        last_good["five_hour"] = {"pct": pct5, "resets_at": _iso_in(7200)}
    if pct7 is not None:
        last_good["seven_day"] = {"pct": pct7, "resets_at": _iso_in(86400 * 3)}
    if scoped is not None:
        last_good["scoped"] = [
            {"name": name, "pct": pct, "resets_at": _iso_in(86400 * 2)}
            for name, pct in scoped
        ]
    if spend is not None:
        last_good["spend"] = spend
    return UsageEntry(
        last_good=last_good,
        fetched_at=time.time() - age_s,
        age_s=age_s,
    )


def make_account(
    number: int | str,
    *,
    active: bool = False,
    switchable: bool = True,
    kind: str = "oauth",
    entry: UsageEntry | None = None,
    email: str | None = None,
    alias: str = "",
    disabled: bool = False,
) -> AccountSnapshot:
    return AccountSnapshot(
        number=str(number),
        email=email or f"user{number}@example.com",
        org_name="",
        org_uuid="",
        is_active=active,
        kind=kind,
        switchable=switchable,
        usage=entry if entry is not None else make_entry(),
        alias=alias,
        disabled=disabled,
    )


class FakeSwitcher:
    """Structured-surface stand-in for ClaudeAccountSwitcher."""

    def __init__(self, accounts: list[AccountSnapshot], backup_dir: Path):
        self._accounts = list(accounts)
        self.backup_dir = backup_dir
        self.active = next(
            (a.number for a in accounts if a.is_active), None
        )
        self.calls: list[tuple] = []
        self.fetch_sets: list[set[str] | None] = []

    # -- surface the TUI consumes ------------------------------------------

    def accounts_snapshot(self, fetch: set[str] | None = None) -> AccountsSnapshot:
        self.fetch_sets.append(fetch)
        return AccountsSnapshot(
            active_number=self.active,
            accounts=tuple(self._accounts),
            taken_at=time.time(),
        )

    def current_account_number(self) -> str | None:
        return self.active

    def switch_to(
        self, identifier: str, json_output: bool = False, force: bool = False
    ) -> dict:
        self.calls.append(("switch_to", str(identifier)))
        old = self.active
        self.active = str(identifier)
        self._accounts = [
            dataclasses.replace(a, is_active=(a.number == self.active))
            for a in self._accounts
        ]
        return {
            "switched": True,
            "from": {"number": int(old) if old else None, "email": ""},
            "to": {
                "number": int(identifier),
                "email": f"user{identifier}@example.com",
            },
            "reason": "requested",
        }

    def switch(self, strategy: str | None = None, json_output: bool = False) -> dict:
        self.calls.append(("switch", strategy))
        return {"switched": False, "from": None, "to": None, "reason": "no-better-target"}

    def remove_account(self, identifier: str, assume_yes: bool = False) -> None:
        self.calls.append(("remove", str(identifier), assume_yes))
        self._accounts = [a for a in self._accounts if a.number != str(identifier)]
        print(f"Removed account {identifier}")

    def set_account_disabled(self, identifier: str, disabled: bool) -> None:
        self.calls.append(("set_disabled", str(identifier), disabled))
        self._accounts = [
            dataclasses.replace(a, disabled=disabled)
            if a.number == str(identifier)
            else a
            for a in self._accounts
        ]
        verb = "Disabled" if disabled else "Enabled"
        print(f"{verb} Account-{identifier}")

    def add_account(self, slot: int | None = None, assume_yes: bool = False) -> None:
        self.calls.append(("add", slot, assume_yes))
        print("Added Account 9: fresh@example.com")

    def add_account_from_token(
        self,
        token: str,
        email: str | None = None,
        slot: int | None = None,
        assume_yes: bool = False,
    ) -> None:
        self.calls.append(("add_token", token, email, slot, assume_yes))
        print(f"Added Account {slot or 9}")

    def set_poll_policy_inputs(
        self, threshold: float, models: tuple[str, ...]
    ) -> None:
        self._poll_inputs_override = (threshold, models)

    def clear_poll_policy_inputs(self) -> None:
        self._poll_inputs_override = None


def make_app(fake: FakeSwitcher):
    from claude_swap.tui.app import CswapApp

    return CswapApp(fake)


async def settle(pilot) -> None:
    """Let thread workers finish and their UI updates apply.

    The (fake) auto engine worker deliberately runs until its screen stops
    it, so waiting on it would block; wait on everything else.
    """
    app = pilot.app
    pending = [w for w in app.workers if w.group != "engine"]
    if pending:
        await app.workers.wait_for_complete(pending)
    await pilot.pause()
    await pilot.pause()


async def menu_select(pilot, action_id: str) -> None:
    """Drive the dashboard menu: highlight the entry by id, press Enter."""
    from textual.widgets import ListView

    from claude_swap.tui.widgets import MenuItem

    menu = pilot.app.screen.query_one("#menu", ListView)
    items = list(menu.query(MenuItem))
    menu.index = next(
        i for i, item in enumerate(items) if item.action_id == action_id
    )
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()


# ---------------------------------------------------------------------------
# Data service units (sync)
# ---------------------------------------------------------------------------


class TestFormatting:
    def test_format_duration(self):
        assert tui_data.format_duration(42) == "42s"
        assert tui_data.format_duration(180) == "3m"
        assert tui_data.format_duration(7980) == "2h 13m"
        assert tui_data.format_duration(3600 * 26) == "1d 2h"

    def test_format_age_fresh_is_silent(self):
        # Ages inside the serve TTL are the polling cadence at work, not
        # staleness worth flagging.
        assert tui_data.format_age(3.0) is None
        assert tui_data.format_age(120) is None
        assert tui_data.format_age(None) is None
        assert tui_data.format_age(400) == "· 6m ago"

    def test_sentinel_labels_match_cswap_list(self):
        # The TUI must describe sentinel states with the exact wording `cswap
        # list` prints — owned-and-expired means Claude Code refreshes the
        # active account, not that the user must re-login.
        assert (
            tui_data.sentinel_label(USAGE_TOKEN_EXPIRED)
            == "token expired — Claude Code refreshes the active account"
        )
        from claude_swap.switcher import SENTINEL_NOTES

        for sentinel, note in SENTINEL_NOTES.items():
            assert tui_data.sentinel_label(sentinel) == note
        assert tui_data.sentinel_label("unknown state") == "unknown state"

    def test_sentinel_card_shows_last_seen_like_cswap_list(self):
        # A sentinel is a live overlay — the entry can still carry the last
        # good measurement, and `cswap list` prints it as a "last seen" line.
        # The card must too (except for API-key accounts, which have no quota).
        from claude_swap.tui.widgets import account_card_text

        entry = UsageEntry(
            sentinel=USAGE_TOKEN_EXPIRED,
            last_good={"five_hour": {"pct": 53.0}},
            fetched_at=time.time() - 720,
            age_s=720.0,
        )
        card = account_card_text(make_account(1, active=True, entry=entry), 80).plain
        assert "token expired — Claude Code refreshes the active account" in card
        assert "last seen 53% used" in card

        no_history = account_card_text(
            make_account(1, entry=UsageEntry(sentinel=USAGE_TOKEN_EXPIRED)), 80
        ).plain
        assert "last seen" not in no_history

        api_key = account_card_text(
            make_account(
                1,
                kind="api_key",
                entry=dataclasses.replace(entry, sentinel=USAGE_API_KEY),
            ),
            80,
        ).plain
        assert "last seen" not in api_key

    def test_window_helpers(self):
        entry = make_entry(pct5=47.0)
        assert tui_data.window_pct(entry.last_good, "five_hour") == 47.0
        assert tui_data.window_pct(None, "five_hour") is None
        text = tui_data.window_reset_text(entry.last_good, "five_hour", time.time())
        assert text is not None and text.startswith("resets ")
        assert tui_data.window_reset_text(None, "five_hour", time.time()) is None

    def test_reset_clock(self):
        # Same-day reset → bare HH:MM; a reset days out carries its date.
        now = time.time()
        entry = make_entry()  # 5h resets in 2h, 7d in 3d
        clock5 = tui_data.reset_clock(entry.last_good["five_hour"], now)
        assert clock5 is not None and clock5.count(":") == 1
        clock7 = tui_data.reset_clock(entry.last_good["seven_day"], now)
        import calendar

        months = list(calendar.month_abbr)[1:]
        assert clock7 is not None and any(m in clock7 for m in months)

    def test_reset_clock_unknown_or_elapsed_is_none(self):
        now = time.time()
        assert tui_data.reset_clock(None, now) is None
        assert tui_data.reset_clock({"pct": 5.0}, now) is None
        assert tui_data.reset_clock({"resets_at": "garbage"}, now) is None
        # elapsed reset: the row says "resets now" — no clock to show
        elapsed = {"resets_at": _iso_in(-60)}
        assert tui_data.reset_clock(elapsed, now) is None
        assert tui_data.reset_text(elapsed, now) == "resets now"


class TestSnapshotSource:
    def _source(self, tmp_path: Path, accounts=None):
        fake = FakeSwitcher(
            accounts
            or [make_account(1, active=True), make_account(2)],
            tmp_path,
        )
        return fake, tui_data.SnapshotSource(fake)

    def test_every_pass_is_store_governed(self, tmp_path):
        # Pacing lives in the usage store (poll plans + freshness + atomic
        # reservation), so every take is the same on-demand pass `cswap list`
        # runs — including the user's explicit refresh, which cannot bypass
        # the store's per-token cadence.
        fake, source = self._source(tmp_path)
        source.take()
        source.take()
        source.take(full=True)
        assert fake.fetch_sets == [None, None, None]

    def test_store_only_never_fetches(self, tmp_path):
        fake, source = self._source(tmp_path)
        source.take(store_only=True)
        assert fake.fetch_sets == [set()]


class TestUsageRows:
    """The card's rows must mirror the CLI's _format_usage_lines semantics."""

    def test_absent_window_produces_no_row(self):
        from claude_swap.tui.widgets import usage_rows

        entry = make_entry(pct5=47.0, pct7=None)  # annual plan: no 7d window
        labels = [label for label, *_ in usage_rows(entry.last_good, time.time())]
        assert labels == ["5h"]

    def test_scoped_models_and_over_limit_marker(self):
        from claude_swap.tui.widgets import usage_rows

        entry = make_entry(scoped=[("Fable", 100.0), ("Opus", 12.0)])
        rows = usage_rows(entry.last_good, time.time())
        labels = [label for label, *_ in rows]
        assert labels == ["5h", "7d", "Fable", "Opus"]
        fable = next(row for row in rows if row[0] == "Fable")
        assert "(!)" in fable[2]
        # the marker stays terminal in the clock-extended variant too
        assert fable[3].endswith("(!)") and " · " in fable[3]

    def test_spend_row_first_with_amounts(self):
        from claude_swap.tui.widgets import usage_rows

        entry = make_entry(spend={"used": 12.5, "limit": 50.0, "pct": 25.0, "currency": "USD"})
        rows = usage_rows(entry.last_good, time.time())
        assert rows[0][0] == "$$"
        assert "$12.50 / $50.00" in rows[0][2]

    def test_suffix_full_extends_countdown_with_clock(self):
        from claude_swap.tui.widgets import usage_rows

        entry = make_entry(pct5=47.0)
        row5 = usage_rows(entry.last_good, time.time())[0]
        assert row5[2].startswith("resets ")
        assert row5[3].startswith(row5[2] + " · ")

    def test_spend_clock_sits_with_reset_not_after_amounts(self):
        from claude_swap.tui.widgets import usage_rows

        entry = make_entry(
            spend={
                "used": 12.5,
                "limit": 50.0,
                "pct": 25.0,
                "currency": "USD",
                "resets_at": _iso_in(7200),
            }
        )
        spend = usage_rows(entry.last_good, time.time())[0]
        assert spend[0] == "$$"
        assert " · " in spend[3]
        assert spend[3].index(" · ") < spend[3].index("$12.50")

    def test_no_data_no_rows(self):
        from claude_swap.tui.widgets import usage_rows

        assert usage_rows(None, time.time()) == []
        assert usage_rows({}, time.time()) == []

    def test_card_shows_clock_only_where_it_fits(self):
        # Per-row degradation: the wide card shows every clock, a mid width
        # keeps 5h/7d clocks while the longer spend row falls back to its
        # countdown, and a narrow card is exactly the old countdown-only look.
        from claude_swap.tui.widgets import account_card_text

        entry = make_entry(
            spend={
                "used": 12.5,
                "limit": 50.0,
                "pct": 25.0,
                "currency": "USD",
                "resets_at": _iso_in(7200),
            }
        )
        acc = make_account(1, active=True, entry=entry)

        wide = account_card_text(acc, 100).plain
        assert wide.count(" · ") == 3

        mid_lines = account_card_text(acc, 78).plain.splitlines()
        spend_line = next(line for line in mid_lines if "$12.50" in line)
        assert " · " not in spend_line
        for line in mid_lines:
            if "resets" in line and "$12.50" not in line:
                assert " · " in line

        narrow = account_card_text(acc, 40).plain
        assert " · " not in narrow


class TestRunAction:
    def test_captures_output_and_payload(self):
        def fn():
            print("hello")
            return {"switched": True}

        result = tui_data.run_action(fn)
        assert result.ok and result.payload == {"switched": True}
        assert "hello" in result.output

    def test_switch_error_is_captured_not_raised(self):
        from claude_swap.exceptions import ClaudeSwitchError

        def fn():
            raise ClaudeSwitchError("boom")

        result = tui_data.run_action(fn)
        assert not result.ok
        assert "boom" in result.output

    def test_unexpected_input_becomes_eoferror(self):
        def fn():
            input("should not block")

        result = tui_data.run_action(fn)
        assert not result.ok
        assert "interactive input" in result.output

    def test_first_line_strips_ansi(self):
        def fn():
            print("\x1b[1mBold headline\x1b[0m")

        assert tui_data.run_action(fn).first_line == "Bold headline"


# ---------------------------------------------------------------------------
# Pilot tests (async)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestDashboard:
    async def test_panel_shows_active_full_and_others_mini(self, tmp_path):
        fake = FakeSwitcher(
            [
                make_account(1, active=True, entry=make_entry(47.0, 63.0)),
                make_account(2, entry=make_entry(92.0, 71.0)),
            ],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            from claude_swap.tui.widgets import AccountsPanel

            panel = app.screen.query_one(AccountsPanel).render().plain
            assert "user1@example.com" in panel and "● active" in panel
            assert "resets" in panel  # the active card is the full one
            assert "user2@example.com" in panel and "92%" in panel
            # the mini line has no bars — bar glyphs only in the active card
            mini_part = panel.split("user2@example.com", 1)[1]
            assert "━" not in mini_part

    async def test_disabled_marker_on_active_card_and_mini(self, tmp_path):
        # A disabled account is still shown; it's just annotated so the user
        # can see it's held out of auto-rotation — on the full card when it's
        # the active login, and on the one-line form otherwise.
        fake = FakeSwitcher(
            [
                make_account(1, active=True, disabled=True),
                make_account(2, disabled=True),
            ],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            from claude_swap.tui.widgets import AccountsPanel

            panel = app.screen.query_one(AccountsPanel).render().plain
            assert "● active" in panel  # still the active card
            # both the active card and the mini row carry the marker
            assert panel.count("(disabled)") == 2

    async def test_active_card_skips_absent_window_and_shows_scoped(self, tmp_path):
        fake = FakeSwitcher(
            [
                make_account(
                    1,
                    active=True,
                    entry=make_entry(pct5=47.0, pct7=None, scoped=[("Fable", 62.0)]),
                )
            ],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            from claude_swap.tui.widgets import AccountsPanel

            panel = app.screen.query_one(AccountsPanel).render().plain
            assert "5h" in panel
            assert "7d" not in panel  # annual plan: no invented row
            assert "usage unknown" not in panel
            assert "Fable" in panel and "62%" in panel

    async def test_mini_line_skips_absent_window(self, tmp_path):
        fake = FakeSwitcher(
            [
                make_account(1, active=True),
                make_account(2, entry=make_entry(pct5=92.0, pct7=None)),
            ],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            from claude_swap.tui.widgets import AccountsPanel

            panel = app.screen.query_one(AccountsPanel).render().plain
            mini_part = panel.split("user2@example.com", 1)[1]
            assert "5h 92%" in mini_part
            assert "7d" not in mini_part

    async def test_menu_is_default_navigation_and_nests(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            from textual.widgets import ListView

            from claude_swap.tui.widgets import MenuItem

            menu = app.screen.query_one("#menu", ListView)
            ids = [item.action_id for item in menu.query(MenuItem)]
            assert ids == [
                "switch",
                "watch",
                "auto",
                "add-menu",
                "disable-menu",
                "remove-menu",
                "quit",
            ]
            # nest into Add (index 3), then back out with escape
            await pilot.press("down", "down", "down", "enter")
            await pilot.pause()
            ids = [item.action_id for item in menu.query(MenuItem)]
            assert ids == ["add-login", "add-token", "back"]
            await pilot.press("escape")
            await pilot.pause()
            ids = [item.action_id for item in menu.query(MenuItem)]
            assert ids[0] == "switch"

    async def test_remove_menu_shows_alias_before_email(self, tmp_path):
        fake = FakeSwitcher(
            [
                make_account(1, active=True, alias="dev"),
                make_account(2, email="plain@example.com"),
            ],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            from textual.widgets import ListView

            from claude_swap.tui.widgets import MenuItem

            await menu_select(pilot, "remove-menu")
            from textual.widgets import Static

            menu = app.screen.query_one("#menu", ListView)
            labels = [
                item.query_one(Static).render().plain for item in menu.query(MenuItem)
            ]
            assert any("dev (user1@example.com)" in label for label in labels)
            assert any("plain@example.com" in label for label in labels)
            assert not any("(plain@example.com)" in label for label in labels)

    async def test_back_menu_entry_pops_submenu(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            from textual.widgets import ListView

            from claude_swap.tui.widgets import MenuItem

            await menu_select(pilot, "add-menu")
            await menu_select(pilot, "back")
            menu = app.screen.query_one("#menu", ListView)
            ids = [item.action_id for item in menu.query(MenuItem)]
            assert ids[0] == "switch"

    async def test_vim_keys_move_menu_cursor(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            from textual.widgets import ListView

            menu = app.screen.query_one("#menu", ListView)
            assert menu.index == 0
            await pilot.press("j")
            assert menu.index == 1
            await pilot.press("k")
            assert menu.index == 0

    async def test_s_opens_switch_screen_and_enter_switches(self, tmp_path):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await pilot.press("s")
            await pilot.pause()
            from textual.widgets import ListView

            from claude_swap.tui.dashboard import DashboardScreen, SwitchScreen
            from claude_swap.tui.widgets import AccountItem

            assert isinstance(app.screen, SwitchScreen)
            listview = app.screen.query_one("#accounts", ListView)
            items = list(listview.query(AccountItem))
            assert [item.number for item in items] == ["1", "2"]
            assert listview.index == 0  # starts on the active account
            await pilot.press("down", "enter")
            await settle(pilot)
            assert ("switch_to", "2") in fake.calls
            assert isinstance(app.screen, DashboardScreen)  # popped back
            assert app.snapshot.active_number == "2"

    async def test_switch_screen_escape_backs_out(self, tmp_path):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await pilot.press("enter")  # menu: Switch account…
            await pilot.pause()
            from claude_swap.tui.dashboard import DashboardScreen, SwitchScreen

            assert isinstance(app.screen, SwitchScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, DashboardScreen)
            assert not any(call[0] == "switch_to" for call in fake.calls)

    async def test_remove_via_menu_confirms_then_removes(self, tmp_path):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "remove-menu")
            await menu_select(pilot, "remove:2")
            from claude_swap.tui.modals import ConfirmModal

            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("y")
            await settle(pilot)
            assert ("remove", "2", True) in fake.calls

    async def test_remove_via_menu_cancel_is_safe(self, tmp_path):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "remove-menu")
            await menu_select(pilot, "remove:1")
            await pilot.press("n")
            await settle(pilot)
            assert not any(call[0] == "remove" for call in fake.calls)

    async def test_disable_via_menu_toggles_without_confirm(self, tmp_path):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "disable-menu")
            await menu_select(pilot, "disable:2")  # no modal — direct action
            await settle(pilot)
            assert ("set_disabled", "2", True) in fake.calls
            # the submenu pops back to root after the toggle
            from textual.widgets import ListView

            from claude_swap.tui.widgets import MenuItem

            menu = app.screen.query_one("#menu", ListView)
            ids = [item.action_id for item in menu.query(MenuItem)]
            assert ids[0] == "switch"

    async def test_disable_menu_row_reflects_state_and_re_enables(self, tmp_path):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2, disabled=True)],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "disable-menu")
            from textual.widgets import ListView, Static

            from claude_swap.tui.widgets import MenuItem

            menu = app.screen.query_one("#menu", ListView)
            labels = [
                item.query_one(Static).render().plain for item in menu.query(MenuItem)
            ]
            # the already-disabled account offers to enable; the active one to disable
            assert any("(disabled)" in label and "enable" in label for label in labels)
            assert any("disable" in label and "(disabled)" not in label for label in labels)
            # selecting the disabled account flips it back on
            await menu_select(pilot, "disable:2")
            await settle(pilot)
            assert ("set_disabled", "2", False) in fake.calls

    async def test_modal_arrow_keys_choose_button(self, tmp_path):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "remove-menu")
            await menu_select(pilot, "remove:2")  # → confirm modal
            # focus starts on the confirm button; → moves to Cancel, enter presses it
            await pilot.press("right", "enter")
            await settle(pilot)
            assert not any(call[0] == "remove" for call in fake.calls)
            # reopen (menu index still on account 2), ← back to confirm, press it
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("right", "left", "enter")
            await settle(pilot)
            assert ("remove", "2", True) in fake.calls

    async def test_full_refresh_binding(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await pilot.press("f")
            await settle(pilot)
            assert fake.fetch_sets[-1] is None  # full on-demand pass

    async def test_add_token_via_menu_passes_assume_yes(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "add-menu")
            await menu_select(pilot, "add-token")
            from textual.widgets import Input

            app.screen.query_one("#token", Input).value = "sk-ant-oat01-test"
            app.screen.query_one("#slot", Input).value = "5"
            await pilot.click("#add")
            await settle(pilot)
            assert ("add_token", "sk-ant-oat01-test", None, 5, True) in fake.calls

    async def test_add_token_occupied_slot_asks_first(self, tmp_path):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "add-menu")
            await menu_select(pilot, "add-token")
            from textual.widgets import Input

            app.screen.query_one("#token", Input).value = "sk-ant-oat01-test"
            app.screen.query_one("#slot", Input).value = "2"
            await pilot.click("#add")
            await pilot.pause()
            from claude_swap.tui.modals import ConfirmModal

            assert isinstance(app.screen, ConfirmModal)  # overwrite confirm
            await pilot.press("n")
            await settle(pilot)
            assert not any(call[0] == "add_token" for call in fake.calls)

    async def test_empty_state_hint_in_panel(self, tmp_path):
        fake = FakeSwitcher([], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            from claude_swap.tui.widgets import AccountsPanel

            panel = app.screen.query_one(AccountsPanel).render().plain
            assert "No managed accounts yet" in panel

    async def test_palette_is_disabled(self, tmp_path):
        from claude_swap.tui.app import CswapApp

        assert CswapApp.ENABLE_COMMAND_PALETTE is False


@pytest.mark.asyncio
class TestWatchScreen:
    def _fake(self, tmp_path):
        return FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )

    async def test_w_opens_monitor_without_cursor(self, tmp_path):
        app = make_app(self._fake(tmp_path))
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await pilot.press("w")
            await pilot.pause()
            from textual.widgets import ListView

            from claude_swap.tui.dashboard import WatchScreen
            from claude_swap.tui.widgets import AccountItem

            assert isinstance(app.screen, WatchScreen)
            listview = app.screen.query_one("#accounts", ListView)
            assert len(list(listview.query(AccountItem))) == 2  # full cards
            assert listview.index is None  # monitor mode: no cursor
            await pilot.press("enter")  # inert while just watching
            await settle(pilot)
            assert not any(call[0] == "switch_to" for call in fake_calls(app))

    async def test_s_arms_selection_switch_stays_watching(self, tmp_path):
        fake = self._fake(tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await pilot.press("w")
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            from textual.widgets import ListView

            from claude_swap.tui.dashboard import WatchScreen

            listview = app.screen.query_one("#accounts", ListView)
            assert listview.index == 0  # cursor armed, on the active account
            await pilot.press("down", "enter")
            await settle(pilot)
            assert ("switch_to", "2") in fake.calls
            assert isinstance(app.screen, WatchScreen)  # stayed watching
            assert app.screen.query_one("#accounts", ListView).index is None
            assert app.snapshot.active_number == "2"

    async def test_escape_disarms_then_leaves(self, tmp_path):
        fake = self._fake(tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await pilot.press("w")
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            await pilot.press("escape")  # disarm selection only
            await pilot.pause()
            from textual.widgets import ListView

            from claude_swap.tui.dashboard import DashboardScreen, WatchScreen

            assert isinstance(app.screen, WatchScreen)
            assert app.screen.query_one("#accounts", ListView).index is None
            await pilot.press("escape")  # now leave
            await pilot.pause()
            assert isinstance(app.screen, DashboardScreen)
            assert not any(call[0] == "switch_to" for call in fake.calls)

    async def test_menu_watch_entry_opens_it(self, tmp_path):
        app = make_app(self._fake(tmp_path))
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "watch")
            from claude_swap.tui.dashboard import WatchScreen

            assert isinstance(app.screen, WatchScreen)

    async def test_app_start_watch_stacks_over_dashboard(self, tmp_path):
        from claude_swap.tui.app import CswapApp

        app = CswapApp(self._fake(tmp_path), start="watch")
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            from claude_swap.tui.dashboard import DashboardScreen, WatchScreen

            assert isinstance(app.screen, WatchScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, DashboardScreen)


def fake_calls(app) -> list[tuple]:
    return app.switcher.calls



class _FakeEngine:
    """Stands in for AutoSwitchEngine: records construction, blocks until stop."""

    instances: list["_FakeEngine"] = []

    def __init__(self, switcher, settings, on_event, *, dry_run=False, **kwargs):
        self.settings = settings
        self.on_event = on_event
        self.dry_run = dry_run
        self.stopped = False
        self.applied_thresholds: list[float] = []
        self.wakes = 0
        self._stop = threading.Event()
        _FakeEngine.instances.append(self)

    def run_loop(self) -> int:
        self.on_event(NoSwitchEvent(reason="cooldown"))
        self._stop.wait(30)
        return 0

    def stop(self) -> None:
        self.stopped = True
        self._stop.set()

    def apply_threshold(self, threshold: float) -> None:
        self.settings = dataclasses.replace(self.settings, threshold=threshold)
        self.applied_thresholds.append(threshold)

    def wake(self) -> None:
        self.wakes += 1


@pytest.fixture
def fake_engine(monkeypatch):
    _FakeEngine.instances = []
    monkeypatch.setattr(
        "claude_swap.tui.autoview.AutoSwitchEngine", _FakeEngine
    )
    return _FakeEngine


@pytest.mark.asyncio
class TestAutoScreen:
    async def _open(self, pilot):
        await settle(pilot)
        await pilot.press("g")
        await pilot.pause()

    async def test_opens_in_dry_run_and_store_only(self, tmp_path, fake_engine):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._open(pilot)
            from claude_swap.tui.autoview import AutoScreen

            assert isinstance(app.screen, AutoScreen)
            assert len(fake_engine.instances) == 1
            assert fake_engine.instances[0].dry_run is True
            assert app._store_only is True
            await settle(pilot)
            # engine event reached the log via call_from_thread
            from textual.widgets import RichLog

            assert len(app.screen.query_one("#event-log", RichLog).lines) > 0

    async def test_go_live_requires_confirmation(self, tmp_path, fake_engine):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._open(pilot)
            await pilot.press("l")
            await pilot.pause()
            from claude_swap.tui.modals import ConfirmModal

            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("y")
            await settle(pilot)
            assert len(fake_engine.instances) == 2
            assert fake_engine.instances[0].stopped is True
            assert fake_engine.instances[1].dry_run is False

    async def test_back_stops_engine_and_restores_fetching(
        self, tmp_path, fake_engine
    ):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._open(pilot)
            await pilot.press("escape")
            await settle(pilot)
            from claude_swap.tui.dashboard import DashboardScreen

            assert isinstance(app.screen, DashboardScreen)
            assert fake_engine.instances[0].stopped is True
            assert app._store_only is False

    async def test_threshold_adjust_is_session_only(self, tmp_path, fake_engine):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._open(pilot)
            screen = app.screen
            assert app.threshold_pct == 90.0  # mount syncs to the file value
            await pilot.press("right")  # inert outside adjust mode
            await pilot.pause()
            assert screen._settings.threshold == 90.0
            await pilot.press("t", "right", "right", "right")
            await pilot.pause()
            assert screen._settings.threshold == 93.0
            assert app.threshold_pct == 93.0
            engine = fake_engine.instances[0]
            assert engine.applied_thresholds == [91.0, 92.0, 93.0]
            from textual.widgets import Static

            summary = screen.query_one("#auto-summary", Static)
            assert "threshold 93% (session)" in summary.render().plain
            await pilot.press("enter")
            await pilot.pause()
            assert engine.wakes == 1  # one forced tick on leaving the mode
            # the override lives in memory only — nothing was persisted
            assert not (tmp_path / "settings.json").exists()
            # a dry↔live restart rebuilds the engine from the adjusted copy
            await pilot.press("l")
            await pilot.pause()
            await pilot.press("y")
            await settle(pilot)
            assert fake_engine.instances[1].settings.threshold == 93.0
            await pilot.press("escape")
            await settle(pilot)
            # leaving the screen reverts the tick and unpins poll planning
            assert app.threshold_pct == 90.0
            assert fake._poll_inputs_override is None

    async def test_threshold_adjust_escape_exits_mode_not_screen(
        self, tmp_path, fake_engine
    ):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._open(pilot)
            from claude_swap.tui.autoview import AutoScreen

            await pilot.press("t")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, AutoScreen)
            # no net change → no forced tick
            assert fake_engine.instances[0].wakes == 0
            await pilot.press("escape")
            await settle(pilot)
            from claude_swap.tui.dashboard import DashboardScreen

            assert isinstance(app.screen, DashboardScreen)

    async def test_threshold_clamps_and_keeps_meaningful_decimals(
        self, tmp_path, fake_engine
    ):
        import json as _json

        (tmp_path / "settings.json").write_text(_json.dumps({
            "schemaVersion": 1, "autoswitch": {"threshold": 99.0},
        }))
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._open(pilot)
            screen = app.screen
            await pilot.press("t", "right", "right")
            await pilot.pause()
            assert screen._settings.threshold == 99.9  # spec's upper bound
            from textual.widgets import Static

            summary = screen.query_one("#auto-summary", Static)
            # never a lying "100%"
            assert "threshold 99.9% (session)" in summary.render().plain
            screen.action_threshold_step(-60.0)
            await pilot.pause()
            assert screen._settings.threshold == 50.0  # spec's lower bound

    async def test_candidates_ranked_by_headroom(self, tmp_path, fake_engine):
        fake = FakeSwitcher(
            [
                make_account(1, active=True, entry=make_entry(91.0, 20.0)),
                make_account(2, entry=make_entry(80.0, 10.0)),
                make_account(3, entry=make_entry(15.0, 5.0)),
            ],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._open(pilot)
            await settle(pilot)
            from textual.widgets import Static

            plain = app.screen.query_one("#candidates", Static).render().plain
            assert plain.index("user3@example.com") < plain.index(
                "user2@example.com"
            )

    async def test_candidates_ranking_honors_configured_model(
        self, tmp_path, fake_engine
    ):
        """The 'Next best' ranking must use the same window set as the
        engine: with autoswitch.model set, a Fable-bound account ranks by
        its Fable pct, not its roomy 5h."""
        import json as _json

        (tmp_path / "settings.json").write_text(_json.dumps({
            "schemaVersion": 1, "autoswitch": {"model": "Fable"},
        }))
        fake = FakeSwitcher(
            [
                make_account(1, active=True, entry=make_entry(91.0, 20.0)),
                make_account(
                    2, entry=make_entry(10.0, 5.0, scoped=[("Fable", 95.0)])
                ),
                make_account(
                    3, entry=make_entry(50.0, 5.0, scoped=[("Fable", 20.0)])
                ),
            ],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._open(pilot)
            await settle(pilot)
            from textual.widgets import Static

            plain = app.screen.query_one("#candidates", Static).render().plain
            # On 5h alone #2 (10% used) would rank first; Fable 95% binds it
            # below #3 (50% binding).
            assert plain.index("user3@example.com") < plain.index(
                "user2@example.com"
            )


class TestEventText:
    def test_switch_event_styling_and_content(self):
        event = SwitchEvent(
            trigger="proactive",
            from_ref={"number": 1, "email": "a@x.com"},
            to_ref={"number": 2, "email": "b@x.com"},
        )
        from claude_swap.tui.autoview import event_text

        assert event.human() in event_text(event).plain


# ---------------------------------------------------------------------------
# accounts_snapshot on the real switcher
# ---------------------------------------------------------------------------


class TestAccountsSnapshot:
    def test_one_pass_snapshot(self, temp_home, mock_claude_config):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._init_sequence_file()
        data = switcher._get_sequence_data()
        data["sequence"] = [1, 2]
        data["accounts"] = {
            "1": {"email": "test@example.com", "uuid": "test-uuid-1234"},
            "2": {"email": "other@example.com", "uuid": "uuid-2"},
        }
        switcher._write_json(switcher.sequence_file, data)

        snap = switcher.accounts_snapshot(fetch=set())  # store-only: no network
        assert snap.active_number == "1"
        assert [acc.number for acc in snap.accounts] == ["1", "2"]
        active = snap.accounts[0]
        assert active.is_active and active.email == "test@example.com"
        assert all(acc.kind == "oauth" for acc in snap.accounts)
        # No stored credential backups: nothing is switchable, and usage is
        # sentinel'd rather than fetched.
        assert all(not acc.switchable for acc in snap.accounts)
        assert all(acc.usage.sentinel is not None for acc in snap.accounts)
        assert isinstance(snap.taken_at, float)


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


class TestBareInvocation:
    def test_bare_tty_launches_tui(self, monkeypatch, temp_home):
        import claude_swap.cli as cli
        import claude_swap.tui as tui

        launched = {}

        def fake_run(switcher):
            launched["switcher"] = switcher
            return 0

        monkeypatch.setattr(sys, "argv", ["cswap"])
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(tui, "run", fake_run)
        with pytest.raises(SystemExit) as excinfo:
            cli.main()
        assert excinfo.value.code == 0
        assert "switcher" in launched

    def test_bare_non_tty_keeps_usage_error(self, monkeypatch, temp_home):
        import claude_swap.cli as cli

        monkeypatch.setattr(sys, "argv", ["cswap"])
        monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        with pytest.raises(SystemExit) as excinfo:
            cli.main()
        assert excinfo.value.code == 2  # argparse usage error

    def test_cswap_watch_opens_tui_on_watch_page(self, monkeypatch, temp_home):
        import claude_swap.cli as cli
        import claude_swap.tui as tui

        launched = {}

        def fake_run(switcher, start="dashboard"):
            launched["start"] = start
            return 0

        monkeypatch.setattr(sys, "argv", ["cswap", "watch"])
        monkeypatch.setattr(tui, "run", fake_run)
        with pytest.raises(SystemExit) as excinfo:
            cli.main()
        assert excinfo.value.code == 0
        assert launched["start"] == "watch"
