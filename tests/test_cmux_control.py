"""Tests for the cmux /rc re-arm sweep (cmux_control.py)."""

from __future__ import annotations

import json
from unittest.mock import patch

from claude_swap import cmux_control
from claude_swap.cmux_control import (
    Surface,
    SweepResult,
    list_surfaces,
    rearm_remote_control,
)
from claude_swap.process_detection import ClaudeSession


def _tree(surfaces):
    """A minimal cmux `tree --all --json` payload holding the given surfaces."""
    return json.dumps({
        "active": {},
        "windows": [{
            "workspaces": [{
                "panes": [{
                    "surfaces": [
                        {
                            "ref": ref, "type": kind, "tty": tty,
                            "title": title, "pane_ref": "pane:1",
                        }
                        for ref, kind, tty, title in surfaces
                    ],
                }],
            }],
        }],
    })


def _session(pid, sid="s1"):
    return ClaudeSession(
        pid=pid, session_id=sid, cwd="/w", started_at=0,
        kind="interactive", entrypoint="cli",
    )


class TestListSurfaces:
    def test_keeps_only_terminals_with_a_tty(self):
        out = _tree([
            ("surface:1", "terminal", "ttys001", "claude"),
            ("surface:2", "terminal", None, "no tty"),
            ("surface:3", "browser", "ttys002", "web"),
        ])
        got = list_surfaces("/bin/cmux", runner=lambda b, a: out)
        assert got == [Surface(ref="surface:1", tty="ttys001", title="claude")]

    def test_walks_every_window(self):
        payload = json.loads(_tree([("surface:1", "terminal", "ttys001", "a")]))
        payload["windows"].append(
            json.loads(_tree([("surface:9", "terminal", "ttys009", "b")]))["windows"][0]
        )
        got = list_surfaces("/bin/cmux", runner=lambda b, a: json.dumps(payload))
        assert [s.ref for s in got] == ["surface:1", "surface:9"]


class TestRearmRemoteControl:
    def test_none_when_cmux_absent(self):
        with patch.object(cmux_control, "find_cmux", return_value=None):
            assert rearm_remote_control() is None

    def _run(self, sessions, ttys, own_tty=None, send_fails=frozenset(),
             active_within_s=0.0, idle_by_tty=None):
        """Drive a sweep with fake surfaces/sessions; returns (result, sent)."""
        tree = _tree([
            (f"surface:{i}", "terminal", tty, "t") for i, tty in enumerate(ttys)
        ])
        sent = []

        def runner(binary, args):
            if args[0] == "tree":
                return tree
            assert args[:2] == ["send", "--surface"]
            if args[2] in send_fails:
                raise RuntimeError("send failed")
            sent.append((args[2], args[-1]))
            return ""

        pid_tty = {s.pid: t for s, t in zip(sessions, ttys)}
        with (
            patch.object(cmux_control, "list_sessions", return_value=sessions),
            patch.object(cmux_control, "_tty_of_pid", side_effect=pid_tty.get),
            patch.object(cmux_control, "_own_tty", return_value=own_tty),
            patch.object(cmux_control, "_tty_idle_seconds",
                         side_effect=(idle_by_tty or {}).get),
        ):
            result = rearm_remote_control(
                binary="/bin/cmux", runner=runner,
                active_within_s=active_within_s,
            )
        return result, sent

    def test_sends_rc_to_each_claude_surface(self):
        result, sent = self._run(
            [_session(10, "a"), _session(11, "b")], ["ttys000", "ttys001"]
        )
        assert result == SweepResult(
            sent=["surface:0", "surface:1"], skipped_self=None, no_surface=0
        )
        assert all(text == "/rc\r" for _, text in sent)

    def test_never_targets_own_surface(self):
        result, sent = self._run(
            [_session(10, "a"), _session(11, "b")],
            ["ttys000", "ttys001"],
            own_tty="ttys001",
        )
        assert result.sent == ["surface:0"]
        assert result.skipped_self == "surface:1"
        assert [ref for ref, _ in sent] == ["surface:0"]

    def test_idle_session_is_skipped_when_filter_is_on(self):
        result, sent = self._run(
            [_session(10, "a"), _session(11, "b")],
            ["ttys000", "ttys001"],
            active_within_s=3600,
            idle_by_tty={"ttys000": 30.0, "ttys001": 7200.0},
        )
        assert result.sent == ["surface:0"]
        assert result.skipped_idle == 1
        assert [ref for ref, _ in sent] == ["surface:0"]

    def test_zero_threshold_sweeps_idle_sessions_too(self):
        result, _ = self._run(
            [_session(10, "a")], ["ttys000"],
            active_within_s=0.0,
            idle_by_tty={"ttys000": 7200.0},
        )
        assert result.sent == ["surface:0"]
        assert result.skipped_idle == 0

    def test_unknown_idle_fails_open_and_sweeps(self):
        # No /dev/<tty> to stat -> idle is None -> the session is swept:
        # skipping a live session on a missing stat would be a new bug.
        result, _ = self._run(
            [_session(10, "a")], ["ttys000"],
            active_within_s=3600,
            idle_by_tty={},
        )
        assert result.sent == ["surface:0"]
        assert result.skipped_idle == 0

    def test_sessions_outside_cmux_are_counted_not_typed_at(self):
        # Session 12's tty has no surface: typing /rc would land nowhere —
        # and a session with NO tty (pid unknown to ps) must not match either.
        sessions = [_session(10, "a"), _session(12, "gone")]
        tree = _tree([("surface:0", "terminal", "ttys000", "t")])
        sent = []

        def runner(binary, args):
            if args[0] == "tree":
                return tree
            sent.append(args[2])
            return ""

        with (
            patch.object(cmux_control, "list_sessions", return_value=sessions),
            patch.object(
                cmux_control, "_tty_of_pid",
                side_effect={10: "ttys000", 12: None}.get,
            ),
            patch.object(cmux_control, "_own_tty", return_value=None),
        ):
            result = rearm_remote_control(binary="/bin/cmux", runner=runner)
        assert result.sent == ["surface:0"]
        assert result.no_surface == 1

    def test_one_failed_send_does_not_stop_the_sweep(self):
        result, sent = self._run(
            [_session(10, "a"), _session(11, "b")],
            ["ttys000", "ttys001"],
            send_fails={"surface:0"},
        )
        assert result.sent == ["surface:1"]

    def test_listing_failure_returns_none(self):
        def runner(binary, args):
            raise RuntimeError("cmux exploded")

        assert rearm_remote_control(binary="/bin/cmux", runner=runner) is None


class TestEngineWiring:
    """The engine sweeps on its own switches, gated by the setting."""

    def _switch(self, temp_home, **settings_kwargs):
        import sys, pathlib
        sys.path.insert(0, str(pathlib.Path(__file__).parent))
        from test_autoswitch import EngineHarness, _usage

        h = EngineHarness(temp_home, threshold=90.0, **settings_kwargs)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)
        return h, {"1": _usage(95.0), "2": _usage(10.0)}

    def test_switch_sweeps_when_enabled(self, temp_home):
        h, usage = self._switch(temp_home, rearm_remote_control=True)
        with patch.object(
            cmux_control, "rearm_remote_control",
            return_value=SweepResult(sent=["surface:1"], skipped_self=None, no_surface=0),
        ) as sweep:
            h.tick_with_usage(usage)
        assert sweep.called
        assert "remote-control-rearmed" in h.kinds()

    def test_off_by_default(self, temp_home):
        h, usage = self._switch(temp_home)
        with patch.object(cmux_control, "rearm_remote_control") as sweep:
            h.tick_with_usage(usage)
        assert "switch" in h.kinds()
        assert not sweep.called

    def test_sweep_failure_never_breaks_the_switch(self, temp_home):
        h, usage = self._switch(temp_home, rearm_remote_control=True)
        with patch.object(
            cmux_control, "rearm_remote_control",
            side_effect=RuntimeError("cmux exploded"),
        ):
            h.tick_with_usage(usage)
        assert "switch" in h.kinds()
        assert h.active_number() == 2


class TestConfirmAndDismiss:
    """The sweep's confirm pass: scrape session URLs, close the RC panels."""

    PANEL = (
        "   Remote Control\n"
        "   This session is available in the Claude mobile app and at "
        "https://claude.ai/code/session_01ABC.\n"
        "   ❯ Continue\n   Enter to select · Esc to continue\n"
    )

    def _sweep(self, screens):
        """Run a confirmed sweep over one session per screen text."""
        ttys = [f"ttys00{i}" for i in range(len(screens))]
        tree = _tree([
            (f"surface:{i}", "terminal", tty, "t") for i, tty in enumerate(ttys)
        ])
        calls = []

        def runner(binary, args):
            calls.append(args)
            if args[0] == "tree":
                return tree
            if args[0] == "read-screen":
                ref = args[2]
                return screens[int(ref.split(":")[1])]
            return ""

        sessions = [_session(10 + i, f"s{i}") for i in range(len(screens))]
        pid_tty = {s.pid: t for s, t in zip(sessions, ttys)}
        with (
            patch.object(cmux_control, "list_sessions", return_value=sessions),
            patch.object(cmux_control, "_tty_of_pid", side_effect=pid_tty.get),
            patch.object(cmux_control, "_own_tty", return_value=None),
        ):
            result = rearm_remote_control(
                binary="/bin/cmux", runner=runner, confirm=True,
                sleeper=lambda s: None,
            )
        return result, calls

    def test_scrapes_url_and_dismisses_panel(self):
        result, calls = self._sweep([self.PANEL])
        assert result.confirmed == ["surface:0"]
        assert result.urls == ["https://claude.ai/code/session_01ABC"]
        # The panel captures input until Esc — a raw ESC byte must follow.
        assert ["send", "--surface", "surface:0", "--", "\x1b"] in calls

    def test_mid_turn_session_is_not_confirmed_and_not_escaped(self):
        # No panel on screen (the /rc is still queued): nothing to dismiss,
        # and typing Esc into a busy session would be a stray keystroke.
        result, calls = self._sweep(["$ some shell output, no panel\n"])
        assert result.confirmed == []
        assert result.urls == []
        assert ["send", "--surface", "surface:0", "--", "\x1b"] not in calls

    def test_one_unreadable_surface_does_not_stop_the_pass(self):
        screens = {1: self.PANEL}

        def flaky(binary, args):
            if args[0] == "tree":
                return _tree([
                    ("surface:0", "terminal", "ttys000", "t"),
                    ("surface:1", "terminal", "ttys001", "t"),
                ])
            if args[0] == "read-screen":
                idx = int(args[2].split(":")[1])
                if idx not in screens:
                    raise RuntimeError("read failed")
                return screens[idx]
            return ""

        sessions = [_session(10, "a"), _session(11, "b")]
        with (
            patch.object(cmux_control, "list_sessions", return_value=sessions),
            patch.object(
                cmux_control, "_tty_of_pid",
                side_effect={10: "ttys000", 11: "ttys001"}.get,
            ),
            patch.object(cmux_control, "_own_tty", return_value=None),
        ):
            result = rearm_remote_control(
                binary="/bin/cmux", runner=flaky, confirm=True,
                sleeper=lambda s: None,
            )
        assert result.confirmed == ["surface:1"]
        assert result.urls == ["https://claude.ai/code/session_01ABC"]

    def test_default_sweep_never_reads_screens(self):
        # confirm=False must stay a pure sweep — no read-screen, no Esc.
        tree = _tree([("surface:0", "terminal", "ttys000", "t")])
        calls = []

        def runner(binary, args):
            calls.append(args[0])
            return tree if args[0] == "tree" else ""

        with (
            patch.object(cmux_control, "list_sessions", return_value=[_session(10)]),
            patch.object(cmux_control, "_tty_of_pid", return_value="ttys000"),
            patch.object(cmux_control, "_own_tty", return_value=None),
        ):
            rearm_remote_control(binary="/bin/cmux", runner=runner)
        assert "read-screen" not in calls

    def test_url_in_scrollback_without_panel_is_ignored(self):
        # A busy session's screen can carry session URLs (test output, docs)
        # without the panel. No ESC — it would interrupt the running turn —
        # and no scrape: that URL is data on screen, not the panel's.
        screen = (
            "assert url == 'https://claude.ai/code/session_fake'\n"
            "reading Remote Control docs...\n"   # one marker, not both
        )
        result, calls = self._sweep([screen])
        assert result.confirmed == []
        assert result.urls == []
        assert not any(a[-1] == "\x1b" for a in calls if a[0] == "send")


class TestCaptureScreenForPid:
    """capture_screen_for_pid — read-only screen grab for 6b's dialog corpus."""

    def _runner(self, screens):
        def runner(binary, args):
            if args[0] == "tree":
                return _tree([("surface:7", "terminal", "ttys007", "claude")])
            if args[0] == "read-screen":
                return screens[args[2]]
            raise AssertionError(f"unexpected cmux call: {args}")
        return runner

    def test_captures_screen_of_hosting_surface(self):
        with patch.object(cmux_control, "_tty_of_pid", return_value="ttys007"):
            got = cmux_control.capture_screen_for_pid(
                42, binary="/bin/cmux",
                runner=self._runner({"surface:7": "limit dialog text"}),
            )
        assert got == ("surface:7", "limit dialog text")

    def test_pid_without_surface_is_none(self):
        with patch.object(cmux_control, "_tty_of_pid", return_value="ttys099"):
            got = cmux_control.capture_screen_for_pid(
                42, binary="/bin/cmux", runner=self._runner({})
            )
        assert got is None

    def test_no_tty_is_none_without_cmux_calls(self):
        def runner(binary, args):
            raise AssertionError("cmux must not be called when the pid has no tty")
        with patch.object(cmux_control, "_tty_of_pid", return_value=None):
            assert cmux_control.capture_screen_for_pid(
                42, binary="/bin/cmux", runner=runner
            ) is None

    def test_read_screen_failure_is_none(self):
        def runner(binary, args):
            if args[0] == "tree":
                return _tree([("surface:7", "terminal", "ttys007", "claude")])
            raise RuntimeError("boom")
        with patch.object(cmux_control, "_tty_of_pid", return_value="ttys007"):
            assert cmux_control.capture_screen_for_pid(
                42, binary="/bin/cmux", runner=runner
            ) is None


class TestNudgeViaCmux:
    """nudge_via_cmux — the PTY nudge with dialog handling (backlog 6b).

    Screen markers come from the Claude Code 2.1.248 binary; the fakes here
    reproduce the four screen classes the design distinguishes.
    """

    MENU = "Usage limit reached\n  Adjust monthly spend limit: $40\n> Wait for limit to reset\n"
    BANNER = ("Claude Code will continue automatically when your limit resets. "
              "Press esc to cancel the wait.\n> \n")
    PROMPT = "some scrollback\n> \n"
    RC_PANEL = "Remote Control\nhttps://claude.ai/code/session_abc\nEsc to continue\n"
    RUNNING = "Thinking... esc to interrupt\n"
    TEXT = "[claude-swap] quota is back"

    def _rig(self, screens):
        """A fake runner over a mutable list of screens (popped per read)."""
        calls = []

        def runner(binary, args):
            calls.append(args)
            if args[0] == "tree":
                return _tree([("surface:7", "terminal", "ttys007", "claude")])
            if args[0] == "read-screen":
                return screens.pop(0)
            if args[0] == "send":
                return ""
            raise AssertionError(f"unexpected: {args}")
        return runner, calls

    def _sends(self, calls):
        return [c[-1] for c in calls if c[0] == "send"]

    def _nudge(self, screens):
        runner, calls = self._rig(screens)
        with patch.object(cmux_control, "_tty_of_pid", return_value="ttys007"):
            status = cmux_control.nudge_via_cmux(
                42, self.TEXT, binary="/bin/cmux", runner=runner,
                sleeper=lambda s: None,
            )
        return status, self._sends(calls)

    def test_plain_prompt_types_and_verifies(self):
        status, sends = self._nudge([self.PROMPT, self.PROMPT + self.TEXT])
        assert status == "delivered"
        assert sends == [self.TEXT + "\r"]      # no Esc at a plain prompt

    def test_menu_is_dismissed_with_exactly_one_esc_before_typing(self):
        status, sends = self._nudge([self.MENU, self.PROMPT, self.PROMPT + self.TEXT])
        assert status == "delivered"
        assert sends == ["\x1b", self.TEXT + "\r"]

    def test_menu_that_survives_esc_gets_nothing_typed(self):
        # Enter would SELECT a menu option — never type into a live menu.
        status, sends = self._nudge([self.MENU, self.MENU])
        assert status == "captured-input"
        assert sends == ["\x1b"]                # one Esc, nothing else

    def test_own_rc_panel_is_dismissed_first(self):
        status, sends = self._nudge([self.RC_PANEL, self.PROMPT, self.PROMPT + self.TEXT])
        assert status == "delivered"
        assert sends == ["\x1b", self.TEXT + "\r"]

    def test_armed_wait_banner_is_never_escaped(self):
        # Esc there cancels Claude Code's own auto-continue; typing is a
        # plain manual-submit takeover.
        status, sends = self._nudge([self.BANNER, self.BANNER + self.TEXT])
        assert status == "delivered"
        assert sends == [self.TEXT + "\r"]

    def test_running_session_is_untouched(self):
        status, sends = self._nudge([self.RUNNING])
        assert status == "running"
        assert sends == []

    def test_echo_not_visible_is_typed_unverified(self):
        status, sends = self._nudge([self.PROMPT, self.PROMPT])
        assert status == "typed-unverified"
        assert sends == [self.TEXT + "\r"]

    def test_no_surface(self):
        runner, calls = self._rig([])
        with patch.object(cmux_control, "_tty_of_pid", return_value=None):
            status = cmux_control.nudge_via_cmux(
                42, self.TEXT, binary="/bin/cmux", runner=runner,
                sleeper=lambda s: None,
            )
        assert status == "no-surface"
        assert self._sends(calls) == []
