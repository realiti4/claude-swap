"""Tests for settings.json load/save/merge (settings.py)."""

from __future__ import annotations

import argparse
import json
import stat
import sys
from pathlib import Path

import pytest

from claude_swap.exceptions import ConfigError
from claude_swap.settings import (
    SETTING_SPECS,
    AutoSwitchSettings,
    effective_settings,
    load_settings,
    merged_with_cli,
    save_settings,
    set_setting,
    settings_path,
    unset_setting,
)


def _args(**kwargs) -> argparse.Namespace:
    defaults = {
        "threshold": None,
        "interval": None,
        "cooldown": None,
        "include_api_key_accounts": None,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


class TestLoadSettings:
    def test_missing_file_gives_defaults(self, tmp_path: Path):
        assert load_settings(tmp_path) == AutoSwitchSettings()

    def test_corrupt_file_gives_defaults(self, tmp_path: Path):
        settings_path(tmp_path).write_text("{not json")
        assert load_settings(tmp_path) == AutoSwitchSettings()

    def test_non_object_gives_defaults(self, tmp_path: Path):
        settings_path(tmp_path).write_text("[1, 2]")
        assert load_settings(tmp_path) == AutoSwitchSettings()

    def test_partial_section_fills_defaults(self, tmp_path: Path):
        settings_path(tmp_path).write_text(
            json.dumps({"schemaVersion": 1, "autoswitch": {"threshold": 80}})
        )
        loaded = load_settings(tmp_path)
        assert loaded.threshold == 80.0
        assert loaded.interval_seconds == AutoSwitchSettings().interval_seconds

    def test_values_are_clamped(self, tmp_path: Path):
        settings_path(tmp_path).write_text(json.dumps({
            "autoswitch": {
                "threshold": 200,
                "intervalSeconds": 1,
                "hysteresisPct": -5,
                "unhealthyTicks": 0,
            }
        }))
        loaded = load_settings(tmp_path)
        assert loaded.threshold == 99.9
        assert loaded.interval_seconds == 15.0  # usage-cache TTL floor
        assert loaded.hysteresis_pct == 0.0
        assert loaded.unhealthy_ticks == 1

    def test_bad_types_fall_back_to_defaults(self, tmp_path: Path):
        settings_path(tmp_path).write_text(json.dumps({
            "autoswitch": {"threshold": "high", "includeApiKeyAccounts": 1}
        }))
        loaded = load_settings(tmp_path)
        assert loaded.threshold == AutoSwitchSettings().threshold
        assert loaded.include_api_key_accounts is True

    def test_unsupported_strategy_falls_back_to_best(self, tmp_path: Path):
        settings_path(tmp_path).write_text(
            json.dumps({"autoswitch": {"strategy": "chaos"}})
        )
        assert load_settings(tmp_path).strategy == "best"


class TestSaveSettings:
    def test_roundtrip(self, tmp_path: Path):
        custom = AutoSwitchSettings(threshold=85.0, cooldown_seconds=60.0)
        save_settings(tmp_path, custom)
        assert load_settings(tmp_path) == custom

    def test_unknown_keys_survive(self, tmp_path: Path):
        settings_path(tmp_path).write_text(json.dumps({
            "schemaVersion": 1,
            "futureSection": {"x": 1},
            "autoswitch": {"threshold": 80, "futureKnob": True},
        }))
        save_settings(tmp_path, AutoSwitchSettings(threshold=70.0))
        raw = json.loads(settings_path(tmp_path).read_text())
        assert raw["futureSection"] == {"x": 1}
        assert raw["autoswitch"]["futureKnob"] is True
        assert raw["autoswitch"]["threshold"] == 70.0

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    def test_file_mode_is_0600(self, tmp_path: Path):
        save_settings(tmp_path, AutoSwitchSettings())
        mode = stat.S_IMODE(settings_path(tmp_path).stat().st_mode)
        assert mode == 0o600


class TestSettingSpecs:
    def test_registry_covers_every_dataclass_field(self):
        spec_fields = {spec.field for spec in SETTING_SPECS.values()}
        dataclass_fields = {
            f.name for f in AutoSwitchSettings.__dataclass_fields__.values()
        }
        assert spec_fields == dataclass_fields

    def test_defaults_match_dataclass(self):
        defaults = AutoSwitchSettings()
        for spec in SETTING_SPECS.values():
            assert spec.default == getattr(defaults, spec.field)


class TestSetUnsetSetting:
    def test_set_writes_minimal_file(self, tmp_path: Path):
        value = set_setting(tmp_path, "autoswitch.threshold", "80")
        assert value == 80.0
        raw = json.loads(settings_path(tmp_path).read_text())
        assert raw == {"schemaVersion": 1, "autoswitch": {"threshold": 80.0}}

    def test_set_int_kind_coerces_and_rejects_floats(self, tmp_path: Path):
        assert set_setting(tmp_path, "autoswitch.unhealthyTicks", "5") == 5
        with pytest.raises(ConfigError, match="integer"):
            set_setting(tmp_path, "autoswitch.unhealthyTicks", "3.5")

    def test_set_rejects_out_of_range_without_writing(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="between 50 and 99.9"):
            set_setting(tmp_path, "autoswitch.threshold", "200")
        assert not settings_path(tmp_path).exists()

    def test_set_rejects_unknown_key(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="unknown setting"):
            set_setting(tmp_path, "autoswitch.bogus", "1")

    def test_set_rejects_bool_words_strictly(self, tmp_path: Path):
        assert set_setting(tmp_path, "autoswitch.includeApiKeyAccounts", "FALSE") is False
        with pytest.raises(ConfigError, match="true or false"):
            set_setting(tmp_path, "autoswitch.includeApiKeyAccounts", "falsy")

    def test_set_on_corrupt_file_raises_and_preserves_it(self, tmp_path: Path):
        settings_path(tmp_path).write_text("{not json")
        with pytest.raises(ConfigError, match="not valid JSON"):
            set_setting(tmp_path, "autoswitch.threshold", "80")
        assert settings_path(tmp_path).read_text() == "{not json"

    def test_unset_removes_key_and_empty_section(self, tmp_path: Path):
        set_setting(tmp_path, "autoswitch.threshold", "80")
        assert unset_setting(tmp_path, "autoswitch.threshold") is True
        raw = json.loads(settings_path(tmp_path).read_text())
        assert "autoswitch" not in raw

    def test_unset_stamps_schema_version_on_unversioned_file(self, tmp_path: Path):
        settings_path(tmp_path).write_text(
            json.dumps({"autoswitch": {"threshold": 80}})
        )
        assert unset_setting(tmp_path, "autoswitch.threshold") is True
        raw = json.loads(settings_path(tmp_path).read_text())
        assert raw["schemaVersion"] == 1

    def test_unset_absent_key_is_noop(self, tmp_path: Path):
        assert unset_setting(tmp_path, "autoswitch.threshold") is False
        assert not settings_path(tmp_path).exists()


class TestEffectiveSettings:
    def test_missing_file_reports_all_defaults(self, tmp_path: Path):
        rows = effective_settings(tmp_path)
        assert len(rows) == len(SETTING_SPECS)
        assert all(not is_set for _, _, is_set in rows)

    def test_presence_not_value_equality_marks_set(self, tmp_path: Path):
        set_setting(tmp_path, "autoswitch.threshold", "90")  # equals default
        by_key = {spec.dotted: is_set for spec, _, is_set in effective_settings(tmp_path)}
        assert by_key["autoswitch.threshold"] is True
        assert by_key["autoswitch.intervalSeconds"] is False


class TestMergedWithCli:
    def test_no_flags_returns_settings_unchanged(self):
        base = AutoSwitchSettings(threshold=80.0)
        assert merged_with_cli(base, _args()) is base

    def test_cli_beats_settings(self):
        base = AutoSwitchSettings(threshold=80.0, cooldown_seconds=10.0)
        merged = merged_with_cli(base, _args(threshold=60.0, interval=30.0))
        assert merged.threshold == 60.0
        assert merged.interval_seconds == 30.0
        assert merged.cooldown_seconds == 10.0  # untouched

    def test_cli_values_are_clamped(self):
        merged = merged_with_cli(AutoSwitchSettings(), _args(interval=1.0))
        assert merged.interval_seconds == 15.0

    def test_boolean_override(self):
        merged = merged_with_cli(
            AutoSwitchSettings(), _args(include_api_key_accounts=True)
        )
        assert merged.include_api_key_accounts is True
