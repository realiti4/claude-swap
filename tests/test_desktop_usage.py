import json
from datetime import datetime, timezone

import pytest

from claude_swap import desktop_usage as du
from claude_swap.desktop_usage_html import payload, render


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "backup"
    (root / "cache").mkdir(parents=True)
    monkeypatch.setattr(du.paths, "get_backup_root", lambda: root)
    return root


def _cache(root, accounts):
    (root / "cache" / "usage.json").write_text(json.dumps({"accounts": accounts}))


def _account(email, fetched, week, fable=0.0, five=None):
    good = {"seven_day": {"pct": week, "resets_at": "2026-11-07T18:00:00.524+00:00"},
            "scoped": [{"name": "Fable", "pct": fable, "resets_at": "2026-11-07T18:00:00+00:00"}]}
    if five is not None:
        good["five_hour"] = {"pct": five, "resets_at": "2026-11-02T14:20:00+00:00"}
    return {"email": email, "fetchedAt": fetched, "lastGood": good}


def test_sampler_records_each_new_reading_once(home):
    _cache(home, {"1": _account("a@x.com", 1_793_600_000.0, 10.0, five=3.0)})
    assert du.sample_usage() == 1
    assert du.sample_usage() == 0  # same reading
    _cache(home, {"1": _account("a@x.com", 1_793_600_300.0, 12.0)})
    assert du.sample_usage() == 1
    rows = du.read_jsonl(du.samples_path())
    assert [r["seven_day"]["pct"] for r in rows] == [10.0, 12.0]
    assert rows[0]["five_hour"]["pct"] == 3.0 and rows[0]["scoped"]["Fable"]["pct"] == 0.0


def test_sampler_survives_a_missing_cache(home):
    assert du.sample_usage() == 0


def _transcript(root, lines):
    folder = root / "proj"
    folder.mkdir(parents=True)
    (folder / "s.jsonl").write_text("\n".join(json.dumps(x) for x in lines))


def _line(rid, out, model="claude-sonnet-5-5", cwd="C:/p/app", entry="claude-desktop"):
    return {"requestId": rid, "cwd": cwd, "sessionId": "s1", "entrypoint": entry, "isSidechain": False,
            "message": {"model": model, "usage": {"input_tokens": 2, "output_tokens": out, "cache_read_input_tokens": 1000,
                                                  "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 50}}}}


def test_transcripts_keep_the_fullest_line_per_request(tmp_path):
    _transcript(tmp_path, [_line("req_1", 0), _line("req_1", 700), _line("req_2", 5)])
    found = du.read_transcripts(tmp_path)
    assert found["req_1"]["tokens"] == {"input": 2, "output": 700, "cache_read": 1000, "cache_5m": 0, "cache_1h": 50}
    assert found["req_1"]["model"] == "claude-sonnet-5-5" and found["req_1"]["cwd"] == "C:/p/app"


def test_requests_join_the_ledger_to_transcripts_by_request_id():
    ledger = [{"ts": "2026-11-02T11:00:00Z", "account": "a@x.com", "request_id": "req_1", "status": 200, "entrypoint": "x"},
              {"ts": "2026-11-02T11:00:05Z", "account": "a@x.com", "request_id": "req_9", "status": 200, "entrypoint": "agent-sdk"},
              {"ts": "2026-11-02T11:00:00Z", "account": "a@x.com", "request_id": "req_1", "status": 200, "entrypoint": "x"}]
    reqs = du.load_requests(ledger, {"req_1": {"tokens": {**dict.fromkeys(du.KINDS, 0), "output": 10}, "model": "m", "cwd": "c", "session": "s", "entrypoint": "claude-desktop", "sidechain": False}})
    assert [r.request_id for r in reqs] == ["req_1", "req_9"]  # the duplicate id is one request
    assert reqs[0].counted and reqs[0].weighted == 50 and not reqs[1].counted


def _sample(ts, pct, fable=0.0):
    return du.Sample(datetime.fromisoformat(ts), None, {"pct": pct, "resets_at": "2026-11-07T18:00:00.524+00:00"},
                     {"Fable": {"pct": fable, "resets_at": "2026-11-07T18:00:00+00:00"}}, None)


def _request(ts, out, model="claude-sonnet-5-5"):
    return du.Request(datetime.fromisoformat(ts), "a@x.com", ts, 200, model=model,
                      tokens={"input": 0, "output": out, "cache_read": 0, "cache_5m": 0, "cache_1h": 0}, counted=True)


def test_tokens_per_percent_uses_the_interval_the_percentage_covers():
    t = "2026-11-03T"
    samples = {"a@x.com": [_sample(t + "10:00:00+00:00", 20.0), _sample(t + "12:00:00+00:00", 24.0)]}
    requests = [_request(t + "09:00:00+00:00", 999_999),  # before the first reading: not counted
                _request(t + "10:30:00+00:00", 1000), _request(t + "11:00:00+00:00", 1000)]
    report = du.build_report(requests, samples, datetime(2026, 11, 3, 13, tzinfo=timezone.utc))
    (week,) = report.weeks
    assert week.week_pct() == (20.0, 24.0, 4.0)
    seen = week.observed()
    assert week.totals(seen)["weighted"] == 10_000  # 2000 output x 5
    assert week.per_pct(seen, 4.0) == 2500
    assert week.per_pct(seen, 0.0) is None  # no movement yet
    assert week.end == datetime(2026, 11, 7, 18, tzinfo=timezone.utc)  # snapped to the reset


def test_fable_has_its_own_percentage_and_tokens():
    t = "2026-11-03T"
    samples = {"a@x.com": [_sample(t + "10:00:00+00:00", 20.0, 0.0), _sample(t + "12:00:00+00:00", 21.0, 2.0)]}
    requests = [_request(t + "11:00:00+00:00", 100, "claude-fable-5-1"), _request(t + "11:30:00+00:00", 100)]
    (week,) = du.build_report(requests, samples).weeks
    assert week.scoped_pct("Fable")[2] == 2.0
    fable = [r for r in week.observed() if "fable" in r.model]
    assert week.per_pct(fable, 2.0) == 250


def test_the_html_embeds_the_data_and_cannot_break_out_of_its_script():
    t = "2026-11-03T"
    samples = {"a@x.com": [_sample(t + "10:00:00+00:00", 20.0)]}
    request = _request(t + "11:00:00+00:00", 5)
    request.model = "claude-</script><b>"
    page = render(du.build_report([request], samples))
    assert "Desktop usage by account" in page and '"accounts"' in page
    assert "</script><b>" not in page
    assert payload(du.build_report([request], samples))["requests"][0]["m"] == "</script><b>"


def test_one_reading_gives_the_current_value_and_no_movement():
    samples = {"a@x.com": [_sample("2026-11-03T10:00:00+00:00", 20.0)]}
    (week,) = du.build_report([], samples).weeks
    assert week.week_pct() == (20.0, 20.0, 0.0)


def test_a_drop_in_the_percentage_counts_from_zero():
    t = "2026-11-03T"
    samples = {"a@x.com": [_sample(t + "10:00:00+00:00", 20.0), _sample(t + "11:00:00+00:00", 25.0), _sample(t + "12:00:00+00:00", 3.0)]}
    (week,) = du.build_report([], samples).weeks
    assert week.week_pct()[2] == 8.0
