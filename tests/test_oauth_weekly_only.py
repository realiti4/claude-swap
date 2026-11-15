"""OAuth accounts may report a weekly quota without a five-hour window.

Verified Ultra accounts have no five-hour limit, and the usage API answers
with ``five_hour: null``. Keep that response connected to the displays and
the switch decision: an absent window is not a zero reading, a window
reported without a utilization figure (``utilization: null``, which Claude
Code's usage schema allows) is unknown rather than zero or absent, an
unreadable window is a failed measurement unless a refused probe carries it
(the refusal is kept), and a failed fetch is not headroom. No network call
is made.
"""

from __future__ import annotations

import json
import urllib.error
from unittest.mock import MagicMock

import pytest

from claude_swap import oauth
from claude_swap.autoswitch import TickOutcome
from claude_swap.json_output import usage_to_json
from claude_swap.menubar import usage_summary
from claude_swap.switcher import _format_usage_lines
from claude_swap.tui.widgets import usage_rows
from claude_swap.usage_store import FetchRecord, UsageStore
from tests.test_autoswitch import EngineHarness, _usage

_OMITTED = object()
_LABELS = {"five_hour": "5h", "seven_day": "7d"}
_JSON_KEYS = {"five_hour": "fiveHour", "seven_day": "sevenDay"}
_CREDENTIALS = json.dumps({
    "claudeAiOauth": {
        "accessToken": "test-access",
        "refreshToken": "test-refresh",
        "expiresAt": 4_102_444_800_000,  # 2100-01-01: never due for a refresh.
        "scopes": ["user:profile", "user:inference", "user:sessions:claude_code"],
    },
})


@pytest.fixture
def fetch_outcome(monkeypatch):
    """Run ``try_fetch_usage_for_account`` for an inactive account against a
    canned usage response, or a transport error."""

    def fetch(payload=None, *, error=None):
        response = MagicMock()
        response.read.return_value = json.dumps(payload).encode()
        response.__enter__.return_value = response

        def urlopen(request, timeout):
            assert request.full_url == "https://api.anthropic.com/api/oauth/usage"
            if error is not None:
                raise error
            return response

        monkeypatch.setattr(oauth.urllib.request, "urlopen", urlopen)
        return oauth.try_fetch_usage_for_account(
            "2", "weekly@example.com", _CREDENTIALS, is_active=False,
        )

    return fetch


@pytest.fixture
def harness(temp_home):
    h = EngineHarness(temp_home)
    for number, email in (
        (1, "active@example.com"),
        (2, "weekly@example.com"),
        (3, "fallback@example.com"),
    ):
        h.seed(number, email)
    h.make_live("active@example.com", 1)
    return h


@pytest.mark.parametrize("five_hour, present", [
    pytest.param({"utilization": 10.0}, True, id="present"),
    pytest.param(None, False, id="null"),
    pytest.param(_OMITTED, False, id="omitted"),
    # ``{}`` stays absent: the falsy check this replaces skipped it (compatibility
    # exception).
    pytest.param({}, False, id="empty-object"),
])
def test_an_absent_five_hour_window_is_a_limit_the_account_does_not_have(
    fetch_outcome, five_hour, present,
):
    payload = {"seven_day": {"utilization": 25.0}}
    if five_hour is not _OMITTED:
        payload["five_hour"] = five_hour

    outcome = fetch_outcome(payload)

    assert outcome.error is None
    assert outcome.usage["seven_day"]["pct"] == 25.0
    assert ("five_hour" in outcome.usage) is present
    assert oauth.account_headroom(outcome.usage) == 75.0


@pytest.mark.parametrize("response_kind", ["empty", "null-windows", "timeout"])
def test_an_account_without_a_reading_has_no_usage(fetch_outcome, response_kind):
    if response_kind == "timeout":
        outcome = fetch_outcome(error=urllib.error.URLError(TimeoutError()))
        assert outcome.error == "timeout"
    else:
        payload = {} if response_kind == "empty" else {"five_hour": None, "seven_day": None}
        outcome = fetch_outcome(payload)
        assert outcome.error is None

    assert outcome.usage is None
    assert oauth.account_headroom(outcome.usage) is None
    assert usage_summary(outcome.usage) == "usage unavailable"
    assert usage_rows(outcome.usage, now=0) == []


def test_an_account_without_a_reading_never_becomes_a_switch_target(harness):
    result = harness.tick_with_usage({"1": _usage(98), "2": None, "3": _usage(70)})
    assert result is TickOutcome.SWITCHED
    assert harness.active_number() == 3


@pytest.mark.parametrize("window", ["five_hour", "seven_day"])
@pytest.mark.parametrize("broken", [
    # Every other falsy value is a malformed window, not a missing one.
    pytest.param(False, id="false-window"),
    pytest.param(0, id="zero-window"),
    pytest.param("", id="empty-string-window"),
    pytest.param([], id="empty-array-window"),
    pytest.param(["0"], id="array"),
    pytest.param(True, id="boolean-window"),
    pytest.param("50", id="string-window"),
    pytest.param({"resets_at": None}, id="missing-utilization"),
    pytest.param({"utilization": "0"}, id="string-utilization"),
    pytest.param({"utilization": False}, id="boolean-utilization"),
    pytest.param({"utilization": float("nan")}, id="nan"),
    pytest.param({"utilization": float("inf")}, id="infinity"),
    pytest.param({"utilization": 10 ** 400}, id="overflowing-integer"),
    pytest.param({"utilization": -1}, id="negative"),
])
def test_an_unreadable_window_fails_the_whole_reading(fetch_outcome, window, broken):
    # Dropping the unreadable window would let the healthy 10% peer pass as a complete
    # reading.
    payload = {
        "five_hour": {"utilization": 10.0},
        "seven_day": {"utilization": 10.0},
        window: broken,
    }
    outcome = fetch_outcome(payload)
    assert outcome.error == "bad-response"
    assert outcome.usage is None


def test_an_unreadable_window_from_the_header_probe_is_classified(monkeypatch):
    # The probe path normalises after its request; a window the validator rejects
    # must still classify as bad-response (no rejection to keep here).
    data = oauth.parse_unified_ratelimit_headers(
        {"anthropic-ratelimit-unified-5h-utilization": "1e308"}
    )
    monkeypatch.setattr(oauth, "request_usage_via_probe", lambda token: data)
    outcome = oauth._usage_from_probe("token", "for account 2")
    assert outcome.usage is None
    assert outcome.error == "bad-response"


def test_a_probe_without_a_5h_header_reads_as_weekly_only(monkeypatch):
    # An Ultra account has no 5h limit, so its probe carries no 5h header.
    data = oauth.parse_unified_ratelimit_headers({
        "anthropic-ratelimit-unified-status": "allowed",
        "anthropic-ratelimit-unified-7d-utilization": "0.88",
        "anthropic-ratelimit-unified-7d-reset": "1791154800",
        "anthropic-ratelimit-unified-representative-claim": "seven_day",
    })
    monkeypatch.setattr(oauth, "request_usage_via_probe", lambda token: data)
    outcome = oauth._usage_from_probe("token", "for account 2")
    assert outcome.error is None
    assert "five_hour" not in outcome.usage
    assert oauth.account_headroom(outcome.usage) == 12.0


@pytest.mark.parametrize("window", ["5h", "7d"])
@pytest.mark.parametrize(
    "bad",
    [
        pytest.param("nan", id="nan"),
        pytest.param("inf", id="infinity"),
        pytest.param("-0.1", id="negative"),
        pytest.param("", id="empty-string"),
        pytest.param("high", id="string"),
    ],
)
def test_an_unusable_header_utilization_fails_the_reading(monkeypatch, window, bad):
    # Dropping it would read a 5h account as one with no 5h window.
    data = oauth.parse_unified_ratelimit_headers({
        "anthropic-ratelimit-unified-5h-utilization": "0.2",
        "anthropic-ratelimit-unified-7d-utilization": "0.4",
        f"anthropic-ratelimit-unified-{window}-utilization": bad,
    })
    monkeypatch.setattr(oauth, "request_usage_via_probe", lambda token: data)
    outcome = oauth._usage_from_probe("token", "for account 2")
    assert outcome.usage is None
    assert outcome.error == "bad-response"


@pytest.mark.parametrize("header, value", [
    pytest.param("reset", "1791154800", id="reset-only"),
    pytest.param("status", "allowed", id="status-allowed-only"),
    pytest.param("status", "rejected", id="status-rejected-only"),
])
def test_a_5h_header_without_a_utilization_fails_the_reading(
    monkeypatch, header, value,
):
    # A 5h window that is present but has no figure is not "no 5h window".
    data = oauth.parse_unified_ratelimit_headers({
        "anthropic-ratelimit-unified-7d-utilization": "0.4",
        f"anthropic-ratelimit-unified-5h-{header}": value,
    })
    monkeypatch.setattr(oauth, "request_usage_via_probe", lambda token: data)
    outcome = oauth._usage_from_probe("token", "for account 2")
    assert outcome.usage is None
    assert outcome.error == "bad-response"


def test_a_fable_bucket_header_is_not_a_7d_window_header(monkeypatch):
    data = oauth.parse_unified_ratelimit_headers({
        "anthropic-ratelimit-unified-5h-utilization": "0.2",
        "anthropic-ratelimit-unified-7d_oi-reset": "1791154800",
    })
    monkeypatch.setattr(oauth, "request_usage_via_probe", lambda token: data)
    outcome = oauth._usage_from_probe("token", "for account 2")
    assert outcome.error is None
    assert "seven_day" not in outcome.usage


@pytest.mark.parametrize("five_hour", [
    pytest.param({"utilization": "1e308"}, id="overflow"),
    pytest.param({"utilization": "nan"}, id="nan"),
    pytest.param({"utilization": "inf"}, id="infinity"),
    pytest.param({"utilization": "-0.1"}, id="negative"),
    pytest.param({"utilization": ""}, id="empty-string"),
    pytest.param({"utilization": "high"}, id="string"),
    pytest.param({"status": "rejected"}, id="status-without-a-figure"),
])
def test_a_rejected_probe_with_an_unreadable_window_keeps_its_refusal(
    monkeypatch, tmp_path, five_hour,
):
    # A 429 rejection outranks its unreadable 5h window, whether the figure
    # overflows, fails to parse or is missing beside a 5h status header: the
    # refusal and its Retry-After are kept, so a healthy cached reading cannot
    # leave the refused account eligible for a switch.
    import email.message
    import time

    now = time.time()
    store = UsageStore(tmp_path / "cache", clock=lambda: now)
    identities = {"3": ("setup@example.com", "")}
    healthy = {
        "five_hour": {"pct": 10.0}, "seven_day": {"pct": 10.0},
        "source": oauth.USAGE_SOURCE_HEADERS,
    }
    store.record({"3": FetchRecord(usage=healthy)}, identities)
    cached = store.entries(identities)["3"].decision_value()
    assert oauth.account_headroom(cached) == 90.0

    headers = email.message.Message()
    for key, value in {
        "anthropic-ratelimit-unified-status": "rejected",
        **{f"anthropic-ratelimit-unified-5h-{k}": v for k, v in five_hour.items()},
        "anthropic-ratelimit-unified-7d-utilization": "0.6",
        "anthropic-ratelimit-unified-reset": str(int(now + 1800)),
        "Retry-After": "600",
    }.items():
        headers[key] = value

    def urlopen(request, timeout):
        assert request.full_url == oauth.USAGE_PROBE_URL
        raise urllib.error.HTTPError(
            oauth.USAGE_PROBE_URL, 429, "Too Many", hdrs=headers, fp=None,
        )

    monkeypatch.setattr(oauth.urllib.request, "urlopen", urlopen)
    setup_token = json.dumps({"claudeAiOauth": {
        "accessToken": "setup-access", "scopes": ["user:inference"],
    }})
    outcome = oauth.try_fetch_usage_for_account(
        "3", "setup@example.com", setup_token, is_active=False, header_probe=True,
    )

    assert outcome.error is None
    assert outcome.usage["rejected"]["retry_after_s"] == 600.0
    assert outcome.usage["source"] == oauth.USAGE_SOURCE_HEADERS
    assert "five_hour" not in outcome.usage and "seven_day" not in outcome.usage
    assert oauth.relevant_windows(outcome.usage) == [
        ("rejected", 100.0, outcome.usage["rejected"]["resets_at"]),
    ]

    store.record({"3": FetchRecord(usage=outcome.usage)}, identities)
    entry = store.entries(identities)["3"]
    assert entry.last_error is None
    assert entry.last_good == outcome.usage
    assert oauth.account_headroom(entry.decision_value()) == 0.0
    assert entry.backoff_until == pytest.approx(now + 600.0)


def test_an_unreadable_window_keeps_the_last_good_reading(fetch_outcome, tmp_path):
    now = 10_000_000.0
    store = UsageStore(tmp_path / "cache", clock=lambda: now)
    identities = {"2": ("weekly@example.com", "")}
    good = fetch_outcome({"seven_day": {"utilization": 83.0}})
    store.record({"2": FetchRecord(usage=good.usage)}, identities)
    fetched_at = now

    now += 60
    bad = fetch_outcome({
        "five_hour": {"utilization": "0"},
        "seven_day": {"utilization": 1.0},
    })
    store.record({"2": FetchRecord(usage=bad.usage, error=bad.error)}, identities)
    entry = store.entries(identities)["2"]
    assert entry.last_error == "bad-response"
    assert entry.last_good == good.usage
    assert entry.fetched_at == fetched_at
    # The last good reading survives; the unreadable 1% never becomes headroom.
    assert entry.decision_value() == good.usage


@pytest.mark.parametrize("window, other", [
    ("five_hour", "seven_day"), ("seven_day", "five_hour"),
])
@pytest.mark.parametrize("resets_at", [
    pytest.param(None, id="no-reset"),
    pytest.param("2099-01-01T05:00:00+00:00", id="with-reset"),
])
def test_a_window_without_a_utilization_figure_is_unknown_not_absent(
    fetch_outcome, window, other, resets_at,
):
    # ``utilization: null``: the limit exists, its figure is unavailable. Not a failed
    # fetch, not 0%, not absent.
    reported = {"utilization": None}
    if resets_at:
        reported["resets_at"] = resets_at
    outcome = fetch_outcome({window: reported, other: {"utilization": 10.0}})

    assert outcome.error is None
    entry = outcome.usage[window]
    assert entry["pct"] is None
    assert entry.get("resets_at") == resets_at
    assert ("countdown" in entry and "clock" in entry) is bool(resets_at)
    assert outcome.usage[other] == {"pct": 10.0}

    # Decisions use only the window that carries a figure.
    assert oauth.account_headroom(outcome.usage) == 90.0
    assert [w[0] for w in oauth.relevant_windows(outcome.usage)] == [_LABELS[other]]

    # CLI and TUI rows show the window as unknown; the menu bar keeps its numeric-only
    # segments.
    label = _LABELS[window]
    assert f"{_LABELS[other]} 10%" in usage_summary(outcome.usage)
    line = next(l for l in _format_usage_lines(outcome.usage) if l.startswith(f"{label}:"))
    assert line.split()[1] == "?" and "%" not in line
    assert ("resets" in line) is bool(resets_at)
    row = next(r for r in usage_rows(outcome.usage, now=0) if r[0] == label)
    assert row[1] is None
    exported = usage_to_json(outcome.usage)[_JSON_KEYS[window]]
    assert exported["pct"] is None and exported.get("resetsAt") == resets_at


def test_a_reading_with_an_unknown_window_replaces_the_last_good_one(
    fetch_outcome, tmp_path,
):
    # A valid reading: its weekly figure replaces the last good one, with no
    # bad-response or backoff.
    now = 10_000_000.0
    store = UsageStore(tmp_path / "cache", clock=lambda: now)
    identities = {"2": ("weekly@example.com", "")}
    old = fetch_outcome({"seven_day": {"utilization": 83.0}})
    store.record({"2": FetchRecord(usage=old.usage)}, identities)

    now += 60
    new = fetch_outcome({
        "five_hour": {"utilization": None, "resets_at": "2099-01-01T05:00:00+00:00"},
        "seven_day": {"utilization": 1.0},
    })
    assert new.error is None
    store.record({"2": FetchRecord(usage=new.usage, error=new.error)}, identities)
    entry = store.entries(identities)["2"]
    assert entry.last_error is None
    assert entry.fetched_at == now
    assert entry.decision_value() == new.usage
    assert oauth.account_headroom(entry.decision_value()) == 99.0


def test_an_unknown_window_alone_is_no_figure_to_rank_on(fetch_outcome, harness):
    outcome = fetch_outcome({"five_hour": {"utilization": None}, "seven_day": None})
    assert outcome.error is None
    assert outcome.usage == {"five_hour": {"pct": None}}
    assert oauth.account_headroom(outcome.usage) is None
    assert _format_usage_lines(outcome.usage) == ["5h:    ?"]
    # Unknown headroom never becomes a switch target.
    result = harness.tick_with_usage({"1": _usage(98), "2": outcome.usage, "3": _usage(70)})
    assert result is TickOutcome.SWITCHED
    assert harness.active_number() == 3


def test_a_new_weekly_only_reading_drops_the_previous_five_hour_window(
    fetch_outcome, harness, tmp_path,
):
    store = UsageStore(tmp_path / "cache", clock=lambda: harness.clock.now)
    identities = {"2": ("weekly@example.com", "")}
    old = fetch_outcome({
        "five_hour": {"utilization": 100.0},
        "seven_day": {"utilization": 25.0},
    })
    store.record({"2": FetchRecord(usage=old.usage)}, identities)
    current = fetch_outcome({"five_hour": None, "seven_day": {"utilization": 25.0}})
    store.record({"2": FetchRecord(usage=current.usage)}, identities)
    value = store.entries(identities)["2"].decision_value()

    assert value == {"seven_day": {"pct": 25.0}}
    assert "5h" not in usage_summary(value)
    assert harness.tick_with_usage({
        "1": _usage(98), "2": value, "3": _usage(70),
    }) is TickOutcome.SWITCHED
    assert harness.active_number() == 2


def test_an_over_limit_reading_is_retained_without_clamping(fetch_outcome):
    outcome = fetch_outcome({"seven_day": {"utilization": 105.0}})
    assert outcome.error is None
    assert outcome.usage["seven_day"]["pct"] == 105.0
    assert oauth.account_headroom(outcome.usage) == -5.0
