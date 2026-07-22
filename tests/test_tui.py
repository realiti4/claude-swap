"""Tests for the Textual TUI: data service units + Pilot-driven app tests.

The Pilot tests run the real app headlessly against a ``FakeSwitcher`` that
implements exactly the structured surface the TUI consumes
(``accounts_snapshot``, ``switch_to``/``switch``/``remove_account``/add
flows) — no scraping, no real credentials, no network.
"""

from __future__ import annotations

import dataclasses
import json
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
from claude_swap.tui.widgets import bar_v, gradient_color, meter_grid_dims, meter_windows
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


def set_watch_style(backup_dir: Path, style: str) -> None:
    """Persist ui.watch_style so the app picks that watch layout at launch."""
    from claude_swap.settings import set_setting

    set_setting(backup_dir, "ui.watchStyle", style)


def make_app(fake: FakeSwitcher, *, watch_style: str | None = None):
    from claude_swap.tui.app import CswapApp

    if watch_style is not None:
        set_watch_style(fake.backup_dir, watch_style)
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

    def test_account_card_uses_light_palette_when_passed(self):
        from claude_swap.tui.theme import ACCENT_LIGHT, CSWAP_LIGHT, Palette
        from claude_swap.tui.widgets import account_card_text

        acc = make_account(1, active=True, entry=make_entry(pct5=95.0))
        text = account_card_text(acc, 100, palette=Palette.from_theme(CSWAP_LIGHT))
        styles = {str(span.style) for span in text.spans}
        assert any(ACCENT_LIGHT in s for s in styles)  # active marker uses light accent

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

    def test_seven_day_ahead_of_pace_marker(self):
        # 1 day elapsed of the week, 50% used -> far ahead of the ~14% expected.
        from claude_swap.tui.widgets import usage_rows

        now = time.time()
        last_good = {"seven_day": {"pct": 50.0, "resets_at": _iso_in(86400 * 6)}}
        row = usage_rows(last_good, now, now)[0]
        assert "(ahead of pace)" in row[2]
        assert "(ahead of pace)" in row[3]

    def test_five_hour_never_shows_pace_marker(self):
        from claude_swap.tui.widgets import usage_rows

        now = time.time()
        last_good = {"five_hour": {"pct": 90.0, "resets_at": _iso_in(3600 * 4)}}
        row = usage_rows(last_good, now, now)[0]
        assert "pace" not in row[2]

    def test_scoped_ahead_of_pace_marker(self):
        from claude_swap.tui.widgets import usage_rows

        now = time.time()
        last_good = {"scoped": [{"name": "Fable", "pct": 50.0, "resets_at": _iso_in(86400 * 6)}]}
        row = usage_rows(last_good, now, now)[0]
        assert "(ahead of pace)" in row[2]

    def test_maxed_scoped_marker_wins_over_pace(self):
        from claude_swap.tui.widgets import usage_rows

        now = time.time()
        last_good = {"scoped": [{"name": "Fable", "pct": 100.0, "resets_at": _iso_in(86400 * 6)}]}
        row = usage_rows(last_good, now, now)[0]
        assert "(!)" in row[2]
        assert "ahead of pace" not in row[2]

    def test_no_pace_marker_without_fetched_at(self):
        from claude_swap.tui.widgets import usage_rows

        now = time.time()
        last_good = {"seven_day": {"pct": 50.0, "resets_at": _iso_in(86400 * 6)}}
        row = usage_rows(last_good, now)[0]
        assert "pace" not in row[2]

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


class TestMiniAccountText:
    def test_seven_day_ahead_of_pace_marker(self):
        from claude_swap.tui.widgets import mini_account_text

        now = time.time()
        entry = UsageEntry(
            last_good={"seven_day": {"pct": 50.0, "resets_at": _iso_in(86400 * 6)}},
            fetched_at=now,
            age_s=0.0,
        )
        acc = make_account(1, entry=entry)
        assert "(ahead)" in mini_account_text(acc, now).plain

    def test_five_hour_never_shows_pace_marker(self):
        from claude_swap.tui.widgets import mini_account_text

        now = time.time()
        entry = UsageEntry(
            last_good={"five_hour": {"pct": 90.0, "resets_at": _iso_in(3600 * 4)}},
            fetched_at=now,
            age_s=0.0,
        )
        acc = make_account(1, entry=entry)
        assert "pace" not in mini_account_text(acc, now).plain

    def test_no_pace_marker_without_fetched_at(self):
        from claude_swap.tui.widgets import mini_account_text

        now = time.time()
        entry = UsageEntry(
            last_good={"seven_day": {"pct": 50.0, "resets_at": _iso_in(86400 * 6)}},
            fetched_at=None,
            age_s=None,
        )
        acc = make_account(1, entry=entry)
        assert "pace" not in mini_account_text(acc, now).plain


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
                "theme-menu",
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

    async def test_remove_menu_label_renders_bracket_tag_literally(self, tmp_path):
        # The remove menu labels each account with `[{display_tag}]`, and an
        # org name of "red" makes that literally "[red]" — a valid Rich
        # color markup tag. MenuItem must render it as text, not consume it
        # as styling (which would silently drop the tag from the label).
        fake = FakeSwitcher(
            [dataclasses.replace(make_account(1, active=True), org_name="red")],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            from textual.widgets import ListView, Static

            from claude_swap.tui.widgets import MenuItem

            await menu_select(pilot, "remove-menu")
            menu = app.screen.query_one("#menu", ListView)
            labels = [
                item.query_one(Static).render().plain for item in menu.query(MenuItem)
            ]
            assert any("[red]" in label for label in labels)

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
class TestMeterWatchScreen:
    """The opt-in ``ui.watch_style = meters`` vertical-meter grid layout."""

    def _fake(self, tmp_path):
        return FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )

    async def test_w_opens_monitor_without_cursor(self, tmp_path):
        app = make_app(self._fake(tmp_path), watch_style="meters")
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await pilot.press("w")
            await pilot.pause()
            from claude_swap.tui.dashboard import MeterWatchScreen
            from claude_swap.tui.widgets import MetersGrid

            assert isinstance(app.screen, MeterWatchScreen)
            grid = app.screen.query_one("#meters", MetersGrid)
            assert grid.cursor is None  # monitor mode: no cursor
            await pilot.press("enter")  # inert while just watching
            await settle(pilot)
            assert not any(call[0] == "switch_to" for call in fake_calls(app))

    async def test_no_monitor_title_but_prompt_when_armed(self, tmp_path):
        # The meters view drops the "watching all accounts" header (the grid is
        # self-evident) but still shows the selection prompt once armed.
        app = make_app(self._fake(tmp_path), watch_style="meters")
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await pilot.press("w")
            await pilot.pause()
            from textual.widgets import Static

            title = app.screen.query_one("#list-title", Static)
            assert "watching all accounts" not in str(title.render())
            assert str(title.render()) == ""  # blank while just watching
            assert title.display is False  # row collapsed — no wasted space
            await pilot.press("s")  # arm selection
            await pilot.pause()
            assert "switch to which account?" in str(title.render())
            assert title.display is True  # prompt row appears while selecting

    async def test_s_arms_selection_at_active_index(self, tmp_path):
        app = make_app(self._fake(tmp_path), watch_style="meters")
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await pilot.press("w")
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            from claude_swap.tui.widgets import MetersGrid

            grid = app.screen.query_one("#meters", MetersGrid)
            assert grid.cursor == 0  # armed on the active account

    async def test_nav_right_moves_cursor_one(self, tmp_path):
        app = make_app(self._fake(tmp_path), watch_style="meters")
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await pilot.press("w")
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            from claude_swap.tui.widgets import MetersGrid

            grid = app.screen.query_one("#meters", MetersGrid)
            assert grid.cursor == 0
            await pilot.press("l")  # two accounts side by side at this width
            await pilot.pause()
            assert grid.cursor == 1

    async def test_grid_content_fits_small_terminal_without_clipping(self, tmp_path):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2), make_account(3)],
            tmp_path,
        )
        app = make_app(fake, watch_style="meters")
        async with app.run_test(size=(44, 38)) as pilot:
            await settle(pilot)
            await pilot.press("w")
            await pilot.pause()
            from claude_swap.tui.widgets import MetersGrid

            grid = app.screen.query_one("#meters", MetersGrid)
            assert grid._ncols() == 2
            rendered_lines = grid.render().plain.count("\n") + 1
            assert rendered_lines <= grid.size.height

    async def test_grid_fallback_cursor_moves_through_compact_list(self, tmp_path):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2), make_account(3)],
            tmp_path,
        )
        app = make_app(fake, watch_style="meters")
        async with app.run_test(size=(30, 12)) as pilot:
            await settle(pilot)
            await pilot.press("w")
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            from claude_swap.tui.widgets import MetersGrid

            grid = app.screen.query_one("#meters", MetersGrid)
            assert grid._ncols() == 1  # fallback list is effectively one column
            assert grid.cursor == 0
            rendered_lines = grid.render().plain.count("\n") + 1
            assert rendered_lines <= grid.size.height
            await pilot.press("j")  # down moves to the next account in the list
            await pilot.pause()
            assert grid.cursor == 1
            assert grid.selected_number() == "2"

    async def test_selection_anchors_to_active_after_pre_snapshot_arm(self, tmp_path):
        """``s`` pressed before the initial snapshot lands must still end up
        selecting the active account once data arrives, not slot 1."""

        class BlockingFakeSwitcher(FakeSwitcher):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.release = threading.Event()

            def accounts_snapshot(self, fetch=None):
                self.release.wait(5)
                return super().accounts_snapshot(fetch)

        fake = BlockingFakeSwitcher(
            [make_account(1), make_account(2, active=True), make_account(3)],
            tmp_path,
        )
        app = make_app(fake, watch_style="meters")
        async with app.run_test(size=(100, 40)) as pilot:
            assert app.snapshot is None  # initial refresh hasn't landed yet
            await pilot.press("w")
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            from claude_swap.tui.widgets import MetersGrid

            grid = app.screen.query_one("#meters", MetersGrid)
            assert grid.cursor == 0  # arms at the fallback default, snapshot unknown

            fake.release.set()
            await settle(pilot)

            assert grid.selected_number() == "2"  # anchored to the active account

            # Membership changing later must keep the cursor in range too.
            shrunk = dataclasses.replace(
                app.snapshot, accounts=app.snapshot.accounts[:1]
            )
            app.snapshot = shrunk
            await pilot.pause()
            assert grid.cursor is not None
            assert grid.cursor < len(shrunk.accounts)

    async def test_armed_cursor_follows_account_across_reorder(self, tmp_path):
        """An external move/swap that reorders accounts while selection is
        armed must keep the cursor on the *same account*, not the same slot —
        otherwise Enter would switch to the wrong target."""
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2), make_account(3)],
            tmp_path,
        )
        app = make_app(fake, watch_style="meters")
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await pilot.press("w")
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            await pilot.press("l")  # move selection off the active account
            await pilot.pause()
            from claude_swap.tui.widgets import MetersGrid

            grid = app.screen.query_one("#meters", MetersGrid)
            selected = grid.selected_number()
            assert selected == "2"

            # Reorder so account "2" moves to a different index.
            accts = app.snapshot.accounts
            reordered = dataclasses.replace(
                app.snapshot, accounts=(accts[2], accts[0], accts[1])
            )
            app.snapshot = reordered
            await pilot.pause()
            assert grid.selected_number() == selected  # still on account "2"

    async def test_s_arms_selection_switch_stays_watching(self, tmp_path):
        fake = self._fake(tmp_path)
        app = make_app(fake, watch_style="meters")
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await pilot.press("w")
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            from claude_swap.tui.dashboard import MeterWatchScreen
            from claude_swap.tui.widgets import MetersGrid

            await pilot.press("l", "enter")
            await settle(pilot)
            assert ("switch_to", "2") in fake.calls
            assert isinstance(app.screen, MeterWatchScreen)  # stayed watching
            grid = app.screen.query_one("#meters", MetersGrid)
            assert grid.cursor is None  # disarmed after switch
            assert app.snapshot.active_number == "2"

    async def test_escape_disarms_then_leaves(self, tmp_path):
        fake = self._fake(tmp_path)
        app = make_app(fake, watch_style="meters")
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await pilot.press("w")
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            await pilot.press("escape")  # disarm selection only
            await pilot.pause()
            from claude_swap.tui.dashboard import DashboardScreen, MeterWatchScreen
            from claude_swap.tui.widgets import MetersGrid

            assert isinstance(app.screen, MeterWatchScreen)
            assert app.screen.query_one("#meters", MetersGrid).cursor is None
            await pilot.press("escape")  # now leave
            await pilot.pause()
            assert isinstance(app.screen, DashboardScreen)
            assert not any(call[0] == "switch_to" for call in fake.calls)

    async def test_menu_watch_entry_opens_it(self, tmp_path):
        app = make_app(self._fake(tmp_path), watch_style="meters")
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "watch")
            from claude_swap.tui.dashboard import MeterWatchScreen

            assert isinstance(app.screen, MeterWatchScreen)

    async def test_app_start_watch_stacks_over_dashboard(self, tmp_path):
        from claude_swap.tui.app import CswapApp

        fake = self._fake(tmp_path)
        set_watch_style(fake.backup_dir, "meters")
        app = CswapApp(fake, start="watch")
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            from claude_swap.tui.dashboard import DashboardScreen, MeterWatchScreen

            assert isinstance(app.screen, MeterWatchScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, DashboardScreen)


@pytest.mark.asyncio
class TestClassicWatchScreen:
    """The default horizontal-bar account-list watch layout (``classic``)."""

    def _fake(self, tmp_path):
        return FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )

    async def test_w_opens_monitor_without_cursor(self, tmp_path):
        # No ui.watch_style set → default classic.
        app = make_app(self._fake(tmp_path))
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await pilot.press("w")
            await pilot.pause()
            from textual.widgets import ListView

            from claude_swap.tui.dashboard import ClassicWatchScreen

            assert isinstance(app.screen, ClassicWatchScreen)
            listview = app.screen.query_one("#accounts", ListView)
            assert listview.index is None  # monitor mode: no cursor
            await pilot.press("enter")  # inert while just watching
            await settle(pilot)
            assert not any(call[0] == "switch_to" for call in fake_calls(app))

    async def test_monitor_title_preserved(self, tmp_path):
        # The classic screen is the author's default view — keep its
        # "watching all accounts" header (only the meters view drops it).
        app = make_app(self._fake(tmp_path))
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await pilot.press("w")
            await pilot.pause()
            from textual.widgets import Static

            title = app.screen.query_one("#list-title", Static)
            assert "watching all accounts" in str(title.render())

    async def test_s_arms_selection_at_active_index(self, tmp_path):
        app = make_app(self._fake(tmp_path))
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await pilot.press("w")
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            from textual.widgets import ListView

            listview = app.screen.query_one("#accounts", ListView)
            assert listview.index == 0  # armed on the active account

    async def test_armed_cursor_follows_account_across_reorder(self, tmp_path):
        """A reorder while selection is armed keeps the cursor on the same
        account, not the same row — so Enter can't switch to the wrong one."""
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2), make_account(3)],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await pilot.press("w")
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            await pilot.press("down")  # highlight account 2
            await pilot.pause()
            from textual.widgets import ListView

            listview = app.screen.query_one("#accounts", ListView)
            assert app.snapshot.accounts[listview.index].number == "2"

            accts = app.snapshot.accounts
            app.snapshot = dataclasses.replace(
                app.snapshot, accounts=(accts[2], accts[0], accts[1])
            )
            await pilot.pause()
            assert app.snapshot.accounts[listview.index].number == "2"

    async def test_s_arms_selection_switch_stays_watching(self, tmp_path):
        fake = self._fake(tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await pilot.press("w")
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            await pilot.press("down", "enter")  # move to account 2, confirm
            await settle(pilot)
            from textual.widgets import ListView

            from claude_swap.tui.dashboard import ClassicWatchScreen

            assert ("switch_to", "2") in fake.calls
            assert isinstance(app.screen, ClassicWatchScreen)  # stayed watching
            listview = app.screen.query_one("#accounts", ListView)
            assert listview.index is None  # disarmed after switch
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

            from claude_swap.tui.dashboard import ClassicWatchScreen, DashboardScreen

            assert isinstance(app.screen, ClassicWatchScreen)
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
            from claude_swap.tui.dashboard import ClassicWatchScreen

            assert isinstance(app.screen, ClassicWatchScreen)

    async def test_app_start_watch_stacks_over_dashboard(self, tmp_path):
        from claude_swap.tui.app import CswapApp

        app = CswapApp(self._fake(tmp_path), start="watch")
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            from claude_swap.tui.dashboard import ClassicWatchScreen, DashboardScreen

            assert isinstance(app.screen, ClassicWatchScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, DashboardScreen)


def test_watch_screen_factory_maps_styles():
    from claude_swap.tui.dashboard import (
        ClassicWatchScreen,
        MeterWatchScreen,
        watch_screen,
    )

    assert isinstance(watch_screen("classic"), ClassicWatchScreen)
    assert isinstance(watch_screen("meters"), MeterWatchScreen)
    # An unexpected value falls back to the classic default.
    assert isinstance(watch_screen("hologram"), ClassicWatchScreen)


@pytest.mark.asyncio
class TestWatchStyle:
    """ui.watch_style selects which watch layout the launcher opens."""

    def _fake(self, tmp_path):
        return FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )

    async def test_unset_setting_opens_classic(self, tmp_path):
        app = make_app(self._fake(tmp_path))
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await pilot.press("w")
            await pilot.pause()
            from claude_swap.tui.dashboard import ClassicWatchScreen

            assert isinstance(app.screen, ClassicWatchScreen)

    async def test_meters_setting_opens_meters(self, tmp_path):
        app = make_app(self._fake(tmp_path), watch_style="meters")
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await pilot.press("w")
            await pilot.pause()
            from claude_swap.tui.dashboard import MeterWatchScreen

            assert isinstance(app.screen, MeterWatchScreen)

    async def test_ctrl_v_toggles_layout_live_and_persists(self, tmp_path):
        fake = self._fake(tmp_path)
        app = make_app(fake, watch_style="meters")
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await pilot.press("w")
            await pilot.pause()
            from claude_swap.tui.dashboard import (
                ClassicWatchScreen,
                DashboardScreen,
                MeterWatchScreen,
            )
            from claude_swap.settings import load_ui_settings

            assert isinstance(app.screen, MeterWatchScreen)
            await pilot.press("ctrl+v")  # swap to the classic view live
            await pilot.pause()
            assert isinstance(app.screen, ClassicWatchScreen)
            assert app._watch_style == "classic"
            assert load_ui_settings(fake.backup_dir).watch_style == "classic"

            await pilot.press("ctrl+v")  # and back to meters
            await pilot.pause()
            assert isinstance(app.screen, MeterWatchScreen)
            assert load_ui_settings(fake.backup_dir).watch_style == "meters"

            # Esc still lands on the dashboard, not app-exit (stack intact).
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

    def test_event_text_uses_light_accent_for_switch(self):
        from claude_swap.tui.autoview import event_text
        from claude_swap.tui.theme import ACCENT_LIGHT, CSWAP_LIGHT, Palette

        event = SwitchEvent(
            trigger="proactive",
            from_ref={"number": 1, "email": "a@x.com"},
            to_ref={"number": 2, "email": "b@x.com"},
        )
        text = event_text(event, palette=Palette.from_theme(CSWAP_LIGHT))
        assert any(ACCENT_LIGHT in str(s.style) for s in text.spans)


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


# ---------------------------------------------------------------------------
# Theme wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestThemeWiring:
    async def test_mount_selects_light_theme_from_settings(self, tmp_path):
        (tmp_path / "settings.json").write_text(json.dumps({"ui": {"theme": "light"}}))
        fake = FakeSwitcher([make_account("1", active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test() as pilot:
            await settle(pilot)
            assert app.theme == "cswap-light"

    async def test_auto_setting_uses_detected_light(self, tmp_path):
        (tmp_path / "settings.json").write_text(json.dumps({"ui": {"theme": "auto"}}))
        fake = FakeSwitcher([make_account("1", active=True)], tmp_path)
        from claude_swap.tui.app import CswapApp
        app = CswapApp(fake, detected="light")
        async with app.run_test() as pilot:
            await settle(pilot)
            assert app.theme == "cswap-light"

    async def test_auto_setting_no_detection_falls_back_to_dark(self, tmp_path):
        (tmp_path / "settings.json").write_text(json.dumps({"ui": {"theme": "auto"}}))
        fake = FakeSwitcher([make_account("1", active=True)], tmp_path)
        from claude_swap.tui.app import CswapApp
        app = CswapApp(fake, detected=None)
        async with app.run_test() as pilot:
            await settle(pilot)
            assert app.theme == "cswap-dark"

    async def test_toggle_cycles_dark_light_auto(self, tmp_path):
        (tmp_path / "settings.json").write_text(json.dumps({"ui": {"theme": "dark"}}))
        fake = FakeSwitcher([make_account("1", active=True)], tmp_path)
        from claude_swap.tui.app import CswapApp
        app = CswapApp(fake, detected="light")
        async with app.run_test() as pilot:
            await settle(pilot)
            assert app.theme == "cswap-dark"          # setting dark
            app.action_toggle_theme(); await pilot.pause()
            assert app.theme == "cswap-light"          # → light
            app.action_toggle_theme(); await pilot.pause()
            assert app.theme == "cswap-light"          # → auto, detected=light
            assert json.loads((tmp_path / "settings.json").read_text())["ui"]["theme"] == "auto"
            app.action_toggle_theme(); await pilot.pause()
            assert app.theme == "cswap-dark"           # → back to dark

    async def test_theme_menu_marks_current_and_applies(self, tmp_path):
        from textual.widgets import ListView, Static

        from claude_swap.tui.widgets import MenuItem

        fake = FakeSwitcher([make_account("1", active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            assert app._theme_name == "auto"  # default
            await menu_select(pilot, "theme-menu")
            menu = app.screen.query_one("#menu", ListView)
            labels = [it.query_one(Static).render().plain for it in menu.query(MenuItem)]
            assert any("dark" in lbl for lbl in labels)
            assert any("light" in lbl for lbl in labels)
            current = next(lbl for lbl in labels if "auto" in lbl)
            assert "●" in current  # the current theme is marked
            await menu_select(pilot, "theme:light")
            assert app._theme_name == "light"
            assert app.theme == "cswap-light"


def test_bar_color_maxed_is_all_red_else_green_climb():
    from claude_swap.tui.widgets import _bar_color, gradient_color
    from claude_swap.tui.theme import SEV_OK

    # A maxed window's whole bar is red-dominant (no calm green at the base)
    for t in (0.0, 0.5, 1.0):
        c = _bar_color(100.0, t)
        r, g, b = int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)
        assert r > g and r > b, (t, c)
    assert _bar_color(100.0, 0.0) != gradient_color(0.0)  # maxed base != green

    # Below 100%, the bar keeps the green→amber→red climb (green at the base)
    assert _bar_color(50.0, 0.0) == SEV_OK
    assert _bar_color(50.0, 1.0) == gradient_color(1.0)


def test_gradient_color_hits_stops_and_interpolates():
    assert gradient_color(0.0) == "#87af87"   # SEV_OK
    assert gradient_color(0.5) == "#d7af5f"   # SEV_WARN
    assert gradient_color(1.0) == "#d75f5f"   # SEV_CRIT
    # quarter point = midpoint of SEV_OK(135,175,135) and SEV_WARN(215,175,95)
    assert gradient_color(0.25) == "#afaf73"  # (175,175,115)
    assert gradient_color(-1.0) == "#87af87"  # clamp low
    assert gradient_color(2.0) == "#d75f5f"   # clamp high


def test_bar_v_fill_from_bottom():
    from claude_swap.tui.widgets import bar_v
    assert bar_v(100.0, 4) == ["█", "█", "█", "█"]
    assert bar_v(0.0, 4)   == [" ", " ", " ", " "]
    assert bar_v(50.0, 4)  == [" ", " ", "█", "█"]   # 2 full at bottom
    assert bar_v(75.0, 4)  == [" ", "█", "█", "█"]
    assert bar_v(62.5, 4)  == [" ", "▄", "█", "█"]   # partial top cell
    assert bar_v(150.0, 2) == ["█", "█"]             # clamps to 100


def test_meter_windows_order_and_fields():
    now = 1_000_000.0

    def _iso(offset: float) -> str:
        return datetime.fromtimestamp(now + offset, tz=timezone.utc).isoformat()

    last_good = {
        "five_hour": {"pct": 78.0, "resets_at": _iso(3 * 3600)},
        "seven_day": {"pct": 34.0, "resets_at": _iso(4 * 86400)},
        "scoped": [{"name": "Fable", "pct": 100.0, "resets_at": _iso(3600)}],
    }
    rows = meter_windows(last_good, now)
    assert [r[0] for r in rows] == ["5h", "7d", "Fable"]
    assert [r[1] for r in rows] == [78.0, 34.0, 100.0]
    assert [r[3] for r in rows] == [False, False, True]
    assert meter_windows(None, now) == []


def test_meter_grid_dims_fluid():
    from claude_swap.tui.widgets import meter_grid_dims
    # 44x16, 3 accounts -> 2 columns, card_width (44-1)//2
    ncols, cw, bh = meter_grid_dims(44, 16, 3)
    assert ncols == 2
    assert cw == 21
    # 96x34, 3 accounts -> 3 columns, card_width (96-2)//3
    ncols, cw, bh = meter_grid_dims(96, 34, 3)
    assert ncols == 3
    assert cw == 31
    # narrow: single column; bars shrink to fit (floor is 1, not 3)
    ncols, cw, bh = meter_grid_dims(30, 12, 3)
    assert ncols == 1
    assert bh >= 1


def test_meter_grid_dims_fits_height():
    from claude_swap.tui.widgets import CARD_CHROME, meter_grid_dims

    # (44, 16, 3) is deliberately excluded: at that size the bar floors at
    # bar_min before the chrome fits, which is exactly the case
    # meters_grid_text's _cards_fit check catches to fall back to the
    # compact view instead of overflowing.
    for w, h, n in ((44, 31, 3), (96, 34, 3), (21, 16, 1)):
        ncols, _cw, bh = meter_grid_dims(w, h, n)
        rows = -(-n // ncols)  # ceil
        assert rows * (bh + CARD_CHROME) + (rows - 1) <= h, (w, h, n, ncols, bh)


def test_meter_grid_dims_honors_gutter_min_card_width():
    from claude_swap.tui.widgets import meter_grid_dims

    # 40 wide, 3 accounts: gutters must not push cards below min_card_w (20)
    ncols, cw, _bh = meter_grid_dims(40, 16, 3)
    assert cw >= 20


def test_meter_grid_dims_bars_fill_tall_terminal():
    from claude_swap.tui.widgets import meter_grid_dims

    # one account on a tall terminal: the bar consumes the spare rows
    # (no fixed height cap), leaving only the 12-line card chrome.
    _ncols, _cw, bh = meter_grid_dims(40, 40, 1)
    assert bh == 40 - 12


def test_meter_bar_width_fills_cell():
    from claude_swap.tui.widgets import _meter_bar_width

    # bar spans its cell minus a one-column gutter each side (no width cap)
    assert _meter_bar_width(15) == 13
    assert _meter_bar_width(30) == 28
    assert _meter_bar_width(4) == 2


def test_big_number_pixel_5_rows():
    from claude_swap.tui.widgets import big_number

    rows = big_number("100")
    assert len(rows) == 5
    # three digits, each 3 cols, joined by a one-space gap: 3+1+3+1+3 = 11
    assert all(len(r) == 11 for r in rows)

    rows7 = big_number("7")
    assert len(rows7) == 5
    assert all(len(r) == 3 for r in rows7)

    # the pixel font uses only the full block and spaces
    for r in rows + rows7:
        assert set(r) <= {"█", " "}


def test_meter_card_has_plain_label_row():
    from claude_swap.tui.widgets import meter_card

    acc = make_account(
        1,
        active=True,
        email="work@acme.dev",
        entry=make_entry(pct5=78.0, pct7=34.0, scoped=[("Fable", 100.0)]),
    )
    now = time.time()
    bar_height = 10
    card = meter_card(acc, 34, bar_height, now=now)
    lines = card.plain.split("\n")
    # header + bar rows + baseline + label + 5 percent + reset + bottom
    assert len(lines) == bar_height + 12
    assert all(len(ln) == 34 for ln in lines)

    label_row = lines[bar_height + 2]  # header(1) + bars(bar_height) + baseline(1)
    assert "5h" in label_row
    assert "7d" in label_row
    assert "Fable" in label_row

    # not carved into the bars: no bar row contains the label text
    bar_rows = lines[1 : 1 + bar_height]
    assert not any("5h" in row or "7d" in row or "Fable" in row for row in bar_rows)

    # a label longer than its cell truncates with an ellipsis rather than
    # overflowing or wrapping
    narrow_acc = make_account(
        2,
        active=True,
        email="work@acme.dev",
        entry=make_entry(pct5=None, pct7=None, scoped=[("AnthropicMaxPlan", 50.0)]),
    )
    narrow_bar_height = 3
    narrow_card = meter_card(narrow_acc, 8, narrow_bar_height, now=now)
    narrow_lines = narrow_card.plain.split("\n")
    narrow_label_row = narrow_lines[narrow_bar_height + 2]
    assert "…" in narrow_label_row
    assert "AnthropicMaxPlan" not in narrow_card.plain


def test_meter_card_line_count_chrome_12():
    from claude_swap.tui.widgets import meter_card

    acc = make_account(
        1,
        active=True,
        email="work@acme.dev",
        entry=make_entry(pct5=78.0, pct7=34.0, scoped=[("Fable", 100.0)]),
    )
    card = meter_card(acc, 40, 12, now=time.time())
    lines = card.plain.split("\n")
    # bar_height (12) + chrome (12): 2 borders + baseline + label + blank +
    # 5 percent + blank + reset
    assert len(lines) == 12 + 12
    assert all(len(ln) == 40 for ln in lines)


def test_meter_card_reset_is_plain_single_row():
    from claude_swap.tui.widgets import meter_card
    from claude_swap.usage_store import UsageEntry

    now = 1_000_000.0

    def _iso(offset: float) -> str:
        return datetime.fromtimestamp(now + offset, tz=timezone.utc).isoformat()

    last_good = {
        "five_hour": {"pct": 78.0, "resets_at": _iso(3 * 3600)},
        "seven_day": {"pct": 34.0, "resets_at": _iso(3 * 86400)},
    }
    entry = UsageEntry(last_good=last_good, fetched_at=now - 5.0, age_s=5.0)
    # inactive account: keeps the single-line frame this test's glyph checks
    # rely on — the active card's border is covered separately.
    acc = make_account(1, email="work@acme.dev", entry=entry)
    bar_height = 10
    card = meter_card(acc, 34, bar_height, now=now)
    lines = card.plain.split("\n")
    assert len(lines) == bar_height + 12
    assert all(len(ln) == 34 for ln in lines)

    def is_blank_interior(line: str) -> bool:
        return line[0] == "│" and line[-1] == "│" and line[1:-1].strip() == ""

    # header(1) + bars(bar_height) + baseline(1) + label(1) -> percent block
    # starts right after a blank margin row; below it, another blank margin
    # row precedes a SINGLE plain reset row, then the bottom border.
    label_row = bar_height + 2
    percent_start = label_row + 2
    assert is_blank_interior(lines[percent_start - 1])
    percent_end = percent_start + 5
    assert is_blank_interior(lines[percent_end])  # blank margin below percent

    reset_row = lines[percent_end + 1]
    assert "3d" in reset_row  # seven_day resets in 3 days
    assert not is_blank_interior(reset_row)
    # the reset row is a single row: the very next line is the bottom border
    assert lines[percent_end + 2] == lines[-1]
    assert lines[-1].startswith("╰") and lines[-1].endswith("╯")

    # not a half-block pixel font: the reset row itself has no block glyphs
    # (the bar rows above legitimately use them for the meter fill)
    assert not (set(reset_row) & set("█▀▄"))


def test_meter_card_percent_has_margin_rows():
    from claude_swap.tui.widgets import meter_card

    # inactive account: keeps the single-line frame this test's glyph checks
    # rely on — the active card's border is covered separately.
    acc = make_account(
        1,
        email="work@acme.dev",
        entry=make_entry(pct5=78.0, pct7=34.0, scoped=[("Fable", 100.0)]),
    )
    bar_height = 10
    card = meter_card(acc, 34, bar_height, now=time.time())
    lines = card.plain.split("\n")
    assert len(lines) == bar_height + 12
    assert all(len(ln) == 34 for ln in lines)

    def is_blank_interior(line: str) -> bool:
        return line[0] == "│" and line[-1] == "│" and line[1:-1].strip() == ""

    # header(1) + bars(bar_height) + baseline(1) + label(1) -> percent block
    # starts right after a blank margin row.
    label_row = bar_height + 2
    percent_start = label_row + 2
    assert is_blank_interior(lines[percent_start - 1])
    assert not is_blank_interior(lines[percent_start])  # first percent row has content

    # the percent block is exactly 5 rows, followed by a blank margin row
    # before the reset row.
    percent_end = percent_start + 5
    assert not is_blank_interior(lines[percent_end - 1])  # last percent row has content
    assert is_blank_interior(lines[percent_end])


def test_meter_card_active_double_green_border():
    from claude_swap.tui.widgets import _ACTIVE_GREEN, meter_card
    from claude_swap.tui.theme import MUTED

    entry = make_entry(pct5=78.0, pct7=34.0)
    active = make_account(1, active=True, email="work@acme.dev", entry=entry)
    inactive = make_account(2, active=False, email="work@acme.dev", entry=entry)
    now = time.time()
    acard = meter_card(active, 21, 5, now=now)
    icard = meter_card(inactive, 21, 5, now=now)
    active_style = f"{_ACTIVE_GREEN} bold"

    def frame_styles(card, glyphs):
        return {
            sp.style
            for sp in card.spans
            if set(card.plain[sp.start : sp.end]) & set(glyphs)
        }

    # active: double-line box glyphs, bold bright-green frame, no single-line
    # glyphs anywhere in the card
    assert "║" in acard.plain
    assert all(g in acard.plain for g in "╔╗╚╝")
    assert "│" not in acard.plain
    assert not (set(acard.plain) & set("╭╮╰╯"))
    assert active_style in frame_styles(acard, "║")
    assert active_style in frame_styles(acard, "╔╗╚╝")

    # active: the header number, name, and active dot render in the same
    # bold bright-green style as the frame, not ACCENT/FOREGROUND
    def texts_with_style(card, style):
        return [card.plain[sp.start : sp.end] for sp in card.spans if sp.style == style]

    assert "1" in texts_with_style(acard, active_style)
    assert "work" in texts_with_style(acard, active_style)
    assert "●" in texts_with_style(acard, active_style)

    # inactive: unchanged thin rounded single-line frame, muted, never green
    assert "│" in icard.plain
    assert all(g in icard.plain for g in "╭╮╰╯")
    assert "║" not in icard.plain
    assert not (set(icard.plain) & set("╔╗╚╝"))
    assert MUTED in frame_styles(icard, "│")
    assert active_style not in frame_styles(icard, "│")
    assert active_style not in frame_styles(icard, "╭╮╰╯")


def test_meter_card_percent_is_five_big_rows():
    from claude_swap.tui.widgets import meter_card

    acc = make_account(
        1,
        active=True,
        email="work@acme.dev",
        entry=make_entry(pct5=78.0, pct7=None),
    )
    now = time.time()
    card = meter_card(acc, 30, 5, now=now)
    lines = card.plain.split("\n")
    # top border + 5 bar rows + baseline + 5 percent rows + reset + bottom
    assert len(lines) == 5 + 12
    assert all(len(ln) == 30 for ln in lines)
    # the single window's cell is wide enough for the pixel digits, so the
    # small "78%" token never appears — the percent is drawn as block glyphs
    # spanning five rows instead.
    assert "78%" not in card.plain
    assert "█" in card.plain  # the pixel-block digit font


def test_meter_card_structure():
    from claude_swap.tui.widgets import meter_card
    from claude_swap.tui.theme import SEV_OK

    # inactive account: keeps the single-line frame this test's glyph checks
    # rely on — the active card's border is covered separately.
    acc = make_account(
        1,
        email="work@acme.dev",
        entry=make_entry(pct5=78.0, pct7=34.0, scoped=[("Fable", 100.0)]),
    )
    now = time.time()
    card = meter_card(acc, 21, 5, now=now)
    lines = card.plain.split("\n")
    assert len(lines) == 5 + 12
    assert all(len(ln) == 21 for ln in lines)
    assert lines[0].startswith("╭") and lines[0].endswith("╮")
    assert lines[-1].startswith("╰") and lines[-1].endswith("╯")
    # window labels render as plain text in the label row beneath the bars.
    assert "5h" in card.plain and "7d" in card.plain
    assert "Fable" in card.plain
    # Fable's cell is too narrow for the pixel digits ("100" needs 11 cols),
    # so it degrades to the small "100%" token.
    assert "100%" in card.plain
    # bottom cell of a filled bar is the green (SEV_OK) end of the gradient
    assert any(SEV_OK in str(span.style) for span in card.spans)


def test_meter_card_handles_no_windows():
    from claude_swap.tui.widgets import meter_card

    acc = make_account(1, active=True, entry=make_entry(pct5=None, pct7=None))
    card = meter_card(acc, 21, 5, now=time.time())
    lines = card.plain.split("\n")
    assert len(lines) == 5 + 12
    assert all(len(ln) == 21 for ln in lines)
    # the placeholder message is actually rendered, not just blank rows
    assert "usage unavailable" in card.plain


def test_meter_card_sentinel_message_wraps_full():
    from claude_swap.tui.widgets import meter_card

    acc = make_account(1, active=True, entry=UsageEntry(sentinel=USAGE_TOKEN_EXPIRED))
    now = time.time()
    card = meter_card(acc, 21, 5, now=now)
    lines = card.plain.split("\n")
    assert len(lines) == 5 + 12
    assert all(len(ln) == 21 for ln in lines)
    # the full sentinel label wraps across rows rather than truncating to one
    # line — its last word must survive.
    label = tui_data.sentinel_label(USAGE_TOKEN_EXPIRED)
    assert label.split()[-1] in card.plain


def test_meter_card_exact_width_at_narrow_widths():
    from claude_swap.tui.widgets import meter_card

    acc = make_account(
        1,
        active=True,
        email="work@acme.dev",
        entry=make_entry(pct5=78.0, pct7=34.0, scoped=[("Fable", 100.0)]),
    )
    now = time.time()
    for w in (1, 2, 3, 6, 8, 12, 21):
        card = meter_card(acc, w, 3, now=now)
        assert all(len(ln) == w for ln in card.plain.split("\n")), (
            w,
            card.plain.split("\n"),
        )


def test_meter_card_header_and_row_styling():
    from claude_swap.tui.widgets import meter_card
    from claude_swap.tui.theme import ACCENT, FOREGROUND, SEV_CRIT, Palette
    from claude_swap.usage_store import UsageEntry

    severity_color = Palette.DARK.severity  # meter_card() defaults to Palette.DARK

    now = 1_000_000.0

    def _iso(offset: float) -> str:
        return datetime.fromtimestamp(now + offset, tz=timezone.utc).isoformat()

    last_good = {
        "five_hour": {"pct": 78.0, "resets_at": _iso(3 * 3600)},
        "seven_day": {"pct": 34.0, "resets_at": _iso(4 * 86400)},
        "scoped": [{"name": "Fable", "pct": 100.0, "resets_at": _iso(2 * 86400)}],
    }
    entry = UsageEntry(last_good=last_good, fetched_at=now - 5.0, age_s=5.0)
    # inactive account: the active card's header renders in the bright-green
    # frame style instead of ACCENT/FOREGROUND — covered separately.
    acc = make_account(1, email="work@acme.dev", entry=entry)

    card = meter_card(acc, 21, 5, now=now)

    def texts_with_style(style: str) -> list[str]:
        return [card.plain[sp.start : sp.end] for sp in card.spans if sp.style == style]

    # header: number in ACCENT, name in FOREGROUND
    assert "1" in texts_with_style(ACCENT)
    assert "work" in texts_with_style(FOREGROUND)

    # percent rows: 5h and 7d's cells fit big digits (no "%" glyph rendered),
    # each styled by severity_color(pct); Fable's cell is too narrow for its
    # 3-digit "100" and degrades to the small "100%" token.
    for pct in (78.0, 34.0):
        assert any(
            any(ch != " " for ch in t) for t in texts_with_style(severity_color(pct))
        )
    assert any("100%" in t for t in texts_with_style(severity_color(100.0)))

    # MAXED window's reset (Fable, exactly 2 days out -> "2d") renders as
    # plain text styled SEV_CRIT.
    assert any("2d" in t for t in texts_with_style(SEV_CRIT))


def test_meter_card_renders_in_light_palette():
    # The meters follow ui.theme: a light-palette render paints its text in the
    # light theme's colours, never the dark constants.
    from claude_swap.tui.widgets import meter_card
    from claude_swap.tui.theme import (
        ACCENT,
        ACCENT_LIGHT,
        FOREGROUND,
        FOREGROUND_LIGHT,
        CSWAP_LIGHT,
        Palette,
    )
    from claude_swap.usage_store import UsageEntry

    now = 1_000_000.0
    entry = UsageEntry(
        last_good={"five_hour": {"pct": 50.0}}, fetched_at=now - 5.0, age_s=5.0
    )
    acc = make_account(1, email="work@acme.dev", entry=entry)

    light = Palette.from_theme(CSWAP_LIGHT)
    card = meter_card(acc, 21, 5, now=now, palette=light)
    styles = {str(sp.style) for sp in card.spans}

    # Header number/name pick up the light accent/foreground, not the dark ones.
    assert any(ACCENT_LIGHT in s for s in styles)
    assert any(FOREGROUND_LIGHT in s for s in styles)
    assert not any(ACCENT in s and ACCENT_LIGHT not in s for s in styles)
    assert not any(FOREGROUND in s and FOREGROUND_LIGHT not in s for s in styles)


def test_meter_card_header_honors_alias():
    from claude_swap.tui.widgets import meter_card

    now = time.time()
    aliased = make_account(
        1,
        active=True,
        email="work@acme.dev",
        alias="acme",
        entry=make_entry(pct5=78.0, pct7=34.0),
    )
    unaliased = make_account(
        2,
        active=True,
        email="work@acme.dev",
        entry=make_entry(pct5=78.0, pct7=34.0),
    )

    aliased_card = meter_card(aliased, 21, 5, now=now)
    unaliased_card = meter_card(unaliased, 21, 5, now=now)

    assert "acme" in aliased_card.plain
    assert "work" not in aliased_card.plain
    assert "work" in unaliased_card.plain

    for card in (aliased_card, unaliased_card):
        lines = card.plain.split("\n")
        assert len(lines) == 5 + 12
        assert all(len(ln) == 21 for ln in lines)


def test_meter_card_stale_dims_bars_and_percent():
    from claude_swap.tui.widgets import meter_card
    from claude_swap.usage_store import STALE_OK_S

    now = time.time()
    stale = make_account(
        1,
        active=True,
        email="work@acme.dev",
        entry=make_entry(pct5=78.0, pct7=34.0, age_s=STALE_OK_S + 60),
    )
    fresh = make_account(
        2,
        active=True,
        email="work@acme.dev",
        entry=make_entry(pct5=78.0, pct7=34.0, age_s=5.0),
    )
    stale_card = meter_card(stale, 21, 5, now=now)
    fresh_card = meter_card(fresh, 21, 5, now=now)

    assert any("dim" in str(sp.style) for sp in stale_card.spans)
    assert not any("dim" in str(sp.style) for sp in fresh_card.spans)


def test_meter_card_flash_highlights_top_border():
    from claude_swap.tui.widgets import meter_card
    from claude_swap.tui.theme import ACCENT

    acc = make_account(
        1, active=True, email="work@acme.dev", entry=make_entry(pct5=78.0, pct7=34.0)
    )
    now = time.time()
    plain = meter_card(acc, 21, 5, now=now)
    flashed = meter_card(acc, 21, 5, now=now, flash=True)

    # flash never changes layout or text, only the top border's style
    assert plain.plain == flashed.plain
    lines = flashed.plain.split("\n")
    assert len(lines) == 5 + 12
    assert all(len(ln) == 21 for ln in lines)

    header_end = len(lines[0])
    flash_style = f"bold {ACCENT}"
    assert any(
        sp.start == 0 and sp.end == header_end and sp.style == flash_style
        for sp in flashed.spans
    )
    assert not any(
        sp.start == 0 and sp.end == header_end and sp.style == flash_style
        for sp in plain.spans
    )


def test_meters_grid_text_flash_marks_only_flashed_account():
    from claude_swap.tui.theme import ACCENT
    from claude_swap.tui.widgets import meters_grid_text

    accounts = _make_three_meter_accounts()
    flash_style = f"bold {ACCENT}"

    plain_out = meters_grid_text(accounts, 44, 31, now=time.time())
    assert not [sp for sp in plain_out.spans if sp.style == flash_style]

    flashed_out = meters_grid_text(
        accounts, 44, 31, now=time.time(), flashed={accounts[1].number}
    )
    assert flashed_out.plain == plain_out.plain  # flash never changes layout/text
    assert len([sp for sp in flashed_out.spans if sp.style == flash_style]) == 1


def test_fit_center_truncates_oversized_content():
    from claude_swap.tui.widgets import _fit_center

    long_label = "Anthropic-Claude-Max-Plan"
    for w in (1, 3, 4, 5):
        result = _fit_center(long_label, w)
        assert len(result) == w
    assert _fit_center(long_label, 4) == "Anth"


def test_grid_move_clamps_and_no_wrap():
    from claude_swap.tui.widgets import grid_move

    # 3 items, 2 cols -> layout [0,1 / 2]
    assert grid_move(0, 1, 0, 2, 3) == 1  # right
    assert grid_move(1, 1, 0, 2, 3) == 1  # right at edge: clamp (no wrap)
    assert grid_move(0, 0, 1, 2, 3) == 2  # down
    assert grid_move(0, -1, 0, 2, 3) == 0  # left at edge: clamp
    assert grid_move(2, 0, -1, 2, 3) == 0  # up
    # short last row: moving down from col 1 lands on the only item in row 1
    assert grid_move(1, 0, 1, 2, 3) == 2


def _make_three_meter_accounts() -> list[AccountSnapshot]:
    return [
        make_account(1, active=True, email="a@example.com", entry=make_entry(pct5=78.0, pct7=34.0)),
        make_account(2, email="b@example.com", entry=make_entry(pct5=50.0, pct7=10.0)),
        make_account(3, email="c@example.com", entry=make_entry(pct5=20.0, pct7=5.0)),
    ]


def test_meters_grid_text_two_columns():
    from claude_swap.tui.widgets import meters_grid_text

    accounts = _make_three_meter_accounts()
    out = meters_grid_text(accounts, 44, 31, now=time.time())
    lines = out.plain.split("\n")
    # two cards side by side on row 1: account 1 is active (double-line ╔),
    # account 2 is not (single-line rounded ╭).
    assert lines[0].count("╭") + lines[0].count("╔") == 2
    assert len(lines) <= 31  # the whole grid fits the device height


def test_meters_grid_text_rows_separated_by_blank_line():
    from claude_swap.tui.widgets import meter_grid_dims, meters_grid_text

    accounts = _make_three_meter_accounts()
    out = meters_grid_text(accounts, 44, 31, now=time.time())
    lines = out.plain.split("\n")
    _ncols, _cw, bar_height = meter_grid_dims(44, 31, len(accounts))
    card_lines = bar_height + 12
    assert lines[card_lines] == ""  # blank separator between card rows
    assert lines[card_lines + 1].count("╭") == 1  # lone third card on row 2


def _accent_cols_by_line(text) -> dict[int, set[int]]:
    """Per-line column offsets styled ACCENT, for pinpointing which card's
    border the cursor marked."""
    from claude_swap.tui.theme import ACCENT

    accent_offsets: set[int] = set()
    for sp in text.spans:
        if sp.style == ACCENT:
            accent_offsets.update(range(sp.start, sp.end))
    result: dict[int, set[int]] = {}
    offset = 0
    for i, line in enumerate(text.plain.split("\n")):
        cols = {o - offset for o in accent_offsets if offset <= o < offset + len(line)}
        if cols:
            result[i] = cols
        offset += len(line) + 1
    return result


def test_meters_grid_text_cursor_marks_selected_card():
    from claude_swap.tui.theme import ACCENT
    from claude_swap.tui.widgets import meters_grid_text, _SELECT_BG

    accounts = _make_three_meter_accounts()
    now = time.time()

    plain_out = meters_grid_text(accounts, 44, 31, now=now)
    marked0 = meters_grid_text(accounts, 44, 31, cursor=0, now=now)
    assert plain_out.plain == marked0.plain  # marking never changes layout/text

    # the selection highlight fills the selected card's background...
    assert any(_SELECT_BG in str(s.style) for s in marked0.spans)
    assert not any(_SELECT_BG in str(s.style) for s in plain_out.spans)
    # ...and gives its border a bold accent
    assert any("bold" in str(s.style) and ACCENT in str(s.style) for s in marked0.spans)


def test_meters_grid_text_empty_accounts():
    from claude_swap.tui.widgets import meters_grid_text

    out = meters_grid_text([], 44, 16, now=time.time())
    assert out.plain == "no accounts"


def test_meters_grid_text_falls_back_to_compact_when_cards_cannot_fit():
    from claude_swap.tui.widgets import meters_grid_text

    accounts = _make_three_meter_accounts()
    out = meters_grid_text(accounts, 30, 12, now=time.time())
    lines = out.plain.split("\n")
    assert len(lines) <= 12  # fits the viewport instead of clipping
    assert "╭" not in out.plain  # compact form, not framed cards
    for acc, line in zip(accounts, lines):
        assert line.lstrip().startswith(str(acc.number))


def test_meters_grid_text_fallback_cursor_marks_selected_line():
    from claude_swap.tui.widgets import meters_grid_text

    accounts = _make_three_meter_accounts()
    now = time.time()
    marked0 = meters_grid_text(accounts, 30, 12, cursor=0, now=now)
    marked1 = meters_grid_text(accounts, 30, 12, cursor=1, now=now)

    cols0 = _accent_cols_by_line(marked0)
    cols1 = _accent_cols_by_line(marked1)
    assert 0 in cols0 and 1 not in cols0  # cursor=0 accents only line 0
    assert 1 in cols1 and 0 not in cols1  # cursor=1 accents only line 1


def _snap_with_fetched_at(fetched_at: float) -> AccountsSnapshot:
    entry = dataclasses.replace(make_entry(pct5=50.0), fetched_at=fetched_at)
    return AccountsSnapshot(
        active_number="1",
        accounts=(make_account(1, active=True, entry=entry),),
        taken_at=time.time(),
    )


def test_meters_grid_flash_extends_on_reflash():
    # A re-flash of an already-flashing account must not be cleared early by
    # the earlier timer: the generation guard keeps the account flashed until
    # the LATEST timer fires.
    from claude_swap.tui.widgets import MetersGrid

    grid = MetersGrid()
    grid.set_timer = lambda *a, **k: None  # no live scheduling in a unit test
    grid.refresh = lambda *a, **k: None

    grid._flash_updated(_snap_with_fetched_at(1000.0))  # baseline, no flash
    assert grid._flash == set()

    grid._flash_updated(_snap_with_fetched_at(1001.0))  # first change -> gen 1
    assert "1" in grid._flash

    grid._flash_updated(_snap_with_fetched_at(1002.0))  # re-flash -> gen 2
    assert "1" in grid._flash

    # The FIRST timer fires with its stale generation: must NOT clear.
    grid._clear_flash("1", 1)
    assert "1" in grid._flash

    # The LATEST timer fires with the current generation: clears.
    grid._clear_flash("1", 2)
    assert "1" not in grid._flash
