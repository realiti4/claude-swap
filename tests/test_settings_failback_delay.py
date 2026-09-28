"""MEU-FBD-01 - the ``autoswitch.failbackDelaySeconds`` settings surface.

Covers AC-1..AC-5.

The load-bearing distinction in this file is **`0.0` is not `None`**. Unset
means "failback keeps following `cooldownSeconds`"; `0` means "hand back on the
first fresh eligible poll". An implementation that spells the check
``if not delay:`` conflates the two and silently turns the one value the
follow-up exists for (ivan-andreyev, #318 comment 5739400557) back into a
300-second floor. Every test below that passes `0` is there to catch exactly
that.

Synthetic fixtures only: every case writes into ``tmp_path``. Nothing here
reads a real account store.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from claude_swap.exceptions import ConfigError
from claude_swap.settings import (
    SETTING_SPECS,
    AutoSwitchSettings,
    effective_settings,
    format_setting_value,
    load_settings,
    merged_with_cli,
    parse_setting_value,
    set_setting,
    setting_spec,
    settings_path,
    unset_setting,
)

KEY = "autoswitch.failbackDelaySeconds"


def _args(**kwargs) -> argparse.Namespace:
    defaults = {
        "threshold": None,
        "interval": None,
        "cooldown": None,
        "failback_delay": None,
        "include_api_key_accounts": None,
        "strategy": None,
        "model": None,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _write(tmp_path: Path, value) -> None:
    """Seed settings.json with one raw `failbackDelaySeconds` value."""
    settings_path(tmp_path).write_text(
        json.dumps({"schemaVersion": 1, "autoswitch": {"failbackDelaySeconds": value}}),
        encoding="utf-8",
    )


class TestTheSpecExists:
    """AC-1 - the registry entry, with `cooldownSeconds`' own bounds."""

    def test_the_dataclass_field_defaults_to_none(self):
        assert AutoSwitchSettings().failback_delay_seconds is None

    def test_the_spec_is_registered_as_a_float(self):
        spec = setting_spec(KEY)
        assert spec.section == "autoswitch"
        assert spec.field == "failback_delay_seconds"
        assert spec.kind == "float"

    def test_it_borrows_the_cooldown_bounds(self):
        """A different range from the sibling timer would be arbitrary: both
        are "seconds between switches" knobs on the same engine."""
        spec = setting_spec(KEY)
        cooldown = SETTING_SPECS["autoswitch.cooldownSeconds"]
        assert (spec.lo, spec.hi) == (cooldown.lo, cooldown.hi) == (0.0, 86400.0)

    def test_the_help_names_the_fallback(self):
        """A user reading `cswap config` must learn what unset means, because
        unset is not zero."""
        assert "cooldownSeconds" in setting_spec(KEY).help


class TestLenientLoad:
    """AC-2 - a garbage file degrades to unset; a number is clamped."""

    @pytest.mark.parametrize("raw", [None, "abc", True, [], {}, "60"])
    def test_a_non_number_reads_as_unset(self, tmp_path: Path, raw):
        _write(tmp_path, raw)
        assert load_settings(tmp_path).failback_delay_seconds is None

    def test_an_absent_key_reads_as_unset(self, tmp_path: Path):
        settings_path(tmp_path).write_text(
            json.dumps({"schemaVersion": 1, "autoswitch": {"threshold": 80.0}}),
            encoding="utf-8",
        )
        assert load_settings(tmp_path).failback_delay_seconds is None

    def test_zero_survives_the_load_as_zero(self, tmp_path: Path):
        """The whole feature, in one assertion: `0` must not become `None`."""
        _write(tmp_path, 0)
        loaded = load_settings(tmp_path)
        assert loaded.failback_delay_seconds == 0.0
        assert loaded.failback_delay_seconds is not None

    @pytest.mark.parametrize("raw,expected", [(-5, 0.0), (999999, 86400.0), (60, 60.0)])
    def test_numbers_are_clamped_into_the_range(self, tmp_path: Path, raw, expected):
        _write(tmp_path, raw)
        assert load_settings(tmp_path).failback_delay_seconds == expected

    def test_an_unrelated_setting_is_untouched(self, tmp_path: Path):
        _write(tmp_path, 30)
        assert load_settings(tmp_path).cooldown_seconds == 300.0


class TestStrictSet:
    """AC-3 - `cswap config set` rejects loudly rather than clamping."""

    def test_a_non_number_raises_naming_the_key(self):
        with pytest.raises(ConfigError) as e:
            parse_setting_value(setting_spec(KEY), "abc")
        assert KEY in str(e.value)
        assert "number" in str(e.value)

    def test_out_of_range_raises_naming_the_bounds(self):
        with pytest.raises(ConfigError) as e:
            parse_setting_value(setting_spec(KEY), "86401")
        assert "86400" in str(e.value)

    def test_zero_is_accepted_and_persisted(self, tmp_path: Path):
        assert set_setting(tmp_path, KEY, "0") == 0.0
        raw = json.loads(settings_path(tmp_path).read_text(encoding="utf-8"))
        assert raw["autoswitch"]["failbackDelaySeconds"] == 0.0
        assert load_settings(tmp_path).failback_delay_seconds == 0.0

    def test_setting_it_does_not_freeze_the_other_defaults(self, tmp_path: Path):
        """`set_setting` writes one key; a file that also pinned today's
        cooldown default would silently pin the user to it forever."""
        set_setting(tmp_path, KEY, "45")
        raw = json.loads(settings_path(tmp_path).read_text(encoding="utf-8"))
        assert list(raw["autoswitch"]) == ["failbackDelaySeconds"]


class TestUnset:
    """AC-4 - clearing returns to unset, and the listing says so."""

    def test_unset_removes_the_key_and_restores_none(self, tmp_path: Path):
        set_setting(tmp_path, KEY, "45")
        assert unset_setting(tmp_path, KEY) is True
        assert load_settings(tmp_path).failback_delay_seconds is None

    def test_unset_on_an_absent_key_is_a_no_op(self, tmp_path: Path):
        assert unset_setting(tmp_path, KEY) is False

    def test_the_listing_shows_none_and_default_when_unset(self, tmp_path: Path):
        rows = {spec.dotted: (value, is_set) for spec, value, is_set in
                effective_settings(tmp_path)}
        value, is_set = rows[KEY]
        assert value is None
        assert is_set is False
        assert format_setting_value(value) == "(none)"

    def test_the_listing_shows_zero_as_zero_not_none(self, tmp_path: Path):
        """`(none)` for an explicit `0` would tell the user the opposite of
        what their fleet does."""
        set_setting(tmp_path, KEY, "0")
        rows = {spec.dotted: (value, is_set) for spec, value, is_set in
                effective_settings(tmp_path)}
        value, is_set = rows[KEY]
        assert value == 0.0
        assert is_set is True
        assert format_setting_value(value) == "0"


class TestCliOverlay:
    """AC-5 - `cswap auto --failback-delay` overlays, absence does not."""

    def test_the_flag_overlays_the_file(self):
        merged = merged_with_cli(
            AutoSwitchSettings(failback_delay_seconds=600.0),
            _args(failback_delay=30.0),
        )
        assert merged.failback_delay_seconds == 30.0

    def test_zero_from_the_flag_overlays_a_configured_value(self):
        """`if value is not None` is the only correct test here; `if value:`
        drops `--failback-delay 0` on the floor."""
        merged = merged_with_cli(
            AutoSwitchSettings(failback_delay_seconds=600.0),
            _args(failback_delay=0.0),
        )
        assert merged.failback_delay_seconds == 0.0

    def test_an_absent_flag_leaves_the_configured_value_alone(self):
        merged = merged_with_cli(
            AutoSwitchSettings(failback_delay_seconds=600.0), _args()
        )
        assert merged.failback_delay_seconds == 600.0

    def test_an_absent_flag_leaves_unset_unset(self):
        merged = merged_with_cli(AutoSwitchSettings(), _args())
        assert merged.failback_delay_seconds is None

    def test_the_flag_is_clamped_like_every_other_override(self):
        merged = merged_with_cli(AutoSwitchSettings(), _args(failback_delay=999999.0))
        assert merged.failback_delay_seconds == 86400.0
