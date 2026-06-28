"""Tests for threshold-driven auto-switching (autoswitch module)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_swap import autoswitch
from claude_swap.switcher import ClaudeAccountSwitcher


def _seed(switcher: ClaudeAccountSwitcher, data: dict) -> None:
    switcher.backup_dir.mkdir(parents=True, exist_ok=True)
    switcher._write_json(switcher.sequence_file, data)


def _usage(pct_5h: float | None = None, pct_7d: float | None = None) -> dict:
    """Build a usage dict shaped like oauth.build_usage_result (only pct used)."""
    usage: dict = {}
    if pct_5h is not None:
        usage["five_hour"] = {"pct": pct_5h}
    if pct_7d is not None:
        usage["seven_day"] = {"pct": pct_7d}
    return usage


# --- Config -------------------------------------------------------------


class TestConfig:
    def test_defaults_when_no_file(self, temp_home: Path):
        cfg = autoswitch.load_config(ClaudeAccountSwitcher())
        assert cfg["globalThreshold"] == 85
        assert cfg["enabled"] is False
        assert cfg["overrides"] == {}
        assert cfg["interval"] == 600

    def test_round_trip(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        cfg = autoswitch.load_config(switcher)
        cfg["globalThreshold"] = 70
        cfg["overrides"] = {"2": 95}
        cfg["interval"] = 300
        autoswitch.save_config(switcher, cfg)

        reread = autoswitch.load_config(switcher)
        assert reread["globalThreshold"] == 70
        assert reread["overrides"] == {"2": 95}
        assert reread["interval"] == 300
        # Only known keys are persisted.
        on_disk = json.loads((switcher.backup_dir / "autoswitch.json").read_text())
        assert set(on_disk) == set(autoswitch.DEFAULTS)

    def test_unknown_keys_ignored(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        (switcher.backup_dir).mkdir(parents=True, exist_ok=True)
        (switcher.backup_dir / "autoswitch.json").write_text(
            json.dumps({"globalThreshold": 50, "bogus": 1})
        )
        cfg = autoswitch.load_config(switcher)
        assert cfg["globalThreshold"] == 50
        assert "bogus" not in cfg

    def test_effective_threshold_override_beats_global(self):
        cfg = {"globalThreshold": 85, "overrides": {"2": 95}}
        assert autoswitch.effective_threshold(cfg, "1") == 85  # falls back to global
        assert autoswitch.effective_threshold(cfg, "2") == 95  # override wins
        assert autoswitch.effective_threshold(cfg, None) == 85


# --- evaluate_and_switch ------------------------------------------------


class TestEvaluate:
    def _switcher(self, monkeypatch, *, active, usage_map):
        """A switcher with mocked identity + usage; switch() records its calls."""
        switcher = ClaudeAccountSwitcher()
        monkeypatch.setattr(autoswitch, "_active_account_num", lambda s: active)
        monkeypatch.setattr(
            ClaudeAccountSwitcher, "_usage_by_account", lambda self: usage_map
        )
        calls: list = []

        def fake_switch(self, strategy=None, json_output=False):
            calls.append(strategy)
            return {
                "switched": True,
                "to": {"number": 2, "email": "b@example.com"},
            }

        monkeypatch.setattr(ClaudeAccountSwitcher, "switch", fake_switch)
        return switcher, calls

    def test_over_threshold_switches(self, temp_home: Path, monkeypatch):
        switcher, calls = self._switcher(
            monkeypatch, active="1", usage_map={"1": _usage(pct_5h=92.0)}
        )
        cfg = {"globalThreshold": 85, "overrides": {}, "strategy": "best"}
        result = autoswitch.evaluate_and_switch(switcher, cfg)
        assert result["switched"] is True
        assert result["target"] == 2
        assert calls == ["best"]

    def test_under_threshold_noop(self, temp_home: Path, monkeypatch):
        switcher, calls = self._switcher(
            monkeypatch, active="1", usage_map={"1": _usage(pct_5h=40.0)}
        )
        cfg = {"globalThreshold": 85, "overrides": {}, "strategy": "best"}
        result = autoswitch.evaluate_and_switch(switcher, cfg)
        assert result["switched"] is False
        assert result["reason"] == "under-threshold"
        assert calls == []  # switch never attempted

    def test_per_account_override_applies(self, temp_home: Path, monkeypatch):
        # 88% is under the global 85?... no: 88 >= 85 would fire globally, but the
        # override raises Account-1's threshold to 95, so 88% must NOT fire.
        switcher, calls = self._switcher(
            monkeypatch, active="1", usage_map={"1": _usage(pct_5h=88.0)}
        )
        cfg = {"globalThreshold": 85, "overrides": {"1": 95}, "strategy": "best"}
        result = autoswitch.evaluate_and_switch(switcher, cfg)
        assert result["switched"] is False
        assert result["reason"] == "under-threshold"
        assert calls == []

    def test_binding_window_is_the_max(self, temp_home: Path, monkeypatch):
        # 7d window at 90% binds even though 5h is low.
        switcher, calls = self._switcher(
            monkeypatch, active="1", usage_map={"1": _usage(pct_5h=10.0, pct_7d=90.0)}
        )
        cfg = {"globalThreshold": 85, "overrides": {}, "strategy": "best"}
        result = autoswitch.evaluate_and_switch(switcher, cfg)
        assert result["switched"] is True

    def test_usage_unavailable_noop(self, temp_home: Path, monkeypatch):
        switcher, calls = self._switcher(
            monkeypatch, active="1", usage_map={"1": None}
        )
        cfg = {"globalThreshold": 85, "overrides": {}, "strategy": "best"}
        result = autoswitch.evaluate_and_switch(switcher, cfg)
        assert result["switched"] is False
        assert result["reason"] == "usage-unavailable"
        assert calls == []

    def test_no_active_account_noop(self, temp_home: Path, monkeypatch):
        switcher, calls = self._switcher(monkeypatch, active=None, usage_map={})
        cfg = {"globalThreshold": 85, "overrides": {}, "strategy": "best"}
        result = autoswitch.evaluate_and_switch(switcher, cfg)
        assert result["switched"] is False
        assert result["reason"] == "no-active-account"

    def test_single_account_over_threshold_stays(self, temp_home: Path, monkeypatch):
        # Only one managed account: over threshold but nowhere to go.
        switcher = ClaudeAccountSwitcher()
        monkeypatch.setattr(autoswitch, "_active_account_num", lambda s: "1")
        monkeypatch.setattr(
            ClaudeAccountSwitcher, "_usage_by_account",
            lambda self: {"1": _usage(pct_5h=99.0)},
        )
        # switch() reports the only-one-account no-op (matches real switcher).
        monkeypatch.setattr(
            ClaudeAccountSwitcher, "switch",
            lambda self, strategy=None, json_output=False: {
                "switched": False, "reason": "only-one-account", "to": None,
            },
        )
        cfg = {"globalThreshold": 85, "overrides": {}, "strategy": "best"}
        result = autoswitch.evaluate_and_switch(switcher, cfg)
        assert result["switched"] is False
        assert result["reason"] == "only-one-account"
        assert "only managed account" in result["message"]


# --- Watcher process management -----------------------------------------


class TestWatcher:
    def test_start_refuses_when_already_running(self, temp_home: Path, monkeypatch):
        switcher = ClaudeAccountSwitcher()
        monkeypatch.setattr(autoswitch, "_read_pid", lambda s: 4321)
        with pytest.raises(Exception) as exc:
            autoswitch.start_watcher(switcher)
        assert "already running" in str(exc.value)

    def test_start_spawns_and_marks_enabled(self, temp_home: Path, monkeypatch):
        switcher = ClaudeAccountSwitcher()
        # No daemon writes a PID file here, so start_watcher falls back to
        # proc.pid; neutralize the poll sleep so the fallback is reached fast.
        monkeypatch.setattr(autoswitch, "_read_pid", lambda s: None)
        monkeypatch.setattr(autoswitch.time, "sleep", lambda *a: None)

        spawned = {}

        class FakeProc:
            pid = 9999

        def fake_popen(cmd, **kwargs):
            spawned["cmd"] = cmd
            spawned["kwargs"] = kwargs
            return FakeProc()

        monkeypatch.setattr(autoswitch.subprocess, "Popen", fake_popen)
        pid = autoswitch.start_watcher(switcher)
        assert pid == 9999
        assert spawned["cmd"][-2:] == ["autoswitch", "__run"]
        assert autoswitch.load_config(switcher)["enabled"] is True

    def test_stop_clears_pid_and_disables(self, temp_home: Path, monkeypatch):
        switcher = ClaudeAccountSwitcher()
        switcher.backup_dir.mkdir(parents=True, exist_ok=True)
        pid_file = switcher.backup_dir / "autoswitch.pid"
        pid_file.write_text("12345")
        cfg = autoswitch.load_config(switcher)
        cfg["enabled"] = True
        autoswitch.save_config(switcher, cfg)

        monkeypatch.setattr(autoswitch, "_read_pid", lambda s: 12345)
        killed = {}
        monkeypatch.setattr(autoswitch.os, "kill", lambda p, s: killed.setdefault("pid", p))

        assert autoswitch.stop_watcher(switcher) is True
        assert killed["pid"] == 12345
        assert not pid_file.exists()
        assert autoswitch.load_config(switcher)["enabled"] is False

    def test_stop_when_not_running(self, temp_home: Path, monkeypatch):
        switcher = ClaudeAccountSwitcher()
        monkeypatch.setattr(autoswitch, "_read_pid", lambda s: None)
        assert autoswitch.stop_watcher(switcher) is False

    def test_read_pid_cleans_stale_file(self, temp_home: Path, monkeypatch):
        switcher = ClaudeAccountSwitcher()
        switcher.backup_dir.mkdir(parents=True, exist_ok=True)
        pid_file = switcher.backup_dir / "autoswitch.pid"
        pid_file.write_text("424242")
        monkeypatch.setattr(autoswitch, "is_pid_alive", lambda pid: False)
        assert autoswitch._read_pid(switcher) is None
        assert not pid_file.exists()  # stale file removed


# --- Wizard -------------------------------------------------------------


class TestWizard:
    def test_set_global_then_done(self, temp_home: Path, monkeypatch):
        switcher = ClaudeAccountSwitcher()
        monkeypatch.setattr(autoswitch, "is_running", lambda s: False)
        # Choose 1 (set global) -> 70 -> 5 (done)
        answers = iter(["1", "70", "5"])
        monkeypatch.setattr("builtins.input", lambda *a: next(answers))
        autoswitch.run_wizard(switcher)
        assert autoswitch.load_config(switcher)["globalThreshold"] == 70

    def test_invalid_threshold_rejected(self, temp_home: Path, monkeypatch):
        switcher = ClaudeAccountSwitcher()
        monkeypatch.setattr(autoswitch, "is_running", lambda s: False)
        # 1 -> 150 (out of range, ignored) -> 5 (done): global stays default 85
        answers = iter(["1", "150", "5"])
        monkeypatch.setattr("builtins.input", lambda *a: next(answers))
        autoswitch.run_wizard(switcher)
        assert autoswitch.load_config(switcher)["globalThreshold"] == 85

    def test_set_and_clear_override(self, temp_home: Path, monkeypatch):
        switcher = ClaudeAccountSwitcher()
        _seed(switcher, {
            "activeAccountNumber": 1,
            "sequence": [1, 2],
            "accounts": {
                "1": {"email": "a@example.com", "uuid": "u1"},
                "2": {"email": "b@example.com", "uuid": "u2"},
            },
        })
        monkeypatch.setattr(autoswitch, "is_running", lambda s: False)
        # 2 (override submenu) -> account "2" -> 90  ; then 5 done
        answers = iter(["2", "2", "90", "5"])
        monkeypatch.setattr("builtins.input", lambda *a: next(answers))
        autoswitch.run_wizard(switcher)
        assert autoswitch.load_config(switcher)["overrides"] == {"2": 90}

        # Now clear it: 2 -> account "2" -> "x" ; then 5 done
        answers = iter(["2", "2", "x", "5"])
        monkeypatch.setattr("builtins.input", lambda *a: next(answers))
        autoswitch.run_wizard(switcher)
        assert autoswitch.load_config(switcher)["overrides"] == {}
