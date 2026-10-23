"""Subscription plan display (#350): fetch, label, expiry, pacing, surfaces.

The three subscription states below are the measured ones from #350; every
classification test is pinned to them rather than to guessed values.
"""

from __future__ import annotations

import dataclasses
import io
import json
import time
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import json_output, subscription
from claude_swap.models import AccountSnapshot

ACTIVE_MAX = {
    "tier": "default_claude_max_20x",
    "billingType": "stripe_subscription",
    "subscriptionStatus": "active",
}
# Cancelled but still inside the paid period: indistinguishable from ACTIVE_MAX,
# which is the whole reason renewal is not shown.
CANCELLED_IN_PERIOD = dict(ACTIVE_MAX)
LAPSED = {
    "tier": "default_claude_ai",
    "billingType": "none",
    "subscriptionStatus": "canceled",
}
TEAM = {
    "tier": "default_claude_max_5x",
    "organizationType": "claude_team",
    "billingType": "stripe_subscription",
    "subscriptionStatus": "active",
}


def _creds(token: str | None = "tok", expires_ms: int | None = None) -> str:
    blob: dict = {}
    if token is not None:
        blob["accessToken"] = token
    if expires_ms is not None:
        blob["expiresAt"] = expires_ms
    return json.dumps({"claudeAiOauth": blob})


def _future_ms() -> int:
    return int((time.time() + 3600) * 1000)


def _past_ms() -> int:
    return int((time.time() - 3600) * 1000)


# --- fetch_plan ---------------------------------------------------------------


def _response(payload) -> io.BytesIO:
    body = io.BytesIO(json.dumps(payload).encode())
    body.__enter__ = lambda self=body: self  # type: ignore[attr-defined]
    body.__exit__ = lambda *a: None  # type: ignore[attr-defined]
    return body


@pytest.mark.no_oauth_profile_fake
class TestFetchPlan:
    def test_keeps_the_four_plan_fields_in_camel_case(self):
        payload = {
            "account": {"uuid": "u", "email": "e@x"},
            "organization": {
                "rate_limit_tier": "default_claude_max_5x",
                "organization_type": "claude_team",
                "billing_type": "stripe_subscription",
                "subscription_status": "active",
                "name": "not kept",
                "uuid": "not kept",
            },
        }
        with patch("urllib.request.urlopen", return_value=_response(payload)):
            assert subscription.fetch_plan("tok") == TEAM

    def test_no_tier_is_no_answer(self):
        payload = {"organization": {"billing_type": "stripe_subscription"}}
        with patch("urllib.request.urlopen", return_value=_response(payload)):
            assert subscription.fetch_plan("tok") is None

    def test_empty_tier_is_no_answer(self):
        payload = {"organization": {"rate_limit_tier": ""}}
        with patch("urllib.request.urlopen", return_value=_response(payload)):
            assert subscription.fetch_plan("tok") is None

    def test_non_string_fields_are_dropped_not_kept(self):
        payload = {
            "organization": {
                "rate_limit_tier": "default_claude_ai",
                "subscription_status": None,
                "billing_type": 3,
            }
        }
        with patch("urllib.request.urlopen", return_value=_response(payload)):
            assert subscription.fetch_plan("tok") == {"tier": "default_claude_ai"}

    @pytest.mark.parametrize(
        "error",
        [
            urllib.error.HTTPError("u", 401, "Unauthorized", {}, None),
            urllib.error.URLError("down"),
            TimeoutError("slow"),
        ],
    )
    def test_any_transport_failure_is_none(self, error):
        with patch("urllib.request.urlopen", side_effect=error):
            assert subscription.fetch_plan("tok") is None

    def test_malformed_body_is_none(self):
        body = io.BytesIO(b"not json")
        body.__enter__ = lambda self=body: self  # type: ignore[attr-defined]
        body.__exit__ = lambda *a: None  # type: ignore[attr-defined]
        with patch("urllib.request.urlopen", return_value=body):
            assert subscription.fetch_plan("tok") is None

    def test_non_dict_organization_is_none(self):
        with patch(
            "urllib.request.urlopen", return_value=_response({"organization": []})
        ):
            assert subscription.fetch_plan("tok") is None


# --- label and expiry ---------------------------------------------------------


class TestPlanLabel:
    def test_team_organization_wins_over_the_seat_tier(self):
        assert subscription.plan_label(TEAM) == "Team"

    @pytest.mark.parametrize(
        "tier, label",
        [
            ("default_claude_ai", "Free"),
            ("default_claude_max_5x", "Max 5x"),
            ("default_claude_max_20x", "Max 20x"),
        ],
    )
    def test_observed_tiers(self, tier, label):
        assert subscription.plan_label({"tier": tier}) == label

    def test_unobserved_tier_shows_raw_rather_than_a_guess(self):
        assert subscription.plan_label({"tier": "default_claude_pro"}) == (
            "default_claude_pro"
        )

    def test_unknown_organization_type_falls_through_to_the_tier(self):
        plan = {"tier": "default_claude_max_20x", "organizationType": "claude_max"}
        assert subscription.plan_label(plan) == "Max 20x"

    def test_no_plan_no_label(self):
        assert subscription.plan_label(None) is None


class TestIsExpired:
    def test_active_max_is_not_expired(self):
        assert not subscription.is_expired(ACTIVE_MAX)

    def test_cancelled_still_in_period_is_not_expired(self):
        assert not subscription.is_expired(CANCELLED_IN_PERIOD)

    def test_lapsed_is_expired(self):
        assert subscription.is_expired(LAPSED)

    def test_canceled_on_a_paid_tier_is_not_enough(self):
        assert not subscription.is_expired(
            {"tier": "default_claude_max_20x", "subscriptionStatus": "canceled"}
        )

    def test_free_tier_while_active_is_not_enough(self):
        assert not subscription.is_expired(
            {"tier": "default_claude_ai", "subscriptionStatus": "active"}
        )

    def test_no_plan_is_not_expired(self):
        assert not subscription.is_expired(None)


# --- records, due, tokens -----------------------------------------------------


class TestRecords:
    def test_missing_is_due(self):
        assert subscription.is_due(None, now=1000.0)

    def test_fresh_is_not_due(self):
        record = {"plan": TEAM, "fetchedAt": 1000.0}
        assert not subscription.is_due(record, now=1000.0 + 60)

    def test_due_at_ttl(self):
        record = {"plan": TEAM, "fetchedAt": 1000.0}
        assert subscription.is_due(record, now=1000.0 + subscription.PLAN_TTL_S)

    def test_future_stamp_is_clock_skew_and_due(self):
        record = {"plan": TEAM, "fetchedAt": 5000.0}
        assert subscription.is_due(record, now=1000.0)

    @pytest.mark.parametrize(
        "record",
        [
            {"plan": TEAM},
            {"fetchedAt": 1.0},
            {"plan": {"label": "no tier"}, "fetchedAt": 1.0},
            {"plan": TEAM, "fetchedAt": True},
            "not a dict",
        ],
    )
    def test_malformed_record_is_invalid_and_due(self, record):
        assert not subscription.valid_record(record)
        assert subscription.is_due(record, now=1000.0)

    def test_identity_key_separates_organizations(self):
        assert subscription.identity_key("a@x", "org-1") != (
            subscription.identity_key("a@x", "org-2")
        )


class TestUsableAccessToken:
    def test_valid_token(self):
        assert subscription.usable_access_token(_creds("t", _future_ms())) == "t"

    def test_expired_token_is_not_usable(self):
        assert subscription.usable_access_token(_creds("t", _past_ms())) is None

    def test_missing_token(self):
        assert subscription.usable_access_token(_creds(None, _future_ms())) is None

    @pytest.mark.parametrize("creds", [None, "", "not json", "{}"])
    def test_garbage(self, creds):
        assert subscription.usable_access_token(creds) is None


# --- store --------------------------------------------------------------------


class TestPlanStore:
    def test_round_trip(self, tmp_path: Path):
        store = subscription.PlanStore(tmp_path / "p.json")
        store.save({"k": {"plan": TEAM, "fetchedAt": 1.0}})
        assert store.load() == {"k": {"plan": TEAM, "fetchedAt": 1.0}}

    def test_save_merges_with_another_writer(self, tmp_path: Path):
        """Two processes refreshing different accounts must both land."""
        path = tmp_path / "p.json"
        first = subscription.PlanStore(path)
        second = subscription.PlanStore(path)
        first.save({"a": {"plan": TEAM, "fetchedAt": 1.0}})
        second.save({"b": {"plan": LAPSED, "fetchedAt": 2.0}})
        assert set(first.load()) == {"a", "b"}

    def test_corrupt_file_reads_as_empty(self, tmp_path: Path):
        path = tmp_path / "p.json"
        path.write_text("{ not json")
        assert subscription.PlanStore(path).load() == {}

    def test_missing_file_reads_as_empty(self, tmp_path: Path):
        assert subscription.PlanStore(tmp_path / "absent.json").load() == {}


# --- collector ----------------------------------------------------------------


class _Clock:
    def __init__(self, now: float):
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def switcher(temp_home):
    from claude_swap.switcher import ClaudeAccountSwitcher

    sw = ClaudeAccountSwitcher()
    sw._setup_directories()
    sw._usage_store.clock = _Clock(1_000_000.0)
    return sw


def _info(num, email, org, active, creds):
    return (num, email, "Org", org, active, creds, "")


class TestCollector:
    def test_fetches_a_due_slot_and_stores_it(self, switcher, monkeypatch):
        calls = []
        monkeypatch.setattr(
            subscription, "fetch_plan", lambda t, *a, **k: calls.append(t) or TEAM
        )
        plans = switcher._collect_subscription_plans(
            [_info(1, "a@x", "o1", True, _creds("live", _future_ms()))]
        )
        assert calls == ["live"]
        assert plans["1"]["plan"] == TEAM
        assert plans["1"]["fetchedAt"] == 1_000_000.0

    def test_a_fresh_record_is_served_without_a_call(self, switcher, monkeypatch):
        monkeypatch.setattr(subscription, "fetch_plan", lambda t, *a, **k: TEAM)
        info = [_info(1, "a@x", "o1", True, _creds("live", _future_ms()))]
        switcher._collect_subscription_plans(info)

        calls = []
        monkeypatch.setattr(
            subscription, "fetch_plan", lambda t, *a, **k: calls.append(t) or LAPSED
        )
        switcher._usage_store.clock.now += 60
        plans = switcher._collect_subscription_plans(info)
        assert calls == []
        assert plans["1"]["plan"] == TEAM

    def test_a_failed_fetch_keeps_the_last_record_and_does_not_advance_it(
        self, switcher, monkeypatch
    ):
        """The stuck-label case: a failure must neither erase the plan nor
        stamp it fresh — stamping would postpone the retry by a whole TTL."""
        info = [_info(1, "a@x", "o1", True, _creds("live", _future_ms()))]
        monkeypatch.setattr(subscription, "fetch_plan", lambda t, *a, **k: TEAM)
        switcher._collect_subscription_plans(info)

        monkeypatch.setattr(subscription, "fetch_plan", lambda t, *a, **k: None)
        switcher._usage_store.clock.now += subscription.PLAN_TTL_S
        plans = switcher._collect_subscription_plans(info)
        assert plans["1"] == {"plan": TEAM, "fetchedAt": 1_000_000.0}

        calls = []
        monkeypatch.setattr(
            subscription, "fetch_plan", lambda t, *a, **k: calls.append(t) or LAPSED
        )
        switcher._usage_store.clock.now += 60
        plans = switcher._collect_subscription_plans(info)
        assert calls == ["live"], "the failure must leave the record due"
        assert plans["1"]["plan"] == LAPSED

    def test_inactive_slot_rereads_a_token_the_usage_pass_refreshed(
        self, switcher, monkeypatch
    ):
        calls = []
        monkeypatch.setattr(
            subscription, "fetch_plan", lambda t, *a, **k: calls.append(t) or TEAM
        )
        monkeypatch.setattr(
            switcher,
            "_read_account_credentials",
            lambda num, email, **k: _creds("refreshed", _future_ms()),
        )
        switcher._collect_subscription_plans(
            [_info(2, "b@x", "o1", False, _creds("stale", _past_ms()))]
        )
        assert calls == ["refreshed"]

    def test_no_valid_token_means_no_call_and_no_record(self, switcher, monkeypatch):
        calls = []
        monkeypatch.setattr(
            subscription, "fetch_plan", lambda t, *a, **k: calls.append(t) or TEAM
        )
        monkeypatch.setattr(
            switcher,
            "_read_account_credentials",
            lambda num, email, **k: _creds("still-stale", _past_ms()),
        )
        plans = switcher._collect_subscription_plans(
            [_info(2, "b@x", "o1", False, _creds("stale", _past_ms()))]
        )
        assert calls == []
        assert "2" not in plans

    def test_active_slot_never_falls_back_to_the_slot_backup(
        self, switcher, monkeypatch
    ):
        """The live store is the active slot's truth; its backup may be older."""
        read = []
        monkeypatch.setattr(subscription, "fetch_plan", lambda t, *a, **k: TEAM)
        monkeypatch.setattr(
            switcher,
            "_read_account_credentials",
            lambda num, email, **k: read.append(num) or _creds("backup", _future_ms()),
        )
        switcher._collect_subscription_plans(
            [_info(1, "a@x", "o1", True, _creds("live", _past_ms()))]
        )
        assert read == []

    def test_a_slot_without_oauth_is_skipped(self, switcher, monkeypatch):
        calls = []
        monkeypatch.setattr(
            subscription, "fetch_plan", lambda t, *a, **k: calls.append(t) or TEAM
        )
        monkeypatch.setattr(
            switcher, "_read_account_credentials", lambda num, email, **k: "sk-ant-api"
        )
        plans = switcher._collect_subscription_plans(
            [_info(3, "k@x", "", False, "sk-ant-api")]
        )
        assert calls == [] and plans == {}

    def test_the_record_follows_the_identity_when_slots_are_renumbered(
        self, switcher, monkeypatch
    ):
        monkeypatch.setattr(subscription, "fetch_plan", lambda t, *a, **k: LAPSED)
        switcher._collect_subscription_plans(
            [_info(1, "a@x", "o1", True, _creds("live", _future_ms()))]
        )
        monkeypatch.setattr(
            subscription, "fetch_plan", lambda t, *a, **k: pytest.fail("refetched")
        )
        plans = switcher._collect_subscription_plans(
            [_info(4, "a@x", "o1", True, _creds("live", _future_ms()))]
        )
        assert plans["4"]["plan"] == LAPSED

    def test_lands_in_the_switchers_own_backup_dir(self, switcher, monkeypatch):
        monkeypatch.setattr(subscription, "fetch_plan", lambda t, *a, **k: TEAM)
        switcher._collect_subscription_plans(
            [_info(1, "a@x", "o1", True, _creds("live", _future_ms()))]
        )
        assert (
            switcher.backup_dir / "cache" / subscription.PLAN_FILENAME
        ).exists()


# --- surfaces -----------------------------------------------------------------


class TestJson:
    def _row(self, **kw):
        return json_output.account_row(1, "a@x", "Org", "o1", True, None, **kw)

    def test_no_plan_emits_no_key(self):
        assert "plan" not in self._row()

    def test_plan_object(self):
        row = self._row(subscription_plan={"plan": LAPSED, "fetchedAt": 0.0})
        assert row["plan"] == {
            "label": "Free",
            "tier": "default_claude_ai",
            "billingType": "none",
            "subscriptionStatus": "canceled",
            "expired": True,
            "fetchedAt": "1970-01-01T00:00:00Z",
        }

    def test_absent_source_fields_are_omitted_not_nulled(self):
        row = self._row(
            subscription_plan={"plan": {"tier": "default_claude_ai"}, "fetchedAt": 0.0}
        )
        assert set(row["plan"]) == {"label", "tier", "expired", "fetchedAt"}


class TestListMarker:
    def test_label(self):
        from claude_swap.switcher import _plan_marker

        marker = _plan_marker({"plan": TEAM, "fetchedAt": 1000.0}, now=1000.0)
        assert "Team" in marker and "expired" not in marker

    def test_expired(self):
        from claude_swap.switcher import _plan_marker

        marker = _plan_marker({"plan": LAPSED, "fetchedAt": 1000.0}, now=1000.0)
        assert "Free" in marker and "(expired)" in marker

    def test_age_only_once_stale(self):
        from claude_swap.switcher import _plan_marker

        fresh = _plan_marker({"plan": TEAM, "fetchedAt": time.time()}, now=time.time())
        old_at = time.time() - 3 * subscription.PLAN_TTL_S
        stale = _plan_marker({"plan": TEAM, "fetchedAt": old_at}, now=time.time())
        assert "ago" not in fresh
        assert "ago" in stale

    def test_no_record_no_marker(self):
        from claude_swap.switcher import _plan_marker

        assert _plan_marker(None, now=0.0) == ""


class TestSnapshotField:
    def test_appended_after_policy(self):
        names = [f.name for f in dataclasses.fields(AccountSnapshot)]
        assert names[-1] == "subscription_plan"
        assert names[names.index("subscription_plan") - 1] == "policy"

    def test_defaults_to_none(self):
        from claude_swap.usage_store import UsageEntry

        snap = AccountSnapshot(
            number="1",
            email="a@x",
            org_name="",
            org_uuid="",
            is_active=True,
            kind="oauth",
            switchable=True,
            usage=UsageEntry(),
        )
        assert snap.subscription_plan is None


def _snap(**kw) -> AccountSnapshot:
    from claude_swap.usage_store import UsageEntry

    base = {
        "number": "1",
        "email": "a@x",
        "org_name": "",
        "org_uuid": "",
        "is_active": True,
        "kind": "oauth",
        "switchable": True,
        "usage": UsageEntry(),
    }
    base.update(kw)
    return AccountSnapshot(**base)


class _Palette:
    muted = "dim"
    sev_warn = "yellow"


class TestTuiMarker:
    def _render(self, record) -> str:
        from rich.text import Text

        from claude_swap.tui.widgets import _append_plan

        text = Text()
        _append_plan(text, _snap(subscription_plan=record), "  ", _Palette())
        return text.plain

    def test_label(self):
        assert self._render({"plan": TEAM, "fetchedAt": time.time()}) == "  · Team"

    def test_expired(self):
        rendered = self._render({"plan": LAPSED, "fetchedAt": time.time()})
        assert "Free" in rendered and "(expired)" in rendered

    def test_stale_age_uses_the_same_wording_as_list(self):
        """12h old is the first stale moment — it must not read as "0d ago"."""
        old_at = time.time() - 2 * subscription.PLAN_TTL_S - 60
        rendered = self._render({"plan": TEAM, "fetchedAt": old_at})
        assert "(12h ago)" in rendered

    def test_nothing_without_a_record(self):
        assert self._render(None) == ""
