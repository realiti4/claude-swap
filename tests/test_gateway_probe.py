"""Gateway HTTP verdicts, secret redaction, and persisted independent cadence."""

import io
import json
from urllib.error import HTTPError, URLError
from unittest.mock import Mock

import pytest

from claude_swap import gateway
from claude_swap.usage_store import FetchRecord, UsageStore
from tests.test_gateway_accounts import KEY


def respond(monkeypatch, status, message="", retry=None):
    body = json.dumps({"error": {"type": "test_error", "message": message}}).encode()
    response = Mock(status=status, headers={"Retry-After": retry} if retry else {})
    response.read.return_value = body
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=None)
    opener = Mock()
    if status == 200:
        opener.open.return_value = response
    else:
        opener.open.side_effect = HTTPError(
            "https://gateway.example/v1/messages",
            status,
            "error",
            response.headers,
            io.BytesIO(body),
        )
    monkeypatch.setattr(gateway, "build_opener", lambda *args: opener)
    return opener


class TestGatewayProbe:
    @pytest.mark.parametrize("cached", [None, 42, "bad"])
    def test_gateway_rebuilds_non_object_cache_rows(self, temp_home, monkeypatch, cached):
        store = UsageStore(temp_home / "cache", clock=lambda: 1000.0)
        store._write_rows({"1": cached})
        respond(monkeypatch, 200)
        entry = store.collect_gateway("1", ("gw@example.com", ""), gateway.account_fields("https://gateway.example"), KEY, active=True, fetch=True)
        assert entry.gateway["probeStatus"] == "ok"

    def test_gateway_legacy_unavailable_fingerprint_keeps_failure_clock(self, temp_home, monkeypatch):
        import hashlib

        now = [1000.0]
        store = UsageStore(temp_home / "cache", clock=lambda: now[0])
        record = gateway.account_fields("https://gateway.example")
        store.collect_gateway("1", ("gw@example.com", ""), record, "", active=True, fetch=True)
        rows = store._read_rows()
        row = rows["1"]
        row.pop("gatewayAuthGeneration", None)
        row.pop("gatewayConfigFingerprint", None)
        config = {name: record.get(name) for name in ("baseUrl", "auth", "probeModel", "usageUrl")}
        row["gatewayFingerprint"] = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
        store._write_rows(rows)
        now[0] += 601
        respond(monkeypatch, 500)
        entry = store.collect_gateway("1", ("gw@example.com", ""), record, KEY, active=True, fetch=True)
        assert entry.gateway["failureSince"] == 1000

    @pytest.mark.parametrize("status", [401, 500, 503])
    def test_gateway_read_failures_preserve_failure_generation(self, temp_home, monkeypatch, status):
        now = [1000.0]
        store = UsageStore(temp_home / "cache", clock=lambda: now[0])
        record = dict(gateway.account_fields("https://gateway.example"), authGeneration="original")
        monkeypatch.setattr(gateway, "JITTER_FRAC", 0)
        opener = respond(monkeypatch, status)
        def collect(key):
            return store.collect_gateway("1", ("gw@example.com", ""), record, key, active=True, fetch=True)
        first = collect(KEY)
        for key in ("", KEY, "", KEY):
            now[0] += 601
            entry = collect(key)
            if status == 401:
                assert entry.gateway["probeStatus"] == "unauthorized"
                assert opener.open.call_count == 1
            else:
                assert entry.gateway["failureSince"] == first.gateway["failureSince"] == 1000
        now[0] += 601
        # An actual credential replacement starts a distinct failure history.
        replaced = collect(KEY + "-replacement")
        if status == 401:
            assert opener.open.call_count == 2
        else:
            assert replaced.gateway["failureSince"] == now[0]
        now[0] += 601
        record["authGeneration"] = "explicit-replacement"
        replaced = collect(KEY + "-replacement")
        if status == 401:
            assert opener.open.call_count == 3
        else:
            assert replaced.gateway["failureSince"] == now[0]

    def test_gateway_first_read_recovery_preserves_continuous_failure(self, temp_home, monkeypatch):
        now = [1000.0]
        store = UsageStore(temp_home / "cache", clock=lambda: now[0])
        record = dict(gateway.account_fields("https://gateway.example"), authGeneration="original")
        monkeypatch.setattr(gateway, "JITTER_FRAC", 0)
        respond(monkeypatch, 500)
        first = store.collect_gateway("1", ("gw@example.com", ""), record, "", active=True, fetch=True)
        now[0] += 601
        recovered_read = store.collect_gateway("1", ("gw@example.com", ""), record, KEY, active=True, fetch=True)
        assert recovered_read.gateway["failureSince"] == first.gateway["failureSince"] == 1000

    def test_transient_since_persists_and_resets_after_recovery(self, monkeypatch):
        record = gateway.account_fields("https://gateway.example")
        respond(monkeypatch, 500)
        first = gateway.probe(record, KEY, now=1000)
        assert first["transientSince"] == 1000
        opener = respond(monkeypatch, 500)
        opener.open.side_effect = URLError(KEY)
        second = gateway.probe(record, KEY, now=1601, previous=first)
        assert second["transientSince"] == 1000
        respond(monkeypatch, 200)
        recovered = gateway.probe(record, KEY, now=1700, previous=second)
        assert "transientSince" not in recovered
        respond(monkeypatch, 404)
        assert gateway.probe(record, KEY, now=1800, previous=recovered)["transientSince"] == 1800

    @pytest.mark.parametrize("status", [503, 529])
    def test_cooling_collection_obeys_growing_retry_deadline(self, temp_home, monkeypatch, status):
        now = [1000.0]
        store = UsageStore(temp_home / "cache", clock=lambda: now[0])
        monkeypatch.setattr(gateway, "JITTER_FRAC", 0)
        record = gateway.account_fields("https://gateway.example")

        def collect():
            return store.collect_gateway(
                "1", ("gateway@example.com", ""), record, KEY, active=True, fetch=True
            )

        for delay in (60, 120, 240, 480, 600, 600):
            opener = respond(monkeypatch, status)
            entry = collect()
            assert opener.open.call_count == 1
            assert entry.next_poll_at == now[0] + delay
            assert entry.next_poll_at == entry.gateway["retryAt"]
            now[0] += delay - 1
            collect()
            assert opener.open.call_count == 1
            now[0] += 1

    @pytest.mark.parametrize("reset", ["clear_dead_token", "record"])
    def test_oauth_cache_write_clears_prior_gateway_state(self, temp_home, monkeypatch, reset):
        store = UsageStore(temp_home / "cache", clock=lambda: 1000.0)
        identity = {"1": ("same@example.com", "")}
        respond(monkeypatch, 200)
        store.collect_gateway(
            "1", identity["1"], gateway.account_fields("https://gateway.example"), KEY,
            active=False, fetch=True,
        )
        assert store.entries(identity)["1"].sentinel == "gateway"
        usage = {"five_hour": {"pct": 10.0}}
        if reset == "clear_dead_token":
            store.clear_dead_token(["1"], identity)
        else:
            store.record({"1": FetchRecord(usage=usage)}, identity)
        entry = store.entries(identity)["1"]
        assert entry.sentinel is None and entry.gateway is None
        assert entry.next_poll_at is None
        assert not any(key.startswith("gateway") for key in store._read_rows()["1"])
        if reset == "record":
            assert entry.decision_value() == usage
        else:
            assert "1" in store.reserve(["1"], identity, respect_plans=True)

    @pytest.mark.parametrize(
        "status,verdict",
        [
            (200, "ok"),
            (401, "unauthorized"),
            (403, "unauthorized"),
            (429, "exhausted"),
            (503, "cooling"),
            (529, "cooling"),
            (500, "transient"),
            (404, "transient"),
        ],
    )
    def test_http_verdicts(self, monkeypatch, status, verdict):
        respond(monkeypatch, status)
        result = gateway.probe(
            gateway.account_fields("https://gateway.example"), KEY, now=100
        )
        assert result["probeStatus"] == verdict
        if status == 429:
            assert result["retryAt"] == 3700

    @pytest.mark.parametrize("auth", ["bearer", "x-api-key"])
    def test_probe_request(self, monkeypatch, auth):
        opener = respond(monkeypatch, 200)
        gateway.probe(
            gateway.account_fields(
                "http://127.0.0.1:8000", auth, probe_model="dummy-model"
            ),
            KEY,
        )
        request = opener.open.call_args.args[0]
        assert request.full_url == "http://127.0.0.1:8000/v1/messages"
        assert request.get_method() == "POST"
        assert request.headers["Anthropic-version"] == "2023-06-01"
        assert request.headers[
            "Authorization" if auth == "bearer" else "X-api-key"
        ] == ("Bearer " + KEY if auth == "bearer" else KEY)
        assert json.loads(request.data) == {
            "model": "dummy-model",
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "."}],
        }
        assert opener.open.call_args.kwargs["timeout"] == 15

    @pytest.mark.parametrize(
        "message,expected",
        [
            ("unknown model dummy", "ok"),
            ("model not found", "ok"),
            ("invalid request", "transient"),
        ],
    )
    def test_unknown_model_proves_auth(self, monkeypatch, message, expected):
        respond(monkeypatch, 400, message)
        assert (
            gateway.probe(gateway.account_fields("https://gateway.example"), KEY)[
                "probeStatus"
            ]
            == expected
        )

    def test_retry_after_and_cooling_backoff(self, monkeypatch):
        respond(monkeypatch, 429, retry="90")
        assert (
            gateway.probe(
                gateway.account_fields("https://gateway.example"), KEY, now=100
            )["retryAt"]
            == 190
        )
        previous = {}
        for count, delay in enumerate((60, 120, 240, 480, 600, 600)):
            respond(monkeypatch, 529)
            previous = gateway.probe(
                gateway.account_fields("https://gateway.example"),
                KEY,
                now=100 + count,
                previous=previous,
            )
            assert previous["retryAt"] == 100 + count + delay
            assert previous["coolingSince"] == 100

    def test_no_key_leak_in_verdict_log_json_or_exception(self, monkeypatch, caplog):
        respond(monkeypatch, 503, KEY + " " + "x" * 300)
        record = gateway.account_fields("https://gateway.example")
        verdict = gateway.probe(record, KEY)
        assert len(verdict["message"]) <= 120
        assert (
            KEY
            not in repr(verdict)
            + json.dumps(gateway.json_fields(record, verdict))
            + caplog.text
        )
        opener = Mock()
        opener.open.side_effect = URLError(KEY)
        monkeypatch.setattr(gateway, "build_opener", lambda *args: opener)
        # An exception containing the credential is swallowed and sanitized.
        result = gateway.probe(record, KEY)
        assert result["probeStatus"] == "transient"
        assert KEY not in repr(result) + caplog.text
        with pytest.raises(ValueError) as exc:
            gateway.validate_url("https://example.com?key=" + KEY)
        assert KEY not in str(exc.value)

    def test_cadences_and_unconfigured_usage(self, monkeypatch):
        monkeypatch.setattr(gateway, "JITTER_FRAC", 0)
        assert gateway.next_probe_at({"probeStatus": "ok"}, active=True, now=0) == 120
        assert gateway.next_probe_at({"probeStatus": "ok"}, active=False, now=0) == 600
        assert (
            gateway.next_probe_at(
                {"probeStatus": "exhausted", "retryAt": 20}, active=False, now=0
            )
            == 80
        )
        assert (
            gateway.next_probe_at({"probeStatus": "cooling"}, active=False, now=0) == 60
        )
        assert (
            gateway.next_probe_at({"probeStatus": "unauthorized"}, active=True, now=0)
            is None
        )
        assert gateway.usage({}, KEY) == "unconfigured"

    def test_persistence_cadence_quarantine_and_replaced_key(
        self, temp_home, monkeypatch
    ):
        now = [1000.0]
        store = UsageStore(temp_home / "cache", clock=lambda: now[0])
        record = gateway.account_fields("https://gateway.example")
        status = ["ok"]
        probe = Mock(
            side_effect=lambda *a, **kw: dict(
                probeStatus=status[0], probedAt=kw["now"], retryAt=None, budget=None
            )
        )
        monkeypatch.setattr(gateway, "probe", probe)
        monkeypatch.setattr(gateway, "JITTER_FRAC", 0)

        def collect(key=KEY, active=False):
            return store.collect_gateway(
                "1", ("gateway@token.local", ""), record, key, active=active, fetch=True
            )

        assert collect().gateway["probeStatus"] == "ok"
        collect()
        assert probe.call_count == 1
        now[0] += 120
        collect(active=True)
        assert probe.call_count == 2
        now[0] += 120
        status[0] = "unauthorized"
        collect(active=True)
        now[0] += 10000
        collect(active=True)
        assert probe.call_count == 3
        status[0] = "ok"
        collect(KEY + "-replacement")
        assert probe.call_count == 4
        rows = store._read_rows()
        assert rows["1"]["sentinel"] == "gateway"
        assert KEY not in json.dumps(rows)


@pytest.mark.parametrize("status", ["unauthorized", "transient"])
@pytest.mark.parametrize("field,value", [("alias", "renamed"), ("threshold", 60), ("disabled", True)])
def test_gateway_metadata_changes_preserve_failure_history(temp_home, monkeypatch, status, field, value):
    now = [1000.0]
    store = UsageStore(temp_home / "cache", clock=lambda: now[0])
    record = gateway.account_fields("https://gateway.example")
    def verdict(*args, **kw):
        since = (kw.get("previous") or {}).get("failureSince", kw["now"])
        return dict(probeStatus=status, probedAt=kw["now"], budget=None, failureSince=since, transientSince=since)
    probe = Mock(side_effect=verdict)
    monkeypatch.setattr(gateway, "probe", probe)
    store.collect_gateway("1", ("gw@example.com", ""), record, KEY, active=True, fetch=True)
    now[0] += 601
    record[field] = value
    entry = store.collect_gateway("1", ("gw@example.com", ""), record, KEY, active=True, fetch=True)
    assert entry.gateway["failureSince"] == 1000
    if status == "unauthorized":
        assert probe.call_count == 1
