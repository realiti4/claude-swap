"""Model control (modelctl.py): which per-model windows count, and settings.json."""

import json
import os
import sys

import pytest

from claude_swap import modelctl


def test_parse_model_map():
    assert modelctl.parse_model_map("1=claude-fable-5-2, 2=claude-opus-5-5, x=y, 3=") == {
        "1": "claude-fable-5-2",
        "2": "claude-opus-5-5",
    }
    assert modelctl.parse_model_map(None) == {}


def test_matches():
    assert modelctl.matches("Fable", "claude-fable-5-2")
    assert not modelctl.matches("Fable", "claude-opus-5-5")
    assert not modelctl.matches("Fable", None)


# -- view_usage -------------------------------------------------------------


def _usage(fable_pct, opus_pct=None):
    scoped = [{"name": "Fable", "pct": fable_pct}]
    if opus_pct is not None:
        scoped.append({"name": "Opus", "pct": opus_pct})
    return {"seven_day": {"pct": 10}, "scoped": scoped}


def _view(usage, **kw):
    args = dict(models=("Fable",), model_map={}, configured=None,
                fallback="claude-opus-5-5", line=90.0, weekly=None, recent={})
    args.update(kw)
    return modelctl.view_usage(usage, "1", **args)


def test_unused_model_window_is_ignored():
    view = _view(_usage(100))
    assert view.ignored_windows == ["Fable"]
    assert view.usage["scoped"] == []
    assert view.model_to_set is None


def test_recently_used_model_window_counts():
    view = _view(_usage(100), recent={"fable": True})
    assert view.ignored_windows == []


def test_mapped_model_window_counts_and_falls_back_when_over():
    view = _view(_usage(95), model_map={"1": "claude-fable-5-2"})
    assert view.using_fallback
    assert view.model_to_set == "claude-opus-5-5"
    # Now running on the fallback, the Fable window no longer binds.
    assert view.ignored_windows == ["Fable"]


def test_mapped_model_under_its_line_is_set():
    view = _view(_usage(50), model_map={"1": "claude-fable-5-2"})
    assert not view.using_fallback
    assert view.model_to_set == "claude-fable-5-2"
    assert view.ignored_windows == []


def test_no_fallback_when_the_fallback_is_over_too():
    view = _view(_usage(95, opus_pct=99), models=("Fable", "Opus"),
                 model_map={"1": "claude-fable-5-2"})
    assert not view.using_fallback


def test_window_without_a_figure_does_not_crash():
    usage = {"seven_day": {"pct": 10}, "scoped": [{"name": "Fable", "pct": 95},
                                                  {"name": "Opus", "pct": None}]}
    view = _view(usage, models=("Fable", "Opus"), model_map={"1": "claude-fable-5-2"})
    assert view.using_fallback


@pytest.mark.parametrize("usage", [None, "token-expired"])
def test_unreadable_usage_passes_through_with_mapped_model(usage):
    view = _view(usage, model_map={"1": "claude-fable-5-2"})
    assert view.usage == usage and view.model_to_set == "claude-fable-5-2"


class _FakeLedger:
    def __init__(self, seen):
        self.seen = seen

    def model_last_seen(self, name):
        return self.seen.get(name)


def test_models_in_use():
    ledger = _FakeLedger({"Fable": 1000.0})
    assert modelctl.models_in_use(ledger, ("Fable", "Opus"), 600, 1500.0) == {
        "fable": True, "opus": False}
    assert modelctl.models_in_use(ledger, ("Fable",), 600, 2000.0) == {"fable": False}


# -- settings.json ----------------------------------------------------------


@pytest.fixture
def claude_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    return tmp_path


def test_read_missing_file_is_readable_default(claude_home):
    assert modelctl.read_model() == (True, None)


def test_read_damaged_file_is_unreadable(claude_home):
    (claude_home / "settings.json").write_text("{not json")
    assert modelctl.read_model() == (False, None)


def test_write_changes_only_the_model_key(claude_home):
    path = claude_home / "settings.json"
    path.write_text(json.dumps({"theme": "dark", "model": "old"}))
    modelctl.write_model("claude-fable-5-2")
    assert json.loads(path.read_text()) == {"theme": "dark", "model": "claude-fable-5-2"}
    modelctl.write_model(None)
    assert json.loads(path.read_text()) == {"theme": "dark"}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_write_keeps_file_mode(claude_home):
    path = claude_home / "settings.json"
    path.write_text("{}")
    os.chmod(path, 0o640)
    modelctl.write_model("x")
    assert path.stat().st_mode & 0o777 == 0o640


def test_write_refuses_a_non_object(claude_home):
    path = claude_home / "settings.json"
    path.write_text("[1, 2]")
    with pytest.raises(ValueError):
        modelctl.write_model("x")
    assert path.read_text() == "[1, 2]"


def test_state_round_trip(tmp_path):
    assert modelctl.load_state(tmp_path) == {}
    modelctl.save_state(tmp_path, {"saved": None, "applied": "m"})
    assert modelctl.load_state(tmp_path) == {"saved": None, "applied": "m"}


# -- the engine's apply step ------------------------------------------------


def _engine(temp_home, *, dry_run):
    from tests.test_autoswitch import EngineHarness

    h = EngineHarness(temp_home, model="Fable", model_control=True,
                      model_map="1=claude-fable-5-2")
    h.seed(1, "account1@example.com")
    h.seed(2, "account2@example.com")
    h.make_live("account1@example.com", 1)
    h.engine = h._make_engine(dry_run=dry_run)
    return h


def test_dry_run_reports_without_writing(temp_home):
    from claude_swap.autoswitch import ModelEvent
    from claude_swap.paths import get_claude_config_home

    h = _engine(temp_home, dry_run=True)
    h.tick_with_usage({
        "1": {"seven_day": {"pct": 10}, "scoped": [{"name": "Fable", "pct": 20}]},
        "2": {"seven_day": {"pct": 10}},
    })
    notes = [e.detail for e in h.events if isinstance(e, ModelEvent)]
    assert notes == ["[dry-run] would set Claude default model default -> claude-fable-5-2"]
    assert not (get_claude_config_home() / "settings.json").exists()


def test_write_then_restore_the_original_value(temp_home):
    from claude_swap.autoswitch import _ModelPlan
    from claude_swap.paths import get_claude_config_home

    h = _engine(temp_home, dry_run=False)
    settings_file = get_claude_config_home() / "settings.json"
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text(json.dumps({"model": "mine", "theme": "dark"}))

    plan = _ModelPlan(configured="mine", readable=True, recent={},
                      model_to_set={"1": "claude-fable-5-2"})
    note = h.engine._sync_model_setting(plan, "1")
    assert "mine -> claude-fable-5-2" in note
    assert json.loads(settings_file.read_text())["model"] == "claude-fable-5-2"

    # Nothing to set any more: the original value comes back.
    plan = _ModelPlan(configured="claude-fable-5-2", readable=True, recent={})
    h.engine._sync_model_setting(plan, "1")
    assert json.loads(settings_file.read_text()) == {"model": "mine", "theme": "dark"}
    assert modelctl.load_state(h.engine.switcher.backup_dir) == {}


def test_a_hand_edit_is_not_overwritten_on_restore(temp_home):
    from claude_swap.autoswitch import _ModelPlan
    from claude_swap.paths import get_claude_config_home

    h = _engine(temp_home, dry_run=False)
    settings_file = get_claude_config_home() / "settings.json"
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text(json.dumps({"model": "mine"}))
    h.engine._sync_model_setting(
        _ModelPlan(configured="mine", readable=True, recent={},
                   model_to_set={"1": "claude-fable-5-2"}), "1")
    # Someone picks another model by hand; cswap then has nothing to set.
    settings_file.write_text(json.dumps({"model": "their-choice"}))
    note = h.engine._sync_model_setting(
        _ModelPlan(configured="their-choice", readable=True, recent={}), "1")
    assert note == ""
    assert json.loads(settings_file.read_text())["model"] == "their-choice"


def test_unreadable_settings_file_is_left_alone(temp_home):
    from claude_swap.autoswitch import _ModelPlan

    h = _engine(temp_home, dry_run=False)
    note = h.engine._sync_model_setting(
        _ModelPlan(configured=None, readable=False, recent={},
                   model_to_set={"1": "claude-fable-5-2"}), "1")
    assert "unreadable" in note
