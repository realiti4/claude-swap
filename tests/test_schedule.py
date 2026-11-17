"""Deterministic daily-window, failover and secret-free snapshot checks."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock
import json
import urllib.error

import pytest

from claude_swap import schedule


def at(hour=7, minute=0, day=13):
    return datetime.fromisoformat(f"2026-11-{day:02}T{hour:02}:{minute:02}:00+08:00").timestamp()


def account(number, *, pct=0, weekly=20, active=False, age=0, reset=...,
            sentinel=None, disabled=False, weekly_reset=None):
    if reset is ...:
        reset = None if str(number) == "1" else schedule.iso(at(12))
    data = {"five_hour": {"pct": pct, "resets_at": reset},
            "seven_day": {"pct": weekly, "resets_at": weekly_reset}}
    usage = SimpleNamespace(age_s=age, sentinel=sentinel, last_good=data,
                            fetched_at=at()-age, decision_value=lambda: data if age <= 300 else None)
    return SimpleNamespace(number=str(number), email=f"person{number}@example.com", org_uuid="org",
                           usage=usage, is_active=active, switchable=True, disabled=disabled,
                           alias=f"alias{number}", subscription_plan=None)


def harness(tmp_path, monkeypatch, rows, now=None, probe=None):
    now = now or at()
    for row in rows:
        row.usage.fetched_at = now - row.usage.age_s
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


@pytest.mark.parametrize("hour,expected", [(0,[]),(6,[]),(7,["1","2","3"]),(11,["1","2","3"]),(12,["1","2","3"]),(16,["1","2","3"]),(17,["1","2","3"]),(21,["1","2","3"]),(22,["1","2","3"])])
def test_all_due_accounts_receive_inference_without_starting_future_accounts(tmp_path, monkeypatch, hour, expected):
    rows = [account(1, reset=None), account(2, active=True, reset=None), account(3, reset=None)]
    service, switcher, _, _, probe = harness(tmp_path, monkeypatch, rows, now=at(hour))
    service.tick()
    attempts = [key.split(":")[-1] for key, rec in service.state["activations"].items() if rec.get("attempted_at")]
    assert attempts == expected
    assert probe.call_count == len(expected)


def test_seven_am_activation_uses_each_accounts_own_credential(tmp_path, monkeypatch):
    rows = [account(1, active=True, reset=None), account(2, reset=None), account(3, reset=None)]
    service, switcher, _, _, probe = harness(tmp_path, monkeypatch, rows)
    credentials = []
    for row in switcher._build_accounts_info.return_value:
        item = list(row)
        item[5] = json.dumps({"claudeAiOauth": {"accessToken": f"TEST_TOKEN_{row[0]}"}})
        credentials.append(tuple(item))
    switcher._build_accounts_info.return_value = credentials
    service.tick()
    assert [call.args[0] for call in probe.call_args_list] == ["TEST_TOKEN_1", "TEST_TOKEN_2", "TEST_TOKEN_3"]
    switcher.switch_to.assert_not_called()
    assert all(record["attempted_at"] == schedule.iso(at()) for record in service.state["activations"].values())
    assert json.loads((tmp_path / "schedule-status.json").read_text())["scheduler"]["activation_start"] == "07:00"


@pytest.mark.parametrize("hour,current,target", [(7,3,"1"),(12,1,"2"),(17,2,"3")])
def test_account_selection_remains_independent_of_shared_activation_time(tmp_path, monkeypatch, hour, current, target):
    rows = [account(n, active=n == current, reset=None) for n in (1,2,3)]
    service, switcher, _, _, probe = harness(tmp_path, monkeypatch, rows, now=at(hour))
    service.tick()
    assert probe.call_count == 3
    switcher.switch_to.assert_called_once_with(target, json_output=True)


def test_morning_catch_up_only_starts_accounts_without_a_window(tmp_path, monkeypatch):
    rows = [account(1, active=True, reset=schedule.iso(at(12))),
            account(2, reset=None), account(3, reset=None)]
    service, switcher, _, _, probe = harness(tmp_path, monkeypatch, rows, now=at(10,21))
    service.tick()
    assert probe.call_count == 2
    assert service.state["activations"]["2026-11-13:1"]["status"] == "already_active"
    for number in ("2","3"):
        record = service.state["activations"][f"2026-11-13:{number}"]
        assert record["status"] == "request_accepted"
        assert record["late_seconds"] == at(10,21) - at(7)
    switcher.switch_to.assert_not_called()


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
    assert "SECRET" not in (tmp_path / "schedule-events.jsonl").read_text()


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
    endpoint_sample(rows[0], at(day=14))
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
    row = account(1, pct=10, age=500)
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


# A response header is a probe reading, not proof that the provider persisted
# a subscription window. These cases reproduce the 07:00 success / 10:36 idle
# contradiction without contacting an account or the provider.
def endpoint_sample(row, fetched_at, *, reset=None, pct=0, source=None, age=0):
    row.usage.fetched_at = fetched_at
    row.usage.age_s = age
    row.usage.last_good["five_hour"].update(pct=pct, resets_at=reset)
    row.usage.last_good.pop("source", None)
    if source is not None:
        row.usage.last_good["source"] = source


def test_reset_header_alone_never_confirms_persisted_window(tmp_path, monkeypatch):
    rows = [account(1, active=True), account(2), account(3)]
    service, _, _, _, probe = harness(tmp_path, monkeypatch, rows)
    service.tick()
    record = service.state["activations"]["2026-11-13:1"]
    assert probe.call_count == 1
    assert record["status"] == "request_accepted"
    assert record["attempt_count"] == 1


def test_fresh_endpoint_window_confirms_without_another_request(tmp_path, monkeypatch):
    rows = [account(1, active=True), account(2), account(3)]
    service, _, _, clock, probe = harness(tmp_path, monkeypatch, rows)
    service.tick()
    clock.return_value = at(7, 1)
    endpoint_sample(rows[0], at(7, 1), reset=schedule.iso(at(12)), pct=0.1)
    service.tick()
    record = service.state["activations"]["2026-11-13:1"]
    assert record["status"] == "activated"
    assert record["actual_resets_at"] == schedule.iso(at(12))
    assert probe.call_count == 1


@pytest.mark.parametrize("sample_kind", ["old", "headers", "stale", "sentinel", "same_time"])
def test_untrusted_sample_cannot_confirm_or_repeat_request(tmp_path, monkeypatch, sample_kind):
    rows = [account(1, active=True), account(2), account(3)]
    service, _, _, clock, probe = harness(tmp_path, monkeypatch, rows)
    service.tick()
    clock.return_value = at(7, 6)
    sample_time = at() if sample_kind in ("old", "same_time") else at(7, 6)
    endpoint_sample(rows[0], sample_time, reset=schedule.iso(at(12)),
                    source=schedule.oauth.USAGE_SOURCE_HEADERS if sample_kind == "headers" else None,
                    age=301 if sample_kind == "stale" else 0)
    if sample_kind == "sentinel":
        rows[0].usage.sentinel = "unavailable"
    service.tick()
    assert probe.call_count == 1
    assert service.state["activations"]["2026-11-13:1"]["status"] != "activated"


def test_accepted_request_waits_for_new_idle_endpoint_sample(tmp_path, monkeypatch):
    rows = [account(1, active=True, pct=0), account(2), account(3)]
    service, _, _, clock, probe = harness(tmp_path, monkeypatch, rows)
    service.tick()
    clock.return_value = at(7, 6)
    service.tick()
    assert probe.call_count == 1
    endpoint_sample(rows[0], at(7, 6))
    service.tick()
    assert probe.call_count == 2
    assert service.state["activations"]["2026-11-13:1"]["attempt_count"] == 2


def test_fresh_idle_sample_still_obeys_retry_delay(tmp_path, monkeypatch):
    rows = [account(1, active=True, pct=0), account(2), account(3)]
    service, _, _, clock, probe = harness(tmp_path, monkeypatch, rows)
    service.tick()
    clock.return_value = at(7, 1)
    endpoint_sample(rows[0], at(7, 1))
    service.tick()
    assert probe.call_count == 1


@pytest.mark.parametrize("old_status", ["activated", "request_accepted"])
def test_legacy_success_is_rechecked_and_retried_after_idle_proof(tmp_path, monkeypatch, old_status):
    rows = [account(1, active=True, pct=0), account(2), account(3)]
    (tmp_path / "schedule-state.json").write_text(json.dumps({"activations": {
        "2026-11-13:1": {"status": old_status, "attempted_at": schedule.iso(at()),
                            "completed_at": schedule.iso(at()),
                            "actual_resets_at": schedule.iso(at(12))}}}))
    endpoint_sample(rows[0], at(10, 36))
    service, _, _, _, probe = harness(tmp_path, monkeypatch, rows, now=at(10, 36))
    service.tick()
    assert probe.call_count == 1
    record = service.state["activations"]["2026-11-13:1"]
    assert record["attempt_count"] == 2
    assert record["status"] == "request_accepted"


def test_three_attempt_budget_survives_service_restart(tmp_path, monkeypatch):
    rows = [account(1, active=True, pct=0), account(2), account(3)]
    counts = []
    for minute in (0, 6, 12, 18, 24):
        now = at(7, minute)
        endpoint_sample(rows[0], now)
        service, _, _, _, probe = harness(tmp_path, monkeypatch, rows, now=now)
        service.tick()
        counts.append(probe.call_count)
    assert counts == [1, 1, 1, 0, 0]
    record = service.state["activations"]["2026-11-13:1"]
    assert record["attempt_count"] == 3
    assert record["status"] != "activated"


def test_rejected_quota_headers_cannot_report_accepted_request(tmp_path, monkeypatch):
    rows = [account(1, active=True), account(2), account(3)]
    probe = Mock(return_value={"five_hour": {"resets_at": schedule.iso(at(12))},
                               "rejected": {"resets_at": schedule.iso(at(12))}})
    service, _, _, _, _ = harness(tmp_path, monkeypatch, rows, probe=probe)
    service.tick()
    record = service.state["activations"]["2026-11-13:1"]
    assert record["status"] == "failed"
    assert record["error"]


def test_missing_unrelated_fable_reading_does_not_block_sonnet_activation(tmp_path, monkeypatch):
    rows = [account(1, active=True), account(2), account(3)]
    service, _, _, _, probe = harness(tmp_path, monkeypatch, rows)
    service.models = ("Fable",)
    service.tick()
    probe.assert_called_once()
    assert service.state["activations"]["2026-11-13:1"]["status"] == "request_accepted"


def test_date_is_rechecked_after_snapshot_work(tmp_path, monkeypatch):
    # The snapshot crosses midnight. Yesterday's slots must not be sent
    # using the timestamp from before the blocking snapshot.
    rows = [account(1, active=True), account(2), account(3)]
    service, _, _, clock, probe = harness(tmp_path, monkeypatch, rows, now=at(11, 59))
    snap = service.source.take.return_value
    def slow_snapshot(*args, **kwargs):
        clock.return_value = at(0, day=14)
        return snap
    service.source.take.side_effect = slow_snapshot
    service.tick()
    probe.assert_not_called()


def test_date_is_rechecked_after_credential_collection(tmp_path, monkeypatch):
    rows = [account(1, active=True), account(2), account(3)]
    service, switcher, _, clock, probe = harness(tmp_path, monkeypatch, rows, now=at(11, 59))
    credential_rows = switcher._build_accounts_info.return_value
    def slow_credentials():
        clock.return_value = at(0, day=14)
        return credential_rows
    switcher._build_accounts_info.side_effect = slow_credentials
    service.tick()
    probe.assert_not_called()


def message_response(monkeypatch, body):
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = body
    response.headers = {
        "anthropic-ratelimit-unified-5h-utilization": "0",
        "anthropic-ratelimit-unified-5h-reset": str(int(at(12))),
    }
    opener = Mock()
    opener.open.return_value = response
    monkeypatch.setattr(schedule.urllib.request, "build_opener", Mock(return_value=opener))
    return opener


def test_activation_request_accepts_real_message_shape(monkeypatch):
    opener = message_response(monkeypatch, json.dumps({
        "type": "message", "role": "assistant", "content": [{"type": "text", "text": "Hi"}],
        "usage": {"input_tokens": 10, "output_tokens": 1},
    }).encode())
    data = schedule.activation_request("FAKE_TEST_TOKEN")
    assert data["five_hour"]["resets_at"] == schedule.iso(at(12))
    request = opener.open.call_args.args[0]
    assert request.full_url == schedule.oauth.USAGE_PROBE_URL
    body = json.loads(request.data)
    assert body["model"] == "claude-sonnet-5-5"
    assert body["max_tokens"] == 16
    assert body["messages"] == [{"role": "user", "content": "Reply with OK."}]


@pytest.mark.parametrize("body", [
    b"not json", b"{}", b"[]", b'{"type":"error"}', b" " * 65537,
    json.dumps({"type": "message", "role": "assistant", "content": [],
                "usage": {"input_tokens": 10, "output_tokens": 1}}).encode(),
    json.dumps({"type": "message", "role": "assistant", "content": [{"type":"text", "text":"Hi"}],
                "usage": {"input_tokens": 10, "output_tokens": 0}}).encode(),
    json.dumps({"type": "message", "role": "user", "content": [{"type":"text", "text":"Hi"}],
                "usage": {"input_tokens": 10, "output_tokens": 1}}).encode(),
])
def test_invalid_message_body_never_proves_request_execution(monkeypatch, body):
    message_response(monkeypatch, body)
    with pytest.raises(ValueError):
        schedule.activation_request("FAKE_TEST_TOKEN")


@pytest.mark.parametrize("status", ["request_accepted", "activated"])
def test_pre_completion_snapshot_cannot_confirm_or_restart(tmp_path, monkeypatch, status):
    rows = [account(1, active=True), account(2), account(3)]
    service, _, _, clock, probe = harness(tmp_path, monkeypatch, rows)
    clock.return_value = at() + 0.25
    def slow_probe(_):
        clock.return_value = at() + 0.75
        return {"five_hour": {"resets_at": schedule.iso(at(12))}}
    probe.side_effect = slow_probe
    service.tick()
    record = service.state["activations"]["2026-11-13:1"]
    record["status"] = status
    clock.return_value = at(7, 5)
    # This reading arrived during the request, within the same ISO second.
    endpoint_sample(rows[0], at() + 0.5, reset=schedule.iso(at(12)))
    service.tick()
    assert probe.call_count == 1
    assert "confirmed_at" not in record


def test_credential_identity_change_cannot_receive_request(tmp_path, monkeypatch):
    rows = [account(1, active=True), account(2), account(3)]
    service, switcher, _, _, probe = harness(tmp_path, monkeypatch, rows)
    credential_rows = switcher._build_accounts_info.return_value
    changed = list(credential_rows[0])
    changed[1] = "replacement@example.com"
    switcher._build_accounts_info.return_value = [tuple(changed), *credential_rows[1:]]
    service.tick()
    probe.assert_not_called()
    assert service.state["activations"]["2026-11-13:1"]["attempt_count"] == 0


def test_sonnet_scoped_exhaustion_prevents_sonnet_activation(tmp_path, monkeypatch):
    rows = [account(1, active=True), account(2), account(3)]
    rows[0].usage.last_good["scoped"] = [{"name": "Sonnet", "pct": 99}]
    service, _, _, _, probe = harness(tmp_path, monkeypatch, rows)
    service.tick()
    probe.assert_not_called()


def test_ambiguous_failures_share_persistent_three_attempt_budget(tmp_path, monkeypatch):
    rows = [account(1, active=True), account(2), account(3)]
    attempts = []
    for minute in (0, 6, 12, 18):
        probe = Mock(side_effect=TimeoutError("FAKE_TEST_TOKEN"))
        service, _, _, _, _ = harness(tmp_path, monkeypatch, rows, now=at(7, minute), probe=probe)
        service.tick()
        attempts.append(probe.call_count)
    assert attempts == [1, 1, 1, 0]
    assert service.state["activations"]["2026-11-13:1"]["attempt_count"] == 3


def test_endpoint_can_confirm_after_target_slot_ends(tmp_path, monkeypatch):
    rows = [account(1, active=True), account(2, disabled=True), account(3, disabled=True)]
    service, _, _, clock, probe = harness(tmp_path, monkeypatch, rows, now=at(11, 59))
    service.tick()
    clock.return_value = at(12, 1)
    endpoint_sample(rows[0], at(12, 1), reset=schedule.iso(at(16, 59)), pct=0.1)
    service.tick()
    record = service.state["activations"]["2026-11-13:1"]
    assert record["status"] == "activated"
    assert record["actual_resets_at"] == schedule.iso(at(16, 59))
    assert probe.call_count == 1


def test_false_legacy_success_is_retried_after_original_slot_ends(tmp_path, monkeypatch):
    rows = [account(1, active=True), account(2, disabled=True), account(3, disabled=True)]
    (tmp_path / "schedule-state.json").write_text(json.dumps({"activations": {
        "2026-11-13:1": {"status": "activated", "attempted_at": schedule.iso(at()),
                            "completed_at": schedule.iso(at()),
                            "actual_resets_at": schedule.iso(at(12))}}}))
    service, _, _, _, probe = harness(tmp_path, monkeypatch, rows, now=at(12, 1))
    service.tick()
    assert service.state["activations"]["2026-11-13:1"]["status"] == "request_accepted"
    probe.assert_called_once()


def test_confirmed_window_expiry_starts_next_cycle_and_preserves_history(tmp_path, monkeypatch):
    rows = [account(1, active=True), account(2, disabled=True), account(3, disabled=True)]
    service, _, _, clock, probe = harness(tmp_path, monkeypatch, rows)
    service.tick()
    clock.return_value = at(7, 1)
    endpoint_sample(rows[0], at(7, 1), reset=schedule.iso(at(12)), pct=0.1)
    service.tick()
    record = service.state["activations"]["2026-11-13:1"]
    assert record["status"] == "activated"
    clock.return_value = at(12, 1)
    endpoint_sample(rows[0], at(12, 1))
    service.tick()
    assert record["status"] == "request_accepted"
    assert record["actual_resets_at"] is None
    assert probe.call_count == 2
    assert record["cycle_number"] == 2
    assert record["cycle_attempt_count"] == 1
    assert record["completed_cycles"][0]["actual_resets_at"] == schedule.iso(at(12))


def test_legacy_matching_reset_gets_endpoint_confirmation_marker(tmp_path, monkeypatch):
    rows = [account(1, active=True, pct=0.1, reset=schedule.iso(at(12))),
            account(2), account(3)]
    (tmp_path / "schedule-state.json").write_text(json.dumps({"activations": {
        "2026-11-13:1": {"status": "activated", "attempted_at": schedule.iso(at()),
                            "completed_at": schedule.iso(at()),
                            "actual_resets_at": schedule.iso(at(12))}}}))
    service, _, _, _, probe = harness(tmp_path, monkeypatch, rows, now=at(7, 1))
    service.tick()
    record = service.state["activations"]["2026-11-13:1"]
    assert record["status"] == "activated"
    assert record["confirmed_sample_at"] == schedule.iso(at(7, 1))
    probe.assert_not_called()


def test_confirmed_window_expiring_inside_slot_starts_second_window(tmp_path, monkeypatch):
    rows = [account(1, active=True), account(2), account(3)]
    service, _, _, clock, probe = harness(tmp_path, monkeypatch, rows)
    service.tick()
    clock.return_value = at(7, 1)
    endpoint_sample(rows[0], at(7, 1), reset=schedule.iso(at(11, 59)), pct=0.1)
    service.tick()
    record = service.state["activations"]["2026-11-13:1"]
    assert record["status"] == "activated"
    clock.return_value = at(11, 59) + 30
    endpoint_sample(rows[0], clock.return_value)
    service.tick()
    assert record["status"] == "request_accepted"
    assert record["cycle_number"] == 2
    assert record["late_seconds"] == 30
    assert probe.call_count == 2


def test_catch_up_does_not_switch_back_to_missed_account_or_repeat_on_restart(tmp_path, monkeypatch):
    rows = [account(1), account(2, active=True, reset=None), account(3, disabled=True)]
    service, switcher, _, _, probe = harness(tmp_path, monkeypatch, rows, now=at(13))
    service.tick()
    assert probe.call_count == 2
    assert service.state["activations"]["2026-11-13:1"]["late_seconds"] == 6 * 3600
    switcher.switch_to.assert_not_called()
    restarted, _, _, _, probe2 = harness(tmp_path, monkeypatch, rows, now=at(13))
    restarted.tick()
    probe2.assert_not_called()


def test_crossing_slot_end_during_credentials_still_sends_catch_up(tmp_path, monkeypatch):
    rows = [account(1, active=True), account(2, disabled=True), account(3, disabled=True)]
    service, switcher, _, clock, probe = harness(tmp_path, monkeypatch, rows, now=at(11, 59))
    credential_rows = switcher._build_accounts_info.return_value
    def slow_credentials():
        clock.return_value = at(12)
        return credential_rows
    switcher._build_accounts_info.side_effect = slow_credentials
    service.tick()
    probe.assert_called_once()
    assert service.state["activations"]["2026-11-13:1"]["attempted_at"] == schedule.iso(at(12))


def test_failed_catch_up_does_not_block_other_due_accounts(tmp_path, monkeypatch):
    rows = [account(1), account(2, active=True, reset=None), account(3, disabled=True)]
    probe = Mock(side_effect=[TimeoutError("FAKE_TOKEN"), {}])
    service, _, _, _, _ = harness(tmp_path, monkeypatch, rows, now=at(13), probe=probe)
    service.tick()
    assert probe.call_count == 2
    assert service.state["activations"]["2026-11-13:1"]["status"] == "failed"
    assert service.state["activations"]["2026-11-13:2"]["status"] == "request_accepted"
    assert "2026-11-13:3" not in service.state["activations"]


def test_already_active_window_is_renewed_after_expiry(tmp_path, monkeypatch):
    rows = [account(1, active=True, reset=schedule.iso(at(12))),
            account(2, disabled=True), account(3, disabled=True)]
    service, _, _, clock, probe = harness(tmp_path, monkeypatch, rows)
    service.tick()
    assert service.state["activations"]["2026-11-13:1"]["status"] == "already_active"
    clock.return_value = at(13)
    endpoint_sample(rows[0], at(13))
    service.tick()
    probe.assert_called_once()
    assert service.state["activations"]["2026-11-13:1"]["status"] == "request_accepted"


def test_four_successful_cycles_are_not_limited_by_daily_attempt_count(tmp_path, monkeypatch):
    rows = [account(1, active=True), account(2, disabled=True), account(3, disabled=True)]
    service, _, _, clock, probe = harness(tmp_path, monkeypatch, rows)
    for hour in (7, 12, 17, 22):
        clock.return_value = at(hour) + 1
        endpoint_sample(rows[0], clock.return_value)
        service.tick()
        clock.return_value = at(hour, 1)
        endpoint_sample(rows[0], at(hour, 1), reset=schedule.iso(at(hour) + 18000), pct=0.1)
        service.tick()
    record = service.state["activations"]["2026-11-13:1"]
    assert probe.call_count == 4
    assert record["attempt_count"] == 4
    assert record["cycle_attempt_count"] == 1
    assert record["cycle_number"] == 4
    assert len(record["completed_cycles"]) == 3
    assert record["status"] == "activated"


def test_new_cycle_retry_budget_survives_restart(tmp_path, monkeypatch):
    rows = [account(1, active=True), account(2, disabled=True), account(3, disabled=True)]
    service, _, _, clock, probe = harness(tmp_path, monkeypatch, rows)
    service.tick()
    clock.return_value = at(7, 1)
    endpoint_sample(rows[0], at(7, 1), reset=schedule.iso(at(12)), pct=0.1)
    service.tick()
    counts = []
    for minute in (0, 6, 12, 18):
        endpoint_sample(rows[0], at(12, minute) + 1)
        service, _, _, _, retry = harness(tmp_path, monkeypatch, rows, now=at(12, minute) + 1,
                                        probe=Mock(side_effect=TimeoutError("FAKE_TOKEN")))
        service.tick()
        counts.append(retry.call_count)
    assert counts == [1, 1, 1, 0]
    record = service.state["activations"]["2026-11-13:1"]
    assert record["attempt_count"] == 4
    assert record["cycle_attempt_count"] == 3
    assert record["cycle_number"] == 2
    assert record["error"] == "window_attempt_limit"


@pytest.mark.parametrize("kind", ["pre_reset", "stale", "headers", "unknown"])
def test_expiry_needs_fresh_idle_proof_before_renewal(tmp_path, monkeypatch, kind):
    rows = [account(1, active=True), account(2, disabled=True), account(3, disabled=True)]
    service, _, _, clock, probe = harness(tmp_path, monkeypatch, rows)
    service.tick()
    clock.return_value = at(7, 1)
    endpoint_sample(rows[0], at(7, 1), reset=schedule.iso(at(12)), pct=0.1)
    service.tick()
    clock.return_value = at(12, 1)
    endpoint_sample(rows[0], at(11, 59) if kind == "pre_reset" else at(12, 1),
                    source=schedule.oauth.USAGE_SOURCE_HEADERS if kind == "headers" else None,
                    age=301 if kind == "stale" else 0,
                    pct=None if kind == "unknown" else 0)
    service.tick()
    assert probe.call_count == 1
    assert service.state["activations"]["2026-11-13:1"]["cycle_number"] == 1


def test_external_next_window_is_adopted_without_extra_inference(tmp_path, monkeypatch):
    rows = [account(1, active=True, reset=schedule.iso(at(12))),
            account(2, disabled=True), account(3, disabled=True)]
    service, _, _, clock, probe = harness(tmp_path, monkeypatch, rows)
    service.tick()
    clock.return_value = at(13)
    endpoint_sample(rows[0], at(13), reset=schedule.iso(at(17)), pct=5)
    service.tick()
    probe.assert_not_called()
    record = service.state["activations"]["2026-11-13:1"]
    assert record["cycle_number"] == 2
    assert record["actual_resets_at"] == schedule.iso(at(17))
    assert record["status"] == "already_active"
    clock.return_value = at(17, 1)
    endpoint_sample(rows[0], at(17, 1))
    service.tick()
    probe.assert_called_once()
    assert record["cycle_number"] == 3


def test_renewal_stops_at_midnight_and_resumes_at_next_daily_start(tmp_path, monkeypatch):
    rows = [account(1, active=True), account(2, disabled=True), account(3, disabled=True)]
    service, _, _, clock, probe = harness(tmp_path, monkeypatch, rows, now=at(22))
    service.tick()
    clock.return_value = at(22, 1)
    endpoint_sample(rows[0], at(22, 1), reset=schedule.iso(at(0, day=14)), pct=0.1)
    service.tick()
    for hour in (0, 6):
        clock.return_value = at(hour, day=14)
        endpoint_sample(rows[0], clock.return_value)
        service.tick()
    assert probe.call_count == 1
    assert "2026-11-14:1" not in service.state["activations"]
    clock.return_value = at(7, day=14)
    endpoint_sample(rows[0], clock.return_value)
    service.tick()
    assert probe.call_count == 2
    assert service.state["activations"]["2026-11-14:1"]["cycle_number"] == 1


def test_adopted_window_exhaustion_keeps_confirmation_for_renewal(tmp_path, monkeypatch):
    rows = [account(1, active=True, reset=schedule.iso(at(12))),
            account(2, disabled=True), account(3, disabled=True)]
    service, _, _, clock, probe = harness(tmp_path, monkeypatch, rows)
    service.tick()
    clock.return_value = at(13)
    endpoint_sample(rows[0], at(13), reset=schedule.iso(at(17)), pct=5)
    service.tick()
    clock.return_value = at(13, 1)
    endpoint_sample(rows[0], at(13, 1), reset=schedule.iso(at(17)), pct=100)
    service.tick()
    record = service.state["activations"]["2026-11-13:1"]
    assert record["status"] == "already_active"
    probe.assert_not_called()
    clock.return_value = at(17, 1)
    endpoint_sample(rows[0], at(17, 1))
    service.tick()
    probe.assert_called_once()
    assert record["cycle_number"] == 3
    assert record["cycle_attempt_count"] == 1
    assert len(record["completed_cycles"]) == 2


def test_attempt_limit_with_obsolete_confirmation_cannot_reset_budget(tmp_path, monkeypatch):
    rows = [account(1, active=True), account(2, disabled=True), account(3, disabled=True)]
    (tmp_path / "schedule-state.json").write_text(json.dumps({"activations": {
        "2026-11-13:1": {"status": "attempt_limit", "attempt_count": 3,
                            "cycle_number": 1, "cycle_attempt_count": 3,
                            "attempted_at": schedule.iso(at(7, 12)),
                            "completed_at": schedule.iso(at(7, 12)),
                            "actual_resets_at": schedule.iso(at(12)),
                            "confirmed_sample_at": schedule.iso(at(7, 1))}}}))
    service, _, _, _, probe = harness(tmp_path, monkeypatch, rows, now=at(12, 1))
    service.tick()
    probe.assert_not_called()
    record = service.state["activations"]["2026-11-13:1"]
    assert record["cycle_number"] == 1
    assert record["cycle_attempt_count"] == 3


@pytest.mark.parametrize("hour", [7,12,17,23])
def test_weekly_expiry_overrides_selection_slots_without_extra_inference(tmp_path, monkeypatch, hour):
    rows = [account(1, active=True, weekly_reset=schedule.iso(at(day=19)), reset=schedule.iso(at(day=14))),
            account(2, weekly=80, weekly_reset=schedule.iso(at(day=14)), reset=schedule.iso(at(day=14))),
            account(3, weekly_reset=schedule.iso(at(day=16)), reset=schedule.iso(at(day=14)))]
    service, switcher, _, _, probe = harness(tmp_path, monkeypatch, rows, now=at(hour))
    service.tick()
    switcher.switch_to.assert_called_once_with("2", json_output=True)
    probe.assert_not_called()
    saved = json.loads((tmp_path / "schedule-status.json").read_text())
    assert saved["scheduler"]["selection"]["preferred_account_id"] == "2"
    assert saved["scheduler"]["selection"]["ranked_account_ids"] == ["2","3","1"]


def test_weekly_priority_keeps_earliest_account_at_selection_boundary(tmp_path, monkeypatch):
    rows = [account(1, active=True, weekly=98, weekly_reset=schedule.iso(at(day=14))),
            account(2, weekly=10, weekly_reset=schedule.iso(at(day=18))),
            account(3, weekly_reset=schedule.iso(at(day=19)))]
    service, switcher, _, _, _ = harness(tmp_path, monkeypatch, rows, now=at(12))
    service.select_account(service.source.take())
    switcher.switch_to.assert_not_called()
    assert service.selection["reason"] == "current_earliest_group"


@pytest.mark.parametrize("minutes", [0,1,29,30])
def test_near_equal_weekly_deadlines_keep_current_account(tmp_path, monkeypatch, minutes):
    rows = [account(1, active=True, weekly=90, weekly_reset=schedule.iso(at(day=14)+minutes*60)),
            account(2, weekly=10, weekly_reset=schedule.iso(at(day=14))), account(3)]
    service, switcher, _, _, _ = harness(tmp_path, monkeypatch, rows)
    service.select_account(service.source.take())
    switcher.switch_to.assert_not_called()
    assert service.selection["preferred_account_id"] == "1"


def test_equivalent_deadlines_choose_more_weekly_quota_on_failover(tmp_path, monkeypatch):
    rows = [account(1, active=True, pct=99),
            account(2, weekly=80, weekly_reset=schedule.iso(at(day=14))),
            account(3, weekly=30, weekly_reset=schedule.iso(at(day=14)+60))]
    service, switcher, _, _, _ = harness(tmp_path, monkeypatch, rows)
    service.select_account(service.source.take())
    switcher.switch_to.assert_called_once_with("3", json_output=True)


def test_failover_prefers_earliest_weekly_reset_over_largest_quota(tmp_path, monkeypatch):
    rows = [account(1, active=True, pct=99),
            account(2, weekly=10, weekly_reset=schedule.iso(at(day=18))),
            account(3, weekly=98, weekly_reset=schedule.iso(at(day=14)))]
    service, switcher, _, _, _ = harness(tmp_path, monkeypatch, rows)
    service.select_account(service.source.take())
    switcher.switch_to.assert_called_once_with("3", json_output=True)
    assert "7d reset" in service.last_event["detail"]


@pytest.mark.parametrize("bad", [dict(pct=99),dict(weekly=99),dict(age=301),dict(disabled=True),
                                  dict(sentinel="token expired")])
def test_unavailable_earliest_weekly_account_is_skipped(tmp_path, monkeypatch, bad):
    rows = [account(1, active=True, weekly_reset=schedule.iso(at(day=19))),
            account(2, weekly_reset=schedule.iso(at(day=14)), **bad),
            account(3, weekly_reset=schedule.iso(at(day=16)))]
    service, switcher, _, _, _ = harness(tmp_path, monkeypatch, rows)
    service.select_account(service.source.take())
    switcher.switch_to.assert_called_once_with("3", json_output=True)


def test_scoped_exhaustion_excludes_earliest_weekly_account(tmp_path, monkeypatch):
    rows = [account(1, active=True, weekly_reset=schedule.iso(at(day=19))),
            account(2, weekly_reset=schedule.iso(at(day=14))),
            account(3, weekly_reset=schedule.iso(at(day=16)))]
    for row, pct in zip(rows, (10,99,20)):
        row.usage.last_good["scoped"] = [{"name":"Sonnet", "pct":pct}]
    service, switcher, _, _, _ = harness(tmp_path, monkeypatch, rows)
    service.models = ("Sonnet",)
    service.select_account(service.source.take())
    switcher.switch_to.assert_called_once_with("3", json_output=True)


@pytest.mark.parametrize("reset", [None,"invalid",schedule.iso(at(6))])
def test_unknown_or_expired_weekly_deadline_does_not_outrank_known(tmp_path, monkeypatch, reset):
    rows = [account(1, active=True, weekly_reset=schedule.iso(at(day=19))),
            account(2, weekly_reset=reset), account(3, weekly_reset=schedule.iso(at(day=16)))]
    service, switcher, _, _, _ = harness(tmp_path, monkeypatch, rows)
    service.select_account(service.source.take())
    switcher.switch_to.assert_called_once_with("3", json_output=True)


def test_old_pre_reset_sample_cannot_be_selected_as_earliest(tmp_path, monkeypatch):
    rows = [account(1, active=True, weekly_reset=schedule.iso(at(day=19))),
            account(2, weekly_reset=schedule.iso(at(7,1))),
            account(3, weekly_reset=schedule.iso(at(day=16)))]
    service, switcher, _, _, _ = harness(tmp_path, monkeypatch, rows, now=at(7,2))
    rows[1].usage.fetched_at = at()
    service.select_account(service.source.take())
    switcher.switch_to.assert_called_once_with("3", json_output=True)


def test_weekly_priority_does_not_use_replaced_identity(tmp_path, monkeypatch):
    rows = [account(1, active=True, weekly_reset=schedule.iso(at(day=19))),
            account(2, weekly_reset=schedule.iso(at(day=14))),
            account(3, weekly_reset=schedule.iso(at(day=16)))]
    service, switcher, _, _, _ = harness(tmp_path, monkeypatch, rows)
    service.config["identities"]["2"] = "replacement"
    service.select_account(service.source.take())
    switcher.switch_to.assert_called_once_with("3", json_output=True)


def test_weekly_selection_skips_refused_credential(tmp_path, monkeypatch):
    rows = [account(1, active=True, weekly_reset=schedule.iso(at(day=19))),
            account(2, weekly_reset=schedule.iso(at(day=14))),
            account(3, weekly_reset=schedule.iso(at(day=16)))]
    service, switcher, freshener, _, _ = harness(tmp_path, monkeypatch, rows)
    freshener._freshen_target.side_effect = ["skip-live-session","ok"]
    service.select_account(service.source.take())
    switcher.switch_to.assert_called_once_with("3", json_output=True)


def test_blocked_priority_never_falls_back_to_later_weekly_account(tmp_path, monkeypatch):
    rows = [account(1, active=True, weekly_reset=schedule.iso(at(day=16))),
            account(2, weekly_reset=schedule.iso(at(day=14))),
            account(3, weekly_reset=schedule.iso(at(day=19)))]
    service, switcher, freshener, _, _ = harness(tmp_path, monkeypatch, rows)
    freshener._freshen_target.return_value = "skip-live-session"
    service.select_account(service.source.take())
    switcher.switch_to.assert_not_called()
    freshener._freshen_target.assert_called_once_with("2",rows[1].email)
    assert service.selection["reason"] == "weekly_selection_blocked"


def test_weekly_selection_cooldown_survives_restart(tmp_path, monkeypatch):
    rows = [account(1, active=True, weekly_reset=schedule.iso(at(day=19))),
            account(2, weekly_reset=schedule.iso(at(day=14))),
            account(3, weekly_reset=schedule.iso(at(day=16)))]
    first, switcher, _, _, _ = harness(tmp_path, monkeypatch, rows)
    first.select_account(first.source.take())
    switcher.switch_to.assert_called_once_with("2",json_output=True)
    rows[0].is_active = False
    rows[1].is_active = True
    rows[1].usage.last_good["seven_day"]["resets_at"] = schedule.iso(at(day=20))
    second, switcher, _, clock, _ = harness(tmp_path, monkeypatch, rows, now=at(7,9))
    second.select_account(second.source.take())
    switcher.switch_to.assert_not_called()
    assert second.selection["reason"] == "selection_cooldown"
    clock.return_value = at(7,10)
    second.select_account(second.source.take())
    switcher.switch_to.assert_called_once_with("3",json_output=True)


@pytest.mark.parametrize("field", ["five_hour","seven_day"])
def test_exhaustion_bypasses_weekly_selection_cooldown(tmp_path, monkeypatch, field):
    rows = [account(1, weekly_reset=schedule.iso(at(day=19))),
            account(2, active=True, weekly_reset=schedule.iso(at(day=14))),
            account(3, weekly_reset=schedule.iso(at(day=16)))]
    service, switcher, _, _, _ = harness(tmp_path, monkeypatch, rows, now=at(7,1))
    service.state.update(selection_active_account="2",selection_changed_at=schedule.iso(at()))
    rows[1].usage.last_good[field]["pct"] = 99
    service.select_account(service.source.take())
    switcher.switch_to.assert_called_once_with("3",json_output=True)


def test_earliest_account_returns_after_five_hour_quota_recovers(tmp_path, monkeypatch):
    rows = [account(1, active=True, weekly_reset=schedule.iso(at(day=19))),
            account(2, pct=99, weekly_reset=schedule.iso(at(day=14))),
            account(3, weekly_reset=schedule.iso(at(day=16)))]
    service, switcher, _, clock, _ = harness(tmp_path, monkeypatch, rows)
    snap = service.source.take()
    service.select_account(snap)
    switcher.switch_to.assert_called_once_with("3",json_output=True)
    rows[0].is_active = False
    rows[2].is_active = True
    snap.active_number = "3"
    rows[1].usage.last_good["five_hour"]["pct"] = 0
    clock.return_value = at(7,11)
    service.select_account(snap)
    assert switcher.switch_to.call_args.args == ("2",)


def test_manual_account_change_gets_persisted_ten_minute_hold(tmp_path, monkeypatch):
    rows = [account(1, active=True, weekly_reset=schedule.iso(at(day=19))),
            account(2, weekly_reset=schedule.iso(at(day=14))),account(3)]
    service, switcher, _, clock, _ = harness(tmp_path, monkeypatch, rows)
    service.state.update(selection_active_account="2",selection_changed_at=schedule.iso(at(6)))
    service.select_account(service.source.take())
    switcher.switch_to.assert_not_called()
    assert service.selection["hold_until"] == schedule.iso(at(7,10))
    clock.return_value = at(7,10)
    service.select_account(service.source.take())
    switcher.switch_to.assert_called_once_with("2",json_output=True)


def test_new_weekly_cycle_moves_priority_to_next_deadline(tmp_path, monkeypatch):
    rows = [account(1, active=True, weekly_reset=schedule.iso(at(8))),
            account(2, weekly_reset=schedule.iso(at(day=14))),account(3)]
    service, switcher, _, clock, _ = harness(tmp_path, monkeypatch, rows)
    service.select_account(service.source.take())
    switcher.switch_to.assert_not_called()
    clock.return_value = at(8,1)
    rows[0].usage.fetched_at = at(8,1)
    rows[0].usage.last_good["seven_day"] = {"pct":0,"resets_at":schedule.iso(at(8,day=20))}
    service.select_account(service.source.take())
    switcher.switch_to.assert_called_once_with("2",json_output=True)


def test_priority_chooses_more_quota_inside_earliest_group(tmp_path, monkeypatch):
    rows = [account(1, active=True, weekly_reset=schedule.iso(at(day=14)+31*60)),
            account(2, weekly=80, weekly_reset=schedule.iso(at(day=14))),
            account(3, weekly=20, weekly_reset=schedule.iso(at(day=14)+29*60))]
    service, switcher, _, _, _ = harness(tmp_path, monkeypatch, rows)
    service.select_account(service.source.take())
    switcher.switch_to.assert_called_once_with("3",json_output=True)
    assert service.selection["last_changed_at"] == schedule.iso(at())
    assert service.selection["hold_until"] == schedule.iso(at(7,10))


def test_credential_refresh_crossing_weekly_reset_rejects_old_sample(tmp_path, monkeypatch):
    rows = [account(1, active=True, weekly_reset=schedule.iso(at(day=19))),
            account(2, weekly_reset=schedule.iso(at(7,1))),
            account(3, weekly_reset=schedule.iso(at(day=16)))]
    service, switcher, freshener, clock, _ = harness(tmp_path, monkeypatch, rows)
    def refresh(*_):
        clock.return_value = at(7,2)
        return "ok"
    freshener._freshen_target.side_effect = refresh
    service.select_account(service.source.take())
    switcher.switch_to.assert_called_once_with("3",json_output=True)
