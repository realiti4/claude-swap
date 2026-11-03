"""Tests for settings.json load/save/merge (settings.py)."""

from __future__ import annotations

import argparse
import json
import stat
import os
import sys
from pathlib import Path

import pytest

from claude_swap.exceptions import ConfigError
from claude_swap.settings import (
    SETTING_SPECS,
    atomic_write_json,
    AutoSwitchSettings,
    RunSettings,
    UiSettings,
    UpdateSettings,
    effective_settings,
    load_run_settings,
    load_settings,
    load_ui_settings,
    load_update_settings,
    merged_with_cli,
    save_settings,
    set_setting,
    settings_path,
    unset_setting,
)


def _args(**kwargs) -> argparse.Namespace:
    defaults = {
        "threshold": None,
        "weekly_threshold": None,
        "interval": None,
        "cooldown": None,
        "include_api_key_accounts": None,
        "strategy": None,
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

    def test_consume_first_is_a_valid_strategy(self, tmp_path: Path):
        settings_path(tmp_path).write_text(
            json.dumps({"autoswitch": {"strategy": "consume-first"}})
        )
        assert load_settings(tmp_path).strategy == "consume-first"

    def test_set_strategy_consume_first(self, tmp_path: Path):
        set_setting(tmp_path, "autoswitch.strategy", "consume-first")
        assert load_settings(tmp_path).strategy == "consume-first"


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


class TestUiSettings:
    def test_missing_file_defaults_to_auto(self, tmp_path: Path):
        assert load_ui_settings(tmp_path) == UiSettings(theme="auto")

    def test_reads_auto(self, tmp_path: Path):
        settings_path(tmp_path).write_text(json.dumps({"ui": {"theme": "auto"}}))
        assert load_ui_settings(tmp_path).theme == "auto"

    def test_reads_light(self, tmp_path: Path):
        settings_path(tmp_path).write_text(json.dumps({"ui": {"theme": "light"}}))
        assert load_ui_settings(tmp_path).theme == "light"

    def test_unknown_theme_clamps_to_default(self, tmp_path: Path):
        settings_path(tmp_path).write_text(json.dumps({"ui": {"theme": "purple"}}))
        assert load_ui_settings(tmp_path).theme == "auto"

    def test_set_and_unset_ui_theme(self, tmp_path: Path):
        assert set_setting(tmp_path, "ui.theme", "light") == "light"
        raw = json.loads(settings_path(tmp_path).read_text())
        assert raw == {"schemaVersion": 1, "ui": {"theme": "light"}}
        assert unset_setting(tmp_path, "ui.theme") is True
        assert "ui" not in json.loads(settings_path(tmp_path).read_text())

    def test_set_rejects_bad_choice(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="dark, light"):
            set_setting(tmp_path, "ui.theme", "purple")


class TestRunSettings:
    def test_missing_file_defaults_to_per_account_history(self, tmp_path: Path):
        assert load_run_settings(tmp_path) == RunSettings(share_history=False)

    def test_reads_share_history(self, tmp_path: Path):
        settings_path(tmp_path).write_text(json.dumps({"run": {"shareHistory": True}}))
        assert load_run_settings(tmp_path).share_history is True

    def test_non_bool_falls_back_to_default(self, tmp_path: Path):
        settings_path(tmp_path).write_text(json.dumps({"run": {"shareHistory": "yes"}}))
        assert load_run_settings(tmp_path).share_history is False

    def test_set_share_history(self, tmp_path: Path):
        assert set_setting(tmp_path, "run.shareHistory", "true") is True
        raw = json.loads(settings_path(tmp_path).read_text())
        assert raw == {"schemaVersion": 1, "run": {"shareHistory": True}}


class TestUpdateSettings:
    def test_missing_file_checks(self, tmp_path: Path):
        assert load_update_settings(tmp_path) == UpdateSettings(check=True)

    def test_reads_false(self, tmp_path: Path):
        settings_path(tmp_path).write_text(json.dumps({"updates": {"check": False}}))
        assert load_update_settings(tmp_path).check is False

    def test_non_boolean_falls_back_to_default(self, tmp_path: Path):
        settings_path(tmp_path).write_text(json.dumps({"updates": {"check": "false"}}))
        assert load_update_settings(tmp_path).check is True

    def test_set_and_unset_updates_check(self, tmp_path: Path):
        assert set_setting(tmp_path, "updates.check", "false") is False
        raw = json.loads(settings_path(tmp_path).read_text())
        assert raw == {"schemaVersion": 1, "updates": {"check": False}}
        assert load_update_settings(tmp_path).check is False
        assert unset_setting(tmp_path, "updates.check") is True
        assert "updates" not in json.loads(settings_path(tmp_path).read_text())


class TestSettingSpecs:
    def test_registry_covers_every_dataclass_field(self):
        by_section: dict[str, set[str]] = {}
        for spec in SETTING_SPECS.values():
            by_section.setdefault(spec.section, set()).add(spec.field)
        assert by_section["autoswitch"] == {
            f.name for f in AutoSwitchSettings.__dataclass_fields__.values()
        }
        assert by_section["ui"] == {
            f.name for f in UiSettings.__dataclass_fields__.values()
        }
        assert by_section["run"] == {
            f.name for f in RunSettings.__dataclass_fields__.values()
        }
        assert by_section["updates"] == {
            f.name for f in UpdateSettings.__dataclass_fields__.values()
        }

    def test_defaults_match_dataclass(self):
        sources = {
            "autoswitch": AutoSwitchSettings(),
            "ui": UiSettings(),
            "run": RunSettings(),
            "updates": UpdateSettings(),
        }
        for spec in SETTING_SPECS.values():
            assert spec.default == getattr(sources[spec.section], spec.field)


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

    def test_set_string_kind_round_trips(self, tmp_path: Path):
        assert set_setting(tmp_path, "autoswitch.model", "Fable") == "Fable"
        raw = json.loads(settings_path(tmp_path).read_text())
        assert raw["autoswitch"]["model"] == "Fable"
        assert load_settings(tmp_path).model == "Fable"

    def test_set_string_kind_rejects_empty(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="unset"):
            set_setting(tmp_path, "autoswitch.model", "   ")
        assert not settings_path(tmp_path).exists()

    def test_garbage_model_value_falls_back_to_none(self, tmp_path: Path):
        settings_path(tmp_path).write_text(
            json.dumps({"autoswitch": {"model": 123}})
        )
        assert load_settings(tmp_path).model is None

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

    def test_model_override(self):
        merged = merged_with_cli(AutoSwitchSettings(), _args(model="Fable"))
        assert merged.model == "Fable"

    def test_strategy_override(self):
        merged = merged_with_cli(AutoSwitchSettings(), _args(strategy="consume-first"))
        assert merged.strategy == "consume-first"


class TestAtomicWriteThroughSymlink:
    """A rename does not follow links, so renaming onto a symlinked path
    detaches it and the target silently stops updating. Covers the write
    itself plus the two placement decisions it forces: the temp file goes
    beside the RESOLVED target (else EXDEV across mounts), the 0700 chmod
    stays on the directory cswap owns (else it narrows — or cannot touch —
    a foreign one)."""

    def test_write_preserves_the_link_and_updates_the_target(self, tmp_path):
        repo = tmp_path / "repo"; repo.mkdir()
        live = tmp_path / "live"; live.mkdir()
        tracked = repo / "settings.json"
        tracked.write_text(json.dumps({"tracked": True}))
        link = live / "settings.json"
        link.symlink_to(tracked)

        atomic_write_json(link, {"written": "through"})

        assert link.is_symlink(), "the dotfiles link must survive the write"
        assert json.loads(tracked.read_text()) == {"written": "through"}

    @pytest.mark.skipif(
        not hasattr(os.path, "ALLOW_MISSING"),
        reason="pre-3.12.11 resolver refuses a dangling link",
    )
    def test_dangling_link_writes_where_it_points(self, tmp_path):
        target = tmp_path / "gone" / "settings.json"
        link = tmp_path / "settings.json"
        link.symlink_to(target)

        atomic_write_json(link, {"dangling": "ok"})

        assert link.is_symlink()
        assert json.loads(target.read_text()) == {"dangling": "ok"}

    def test_plain_file_write_unchanged(self, tmp_path):
        p = tmp_path / "settings.json"
        atomic_write_json(p, {"plain": 1})
        assert not p.is_symlink()
        assert json.loads(p.read_text()) == {"plain": 1}

    def test_temp_file_is_created_beside_the_target(self, tmp_path, monkeypatch):
        """Beside the LINK, the rename hits EXDEV whenever the target is on
        another mount — the write fails outright. Assert the placement
        directly; staging two filesystems in a unit test is not portable."""
        import tempfile
        repo = tmp_path / "repo"; repo.mkdir()
        live = tmp_path / "live"; live.mkdir()
        tracked = repo / "settings.json"; tracked.write_text("{}")
        link = live / "settings.json"; link.symlink_to(tracked)
        seen = []
        real_mkstemp = tempfile.mkstemp
        monkeypatch.setattr(
            tempfile, "mkstemp",
            lambda *a, **kw: (seen.append(kw.get("dir")), real_mkstemp(*a, **kw))[1],
        )

        atomic_write_json(link, {"x": 1})

        assert seen == [str(repo)], f"tmp must land beside the target, got {seen}"

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
    def test_hardening_stays_on_the_directory_cswap_owns(self, tmp_path):
        """The 0700 belongs to cswap's own dir. On the target's parent it
        would narrow a foreign directory, and raise PermissionError when
        that parent cannot be chmod'ed at all."""
        repo = tmp_path / "repo"; repo.mkdir(mode=0o755)
        live = tmp_path / "live"; live.mkdir()
        tracked = repo / "settings.json"; tracked.write_text("{}")
        link = live / "settings.json"; link.symlink_to(tracked)

        atomic_write_json(link, {"x": 1})

        assert (repo.stat().st_mode & 0o777) == 0o755, "foreign dir untouched"
        assert (live.stat().st_mode & 0o777) == 0o700, "our dir hardened"
        assert (tracked.stat().st_mode & 0o777) == 0o600, "file still 0600"


class TestWeeklyThreshold:
    """`weekly_threshold`: optional, same bounds as `threshold`, forgiving on
    load and loud on `set` — the registry's first optional float."""

    def test_defaults_to_none(self, tmp_path: Path):
        assert AutoSwitchSettings().weekly_threshold is None
        assert load_settings(tmp_path).weekly_threshold is None

    @pytest.mark.parametrize(
        "raw,expected",
        [("97", 97.0), ("970", 99.9), ("10", 50.0), ('"banana"', None), ("null", None)],
    )
    def test_load_clamps_numbers_and_drops_garbage(self, tmp_path: Path, raw, expected):
        settings_path(tmp_path).write_text(
            '{"autoswitch": {"weeklyThreshold": %s}}' % raw
        )
        assert load_settings(tmp_path).weekly_threshold == expected

    @pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
    def test_non_finite_numbers_fall_back_to_defaults(self, tmp_path: Path, literal):
        # json.loads accepts these literals and min/max pass NaN through, so
        # the clamp must treat non-finite as garbage for both lines — a NaN
        # line would never be crossed.
        settings_path(tmp_path).write_text(
            '{"autoswitch": {"threshold": %s, "weeklyThreshold": %s}}'
            % (literal, literal)
        )
        loaded = load_settings(tmp_path)
        assert loaded.threshold == AutoSwitchSettings().threshold
        assert loaded.weekly_threshold is None

    def test_an_integer_too_large_for_a_float_still_clamps(self, tmp_path: Path):
        # Python ints are unbounded; isfinite() on one past float range raises
        # OverflowError. The clamp must keep answering 99.9 for both lines.
        huge = "1" + "0" * 400
        settings_path(tmp_path).write_text(
            '{"autoswitch": {"threshold": %s, "weeklyThreshold": %s}}' % (huge, huge)
        )
        loaded = load_settings(tmp_path)
        assert (loaded.threshold, loaded.weekly_threshold) == (99.9, 99.9)

    def test_set_validates_loudly_and_unset_clears(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="between 50 and 99.9"):
            set_setting(tmp_path, "autoswitch.weeklyThreshold", "970")
        with pytest.raises(ConfigError, match="expects a number.*config unset"):
            set_setting(tmp_path, "autoswitch.weeklyThreshold", "banana")
        assert not settings_path(tmp_path).exists()
        assert set_setting(tmp_path, "autoswitch.weeklyThreshold", "97") == 97.0
        assert load_settings(tmp_path).weekly_threshold == 97.0
        assert unset_setting(tmp_path, "autoswitch.weeklyThreshold") is True
        assert load_settings(tmp_path).weekly_threshold is None

    def test_a_weekly_line_below_the_5h_line_is_accepted(self, tmp_path: Path):
        # "Keep a weekly reserve" is a legitimate policy; no cross-field rule.
        set_setting(tmp_path, "autoswitch.threshold", "90")
        assert set_setting(tmp_path, "autoswitch.weeklyThreshold", "80") == 80.0
        loaded = load_settings(tmp_path)
        assert (loaded.threshold, loaded.weekly_threshold) == (90.0, 80.0)

    def test_cli_override_and_anchor(self):
        merged = merged_with_cli(
            AutoSwitchSettings(weekly_threshold=95.0), _args(weekly_threshold=97.0)
        )
        assert merged.weekly_threshold == 97.0
        # --threshold alone leaves a configured weekly line in force (the
        # #355 anchor bug class: both lines come from the one settings object).
        merged = merged_with_cli(
            AutoSwitchSettings(threshold=90.0, weekly_threshold=97.0),
            _args(threshold=85.0),
        )
        assert (merged.threshold, merged.weekly_threshold) == (85.0, 97.0)

    def test_cli_value_is_clamped(self):
        merged = merged_with_cli(AutoSwitchSettings(), _args(weekly_threshold=970.0))
        assert merged.weekly_threshold == 99.9
