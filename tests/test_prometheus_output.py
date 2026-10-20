"""Tests for ``list --prometheus`` (Prometheus text-format output)."""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_swap import cli, prometheus_output
from claude_swap.exceptions import ConfigError
from claude_swap.json_output import (
    USAGE_API_KEY,
    USAGE_FOREIGN_CREDENTIAL,
    USAGE_KEYCHAIN_UNAVAILABLE,
    USAGE_NO_CREDENTIALS,
    USAGE_RELOGIN_REQUIRED,
    USAGE_TOKEN_EXPIRED,
    account_row,
)
from claude_swap.poll_policy import parse_reset_ts

_SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")

# Fixed measurement time (whole second, so usageFetchedAt round-trips exactly).
_FETCHED = 1_760_000_000.0

# The pinned families, in order, with their verbatim HELP text.
_FAMILIES = [
    ("cswap_account_info",
     "Managed account (1 per slot); account is the slot number, alias is empty when unset."),
    ("cswap_account_active", "1 for the active account, else 0."),
    ("cswap_account_disabled",
     "1 when the account is held out of rotation (cswap disable), else 0."),
    ("cswap_usage_up", "1 when the account has current usage (usageStatus ok), else 0."),
    ("cswap_usage_status",
     "1 for the account's current usageStatus (as in list --json), 0 for the other known states."),
    ("cswap_usage_ratio", "Share of the window's quota used, from 0 to 1."),
    ("cswap_usage_expected_ratio",
     "Share of the weekly quota that would be used by now at an even pace, from 0 to 1."),
    ("cswap_usage_reset_timestamp_seconds", "When the window's quota resets, as a Unix timestamp."),
    ("cswap_usage_fetched_timestamp_seconds",
     "When the usage measurement was taken, as a Unix timestamp."),
    ("cswap_login_expiry_timestamp_seconds",
     "When the stored login expires and needs a new /login, as a Unix timestamp."),
]
_NAMES = [name for name, _ in _FAMILIES]

_STATUSES = (
    "ok", "token_expired", "api_key", "keychain_unavailable",
    "relogin_required", "foreign_credential", "no_credentials", "unavailable",
)

_NAME_RE = r"[a-zA-Z_:][a-zA-Z0-9_:]*"
_LABEL_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_SAMPLE_RE = re.compile(
    r"^(?P<name>" + _NAME_RE + r")"
    r"(?:\{(?P<labels>.*)\})? "
    r"(?P<value>[-+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][-+]?[0-9]+)?)$"
)
_LABEL_PAIR_RE = re.compile(r'([^=,{}"\s]+)="((?:[^"\\]|\\.)*)"')
_UNESCAPES = {"\\": "\\", '"': '"', "n": "\n"}


def _unescape(text: str) -> str:
    return re.sub(r"\\(.)", lambda m: _UNESCAPES.get(m.group(1), m.group(0)), text)


def _label_pairs(raw: str | None) -> list[tuple[str, str]]:
    """Strictly split a label block into (name, unescaped value) pairs."""
    if not raw:
        return []
    pairs = []
    pos = 0
    while pos < len(raw):
        m = _LABEL_PAIR_RE.match(raw, pos)
        assert m, f"bad label block: {raw!r}"
        pairs.append((m.group(1), _unescape(m.group(2))))
        pos = m.end()
        if pos < len(raw):
            assert raw[pos] == ",", f"bad label separator in: {raw!r}"
            pos += 1
    return pairs


def _parse_page(page: str) -> dict:
    """Page -> {(name, frozenset(labels.items())): float}, strict per sample line."""
    out: dict = {}
    for line in page.split("\n"):
        if not line or line.startswith("#"):
            continue
        m = _SAMPLE_RE.match(line)
        assert m, f"not a sample line: {line!r}"
        key = (m.group("name"), frozenset(_label_pairs(m.group("labels"))))
        assert key not in out, f"duplicate series: {line!r}"
        out[key] = float(m.group("value"))
    return out


def _comment_lines(page: str) -> list[str]:
    return [line for line in page.split("\n") if line.startswith("# HELP ") or line.startswith("# TYPE ")]


def _get(parsed: dict, name: str, **labels: str) -> float | None:
    return parsed.get((name, frozenset(labels.items())))


def _series(parsed: dict, name: str) -> list[dict]:
    return [dict(k[1]) for k in parsed if k[0] == name]


def _iso(delta: timedelta, base: float = _FETCHED) -> str:
    return (datetime.fromtimestamp(base, tz=timezone.utc) + delta).isoformat().replace("+00:00", "Z")


def _usage(five: float = 25.0, seven: float = 60.0, scoped: list | None = None) -> dict:
    out = {
        "five_hour": {"pct": five, "resets_at": _iso(timedelta(hours=2))},
        "seven_day": {"pct": seven, "resets_at": _iso(timedelta(days=3))},
    }
    if scoped:
        out["scoped"] = scoped
    return out


def _row(number: int, entry, **kw) -> dict:
    kw.setdefault("usage_fetched_at", _FETCHED)
    return account_row(
        number, f"user{number}@example.com", "", "", kw.pop("active", False), entry, **kw
    )


def _payload(rows: list[dict], active: int | None = None) -> dict:
    return {"schemaVersion": 1, "activeAccountNumber": active, "accounts": rows}


def _rich_payload() -> dict:
    rows = [
        _row(
            1,
            _usage(scoped=[
                {"name": "Fable", "pct": 50.0, "resets_at": _iso(timedelta(days=3))},
                {"name": "Opus 4.1", "pct": 10.0, "resets_at": _iso(timedelta(days=3))},
            ]),
            alias='a"b\\c\nd',
            login_expires_at="2026-11-01T00:00:00Z",
        ),
        _row(2, _usage(5.0, 5.0), alias="Équipe café", active=True, disabled=True),
        _row(3, USAGE_TOKEN_EXPIRED, login_expires_at="2026-10-01T00:00:00Z"),
        _row(4, USAGE_API_KEY),
        _row(5, USAGE_KEYCHAIN_UNAVAILABLE),
        _row(6, USAGE_RELOGIN_REQUIRED),
        _row(7, USAGE_FOREIGN_CREDENTIAL),
        _row(8, USAGE_NO_CREDENTIALS),
        _row(9, None, last_error="http-429"),
    ]
    return _payload(rows, active=2)


def _run_cli(argv: list[str], payload=None, side_effect=None):
    """Run cli.main() with a mocked switcher; returns (switcher_cls, update_mock)."""
    update = MagicMock(return_value=None)
    with patch("claude_swap.cli.ClaudeAccountSwitcher") as switcher_cls, \
         patch.object(sys, "argv", argv), \
         patch("os.geteuid", return_value=1000, create=True), \
         patch("claude_swap.update_check.check_for_update", update):
        if side_effect is not None:
            switcher_cls.return_value.list_accounts.side_effect = side_effect
        else:
            switcher_cls.return_value.list_accounts.return_value = payload
        try:
            cli.main()
        finally:
            run_state = (switcher_cls, update)
    return run_state


class TestRender:
    def test_prometheus_two_accounts_ok(self):
        payload = _payload([
            _row(1, _usage(25.0, 60.0), alias="work", login_expires_at="2026-11-01T00:00:00Z"),
            _row(2, _usage(10.0, 20.0), active=True),
        ], active=2)
        page = prometheus_output.render(payload)
        p = _parse_page(page)

        assert _get(p, "cswap_account_info", account="1", alias="work") == 1
        assert _get(p, "cswap_account_info", account="2", alias="") == 1
        assert _get(p, "cswap_account_active", account="1") == 0
        assert _get(p, "cswap_account_active", account="2") == 1
        assert _get(p, "cswap_account_disabled", account="1") == 0
        assert _get(p, "cswap_usage_up", account="1") == 1
        assert _get(p, "cswap_usage_up", account="2") == 1
        assert _get(p, "cswap_usage_ratio", account="1", window="five_hour") == pytest.approx(0.25)
        assert _get(p, "cswap_usage_ratio", account="1", window="seven_day") == pytest.approx(0.60)
        assert _get(p, "cswap_usage_ratio", account="2", window="five_hour") == pytest.approx(0.10)
        assert _get(p, "cswap_usage_ratio", account="2", window="seven_day") == pytest.approx(0.20)
        assert _get(p, "cswap_usage_reset_timestamp_seconds", account="1", window="five_hour") \
            == pytest.approx(_FETCHED + 2 * 3600)
        assert _get(p, "cswap_usage_reset_timestamp_seconds", account="1", window="seven_day") \
            == pytest.approx(_FETCHED + 3 * 86400)
        assert _get(p, "cswap_usage_fetched_timestamp_seconds", account="1") == pytest.approx(_FETCHED)
        assert _get(p, "cswap_usage_fetched_timestamp_seconds", account="2") == pytest.approx(_FETCHED)
        assert _get(p, "cswap_login_expiry_timestamp_seconds", account="1") == pytest.approx(
            parse_reset_ts("2026-11-01T00:00:00Z")
        )
        assert _get(p, "cswap_login_expiry_timestamp_seconds", account="2") is None

    def test_prometheus_no_email_or_org_anywhere(self):
        rows = [
            account_row(
                1, "secret.person@example.com", "Acme Secret Org",
                "11111111-2222-3333-4444-555555555555", True, _usage(),
                usage_fetched_at=_FETCHED, alias="work",
            ),
            account_row(
                2, "secret.person@example.com", "Acme Secret Org",
                "11111111-2222-3333-4444-555555555555", False, None,
                usage_fetched_at=_FETCHED, last_error="http-429",
            ),
        ]
        assert rows[1]["usageStatus"] == "unavailable"
        payload = _payload(rows, active=1)
        payload["duplicateAccountWarnings"] = ["leak@example.com appears twice"]
        payload["lockstepUsageWarnings"] = ["leak@example.com in lockstep"]
        payload["unclaimedCredentials"] = [{"email": "leak@example.com"}]
        page = prometheus_output.render(payload)

        for needle in (
            "secret.person@example.com", "leak@example.com", "Acme",
            "11111111-2222-3333-4444-555555555555", "@",
        ):
            assert needle not in page
        assert "http-429" not in page

    def test_prometheus_label_escaping_quote_backslash_newline(self):
        """Defence in depth: the CLI normalises aliases to [a-z0-9_.-] (models.py),
        so only a hand-edited sequence.json can carry quote, backslash or newline."""
        alias = 'a"b\\c' + "\n" + "d"
        page = prometheus_output.render(_payload([_row(1, _usage(), alias=alias)]))
        assert 'alias="a\\"b\\\\c\\nd"' in page
        assert "\n" not in page.split("# TYPE cswap_account_info gauge\n", 1)[1].split("\n", 1)[0]
        assert _get(_parse_page(page), "cswap_account_info", account="1", alias=alias) == 1

    def test_prometheus_unicode_alias(self, monkeypatch):
        page = prometheus_output.render(_payload([_row(1, _usage(), alias="Équipe café")]))
        assert 'alias="Équipe café"' in page
        # Through a console that is not UTF-8, the page still goes out as UTF-8.
        raw = io.BytesIO()
        monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="cp1252"))
        prometheus_output.write(page)
        assert 'alias="Équipe café"'.encode("utf-8") in raw.getvalue()

    def test_prometheus_overflow_and_lone_surrogate_never_raise(self, monkeypatch):
        """Values no real payload carries still drop one sample, never the page."""
        huge = _row(2, {"five_hour": {"pct": 1.0}})
        huge["usage"]["fiveHour"]["pct"] = 10**400
        bad_number = _row(3, _usage())
        bad_number["number"] = 10**400
        rows = [_row(1, _usage(), alias="x\ud800y"), huge, bad_number]
        page = prometheus_output.render(_payload(rows))
        p = _parse_page(page)
        assert _get(p, "cswap_usage_ratio", account="2", window="five_hour") is None
        assert _get(p, "cswap_usage_up", account="2") == 1
        assert {s["account"] for s in _series(p, "cswap_account_info")} == {"1", "2"}
        raw = io.BytesIO()
        monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="utf-8"))
        prometheus_output.write(page)
        assert b'alias="x?y"' in raw.getvalue()

    def test_prometheus_token_expired_row_has_up_zero_and_status(self):
        payload = _payload([_row(1, USAGE_TOKEN_EXPIRED, login_expires_at="2026-11-01T00:00:00Z")])
        p = _parse_page(prometheus_output.render(payload))
        assert _get(p, "cswap_usage_up", account="1") == 0
        for status in _STATUSES:
            expected = 1 if status == "token_expired" else 0
            assert _get(p, "cswap_usage_status", account="1", status=status) == expected, status
        assert _series(p, "cswap_usage_ratio") == []
        assert _get(p, "cswap_login_expiry_timestamp_seconds", account="1") == pytest.approx(
            parse_reset_ts("2026-11-01T00:00:00Z")
        )

    def test_prometheus_null_usage_no_ratio_rows(self):
        row = _row(1, None, last_good_usage=_usage(), last_error="http-429")
        assert row["usage"] is None and "lastGoodUsage" in row
        p = _parse_page(prometheus_output.render(_payload([row])))
        assert _get(p, "cswap_usage_up", account="1") == 0
        assert _get(p, "cswap_usage_status", account="1", status="unavailable") == 1
        for name in (
            "cswap_usage_ratio", "cswap_usage_expected_ratio",
            "cswap_usage_reset_timestamp_seconds", "cswap_usage_fetched_timestamp_seconds",
        ):
            assert _series(p, name) == [], name

    def test_prometheus_scoped_model_windows(self):
        scoped = [
            {"name": "Fable", "pct": 50.0, "resets_at": _iso(timedelta(days=3))},
            {"name": "Opus 4.1", "pct": 10.0, "resets_at": _iso(timedelta(days=3))},
            {"name": "fable", "pct": 99.0, "resets_at": _iso(timedelta(days=3))},
            {"name": "   ", "pct": 77.0, "resets_at": _iso(timedelta(days=3))},
        ]
        row = _row(1, _usage(scoped=scoped))
        page = prometheus_output.render(_payload([row]))  # duplicate series would assert in _parse_page
        p = _parse_page(page)
        windows = sorted(s["window"] for s in _series(p, "cswap_usage_ratio"))
        assert windows == ["five_hour", "seven_day", "seven_day_fable", "seven_day_opus_4.1"]
        # first of the case-repeat wins
        assert _get(p, "cswap_usage_ratio", account="1", window="seven_day_fable") == pytest.approx(0.5)
        assert _get(p, "cswap_usage_ratio", account="1", window="seven_day_opus_4.1") == pytest.approx(0.1)

    def test_prometheus_disabled_account(self):
        payload = _payload([_row(1, _usage(), disabled=True), _row(2, _usage())])
        p = _parse_page(prometheus_output.render(payload))
        assert _get(p, "cswap_account_disabled", account="1") == 1
        assert _get(p, "cswap_account_disabled", account="2") == 0

    def test_prometheus_zero_accounts_is_valid_empty_output(self):
        page = prometheus_output.render(_payload([]))
        assert page.endswith("\n")
        lines = page.split("\n")[:-1]
        assert len(lines) == 20
        assert all(line.startswith("# HELP ") or line.startswith("# TYPE ") for line in lines)
        expected = []
        for name, help_text in _FAMILIES:
            expected += [f"# HELP {name} {help_text}", f"# TYPE {name} gauge"]
        assert lines == expected

    def test_prometheus_matches_list_json_numbers(self):
        scoped = [{"name": "Fable", "pct": 50.0, "resets_at": _iso(timedelta(days=3))}]
        payload = _payload([
            _row(1, _usage(25.0, 60.0, scoped), login_expires_at="2026-11-01T00:00:00Z"),
            _row(2, _usage(12.5, 33.3), active=True, login_expires_at="2026-12-01T12:30:00Z"),
        ], active=2)
        doc = json.loads(json.dumps(payload))
        p = _parse_page(prometheus_output.render(payload))

        checked_expected = 0
        for acct in doc["accounts"]:
            n = str(acct["number"])
            usage = acct["usage"]
            windows = {"five_hour": usage["fiveHour"], "seven_day": usage["sevenDay"]}
            for s in usage.get("scoped", []):
                windows["seven_day_" + s["name"].lower()] = s
            for label, win in windows.items():
                assert _get(p, "cswap_usage_ratio", account=n, window=label) == pytest.approx(
                    win["pct"] / 100, abs=1e-6)
                assert _get(p, "cswap_usage_reset_timestamp_seconds", account=n, window=label) \
                    == pytest.approx(parse_reset_ts(win["resetsAt"]), abs=1e-6)
                got = _get(p, "cswap_usage_expected_ratio", account=n, window=label)
                if "expectedPct" in win:
                    checked_expected += 1
                    assert got == pytest.approx(win["expectedPct"] / 100, abs=1e-6)
                else:
                    assert got is None
            assert _get(p, "cswap_usage_fetched_timestamp_seconds", account=n) == pytest.approx(
                parse_reset_ts(acct["usageFetchedAt"]), abs=1e-6)
            assert _get(p, "cswap_login_expiry_timestamp_seconds", account=n) == pytest.approx(
                parse_reset_ts(acct["loginExpiresAt"]), abs=1e-6)
        # not vacuous: weekly + scoped windows of acct 1, weekly of acct 2
        assert checked_expected == 3

    def test_prometheus_value_formatting(self):
        scoped = [
            {"name": "Tiny", "pct": 0.001},
            {"name": "Full", "pct": 100},
        ]
        rows = [
            _row(1, {"five_hour": {"pct": 33.3}, "seven_day": {"pct": 50.0}, "scoped": scoped}),
            _row(2, {"five_hour": {"pct": 1.0}}),
            _row(3, {"five_hour": {"pct": 1.0}}),
        ]
        rows[1]["usage"]["fiveHour"]["pct"] = float("inf")
        rows[2]["usage"]["fiveHour"]["pct"] = True
        page = prometheus_output.render(_payload(rows))
        lines = page.split("\n")
        assert 'cswap_usage_ratio{account="1",window="five_hour"} 0.333' in lines
        assert 'cswap_usage_ratio{account="1",window="seven_day"} 0.5' in lines
        assert 'cswap_usage_ratio{account="1",window="seven_day_tiny"} 1e-05' in lines
        assert 'cswap_usage_ratio{account="1",window="seven_day_full"} 1' in lines
        p = _parse_page(page)
        assert _get(p, "cswap_usage_ratio", account="2", window="five_hour") is None
        assert _get(p, "cswap_usage_ratio", account="3", window="five_hour") is None
        # the rows themselves still render
        assert _get(p, "cswap_usage_up", account="2") == 1
        assert _get(p, "cswap_usage_up", account="3") == 1

    def test_prometheus_reset_with_fraction_and_offset(self):
        row = _row(1, {"five_hour": {"pct": 5.0, "resets_at": "2026-10-05T07:00:00.5+10:00"}})
        assert row["usage"]["fiveHour"]["resetsAt"] == "2026-10-05T07:00:00.5+10:00"
        expected = datetime(2026, 10, 4, 21, 0, 0, 500000, tzinfo=timezone.utc).timestamp()
        p = _parse_page(prometheus_output.render(_payload([row])))
        assert _get(p, "cswap_usage_reset_timestamp_seconds", account="1", window="five_hour") \
            == pytest.approx(expected, abs=1e-6)

    def test_prometheus_window_without_resets_at(self):
        row = _row(1, _usage())
        del row["usage"]["sevenDay"]["resetsAt"]
        p = _parse_page(prometheus_output.render(_payload([row])))
        assert _get(p, "cswap_usage_ratio", account="1", window="seven_day") == pytest.approx(0.6)
        assert _get(p, "cswap_usage_reset_timestamp_seconds", account="1", window="seven_day") is None
        assert _get(p, "cswap_usage_reset_timestamp_seconds", account="1", window="five_hour") is not None

    def test_prometheus_duplicate_account_number_first_wins(self):
        payload = _payload([
            _row(1, _usage(10.0, 10.0), alias="first"),
            _row(1, _usage(90.0, 90.0), alias="second"),
        ])
        page = prometheus_output.render(payload)
        p = _parse_page(page)  # asserts series uniqueness
        assert len(_series(p, "cswap_account_info")) == 1
        assert _get(p, "cswap_account_info", account="1", alias="first") == 1
        assert _get(p, "cswap_usage_ratio", account="1", window="five_hour") == pytest.approx(0.1)
        assert 'alias="second"' not in page

    def test_prometheus_no_active_account(self):
        payload = _payload([_row(1, _usage()), _row(2, _usage())], active=None)
        p = _parse_page(prometheus_output.render(payload))
        assert _get(p, "cswap_account_active", account="1") == 0
        assert _get(p, "cswap_account_active", account="2") == 0
        assert len(_series(p, "cswap_account_active")) == 2

    def test_prometheus_spend_not_emitted(self):
        usage = _usage()
        usage["spend"] = {"used": 12.5, "limit": 300.0, "pct": 4.0, "currency": "USD",
                          "resets_at": _iso(timedelta(days=3))}
        row = _row(1, usage)
        assert "spend" in row["usage"]
        page = prometheus_output.render(_payload([row]))
        assert "spend" not in page.lower()

    def test_prometheus_unknown_usage_status_gets_extra_sample(self):
        row = _row(1, _usage())
        row["usageStatus"] = "future_state"
        p = _parse_page(prometheus_output.render(_payload([row])))
        for status in _STATUSES:
            assert _get(p, "cswap_usage_status", account="1", status=status) == 0
        assert _get(p, "cswap_usage_status", account="1", status="future_state") == 1
        assert _get(p, "cswap_usage_up", account="1") == 0

    def test_prometheus_statuses_tuple_is_pinned(self):
        assert prometheus_output.USAGE_STATUSES == _STATUSES

    def test_prometheus_label_names_allowlist(self):
        page = prometheus_output.render(_rich_payload())
        names = set()
        for line in page.split("\n"):
            m = _SAMPLE_RE.match(line)
            if m:
                names.update(k for k, _ in _label_pairs(m.group("labels")))
        assert names
        assert names <= {"account", "alias", "window", "status"}

    def test_prometheus_lines_match_exposition_grammar(self):
        page = prometheus_output.render(_rich_payload())
        assert "\r" not in page
        assert page.endswith("\n")
        lines = page.split("\n")[:-1]

        seen_families: list[str] = []
        current: str | None = None
        expect_type_for: str | None = None
        seen_series: set = set()
        help_re = re.compile(r"^# HELP (" + _NAME_RE + r") (.*)$")
        type_re = re.compile(r"^# TYPE (" + _NAME_RE + r") gauge$")
        for line in lines:
            assert line != "", "blank line"
            h = help_re.match(line)
            t = type_re.match(line)
            if h:
                assert expect_type_for is None
                assert h.group(1) not in seen_families, "family split into two groups"
                seen_families.append(h.group(1))
                current = h.group(1)
                expect_type_for = current
            elif t:
                assert expect_type_for == t.group(1), "TYPE must directly follow its HELP"
                expect_type_for = None
            else:
                assert expect_type_for is None, "sample before TYPE"
                m = _SAMPLE_RE.match(line)
                assert m, f"line fits no grammar rule: {line!r}"
                assert current is not None and m.group("name") == current, "sample outside its family"
                pairs = _label_pairs(m.group("labels"))
                assert all(_LABEL_NAME_RE.match(k) for k, _ in pairs)
                key = (m.group("name"), tuple(sorted(pairs)))
                assert key not in seen_series, f"duplicate series: {line!r}"
                seen_series.add(key)
        assert seen_families == _NAMES
        assert seen_series  # the page is not vacuous

    def test_prometheus_output_parses_with_reference_parser(self):
        parser = pytest.importorskip("prometheus_client.parser")
        alias = 'a"b\\c' + "\n" + "d"
        payload = _payload([
            _row(1, _usage(25.0, 60.0), alias=alias, login_expires_at="2026-11-01T00:00:00Z"),
            _row(2, USAGE_TOKEN_EXPIRED, alias="Équipe café", active=True),
        ], active=2)
        page = prometheus_output.render(payload)
        families = {f.name: f for f in parser.text_string_to_metric_families(page)}
        assert set(_NAMES) <= set(families)
        for name in _NAMES:
            assert families[name].type == "gauge", name

        info = families["cswap_account_info"].samples
        aliases = {s.labels["account"]: s.labels["alias"] for s in info}
        assert aliases == {"1": alias, "2": "Équipe café"}
        ratio = {
            (s.labels["account"], s.labels["window"]): s.value
            for s in families["cswap_usage_ratio"].samples
        }
        assert ratio[("1", "five_hour")] == pytest.approx(0.25)
        assert ratio[("1", "seven_day")] == pytest.approx(0.6)
        status = {
            (s.labels["account"], s.labels["status"]): s.value
            for s in families["cswap_usage_status"].samples
        }
        assert status[("2", "token_expired")] == 1
        assert status[("2", "ok")] == 0


class TestPrometheusCli:
    def test_prometheus_and_json_together_is_usage_error(self, capsys):
        with patch.object(sys, "argv", ["claude-swap", "list", "--prometheus", "--json"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == 2
        assert "--prometheus cannot be combined with --json" in capsys.readouterr().err

    def test_prometheus_requires_list(self, capsys):
        with patch.object(sys, "argv", ["claude-swap", "--status", "--prometheus"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == 2
        assert "--prometheus can only be used with 'list'" in capsys.readouterr().err

    def test_prometheus_with_token_status_is_usage_error(self, capsys):
        with patch.object(sys, "argv", ["claude-swap", "--list", "--token-status", "--prometheus"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == 2
        assert "--token-status cannot be combined with --prometheus" in capsys.readouterr().err

    def test_list_prometheus_calls_list_json_payload_and_skips_update_check(self, capsys):
        payload = _payload([_row(1, _usage(), alias="work", active=True)], active=1)
        switcher_cls, update = _run_cli(["claude-swap", "list", "--prometheus"], payload=payload)

        switcher_cls.return_value.list_accounts.assert_called_once_with(
            show_token_status=False, json_output=True,
        )
        update.assert_not_called()
        assert capsys.readouterr().out == prometheus_output.render(payload)

    def test_prometheus_error_path_prints_nothing_to_stdout(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            _run_cli(["claude-swap", "list", "--prometheus"], side_effect=ConfigError("nope"))
        assert excinfo.value.code == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "nope" in captured.err

    def test_prometheus_keyboard_interrupt_note_on_stderr(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            _run_cli(["claude-swap", "list", "--prometheus"], side_effect=KeyboardInterrupt())
        assert excinfo.value.code == 130
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "cancelled" in captured.err.lower()

    def test_prometheus_subprocess_writes_lf_only(self, temp_home: Path):
        """The page is written as bytes, so no platform turns LF into CRLF."""
        env = dict(os.environ)
        env["PYTHONPATH"] = _SRC_DIR + os.pathsep + env.get("PYTHONPATH", "")
        env["HOME"] = str(temp_home)
        env["USERPROFILE"] = str(temp_home)
        for var in ("CLAUDE_CONFIG_DIR", "CLAUDE_SECURESTORAGE_CONFIG_DIR", "XDG_DATA_HOME"):
            env.pop(var, None)
        env["NO_COLOR"] = "1"
        result = subprocess.run(
            [sys.executable, "-m", "claude_swap", "list", "--prometheus"],
            env=env, capture_output=True, timeout=60,
        )
        assert result.returncode == 0, result.stderr
        out = result.stdout
        assert b"\r" not in out
        assert out.startswith(b"# HELP cswap_account_info")
        assert out.endswith(b"\n")


class TestWrite:
    def test_prometheus_write_keeps_lf_and_utf8_on_a_crlf_console(self, monkeypatch):
        """A text stream that turns LF into CRLF (Windows) never sees the page."""
        page = prometheus_output.render(_rich_payload())
        raw = io.BytesIO()
        monkeypatch.setattr(
            sys, "stdout", io.TextIOWrapper(raw, encoding="cp1252", newline="\r\n")
        )
        prometheus_output.write(page)
        assert raw.getvalue() == page.encode("utf-8")
        assert b"\r" not in raw.getvalue()

    def test_prometheus_write_falls_back_to_a_text_stream(self, monkeypatch):
        page = prometheus_output.render(_rich_payload())
        out = io.StringIO()
        monkeypatch.setattr(sys, "stdout", out)
        prometheus_output.write(page)
        assert out.getvalue() == page

    def test_prometheus_write_without_stdout_is_a_no_op(self, monkeypatch):
        monkeypatch.setattr(sys, "stdout", None)
        prometheus_output.write("# HELP x y\n")  # must not raise
