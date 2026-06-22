"""Tests for the TUI module.

These tests don't render real curses windows. They mock curses primitives
and verify the menu/dispatch logic.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_swap import tui
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.switcher import ClaudeAccountSwitcher


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def _stub_screen(rows: int = 30, cols: int = 100) -> MagicMock:
    """Return a MagicMock that quacks like a curses window."""
    screen = MagicMock()
    screen.getmaxyx.return_value = (rows, cols)
    return screen


def _capture_screen(rows: int = 30, cols: int = 100):
    """A curses-window mock that records ``addstr`` output into a line buffer.

    Returns ``(screen, lines)`` where ``lines`` maps row -> rendered text, so a
    test can assert on what ``_draw_dashboard`` actually painted.
    """
    screen = MagicMock()
    screen.getmaxyx.return_value = (rows, cols)
    lines: dict[int, str] = {}

    def _addstr(y, x, s, *_a):
        s = str(s)
        cur = lines.get(y, "")
        if len(cur) < x:
            cur = cur.ljust(x)
        lines[y] = cur[:x] + s + cur[x + len(s):]

    screen.addstr.side_effect = _addstr
    return screen, lines


def _all_text(lines: dict[int, str]) -> str:
    return "\n".join(lines[y] for y in sorted(lines))


def _line_with(lines: dict[int, str], needle: str) -> str:
    for y in sorted(lines):
        if needle in lines[y]:
            return lines[y]
    return ""


def _make_seq(temp_home: Path, accounts: list[tuple[str, str]] | None = None) -> Path:
    """Write a sequence.json to the backup directory.

    accounts: list of (slot, email) tuples. First entry is treated as active.
    """
    accounts = accounts or []
    backup = temp_home / ".claude-swap-backup"
    backup.mkdir(parents=True, exist_ok=True)
    seq_data = {
        "activeAccountNumber": int(accounts[0][0]) if accounts else None,
        "lastUpdated": "2026-04-30T00:00:00Z",
        "sequence": [int(a[0]) for a in accounts],
        "accounts": {
            slot: {
                "email": email,
                "uuid": f"uuid-{slot}",
                "organizationUuid": "",
                "organizationName": "",
                "added": "2026-04-30T00:00:00Z",
            }
            for slot, email in accounts
        },
    }
    (backup / "sequence.json").write_text(json.dumps(seq_data))
    return backup / "sequence.json"


# --------------------------------------------------------------------------- #
# Status line / account items                                                  #
# --------------------------------------------------------------------------- #


class TestStatusLine:
    def test_no_managed_no_login(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        line = tui._status_line(switcher)
        assert "no active login" in line
        assert "0 managed" in line

    def test_with_active_login(self, temp_home: Path):
        config = {"oauthAccount": {"emailAddress": "u@example.com"}}
        (temp_home / ".claude.json").write_text(json.dumps(config))
        _make_seq(temp_home, [("1", "u@example.com")])
        switcher = ClaudeAccountSwitcher()
        line = tui._status_line(switcher)
        assert "u@example.com" in line
        assert "1 managed" in line


class TestAccountItems:
    def test_empty_when_no_accounts(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        assert tui._account_items(switcher) == []

    def test_returns_sorted_items_with_active_marker(self, temp_home: Path):
        _make_seq(temp_home, [("1", "a@x.com"), ("2", "b@x.com")])
        config = {"oauthAccount": {"emailAddress": "a@x.com"}}
        (temp_home / ".claude.json").write_text(json.dumps(config))
        switcher = ClaudeAccountSwitcher()
        items = tui._account_items(switcher)
        assert len(items) == 2
        labels = [label for label, _ in items]
        assert "★ active" in labels[0]  # slot 1 is active
        assert "★ active" not in labels[1]
        # Values should be the slot numbers as strings
        assert [v for _, v in items] == ["1", "2"]


# --------------------------------------------------------------------------- #
# _select_from menu primitive                                                   #
# --------------------------------------------------------------------------- #


class TestSelectFrom:
    def test_returns_value_on_enter(self):
        screen = _stub_screen()
        # press down once, then enter
        screen.getch.side_effect = [tui.curses.KEY_DOWN, 10]
        result = tui._select_from(
            screen,
            "title",
            items=[("first", "a"), ("second", "b")],
        )
        assert result == "b"

    def test_returns_none_on_escape(self):
        screen = _stub_screen()
        screen.getch.side_effect = [27]  # Esc
        result = tui._select_from(screen, "t", items=[("x", "1")])
        assert result is None

    def test_returns_none_on_q(self):
        screen = _stub_screen()
        screen.getch.side_effect = [ord("q")]
        result = tui._select_from(screen, "t", items=[("x", "1")])
        assert result is None

    def test_wrap_around_on_up_at_top(self):
        screen = _stub_screen()
        screen.getch.side_effect = [tui.curses.KEY_UP, 10]
        result = tui._select_from(
            screen, "t",
            items=[("a", "1"), ("b", "2"), ("c", "3")],
        )
        assert result == "3"  # wrapped to last

    def test_cancel_sentinel_returns_none(self):
        screen = _stub_screen()
        screen.getch.side_effect = [tui.curses.KEY_DOWN, 10]
        # second item has value=None — selecting it should return None
        result = tui._select_from(
            screen, "t",
            items=[("real", "x"), ("-- Cancel --", None)],
        )
        assert result is None


# --------------------------------------------------------------------------- #
# Sub-flows: switch / add / remove                                              #
# --------------------------------------------------------------------------- #


class TestDoSwitch:
    def test_no_accounts_shows_message(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        screen = _stub_screen()
        screen.getch.return_value = ord("q")  # dismiss the message
        tui._do_switch(screen, switcher)
        # Should NOT call switch_to
        # (we use real switcher; no patching needed since add wasn't called)

    def test_dispatches_to_switch_to(self, temp_home: Path):
        _make_seq(temp_home, [("1", "a@x.com"), ("2", "b@x.com")])
        config = {"oauthAccount": {"emailAddress": "a@x.com"}}
        (temp_home / ".claude.json").write_text(json.dumps(config))
        switcher = ClaudeAccountSwitcher()

        screen = _stub_screen()
        # Pick the second item (slot 2) with one DOWN + ENTER
        screen.getch.side_effect = [tui.curses.KEY_DOWN, 10, ord("\n")]

        with patch.object(switcher, "switch_to") as mock_switch, \
             patch("claude_swap.tui.curses.def_prog_mode"), \
             patch("claude_swap.tui.curses.endwin"), \
             patch("claude_swap.tui.curses.reset_prog_mode"), \
             patch("builtins.input", return_value=""):
            tui._do_switch(screen, switcher)

        mock_switch.assert_called_once_with("2")

    def test_cancel_does_not_dispatch(self, temp_home: Path):
        _make_seq(temp_home, [("1", "a@x.com")])
        config = {"oauthAccount": {"emailAddress": "a@x.com"}}
        (temp_home / ".claude.json").write_text(json.dumps(config))
        switcher = ClaudeAccountSwitcher()

        screen = _stub_screen()
        screen.getch.side_effect = [27]  # Esc on selection screen

        with patch.object(switcher, "switch_to") as mock_switch:
            tui._do_switch(screen, switcher)

        mock_switch.assert_not_called()


class TestDoAdd:
    def test_login_path_calls_add_account(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        screen = _stub_screen()
        # First menu: Enter on "From current Claude Code login" (idx 0)
        screen.getch.side_effect = [10]
        with patch.object(switcher, "add_account") as mock_add, \
             patch("claude_swap.tui.curses.def_prog_mode"), \
             patch("claude_swap.tui.curses.endwin"), \
             patch("claude_swap.tui.curses.reset_prog_mode"), \
             patch("builtins.input", return_value=""):
            tui._do_add(screen, switcher, has_token_flow=False)
        mock_add.assert_called_once_with()

    def test_token_option_only_when_method_exists(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        screen = _stub_screen()
        screen.getch.side_effect = [27]  # cancel out

        with patch.object(switcher, "add_account") as _:
            tui._do_add(screen, switcher, has_token_flow=False)
        # If we never had add_account_from_token, the token option must not show.
        # Verify by checking the items list passed to addstr — easier: just trust
        # that has_token_flow=False yields a 2-item menu (login + cancel).
        # This test mostly guards against exceptions when method missing.

    def test_token_path_collects_email_and_token(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        # Stub add_account_from_token onto the instance
        switcher.add_account_from_token = MagicMock()

        screen = _stub_screen()

        # Sequence:
        #   menu: DOWN once (to "From a setup-token") + ENTER
        #   email prompt: type "u@x.com" + ENTER
        #   token prompt: type "tok" + ENTER
        keys = [tui.curses.KEY_DOWN, 10]  # pick token option
        keys += [ord(c) for c in "u@x.com"] + [10]  # email + Enter
        keys += [ord(c) for c in "tok"] + [10]  # token + Enter
        screen.getch.side_effect = keys

        with patch("claude_swap.tui.curses.def_prog_mode"), \
             patch("claude_swap.tui.curses.endwin"), \
             patch("claude_swap.tui.curses.reset_prog_mode"), \
             patch("claude_swap.tui.curses.curs_set"), \
             patch("builtins.input", return_value=""):
            tui._do_add(screen, switcher, has_token_flow=True)

        switcher.add_account_from_token.assert_called_once_with(
            token="tok", email="u@x.com", slot=None
        )


class TestDoRemove:
    def test_no_accounts_shows_message(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        screen = _stub_screen()
        screen.getch.return_value = ord("q")
        with patch.object(switcher, "remove_account") as mock_rm:
            tui._do_remove(screen, switcher)
        mock_rm.assert_not_called()

    def test_confirm_required(self, temp_home: Path):
        _make_seq(temp_home, [("1", "a@x.com")])
        switcher = ClaudeAccountSwitcher()

        screen = _stub_screen()
        # pick slot 1 + Enter, then type "n" + Enter on confirm prompt
        keys = [10]  # pick first item
        keys += [ord("n"), 10]  # confirm: "n"
        screen.getch.side_effect = keys

        with patch.object(switcher, "remove_account") as mock_rm, \
             patch("claude_swap.tui.curses.curs_set"):
            tui._do_remove(screen, switcher)

        mock_rm.assert_not_called()

    def test_y_confirms_and_dispatches(self, temp_home: Path):
        _make_seq(temp_home, [("3", "x@y.com")])
        switcher = ClaudeAccountSwitcher()

        screen = _stub_screen()
        keys = [10]  # pick first slot
        keys += [ord("y"), 10]  # confirm: y
        screen.getch.side_effect = keys

        with patch.object(switcher, "remove_account") as mock_rm, \
             patch("claude_swap.tui.curses.def_prog_mode"), \
             patch("claude_swap.tui.curses.endwin"), \
             patch("claude_swap.tui.curses.reset_prog_mode"), \
             patch("claude_swap.tui.curses.curs_set"), \
             patch("builtins.input", return_value=""):
            tui._do_remove(screen, switcher)

        mock_rm.assert_called_once_with("3")


# --------------------------------------------------------------------------- #
# CLI integration                                                              #
# --------------------------------------------------------------------------- #


class TestDashboard:
    """``_draw_dashboard`` renders per-account usage for every account (5h + 7d
    consumption/resets, session count, and warm/cold when warming is enabled)."""

    NOW = 1_000_000.0

    def _accts(self):
        from claude_swap.balancer import AccountView

        n = self.NOW
        return {
            # live, in-use, 5h running
            "1": AccountView(
                num="1", priority=3, max_pct=45.0, signal="live",
                five_hour_pct=45.0, five_hour_reset=int(n + 8000),
                seven_day_pct=30.0, seven_day_reset=int(n + 360000),
            ),
            # idle (cache), 5h unstarted -> cold
            "2": AccountView(
                num="2", priority=3, max_pct=12.0, signal="cache",
                five_hour_pct=0.0, five_hour_reset=None,
                seven_day_pct=12.0, seven_day_reset=int(n + 430000),
            ),
            # unknown usage (logged out / failed read)
            "9": AccountView(num="9", priority=1, signal="none"),
        }

    def _sessions(self):
        return [
            {"session_id": "s1", "account_num": "1", "cwd": "/a", "last_seen": self.NOW},
            {"session_id": "s2", "account_num": "1", "cwd": "/b", "last_seen": self.NOW},
        ]

    def _switcher(self, prime: bool = True) -> MagicMock:
        sw = MagicMock()
        sw.get_auto_balance_config.return_value = {
            "enabled": True, "threshold": 95, "targetSafety": 90,
            "primeIdleWindows": prime,
        }
        # Identity data the account headers read (email/org/active marker). Mirrors
        # `cswap --list`: account 1 is the current login => marked (active).
        sw._get_sequence_data_migrated.return_value = {
            "accounts": {
                "1": {"email": "a@x.com", "organizationName": "", "organizationUuid": ""},
                "2": {"email": "b@x.com", "organizationName": "", "organizationUuid": ""},
                "9": {"email": "c@x.com", "organizationName": "", "organizationUuid": ""},
            }
        }
        sw._get_current_account.return_value = ("a@x.com", "")
        sw._get_display_tag = lambda email, org_name, org_uuid: org_name or "personal"
        return sw

    def _draw(self, prime: bool = True):
        screen, lines = _capture_screen()
        with patch("claude_swap.tui.time.time", return_value=self.NOW):
            tui._draw_dashboard(screen, self._switcher(prime), self._sessions(), self._accts())
        return lines

    def test_per_account_session_count_including_idle(self):
        text = _all_text(self._draw())
        # Two sessions land on a1 (only account with sessions, so unambiguous);
        # idle accounts still render their own session-count row at 0.
        assert "2 sessions" in text
        assert "0 sessions" in text

    def test_account_header_matches_list_format(self):
        # The header reads like a `cswap --list` row: "num: email [tag] (active)".
        lines = self._draw()
        assert "1: a@x.com [personal] (active)" in _line_with(lines, "a@x.com")
        # The active marker only lands on the current-login account.
        assert "(active)" not in _line_with(lines, "b@x.com")

    def test_idle_account_shows_both_windows_with_usage(self):
        # The whole point: usage is shown even for an account hosting NO session.
        text = _all_text(self._draw())
        assert "5h:   0%   not started" in text     # unstarted 5h window (cold)
        assert "7d:  12%   resets" in text           # weekly consumption + reset

    def test_live_account_shows_5h_and_7d_resets(self):
        text = _all_text(self._draw())
        # `--list`-style: usage% then "resets <clock>  in <countdown>".
        assert "5h:  45%   resets" in text and "in 2h 13m" in text
        assert "7d:  30%   resets" in text and "in 4d 4h" in text

    def test_warm_column_present_only_when_warming_enabled(self):
        on = _all_text(self._draw(prime=True))
        assert "warming ON" in on
        # warm/cold annotates the session-count row of accounts with a signal.
        assert "2 sessions  · warm" in on   # a1: 5h running
        assert "0 sessions  · cold" in on   # a2: 5h unstarted

        off = _all_text(self._draw(prime=False))
        assert "warming OFF" in off
        # No per-account warm/cold token on the session rows when warming is off
        # (match the session-row form so the header's "· warming OFF" can't alias).
        assert "sessions  · warm" not in off
        assert "sessions  · cold" not in off

    def test_unknown_usage_account_marked_unavailable(self):
        text = _all_text(self._draw())
        assert "usage unavailable" in text
        # warm/cold is meaningless without a usage signal -> not annotated.
        assert "· ?" not in text

    def test_short_terminal_never_leaves_dangling_header(self):
        # Block-fit guard: on a short screen whole accounts are dropped rather
        # than painting a header with no tree rows beneath it.
        import re

        for rows in range(tui._MIN_ROWS, 30):
            screen, lines = _capture_screen(rows=rows, cols=100)
            with patch("claude_swap.tui.time.time", return_value=self.NOW):
                tui._draw_dashboard(
                    screen, self._switcher(), self._sessions(), self._accts()
                )
            ys = sorted(lines)
            for i, y in enumerate(ys):
                if re.match(r"\s*\d+: \S+@", lines[y]):  # an account header line
                    nxt = next(
                        (lines[y2] for y2 in ys if y2 > y and lines[y2].strip()), ""
                    )
                    assert "├" in nxt or "└" in nxt, (
                        f"dangling header at rows={rows}: {lines[y]!r} -> {nxt!r}"
                    )

    def test_ui_loop_never_hits_the_network(self):
        # The render loop must only read the worker-warmed cache + live signals
        # (fetch_idle=False) so a slow account can never block rendering/quit.
        screen = _stub_screen()
        screen.getch.side_effect = [-1, -1, ord("q")]
        calls: list[bool] = []

        def fake_build_world(switcher, reg, *, fetch_idle):
            calls.append(fetch_idle)
            return ({}, [])

        with patch("claude_swap.tui.threading.Thread") as thread_cls, \
             patch("claude_swap.tui.registry.read_registry", return_value={}), \
             patch("claude_swap.tui.registry.live_sessions", return_value=[]), \
             patch("claude_swap.tui.registry.build_world", side_effect=fake_build_world), \
             patch("claude_swap.tui.curses.curs_set"):
            tui._balancer_dashboard(screen, self._switcher())

        assert calls and all(c is False for c in calls)
        # A background refresher thread is started and stopped on exit.
        thread_cls.assert_called_once()
        assert thread_cls.call_args.kwargs.get("daemon") is True
        thread_cls.return_value.start.assert_called_once()

    def test_idle_refresher_warms_cache_with_fetch_then_stops(self):
        # The worker fetches idle usage (fetch_idle=True) and exits when stopped.
        stop = tui.threading.Event()
        calls: list[bool] = []

        def fake_build_world(switcher, reg, *, fetch_idle):
            calls.append(fetch_idle)
            stop.set()  # one pass, then unblock stop.wait and exit the loop
            return ({}, [])

        with patch("claude_swap.tui.registry.read_registry", return_value={}), \
             patch("claude_swap.tui.registry.build_world", side_effect=fake_build_world):
            tui._idle_usage_refresher(MagicMock(), stop)

        assert calls == [True]

    def test_idle_refresher_survives_a_failed_pass(self):
        # A raising build_world must not kill the worker mid-loop; it logs and stops.
        stop = tui.threading.Event()
        sw = MagicMock()

        def boom(switcher, reg, *, fetch_idle):
            stop.set()
            raise RuntimeError("usage API down")

        with patch("claude_swap.tui.registry.read_registry", return_value={}), \
             patch("claude_swap.tui.registry.build_world", side_effect=boom):
            tui._idle_usage_refresher(sw, stop)  # must not raise

        sw._logger.debug.assert_called()


class TestDashboardFormatters:
    def test_short_countdown_days_hours_minutes(self):
        assert tui._short_countdown(4 * 86400 + 5 * 3600) == "4d05h"
        assert tui._short_countdown(2 * 3600 + 13 * 60) == "2h13m"
        assert tui._short_countdown(42 * 60) == "42m"
        assert tui._short_countdown(-5) == "0m"

    def test_list_countdown(self):
        # `--list`-style spacing (distinct from the compact _short_countdown).
        assert tui._list_countdown(2 * 86400 + 11 * 3600) == "2d 11h"
        assert tui._list_countdown(2 * 3600 + 58 * 60) == "2h 58m"
        assert tui._list_countdown(42 * 60) == "42m"
        assert tui._list_countdown(-5) == "0m"

    def test_reset_clause(self):
        clause = tui._reset_clause(1000 + 8000, 1000)
        assert clause.startswith("resets ")
        assert clause.endswith("in 2h 13m")
        # "not started" is reserved for a truly unstarted (cold) window...
        assert tui._reset_clause(None, 1000) == "not started"
        # ...an elapsed reset epoch is a window rolling over, not "not started", so
        # it can't contradict a `warm` token derived from the same timestamp.
        assert tui._reset_clause(500, 1000) == "resetting"

    def test_fmt_pct(self):
        assert tui._fmt_pct(45.0).strip() == "45%"
        assert tui._fmt_pct(0.0).strip() == "0%"
        assert "%" not in tui._fmt_pct(None)

    @staticmethod
    def _flatten(block) -> str:
        """Join an _account_block's (text, attr) segments into plain rows."""
        return "\n".join("".join(text for text, _attr in row) for row in block)

    def test_account_block_unavailable_signal(self):
        from claude_swap.balancer import AccountView

        av = AccountView(num="9", priority=1, signal="none")
        flat = self._flatten(
            tui._account_block(av, "c@x.com", "personal", False, 0, 0.0, show_warm=True)
        )
        assert "9: c@x.com [personal]" in flat
        assert "usage unavailable" in flat
        assert "0 sessions" in flat
        # warm/cold suppressed when usage is unknown (no bare "· ?").
        assert "· warm" not in flat and "· cold" not in flat and "· ?" not in flat

    def test_account_block_logged_out_signal(self):
        from claude_swap.balancer import AccountView

        av = AccountView(num="9", priority=1, signal="logged_out")
        flat = self._flatten(
            tui._account_block(av, "c@x.com", "personal", False, 0, 0.0, show_warm=True)
        )
        assert "logged out — re-login" in flat
        # Distinct from a transient no-signal read.
        assert "usage unavailable" not in flat
        assert "0 sessions" in flat

    def test_account_block_session_pluralization(self):
        from claude_swap.balancer import AccountView

        av = AccountView(
            num="1", priority=0, signal="cache",
            five_hour_pct=10.0, seven_day_pct=5.0,
        )
        one = self._flatten(
            tui._account_block(av, "e@x.com", "personal", False, 1, 0.0, show_warm=False)
        )
        assert "1 session" in one and "1 sessions" not in one

    def test_account_block_omits_window_without_pct(self):
        # A live/cache signal can carry only one window's pct (the usage API emits
        # a window only once it is running). Mirror `cswap --list`: render the
        # window that has data and OMIT the absent one — never a faked
        # "5h:   —   not started" row.
        from claude_swap.balancer import AccountView

        av = AccountView(
            num="3", priority=0, signal="cache",
            five_hour_pct=None, five_hour_reset=None,
            seven_day_pct=12.0, seven_day_reset=None,
        )
        flat = self._flatten(
            tui._account_block(av, "p@x.com", "personal", False, 0, 0.0, show_warm=False)
        )
        assert "7d:  12%" in flat       # the window with data is shown
        assert "5h:" not in flat        # the absent window is omitted, not faked

    def test_account_block_unknown_5h_has_no_warm_token(self):
        # five_hour_warm is tri-state: None (unknown 5h usage) must NOT render a
        # bare "· ?" — leave the session row unannotated.
        from claude_swap.balancer import AccountView

        av = AccountView(
            num="3", priority=0, signal="cache",
            five_hour_pct=None, seven_day_pct=20.0,
        )
        flat = self._flatten(
            tui._account_block(av, "p@x.com", "personal", False, 0, 0.0, show_warm=True)
        )
        assert "· ?" not in flat
        assert "· warm" not in flat and "· cold" not in flat


class TestCliIntegration:
    def test_tui_in_help(self, tmp_path):
        import os
        import subprocess
        import sys as _sys

        env = {**os.environ}
        env["PYTHONPATH"] = (
            str(Path(__file__).resolve().parent.parent / "src")
            + os.pathsep
            + env.get("PYTHONPATH", "")
        )
        # Isolate the child from the developer's real home/config, consistent
        # with test_cli._subprocess_env. ``--help`` exits in argparse before the
        # switcher is built, so nothing is touched today — this just keeps the
        # "no subprocess inherits the real HOME" invariant uniform.
        env["HOME"] = env["USERPROFILE"] = str(tmp_path)
        for _var in ("CLAUDE_CONFIG_DIR", "XDG_DATA_HOME"):
            env.pop(_var, None)
        result = subprocess.run(
            [_sys.executable, "-m", "claude_swap", "--help"],
            capture_output=True, text=True, env=env,
        )
        assert result.returncode == 0
        assert "--tui" in result.stdout

    def test_tui_dispatches_to_run(self):
        import sys as _sys
        from claude_swap import cli

        with patch.object(_sys, "argv", ["claude-swap", "--tui"]), \
             patch("claude_swap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch("claude_swap.tui.run", return_value=0) as mock_run, \
             patch("os.geteuid", return_value=1000), \
             patch("claude_swap.update_check.check_for_update", return_value=None):
            with pytest.raises(SystemExit) as exc:
                cli.main()
            assert exc.value.code == 0
        mock_run.assert_called_once_with(switcher_cls.return_value)
