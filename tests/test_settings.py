"""Tests for settings.json load/save/merge (settings.py)."""

from __future__ import annotations

import argparse
import json
import stat
import sys
from pathlib import Path

import pytest

from claude_swap import settings
from claude_swap.exceptions import ConfigError
from claude_swap.settings import (
    SETTING_SPECS,
    atomic_write_json,
    AutoSwitchSettings,
    SessionsSettings,
    UiSettings,
    effective_settings,
    load_settings,
    load_ui_settings,
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


class TestBalanceSettings:
    def test_balance_is_a_valid_strategy(self, tmp_path: Path):
        settings_path(tmp_path).write_text(
            json.dumps({"autoswitch": {"strategy": "balance"}})
        )
        assert load_settings(tmp_path).strategy == "balance"

    def test_set_strategy_balance(self, tmp_path: Path):
        set_setting(tmp_path, "autoswitch.strategy", "balance")
        assert load_settings(tmp_path).strategy == "balance"

    def test_balance_defaults(self):
        s = AutoSwitchSettings()
        assert s.balance_lead_hours == 24.0
        assert s.balance_five_hour_ceiling == 85.0
        assert s.balance_five_hour_weight == 0.5
        assert s.balance_load_per_session == 15.0

    def test_balance_keys_load_from_camel_case(self, tmp_path: Path):
        settings_path(tmp_path).write_text(json.dumps({
            "autoswitch": {
                "balanceLeadHours": 12,
                "balanceFiveHourCeiling": 70,
                "balanceFiveHourWeight": 1.5,
                "balanceLoadPerSession": 20,
            }
        }))
        loaded = load_settings(tmp_path)
        assert loaded.balance_lead_hours == 12.0
        assert loaded.balance_five_hour_ceiling == 70.0
        assert loaded.balance_five_hour_weight == 1.5
        assert loaded.balance_load_per_session == 20.0

    def test_balance_values_are_clamped_on_load(self, tmp_path: Path):
        settings_path(tmp_path).write_text(json.dumps({
            "autoswitch": {
                "balanceLeadHours": 500,
                "balanceFiveHourCeiling": 1,
                "balanceFiveHourWeight": -2,
                "balanceLoadPerSession": 250,
            }
        }))
        loaded = load_settings(tmp_path)
        assert loaded.balance_lead_hours == 96.0
        assert loaded.balance_five_hour_ceiling == 10.0
        assert loaded.balance_five_hour_weight == 0.0
        assert loaded.balance_load_per_session == 100.0

    def test_balance_bad_types_fall_back_to_defaults(self, tmp_path: Path):
        settings_path(tmp_path).write_text(json.dumps({
            "autoswitch": {"balanceLeadHours": "soon", "balanceFiveHourWeight": True}
        }))
        loaded = load_settings(tmp_path)
        assert loaded.balance_lead_hours == 24.0
        assert loaded.balance_five_hour_weight == 0.5

    @pytest.mark.parametrize(
        ("key", "value", "bounds"),
        [
            ("autoswitch.balanceLeadHours", "97", "between 0 and 96"),
            ("autoswitch.balanceLeadHours", "-1", "between 0 and 96"),
            ("autoswitch.balanceFiveHourCeiling", "9", "between 10 and 100"),
            ("autoswitch.balanceFiveHourCeiling", "101", "between 10 and 100"),
            ("autoswitch.balanceFiveHourWeight", "5.5", "between 0 and 5"),
            ("autoswitch.balanceLoadPerSession", "100.1", "between 0 and 100"),
        ],
    )
    def test_set_rejects_out_of_range(self, tmp_path: Path, key, value, bounds):
        with pytest.raises(ConfigError, match=bounds):
            set_setting(tmp_path, key, value)
        assert not settings_path(tmp_path).exists()

    def test_set_accepts_boundaries(self, tmp_path: Path):
        set_setting(tmp_path, "autoswitch.balanceLeadHours", "0")
        set_setting(tmp_path, "autoswitch.balanceFiveHourCeiling", "100")
        loaded = load_settings(tmp_path)
        assert loaded.balance_lead_hours == 0.0
        assert loaded.balance_five_hour_ceiling == 100.0


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
        assert by_section["sessions"] == {
            f.name for f in SessionsSettings.__dataclass_fields__.values()
        }

    def test_defaults_match_dataclass(self):
        sources = {
            "autoswitch": AutoSwitchSettings(),
            "ui": UiSettings(),
            "sessions": SessionsSettings(),
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

    def test_balance_strategy_override(self):
        merged = merged_with_cli(AutoSwitchSettings(), _args(strategy="balance"))
        assert merged.strategy == "balance"


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
        from claude_swap import settings as S
        repo = tmp_path / "repo"; repo.mkdir()
        live = tmp_path / "live"; live.mkdir()
        tracked = repo / "settings.json"; tracked.write_text("{}")
        link = live / "settings.json"; link.symlink_to(tracked)
        seen = []
        real_mkstemp = tempfile.mkstemp
        monkeypatch.setattr(
            S.tempfile, "mkstemp",
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


class TestSessionsSection:
    def test_defaults_without_a_file(self, tmp_path):
        s = settings.load_session_settings(tmp_path)
        assert (s.idle_reassign_minutes, s.reassign_margin) == (60.0, 15.0)

    def test_values_are_read(self, tmp_path):
        (tmp_path / "settings.json").write_text(json.dumps({
            "schemaVersion": 1,
            "sessions": {"idleReassignMinutes": 30, "reassignMargin": 5},
        }))
        s = settings.load_session_settings(tmp_path)
        assert (s.idle_reassign_minutes, s.reassign_margin) == (30.0, 5.0)

    def test_out_of_range_values_are_clamped(self, tmp_path):
        (tmp_path / "settings.json").write_text(json.dumps({
            "sessions": {"idleReassignMinutes": 0, "reassignMargin": 500},
        }))
        s = settings.load_session_settings(tmp_path)
        assert (s.idle_reassign_minutes, s.reassign_margin) == (5.0, 100.0)

    def test_garbage_values_fall_back_to_defaults(self, tmp_path):
        (tmp_path / "settings.json").write_text(json.dumps({
            "sessions": {"idleReassignMinutes": "soon", "reassignMargin": True},
        }))
        s = settings.load_session_settings(tmp_path)
        assert (s.idle_reassign_minutes, s.reassign_margin) == (60.0, 15.0)

    def test_set_setting_writes_only_that_key(self, tmp_path):
        settings.set_setting(tmp_path, "sessions.idleReassignMinutes", "20")
        raw = json.loads((tmp_path / "settings.json").read_text())
        assert raw["sessions"] == {"idleReassignMinutes": 20.0}

    def test_effective_settings_lists_the_section(self, tmp_path):
        rows = {spec.dotted: (value, is_set)
                for spec, value, is_set in settings.effective_settings(tmp_path)}
        assert rows["sessions.idleReassignMinutes"] == (60.0, False)
        assert rows["sessions.reassignMargin"] == (15.0, False)

    def test_a_rejected_value_names_the_bounds(self, tmp_path):
        with pytest.raises(ConfigError, match="between 5 and 1440"):
            settings.set_setting(tmp_path, "sessions.idleReassignMinutes", "2")
