"""Tests for the ``re-login in …`` note: when it shows, how it reads, and
how it fits in every view that lists accounts (CLI list/status, TUI dashboard
card and mini rows, TUI auto-view Next-best rows, the menu bar label)."""

from __future__ import annotations

import dataclasses
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from rich.cells import cell_len

from claude_swap import oauth
from claude_swap.json_output import USAGE_API_KEY, USAGE_NO_CREDENTIALS, USAGE_RELOGIN_REQUIRED
from claude_swap.models import AccountSnapshot
from claude_swap.switcher import ClaudeAccountSwitcher, _usage_entry_lines
from claude_swap.tui import widgets
from claude_swap.usage_store import UsageEntry

from tests.test_tui import FakeSwitcher, fake_engine, make_account, make_app, make_entry

NOW = 1_000_000.0
DAY = 86400
DUE = 5 * DAY + 12 * 3600 + 30  # "5d 12h" (with 30 s for the render to happen in)
FAR = 23 * DAY  # a fresh login: nothing to say for weeks


def _replace_login(acc: AccountSnapshot, login_expires_at: float | None) -> AccountSnapshot:
    return dataclasses.replace(acc, login_expires_at=login_expires_at)


async def _workers_done(pilot) -> None:
    """Wait for the thread workers only (the engine worker runs until its screen
    stops it); the caller then pushes its screen, or pauses once."""
    app = pilot.app
    pending = [w for w in app.workers if w.group != "engine"]
    if pending:
        await app.workers.wait_for_complete(pending)


# ---------------------------------------------------------------------------
# The rule: oauth.login_time_left / format_login_expiry
# ---------------------------------------------------------------------------


class TestLoginTimeLeft:
    def test_nothing_to_say_without_a_stamp(self):
        assert oauth.login_time_left(None, False, NOW) is None

    def test_nothing_to_say_further_out_than_a_week(self):
        assert oauth.login_time_left(NOW + 7 * DAY + 1, False, NOW) is None
        assert oauth.login_time_left(NOW + FAR, False, NOW) is None

    def test_due_inside_a_week(self):
        assert oauth.login_time_left(NOW + 7 * DAY, False, NOW) == 7 * DAY
        assert oauth.login_time_left(NOW + 3600, False, NOW) == 3600

    def test_past_reads_as_zero(self):
        assert oauth.login_time_left(NOW - 10, False, NOW) == 0.0
        assert oauth.login_time_left(NOW, False, NOW) == 0.0

    def test_quarantined_says_nothing_even_when_past(self):
        # The usage sentinel already puts "re-login needed" on that row.
        assert oauth.login_time_left(NOW - 10, True, NOW) is None
        assert oauth.login_time_left(NOW + 3600, True, NOW) is None

    @pytest.mark.parametrize("stamp", [float("nan"), float("inf"), float("-inf")])
    def test_a_non_finite_stamp_reads_as_unknown(self, stamp):
        creds = json.dumps({"claudeAiOauth": {"refreshTokenExpiresAt": stamp}})
        expires_at = oauth.login_expires_at_epoch(creds)
        assert expires_at is None
        assert oauth.format_login_expiry(expires_at, False, NOW) is None


class TestFormatLoginExpiry:
    def test_reads_in_the_reset_counters_grammar(self):
        assert oauth.format_login_expiry(NOW + DUE, False, NOW) == "re-login in 5d 12h"
        assert oauth.format_login_expiry(NOW + 3600 + 59 * 60, False, NOW) == "re-login in 1h 59m"
        assert oauth.format_login_expiry(NOW + 45 * 60, False, NOW) == "re-login in 45m"

    def test_same_countdown_as_a_reset(self):
        # One formatter behind both: a window resetting in 5d 12h and a login
        # expiring in 5d 12h show the same figure.
        assert oauth.format_countdown(DUE) == "5d 12h"
        future = datetime.now(timezone.utc) + timedelta(seconds=DUE + 30)
        assert oauth.format_reset(future.isoformat())[0] == "5d 12h"

    def test_short_keeps_the_first_unit(self):
        assert oauth.format_login_expiry(NOW + DUE, False, NOW, short=True) == "re-login in 5d"
        assert oauth.format_login_expiry(NOW + 3600 + 59 * 60, False, NOW, short=True) == "re-login in 1h"
        assert oauth.format_login_expiry(NOW + 45 * 60, False, NOW, short=True) == "re-login in 45m"

    def test_past(self):
        assert oauth.format_login_expiry(NOW - 10, False, NOW) == "re-login needed"
        assert oauth.format_login_expiry(NOW, False, NOW, short=True) == "re-login needed"

    def test_silent_when_there_is_nothing_to_say(self):
        assert oauth.format_login_expiry(None, False, NOW) is None
        assert oauth.format_login_expiry(NOW + FAR, False, NOW) is None
        assert oauth.format_login_expiry(NOW - 10, True, NOW) is None


# ---------------------------------------------------------------------------
# CLI list block: `switcher._usage_entry_lines`, and via `list_accounts`
# ---------------------------------------------------------------------------


def _iso_in(seconds: float) -> str:
    return (
        (datetime.now(timezone.utc) + timedelta(seconds=seconds))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


_MEASURED = {"five_hour": {"pct": 10.0}, "seven_day": {"pct": 50.0}}


class TestCliList:
    def test_no_row_while_the_login_is_not_due(self):
        now = time.time()
        entry = UsageEntry(last_good=_MEASURED, fetched_at=now)
        fresh = _usage_entry_lines(entry, now + FAR)
        assert fresh == _usage_entry_lines(entry, None)  # exactly the block without a stamp
        assert not any("login" in line for line in fresh)
        assert any("5h:  10%" in line for line in fresh)  # label column unwidened

    def test_due_row_shares_the_reset_columns(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict, capsys
    ):
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        active_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-active"}})
        # `list_accounts` sources the non-active slot's creds through
        # `_read_account_credentials` (patched below): that is the slot whose
        # 5h/7d rows the login row is compared against.
        expires_ms = int((time.time() + DUE) * 1000)
        backup_creds = json.dumps(
            {"claudeAiOauth": {"accessToken": "sk-backup", "refreshTokenExpiresAt": expires_ms}}
        )
        usage_response = {
            "five_hour": {"utilization": 10.0, "resets_at": _iso_in(7200)},
            "seven_day": {"utilization": 50.0, "resets_at": _iso_in(3 * DAY)},
        }
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(usage_response).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)
        with patch.object(switcher, "_read_credentials", return_value=active_creds), \
             patch.object(switcher, "_read_account_credentials", return_value=backup_creds), \
             patch("claude_swap.oauth.urllib.request.urlopen", return_value=mock_response):
            switcher.list_accounts()

        lines = capsys.readouterr().out.splitlines()
        line_7d = next(line for line in lines if "7d:" in line)
        (line_login,) = [line for line in lines if "login:" in line]
        assert line_login.lstrip().startswith("└ login:")
        assert line_login.endswith("in 5d 12h")
        # Clock under clock, countdown under countdown.
        assert line_login.index("expires ") + len("expires ") == line_7d.index("resets ") + len("resets ")
        assert line_login.index("  in ") == line_7d.index("  in ")

    def test_past_row(self):
        now = time.time()
        lines = _usage_entry_lines(UsageEntry(last_good=_MEASURED, fetched_at=now), now - 60)
        (line,) = [line for line in lines if "login:" in line]
        assert line.startswith("└ ") and "expired " in line and line.endswith("re-login needed")

    def test_under_a_sentinel_and_a_missing_measurement(self):
        now = time.time()
        no_creds = _usage_entry_lines(UsageEntry(sentinel=USAGE_NO_CREDENTIALS), now + DUE)
        assert no_creds[1].startswith("└ login:") and no_creds[1].endswith("in 5d 12h")
        unavailable = _usage_entry_lines(UsageEntry(last_error="timeout"), now + DUE)
        assert unavailable[0].startswith("usage unavailable")
        assert unavailable[1].startswith("└ login:")

    def test_quarantined_block_says_it_once(self):
        now = time.time()
        lines = _usage_entry_lines(UsageEntry(sentinel=USAGE_RELOGIN_REQUIRED), now - 60)
        assert lines[0].startswith("re-login needed")
        assert len(lines) == 1

    def test_age_note_stays_on_the_last_usage_row(self):
        now = time.time()
        entry = UsageEntry(last_good=_MEASURED, fetched_at=now - 3 * 3600, age_s=3 * 3600)
        with_note = _usage_entry_lines(entry, now + DUE)
        assert "ago" in with_note[-2] and "7d:" in with_note[-2]
        assert "login:" in with_note[-1] and "ago" not in with_note[-1]


# ---------------------------------------------------------------------------
# TUI dashboard: card header, mini rows, and the fit at narrow widths
# ---------------------------------------------------------------------------


PLAN = {"plan": {"tier": "default_claude_max_20x"}, "fetchedAt": time.time()}


class TestCardAndMiniRow:
    def test_nothing_while_the_login_is_not_due(self):
        acc = make_account(2, email="a@example.com", alias="dev")
        fresh = _replace_login(acc, time.time() + FAR)
        assert widgets.account_card_text(fresh, 100).plain == widgets.account_card_text(acc, 100).plain
        assert widgets.mini_account_text(fresh, time.time()).plain == widgets.mini_account_text(acc, time.time()).plain

    def test_note_ends_the_header_and_the_mini_row(self):
        now = time.time()
        acc = _replace_login(
            dataclasses.replace(make_account(2, email="a@example.com", alias="dev"), subscription_plan=PLAN),
            now + 5 * 3600 + 7 * 60,
        )
        header = widgets.account_card_text(acc, 100, now=now).plain.splitlines()[0]
        assert header.endswith("· Max 20x   re-login in 5h 7m")
        mini = widgets.mini_account_text(acc, now).plain
        assert mini == " 2  dev (a@example.com)  [personal]  · Max 20x   5h 25% · 7d 10% · re-login in 5h 7m"

    def test_quarantined_card_says_it_once(self):
        now = time.time()
        acc = _replace_login(
            make_account(1, active=True, entry=make_entry(sentinel=USAGE_RELOGIN_REQUIRED)), now - 60
        )
        lines = widgets.account_card_text(acc, 100, now=now).plain.splitlines()
        assert "re-login" not in lines[0]
        assert lines[1].lstrip().startswith("⚠ re-login needed")
        mini = widgets.mini_account_text(acc, now).plain
        assert mini.count("re-login") == 1

    def test_api_key_slot_shows_nothing(self):
        acc = make_account(3, kind="api_key", entry=make_entry(sentinel=USAGE_API_KEY))
        assert "re-login" not in widgets.mini_account_text(acc, time.time()).plain
        assert "re-login" not in widgets.account_card_text(acc, 100).plain

    def test_past_stamp_without_the_sentinel_yet(self):
        now = time.time()
        acc = _replace_login(make_account(2), now - 60)
        assert widgets.mini_account_text(acc, now).plain.endswith(" · re-login needed")


def _seven(expires_at: float | None) -> list[AccountSnapshot]:
    """Seven `developerN@example.com` slots, the active one on a plan."""
    accounts = [
        _replace_login(
            make_account(n, active=n == 1, email=f"developer{n}@example.com", alias="work" if n in (1, 2) else ""),
            expires_at,
        )
        for n in range(1, 8)
    ]
    accounts[0] = dataclasses.replace(accounts[0], subscription_plan=PLAN)
    return accounts


def _mixed(expires_at: float | None) -> list[AccountSnapshot]:
    """One long address and five short ones under a long organisation tag."""
    accounts = [
        make_account(1, active=True, email="developer1@example.com"),
        make_account(2, email="developer-with-long-name2@example.com"),
    ] + [
        dataclasses.replace(make_account(n, email=f"a{n}@example.com"), org_name="Example Engineering Team")
        for n in range(3, 8)
    ]
    return [_replace_login(acc, expires_at) for acc in accounts]


async def _dashboard(tmp_path, accounts, size):
    """(panel lines, panel content size, fully visible menu items) at ``size``."""
    from textual.widgets import ListView

    from claude_swap.tui.widgets import AccountsPanel, MenuItem

    app = make_app(FakeSwitcher(accounts, tmp_path))
    async with app.run_test(size=size) as pilot:
        await _workers_done(pilot)
        await pilot.pause()
        panel = app.screen.query_one(AccountsPanel)
        menu = app.screen.query_one("#menu", ListView)
        shown = sum(1 for item in menu.query(MenuItem) if menu.region.contains_region(item.region))
        return panel.render().plain.splitlines(), panel.size, shown


class TestDashboardFit:
    @pytest.mark.asyncio
    async def test_fresh_logins_render_exactly_as_without_the_field(self, tmp_path):
        # The mixed case that once wrapped: the rows must be the ones a
        # snapshot without any expiry renders, column for column.
        fresh, size, shown = await _dashboard(tmp_path, _mixed(time.time() + FAR), (80, 24))
        bare, _size, bare_shown = await _dashboard(tmp_path, _mixed(None), (80, 24))
        assert fresh == bare
        assert size.height == len(fresh)  # nothing wrapped
        assert shown == bare_shown > 0

    @pytest.mark.asyncio
    async def test_due_note_steps_down_to_fit_the_row(self, tmp_path):
        # 88 columns: the full note overflows the minis, the short one fits.
        lines, size, _shown = await _dashboard(tmp_path, _seven(time.time() + DUE), (88, 24))
        assert all(cell_len(line) <= size.width for line in lines)
        assert size.height == len(lines)
        minis = [line for line in lines if line.split()[:1] in [[str(n)] for n in range(2, 8)]]
        assert len(minis) == 6
        assert all(line.endswith("5h 25% · 7d 10% · re-login in 5d") for line in minis)

    @pytest.mark.asyncio
    async def test_due_note_goes_before_usage_does(self, tmp_path):
        # 80 columns: not even the short note fits, so the rows read as
        # before and keep their usage figures and the menu its rows.
        lines, size, shown = await _dashboard(tmp_path, _seven(time.time() + DUE), (80, 24))
        bare, _size, bare_shown = await _dashboard(tmp_path, _seven(None), (80, 24))
        assert lines == bare
        assert size.height == len(lines)
        assert shown == bare_shown > 0

    @pytest.mark.asyncio
    async def test_wide_enough_shows_the_full_note(self, tmp_path):
        lines, size, _shown = await _dashboard(tmp_path, _seven(time.time() + DUE), (120, 40))
        assert all(cell_len(line) <= size.width for line in lines)
        minis = [line for line in lines if line.split()[:1] in [[str(n)] for n in range(2, 8)]]
        assert all(line.endswith(" · re-login in 5d 12h") for line in minis)
        assert lines[0].endswith("re-login in 5d 12h")


# ---------------------------------------------------------------------------
# TUI auto view: Next-best rows
# ---------------------------------------------------------------------------


async def _candidates(tmp_path, accounts, size):
    from textual.widgets import Static

    from claude_swap.tui.autoview import AutoScreen

    app = make_app(FakeSwitcher(accounts, tmp_path))
    async with app.run_test(size=size) as pilot:
        await _workers_done(pilot)
        await app.push_screen(AutoScreen())
        await pilot.pause()
        box = app.screen.query_one("#candidates", Static)
        return box.render().plain, box.size.width


class TestNextBest:
    @pytest.mark.asyncio
    async def test_fresh_logins_leave_the_rows_alone(self, tmp_path, fake_engine):
        now = time.time()
        accounts = [
            make_account(1, active=True, email="active@example.com"),
            _replace_login(make_account(2, email="a@example.com"), now + FAR),
            _replace_login(make_account(3, email="a-much-longer-email@example.com"), None),
        ]
        plain, _width = await _candidates(tmp_path, accounts, (100, 40))
        assert "re-login" not in plain
        assert "   2  a@example.com  5h " in plain  # the name is not padded

    @pytest.mark.asyncio
    async def test_due_note_ends_the_row_and_steps_down_when_narrow(self, tmp_path, fake_engine):
        # 40-column addresses at 80 columns: the full note overflows the
        # 76-column row, the short one fits, and the usage figure stays.
        expires = time.time() + DUE
        accounts = [make_account(1, active=True, email="active@example.com")] + [
            _replace_login(make_account(n, email=f"{'x' * 27}{n}@example.com"), expires) for n in (2, 3)
        ]
        plain, width = await _candidates(tmp_path, accounts, (80, 24))
        rows = [line for line in plain.splitlines() if "re-login" in line]
        assert len(rows) == 2
        assert all(cell_len(row) <= width for row in rows)
        assert all(row.endswith("% used  re-login in 5d") for row in rows)


# ---------------------------------------------------------------------------
# TUI switch screen: the account cards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_switch_screen_card_header_keeps_the_note_within_the_width(tmp_path):
    from claude_swap.tui.dashboard import SwitchScreen
    from claude_swap.tui.widgets import AccountCard

    acc = _replace_login(
        make_account(1, active=True, email="dev-account-1@a.io", alias="work"), time.time() + DUE
    )
    # 76 columns: the full note overflows the header, the short one fits.
    for size, expect in (((100, 24), "re-login in 5d 12h"), ((76, 24), "re-login in 5d")):
        app = make_app(FakeSwitcher([acc, make_account(2)], tmp_path))
        async with app.run_test(size=size) as pilot:
            await _workers_done(pilot)
            await app.push_screen(SwitchScreen())
            await pilot.pause()
            card = app.screen.query(AccountCard).first()
            header = card.render().plain.splitlines()[0]
            assert cell_len(header) <= card.size.width
            assert header.endswith(expect)
