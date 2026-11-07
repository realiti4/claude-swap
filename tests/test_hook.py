"""`cswap hook claude`: usage advice for a Claude Code hook.

Three layers, tested separately:

* `hook.assess` / `hook.advice_text` -- pure functions over the schema-v1
  payload `cswap list --json` produces: when to speak, whom to recommend, what
  the text may contain.
* `hook.should_emit` -- the per-session "already said it" memory.
* `cli._hook_command` -- stdin/stdout protocol, flags, and failing open.

Synthetic payloads only; the real store and the real usage API are never
touched (`tests/test_real_store_guard.py` enforces the first, every test here
patches `list_accounts` for the second).
"""

from __future__ import annotations

import io
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import cli, hook
from claude_swap.switcher import ClaudeAccountSwitcher

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
SECRET_EMAIL = "someone.private@example.invalid"


def _iso(delta: timedelta) -> str:
    return (NOW + delta).isoformat()


def row(
    number: int,
    h5: float | None = 0.0,
    d7: float | None = 0.0,
    *,
    active: bool = False,
    alias: str | None = None,
    disabled: bool = False,
    status: str = "ok",
    h5_resets: timedelta | None = timedelta(hours=3),
    d7_resets: timedelta | None = timedelta(days=3),
) -> dict:
    """One `accounts[]` row as `cswap list --json` emits it."""
    usage: dict | None = None
    if status == "ok":
        usage = {}
        if h5 is not None:
            usage["fiveHour"] = {"pct": h5}
            if h5_resets is not None:
                usage["fiveHour"]["resetsAt"] = _iso(h5_resets)
        if d7 is not None:
            usage["sevenDay"] = {"pct": d7}
            if d7_resets is not None:
                usage["sevenDay"]["resetsAt"] = _iso(d7_resets)
    out = {
        "number": number,
        "email": SECRET_EMAIL,
        "active": active,
        "usageStatus": status,
        "usage": usage,
    }
    if alias:
        out["alias"] = alias
    if disabled:
        out["disabled"] = True
    return out


def payload(*rows: dict) -> dict:
    active = next((r["number"] for r in rows if r.get("active")), None)
    return {"schemaVersion": 1, "activeAccountNumber": active, "accounts": list(rows)}


def assess(*rows: dict, **kw):
    return hook.assess(payload(*rows), now=NOW, **kw)


@pytest.fixture(autouse=True)
def _state_file(tmp_path: Path):
    """Keep the per-session memory out of the real cache directory."""
    with patch.object(hook, "STATE_PATH", tmp_path / "claude_hook.json"):
        yield tmp_path / "claude_hook.json"


@pytest.fixture(autouse=True)
def _pinned_clock():
    """`hook.run` and the CLI read the clock themselves: pin it to NOW."""
    with patch.object(hook, "_utcnow", return_value=NOW):
        yield


class TestAssess:
    """When the hook speaks, and about whom."""

    def test_silent_while_the_active_account_is_under_both_lines(self):
        assert assess(row(1, 79.9, 89.9, active=True), row(2, 0, 0)) is None

    def test_a_window_exactly_at_its_line_counts_as_over(self):
        result = assess(row(1, 80.0, 10, active=True), row(2, 0, 0))
        assert result is not None and result.level == "active_high"
        assert assess(row(1, 10, 90.0, active=True), row(2, 0, 0)) is not None

    def test_the_weekly_line_is_separate_from_the_five_hour_line(self):
        # 85% is over the 5h line (80) but under the weekly line (90), and vice versa.
        only_5h = assess(row(1, 85, 85, active=True), row(2, 0, 0))
        assert [w.label for w in only_5h.active.over] == ["5h"]
        only_7d = assess(row(1, 10, 95, active=True), row(2, 0, 0))
        assert [w.label for w in only_7d.active.over] == ["7d"]

    def test_custom_lines(self):
        quiet = assess(row(1, 85, 85, active=True), row(2, 0, 0), threshold=90, weekly_threshold=95)
        assert quiet is None
        loud = assess(row(1, 50, 50, active=True), row(2, 0, 0), threshold=50, weekly_threshold=99)
        assert loud is not None

    def test_recommends_the_account_with_the_most_room(self):
        result = assess(
            row(1, 95, 10, active=True),
            row(2, 40, 30),  # 60 left
            row(3, 20, 60),  # 40 left on its fullest window
            row(4, 10, 10),  # 90 left: the most room
        )
        assert result.level == "active_high"
        assert result.best_other.number == 4

    def test_an_account_over_one_of_its_own_lines_is_not_a_target(self):
        result = assess(row(1, 95, 10, active=True), row(2, 85, 0), row(3, 70, 70))
        assert result.best_other.number == 3

    def test_a_disabled_account_is_never_suggested(self):
        result = assess(row(1, 95, 10, active=True), row(2, 0, 0, disabled=True))
        assert result.level == "all_high"
        assert result.best_other is None

    def test_accounts_without_a_decision_grade_reading_are_ignored(self):
        result = assess(
            row(1, 95, 10, active=True),
            row(2, status="relogin_required"),
            row(3, status="unavailable"),
        )
        assert result.level == "all_high"

    def test_every_usable_account_over_a_line_is_all_high(self):
        result = assess(row(1, 95, 10, active=True), row(2, 10, 95), row(3, 99, 99))
        assert result.level == "all_high"
        assert result.best_other is None

    def test_a_single_account_over_its_line_is_all_high(self):
        assert assess(row(1, 95, 10, active=True)).level == "all_high"

    def test_silent_without_a_usable_active_account(self):
        assert assess(row(1, 95, 95), row(2, 0, 0)) is None  # none active
        assert assess(row(1, status="relogin_required", active=True), row(2, 99, 99)) is None
        assert hook.assess(payload(), now=NOW) is None

    def test_a_window_whose_reset_has_passed_counts_as_empty(self):
        past = row(1, 99, 10, active=True, h5_resets=timedelta(minutes=-5))
        assert assess(past, row(2, 0, 0)) is None

    def test_a_missing_reset_time_keeps_the_reading(self):
        no_reset = row(1, 99, 10, active=True, h5_resets=None)
        assert assess(no_reset, row(2, 0, 0)).level == "active_high"

    def test_a_window_that_is_absent_is_skipped_not_zeroed(self):
        only_week = row(1, None, 95, active=True)
        result = assess(only_week, row(2, 0, 0))
        assert [w.label for w in result.active.windows] == ["7d"]

    def test_relief_is_the_earliest_across_accounts_when_all_are_known(self):
        result = assess(
            row(1, 95, 10, active=True, h5_resets=timedelta(hours=2)),
            row(2, 99, 10, h5_resets=timedelta(minutes=40)),
        )
        assert result.level == "all_high"
        assert result.relief_at == NOW + timedelta(minutes=40)

    def test_relief_is_not_claimed_when_an_account_has_no_reset_time(self):
        result = assess(
            row(1, 95, 10, active=True, h5_resets=timedelta(hours=2)),
            row(2, 99, 10, h5_resets=None),
        )
        assert result.relief_at is None

    def test_relief_waits_for_every_over_window_of_an_account(self):
        both = row(1, 95, 95, active=True, h5_resets=timedelta(hours=1), d7_resets=timedelta(days=2))
        assert assess(both, row(2, 0, 0)).active.relief_at == NOW + timedelta(days=2)

    @pytest.mark.parametrize(
        "bad",
        [None, [], "x", {}, {"accounts": None}, {"accounts": "no"}, {"accounts": [None, 3, "x"]}],
    )
    def test_garbage_payloads_are_silent(self, bad):
        assert hook.assess(bad, now=NOW) is None

    def test_garbage_readings_are_skipped(self):
        junk = {
            "number": 1, "active": True, "usageStatus": "ok",
            "usage": {"fiveHour": {"pct": "lots"}, "sevenDay": {"pct": True}},
        }
        assert hook.assess(payload(junk), now=NOW) is None
        bad_number = dict(row(1, 99, 99, active=True), number="1")
        assert hook.assess(payload(bad_number), now=NOW) is None


class TestAdviceText:
    """What the model is told."""

    def test_active_high_names_the_account_the_window_and_the_way_out(self):
        result = assess(
            row(2, 91, 40, active=True, alias="work", h5_resets=timedelta(minutes=56)),
            row(3, 12, 31),
        )
        text = hook.advice_text(result, now=NOW)
        assert "active account #2 (work)" in text
        assert "5h window at 91% (resets in 56m)" in text
        assert "Account #3 has more room (5h 12%, 7d 31%)" in text
        assert "`cswap switch 3`" in text
        assert "cswap auto" in text

    def test_only_the_windows_over_their_line_are_named_for_the_active_account(self):
        result = assess(row(2, 91, 40, active=True), row(3, 0, 0))
        text = hook.advice_text(result, now=NOW)
        assert "5h window at 91%" in text
        assert "7d window" not in text

    def test_all_high_says_slow_down_and_does_not_offer_a_switch(self):
        result = assess(row(1, 95, 10, active=True), row(2, 99, 99))
        text = hook.advice_text(result, now=NOW)
        assert "every managed account with a usable reading" in text
        assert "fewer parallel subagents" in text
        assert "smaller model" in text
        assert "cswap switch" not in text

    def test_all_high_mentions_earliest_relief_only_when_it_differs(self):
        same = assess(row(1, 95, 10, active=True, h5_resets=timedelta(hours=1)))
        assert "Earliest relief" not in hook.advice_text(same, now=NOW)
        sooner = assess(
            row(1, 95, 10, active=True, h5_resets=timedelta(hours=2)),
            row(2, 99, 10, h5_resets=timedelta(minutes=40)),
        )
        assert "Earliest relief on any account: in 40m." in hook.advice_text(sooner, now=NOW)

    def test_an_email_address_never_reaches_the_text(self):
        for rows in (
            (row(1, 95, 10, active=True, alias="a"), row(2, 0, 0, alias="b")),
            (row(1, 95, 10, active=True), row(2, 99, 99)),
        ):
            text = hook.advice_text(assess(*rows), alternative="x", now=NOW)
            assert SECRET_EMAIL not in text and "@" not in text

    def test_the_alternative_is_appended_verbatim_on_its_own_line(self):
        for rows in (
            (row(1, 95, 10, active=True), row(2, 0, 0)),
            (row(1, 95, 10, active=True), row(2, 99, 99)),
        ):
            text = hook.advice_text(assess(*rows), alternative="  Use the other agent: `run-it`.  ", now=NOW)
            assert text.endswith("\nUse the other agent: `run-it`.")

    @pytest.mark.parametrize("alt", [None, "", "   \n"])
    def test_without_a_real_alternative_nothing_of_the_kind_is_said(self, alt):
        text = hook.advice_text(assess(row(1, 95, 10, active=True), row(2, 99, 99)), alternative=alt, now=NOW)
        assert "\n" not in text

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (0, "<1m"), (59, "<1m"), (60, "1m"), (45 * 60, "45m"), (3600, "1h 0m"),
            (2 * 3600 + 10 * 60, "2h 10m"), (86400, "1d 0h"), (3 * 86400 + 4 * 3600 + 59, "3d 4h"),
        ],
    )
    def test_format_countdown(self, seconds, expected):
        assert hook.format_countdown(seconds) == expected


class TestShouldEmit:
    """Say it once per session, again when the situation changes."""

    def test_first_time_yes_then_quiet_then_again_after_the_interval(self):
        assert hook.should_emit("s1", "fp", 600, now=1000.0)
        assert not hook.should_emit("s1", "fp", 600, now=1100.0)
        assert not hook.should_emit("s1", "fp", 600, now=1599.0)
        assert hook.should_emit("s1", "fp", 600, now=1700.0)

    def test_a_changed_situation_speaks_up_immediately(self):
        assert hook.should_emit("s1", "active_high|1|5h|2", 600, now=1000.0)
        assert hook.should_emit("s1", "all_high|1|5h|-", 600, now=1001.0)

    def test_sessions_are_independent(self):
        assert hook.should_emit("s1", "fp", 600, now=1000.0)
        assert hook.should_emit("s2", "fp", 600, now=1001.0)
        assert not hook.should_emit("s1", "fp", 600, now=1002.0)

    def test_repeat_zero_means_every_time_and_writes_nothing(self, _state_file: Path):
        assert hook.should_emit("s1", "fp", 0, now=1000.0)
        assert hook.should_emit("s1", "fp", 0, now=1001.0)
        assert not _state_file.exists()

    def test_a_corrupt_state_file_just_means_emit(self, _state_file: Path):
        _state_file.write_text("{not json")
        assert hook.should_emit("s1", "fp", 600, now=1000.0)
        _state_file.write_text(json.dumps({"timestamp": 1e18, "data": ["x"]}))
        assert hook.should_emit("s1", "fp", 600, now=1000.0)

    def test_malformed_entries_in_the_state_file_are_dropped_not_fatal(self, _state_file: Path):
        _state_file.write_text(json.dumps({"timestamp": 4102444800, "data": {"s0": "x", "s1": 3, "s2": {"fp": "fp", "ts": 1}}}))
        assert hook.should_emit("s0", "fp", 600, now=1000.0)
        assert not hook.should_emit("s0", "fp", 600, now=1001.0)  # memory works again afterwards

    def test_a_clock_that_went_backwards_does_not_silence_the_hook_forever(self):
        assert hook.should_emit("s1", "fp", 600, now=5000.0)
        assert hook.should_emit("s1", "fp", 600, now=1000.0)

    def test_the_memory_is_bounded(self, _state_file: Path):
        for i in range(hook.STATE_MAX_SESSIONS + 25):
            hook.should_emit(f"s{i}", "fp", 600, now=1000.0 + i)
        stored = json.loads(_state_file.read_text())["data"]
        assert len(stored) == hook.STATE_MAX_SESSIONS
        assert f"s{hook.STATE_MAX_SESSIONS + 24}" in stored and "s0" not in stored

    def test_an_unwritable_state_path_never_blocks_the_advice(self, tmp_path: Path):
        blocker = tmp_path / "file"
        blocker.write_text("x")
        with patch.object(hook, "STATE_PATH", blocker / "sub" / "state.json"):
            assert hook.should_emit("s1", "fp", 600, now=1000.0)


class TestRun:
    """Payload + hook input -> the object Claude Code reads."""

    KW = dict(threshold=80.0, weekly_threshold=90.0, alternative=None, repeat_s=0.0)

    def high(self) -> dict:
        return payload(row(1, 95, 10, active=True), row(2, 0, 0))

    def test_shape_and_event_echo(self):
        for event in sorted(hook.CONTEXT_EVENTS):
            out = hook.run(self.high(), {"hook_event_name": event, "session_id": "s"}, **self.KW)
            assert out["hookSpecificOutput"]["hookEventName"] == event
            assert out["hookSpecificOutput"]["additionalContext"].startswith("Claude usage is high")
            assert set(out) == {"hookSpecificOutput"}

    @pytest.mark.parametrize("hook_input", [{}, {"hook_event_name": None}, {"session_id": "s"}])
    def test_a_missing_event_name_behaves_as_a_prompt_hook(self, hook_input):
        out = hook.run(self.high(), hook_input, **self.KW)
        assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"

    @pytest.mark.parametrize("event", ["Stop", "Notification", "SessionEnd", "", 5, ["PreToolUse"], {"a": 1}])
    def test_an_event_this_command_does_not_answer_gets_no_output(self, event):
        """Only the events in CONTEXT_EVENTS are answered; Claude Code also rejects output
        that names another event than the one it ran for."""
        assert hook.run(self.high(), {"hook_event_name": event, "session_id": "s"}, **self.KW) is None

    def test_silent_when_there_is_nothing_to_say(self):
        quiet = payload(row(1, 10, 10, active=True))
        assert hook.run(quiet, {"session_id": "s"}, **self.KW) is None

    def test_repeat_applies_per_session_and_a_missing_session_id_is_one_session(self):
        kw = dict(self.KW, repeat_s=600.0)
        assert hook.run(self.high(), {"session_id": "a"}, **kw) is not None
        assert hook.run(self.high(), {"session_id": "a"}, **kw) is None
        assert hook.run(self.high(), {"session_id": "b"}, **kw) is not None
        assert hook.run(self.high(), {}, **kw) is not None
        assert hook.run(self.high(), {"session_id": ""}, **kw) is None

    def test_each_event_has_its_own_memory_within_a_session(self):
        kw = dict(self.KW, repeat_s=600.0)
        prompt = {"hook_event_name": "UserPromptSubmit", "session_id": "a"}
        tool = {"hook_event_name": "PreToolUse", "session_id": "a"}
        assert hook.run(self.high(), prompt, **kw) is not None
        assert hook.run(self.high(), tool, **kw) is not None  # not silenced by the prompt
        assert hook.run(self.high(), prompt, **kw) is None
        assert hook.run(self.high(), tool, **kw) is None

    def test_parse_hook_input(self):
        assert hook.parse_hook_input('{"session_id": "x"}') == {"session_id": "x"}
        for bad in ("", "  \n", "not json", "[1]", "3", "null"):
            assert hook.parse_hook_input(bad) == {}


class TestCli:
    """`cswap hook claude` as Claude Code runs it: JSON in on stdin, JSON out."""

    HIGH = payload(row(1, 95, 10, active=True, alias="work"), row(2, 5, 5))
    QUIET = payload(row(1, 10, 10, active=True), row(2, 5, 5))

    @staticmethod
    def run_cli(argv, stdin_text, monkeypatch, *, result=None, error=None):
        """Drive `_hook_command`; `list_accounts` is replaced, nothing real is read."""
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_text))
        with patch("os.geteuid", return_value=1000, create=True), patch.object(
            ClaudeAccountSwitcher, "list_accounts", side_effect=error, return_value=result
        ) as listing:
            cli._hook_command(argv)
        return listing

    def test_prints_one_json_object_when_usage_is_high(self, temp_home, monkeypatch, capsys):
        stdin = json.dumps({"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "Agent"})
        self.run_cli(["claude"], stdin, monkeypatch, result=self.HIGH)
        out = capsys.readouterr()
        assert out.err == ""
        assert len(out.out.strip().splitlines()) == 1
        body = json.loads(out.out)["hookSpecificOutput"]
        assert body["hookEventName"] == "PreToolUse"
        assert "#1 (work)" in body["additionalContext"]
        assert "cswap switch 2" in body["additionalContext"]

    def test_prints_nothing_when_usage_is_fine(self, temp_home, monkeypatch, capsys):
        self.run_cli(["claude"], "{}", monkeypatch, result=self.QUIET)
        assert capsys.readouterr() == ("", "")

    def test_works_without_any_stdin_and_with_garbage_stdin(self, temp_home, monkeypatch, capsys):
        for stdin in ("", "garbage"):
            self.run_cli(["claude", "--repeat", "0"], stdin, monkeypatch, result=self.HIGH)
            body = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
            assert body["hookEventName"] == "UserPromptSubmit"

    def test_an_interactive_terminal_is_not_read(self, temp_home, monkeypatch, capsys):
        class Tty(io.StringIO):
            def isatty(self):
                return True

            def read(self, *a):  # pragma: no cover -- must not be called
                raise AssertionError("stdin was read from a terminal")

        monkeypatch.setattr(sys, "stdin", Tty())
        with patch("os.geteuid", return_value=1000, create=True), patch.object(
            ClaudeAccountSwitcher, "list_accounts", return_value=self.HIGH
        ):
            cli._hook_command(["claude"])
        assert "additionalContext" in capsys.readouterr().out

    def test_cache_only_never_asks_for_a_fetch(self, temp_home, monkeypatch):
        listing = self.run_cli(["claude", "--cache-only"], "{}", monkeypatch, result=self.QUIET)
        listing.assert_called_once_with(json_output=True, fetch=set())
        listing = self.run_cli(["claude"], "{}", monkeypatch, result=self.QUIET)
        listing.assert_called_once_with(json_output=True, fetch=None)

    def test_flags_reach_the_advice(self, temp_home, monkeypatch, capsys):
        # 95% is under a 99 line: silent. Lowering the line to 50 makes 40/60 speak.
        self.run_cli(["claude", "--threshold", "99"], "{}", monkeypatch, result=self.HIGH)
        assert capsys.readouterr().out == ""
        mid = payload(row(1, 60, 10, active=True), row(2, 5, 5))
        self.run_cli(["claude", "--threshold", "50", "--alternative", "Try the other agent."], "{}", monkeypatch, result=mid)
        text = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
        assert text.endswith("\nTry the other agent.")

    def test_repeat_is_honoured_across_invocations(self, temp_home, monkeypatch, capsys):
        stdin = json.dumps({"session_id": "same"})
        self.run_cli(["claude"], stdin, monkeypatch, result=self.HIGH)
        assert capsys.readouterr().out != ""
        self.run_cli(["claude"], stdin, monkeypatch, result=self.HIGH)
        assert capsys.readouterr().out == ""
        self.run_cli(["claude", "--repeat", "0"], stdin, monkeypatch, result=self.HIGH)
        assert capsys.readouterr().out != ""

    def test_a_failing_store_fails_open_and_quietly(self, temp_home, monkeypatch, capsys):
        self.run_cli(["claude"], "{}", monkeypatch, error=RuntimeError("store exploded"))
        assert capsys.readouterr() == ("", "")

    def test_debug_shows_why(self, temp_home, monkeypatch, capsys):
        self.run_cli(["claude", "--debug"], "{}", monkeypatch, error=RuntimeError("store exploded"))
        out = capsys.readouterr()
        assert out.out == ""
        assert "store exploded" in out.err

    def test_a_garbage_payload_from_the_store_is_silent(self, temp_home, monkeypatch, capsys):
        for result in (None, {}, {"accounts": "x"}):
            self.run_cli(["claude"], "{}", monkeypatch, result=result)
            assert capsys.readouterr() == ("", "")

    def test_stray_prints_while_opening_the_store_never_reach_stdout(self, temp_home, monkeypatch, capsys):
        def noisy(**_kw):
            print("migrating something...")
            return self.HIGH

        monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
        with patch("os.geteuid", return_value=1000, create=True), patch.object(
            ClaudeAccountSwitcher, "list_accounts", side_effect=noisy
        ):
            cli._hook_command(["claude"])
        out = capsys.readouterr()
        assert json.loads(out.out)["hookSpecificOutput"]
        assert "migrating something" in out.err

    @pytest.mark.parametrize(
        "argv",
        [
            [],
            ["other"],
            ["claude", "--bogus"],
            ["claude", "--threshold", "0"],
            ["claude", "--threshold", "101"],
            ["claude", "--threshold", "abc"],
            ["claude", "--weekly-threshold", "-5"],
            ["claude", "--repeat", "-1"],
            ["claude", "--repeat", "nan"],
            ["claude", "--repeat", "inf"],
        ],
    )
    def test_a_bad_command_line_exits_1_never_2(self, argv, monkeypatch, capsys):
        """Exit 2 from a Claude Code hook blocks the event; a typo must not."""
        monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
        with pytest.raises(SystemExit) as exc:
            cli._hook_command(argv)
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "usage: cswap hook" in err and "error:" in err

    def test_help_exits_0_and_shows_the_settings_snippet(self, capsys):
        with pytest.raises(SystemExit) as exc:
            cli._hook_command(["--help"])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "UserPromptSubmit" in out and "--alternative" in out

    def test_percent_bounds_are_inclusive_at_100(self, temp_home, monkeypatch, capsys):
        self.run_cli(["claude", "--threshold", "100", "--weekly-threshold", "100"], "{}", monkeypatch, result=self.QUIET)
        assert capsys.readouterr() == ("", "")

    def test_main_dispatches_the_hook_without_the_update_check(self, temp_home, monkeypatch, capsys):
        monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
        with patch.object(sys, "argv", ["cswap", "hook", "claude"]), patch(
            "os.geteuid", return_value=1000, create=True
        ), patch(
            "claude_swap.update_check.check_for_update", side_effect=AssertionError("update check ran")
        ), patch.object(
            ClaudeAccountSwitcher, "list_accounts", return_value=self.HIGH
        ):
            cli.main()
        assert "additionalContext" in capsys.readouterr().out

    @pytest.mark.skipif(sys.platform == "win32", reason="root guard is POSIX-only")
    def test_the_root_guard_applies_like_for_every_other_command(self, temp_home, monkeypatch, capsys):
        monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
        with patch("os.geteuid", return_value=0, create=True), patch.object(
            ClaudeAccountSwitcher, "_is_running_in_container", return_value=False
        ), patch.object(ClaudeAccountSwitcher, "list_accounts", return_value=self.HIGH):
            with pytest.raises(SystemExit) as exc:
                cli._hook_command(["claude"])
        assert exc.value.code == 1
        assert "root" in capsys.readouterr().err

    def test_inside_a_cswap_run_session_shell_it_stays_silent(self, temp_home, monkeypatch, capsys):
        """The default login's "active account" is not the one a `cswap run` session uses."""
        from claude_swap import paths

        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(paths.get_backup_root() / "sessions" / "2-work"))
        listing = self.run_cli(["claude"], "{}", monkeypatch, result=self.HIGH)
        assert capsys.readouterr().out == ""
        listing.assert_not_called()

    def test_input_that_the_locale_cannot_decode_still_yields_advice(self, temp_home, monkeypatch, capsys):
        """A Windows code page must not cost the advice: only ASCII fields are needed."""
        raw = b'{"hook_event_name": "PreToolUse", "session_id": "s", "prompt": "caf\xc3\xa9 \x81\x8d"}'
        stdin = io.TextIOWrapper(io.BytesIO(raw), encoding="cp1252", errors="strict")
        monkeypatch.setattr(sys, "stdin", stdin)
        with patch("os.geteuid", return_value=1000, create=True), patch.object(
            ClaudeAccountSwitcher, "list_accounts", return_value=self.HIGH
        ):
            cli._hook_command(["claude"])
        assert json.loads(capsys.readouterr().out)["hookSpecificOutput"]["hookEventName"] == "PreToolUse"

    def test_help_text_lists_the_command(self, capsys):
        with patch.object(sys, "argv", ["cswap", "help"]), pytest.raises(SystemExit):
            cli.main()
        assert "hook claude" in capsys.readouterr().out


class TestThresholdArithmetic:
    """The lines are inclusive and the maths is plain percent."""

    @pytest.mark.parametrize(
        ("pct", "line", "over"),
        [(79.99, 80, False), (80, 80, True), (100, 100, True), (0, 80, False), (99.9, 100, False)],
    )
    def test_over(self, pct, line, over):
        assert hook.Window("5h", pct, line).over is over

    def test_headroom_is_the_fullest_window(self):
        view = hook.AccountView(1, None, False, (hook.Window("5h", 30, 80), hook.Window("7d", 60, 90)))
        assert math.isclose(view.headroom, 40.0)
