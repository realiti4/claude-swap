"""Tests for the menubar panel's pure view-model builder.

``viewmodel.build()`` turns an ``AccountsSnapshot`` (plus auto-switch
settings and switch history) into the additive schemaVersion-1 JSON the
WKWebView panel renders. Everything here is pure — no PyObjC, no network,
no filesystem — mirroring the legacy helper tests in ``test_menubar.py``.
"""

from __future__ import annotations

import datetime as _dt
from datetime import datetime, timezone

import pytest

from claude_swap.json_output import USAGE_API_KEY, USAGE_TOKEN_EXPIRED
from claude_swap.menubar import viewmodel
from claude_swap.models import AccountSnapshot, AccountsSnapshot
from claude_swap.switcher import SENTINEL_NOTES
from claude_swap.usage_store import UsageEntry

NOW = 1_800_000_000.0


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _win(pct: float, resets_at: float, name: str | None = None) -> dict:
    d = {"pct": pct, "resets_at": _iso(resets_at)}
    if name is not None:
        d["name"] = name
    return d


def usage_fixture() -> dict:
    """A healthy last-good measurement: 5h/7d/Fable windows + spend."""
    return {
        "five_hour": _win(68.4, NOW + 8040),  # resets in 2h 14m
        "seven_day": _win(41.0, NOW + 5.5 * 86400),  # 1.5d elapsed -> ~21% expected
        "scoped": [_win(84.0, NOW + 2 * 86400, name="Fable")],
        "spend": {
            "used": 12.4,
            "limit": 100.0,
            "pct": 12.4,
            "currency": "USD",
            "resets_at": _iso(NOW + 3 * 86400),
        },
    }


def account(
    number: str = "1",
    email: str = "hungtrv@gmail.com",
    *,
    is_active: bool = True,
    usage: UsageEntry | None = None,
    alias: str = "",
    disabled: bool = False,
    kind: str = "oauth",
    org_name: str = "",
) -> AccountSnapshot:
    if usage is None:
        usage = UsageEntry(last_good=usage_fixture(), fetched_at=NOW - 120, age_s=120.0)
    return AccountSnapshot(
        number=number,
        email=email,
        org_name=org_name,
        org_uuid="u1",
        is_active=is_active,
        kind=kind,
        switchable=True,
        usage=usage,
        alias=alias,
        disabled=disabled,
    )


def snapshot(*accounts: AccountSnapshot, active: str | None = None) -> AccountsSnapshot:
    if active is None and accounts:
        active = next((a.number for a in accounts if a.is_active), None)
    return AccountsSnapshot(active_number=active, accounts=accounts, taken_at=NOW)


class TestHealthyAccount:
    def test_top_level_shape(self) -> None:
        vm = viewmodel.build(snapshot(account()), now=NOW)
        assert set(vm) == {
            "schemaVersion",
            "activeSlot",
            "takenAt",
            "freshness",
            "accounts",
            "autoSwitch",
            "history",
        }
        assert vm["schemaVersion"] == 1
        assert vm["activeSlot"] == "1"
        assert vm["takenAt"] == NOW

    def test_account_identity_fields(self) -> None:
        vm = viewmodel.build(
            snapshot(account(alias="Work", org_name="Acme Corp")), now=NOW
        )
        acct = vm["accounts"][0]
        assert acct["slot"] == "1"
        assert acct["label"] == "hungtrv"  # local part, untruncated
        assert acct["alias"] == "Work"
        assert acct["org"] == "Acme Corp"
        assert acct["kind"] == "oauth"
        assert acct["active"] is True
        assert acct["switchable"] is True
        assert "disabled" not in acct  # additive: absent when False
        assert "quarantined" not in acct

    def test_windows_with_live_countdown(self) -> None:
        vm = viewmodel.build(snapshot(account()), now=NOW)
        windows = vm["accounts"][0]["windows"]
        assert [w["kind"] for w in windows] == ["5h", "7d", "model:Fable"]
        five_h = windows[0]
        assert five_h["label"] == "5-hour"
        assert five_h["pct"] == pytest.approx(68.4)
        assert five_h["resetsAt"] == pytest.approx(NOW + 8040)
        assert five_h["countdownText"] == "2h 14m"
        assert five_h["state"] == "ok"
        assert windows[1]["label"] == "7-day"
        assert windows[2]["label"] == "Fable"

    def test_spend_block(self) -> None:
        vm = viewmodel.build(snapshot(account()), now=NOW)
        spend = vm["accounts"][0]["spend"]
        assert spend == {
            "used": 12.4,
            "limit": 100.0,
            "pct": pytest.approx(12.4),
            "currency": "USD",
            "resetsAt": pytest.approx(NOW + 3 * 86400),
        }

    def test_pace_on_seven_day(self) -> None:
        vm = viewmodel.build(snapshot(account()), now=NOW)
        pace = vm["accounts"][0]["pace"]
        # 1.5d elapsed of 7d -> ~21.4% expected; 41% actual -> ahead
        assert pace["expectedPct"] == pytest.approx(100 * 1.5 / 7, abs=0.5)
        assert pace["aheadOfPace"] is True

    def test_freshness_from_active_age(self) -> None:
        vm = viewmodel.build(snapshot(account()), now=NOW)
        assert vm["freshness"] == {"ageText": "2m ago", "ok": True}


class TestDegradedStates:
    def test_sentinel_account_is_quarantined_without_bars(self) -> None:
        acc = account(
            number="2",
            is_active=False,
            usage=UsageEntry(sentinel=USAGE_TOKEN_EXPIRED, last_good=None, age_s=None),
        )
        vm = viewmodel.build(snapshot(account(), acc), now=NOW)
        quarantined = vm["accounts"][1]
        assert quarantined["quarantined"] is True
        assert quarantined["note"] == SENTINEL_NOTES[USAGE_TOKEN_EXPIRED]
        assert quarantined["windows"] == []
        assert "spend" not in quarantined
        assert "pace" not in quarantined

    def test_no_measurement_note(self) -> None:
        acc = account(
            number="3",
            is_active=False,
            usage=UsageEntry(last_good=None, last_error="http 429", age_s=None),
        )
        vm = viewmodel.build(snapshot(account(), acc), now=NOW)
        errored = vm["accounts"][1]
        assert errored["windows"] == []
        assert "quarantined" not in errored
        assert errored["note"] == "http 429"

    def test_stale_on_error_keeps_windows(self) -> None:
        acc = account(
            usage=UsageEntry(
                last_good=usage_fixture(),
                fetched_at=NOW - 3600,
                age_s=3600.0,
                last_error="http 429",
            )
        )
        vm = viewmodel.build(snapshot(acc), now=NOW)
        windows = vm["accounts"][0]["windows"]
        assert windows and all(w["state"] == "stale" for w in windows)
        assert vm["freshness"]["ok"] is False
        assert vm["freshness"]["error"] == "http 429"
        assert vm["freshness"]["ageText"] == "1h ago"

    def test_malformed_spend_is_skipped_not_fatal(self) -> None:
        # A persisted spend dict missing used/limit (shape drift across an
        # upgrade) must not crash build() for every account.
        usage = usage_fixture()
        usage["spend"] = {"pct": 12.4, "currency": "USD"}  # pct-only
        vm = viewmodel.build(snapshot(account(usage=UsageEntry(last_good=usage))), now=NOW)
        assert "spend" not in vm["accounts"][0]

        usage["spend"] = {"used": "12.4", "limit": 100.0, "pct": 12.4}  # wrong type
        vm = viewmodel.build(snapshot(account(usage=UsageEntry(last_good=usage))), now=NOW)
        assert "spend" not in vm["accounts"][0]

    def test_rolled_weekly_window_zeroed(self) -> None:
        stale_weekly = usage_fixture()
        stale_weekly["seven_day"] = _win(95.0, NOW - 86400)  # reset passed
        vm = viewmodel.build(
            snapshot(account(usage=UsageEntry(last_good=stale_weekly))), now=NOW
        )
        seven = vm["accounts"][0]["windows"][1]
        assert seven["pct"] == 0.0
        assert seven["resetsAt"] > NOW  # advanced to next boundary


class TestAutoSwitchAndHistory:
    def test_defaults(self) -> None:
        vm = viewmodel.build(snapshot(account()), now=NOW)
        assert vm["autoSwitch"] == {
            "enabled": False,
            "thresholdPct": 90.0,
            "strategy": "best",
        }
        assert vm["history"] == []

    def test_enabled_with_last_event(self) -> None:
        vm = viewmodel.build(
            snapshot(account()),
            auto_enabled=True,
            auto_threshold=80.0,
            auto_strategy="consume-first",
            history=["2 → 1 · just now", "1 → 2 · 1h ago"],
            now=NOW,
        )
        assert vm["autoSwitch"] == {
            "enabled": True,
            "thresholdPct": 80.0,
            "strategy": "consume-first",
            "lastEventText": "2 → 1 · just now",
        }
        assert vm["history"] == ["2 → 1 · just now", "1 → 2 · 1h ago"]


class TestContract:
    """Pin the additive JSON contract: removals fail loudly, additions are deliberate."""

    ALLOWED_TOP = {
        "schemaVersion", "activeSlot", "takenAt", "freshness",
        "accounts", "autoSwitch", "history",
    }
    ALLOWED_ACCOUNT = {
        "slot", "label", "email", "org", "kind", "active", "switchable", "windows",
        "alias", "disabled", "quarantined", "note", "spend", "pace", "lastError",
    }
    ALLOWED_WINDOW = {
        "kind", "label", "pct", "state",
        "resetsAt", "countdownText", "note",
    }
    REQUIRED_ACCOUNT = {"slot", "label", "email", "org", "kind", "active", "switchable", "windows"}
    REQUIRED_WINDOW = {"kind", "label", "pct", "state"}

    def test_keys_neither_removed_nor_accidentally_added(self) -> None:
        sentinel = account(
            number="2",
            is_active=False,
            usage=UsageEntry(sentinel=USAGE_API_KEY, last_good=None),
        )
        vm = viewmodel.build(
            snapshot(account(), sentinel),
            auto_enabled=True,
            history=["ev"],
            now=NOW,
        )
        assert set(vm) <= self.ALLOWED_TOP
        assert self.ALLOWED_TOP - {"history", "autoSwitch"} <= set(vm)
        for acct in vm["accounts"]:
            assert self.REQUIRED_ACCOUNT <= set(acct)
            assert set(acct) <= self.ALLOWED_ACCOUNT
            for w in acct["windows"]:
                assert self.REQUIRED_WINDOW <= set(w)
                assert set(w) <= self.ALLOWED_WINDOW

    def test_empty_snapshot(self) -> None:
        vm = viewmodel.build(snapshot(), now=NOW)
        assert vm["accounts"] == []
        assert vm["activeSlot"] is None
        assert vm["freshness"]["ok"] is False
        assert "ageText" not in vm["freshness"]


class TestAgeText:
    def test_ages(self) -> None:
        assert viewmodel._age_text(None) is None
        assert viewmodel._age_text(0.0) == "just now"
        assert viewmodel._age_text(30.0) == "just now"
        assert viewmodel._age_text(120.0) == "2m ago"
        assert viewmodel._age_text(5400.0) == "1h 30m ago"
