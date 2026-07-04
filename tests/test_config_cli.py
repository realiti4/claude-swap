"""Tests for the `cswap config` subcommand (get/set/unset/list/path)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import cli


def _run(argv: list[str], capsys) -> tuple[int, str, str]:
    """Run `cswap config <argv>`; returns (exit_code, stdout, stderr).

    Success returns normally from main() (no sys.exit), errors raise
    SystemExit — normalize both to an exit code.
    """
    with patch("os.geteuid", return_value=1000, create=True), \
         patch.object(sys, "argv", ["claude-swap", "config", *argv]):
        code = 0
        try:
            cli.main()
        except SystemExit as e:
            code = e.code or 0
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _settings_file(capsys) -> Path:
    code, out, _ = _run(["path"], capsys)
    assert code == 0
    return Path(out.strip())


class TestConfigList:
    def test_lists_all_keys_as_defaults(self, temp_home, capsys):
        code, out, _ = _run([], capsys)
        assert code == 0
        for key in (
            "autoswitch.threshold",
            "autoswitch.intervalSeconds",
            "autoswitch.cooldownSeconds",
            "autoswitch.hysteresisPct",
            "autoswitch.strategy",
            "autoswitch.includeApiKeyAccounts",
            "autoswitch.unhealthyTicks",
        ):
            assert key in out
        assert out.count("(default)") == 7

    def test_set_key_not_marked_default(self, temp_home, capsys):
        _run(["set", "autoswitch.cooldownSeconds", "600"], capsys)
        code, out, _ = _run([], capsys)
        assert code == 0
        cooldown_line = next(
            ln for ln in out.splitlines() if "cooldownSeconds" in ln
        )
        assert "600" in cooldown_line
        assert "(default)" not in cooldown_line

    def test_set_equal_to_default_still_counts_as_set(self, temp_home, capsys):
        _run(["set", "autoswitch.threshold", "90"], capsys)
        _, out, _ = _run([], capsys)
        threshold_line = next(
            ln for ln in out.splitlines() if "threshold" in ln
        )
        assert "(default)" not in threshold_line

    def test_json_list(self, temp_home, capsys):
        code, out, _ = _run(["--json"], capsys)
        assert code == 0
        payload = json.loads(out)
        assert payload["schemaVersion"] == 1
        assert payload["path"].endswith("settings.json")
        by_key = {entry["key"]: entry for entry in payload["settings"]}
        assert len(by_key) == 7
        assert by_key["autoswitch.threshold"]["value"] == 90.0
        assert by_key["autoswitch.threshold"]["isSet"] is False
        assert by_key["autoswitch.includeApiKeyAccounts"]["value"] is False


class TestConfigSetGet:
    def test_set_then_get(self, temp_home, capsys):
        code, out, _ = _run(["set", "autoswitch.threshold", "80"], capsys)
        assert code == 0
        assert "autoswitch.threshold = 80" in out
        code, out, _ = _run(["get", "autoswitch.threshold"], capsys)
        assert code == 0
        assert out.strip() == "80"

    def test_set_writes_only_that_key(self, temp_home, capsys):
        """The trap guard: no other defaults get materialized into the file."""
        _run(["set", "autoswitch.threshold", "80"], capsys)
        raw = json.loads(_settings_file(capsys).read_text())
        assert set(raw) == {"schemaVersion", "autoswitch"}
        assert set(raw["autoswitch"]) == {"threshold"}
        assert raw["autoswitch"]["threshold"] == 80.0

    def test_set_bool_words(self, temp_home, capsys):
        code, out, _ = _run(
            ["set", "autoswitch.includeApiKeyAccounts", "no"], capsys
        )
        assert code == 0
        assert "= false" in out
        raw = json.loads(_settings_file(capsys).read_text())
        assert raw["autoswitch"]["includeApiKeyAccounts"] is False

    def test_set_preserves_unknown_keys(self, temp_home, capsys):
        path = _settings_file(capsys)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "schemaVersion": 1,
            "futureSection": {"x": 1},
            "autoswitch": {"threshold": 80, "futureKnob": True},
        }))
        code, _, _ = _run(["set", "autoswitch.threshold", "70"], capsys)
        assert code == 0
        raw = json.loads(path.read_text())
        assert raw["futureSection"] == {"x": 1}
        assert raw["autoswitch"]["futureKnob"] is True
        assert raw["autoswitch"]["threshold"] == 70.0

    def test_get_json_trailing_and_leading_flag(self, temp_home, capsys):
        _run(["set", "autoswitch.threshold", "80"], capsys)
        for argv in (
            ["get", "autoswitch.threshold", "--json"],
            ["--json", "get", "autoswitch.threshold"],
        ):
            code, out, _ = _run(argv, capsys)
            assert code == 0
            payload = json.loads(out)
            assert payload == {
                "schemaVersion": 1,
                "key": "autoswitch.threshold",
                "value": 80.0,
                "isSet": True,
            }


class TestConfigValidation:
    def test_out_of_range_exits_1(self, temp_home, capsys):
        code, _, err = _run(["set", "autoswitch.threshold", "30"], capsys)
        assert code == 1
        assert "between 50 and 99.9" in err

    def test_unknown_key_exits_1_and_lists_valid_keys(self, temp_home, capsys):
        code, _, err = _run(["set", "autoswitch.bogus", "1"], capsys)
        assert code == 1
        assert "unknown setting" in err
        assert "autoswitch.threshold" in err

    def test_bad_bool_exits_1(self, temp_home, capsys):
        code, _, err = _run(
            ["set", "autoswitch.includeApiKeyAccounts", "falsy"], capsys
        )
        assert code == 1
        assert "true or false" in err

    def test_bad_number_exits_1(self, temp_home, capsys):
        code, _, err = _run(["set", "autoswitch.threshold", "high"], capsys)
        assert code == 1
        assert "expects a number" in err

    def test_int_key_rejects_float(self, temp_home, capsys):
        code, _, err = _run(["set", "autoswitch.unhealthyTicks", "3.5"], capsys)
        assert code == 1
        assert "expects an integer" in err

    def test_bad_strategy_exits_1(self, temp_home, capsys):
        code, _, err = _run(["set", "autoswitch.strategy", "chaos"], capsys)
        assert code == 1
        assert "must be one of: best" in err

    def test_unknown_key_json_error_envelope(self, temp_home, capsys):
        code, out, _ = _run(["--json", "get", "autoswitch.bogus"], capsys)
        assert code == 1
        payload = json.loads(out)
        assert payload["schemaVersion"] == 1
        assert "unknown setting" in payload["error"]["message"]

    def test_corrupt_file_set_exits_1_and_leaves_file_untouched(
        self, temp_home, capsys
    ):
        path = _settings_file(capsys)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        code, _, err = _run(["set", "autoswitch.threshold", "80"], capsys)
        assert code == 1
        assert "not valid JSON" in err
        assert path.read_text() == "{not json"

    def test_missing_value_usage_error_exits_2(self, temp_home, capsys):
        code, _, _ = _run(["set", "autoswitch.threshold"], capsys)
        assert code == 2

    def test_unknown_action_exits_2(self, temp_home, capsys):
        code, _, _ = _run(["frobnicate"], capsys)
        assert code == 2

    def test_json_with_set_rejected(self, temp_home, capsys):
        code, _, _ = _run(
            ["--json", "set", "autoswitch.threshold", "80"], capsys
        )
        assert code == 2


class TestConfigUnset:
    def test_unset_restores_default(self, temp_home, capsys):
        _run(["set", "autoswitch.threshold", "80"], capsys)
        code, out, _ = _run(["unset", "autoswitch.threshold"], capsys)
        assert code == 0
        assert "default: 90" in out
        code, out, _ = _run(["get", "autoswitch.threshold"], capsys)
        assert out.strip() == "90"
        # The emptied autoswitch section is removed entirely.
        raw = json.loads(_settings_file(capsys).read_text())
        assert "autoswitch" not in raw

    def test_unset_when_not_set_is_a_noop(self, temp_home, capsys):
        code, _, err = _run(["unset", "autoswitch.threshold"], capsys)
        assert code == 0
        assert "not set" in err


class TestConfigMisc:
    def test_path_prints_settings_location(self, temp_home, capsys):
        code, out, _ = _run(["path"], capsys)
        assert code == 0
        assert out.strip().endswith("settings.json")

    def test_config_help(self, temp_home, capsys):
        code, out, _ = _run(["--help"], capsys)
        assert code == 0
        assert "autoswitch.threshold" in out
        assert "unset" in out

    def test_main_help_mentions_config(self, temp_home, capsys):
        with patch.object(sys, "argv", ["claude-swap", "--help"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == 0
        assert "config" in capsys.readouterr().out

    def test_auto_picks_up_configured_threshold(self, temp_home, capsys):
        """End-to-end: a value set via config drives `cswap auto`."""
        _run(["set", "autoswitch.threshold", "77"], capsys)

        captured = {}

        class FakeEngine:
            def __init__(self, switcher, settings, on_event, *, dry_run=False,
                         state_path=None, clock=None):
                captured["settings"] = settings

            def tick(self):
                from claude_swap.autoswitch import TickOutcome

                return TickOutcome.NO_ACTION

        with patch("claude_swap.autoswitch.AutoSwitchEngine", FakeEngine), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch.object(sys, "argv", ["claude-swap", "auto", "--once"]):
            with pytest.raises(SystemExit):
                cli.main()
        assert captured["settings"].threshold == 77.0
