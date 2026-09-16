"""Tests for the PTY wrapper (cswap wrap)."""

from __future__ import annotations

import os
import stat
import time

import pytest

from claude_swap import wrap
from claude_swap.wrap import WrapSession, earliest_recovery_ts, find_limit_message


# ---------------------------------------------------------------------------
# Limit-message detection
# ---------------------------------------------------------------------------


class TestLimitDetection:
    @pytest.mark.parametrize(
        "text",
        [
            "You've hit your session limit",
            "You've hit your weekly limit",
            "You've hit your Opus limit",
            "You've hit your Sonnet limit",
            "You've hit your Fable limit",
            "You've hit your fast limit",
            "You've hit your monthly limit — raise it below",
            "You've hit your monthly spend limit · your X resets 5pm",
            "You've hit your channel's monthly spend limit.",
            "You've hit your team’s shared budget. Run /usage-credits",
            "You've hit your team's shared budget.",
        ],
    )
    def test_known_variants(self, text):
        assert find_limit_message(text) is not None

    @pytest.mark.parametrize(
        "text",
        [
            "everything is fine",
            "you have hit the road",
            "approaching your session limit",  # warning, not exhaustion
            "",
        ],
    )
    def test_negatives(self, text):
        assert find_limit_message(text) is None

    def test_ansi_split_message(self):
        raw = b"\x1b[1mYou've hit your \x1b[0msession limit\x1b[2m"
        assert find_limit_message(wrap.strip_ansi(raw)) == (
            "You've hit your session limit"
        )

    def test_osc_sequence_stripped(self):
        raw = b"\x1b]0;title\x07You've hit your weekly limit"
        assert find_limit_message(wrap.strip_ansi(raw)) is not None


# ---------------------------------------------------------------------------
# earliest_recovery_ts
# ---------------------------------------------------------------------------


def _usage(five_hour=None, seven_day=None, scoped=None):
    """Build an internal (snake_case) usage dict."""
    usage = {}
    if five_hour is not None:
        usage["five_hour"] = five_hour
    if seven_day is not None:
        usage["seven_day"] = seven_day
    if scoped is not None:
        usage["scoped"] = scoped
    return usage


def _window(pct, resets_at):
    return {"pct": pct, "resets_at": resets_at}


class TestEarliestRecovery:
    NOW = 1_800_000_000.0

    def _iso(self, delta_s):
        from datetime import datetime, timezone

        return datetime.fromtimestamp(self.NOW + delta_s, tz=timezone.utc).isoformat()

    def test_single_exhausted_account(self):
        usage = {"1": _usage(five_hour=_window(100.0, self._iso(3600)))}
        assert earliest_recovery_ts(usage, (), self.NOW) == pytest.approx(
            self.NOW + 3600
        )

    def test_per_account_latest_reset_gates(self):
        """Blocked on 5h AND a scoped window: usable at the LATER reset."""
        usage = {
            "1": _usage(
                five_hour=_window(100.0, self._iso(3600)),
                scoped=[{"name": "Fable", "pct": 100.0, "resets_at": self._iso(7200)}],
            )
        }
        assert earliest_recovery_ts(usage, ("Fable",), self.NOW) == pytest.approx(
            self.NOW + 7200
        )

    def test_scoped_window_ignored_without_model_config(self):
        """A scoped limit doesn't count when models weren't configured."""
        usage = {
            "1": _usage(
                five_hour=_window(100.0, self._iso(3600)),
                scoped=[{"name": "Fable", "pct": 100.0, "resets_at": self._iso(7200)}],
            )
        }
        assert earliest_recovery_ts(usage, (), self.NOW) == pytest.approx(
            self.NOW + 3600
        )

    def test_minimum_across_accounts(self):
        usage = {
            "1": _usage(five_hour=_window(100.0, self._iso(7200))),
            "2": _usage(five_hour=_window(100.0, self._iso(1800))),
        }
        assert earliest_recovery_ts(usage, (), self.NOW) == pytest.approx(
            self.NOW + 1800
        )

    def test_unknown_usage_is_unprovable(self):
        usage = {
            "1": _usage(five_hour=_window(100.0, self._iso(3600))),
            "2": None,
        }
        assert earliest_recovery_ts(usage, (), self.NOW) is None

    def test_exhausted_without_reset_is_unprovable(self):
        usage = {"1": _usage(five_hour=_window(100.0, None))}
        assert earliest_recovery_ts(usage, (), self.NOW) is None

    def test_healthy_account_does_not_gate(self):
        usage = {
            "1": _usage(five_hour=_window(100.0, self._iso(3600))),
            "2": _usage(five_hour=_window(40.0, self._iso(100))),
        }
        assert earliest_recovery_ts(usage, (), self.NOW) == pytest.approx(
            self.NOW + 3600
        )

    def test_nobody_exhausted(self):
        usage = {"1": _usage(five_hour=_window(50.0, self._iso(3600)))}
        assert earliest_recovery_ts(usage, (), self.NOW) is None


# ---------------------------------------------------------------------------
# WrapSession recovery flow (fake switcher)
# ---------------------------------------------------------------------------


class FakeSwitcher:
    """Minimal stand-in for ClaudeAccountSwitcher (no real store access)."""

    def __init__(self, switch_results):
        self._switch_results = list(switch_results)
        self.switch_calls = []

    def switch(self, strategy=None, json_output=False, models=()):
        self.switch_calls.append(
            {"strategy": strategy, "json_output": json_output, "models": models}
        )
        return self._switch_results.pop(0)


def _switched_result(number=2, email="b@example.com"):
    return {
        "switched": True,
        "from": {"number": 1, "email": "a@example.com"},
        "to": {"number": number, "email": email},
        "strategy": "best",
        "reason": "switched",
        "message": "switched",
        "warnings": [],
    }


def _noop_result(reason="no-better-candidate"):
    return {
        "switched": False,
        "from": {"number": 1, "email": "a@example.com"},
        "to": {"number": 1, "email": "a@example.com"},
        "strategy": "best",
        "reason": reason,
        "message": "stayed",
        "warnings": [],
    }


class _FakeSession(WrapSession):
    """Captures injections and status lines; skips the real PTY."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.injected = []
        self.lines = []

    def _inject(self, data):
        self.injected.append(data)

    def _out_default(self, line):
        self.lines.append(line)


def _make_session(switcher, **kwargs):
    lines = []
    session = _FakeSession(
        switcher, [], out=lines.append, **kwargs
    )
    session.status_lines = lines
    return session


class TestRecovery:
    def test_switch_and_continue(self):
        switcher = FakeSwitcher([_switched_result()])
        session = _make_session(switcher)
        session._recover("You've hit your session limit")
        assert switcher.switch_calls == [
            {"strategy": "best", "json_output": True, "models": ()}
        ]
        assert session.injected == [b"continue\r"]
        assert any("switched to Account-2" in line for line in session.status_lines)

    def test_no_auto_continue(self):
        switcher = FakeSwitcher([_switched_result()])
        session = _make_session(switcher, auto_continue=False)
        session._recover("You've hit your session limit")
        assert session.injected == []

    def test_strategy_and_models_forwarded(self):
        switcher = FakeSwitcher([_switched_result()])
        session = _make_session(
            switcher, strategy="next-available", models=("Fable",)
        )
        session._recover("You've hit your Fable limit")
        assert switcher.switch_calls[0]["strategy"] == "next-available"
        assert switcher.switch_calls[0]["models"] == ("Fable",)

    def test_all_exhausted_waits_then_recovers(self):
        """First switch finds nothing; after the wait the retry switches."""
        switcher = FakeSwitcher([_noop_result(), _switched_result(number=3)])
        session = _make_session(switcher)
        session._compute_wait_s = lambda: 0.0  # don't really wait
        session._recover("You've hit your session limit")
        assert len(switcher.switch_calls) == 2
        assert session.injected == [b"continue\r"]

    def test_unknown_reset_uses_fallback_cadence(self):
        switcher = FakeSwitcher([_noop_result(), _switched_result()])
        session = _make_session(switcher)
        waits = []
        session._compute_wait_s = lambda: None
        orig_sleep = session._sleep
        session._sleep = lambda s: (waits.append(s), orig_sleep(0))[0]
        session._recover("You've hit your session limit")
        assert waits == [wrap.NO_RESET_FALLBACK_S]

    def test_debounce(self):
        switcher = FakeSwitcher([_switched_result()])
        session = _make_session(switcher)
        session.feed_output(b"You've hit your session limit")
        # The recovery thread needs a moment; wait for it.
        for _ in range(100):
            if session.injected:
                break
            time.sleep(0.05)
        assert session.injected == [b"continue\r"]
        # A reprint inside the debounce window must not trigger again.
        session.feed_output(b"You've hit your session limit")
        time.sleep(0.3)
        assert len(switcher.switch_calls) == 1


# ---------------------------------------------------------------------------
# End-to-end PTY: fake claude that hits a limit and reads the injected input
# ---------------------------------------------------------------------------


class TestPtyIntegration:
    def test_continue_is_typed_into_claude(self, tmp_path):
        transcript = tmp_path / "transcript.txt"
        fake_claude = tmp_path / "fake_claude.sh"
        fake_claude.write_text(
            "#!/bin/bash\n"
            'echo "You\'ve hit your session limit"\n'
            "read -r line\n"
            f"echo \"GOT:$line\" > {transcript}\n"
        )
        fake_claude.chmod(fake_claude.stat().st_mode | stat.S_IXUSR)

        switcher = FakeSwitcher([_switched_result()])
        session = WrapSession(
            switcher,
            [],
            claude_bin=str(fake_claude),
            out=lambda line: None,
        )
        # The continue injection happens CONTINUE_DELAY_S after the switch.
        exit_code = session.run()
        assert exit_code == 0
        assert transcript.read_text().strip() == "GOT:continue"
        assert switcher.switch_calls

    def test_exit_code_propagates(self, tmp_path):
        fake_claude = tmp_path / "fake_claude.sh"
        fake_claude.write_text("#!/bin/bash\nexit 42\n")
        fake_claude.chmod(fake_claude.stat().st_mode | stat.S_IXUSR)

        session = WrapSession(
            FakeSwitcher([]), [], claude_bin=str(fake_claude), out=lambda line: None
        )
        assert session.run() == 42
