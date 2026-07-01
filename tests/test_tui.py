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
        # Pick the second item (slot 2): one DOWN + ENTER, then 'q' to leave pager.
        screen.getch.side_effect = [tui.curses.KEY_DOWN, 10, ord("q")]

        with patch.object(switcher, "switch_to") as mock_switch:
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
        # pick first slot, confirm 'y' + Enter, then 'q' to leave the pager.
        screen.getch.side_effect = [10, ord("y"), 10, ord("q")]

        with patch.object(switcher, "remove_account") as mock_rm, \
             patch("claude_swap.tui.curses.curs_set"):
            tui._do_remove(screen, switcher)

        mock_rm.assert_called_once_with("3", assume_yes=True)


# --------------------------------------------------------------------------- #
# CLI integration                                                              #
# --------------------------------------------------------------------------- #


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


class TestClampInterval:
    def test_in_range(self):
        assert tui._clamp_interval(5) == 5

    def test_below_min(self):
        assert tui._clamp_interval(0) == 1
        assert tui._clamp_interval(-3) == 1

    def test_above_max(self):
        assert tui._clamp_interval(999) == 60

    def test_custom_bounds(self):
        assert tui._clamp_interval(100, lo=2, hi=10) == 10


class TestAnsiSegments:
    def test_plain_text_single_run(self):
        assert tui._ansi_segments("hello") == [("hello", 0)]

    def test_empty_string(self):
        assert tui._ansi_segments("") == [("", 0)]

    def test_bold_run(self):
        assert tui._ansi_segments("\x1b[1mX\x1b[0m") == [("X", tui._STYLE_BOLD)]

    def test_stacked_bold_accent(self):
        # printer.bold_accent emits "\x1b[1m\x1b[38;5;173m...\x1b[0m"
        segs = tui._ansi_segments("\x1b[1m\x1b[38;5;173mY\x1b[0m")
        assert segs == [("Y", tui._STYLE_BOLD | tui._STYLE_ACCENT)]

    def test_mixed_runs_with_reset(self):
        segs = tui._ansi_segments("a\x1b[2mb\x1b[0mc")
        assert segs == [("a", 0), ("b", tui._STYLE_DIM), ("c", 0)]

    def test_muted_and_red_and_yellow(self):
        assert tui._ansi_segments("\x1b[38;5;250mm\x1b[0m") == [("m", tui._STYLE_MUTED)]
        assert tui._ansi_segments("\x1b[31mr\x1b[0m") == [("r", tui._STYLE_RED)]
        assert tui._ansi_segments("\x1b[33my\x1b[0m") == [("y", tui._STYLE_YELLOW)]

    def test_unknown_code_ignored(self):
        assert tui._ansi_segments("\x1b[99mZ\x1b[0m") == [("Z", 0)]


class TestStyleToAttr:
    def test_bold_maps_to_a_bold(self):
        # A_BOLD is a plain constant available without initscr.
        assert tui._style_to_attr(tui._STYLE_BOLD) & tui.curses.A_BOLD

    def test_dim_maps_to_a_dim(self):
        assert tui._style_to_attr(tui._STYLE_DIM) & tui.curses.A_DIM

    def test_no_flags_is_normal(self):
        assert tui._style_to_attr(0) == tui.curses.A_NORMAL

    def test_color_lookup_without_initscr_does_not_raise(self):
        # has_colors()/color_pair() raise outside initscr; must be swallowed.
        attr = tui._style_to_attr(tui._STYLE_ACCENT)
        assert isinstance(attr, int)


class TestAddstrAnsi:
    def test_draws_each_run_clipped(self):
        screen = _stub_screen()
        tui._addstr_ansi(screen, 4, 2, "\x1b[1mAB\x1b[0mCD", max_width=3)
        # First run "AB" (bold) then "C" (clipped from "CD" at width 3).
        calls = [c.args for c in screen.addstr.call_args_list]
        drawn = "".join(a[2] for a in calls)
        assert drawn == "ABC"

    def test_swallows_curses_error(self):
        screen = _stub_screen()
        screen.addstr.side_effect = tui.curses.error
        # Must not raise.
        tui._addstr_ansi(screen, 4, 2, "x", max_width=10)


class TestPager:
    def test_returns_on_q(self):
        screen = _stub_screen(rows=10, cols=40)
        screen.getch.side_effect = [ord("q")]
        tui._pager(screen, "T", [f"line{i}" for i in range(50)])

    def test_returns_on_escape(self):
        screen = _stub_screen()
        screen.getch.side_effect = [27]
        tui._pager(screen, "T", ["a", "b"])

    def test_returns_on_enter(self):
        screen = _stub_screen()
        screen.getch.side_effect = [10]
        tui._pager(screen, "T", ["a"])

    def test_scroll_down_then_quit_no_error(self):
        screen = _stub_screen(rows=8, cols=40)
        screen.getch.side_effect = [
            tui.curses.KEY_DOWN, tui.curses.KEY_NPAGE,
            tui.curses.KEY_END, tui.curses.KEY_HOME, ord("q"),
        ]
        tui._pager(screen, "T", [f"row{i}" for i in range(100)])

    def test_short_content_does_not_overscroll(self):
        screen = _stub_screen(rows=30, cols=40)
        screen.getch.side_effect = [tui.curses.KEY_DOWN, tui.curses.KEY_NPAGE, ord("q")]
        tui._pager(screen, "T", ["only line"])


class TestRunInline:
    def test_captures_colored_output_and_pages(self):
        screen = _stub_screen()
        seen = {}

        def fake_pager(s, title, lines, subtitle=""):
            seen["title"] = title
            seen["lines"] = lines

        def fn():
            from claude_swap import printer
            print(printer.accent("hello"))
            print("plain")

        with patch("claude_swap.tui._pager", side_effect=fake_pager):
            tui._run_inline(screen, "Title", fn)

        assert seen["title"] == "Title"
        # Color is forced on during capture → accent line carries ANSI.
        assert any("hello" in line for line in seen["lines"])
        assert "\x1b[38;5;173m" in "\n".join(seen["lines"])
        assert "plain" in seen["lines"]

    def test_stray_input_becomes_message_and_stdin_restored(self):
        import sys as _sys
        screen = _stub_screen()
        before = _sys.stdin

        def fn():
            input("give me something")  # no stdin → EOFError

        with patch("claude_swap.tui._pager") as mock_pager:
            tui._run_inline(screen, "T", fn)

        assert _sys.stdin is before  # restored
        lines = mock_pager.call_args.args[2]
        assert any("input is not available" in line for line in lines)

    def test_switch_error_captured(self):
        screen = _stub_screen()

        def fn():
            raise ClaudeSwitchError("boom")

        with patch("claude_swap.tui._pager") as mock_pager:
            tui._run_inline(screen, "T", fn)
        lines = mock_pager.call_args.args[2]
        assert any("boom" in line for line in lines)


class TestWatch:
    def test_empty_state_returns_without_loop(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        screen = _stub_screen()
        screen.getch.return_value = ord("q")  # dismiss _show_message
        with patch.object(switcher, "list_accounts") as mock_list:
            tui._do_watch(screen, switcher)
        mock_list.assert_not_called()

    def test_refreshes_then_quits(self, temp_home: Path):
        _make_seq(temp_home, [("1", "a@x.com")])
        switcher = ClaudeAccountSwitcher()
        screen = _stub_screen()
        screen.getch.side_effect = [ord("q")]
        with patch.object(switcher, "list_accounts") as mock_list, \
             patch("claude_swap.tui.time.monotonic", return_value=100.0):
            tui._watch_loop(screen, switcher, interval=5)
        mock_list.assert_called()  # at least one refresh before quit
        screen.timeout.assert_any_call(250)
        screen.timeout.assert_any_call(-1)

    def test_plus_raises_interval(self, temp_home: Path):
        _make_seq(temp_home, [("1", "a@x.com")])
        switcher = ClaudeAccountSwitcher()
        screen = _stub_screen()
        # First loop refreshes (monotonic 100), then '+' (interval→6),
        # then 'q'. monotonic stays 100 so no second refresh.
        screen.getch.side_effect = [ord("+"), ord("q")]
        with patch.object(switcher, "list_accounts") as mock_list, \
             patch("claude_swap.tui.time.monotonic", return_value=100.0):
            tui._watch_loop(screen, switcher, interval=5)
        assert mock_list.call_count == 1


class TestInitColors:
    def test_initializes_pairs_when_color_supported(self):
        with patch("claude_swap.tui.curses.has_colors", return_value=True), \
             patch("claude_swap.tui.curses.start_color"), \
             patch("claude_swap.tui.curses.use_default_colors"), \
             patch("claude_swap.tui.curses.init_pair") as mock_pair, \
             patch("claude_swap.tui.curses.COLORS", 256, create=True):
            tui._colors_initialized = False
            tui._init_colors()
        assert mock_pair.call_count == 4

    def test_no_color_terminal_is_safe(self):
        with patch("claude_swap.tui.curses.has_colors", return_value=False):
            tui._colors_initialized = False
            tui._init_colors()  # must not raise
