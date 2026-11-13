"""Deterministic daily-window, failover and secret-free snapshot checks."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock
import json
import urllib.error

import pytest

from claude_swap import schedule


def at(hour=7, minute=0, day=13):
    return datetime.fromisoformat(f"2026-11-{day:02}T{hour:02}:{minute:02}:00+08:00").timestamp()


def account(number, *, pct=10, weekly=20, active=False, age=0, reset=None,
            sentinel=None, disabled=False):
    data = {"five_hour": {"pct": pct, "resets_at": reset},
            "seven_day": {"pct": weekly, "resets_at": None}}
    usage = SimpleNamespace(age_s=age, sentinel=sentinel, last_good=data,
                            fetched_at=at()-age, decision_value=lambda: data if age <= 300 else None)
    return SimpleNamespace(number=str(number), email=f"person{number}@example.com", org_uuid="org",
                           usage=usage, is_active=active, switchable=True, disabled=disabled,
                           alias=f"alias{number}", subscription_plan=None)


def harness(tmp_path, monkeypatch, rows, now=None, probe=None):
    now = now or at()
    switcher = Mock(backup_dir=tmp_path)
    switcher.switch_to.return_value = {"switched": True}
    switcher._build_accounts_info.return_value = [(int(a.number), a.email, "org", a.org_uuid,
        a.is_active, json.dumps({"claudeAiOauth": {"accessToken": "SECRET"}}), a.alias) for a in rows]
    freshener = Mock()
    freshener._freshen_target.return_value = "ok"
    monkeypatch.setattr(schedule, "AutoSwitchEngine", lambda *a, **kw: freshener)
    snap = SimpleNamespace(accounts=rows, active_number=next((a.number for a in rows if a.is_active), None))
    source = Mock()
    source.take.return_value = snap
    monkeypatch.setattr(schedule, "SnapshotSource", lambda *_: source)
    clock = Mock(return_value=now)
    probe = probe or Mock(return_value={"five_hour": {"resets_at": schedule.iso(now + 18000)}})
    config = {"identities": {a.number: schedule.identity(a) for a in rows}}
    service = schedule.DailySchedule(switcher, config, clock=clock, probe=probe)
    return service, switcher, freshener, clock, probe


@pytest.mark.parametrize("hour,expected", [(0,None),(6,None),(7,"1"),(11,"1"),(12,"2"),(16,"2"),(17,"3"),(21,"3"),(22,None)])
def test_only_current_slot_receives_inference(tmp_path, monkeypatch, hour, expected):
    rows = [account(1), account(2, active=True), account(3)]
    service, switcher, _, _, probe = harness(tmp_path, monkeypatch, rows, now=at(hour))
    service.tick()
    attempts = [key.split(":")[-1] for key, rec in service.state["activations"].items() if rec.get("attempted_at")]
    assert attempts == ([] if expected is None else [expected])
    assert probe.call_count == (0 if expected is None else 1)


def test_restart_does_not_repeat_success(tmp_path, monkeypatch):
    rows = [account(1, active=True), account(2), account(3)]
    first, _, _, _, probe = harness(tmp_path, monkeypatch, rows)
    first.tick()
    second, _, _, _, probe2 = harness(tmp_path, monkeypatch, rows)
    second.tick()
    assert probe.call_count == 1
    probe2.assert_not_called()


def test_ambiguous_failure_backs_off_across_restart(tmp_path, monkeypatch):
    rows = [account(1, active=True), account(2), account(3)]
    first, _, _, clock, probe = harness(tmp_path, monkeypatch, rows)
    probe.side_effect = TimeoutError("Bearer SECRET must not leak")
    first.tick()
    second, _, _, _, probe2 = harness(tmp_path, monkeypatch, rows, now=at(7,1))
    second.tick()
    probe2.assert_not_called()
    assert "SECRET" not in (tmp_path / "schedule-state.json").read_text()
    assert "SECRET" not in (tmp_path / "schedule-status.json").read_text()


@pytest.mark.parametrize("reset,status", [(at(12),"already_active"),(at(11),"waiting_reset"),(at(16),"waiting_reset")])
def test_existing_window_never_sends_another_request(tmp_path, monkeypatch, reset, status):
    rows = [account(1, active=True, reset=schedule.iso(reset)), account(2), account(3)]
    service, _, _, _, probe = harness(tmp_path, monkeypatch, rows)
    service.tick()
    probe.assert_not_called()
    assert service.state["activations"]["2026-11-13:1"]["status"] == status


@pytest.mark.parametrize("pct,switches", [(98.9,False),(99,True),(100,True)])
def test_99_boundary_and_future_slot_fallback(tmp_path, monkeypatch, pct, switches):
    rows = [account(1, active=True, pct=pct), account(2, pct=25), account(3, pct=50)]
    service, switcher, _, _, _ = harness(tmp_path, monkeypatch, rows)
    service.tick()
    assert switcher.switch_to.called is switches
    if switches:
        switcher.switch_to.assert_called_once_with("2", json_output=True)
        assert "future slot" in service.last_event["detail"]


def test_prefer_started_slot_over_fresher_future_slot(tmp_path, monkeypatch):
    rows = [account(1, pct=80), account(2, pct=99, active=True), account(3, pct=0)]
    service, switcher, _, _, _ = harness(tmp_path, monkeypatch, rows, now=at(13))
    service.tick()
    switcher.switch_to.assert_called_once_with("1", json_output=True)


@pytest.mark.parametrize("bad", [dict(pct=99),dict(age=301),dict(sentinel="token expired"),dict(pct=float("nan")),dict(disabled=True)])
def test_unavailable_candidates_are_never_selected(tmp_path, monkeypatch, bad):
    rows = [account(1, pct=99, active=True), account(2, **bad), account(3, pct=100)]
    service, switcher, _, _, _ = harness(tmp_path, monkeypatch, rows)
    service.tick()
    switcher.switch_to.assert_not_called()
    assert service.last_event["kind"] == "all_unavailable"


def test_weekly_threshold_also_switches(tmp_path, monkeypatch):
    rows = [account(1, weekly=99, active=True), account(2), account(3)]
    service, switcher, _, _, _ = harness(tmp_path, monkeypatch, rows)
    service.tick()
    assert switcher.switch_to.called


def test_credential_refusal_tries_another_available_slot(tmp_path, monkeypatch):
    rows = [account(1, pct=99, active=True), account(2), account(3)]
    service, switcher, freshener, _, _ = harness(tmp_path, monkeypatch, rows)
    freshener._freshen_target.side_effect = ["skip-live-session", "ok"]
    service.tick()
    switcher.switch_to.assert_called_once_with("3", json_output=True)


def test_identity_replacement_cannot_receive_activation_or_switch(tmp_path, monkeypatch):
    rows = [account(1, active=True), account(2), account(3)]
    service, switcher, _, _, probe = harness(tmp_path, monkeypatch, rows)
    service.config["identities"]["1"] = "different"
    service.tick()
    probe.assert_not_called()
    switcher.switch_to.assert_not_called()
    assert service.error == "configured_account_missing_or_changed"


def test_snapshot_masks_identity_and_preserves_provider_values(tmp_path, monkeypatch):
    rows = [account(1, pct=22, active=True, reset=schedule.iso(at(12))), account(2), account(3)]
    service, _, _, _, _ = harness(tmp_path, monkeypatch, rows)
    service.tick()
    snapshot = json.loads((tmp_path / "schedule-status.json").read_text())
    assert snapshot["active_account_id"] == "1"
    assert snapshot["accounts"][0]["usage"]["five_hour"]["utilization"] == 22
    assert snapshot["accounts"][0]["email"] == "pe***@example.com"
    assert "person1@" not in json.dumps(snapshot)
    assert "org_uuid" not in json.dumps(snapshot)


def test_http_failure_honors_retry_after(tmp_path, monkeypatch):
    rows = [account(1, active=True), account(2), account(3)]
    service, _, _, _, probe = harness(tmp_path, monkeypatch, rows)
    probe.side_effect = urllib.error.HTTPError("url",429,"secret",{"Retry-After":"3600"},None)
    service.tick()
    record = service.state["activations"]["2026-11-13:1"]
    assert record["error"] == "http_429"
    assert record["retry_at"] == at()+3600


def test_after_midnight_new_day_can_activate(tmp_path, monkeypatch):
    rows = [account(1, active=True), account(2), account(3)]
    service, _, _, clock, probe = harness(tmp_path, monkeypatch, rows)
    service.tick()
    clock.return_value = at(day=14)
    service.tick()
    assert probe.call_count == 2


@pytest.mark.parametrize("pct", [-1, 101, float("inf"), True])
def test_invalid_percentage_cannot_authorize_switch(pct):
    assert schedule.percentages(account(1, pct=pct)) is None


def test_expired_reset_with_pre_reset_sample_is_unusable():
    row = account(1, reset=schedule.iso(at(8)))
    assert schedule.percentages(row, at(9)) is None


def test_scoped_limits_only_bind_when_configured():
    row = account(1)
    row.usage.last_good["scoped"] = [{"name": "Fable", "pct": 100}]
    assert max(schedule.percentages(row)) == 20
    assert max(schedule.percentages(row, models=("Fable",))) == 100


def test_provider_governed_extended_trust_is_preserved():
    row = account(1, age=500)
    row.usage.decision_value = lambda: row.usage.last_good
    assert schedule.percentages(row) == [10,20]


def test_polling_explicitly_disables_inference_probes():
    settings = schedule.ScheduledSwitcher._usage_settings(None)
    assert not settings.header_probe
    assert not settings.fable_probe


def test_success_without_reset_reports_unconfirmed(tmp_path, monkeypatch):
    rows = [account(1, active=True), account(2), account(3)]
    probe = Mock(return_value={})
    service, _, _, _, _ = harness(tmp_path, monkeypatch, rows, probe=probe)
    service.tick()
    service.tick()
    assert probe.call_count == 1
    assert service.state["activations"]["2026-11-13:1"]["status"] == "request_accepted"
    assert service.last_event["kind"] == "request_accepted"


def test_changed_identity_is_not_published_as_available(tmp_path, monkeypatch):
    rows = [account(1, active=True), account(2), account(3)]
    service, _, _, _, _ = harness(tmp_path, monkeypatch, rows)
    service.config["identities"]["1"] = "different"
    service.tick()
    result = json.loads((tmp_path / "schedule-status.json").read_text())
    assert result["accounts"][0]["status"] == "identity_mismatch"


def test_last_five_seconds_of_old_window_never_count_as_idle(tmp_path, monkeypatch):
    rows = [account(1, active=True, reset=schedule.iso(at()+3)), account(2), account(3)]
    service, _, _, clock, probe = harness(tmp_path, monkeypatch, rows)
    service.tick()
    probe.assert_not_called()
    assert service.state["activations"]["2026-11-13:1"]["status"] == "waiting_reset"
    clock.return_value = at()+10
    rows[0].usage.last_good["five_hour"]["resets_at"] = None
    rows[0].usage.fetched_at = at()+10
    service.tick()
    probe.assert_called_once()


def test_explicit_model_missing_reading_is_not_available():
    assert schedule.percentages(account(1), models=("Opus",)) is None


def test_redirects_cannot_forward_oauth_token():
    handler = schedule.NoRedirect()
    assert handler.redirect_request(None,None,302,"Found",{},"https://other.invalid") is None


def test_install_preserves_different_existing_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(schedule.Path, "home", lambda: tmp_path)
    root = tmp_path / "backup"
    root.mkdir()
    (root / "schedule.json").write_text('{"enabled": true}')
    path = tmp_path / "Library/LaunchAgents/com.claude-swap.daily-schedule.plist"
    path.parent.mkdir(parents=True)
    original = schedule.plistlib.dumps({"Label": "different"})
    path.write_bytes(original)
    with pytest.raises(RuntimeError, match="not overwritten"):
        schedule.manage_launch_agent(root)
    assert path.read_bytes() == original


def test_uninstall_waits_for_async_domain_removal(tmp_path, monkeypatch):
    monkeypatch.setattr(schedule.Path, "home", lambda: tmp_path)
    run = Mock(side_effect=[SimpleNamespace(returncode=0), SimpleNamespace(returncode=0), SimpleNamespace(returncode=1)])
    monkeypatch.setattr(schedule.subprocess, "run", run)
    monkeypatch.setattr(schedule.time, "sleep", Mock())
    schedule.manage_launch_agent(tmp_path, uninstall=True)
    assert run.call_count == 3
