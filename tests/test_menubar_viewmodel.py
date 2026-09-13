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

from claude_swap.json_output import (
    USAGE_API_KEY,
    USAGE_FOREIGN_CREDENTIAL,
    USAGE_KEYCHAIN_UNAVAILABLE,
    USAGE_RELOGIN_REQUIRED,
    USAGE_TOKEN_EXPIRED,
)
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


class TestAccountStatus:
    """The redesign's status vocabulary: sentinels are not all dead logins."""

    def test_healthy_account_is_ok(self) -> None:
        vm = viewmodel.build(snapshot(account()), now=NOW)
        assert vm["accounts"][0]["status"] == "ok"

    def test_api_key_account(self) -> None:
        acc = account(number="2", is_active=False,
                      usage=UsageEntry(sentinel=USAGE_API_KEY, last_good=None))
        vm = viewmodel.build(snapshot(account(), acc), now=NOW)
        assert vm["accounts"][1]["status"] == "api-key"

    def test_relogin_account(self) -> None:
        acc = account(number="2", is_active=False,
                      usage=UsageEntry(sentinel=USAGE_RELOGIN_REQUIRED, last_good=None))
        vm = viewmodel.build(snapshot(account(), acc), now=NOW)
        assert vm["accounts"][1]["status"] == "needs-login"

    @pytest.mark.parametrize("sentinel", [
        USAGE_TOKEN_EXPIRED, USAGE_KEYCHAIN_UNAVAILABLE, USAGE_FOREIGN_CREDENTIAL,
    ])
    def test_transient_sentinels_are_unavailable(self, sentinel) -> None:
        acc = account(number="3", is_active=False,
                      usage=UsageEntry(sentinel=sentinel, last_good=None))
        vm = viewmodel.build(snapshot(account(), acc), now=NOW)
        assert vm["accounts"][1]["status"] == "unavailable"

    def test_unknown_sentinel_defaults_to_unavailable(self) -> None:
        acc = account(number="3", is_active=False,
                      usage=UsageEntry(sentinel="mystery", last_good=None))
        vm = viewmodel.build(snapshot(account(), acc), now=NOW)
        assert vm["accounts"][1]["status"] == "unavailable"

    def test_error_without_measurement_is_unavailable(self) -> None:
        # A fetch failure with nothing ever measured must not read "ok" —
        # the panel would call it a fresh account awaiting first data.
        acc = account(number="2", is_active=False,
                      usage=UsageEntry(last_good=None, last_error="http 429"))
        vm = viewmodel.build(snapshot(account(), acc), now=NOW)
        assert vm["accounts"][1]["status"] == "unavailable"
        assert vm["accounts"][1]["note"] == "http 429"

    def test_error_with_last_good_stays_ok(self) -> None:
        # Stale-on-error keeps measured windows; status stays "ok" and the
        # stale state carries the failure semantics instead.
        acc = account(usage=UsageEntry(last_good=usage_fixture(),
                                       fetched_at=NOW - 120, age_s=120.0,
                                       last_error="http 429"))
        vm = viewmodel.build(snapshot(acc), now=NOW)
        assert vm["accounts"][0]["status"] == "ok"

    def test_active_sentinel_freshness_shape(self) -> None:
        # active + sentinel: freshness is exactly {ok: False} — no age or
        # error keys (the API-key case the banner must NOT light up for)
        acc = account(usage=UsageEntry(sentinel=USAGE_API_KEY, last_good=None))
        vm = viewmodel.build(snapshot(acc), now=NOW)
        assert vm["freshness"] == {"ok": False}

    def test_active_error_without_measurement_shape(self) -> None:
        acc = account(usage=UsageEntry(last_good=None, last_error="http 429"))
        vm = viewmodel.build(snapshot(acc), now=NOW)
        assert vm["accounts"][0]["status"] == "unavailable"
        assert vm["freshness"] == {"ok": False, "error": "http 429"}

    def test_ahead_of_pace_false_pin(self) -> None:
        # only the true case was pinned; false is the other wire value
        usage = usage_fixture()
        usage["seven_day"] = _win(10.0, NOW + 5.5 * 86400)  # far under pace
        vm = viewmodel.build(
            snapshot(account(usage=UsageEntry(last_good=usage, fetched_at=NOW - 120))),
            now=NOW)
        assert vm["accounts"][0]["pace"]["aheadOfPace"] is False

    def test_status_always_present_and_quarantined_unchanged(self) -> None:
        acc = account(number="2", is_active=False,
                      usage=UsageEntry(sentinel=USAGE_RELOGIN_REQUIRED, last_good=None))
        vm = viewmodel.build(snapshot(account(), acc), now=NOW)
        # status is the display key now; quarantined stays on the wire (compat)
        assert vm["accounts"][1]["status"] == "needs-login"
        assert vm["accounts"][1]["quarantined"] is True


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
        assert "pace" not in vm["accounts"][0]  # hidden on fetch-error too

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

    def test_passed_weekly_window_keeps_measured_value(self) -> None:
        """Redesign rule: a passed reset must not fabricate a zero — the
        panel keeps the last measured pct, marked stale, awaiting a fresh
        measurement. (CLI/TUI keep the legacy roll-to-zero display.)"""
        stale_weekly = usage_fixture()
        stale_weekly["seven_day"] = _win(95.0, NOW - 86400)  # reset passed
        vm = viewmodel.build(
            snapshot(account(usage=UsageEntry(last_good=stale_weekly))), now=NOW
        )
        seven = vm["accounts"][0]["windows"][1]
        assert seven["pct"] == 95.0  # measured value preserved
        assert seven["state"] == "stale"
        assert seven["countdownText"] == "Awaiting updated usage"
        assert "resetsAt" not in seven  # nothing left to count down to

    def test_passed_five_hour_window_awaits_too(self) -> None:
        stale_5h = usage_fixture()
        stale_5h["five_hour"] = _win(72.0, NOW - 600)
        vm = viewmodel.build(
            snapshot(account(usage=UsageEntry(last_good=stale_5h))), now=NOW
        )
        five = vm["accounts"][0]["windows"][0]
        assert five["pct"] == 72.0
        assert five["state"] == "stale"
        assert five["countdownText"] == "Awaiting updated usage"

    def test_passed_scoped_window_keeps_measured_value(self) -> None:
        usage = usage_fixture()
        usage["scoped"] = [_win(84.0, NOW - 7200, name="Fable")]
        vm = viewmodel.build(
            snapshot(account(usage=UsageEntry(last_good=usage))), now=NOW
        )
        fable = [w for w in vm["accounts"][0]["windows"]
                 if w["kind"] == "model:Fable"][0]
        assert fable["pct"] == 84.0
        assert fable["state"] == "stale"
        assert fable["countdownText"] == "Awaiting updated usage"
        assert "resetsAt" not in fable

    def test_pace_hidden_when_measurement_stale(self) -> None:
        stale_weekly = usage_fixture()
        stale_weekly["seven_day"] = _win(95.0, NOW - 3600)
        vm = viewmodel.build(
            snapshot(account(usage=UsageEntry(last_good=stale_weekly))), now=NOW
        )
        assert "pace" not in vm["accounts"][0]

    def test_future_windows_unchanged_by_rule(self) -> None:
        vm = viewmodel.build(snapshot(account()), now=NOW)
        five = vm["accounts"][0]["windows"][0]
        assert five["state"] == "ok"
        assert five["countdownText"] == "2h 14m"
        assert "pace" in vm["accounts"][0]


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


class TestCardFieldContract:
    """The Account card renders Alias/Email/Team/Account Index for every
    account. The row values must be non-null strings (the panel falls back
    to \"Not available\" for empty ones), and alias must be absent — never
    null — when unset, per the additive-schema rule."""

    def _acct_vms(self) -> list[dict]:
        healthy = account(alias="work", org_name="Acme")
        no_alias = account(number="2", is_active=False)
        api_key = account(
            number="3",
            is_active=False,
            usage=UsageEntry(sentinel=USAGE_API_KEY),
        )
        held_out = account(number="4", is_active=False, disabled=True)
        vm = viewmodel.build(
            snapshot(healthy, no_alias, api_key, held_out), now=NOW
        )
        return vm["accounts"]

    def test_card_rows_are_never_null(self) -> None:
        for acct in self._acct_vms():
            for field in ("slot", "email", "org"):
                value = acct[field]
                assert isinstance(value, str) and value != "", (
                    f"{field} must be a non-empty string, got {value!r}"
                )

    def test_alias_absent_when_unset_never_null(self) -> None:
        vms = self._acct_vms()
        assert vms[0]["alias"] == "work"
        for acct in vms[1:]:
            assert "alias" not in acct, "unset alias must be absent, not null"


class TestContract:
    """Pin the additive JSON contract: removals fail loudly, additions are deliberate."""

    ALLOWED_TOP = {
        "schemaVersion", "activeSlot", "takenAt", "freshness",
        "accounts", "autoSwitch", "history",
    }
    ALLOWED_ACCOUNT = {
        "slot", "label", "email", "org", "kind", "active", "switchable", "status",
        "windows", "alias", "disabled", "quarantined", "note", "spend", "pace",
        "lastError", "timelineWindows",
    }
    ALLOWED_WINDOW = {
        "kind", "label", "pct", "state",
        "resetsAt", "countdownText", "note",
    }
    REQUIRED_ACCOUNT = {"slot", "label", "email", "org", "kind", "active",
                        "switchable", "status", "windows"}
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
        # activeSlot is optional (absent when no active account)
        assert self.ALLOWED_TOP - {"history", "autoSwitch", "activeSlot"} <= set(vm)
        for acct in vm["accounts"]:
            assert self.REQUIRED_ACCOUNT <= set(acct)
            assert set(acct) <= self.ALLOWED_ACCOUNT
            for w in acct["windows"]:
                assert self.REQUIRED_WINDOW <= set(w)
                assert set(w) <= self.ALLOWED_WINDOW

    def test_empty_snapshot(self) -> None:
        vm = viewmodel.build(snapshot(), now=NOW)
        assert vm["accounts"] == []
        assert "activeSlot" not in vm  # additive: absent, never null
        assert vm["freshness"]["ok"] is False
        assert "ageText" not in vm["freshness"]

    def test_nonfinite_pct_rows_are_skipped(self) -> None:
        bad = usage_fixture()
        bad["five_hour"] = _win(float("nan"), NOW + 3600)
        vm = viewmodel.build(
            snapshot(account(usage=UsageEntry(last_good=bad))), now=NOW
        )
        kinds = [w["kind"] for w in vm["accounts"][0]["windows"]]
        assert "5h" not in kinds  # NaN never reaches the wire
        assert "7d" in kinds

    def test_spend_without_limit_omits_the_key(self) -> None:
        usage = usage_fixture()
        usage["spend"] = {"used": 5.0, "pct": 5.0, "currency": "USD"}
        vm = viewmodel.build(
            snapshot(account(usage=UsageEntry(last_good=usage))), now=NOW
        )
        spend = vm["accounts"][0]["spend"]
        assert "limit" not in spend  # absent, never null
        assert spend["used"] == 5.0

    def test_vm_serializes_as_strict_json(self) -> None:
        import json as _json

        vm = viewmodel.build(snapshot(account()), now=NOW)
        _json.dumps(vm, allow_nan=False)  # raises on NaN/Infinity leaks


class TestAgeText:
    def test_ages(self) -> None:
        assert viewmodel._age_text(None) is None
        assert viewmodel._age_text(0.0) == "just now"
        assert viewmodel._age_text(30.0) == "just now"
        assert viewmodel._age_text(120.0) == "2m ago"
        assert viewmodel._age_text(5400.0) == "1h 30m ago"
