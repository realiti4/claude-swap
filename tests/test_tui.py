"""Tests for the Textual TUI: data service units + Pilot-driven app tests.

The Pilot tests run the real app headlessly against a ``FakeSwitcher`` that
implements exactly the structured surface the TUI consumes
(``accounts_snapshot``, ``switch_to``/``switch``/``remove_account``/add
flows) — no scraping, no real credentials, no network.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from claude_swap.autoswitch import ConfigWarningEvent, NoSwitchEvent, SwitchEvent
from claude_swap.json_output import (
    USAGE_API_KEY,
    USAGE_KEYCHAIN_UNAVAILABLE,
    USAGE_TOKEN_EXPIRED,
)
from claude_swap.models import AccountSnapshot, AccountsSnapshot
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.tui import data as tui_data
from claude_swap.usage_store import STALE_OK_S, UsageEntry


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


def make_usage_at(
    fetched_at: float | None,
    pct: float = 25.0,
    *,
    sentinel: str | None = None,
) -> UsageEntry:
    return UsageEntry(
        sentinel=sentinel,
        last_good={"five_hour": {"pct": pct, "resets_at": _iso_in(7200)}},
        fetched_at=fetched_at,
        age_s=(time.time() - fetched_at) if fetched_at is not None else None,
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


class BlockingSnapshotSwitcher(FakeSwitcher):
    """Fake switcher with independently gated normal/store snapshot lanes."""

    def __init__(
        self,
        normal_account: AccountSnapshot,
        store_account: AccountSnapshot,
        backup_dir: Path,
    ):
        super().__init__([normal_account], backup_dir)
        self.normal_account = normal_account
        self.store_account = store_account
        self.normal_started = threading.Event()
        self.normal_release = threading.Event()
        self.normal_done = threading.Event()
        self.store_started = threading.Event()
        self.store_release = threading.Event()
        self.store_done = threading.Event()
        self.block_store = False

    def accounts_snapshot(self, fetch: set[str] | None = None) -> AccountsSnapshot:
        self.fetch_sets.append(fetch)
        if fetch is None:
            self.normal_started.set()
            self.normal_release.wait(timeout=2)
            self.normal_done.set()
            account = self.normal_account
        else:
            self.store_started.set()
            if self.block_store:
                self.store_release.wait(timeout=2)
            self.store_done.set()
            account = self.store_account
        return AccountsSnapshot(
            active_number=account.number,
            accounts=(account,),
            taken_at=time.time(),
        )


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


async def wait_event(event: threading.Event, timeout: float = 1.0) -> None:
    assert await asyncio.to_thread(event.wait, timeout)


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
            == "token expired — auto-refreshing on the next pass (≤1m); no action needed"
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
        assert "token expired — auto-refreshing on the next pass (≤1m); no action needed" in card
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
        # the store's per-account cadence.
        fake, source = self._source(tmp_path)
        source.take()
        source.take()
        source.take(full=True)
        assert fake.fetch_sets == [None, None, None]

    def test_store_only_never_fetches(self, tmp_path):
        fake, source = self._source(tmp_path)
        source.take(store_only=True)
        assert fake.fetch_sets == [set()]

    def test_expired_sentinel_retained_until_fetched_at_advances(self, tmp_path):
        expired = make_account(
            1,
            active=True,
            entry=make_usage_at(100.0, sentinel=USAGE_TOKEN_EXPIRED),
        )
        fresh_same_stamp = make_account(1, active=True, entry=make_usage_at(100.0))
        fresh_new_stamp = make_account(1, active=True, entry=make_usage_at(101.0))
        fake, source = self._source(tmp_path, [expired])

        assert source.take().accounts[0].usage.sentinel == USAGE_TOKEN_EXPIRED
        fake._accounts = [fresh_same_stamp]
        assert source.take(store_only=True).accounts[0].usage.sentinel == USAGE_TOKEN_EXPIRED
        fake._accounts = [fresh_new_stamp]
        assert source.take(store_only=True).accounts[0].usage.sentinel is None

    def test_expired_sentinel_clears_on_superseding_sentinel(self, tmp_path):
        expired = make_account(
            1,
            active=True,
            entry=make_usage_at(100.0, sentinel=USAGE_TOKEN_EXPIRED),
        )
        api_key = make_account(
            1,
            active=True,
            kind="api_key",
            entry=make_usage_at(None, sentinel=USAGE_API_KEY),
        )
        fake, source = self._source(tmp_path, [expired])

        source.take()
        fake._accounts = [api_key]
        assert source.take(store_only=True).accounts[0].usage.sentinel == USAGE_API_KEY

    def test_expired_sentinel_clears_on_identity_replacement(self, tmp_path):
        expired = make_account(
            1,
            active=True,
            email="old@example.com",
            entry=make_usage_at(100.0, sentinel=USAGE_TOKEN_EXPIRED),
        )
        replacement = make_account(
            1,
            active=True,
            email="new@example.com",
            entry=make_usage_at(100.0),
        )
        fake, source = self._source(tmp_path, [expired])

        source.take()
        fake._accounts = [replacement]
        assert source.take(store_only=True).accounts[0].usage.sentinel is None

    def test_late_worker_fetched_at_regression_is_rejected(self, tmp_path):
        newer = make_account(1, active=True, entry=make_usage_at(200.0, pct=80.0))
        older = make_account(1, active=True, entry=make_usage_at(100.0, pct=10.0))
        fake, source = self._source(tmp_path, [newer])

        source.take()
        fake._accounts = [older]
        snap = source.take(store_only=True)
        usage = snap.accounts[0].usage
        assert usage.fetched_at == 200.0
        assert usage.last_good["five_hour"]["pct"] == 80.0

    def test_late_expired_sentinel_cannot_replace_newer_usage(self, tmp_path):
        newer = make_account(1, active=True, entry=make_usage_at(200.0, pct=80.0))
        older = make_account(
            1,
            active=True,
            entry=make_usage_at(100.0, pct=10.0, sentinel=USAGE_TOKEN_EXPIRED),
        )
        fake, source = self._source(tmp_path, [newer])

        source.take()
        fake._accounts = [older]
        usage = source.take(store_only=True).accounts[0].usage
        assert usage.sentinel is None
        assert usage.fetched_at == 200.0
        assert usage.last_good["five_hour"]["pct"] == 80.0


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

    def test_seven_day_with_no_reported_reset_shows_reset_unknown(self):
        """The card must name the gap, not go blank, when a probe candidate's
        weekly reset has never been reported -- the visible half of the
        unknown-reset feature (the decision log carries the other half)."""
        from claude_swap.tui.widgets import usage_rows

        now = time.time()
        last_good = {"seven_day": {"pct": 10.0}}  # no resets_at at all
        row = usage_rows(last_good, now)[0]
        assert row[2] == "reset unknown", row
        assert row[3] == "reset unknown", row

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

    def test_window_reads_the_same_as_the_auto_views_chip(self):
        """One account must not read two ways on two screens.

        The dashboard rendered `5h 100% (resets 2h 28m)` while the auto view
        rendered `5h(⟳2h28m):100%` for the same window in the same second.
        Both now come from data.chip_label, so a change to one surface
        cannot silently diverge from the other.
        """
        from claude_swap.tui import data
        from claude_swap.tui.widgets import mini_account_text

        now = time.time()
        # +1s so the truncating duration format cannot land on 2h27m when the
        # render happens a hair after _iso_in computed the deadline.
        last_good = {
            "five_hour": {"pct": 100.0, "resets_at": _iso_in(3600 * 2 + 1680 + 1)}
        }
        acc = make_account(
            1, entry=UsageEntry(last_good=last_good, fetched_at=now, age_s=0.0)
        )
        chip = data.chip_label("5h", data.reset_text(last_good["five_hour"], now))
        assert chip == "5h(⟳2h28m):"
        assert f"{chip}100%" in mini_account_text(acc, now).plain

    @pytest.mark.parametrize(
        "age_s, expect_dim",
        [(5.0, False), (STALE_OK_S + 100, True)],
        ids=["fresh", "stale"],
    )
    def test_a_spend_only_account_shows_spend_not_usage_unknown(
        self, age_s, expect_dim
    ):
        """PROBE: the same defect `TestUnswitchableRowsAreListed` fixed on the
        auto view, on the dashboard's mini line.

        An extra-usage (pay-as-you-go) account has neither a 5h nor a 7d
        window — only `spend` — so this loop found nothing and fell through to
        "usage unknown", while `usage_rows` IN THIS FILE rendered `$$ 51%
        $10.29 / $50.00` for the same `last_good` in the same second. One
        account must not read two ways on two screens.

        Also covers staleness: every other pct in this file dims once the
        measurement is older than `STALE_OK_S` (`account_card_text` dims the
        very same `$$` row on the card for this same account), so the mini
        line's spend pct must too — an undimmed reading asserts a freshness
        the code never checked.

        Display only: spend is a budget, not rate-limit headroom, and nothing
        here feeds a ranking.
        """
        from claude_swap.tui.widgets import mini_account_text

        now = time.time()
        entry = make_entry(
            pct5=None, pct7=None, age_s=age_s,
            spend={"used": 10.29, "limit": 20.0, "pct": 51.45, "currency": "USD"},
        )
        text = mini_account_text(make_account(1, entry=entry), now)
        out = text.plain
        assert "usage unknown" not in out, (
            f"a spend-only account still reads as unknown: {out!r}"
        )
        assert "51%" in out, out
        assert "$10.29" in out and "$20.00" in out, out
        pct_span = next(s for s in text.spans if out[s.start : s.end] == "51%")
        assert ("dim" in str(pct_span.style)) == expect_dim, (
            f"age_s={age_s}: expected dim={expect_dim}, style={pct_span.style!r}"
        )

    def test_CONTROL_no_windows_and_no_spend_still_says_unknown(self):
        """CONTROL for the probe: "usage unknown" is still the right answer
        with genuinely nothing to show. Deleting the phrase would pass the row
        above and lose the real signal."""
        from claude_swap.tui.widgets import mini_account_text

        now = time.time()
        entry = make_entry(pct5=None, pct7=None)
        out = mini_account_text(make_account(1, entry=entry), now).plain
        assert "usage unknown" in out, (
            f"CONTROL BROKEN: an account with no usage stopped saying so: {out!r}"
        )

    @pytest.mark.parametrize(
        "age_s,expect_dim",
        [(5.0, False), (STALE_OK_S + 100, True)],
        ids=["fresh", "stale"],
    )
    def test_scoped_only_account_below_the_cap_is_shown_not_usage_unknown(
        self, age_s, expect_dim
    ):
        """PROBE: the mini line's maxed-scoped loop only fires at/over 100%,
        so an account whose only window is a per-model (e.g. Fable) limit
        below its cap fell all the way through to "usage unknown" — while
        `account_card_text` renders the same `Fable 99%` row from the same
        `usage_rows` one screen over. Same rendering gap `c209903` closed for
        spend, left open for scoped.

        Staleness rides the same axis as the spend row above: this branch is
        the OTHER place a pct is emitted, and an undimmed reading asserts a
        freshness the code never checked.
        """
        from claude_swap.tui.widgets import mini_account_text

        now = time.time()
        entry = make_entry(
            pct5=None, pct7=None, age_s=age_s, scoped=[("Fable", 99.0)]
        )
        text = mini_account_text(make_account(1, entry=entry), now)
        out = text.plain
        assert "usage unknown" not in out, (
            f"a scoped-only account below its cap still reads as unknown: {out!r}"
        )
        assert "Fable" in out and "99%" in out, out
        pct_span = next(s for s in text.spans if out[s.start : s.end] == "99%")
        assert ("dim" in str(pct_span.style)) == expect_dim, (
            f"age_s={age_s}: expected dim={expect_dim}, style={pct_span.style!r}"
        )

    def test_spend_shows_alongside_a_healthy_window_not_hidden_behind_it(self):
        """PROBE: the spend row only rendered inside `if not parts:`, so a
        95%-spent budget vanished behind ANY healthy 5h/7d window — the mini
        line said "5h:10%" and nothing else, while the card shows both rows
        for the same account. Spend is a separate axis from a rate-limit
        window (never enters the ranking), so hiding it behind one is not a
        real precedence, just an accident of the fallback's shape.
        """
        from claude_swap.tui.widgets import mini_account_text

        now = time.time()
        entry = make_entry(
            pct5=10.0, pct7=None,
            spend={"used": 19.0, "limit": 20.0, "pct": 95.0, "currency": "USD"},
        )
        out = mini_account_text(make_account(1, entry=entry), now).plain
        assert "10%" in out, out
        assert "95%" in out, (
            f"a 95%-spent budget vanished behind a healthy window: {out!r}"
        )
        assert "10% \u00b7 $$" in out, (
            f"the window and the spend row ran together: {out!r}"
        )

    def test_countdown_shows_below_100_too(self):
        """A window's worth IS when it comes back, which is exactly what you
        compare while picking an account — so it is not hidden until 100%."""
        from claude_swap.tui.widgets import mini_account_text

        now = time.time()
        last_good = {"five_hour": {"pct": 42.0, "resets_at": _iso_in(3600 + 1)}}
        acc = make_account(
            1, entry=UsageEntry(last_good=last_good, fetched_at=now, age_s=0.0)
        )
        assert "5h(⟳1h):42%" in mini_account_text(acc, now).plain

    def test_scoped_window_below_100_shows_its_pct_alongside_5h_7d(self):
        """PROBE: the scoped loop only fires at/over 100 (`maxed`), so once a
        5h/7d window already rendered (`parts` nonzero) a scoped window below
        its cap never reaches the dashboard row at all — it is not the
        `usage unknown` fallback catching it either, since 5h/7d already
        produced output. An account sitting at 91% on a per-model window
        reads as if that window does not exist."""
        from claude_swap.tui.widgets import mini_account_text

        now = time.time()
        last_good = {
            "five_hour": {"pct": 28.0},
            "seven_day": {"pct": 70.0},
            "scoped": [{"name": "Fable", "pct": 91.0}],
        }
        acc = make_account(
            1, entry=UsageEntry(last_good=last_good, fetched_at=now, age_s=0.0)
        )
        out = mini_account_text(acc, now).plain
        assert "Fable" in out and "91%" in out, (
            f"a scoped window below 100 vanished from the dashboard row: {out!r}"
        )

    def test_scoped_window_at_100_keeps_its_marker_and_shows_pct(self):
        from claude_swap.tui.widgets import mini_account_text

        now = time.time()
        last_good = {
            "five_hour": {"pct": 28.0},
            "seven_day": {"pct": 70.0},
            "scoped": [{"name": "Fable", "pct": 100.0}],
        }
        acc = make_account(
            1, entry=UsageEntry(last_good=last_good, fetched_at=now, age_s=0.0)
        )
        out = mini_account_text(acc, now).plain
        assert "Fable" in out and "100%" in out and "(!)" in out, out


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
            # The window reads as one chip now — "5h(⟳1h59m):92%" — built by
            # the same helper the auto view uses. Assert the parts that carry
            # the meaning (which window, what pct), not the spacing between
            # them, so the two surfaces can keep sharing one format.
            assert "5h(" in mini_part and ":92%" in mini_part
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
                # No "pin-menu": the cloud pin row appears only when the
                # optional extra is installed, which it is not in CI.
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

    async def test_blocked_normal_allows_store_only_repaint_without_stale_overpaint(
        self, tmp_path
    ):
        normal = make_account(1, active=True, entry=make_usage_at(100.0, pct=10.0))
        store = make_account(1, active=True, entry=make_usage_at(200.0, pct=80.0))
        fake = BlockingSnapshotSwitcher(normal, store, tmp_path)
        app = make_app(fake)

        async with app.run_test(size=(100, 40)) as pilot:
            await wait_event(fake.normal_started)
            app._tick()
            await wait_event(fake.store_done)
            await pilot.pause()
            assert app.snapshot.accounts[0].usage.last_good["five_hour"]["pct"] == 80.0

            fake.normal_release.set()
            await wait_event(fake.normal_done)
            await pilot.pause()
            assert app.snapshot.accounts[0].usage.last_good["five_hour"]["pct"] == 80.0
            assert fake.fetch_sets == [None, set()]

    async def test_late_normal_can_advance_usage_after_store_repaint(self, tmp_path):
        normal = make_account(1, active=True, entry=make_usage_at(200.0, pct=80.0))
        store = make_account(1, active=True, entry=make_usage_at(100.0, pct=10.0))
        fake = BlockingSnapshotSwitcher(normal, store, tmp_path)
        app = make_app(fake)

        async with app.run_test(size=(100, 40)) as pilot:
            await wait_event(fake.normal_started)
            app._tick()
            await wait_event(fake.store_done)
            await pilot.pause()
            assert app.snapshot.accounts[0].usage.last_good["five_hour"]["pct"] == 10.0

            fake.normal_release.set()
            await wait_event(fake.normal_done)
            await pilot.pause()
            assert app.snapshot.accounts[0].usage.last_good["five_hour"]["pct"] == 80.0

    async def test_repeated_ticks_keep_store_lane_single_flight(self, tmp_path):
        normal = make_account(1, active=True, entry=make_usage_at(100.0, pct=10.0))
        store = make_account(1, active=True, entry=make_usage_at(200.0, pct=80.0))
        fake = BlockingSnapshotSwitcher(normal, store, tmp_path)
        fake.block_store = True
        app = make_app(fake)

        async with app.run_test(size=(100, 40)):
            await wait_event(fake.normal_started)
            app._tick()
            await wait_event(fake.store_started)
            app._tick()
            app._tick()
            assert fake.fetch_sets == [None, set()]
            fake.store_release.set()
            fake.normal_release.set()
            await wait_event(fake.store_done)
            await wait_event(fake.normal_done)

    async def test_store_only_mode_launches_only_store_lane(self, tmp_path):
        fake = self._fake(tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            fake.fetch_sets.clear()
            app.set_store_only(True)
            await settle(pilot)
            assert fake.fetch_sets == [set()]

    async def test_watch_title_shows_snapshot_age_and_long_refresh(self, tmp_path):
        app = make_app(self._fake(tmp_path))
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await pilot.press("w")
            await pilot.pause()
            from textual.widgets import Static

            title = app.screen.query_one("#list-title", Static)
            # Fresh snapshots stay quiet; the age note is a staleness alarm.
            assert "snapshot" not in title.render().plain
            app.snapshot = dataclasses.replace(
                app.snapshot, taken_at=time.time() - app.SNAPSHOT_AGE_NOTE_S - 1.0
            )
            app._update_refresh_status()
            await pilot.pause()
            assert "snapshot 1m ago" in title.render().plain
            app._normal_refreshing = True
            app._normal_started_at = time.time() - app.POLL_INTERVAL_S - 1.0
            app._update_refresh_status()
            await pilot.pause()
            assert "refreshing" in title.render().plain


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
        self.applied_strategies: list[str] = []
        self.wakes = 0
        self._stop = threading.Event()
        # Mirrors AutoSwitchEngine's own cached probe-cooldown attribute
        # (see its docstring) -- `_candidates_text` reads it straight off
        # `self._engine`, and a real screen mount reaches that read before
        # any real tick would populate it.
        self._last_probe_cooldown: dict[str, float] = {}
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

    def apply_strategy(self, strategy: str) -> None:
        self.settings = dataclasses.replace(self.settings, strategy=strategy)
        self.applied_strategies.append(strategy)

    def wake(self) -> None:
        self.wakes += 1


@pytest.fixture
def fake_engine(monkeypatch):
    _FakeEngine.instances = []
    monkeypatch.setattr(
        "claude_swap.tui.autoview.AutoSwitchEngine", _FakeEngine
    )
    return _FakeEngine


class _ContendedFakeEngine:
    """Stands in for AutoSwitchEngine, but ALWAYS starts demoted regardless
    of the requested ``dry_run`` -- simulating a second engine that lost the
    LIVE lock to a holder already running. ``promote()`` then simulates
    ``_retry_live_promotion`` succeeding once the holder exits: flips
    ``dry_run``/``demoted_from_live`` and emits the same event kind
    (``config-warning``) the real method does, with NO further human action
    -- exactly what I1 is about.
    """

    instances: list["_ContendedFakeEngine"] = []

    def __init__(self, switcher, settings, on_event, *, dry_run=False, **kwargs):
        self.settings = settings
        self.on_event = on_event
        self.dry_run = True                 # always demoted on construction
        self.demoted_from_live = True
        self.stopped = False
        self._stop = threading.Event()
        self._promote_requested = threading.Event()
        # Mirrors _FakeEngine's `_last_probe_cooldown` -- the panel reads it
        # off `self._engine` on every store-only snapshot, and a mount that
        # reaches that read once this engine is live raised AttributeError
        # (swallowed as a "Store refresh failed" worker notification,
        # freezing the candidates panel on a stale render) until this line.
        self._last_probe_cooldown: dict[str, float] = {}
        _ContendedFakeEngine.instances.append(self)

    def run_loop(self) -> int:
        # `on_event` -- like the real engine's -- must run from THIS worker
        # thread: `_emit_from_thread` reaches it via Textual's
        # `call_from_thread`, which raises RuntimeError (silently swallowed)
        # when called from the app's own thread. `promote()` merely flags
        # the request from the test's thread; the actual emit happens here,
        # matching where the real `_retry_live_promotion` runs.
        self.on_event(NoSwitchEvent(reason="cooldown"))
        while not self._stop.is_set():
            if self._promote_requested.wait(0.05):
                self._promote_requested.clear()
                self.dry_run = False
                self.demoted_from_live = False
                self.on_event(
                    ConfigWarningEvent(
                        message="the LIVE holder released the lock — this "
                                "engine is now LIVE"
                    )
                )
        return 0

    def stop(self) -> None:
        self.stopped = True
        self._stop.set()

    def apply_threshold(self, threshold: float) -> None:
        pass

    def apply_strategy(self, strategy: str) -> None:
        pass

    def wake(self) -> None:
        pass

    def promote(self) -> None:
        self._promote_requested.set()

    def wait_promoted(self, timeout: float = 1.0) -> bool:
        """Block until `run_loop`'s worker thread has flipped `dry_run`."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.dry_run:
                return True
            time.sleep(0.01)
        return not self.dry_run


@pytest.fixture
def contended_fake_engine(monkeypatch):
    _ContendedFakeEngine.instances = []
    monkeypatch.setattr(
        "claude_swap.tui.autoview.AutoSwitchEngine", _ContendedFakeEngine
    )
    return _ContendedFakeEngine


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

    async def test_a_promoted_engine_updates_the_badge(
        self, tmp_path, contended_fake_engine
    ):
        """The engine PROMOTES itself mid-run, with no human action.

        Nothing else re-reads `dry_run` after mount, so without the refresh in
        `_on_engine_event` the badge keeps reading DRY-RUN over an engine that
        is now switching accounts -- worse than the stuck-dry-run it fixes,
        because the display then contradicts what is happening. Deleting that
        one call left the suite green: `contended_fake_engine` was built for
        exactly this and no case used it.
        """
        from textual.widgets import Static

        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._open(pilot)
            engine = contended_fake_engine.instances[0]
            badge = app.screen.query_one("#mode-badge", Static)
            assert badge.has_class("dry"), (
                "premise: the screen did not open demoted, so a later LIVE "
                "badge would prove nothing"
            )

            engine.promote()
            assert engine.wait_promoted(), "premise: the engine never promoted"
            await settle(pilot)

            assert badge.has_class("live"), (
                "the badge still reads DRY-RUN over an engine that is now "
                "LIVE and switching accounts"
            )

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

    async def test_strategy_cycle_is_session_only(self, tmp_path, fake_engine):
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._open(pilot)
            screen = app.screen
            assert screen._settings.strategy == "consume-first"  # the default
            from textual.widgets import Static

            summary = screen.query_one("#auto-summary", Static)
            assert "consume-first" in summary.render().plain
            await pilot.press("s")
            await pilot.pause()
            assert screen._settings.strategy == "dynamic"
            engine = fake_engine.instances[0]
            assert engine.applied_strategies == ["dynamic"]
            assert engine.wakes == 1  # a forced tick shows the new strategy
            assert "dynamic (session)" in summary.render().plain
            await pilot.press("s")
            await pilot.pause()
            assert screen._settings.strategy == "best"
            await pilot.press("s")
            await pilot.pause()
            assert screen._settings.strategy == "consume-first"  # wraps around
            # the override lives in memory only — nothing was persisted
            assert not (tmp_path / "settings.json").exists()
            await pilot.press("escape")
            await settle(pilot)
            # the session strategy does not outlive the screen: a fresh open
            # reverts to the file value, same precedent as the threshold.
            await self._open(pilot)
            assert app.screen._settings.strategy == "consume-first"

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
        import json as _json

        (tmp_path / "settings.json").write_text(_json.dumps({
            "schemaVersion": 1, "autoswitch": {"strategy": "best"},
        }))
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
            "schemaVersion": 1,
            "autoswitch": {"model": "Fable", "strategy": "best"},
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

    async def test_candidates_drop_the_model_gate_when_every_row_is_model_only(
        self, tmp_path, fake_engine
    ):
        """Both candidates' ONLY over-bar window is the pinned model, exactly
        the shape ``_rank_candidates`` (autoswitch.py) retries on 5h/7d alone
        for — but that retry is `dynamic`-only (autoswitch.py:2400): `best`/
        `consume-first` never rank on 5h/7d alone, so under those strategies
        the panel must NOT drop the model gate either, even here. `strategy`
        must be `dynamic` for the panel to take this path at all — ranking on
        the model-gated axis here would name #2 (95% Fable) the worse account
        when its real 5h is #3's better one: the two are on OPPOSITE sides of
        `-Fable, +5h` vs `+Fable, -5h`."""
        import json as _json

        (tmp_path / "settings.json").write_text(_json.dumps({
            "schemaVersion": 1,
            "autoswitch": {"model": "Fable", "strategy": "dynamic", "threshold": 90},
        }))
        fake = FakeSwitcher(
            [
                make_account(1, active=True, entry=make_entry(91.0, 20.0)),
                make_account(
                    2, entry=make_entry(20.0, 5.0, scoped=[("Fable", 95.0)])
                ),
                make_account(
                    3, entry=make_entry(60.0, 5.0, scoped=[("Fable", 90.0)])
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
            # #2's real 5h (20%) beats #3's (60%): once the model gate drops
            # (neither #2 nor #3 has a 5h/7d window over the bar on its own),
            # #2 must rank first — ranking on the model-gated axis instead
            # would put #3 first (90% Fable < 95% Fable).
            assert plain.index("user2@example.com") < plain.index(
                "user3@example.com"
            )

    async def test_candidates_keep_the_model_gate_under_best_even_when_every_row_is_model_only(
        self, tmp_path, fake_engine
    ):
        """The regression this gate exists to stop: same fleet as the test
        above (both candidates blocked ONLY by the pinned model), but
        `strategy: "best"` — the engine's own retry never drops the model
        set for `best`/`consume-first` (autoswitch.py:2400), so the panel
        must not either. Ranking stays on the model-gated axis: #3 (90%
        Fable) ranks first, the OPPOSITE order from the `dynamic` test
        above."""
        import json as _json

        (tmp_path / "settings.json").write_text(_json.dumps({
            "schemaVersion": 1,
            "autoswitch": {"model": "Fable", "strategy": "best", "threshold": 90},
        }))
        fake = FakeSwitcher(
            [
                make_account(1, active=True, entry=make_entry(91.0, 20.0)),
                make_account(
                    2, entry=make_entry(20.0, 5.0, scoped=[("Fable", 95.0)])
                ),
                make_account(
                    3, entry=make_entry(60.0, 5.0, scoped=[("Fable", 90.0)])
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
            assert plain.index("user3@example.com") < plain.index(
                "user2@example.com"
            )

    async def test_candidates_drain_soonest_seven_day_reset_first(
        self, tmp_path, fake_engine
    ):
        """Under the default (consume-first) strategy, 'Next best' lists
        switchable accounts in the order the engine would actually switch to
        them: soonest 7-day reset first, so quota is spent before it resets
        and goes to waste. The active's 5-hour window stays a gate, never a
        key -- it plays no part in this order."""
        import json as _json

        (tmp_path / "settings.json").write_text(_json.dumps({
            "schemaVersion": 1, "autoswitch": {"threshold": 90},
        }))

        def _entry(pct7: float, reset7_s: float, pct5: float | None = None) -> UsageEntry:
            if pct5 is None:
                pct5 = pct7 - 10.0  # seeded below 7d: immaterial to the order
            last_good = {
                "five_hour": {"pct": pct5, "resets_at": _iso_in(7200)},
                "seven_day": {"pct": pct7, "resets_at": _iso_in(reset7_s)},
            }
            return UsageEntry(last_good=last_good, fetched_at=time.time() - 5.0, age_s=5.0)

        fake = FakeSwitcher(
            [
                make_account(  # active: 5h 38% resets 55m, 7d 53% resets 1d18h
                    4, active=True,
                    entry=UsageEntry(
                        last_good={
                            "five_hour": {"pct": 38.0, "resets_at": _iso_in(3300)},
                            "seven_day": {"pct": 53.0, "resets_at": _iso_in(151200)},
                        },
                        fetched_at=time.time() - 5.0, age_s=5.0,
                    ),
                ),
                make_account(6, entry=_entry(37.0, 532800, pct5=0.0)),
                make_account(3, entry=_entry(44.0, 201600)),
                make_account(2, entry=_entry(62.0, 309600)),
                make_account(5, entry=_entry(42.0, 108000)),
                make_account(1, entry=_entry(62.0, 414000)),  # cloud/OAuth slot
                make_account(
                    7, kind="api_key",
                    entry=make_entry(
                        pct5=None, pct7=None,
                        spend={"used": 1.0, "limit": 100.0, "pct": 1.0, "currency": "USD"},
                    ),
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
            positions = [
                plain.index(f"user{n}@example.com") for n in ("5", "3", "2", "1", "6", "7")
            ]
            assert positions == sorted(positions), plain


class TestEventText:
    def test_switch_event_styling_and_content(self):
        event = SwitchEvent(
            trigger="proactive",
            from_ref={"number": 1, "email": "a@x.com"},
            to_ref={"number": 2, "email": "b@x.com"},
        )
        from claude_swap.tui.autoview import event_text

        assert event.human() in event_text(event).plain

    def test_a_deliberate_wait_is_not_painted_as_an_exhausted_fleet(self):
        """`_EVENT_ROLES` keys on the KIND, and one kind carries two states.

        `sev_crit` is the fifth surface saying "exhausted" about a hold whose
        own gate proves every candidate was READ and one still holds quota.

        This case builds the event directly, so it cannot witness that gate;
        `test_a_readable_peer_with_room_does_not_excuse_an_unread_one` is the
        one that does.
        """
        from claude_swap.autoswitch import AllExhaustedEvent
        from claude_swap.tui.autoview import event_text

        wait = AllExhaustedEvent(earliest_reset_at=None, deliberate_wait=True)
        real = AllExhaustedEvent(earliest_reset_at=None, deliberate_wait=False)
        styles = lambda e: {str(s.style) for s in event_text(e).spans}
        assert styles(wait) != styles(real), (
            "a deliberate hold is painted exactly like an exhausted fleet: "
            f"{styles(wait)}"
        )

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

        def fake_run(switcher, start="dashboard"):
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



class TestTheAutoFlagIsTheOnlyRouteToLive:
    """`cswap tui --auto` is the one thing that starts a LIVE engine.

    A bare `cswap tui` lands on the dashboard, and reaching the auto view
    from the menu watches without switching — opening a view must never
    begin switching accounts. That the menu route starts dry-run is asserted
    by `TestAutoScreen::test_opens_in_dry_run_and_store_only`, which drives
    the real keypress; these two cover the flag's own halves.

    A persisted `autoStartLive` setting used to override this, so one
    confirmed "Go live" made every later launch switch accounts unasked, on
    every machine sharing settings.json. The setting is gone.
    """

    @pytest.mark.asyncio
    async def test_a_bare_launch_lands_on_the_dashboard_with_no_engine(
        self, tmp_path, fake_engine
    ):
        from claude_swap.tui.dashboard import DashboardScreen

        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)  # default start="dashboard", no --auto
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            assert isinstance(app.screen, DashboardScreen)
            assert fake_engine.instances == [], (
                "a bare launch constructed the auto-switch engine"
            )

    @pytest.mark.asyncio
    async def test_the_auto_flag_opens_the_view_and_starts_live(
        self, tmp_path, fake_engine
    ):
        """Both halves in one: `--auto` must SHOW the auto view (not merely
        construct-and-never-push it) and the engine it starts must be LIVE.
        Splitting these let a mutation that dropped the `push_screen` call
        survive — the constructed-engine assertion passed on its own."""
        from claude_swap.tui.app import CswapApp
        from claude_swap.tui.autoview import AutoScreen

        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = CswapApp(fake, start="auto")
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            assert isinstance(app.screen, AutoScreen)
            assert fake_engine.instances, "no engine was constructed"
            assert fake_engine.instances[-1].dry_run is False, (
                "--auto did not start a LIVE engine"
            )

class TestUnswitchableRowsAreListed:
    """A slot you cannot switch to must still appear, with the reason.

    It used to be filtered out of "Next best" entirely. On a machine that
    had imported the account roster but not the credentials — which is the
    normal state right after a sync, since credentials deliberately do not
    travel — the auto view showed two accounts while the engine's own log
    line listed five. An absent row reads as "not configured"; a row that
    says why reads as "here is what to do".
    """

    def _snap(self, *accounts):
        from claude_swap.models import AccountsSnapshot
        return AccountsSnapshot(
            accounts=list(accounts), active_number=None, taken_at=0.0
        )

    def _acct(self, number, email, *, switchable, kind="oauth", last_good=None,
              sentinel=None, usage=None):
        from unittest.mock import MagicMock
        a = MagicMock()
        a.number, a.email, a.switchable, a.kind = number, email, switchable, kind
        if usage is not None:
            a.usage = usage
        else:
            # A real UsageEntry, not a MagicMock -- `.decision_value()` is
            # real code, not an auto-mocked callable, and needs actual
            # `sentinel`/`last_good`/`age_s` to answer correctly. `age_s=0.0`
            # reads as freshly-fetched, matching every test here that sets
            # only `last_good`/`sentinel` and has no opinion on staleness; a
            # test that DOES care passes `usage=` with its own `age_s`.
            a.usage = UsageEntry(
                sentinel=sentinel, last_good=last_good,
                fetched_at=time.time(), age_s=0.0,
            )
        return a

    def _render(self, snap, active, *, settings=None, engine=None):
        from unittest.mock import MagicMock, patch
        from claude_swap.tui.autoview import AutoScreen
        from claude_swap.settings import AutoSwitchSettings

        v = AutoScreen.__new__(AutoScreen)
        v._settings = settings or AutoSwitchSettings()
        v._engine = engine
        from claude_swap.tui.theme import CSWAP_DARK
        app = MagicMock()
        app.current_theme = CSWAP_DARK      # Palette.from_theme reads real fields
        with patch.object(AutoScreen, "app", property(lambda s: app)):
            return str(v._candidates_text(snap, active_number=active))

    def test_a_credential_less_slot_is_shown_with_what_to_do(self):
        out = self._render(self._snap(
            self._acct("1", "a@x.com", switchable=True),
            self._acct("4", "new@x.com", switchable=False),
        ), active="1")
        assert "new@x.com" in out, "the slot must not be hidden"
        # Naming the state is not enough — "no credentials" leaves the user
        # to guess, and the obvious guess (/login right where you are) writes
        # the login to whatever slot is active instead of this one.
        assert "switch here" in out
        assert "log in" in out

    def test_an_api_key_slot_says_api_key_not_re_login(self):
        out = self._render(self._snap(
            self._acct("1", "a@x.com", switchable=True),
            self._acct("5", "console-api@token.local",
                       switchable=False, kind="api_key"),
        ), active="1")
        assert "console-api@token.local" in out
        assert "API key" in out
        # There is no login to restore for an API key slot.
        assert "cswap add" not in out

    def test_an_api_key_slot_says_api_key_even_behind_a_locked_keychain(self):
        """CONTROL for the probe below: consulting the sentinel must not let it
        overrule `kind`.

        `dashboard.py`'s pin-menu comment records the measured divergence — an
        API-key slot behind a locked macOS keychain derives
        USAGE_KEYCHAIN_UNAVAILABLE — and `kind` is the fact the CLI and set_pin
        refuse on. "try again" is wrong advice for a slot that has no login to
        come back to, however many times you retry.
        """
        out = self._render(self._snap(
            self._acct("1", "a@x.com", switchable=True),
            self._acct("5", "console-api@token.local", switchable=False,
                       kind="api_key", sentinel=USAGE_KEYCHAIN_UNAVAILABLE),
        ), active="1")
        assert "API key" in out, (
            f"the sentinel overruled `kind` — the divergence dashboard.py's "
            f"pin menu documents: {out!r}"
        )
        assert "keychain" not in out, out

    def test_an_unreadable_slot_says_keychain_not_no_stored_login(self):
        """PROBE for the two rows above: a slot whose backup EXISTS but could
        not be read right now (locked keychain, no GUI session) is unswitchable
        for a different reason, and its own sentinel already says which.

        This arm never consulted it, so the row printed the `no stored login —
        switch here, then log in (`cswap add` …)` advice, and taking it burns a
        working stored grant by overwriting it with whatever is live. The same
        dead end `switcher.py`'s `_static_usage_sentinel` comment says was
        removed from three other sites; this arm was a fourth.
        """
        out = self._render(self._snap(
            self._acct("1", "a@x.com", switchable=True),
            self._acct("4", "locked@x.com", switchable=False,
                       sentinel=USAGE_KEYCHAIN_UNAVAILABLE),
        ), active="1")
        assert "locked@x.com" in out
        assert "keychain unavailable" in out, (
            f"the real sentinel was shadowed by the hardcoded pair: {out!r}"
        )
        assert "cswap add" not in out, (
            f"advice that overwrites a good stored credential: {out!r}"
        )

    def test_a_spend_only_account_shows_its_spend_not_usage_unknown(self):
        """An extra-usage (pay-as-you-go) account has no 5h/7d window, so the
        binding-window helper answers None and the row read "usage unknown"
        while the watch screen showed `$$ 51%  $10.29 / $20.00` for the same
        account from the same `last_good`. One account cannot read two ways.

        `relevant_windows` excludes `spend` deliberately — it is a separate
        axis from a rate-limit window and must not enter the ranking — so this
        is a RENDERING gap, not a missing window. The row stays sorted last.
        """
        out = self._render(self._snap(
            self._acct("1", "a@x.com", switchable=True),
            self._acct("6", "paid@x.com", switchable=True, last_good={
                "spend": {"pct": 51.45, "used": 10.29, "limit": 20.0},
            }),
        ), active="1")
        assert "usage unknown" not in out, (
            f"a spend-only account still reads as unknown: {out!r}"
        )
        assert "$10.29" in out and "$20.00" in out, out
        assert "51%" in out, out

    def test_spend_does_not_enter_the_ranking(self):
        """Showing spend must not make it a sort key. Spend is a budget, not
        rate-limit headroom, and `relevant_windows` excludes it from every
        decision — a spend-only account ranks last whatever its percentage,
        or the display would quietly change which account the engine picks.

        Measured by the row ORDER: a 1%-spent account still sorts behind a
        95%-used oauth account, which it would overtake on any spend-aware key.
        """
        out = self._render(self._snap(
            self._acct("1", "a@x.com", switchable=True),
            self._acct("2", "busy@x.com", switchable=True, last_good={
                "five_hour": {"pct": 95.0}, "seven_day": {"pct": 95.0},
            }),
            self._acct("6", "cheap@x.com", switchable=True, last_good={
                "spend": {"pct": 1.0, "used": 0.2, "limit": 20.0},
            }),
        ), active="1")
        assert out.index("busy@x.com") < out.index("cheap@x.com"), (
            f"spend entered the ranking — a barely-spent account outranked a "
            f"95%-used one: {out!r}"
        )

    def test_CONTROL_an_account_with_no_usage_at_all_still_says_unknown(self):
        """The control: "usage unknown" is still the right answer when there
        is genuinely nothing to show. A fix that removes the phrase outright
        would pass the row above and lose the real signal."""
        out = self._render(self._snap(
            self._acct("1", "a@x.com", switchable=True),
            self._acct("7", "silent@x.com", switchable=True),
        ), active="1")
        assert "usage unknown" in out, (
            f"CONTROL BROKEN: an account with no usage stopped saying so: {out!r}"
        )

    def test_unswitchable_rows_sort_last(self):
        out = self._render(self._snap(
            self._acct("4", "empty@x.com", switchable=False),
            self._acct("1", "a@x.com", switchable=True),
        ), active="9")
        assert out.index("a@x.com") < out.index("empty@x.com")

    def test_the_first_chip_starts_at_the_same_column_across_rows(self):
        """Each row's content must start where the widest email in this
        block ends, not where its own email ends — otherwise a long email
        pushes its row's content far right while short ones sit left, and
        the columns never line up. This must hold at BOTH sites that pad
        the email: the switchable rows (chips) and the unswitchable row
        (a sentinel note instead), which are two separate append sites in
        the panel and can drift out of alignment independently.
        """
        import re

        out = self._render(self._snap(
            self._acct("2", "brief@x.com", switchable=True, last_good={
                "five_hour": {"pct": 0.0}, "seven_day": {"pct": 0.0},
            }),
            self._acct("3", "a-much-longer-address@example.com",
                       switchable=True, last_good={
                "five_hour": {"pct": 0.0}, "seven_day": {"pct": 0.0},
            }),
            self._acct("4", "new@x.com", switchable=False),
        ), active="9")
        rows = [line for line in out.split("\n") if re.match(r"\s+\d+\s+\S+\s+", line)]
        assert len(rows) == 3, rows
        columns = [re.match(r"\s+\d+\s+\S+\s+", line).end() for line in rows]
        assert columns[0] == columns[1] == columns[2], (
            f"content does not share a column: {columns} in {rows!r}"
        )

    def test_later_chips_align_by_window_name_not_position(self):
        """The email pad only lines up the FIRST chip. `5h(⟳4h9m):45%` is
        nine characters wider than `5h:0%`, so a row whose 5h window carries
        a live countdown pushes its 7d and Fable chips right of a row whose
        5h window does not — a positional pad over `chip_label` cannot fix
        this because the two rows' window LISTS can differ in length and
        membership; the column has to be keyed by window NAME. Same emails-
        length rows to isolate this from the already-covered email pad.
        """
        from claude_swap.settings import AutoSwitchSettings

        settings = AutoSwitchSettings(model="Fable", threshold=99.0)
        now = datetime.now(timezone.utc)
        out = self._render(self._snap(
            self._acct("2", "aaaa@x.com", switchable=True, last_good={
                "five_hour": {"pct": 45.0,
                              "resets_at": (now + timedelta(hours=4, minutes=9)).isoformat()},
                "seven_day": {"pct": 9.0,
                              "resets_at": (now + timedelta(days=3, hours=8)).isoformat()},
                "scoped": [{"name": "Fable", "pct": 8.0,
                            "resets_at": (now + timedelta(days=3, hours=8)).isoformat()}],
            }),
            self._acct("3", "bbbb@x.com", switchable=True, last_good={
                "five_hour": {"pct": 0.0},
                "seven_day": {"pct": 0.0},
                "scoped": [{"name": "Fable", "pct": 0.0}],
            }),
        ), active="9", settings=settings)
        lines = [line for line in out.split("\n") if line.strip().startswith(("2 ", "3 "))]
        assert len(lines) == 2, lines
        seven_d = [line.index("7d") for line in lines]
        fable = [line.index("Fable") for line in lines]
        assert seven_d[0] == seven_d[1], f"7d chip not aligned: {seven_d} in {lines!r}"
        assert fable[0] == fable[1], f"Fable chip not aligned: {fable} in {lines!r}"

    def test_the_panel_labels_a_model_only_block_and_a_full_block(self):
        """`classify_candidate_block`'s two blocked outcomes must both reach
        the panel, not just `model` — the decision log already appends
        `(<window> full)` for `full` (`_describe`), and the chip colour
        alone does not say which window blocked: it is driven by the fixed
        WARN/CRIT constants in `theme.py`, not by `settings.threshold`, so
        at an off-default threshold the colour and the block classification
        can disagree."""
        from claude_swap.settings import AutoSwitchSettings

        settings = AutoSwitchSettings(model="Fable", threshold=90.0)
        out = self._render(self._snap(
            self._acct("1", "a@x.com", switchable=True),
            # Model-only block: 5h/7d have room, only the pinned model's
            # scoped window is over the bar.
            self._acct("2", "model-only@x.com", switchable=True, last_good={
                "five_hour": {"pct": 10.0}, "seven_day": {"pct": 5.0},
                "scoped": [{"name": "Fable", "pct": 95.0}],
            }),
            # Full block: 5h itself is over the bar, no model choice escapes it.
            self._acct("3", "c@x.com", switchable=True, last_good={
                "five_hour": {"pct": 95.0}, "seven_day": {"pct": 5.0},
                "scoped": [{"name": "Fable", "pct": 10.0}],
            }),
        ), active="1", settings=settings)
        assert "Fable-only" in out, out
        assert "  5h full" in out, out

    def test_the_panel_chips_include_the_window_its_label_names(self):
        """A row's chips and its label must read the SAME window set — a
        `model`-blocked row used to name the scoped window in its label
        while the chips, built from a literal 5h/7d pair, never printed it
        at all. Account #4's real values: 5h 28%, 7d 70%, Fable 91%,
        threshold 90, model Fable — the label already read `Fable-only`;
        the chips must now show `Fable:91%` alongside `5h:28%`/`7d:70%` (none
        of the three windows carry a reset here, so each chip reads its
        explicit unknown-reset marker rather than the bare label)."""
        from claude_swap.settings import AutoSwitchSettings

        settings = AutoSwitchSettings(model="Fable", threshold=90.0)
        out = self._render(self._snap(
            self._acct("1", "a@x.com", switchable=True),
            self._acct("4", "d@x.com", switchable=True, last_good={
                "five_hour": {"pct": 28.0}, "seven_day": {"pct": 70.0},
                "scoped": [{"name": "Fable", "pct": 91.0}],
            }),
        ), active="1", settings=settings)
        assert "Fable-only" in out, out
        assert "Fable(⟳?):91%" in out, out

    def test_panel_top_matches_the_engines_pick_under_consume_first(
        self, temp_home
    ):
        """The engine's own model-window fallback (`_rank_candidates`,
        autoswitch.py) only drops the model set under `strategy ==
        "dynamic"` (`autoswitch.py:2400`) — `best`/`consume-first` never
        rank on 5h/7d alone. The panel's `rank_models` fallback must be
        gated the same way, or under `consume-first` it names a top row
        the engine is forbidden to pick.

        Four accounts, `model="Fable"`, `threshold=90`, `strategy=
        "consume-first"`: every candidate is blocked on the model-gated
        axis (account 2 on its own 5h window, 3 and 4 only on Fable), so
        an ungated panel drops `models` and ranks 3/4 on 5h/7d alone,
        naming account 3 top. The real engine, ticked on the identical
        fleet, switches to account 2 — the panel must agree.
        """
        from tests.test_autoswitch import EngineHarness, _iso_at
        from claude_swap.autoswitch import TickOutcome
        from claude_swap.settings import AutoSwitchSettings

        h = EngineHarness(
            temp_home, model="Fable", threshold=90.0, strategy="consume-first",
        )
        for num, email in (
            (1, "a@x.invalid"), (2, "b@x.invalid"),
            (3, "c@x.invalid"), (4, "d@x.invalid"),
        ):
            h.seed(num, email)
        h.make_live("a@x.invalid", 1)

        def w(five_h, seven_d, fable, hours_out):
            d = {
                "five_hour": {"pct": five_h},
                "seven_day": {
                    "pct": seven_d,
                    "resets_at": _iso_at(h.clock.now + hours_out * 3600),
                },
            }
            if fable is not None:
                d["scoped"] = [{"name": "Fable", "pct": fable}]
            return d

        fleet = {
            "1": w(0, 91, 99, 10),
            "2": w(91, 5, None, 5),
            "3": w(20, 50, 100, 20),
            "4": w(5, 88, 95, 40),
        }
        out = h.tick_with_usage(fleet)
        assert out is TickOutcome.SWITCHED, f"expected a switch, got {out}"
        engine_pick = str(h.active_number())

        settings = AutoSwitchSettings(
            model="Fable", threshold=90.0, strategy="consume-first",
        )
        rendered = self._render(self._snap(
            self._acct("1", "a@x.invalid", switchable=True, last_good=fleet["1"]),
            self._acct("2", "b@x.invalid", switchable=True, last_good=fleet["2"]),
            self._acct("3", "c@x.invalid", switchable=True, last_good=fleet["3"]),
            self._acct("4", "d@x.invalid", switchable=True, last_good=fleet["4"]),
        ), active="1", settings=settings)
        emails = {"2": "b@x.invalid", "3": "c@x.invalid", "4": "d@x.invalid"}
        positions = {n: rendered.index(e) for n, e in emails.items()}
        panel_top = min(positions, key=positions.get)
        assert panel_top == engine_pick, (
            f"panel top={panel_top!r}, engine picked {engine_pick!r} — "
            f"panel out:\n{rendered}"
        )

    def test_the_panel_top_agrees_with_the_engine_on_an_unknown_reset_candidate(
        self, temp_home
    ):
        """`consume_first_rank_key` used to be called with no `probe` flag and
        no knowledge of the probe target, so an unknown-reset candidate read
        its own absent reset as `+inf` (sorted last) here while the engine
        ranks the SAME candidate `-inf` (first) once it admits it as a probe
        — `consume_first_rank_key`'s own docstring: "a display built from
        this key can never disagree with the account the engine would switch
        to." Same fleet as `TestConsumeFirstProbesAnUnknownReset
        .test_admits_the_unknown_reset_candidate_ahead_of_a_known_soon_reset`.
        """
        from tests.test_autoswitch import EngineHarness, _iso_at
        from claude_swap.autoswitch import TickOutcome
        from claude_swap.settings import AutoSwitchSettings

        h = EngineHarness(temp_home, strategy="consume-first")
        h.seed(1, "a@x.invalid")
        h.seed(2, "b@x.invalid")
        h.seed(3, "c@x.invalid")
        h.make_live("a@x.invalid", 1)

        # Anchored to REAL wall-clock time, not the engine harness's
        # `FakeClock` (which starts near epoch 1_000_000): the panel reads
        # `time.time()` directly, and a fixed past ISO date would read as
        # "reset already elapsed" (== unknown) there while the harness's own
        # far-future-relative `now` still sees it as a real future reset —
        # masking exactly the disagreement this test exists to catch.
        real_now = time.time()
        later = _iso_at(real_now + 8 * 86400)
        soon = _iso_at(real_now + 5 * 86400)

        def w7(five_h, seven_d, reset=None):
            seven: dict = {"pct": seven_d}
            if reset:
                seven["resets_at"] = reset
            return {"five_hour": {"pct": five_h}, "seven_day": seven}

        fleet = {
            "1": w7(20, 20, later),  # active, known reset
            "2": w7(10, 10),         # UNKNOWN reset -- never probed
            "3": w7(10, 10, soon),   # known, soonest of the KNOWN
        }
        out = h.tick_with_usage(fleet)
        assert out is TickOutcome.SWITCHED, f"expected a switch, got {out}"
        engine_pick = str(h.active_number())
        assert engine_pick == "2", (
            f"harness precondition: expected the probe (2), got {engine_pick}"
        )

        settings = AutoSwitchSettings(strategy="consume-first")
        rendered = self._render(self._snap(
            self._acct("1", "a@x.invalid", switchable=True, last_good=fleet["1"]),
            self._acct("2", "b@x.invalid", switchable=True, last_good=fleet["2"]),
            self._acct("3", "c@x.invalid", switchable=True, last_good=fleet["3"]),
        ), active="1", settings=settings)
        emails = {"2": "b@x.invalid", "3": "c@x.invalid"}
        positions = {n: rendered.index(e) for n, e in emails.items()}
        panel_top = min(positions, key=positions.get)
        assert panel_top == engine_pick, (
            f"panel top={panel_top!r}, engine picked {engine_pick!r} — "
            f"panel out:\n{rendered}"
        )

    def test_the_panel_never_probes_an_account_the_engine_has_put_on_cooldown(
        self,
    ):
        """`_candidates_text` used to hardcode `probe_cooldown=None` into its
        own `select_probe_target` call (autoview.py), so a candidate the
        ENGINE was still cooling down from a previous probe
        (`_perform`'s `probeCooldown[num] = now + PROBE_COOLDOWN_S`,
        autoswitch.py) read as fresh here and jumped back to the top of
        "Next best" for up to an hour, pointing at an account the engine
        will not go to. The panel must read the same cooldown record the
        engine cached from its own tick (`AutoSwitchEngine._last_probe_cooldown`).
        """
        from tests.test_autoswitch import _iso_at
        from claude_swap.settings import AutoSwitchSettings

        class _FakeEngine:
            def __init__(self, cooldown):
                self._last_probe_cooldown = cooldown

        real_now = time.time()
        soon = _iso_at(real_now + 5 * 86400)

        active = {
            "five_hour": {"pct": 20.0},
            "seven_day": {"pct": 20.0, "resets_at": _iso_at(real_now + 8 * 86400)},
        }
        unknown = {"five_hour": {"pct": 10.0}, "seven_day": {"pct": 10.0}}
        known_soon = {
            "five_hour": {"pct": 10.0},
            "seven_day": {"pct": 10.0, "resets_at": soon},
        }

        settings = AutoSwitchSettings(strategy="consume-first")
        engine = _FakeEngine({"2": real_now + 3600})
        rendered = self._render(self._snap(
            self._acct("1", "a@x.invalid", switchable=True,
                        usage=UsageEntry(last_good=active, age_s=0.0,
                                          fetched_at=real_now)),
            self._acct("2", "b@x.invalid", switchable=True, last_good=unknown),
            self._acct("3", "c@x.invalid", switchable=True, last_good=known_soon),
        ), active="1", settings=settings, engine=engine)

        emails = {"2": "b@x.invalid", "3": "c@x.invalid"}
        positions = {n: rendered.index(e) for n, e in emails.items()}
        panel_top = min(positions, key=positions.get)
        assert panel_top == "3", (
            f"panel probed a cooling-down account ({panel_top!r}) instead "
            f"of the known-soon-reset one -- panel out:\n{rendered}"
        )

    def test_the_panel_survives_a_null_cooldown_left_behind_by_a_switch_to_another_account(
        self, temp_home
    ):
        """`_perform` (autoswitch.py) publishes the RAW `state["probeCooldown"]`
        into `_last_probe_cooldown` -- it pops only the account the switch just
        landed ON, so a corrupted entry for any OTHER account (`{"5": null}`,
        e.g. from a hand-edited state file) survives into the panel's cache
        untouched, bypassing the type filter `_rank_candidates` applies for
        exactly this reason. A switch to a DIFFERENT account then leaves the
        panel's own `select_probe_target` call comparing `None > now` for
        account 5 and raising `TypeError` as soon as 5 has readable headroom
        and an unmeasured 7-day reset."""
        from tests.test_autoswitch import EngineHarness, _iso_at
        from claude_swap.settings import AutoSwitchSettings

        h = EngineHarness(temp_home, strategy="consume-first")
        h.seed(1, "a@x.invalid")
        h.seed(2, "b@x.invalid")
        h.make_live("a@x.invalid", 1)
        h.switcher._write_json(
            h.switcher.backup_dir / "autoswitch_state.json",
            {"probeCooldown": {"5": None}},
        )
        # Lands on "2", not "5" -- the pop in `_perform` never reaches "5".
        h.engine._perform("2", "b@x.invalid", "proactive", (90.0, float("inf")))
        assert h.active_number() == 2

        # The panel reads real wall-clock time (`time.time()`), not the
        # engine harness's `FakeClock` (near epoch 1_000_000) -- a reset
        # timestamped off the fake clock would read as already elapsed
        # (== unknown) to the panel and never reach the comparison this
        # test exists to exercise.
        real_now = time.time()
        known_active = {
            "five_hour": {"pct": 20.0},
            "seven_day": {"pct": 20.0, "resets_at": _iso_at(real_now + 8 * 86400)},
        }
        unknown_headroom = {"five_hour": {"pct": 10.0}, "seven_day": {"pct": 10.0}}

        settings = AutoSwitchSettings(strategy="consume-first")
        rendered = self._render(self._snap(
            self._acct("2", "b@x.invalid", switchable=True, last_good=known_active),
            self._acct("5", "e@x.invalid", switchable=True,
                        last_good=unknown_headroom),
        ), active="2", settings=settings, engine=h.engine)
        assert "e@x.invalid" in rendered, rendered

    def test_the_panel_never_probes_off_an_active_reset_the_engine_has_stopped_trusting(
        self,
    ):
        """The panel used to read the active account's `sentinel or
        last_good` for its own `select_probe_target` call, ignoring
        staleness -- so once the active account's store row aged past
        `STALE_OK_S` the panel still saw its old known 7-day reset while the
        engine's own gate (`decision_value()`) had already stopped trusting
        it and reads no active reset at all. `select_probe_target`'s
        ``active_reset_ts is None`` guard (autoswitch.py) exists exactly for
        that case: with a stale active it must refuse to name any probe
        target, and the unknown-reset candidate must sort LAST like any
        other candidate with no reset, never jump to the top on `-inf`.
        """
        from tests.test_autoswitch import _iso_at
        from claude_swap.settings import AutoSwitchSettings

        real_now = time.time()
        soon = _iso_at(real_now + 5 * 86400)

        stale_last_good = {
            "five_hour": {"pct": 20.0},
            "seven_day": {"pct": 20.0, "resets_at": _iso_at(real_now + 8 * 86400)},
        }
        active_usage = UsageEntry(
            last_good=stale_last_good,
            fetched_at=real_now - STALE_OK_S - 100.0,
            age_s=STALE_OK_S + 100.0,
        )
        unknown = {"five_hour": {"pct": 10.0}, "seven_day": {"pct": 10.0}}
        known_soon = {
            "five_hour": {"pct": 10.0},
            "seven_day": {"pct": 10.0, "resets_at": soon},
        }

        settings = AutoSwitchSettings(strategy="consume-first")
        rendered = self._render(self._snap(
            self._acct("1", "a@x.invalid", switchable=True, usage=active_usage),
            self._acct("2", "b@x.invalid", switchable=True, last_good=unknown),
            self._acct("3", "c@x.invalid", switchable=True, last_good=known_soon),
        ), active="1", settings=settings)

        emails = {"2": "b@x.invalid", "3": "c@x.invalid"}
        positions = {n: rendered.index(e) for n, e in emails.items()}
        panel_top = min(positions, key=positions.get)
        assert panel_top == "3", (
            f"panel put the unknown-reset account on top ({panel_top!r}) "
            f"while the active account's own reset is stale and unknown to "
            f"the engine -- panel out:\n{rendered}"
        )


@pytest.mark.asyncio
class TestNeedsLoginIsReported:
    """Landing on a credential-less slot logs the machine OUT.

    `switch_to` reports that with `needsLogin`, and the notification path read
    only `switched` — so the one switch that leaves the user unable to work
    announced itself exactly like a working one.
    """

    class _EmptySlotSwitcher(FakeSwitcher):
        def switch_to(
            self, identifier: str, json_output: bool = False, force: bool = False
        ) -> dict:
            payload = super().switch_to(identifier, json_output, force)
            payload["needsLogin"] = True
            payload["reason"] = "switched-needs-login"
            payload["message"] = (
                f"Switched to Account-{identifier} "
                f"(user{identifier}@example.com) — no stored login; run /login"
            )
            return payload

    async def test_the_switch_notification_says_a_login_is_needed(self, tmp_path):
        fake = self._EmptySlotSwitcher(
            [make_account("1", active=True), make_account("2")], tmp_path
        )
        app = make_app(fake)
        seen: list[tuple[str, dict]] = []
        async with app.run_test() as pilot:
            await settle(pilot)
            app.notify = lambda msg, **kw: seen.append((str(msg), kw))
            app.do_switch("2")
            await settle(pilot)

        assert seen, "the switch produced no notification at all"
        body = " ".join(m for m, _ in seen)
        assert "/login" in body, (
            f"a switch that logged the machine out reported a plain success: "
            f"{seen!r}"
        )
