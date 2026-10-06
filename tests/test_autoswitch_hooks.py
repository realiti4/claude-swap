"""Tests for the auto-switch lifecycle hooks (preSwapCmd / postSwapCmd /
imminentCmd). The hooks are real subprocesses here — ``python -c`` one-liners
that record their stdin or exit with a chosen code — so the tests pin the
actual contract an external policy script sees, not a mocked shape."""

from __future__ import annotations

import json
import logging
import shlex
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import autoswitch
from claude_swap.autoswitch import (
    AutoSwitchEngine,
    NoSwitchEvent,
    SwitchEvent,
    TickOutcome,
    VetoEvent,
)
from claude_swap.settings import AutoSwitchSettings, load_settings, settings_path
from tests.test_autoswitch import EngineHarness, _usage

# ---------------------------------------------------------------------------
# Hook command builders. Every command is one interpreter invocation, quoted
# with shlex so paths with spaces survive the engine's shlex.split.
# ---------------------------------------------------------------------------


def _py(code: str) -> str:
    return shlex.join([sys.executable, "-c", code])


def _exit(code: int) -> str:
    return _py(f"import sys; sys.exit({code})")


def _recorder(path: Path, exit_code: int = 0) -> str:
    """A hook that appends its stdin (one JSON line) to ``path`` and exits
    with ``exit_code``."""
    return _py(
        "import sys; "
        f"open({str(path)!r}, 'a').write(sys.stdin.read().strip() + '\\n'); "
        f"sys.exit({exit_code})"
    )


def _lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln]


def _build(temp_home: Path, **settings) -> EngineHarness:
    h = EngineHarness(temp_home, **settings)
    h.seed(1, "a@example.com")
    h.seed(2, "b@example.com")
    h.seed(3, "c@example.com")
    h.make_live("a@example.com", 1)
    return h


# A proactive switch: #1 at 95% (above the 90% default), #2 the best target.
SWITCHING = {"1": _usage(95), "2": _usage(10), "3": _usage(50)}


class TestNoHooksConfigured:
    def test_defaults_are_off(self):
        s = AutoSwitchSettings()
        assert s.pre_swap_cmd is None
        assert s.post_swap_cmd is None
        assert s.imminent_cmd is None
        assert s.imminent_lead_pct == 10.0

    def test_no_subprocess_and_same_events(self, temp_home):
        h = _build(temp_home)
        with patch.object(autoswitch.subprocess, "run") as run:
            outcome = h.tick_with_usage(SWITCHING)
        run.assert_not_called()
        assert outcome is TickOutcome.SWITCHED
        assert h.kinds() == ["poll", "switch"]
        assert h.active_number() == 2


class TestPreSwap:
    def test_exit_3_vetoes_without_cooldown(self, temp_home):
        h = _build(temp_home, pre_swap_cmd=_exit(3))
        outcome = h.tick_with_usage(SWITCHING)
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        veto = next(e for e in h.events if isinstance(e, VetoEvent))
        assert veto.kind == "vetoed"
        assert veto.trigger == "proactive"
        payload = veto.to_json()
        assert payload["event"] == "vetoed"
        assert payload["from"] == {"number": 1, "email": "a@example.com"}
        assert payload["to"] == {"number": 2, "email": "b@example.com"}
        assert payload["hook"] == "preSwapCmd"
        assert "Account-1" in veto.human() and "Account-2" in veto.human()
        # No switch happened, so no cooldown starts: nothing is recorded and
        # the very next tick re-evaluates from scratch.
        assert "lastSwitchAt" not in h.state()
        assert not any(isinstance(e, SwitchEvent) for e in h.events)

    def test_veto_lifts_when_the_hook_allows_next_tick(self, temp_home):
        h = _build(temp_home, pre_swap_cmd=_exit(3))
        assert h.tick_with_usage(SWITCHING) is TickOutcome.NO_ACTION
        h.engine.settings = AutoSwitchSettings(pre_swap_cmd=_exit(0))
        assert h.tick_with_usage(SWITCHING) is TickOutcome.SWITCHED
        assert h.active_number() == 2

    def test_exit_0_proceeds_and_receives_the_payload(self, temp_home):
        log = temp_home / "pre swap.jsonl"
        h = _build(temp_home, pre_swap_cmd=_recorder(log))
        reset = "2026-07-05T12:00:00Z"
        outcome = h.tick_with_usage({
            "1": _usage(95, reset), "2": _usage(10), "3": _usage(50),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2
        (payload,) = _lines(log)
        assert payload["schemaVersion"] == 1
        assert payload["event"] == "pre_swap"
        assert payload["ts"].endswith("Z")
        assert payload["trigger"] == "proactive"
        assert payload["from"] == {"number": 1, "email": "a@example.com"}
        assert payload["to"] == {"number": 2, "email": "b@example.com"}
        assert payload["threshold"] == 90.0
        assert payload["fromUsage"]["bindingPct"] == 95.0
        assert payload["fromUsage"]["windows"]["5h"] == {
            "pct": 95.0, "resetsAt": reset,
        }
        assert payload["fromUsage"]["windows"]["7d"] == {
            "pct": 0.0, "resetsAt": None,
        }
        assert payload["toUsage"]["bindingPct"] == 10.0

    def test_other_nonzero_exit_proceeds(self, temp_home):
        h = _build(temp_home, pre_swap_cmd=_exit(1))
        assert h.tick_with_usage(SWITCHING) is TickOutcome.SWITCHED
        assert not any(isinstance(e, VetoEvent) for e in h.events)

    def test_timeout_proceeds_with_a_warning(self, temp_home, caplog):
        h = _build(temp_home, pre_swap_cmd=_py("import time; time.sleep(30)"))
        with (
            patch.object(autoswitch, "HOOK_TIMEOUT_S", 0.3),
            caplog.at_level(logging.WARNING, logger="claude-swap"),
        ):
            outcome = h.tick_with_usage(SWITCHING)
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2
        assert any("preSwapCmd" in r.message and "timed out" in r.message
                   for r in caplog.records)

    def test_missing_command_proceeds_with_a_warning(self, temp_home, caplog):
        h = _build(temp_home, pre_swap_cmd="/nonexistent/cswap-hook --veto")
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            outcome = h.tick_with_usage(SWITCHING)
        assert outcome is TickOutcome.SWITCHED
        assert any("preSwapCmd" in r.message for r in caplog.records)

    def test_unparseable_command_proceeds(self, temp_home, caplog):
        h = _build(temp_home, pre_swap_cmd="echo 'unterminated")
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            assert h.tick_with_usage(SWITCHING) is TickOutcome.SWITCHED

    def test_runs_outside_the_state_lock(self, temp_home):
        """A slow policy script must never hold up other engines (loop +
        cron --once, or the menu bar) on the shared state lock."""
        h = _build(temp_home, pre_swap_cmd=_exit(0))
        seen: list[bool] = []
        real = autoswitch._run_hook

        def probing(*args, **kwargs):
            lock = h.engine._state_lock()
            seen.append(lock.acquire(timeout=0.5))
            if seen[-1]:
                lock.release()
            return real(*args, **kwargs)

        with patch.object(autoswitch, "_run_hook", probing):
            assert h.tick_with_usage(SWITCHING) is TickOutcome.SWITCHED
        assert seen == [True]

    def test_veto_applies_to_at_limit_too(self, temp_home):
        # The policy owns the decision; an at-limit account is still its
        # call (it may know a cheaper moment is seconds away).
        h = _build(temp_home, pre_swap_cmd=_exit(3))
        outcome = h.tick_with_usage({"1": _usage(100), "2": _usage(10), "3": _usage(50)})
        assert outcome is TickOutcome.NO_ACTION
        assert next(e for e in h.events if isinstance(e, VetoEvent)).trigger == "at-limit"


class TestPostSwap:
    def test_runs_after_a_successful_switch(self, temp_home):
        log = temp_home / "post.jsonl"
        h = _build(temp_home, post_swap_cmd=_recorder(log))
        assert h.tick_with_usage(SWITCHING) is TickOutcome.SWITCHED
        (payload,) = _lines(log)
        assert payload["event"] == "post_swap"
        assert payload["trigger"] == "proactive"
        assert payload["from"] == {"number": 1, "email": "a@example.com"}
        assert payload["to"] == {"number": 2, "email": "b@example.com"}
        assert payload["threshold"] == 90.0
        assert payload["warnings"] == []
        assert payload["fromUsage"]["bindingPct"] == 95.0
        # The engine's own event went out before the hook ran, so --json
        # readers see the switch even if the hook hangs.
        assert h.kinds() == ["poll", "switch"]

    def test_exit_code_is_ignored(self, temp_home):
        h = _build(temp_home, post_swap_cmd=_exit(3))
        assert h.tick_with_usage(SWITCHING) is TickOutcome.SWITCHED
        assert not any(isinstance(e, VetoEvent) for e in h.events)
        assert h.active_number() == 2

    def test_not_run_when_switch_did_not_happen(self, temp_home):
        log = temp_home / "post.jsonl"
        h = _build(temp_home, post_swap_cmd=_recorder(log))
        with patch.object(
            h.switcher, "switch_to",
            return_value={"switched": False, "reason": "already-active"},
        ):
            assert h.tick_with_usage(SWITCHING) is TickOutcome.NO_ACTION
        assert _lines(log) == []

    def test_not_run_after_a_veto(self, temp_home):
        log = temp_home / "post.jsonl"
        h = _build(temp_home, pre_swap_cmd=_exit(3), post_swap_cmd=_recorder(log))
        assert h.tick_with_usage(SWITCHING) is TickOutcome.NO_ACTION
        assert _lines(log) == []

    def test_not_run_when_the_switch_raises(self, temp_home):
        log = temp_home / "post.jsonl"
        h = _build(temp_home, post_swap_cmd=_recorder(log))
        with patch.object(h.switcher, "switch_to", side_effect=OSError("disk")):
            assert h.tick_with_usage(SWITCHING) is TickOutcome.ERROR
        assert _lines(log) == []


class TestSwapImminent:
    def test_fires_once_per_crossing_and_rearms(self, temp_home):
        log = temp_home / "imminent.jsonl"
        h = _build(temp_home, imminent_cmd=_recorder(log))  # lead 10 → fires at 80
        quiet = {"2": _usage(10), "3": _usage(50)}

        assert h.tick_with_usage({"1": _usage(79), **quiet}) is TickOutcome.NO_ACTION
        assert _lines(log) == []

        assert h.tick_with_usage({"1": _usage(80), **quiet}) is TickOutcome.NO_ACTION
        (payload,) = _lines(log)
        assert payload["schemaVersion"] == 1
        assert payload["event"] == "swap_imminent"
        assert payload["active"] == {"number": 1, "email": "a@example.com"}
        assert payload["activeUsage"]["bindingPct"] == 80.0
        assert payload["activeUsage"]["windows"]["5h"]["pct"] == 80.0
        assert payload["threshold"] == 90.0
        assert payload["leadPct"] == 10.0

        # Still above the line: no repeat.
        h.tick_with_usage({"1": _usage(84), **quiet})
        h.tick_with_usage({"1": _usage(88), **quiet})
        assert len(_lines(log)) == 1

        # Drops back below: re-arms silently...
        h.tick_with_usage({"1": _usage(60), **quiet})
        assert len(_lines(log)) == 1
        # ...and crosses again: fires once more.
        h.tick_with_usage({"1": _usage(81), **quiet})
        assert len(_lines(log)) == 2
        assert h.active_number() == 1  # never switched: all below threshold

    def test_is_per_account(self, temp_home):
        log = temp_home / "imminent.jsonl"
        h = _build(temp_home, imminent_cmd=_recorder(log))
        # #1 crosses the line, then the user lands on #2 which crosses too.
        h.tick_with_usage({"1": _usage(85), "2": _usage(10), "3": _usage(50)})
        h.switcher.switch_to("2", json_output=True)
        h.tick_with_usage({"1": _usage(85), "2": _usage(85), "3": _usage(50)})
        actives = [p["active"]["number"] for p in _lines(log)]
        assert actives == [1, 2]

    def test_lead_pct_is_configurable(self, temp_home):
        log = temp_home / "imminent.jsonl"
        h = _build(temp_home, imminent_cmd=_recorder(log), imminent_lead_pct=25.0)
        quiet = {"2": _usage(10), "3": _usage(50)}
        h.tick_with_usage({"1": _usage(64), **quiet})
        assert _lines(log) == []
        h.tick_with_usage({"1": _usage(65), **quiet})
        assert len(_lines(log)) == 1

    def test_exit_code_is_ignored(self, temp_home):
        h = _build(temp_home, imminent_cmd=_exit(3))
        quiet = {"2": _usage(10), "3": _usage(50)}
        assert h.tick_with_usage({"1": _usage(85), **quiet}) is TickOutcome.NO_ACTION
        assert not any(isinstance(e, VetoEvent) for e in h.events)
        assert h.kinds() == ["poll", "no-switch"]

    def test_not_tracked_when_unconfigured(self, temp_home):
        h = _build(temp_home)
        quiet = {"2": _usage(10), "3": _usage(50)}
        with patch.object(autoswitch.subprocess, "run") as run:
            h.tick_with_usage({"1": _usage(85), **quiet})
        run.assert_not_called()


class TestDryRun:
    def test_runs_no_hooks(self, temp_home):
        pre = temp_home / "pre.jsonl"
        post = temp_home / "post.jsonl"
        imminent = temp_home / "imminent.jsonl"
        h = _build(
            temp_home,
            pre_swap_cmd=_recorder(pre, exit_code=3),
            post_swap_cmd=_recorder(post),
            imminent_cmd=_recorder(imminent),
        )
        h.engine = h._make_engine(dry_run=True)
        with patch.object(autoswitch.subprocess, "run", wraps=autoswitch.subprocess.run) as run:
            outcome = h.tick_with_usage(SWITCHING)
        # Dry-run stops at the decision: the would-switch is reported, the
        # veto hook never got a say, and nothing was spawned.
        assert outcome is TickOutcome.SWITCHED
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert switch.dry_run is True
        assert not any(isinstance(e, VetoEvent) for e in h.events)
        run.assert_not_called()
        assert _lines(pre) == _lines(post) == _lines(imminent) == []
        assert h.active_number() == 1


class TestPayloadCarriesNoSecrets:
    @pytest.mark.parametrize("field", ["pre_swap_cmd", "post_swap_cmd", "imminent_cmd"])
    def test_no_credential_material(self, temp_home, field):
        log = temp_home / "hook.jsonl"
        h = _build(temp_home, **{field: _recorder(log)})
        # Seeded credentials are "sk-<n>" / "rt-<n>" (see EngineHarness.seed).
        usage = SWITCHING if field != "imminent_cmd" else {
            "1": _usage(85), "2": _usage(10), "3": _usage(50),
        }
        h.tick_with_usage(usage)
        (payload,) = _lines(log)
        raw = json.dumps(payload).lower()
        for needle in ("sk-", "rt-", "token", "credential", "secret", "key"):
            assert needle not in raw, needle
        assert set(payload) <= {
            "schemaVersion", "event", "ts", "trigger", "from", "to",
            "fromUsage", "toUsage", "threshold", "warnings",
            "active", "activeUsage", "leadPct",
        }


class TestSettingsPlumbing:
    def test_loads_from_settings_json(self, tmp_path: Path):
        settings_path(tmp_path).write_text(json.dumps({
            "schemaVersion": 1,
            "autoswitch": {
                "preSwapCmd": "cswap-plus-plus pre",
                "postSwapCmd": "cswap-plus-plus post",
                "imminentCmd": "cswap-plus-plus imminent",
                "imminentLeadPct": 15,
            },
        }))
        s = load_settings(tmp_path)
        assert s.pre_swap_cmd == "cswap-plus-plus pre"
        assert s.post_swap_cmd == "cswap-plus-plus post"
        assert s.imminent_cmd == "cswap-plus-plus imminent"
        assert s.imminent_lead_pct == 15.0

    def test_garbage_values_fall_back(self, tmp_path: Path):
        settings_path(tmp_path).write_text(json.dumps({
            "schemaVersion": 1,
            "autoswitch": {"preSwapCmd": 42, "imminentLeadPct": "ten"},
        }))
        s = load_settings(tmp_path)
        assert s.pre_swap_cmd is None
        assert s.imminent_lead_pct == 10.0

    def test_engine_constructs_with_hooks(self, temp_home):
        h = _build(temp_home, pre_swap_cmd=_exit(0))
        assert isinstance(h.engine, AutoSwitchEngine)
        assert not any(isinstance(e, NoSwitchEvent) for e in h.events)
