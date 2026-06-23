"""Tests for the auto_switch module.

Pure decision-function tests need no fixtures (no file I/O, no mocks).
AutoSwitcher tests use temp_home + Platform.LINUX mirroring TestUsageAwareSwitch.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_swap.auto_switch import (
    AutoSwitchConfig,
    AutoSwitcher,
    SwitchDecision,
    _parse_reset_ts,
    decide_switch,
    load_config,
    next_interval,
    save_config,
)
from claude_swap.auto_switch_state import MonitorState, load_state, save_state
from claude_swap.models import Platform


# ---------------------------------------------------------------------------
# Helpers shared by decide_switch tests
# ---------------------------------------------------------------------------

def _make_config(**kw) -> AutoSwitchConfig:
    """Build a config with custom fields; rest from defaults."""
    defaults = dict(
        enabled=True,
        session_threshold=98.0,
        weekly_threshold=99.0,
        notify=True,
        min_interval=20,
        max_interval=300,
    )
    defaults.update(kw)
    return AutoSwitchConfig(**defaults)


def _usage(h5: float = 0.0, d7: float = 0.0, resets_at: str | None = None) -> dict:
    """Build a usage dict for tests."""
    d7_entry: dict = {"pct": d7}
    if resets_at is not None:
        d7_entry["resets_at"] = resets_at
    return {
        "five_hour": {"pct": h5},
        "seven_day": d7_entry,
    }


def _ts(delta_hours: float = 0.0) -> str:
    """ISO timestamp offset from now by delta_hours."""
    dt = datetime.now(timezone.utc) + timedelta(hours=delta_hours)
    return dt.isoformat()


# ---------------------------------------------------------------------------
# TestDecideSwitch — pure function table tests
# ---------------------------------------------------------------------------

class TestDecideSwitch:
    """Pure decide_switch tests; no fixtures, no file I/O."""

    CFG = _make_config()

    def _call(self, active_num, usage, switchable, live=None, idx=None):
        return decide_switch(
            active_num,
            usage,
            set(switchable) if not isinstance(switchable, set) else switchable,
            self.CFG,
            live or set(),
            idx,
        )

    # ------------------------------------------------------------------
    # stay — baseline
    # ------------------------------------------------------------------

    def test_under_both_thresholds_stays(self):
        d = self._call("1", {"1": _usage(50.0, 50.0)}, {"2"})
        assert d.action == "stay"
        assert d.reason == "under-threshold"
        assert d.trigger_window is None

    def test_just_below_5h_threshold_stays(self):
        """h5 = 97.9 (< 98.0) and d7 = 0 → must stay (boundary just under)."""
        d = self._call("1", {"1": _usage(97.9, 0.0), "2": _usage(0.0, 0.0)}, {"2"})
        assert d.action == "stay"
        assert d.reason == "under-threshold"
        assert d.trigger_window is None

    def test_exactly_at_5h_threshold_switches(self):
        """>= comparison: 98.0 == threshold must cross."""
        usage = {
            "1": _usage(98.0, 0.0),
            "2": _usage(0.0, 0.0),
        }
        d = self._call("1", usage, {"2"})
        assert d.action == "switch"
        assert d.reason == "5h-threshold"
        assert d.trigger_pct == 98.0

    def test_exactly_at_7d_threshold_switches(self):
        """7d exactly at 99.0 must cross."""
        usage = {
            "1": _usage(0.0, 99.0),
            "2": _usage(0.0, 0.0),
        }
        d = self._call("1", usage, {"2"})
        assert d.action == "switch"
        assert d.reason == "7d-threshold"
        assert d.trigger_pct == 99.0

    # ------------------------------------------------------------------
    # trigger window selection
    # ------------------------------------------------------------------

    def test_5h_triggers_reports_5h_window(self):
        usage = {"1": _usage(99.0, 0.0), "2": _usage(0.0, 0.0)}
        d = self._call("1", usage, {"2"})
        assert d.trigger_window == "5h"
        assert d.trigger_pct == 99.0

    def test_7d_triggers_reports_7d_window(self):
        usage = {"1": _usage(0.0, 99.5), "2": _usage(0.0, 0.0)}
        d = self._call("1", usage, {"2"})
        assert d.trigger_window == "7d"
        assert d.trigger_pct == 99.5

    def test_both_crossed_reports_5h_as_binding(self):
        """5h is the binding/faster window when both cross."""
        usage = {"1": _usage(99.0, 100.0), "2": _usage(0.0, 0.0)}
        d = self._call("1", usage, {"2"})
        assert d.trigger_window == "5h"
        assert d.trigger_pct == 99.0
        assert d.action == "switch"

    # ------------------------------------------------------------------
    # candidate selection
    # ------------------------------------------------------------------

    def test_switches_to_soonest_7d_reset(self):
        """Among two viable candidates, picks the one resetting soonest.

        Pass an explicit rotation_index so the assertion isolates the
        reset-field sort and does NOT depend on CPython set-iteration order.
        """
        usage = {
            "1": _usage(99.0, 0.0),
            "2": _usage(0.0, 0.0, resets_at=_ts(+48)),   # resets in 48h
            "3": _usage(0.0, 0.0, resets_at=_ts(+10)),   # resets in 10h → soonest
        }
        idx = {"2": 0, "3": 1}
        d = self._call("1", usage, {"2", "3"}, idx=idx)
        assert d.action == "switch"
        assert d.target == "3"

    def test_tie_on_reset_picks_more_headroom(self):
        """Same resets_at → pick account with more headroom (lower utilisation)."""
        ts = _ts(+24)
        usage = {
            "1": _usage(99.0),
            "2": _usage(50.0, 0.0, resets_at=ts),   # 50% headroom
            "3": _usage(10.0, 0.0, resets_at=ts),   # 90% headroom → wins
        }
        d = self._call("1", usage, {"2", "3"})
        assert d.target == "3"

    def test_tie_on_headroom_picks_lower_rotation_index(self):
        """Same reset and headroom → lower position in sequence wins."""
        ts = _ts(+24)
        usage = {
            "1": _usage(99.0),
            "2": _usage(20.0, 0.0, resets_at=ts),
            "3": _usage(20.0, 0.0, resets_at=ts),
        }
        idx = {"2": 0, "3": 1}   # "2" is earlier in sequence
        d = self._call("1", usage, {"2", "3"}, idx=idx)
        assert d.target == "2"

    def test_candidate_with_no_reset_sorts_last(self):
        """A candidate missing resets_at sorts after one with a known reset."""
        usage = {
            "1": _usage(99.0),
            "2": _usage(0.0, 0.0),               # no resets_at → +inf
            "3": _usage(0.0, 0.0, resets_at=_ts(+100)),  # known → wins
        }
        d = self._call("1", usage, {"2", "3"})
        assert d.target == "3"

    # ------------------------------------------------------------------
    # candidate exclusion
    # ------------------------------------------------------------------

    def test_candidate_over_own_5h_threshold_excluded(self):
        usage = {
            "1": _usage(99.0),
            "2": _usage(99.0, 0.0),   # candidate itself over threshold
        }
        d = self._call("1", usage, {"2"})
        assert d.action == "stay"
        assert d.reason == "all-exhausted"

    def test_candidate_over_own_7d_threshold_excluded(self):
        usage = {
            "1": _usage(0.0, 99.5),
            "2": _usage(0.0, 99.5),   # candidate itself over 7d threshold
        }
        d = self._call("1", usage, {"2"})
        assert d.action == "stay"
        assert d.reason == "all-exhausted"

    def test_candidate_with_none_usage_excluded(self):
        usage = {"1": _usage(99.0), "2": None}
        d = self._call("1", usage, {"2"})
        assert d.action == "stay"
        assert d.reason == "all-exhausted"

    def test_candidate_with_no_credentials_excluded(self):
        usage = {"1": _usage(99.0), "2": "no credentials"}
        d = self._call("1", usage, {"2"})
        assert d.action == "stay"
        assert d.reason == "all-exhausted"

    def test_candidate_with_live_session_excluded(self):
        usage = {"1": _usage(99.0), "2": _usage(0.0)}
        d = self._call("1", usage, {"2"}, live={"2"})
        assert d.action == "stay"
        assert d.reason == "all-exhausted"

    def test_only_viable_candidate_chosen_when_others_exhausted(self):
        """3 is viable; 2 is over threshold → choose 3."""
        usage = {
            "1": _usage(99.0),
            "2": _usage(99.0),   # over threshold
            "3": _usage(10.0),   # viable
        }
        d = self._call("1", usage, {"2", "3"})
        assert d.action == "switch"
        assert d.target == "3"

    # ------------------------------------------------------------------
    # no switchable accounts
    # ------------------------------------------------------------------

    def test_no_switchable_others_single_account(self):
        usage = {"1": _usage(99.0)}
        d = self._call("1", usage, set())
        assert d.action == "stay"
        assert d.reason == "single-account"

    def test_no_candidates_when_switchable_empty(self):
        """Empty switchable set → single-account even when threshold crossed."""
        usage = {"1": _usage(100.0, 100.0)}
        d = self._call("1", usage, [])
        assert d.reason == "single-account"

    # ------------------------------------------------------------------
    # active usage unknown
    # ------------------------------------------------------------------

    def test_active_usage_none_stays(self):
        usage = {"1": None, "2": _usage(0.0)}
        d = self._call("1", usage, {"2"})
        assert d.action == "stay"
        assert d.reason == "active-usage-unknown"

    def test_active_num_none_stays(self):
        usage = {"2": _usage(0.0)}
        d = self._call(None, usage, {"2"})
        assert d.action == "stay"
        assert d.reason == "active-usage-unknown"

    def test_active_string_sentinel_stays(self):
        usage = {"1": "no credentials", "2": _usage(0.0)}
        d = self._call("1", usage, {"2"})
        assert d.action == "stay"
        assert d.reason == "active-usage-unknown"

    # ------------------------------------------------------------------
    # malformed inputs
    # ------------------------------------------------------------------

    def test_missing_pct_treated_as_zero(self):
        """Missing pct in active usage → treated as 0 → stay."""
        usage = {
            "1": {"five_hour": {}, "seven_day": {}},
            "2": _usage(0.0),
        }
        d = self._call("1", usage, {"2"})
        assert d.action == "stay"
        assert d.reason == "under-threshold"

    def test_missing_windows_treated_as_zero(self):
        usage = {"1": {}, "2": _usage(0.0)}
        d = self._call("1", usage, {"2"})
        assert d.action == "stay"
        assert d.reason == "under-threshold"

    def test_empty_dict_usage_safe(self):
        usage = {"1": {}, "2": {}}
        d = self._call("1", usage, {"2"})
        assert d.action == "stay"   # both under threshold

    def test_non_numeric_pct_treated_as_zero(self):
        usage = {
            "1": {"five_hour": {"pct": "bad"}, "seven_day": {"pct": None}},
            "2": _usage(0.0),
        }
        d = self._call("1", usage, {"2"})
        assert d.action == "stay"

    def test_never_raises_on_wildly_malformed_input(self):
        """decide_switch must not raise regardless of input shape."""
        d = decide_switch(
            "xyz",
            {"xyz": [1, 2, 3], "abc": True},
            {"abc"},
            _make_config(),
            set(),
        )
        assert isinstance(d, SwitchDecision)

    # ------------------------------------------------------------------
    # return value integrity
    # ------------------------------------------------------------------

    def test_switch_decision_has_target(self):
        usage = {"1": _usage(99.0), "2": _usage(0.0)}
        d = self._call("1", usage, {"2"})
        assert d.action == "switch"
        assert d.target == "2"
        assert isinstance(d.detail, str)
        assert len(d.detail) > 0

    def test_decision_is_frozen_dataclass(self):
        usage = {"1": _usage(99.0), "2": _usage(0.0)}
        d = self._call("1", usage, {"2"})
        with pytest.raises((AttributeError, TypeError)):
            d.action = "mutate"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# TestAutoSwitchConfig
# ---------------------------------------------------------------------------

class TestAutoSwitchConfig:
    """Tests for AutoSwitchConfig serialisation and persistence."""

    def test_defaults(self):
        cfg = AutoSwitchConfig()
        assert cfg.enabled is False
        assert cfg.session_threshold == 98.0
        assert cfg.weekly_threshold == 99.0
        assert cfg.notify is True
        assert cfg.min_interval == 60
        assert cfg.max_interval == 300
        assert cfg.offline_backoff_cap == 600

    def test_from_dict_round_trip(self):
        cfg = AutoSwitchConfig(enabled=True, session_threshold=90.0, notify=False)
        cfg2 = AutoSwitchConfig.from_dict(cfg.to_dict())
        assert cfg == cfg2

    def test_offline_backoff_cap_default(self):
        assert AutoSwitchConfig().offline_backoff_cap == 600

    def test_auto_switch_config_offline_backoff_cap_roundtrip(self):
        cfg = AutoSwitchConfig(offline_backoff_cap=1234)
        cfg2 = AutoSwitchConfig.from_dict(cfg.to_dict())
        assert cfg2.offline_backoff_cap == 1234
        # Missing key falls back to default.
        cfg3 = AutoSwitchConfig.from_dict({"enabled": True})
        assert cfg3.offline_backoff_cap == 600

    def test_from_dict_partial_uses_defaults(self):
        cfg = AutoSwitchConfig.from_dict({"enabled": True})
        assert cfg.enabled is True
        assert cfg.session_threshold == 98.0

    def test_from_dict_unknown_keys_ignored(self):
        cfg = AutoSwitchConfig.from_dict({"enabled": True, "future_key": "ignored"})
        assert cfg.enabled is True

    def test_from_dict_non_dict_returns_defaults(self):
        assert AutoSwitchConfig.from_dict(None) == AutoSwitchConfig()
        assert AutoSwitchConfig.from_dict([]) == AutoSwitchConfig()
        assert AutoSwitchConfig.from_dict("bad") == AutoSwitchConfig()

    def test_is_frozen(self):
        cfg = AutoSwitchConfig()
        with pytest.raises((AttributeError, TypeError)):
            cfg.enabled = True  # type: ignore[misc]

    def test_persist_and_load(self, tmp_path: Path):
        cfg = AutoSwitchConfig(enabled=True, session_threshold=90.0)
        save_config(cfg, backup_root=tmp_path)
        loaded = load_config(backup_root=tmp_path)
        assert loaded == cfg

    def test_load_missing_file_returns_defaults(self, tmp_path: Path):
        cfg = load_config(backup_root=tmp_path)
        assert cfg == AutoSwitchConfig()

    def test_load_bad_json_returns_defaults(self, tmp_path: Path):
        (tmp_path / "auto-switch.json").write_text("not-json")
        cfg = load_config(backup_root=tmp_path)
        assert cfg == AutoSwitchConfig()

    def test_save_sets_0o600_permissions(self, tmp_path: Path):
        import sys
        if sys.platform == "win32":
            pytest.skip("POSIX permissions not applicable on Windows")
        cfg = AutoSwitchConfig(enabled=True)
        save_config(cfg, backup_root=tmp_path)
        stat = (tmp_path / "auto-switch.json").stat()
        assert oct(stat.st_mode)[-3:] == "600"

    def test_to_dict_is_json_serialisable(self):
        cfg = AutoSwitchConfig(enabled=True, session_threshold=95.5)
        d = cfg.to_dict()
        json.dumps(d)  # must not raise


# ---------------------------------------------------------------------------
# TestParseResetTs — naive vs aware timestamp ordering
# ---------------------------------------------------------------------------

class TestParseResetTs:
    """Tests for _parse_reset_ts — naive ISO timestamps are treated as UTC."""

    def test_naive_and_aware_yield_same_timestamp(self):
        """A naive "...T12:00:00" and an aware "...T12:00:00+00:00" must match.

        Guarantees cross-account reset ordering is correct regardless of the
        API's tz format (naive values are assumed UTC, not local).
        """
        naive = _parse_reset_ts({"seven_day": {"resets_at": "2026-06-22T12:00:00"}})
        aware = _parse_reset_ts({"seven_day": {"resets_at": "2026-06-22T12:00:00+00:00"}})
        assert naive == aware
        assert naive != float("inf")

    def test_missing_resets_at_is_infinite(self):
        assert _parse_reset_ts({"seven_day": {"pct": 10.0}}) == float("inf")

    def test_non_dict_usage_is_infinite(self):
        assert _parse_reset_ts(None) == float("inf")
        assert _parse_reset_ts("bad") == float("inf")

    def test_unparseable_resets_at_is_infinite(self):
        assert _parse_reset_ts({"seven_day": {"resets_at": "not-a-date"}}) == float("inf")


# ---------------------------------------------------------------------------
# TestNextInterval — adaptive polling
# ---------------------------------------------------------------------------

class TestNextInterval:
    """Tests for the next_interval function (60s floor cadence band)."""

    # Default-band config (min 60, max 300) — matches the shipped defaults.
    CFG = AutoSwitchConfig(min_interval=60, max_interval=300)

    def test_none_usage_returns_max(self):
        assert next_interval(None, self.CFG) == 300

    def test_empty_usage_returns_max(self):
        assert next_interval({}, self.CFG) == 300

    def test_near_threshold_returns_min(self):
        usage = {"five_hour": {"pct": 96.0}, "seven_day": {"pct": 0.0}}
        assert next_interval(usage, self.CFG) == 60   # min_interval floor

    def test_85_band_returns_two_times_min(self):
        usage = {"five_hour": {"pct": 85.0}, "seven_day": {"pct": 0.0}}
        # >= 85% → min(max, 2*min) = 120
        assert next_interval(usage, self.CFG) == 120

    def test_50_band_returns_mid(self):
        usage = {"five_hour": {"pct": 50.0}, "seven_day": {"pct": 0.0}}
        # >= 50% → ~80% of ceiling = 240
        assert next_interval(usage, self.CFG) == 240

    def test_low_usage_returns_max(self):
        usage = {"five_hour": {"pct": 10.0}, "seven_day": {"pct": 5.0}}
        assert next_interval(usage, self.CFG) == 300   # far away → ceiling

    def test_result_always_in_band(self):
        """Every online result is clamped to [min_interval, max_interval]."""
        for pct in (0.0, 10.0, 49.9, 50.0, 84.9, 85.0, 94.9, 95.0, 100.0):
            usage = {"five_hour": {"pct": pct}}
            v = next_interval(usage, self.CFG)
            assert self.CFG.min_interval <= v <= self.CFG.max_interval

    def test_clamped_to_min(self):
        # min_interval above the 2*min band value still floors correctly.
        cfg = AutoSwitchConfig(min_interval=200, max_interval=300)
        usage = {"five_hour": {"pct": 50.0}}   # would be 240, but >= floor 200
        assert next_interval(usage, cfg) >= 200

    def test_clamped_to_max(self):
        cfg = AutoSwitchConfig(min_interval=60, max_interval=300)
        usage = {"five_hour": {"pct": 0.0}}
        assert next_interval(usage, cfg) <= 300

    def test_uses_binding_window(self):
        """The window with the highest utilisation drives the interval."""
        usage_high_d7 = {"five_hour": {"pct": 10.0}, "seven_day": {"pct": 97.0}}
        usage_high_h5 = {"five_hour": {"pct": 97.0}, "seven_day": {"pct": 10.0}}
        assert next_interval(usage_high_d7, self.CFG) == next_interval(
            usage_high_h5, self.CFG
        )

    def test_next_interval_backoff_grows_and_caps(self):
        """Offline backoff grows exponentially from max_interval and caps."""
        cfg = AutoSwitchConfig(
            min_interval=60, max_interval=300, offline_backoff_cap=600
        )
        # failures=1 → max_interval * 2**0 = 300
        assert next_interval(None, cfg, 1) == 300
        # failures=2 → 300 * 2**1 = 600 (== cap)
        assert next_interval(None, cfg, 2) == 600
        # failures=3 → 300 * 2**2 = 1200 → capped at 600
        assert next_interval(None, cfg, 3) == 600
        # Monotonic non-decreasing as failures grow, always <= cap.
        prev = 0
        for f in range(1, 10):
            v = next_interval(None, cfg, f)
            assert v >= prev
            assert v <= cfg.offline_backoff_cap
            prev = v

    def test_next_interval_backoff_ignores_usage(self):
        """When offline, the interval is backoff — usage% is irrelevant."""
        cfg = AutoSwitchConfig(min_interval=60, max_interval=300, offline_backoff_cap=600)
        near_threshold = {"five_hour": {"pct": 99.0}}
        # Online would be min_interval (60); offline overrides to backoff.
        assert next_interval(near_threshold, cfg, 1) == 300

    def test_next_interval_zero_failures_is_online_path(self):
        """consecutive_failures=0 → normal adaptive behaviour."""
        usage = {"five_hour": {"pct": 96.0}, "seven_day": {"pct": 0.0}}
        assert next_interval(usage, self.CFG, 0) == 60


# ---------------------------------------------------------------------------
# TestAutoSwitcherRunOnce — with a mocked switcher
# ---------------------------------------------------------------------------

_NO_CREDS = "no credentials"


def _encode_creds(usage: object) -> str:
    """Encode an account's intended usage into a creds JSON blob.

    The engine probes usage via ``oauth.fetch_usage_for_account`` (patched in
    tests by ``_patch_oauth_fetch``), which decodes this blob. We carry the
    usage IN the creds so the fake is fully self-contained and no per-account
    registry is needed.

    * ``"no credentials"``  → empty creds (engine returns the sentinel without
                              calling oauth — i.e. "we did NOT try")
    * ``None``              → a token IS present but the fetch returns None
                              (genuine outage signal)
    * ``dict``              → a token is present and the fetch returns the dict
    """
    if usage == _NO_CREDS:
        return ""   # no token → engine short-circuits to "no credentials"
    payload = {"claudeAiOauth": {"accessToken": "tok"}}
    if usage is None:
        payload["_fake_usage_kind"] = "none"
    else:
        payload["_fake_usage_kind"] = "dict"
        payload["_fake_usage"] = usage
    return json.dumps(payload)


@pytest.fixture(autouse=True)
def _patch_oauth_fetch():
    """Route the engine's single-account usage probe to the fake's creds blob.

    Decodes ``_fake_usage`` from the credentials passed by
    ``AutoSwitcher._fetch_one`` → ``oauth.fetch_usage_for_account``. Returns a
    MagicMock so tests can assert ``.call_count`` (number of ACTUAL usage API
    calls this tick — uncredentialed accounts never reach here).
    """
    def _decode(account_num, email, credentials, is_active, persist_credentials=None):
        try:
            data = json.loads(credentials)
        except Exception:
            return None
        if data.get("_fake_usage_kind") == "none":
            return None
        return data.get("_fake_usage")

    with patch(
        "claude_swap.oauth.fetch_usage_for_account", side_effect=_decode
    ) as mock_fetch:
        yield mock_fetch


class _FakeSwitcher:
    """Minimal fake switcher for AutoSwitcher unit tests.

    The first account is the ACTIVE one (mirrors ``_get_current_account``).
    Each account dict carries a ``usage`` value (dict / None / "no credentials")
    that is encoded into the row's credentials so the engine's active-first
    probe (``_fetch_one`` → patched ``oauth.fetch_usage_for_account``) recovers
    it. ``_collect_usage`` is retained for ``cswap --list``-style callers but is
    NOT on the daemon tick path anymore.
    """

    def __init__(self, backup_dir: Path, accounts: list[dict]) -> None:
        self.backup_dir = backup_dir
        self.platform = Platform.LINUX
        self._accounts = accounts   # list of {num, email, usage}
        self.switched_to: list[str] = []

    def _active_email(self) -> str | None:
        return self._accounts[0]["email"] if self._accounts else None

    def _build_accounts_info(self):
        active_email = self._active_email()
        return [
            (
                int(a["num"]),
                a["email"],
                "",
                "",
                a["email"] == active_email,          # is_active: first account
                _encode_creds(a.get("usage")),       # creds carry the usage
            )
            for a in self._accounts
        ]

    def _collect_usage(self, accounts_info):
        # Full-set fetch (cswap --list / status). Not used by the daemon tick.
        num_to_usage = {a["num"]: a.get("usage") for a in self._accounts}
        return [num_to_usage.get(str(info[0])) for info in accounts_info]

    def _get_sequence_data_migrated(self):
        return {
            "sequence": [int(a["num"]) for a in self._accounts],
            "accounts": {
                a["num"]: {"email": a["email"], "organizationUuid": ""}
                for a in self._accounts
            },
        }

    def _get_current_account(self):
        # First account is "active"
        if self._accounts:
            return (self._accounts[0]["email"], "")
        return None

    @staticmethod
    def _find_account_slot(data, email, org_uuid):
        for num, acc in data.get("accounts", {}).items():
            if acc.get("email") == email:
                return num
        return None

    def _account_is_switchable(self, num: str) -> bool:
        return any(a["num"] == num for a in self._accounts[1:])

    def _live_session_pids(self, num: str, email: str) -> list[int]:
        return []

    def auto_switch_to(self, target: str, quiet: bool = True) -> None:
        self.switched_to.append(target)


def _make_auto_switcher(backup_dir: Path, accounts: list[dict]) -> AutoSwitcher:
    cfg = AutoSwitchConfig(enabled=True)
    fs = _FakeSwitcher(backup_dir, accounts)
    return AutoSwitcher(switcher=fs, config=cfg)


class TestAutoSwitcherRunOnce:
    """AutoSwitcher.run_once tests (mocked switcher + mocked notify)."""

    def test_performs_switch_when_threshold_crossed(self, tmp_path: Path):
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": _usage(99.0)},
            {"num": "2", "email": "b@x.com", "usage": _usage(10.0)},
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify") as mock_notify:
            decision = as_.run_once()
        assert decision.action == "switch"
        assert decision.target == "2"
        assert as_._switcher.switched_to == ["2"]
        mock_notify.assert_called_once()

    def test_no_switch_when_under_threshold(self, tmp_path: Path):
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": _usage(50.0)},
            {"num": "2", "email": "b@x.com", "usage": _usage(20.0)},
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify") as mock_notify:
            decision = as_.run_once()
        assert decision.action == "stay"
        assert as_._switcher.switched_to == []
        mock_notify.assert_not_called()

    def test_notify_on_exhaustion(self, tmp_path: Path):
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": _usage(99.0)},
            {"num": "2", "email": "b@x.com", "usage": _usage(99.0)},
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify") as mock_notify:
            decision = as_.run_once()
        assert decision.reason == "all-exhausted"
        mock_notify.assert_called_once()

    def test_exhaustion_notify_rate_limited(self, tmp_path: Path):
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": _usage(99.0)},
            {"num": "2", "email": "b@x.com", "usage": _usage(99.0)},
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify") as mock_notify:
            as_.run_once()
            as_.run_once()   # second call — within gate → no second notify
        assert mock_notify.call_count == 1

    def test_notify_not_called_when_config_notify_false(self, tmp_path: Path):
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": _usage(99.0)},
            {"num": "2", "email": "b@x.com", "usage": _usage(10.0)},
        ]
        cfg = AutoSwitchConfig(enabled=True, notify=False)
        fs = _FakeSwitcher(tmp_path, accounts)
        as_ = AutoSwitcher(switcher=fs, config=cfg)
        with patch("claude_swap.auto_switch.notify") as mock_notify:
            as_.run_once()
        mock_notify.assert_not_called()

    def test_returns_stay_on_gather_error(self, tmp_path: Path):
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": _usage(99.0)},
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        with patch.object(as_, "_gather_meta", side_effect=RuntimeError("oops")):
            decision = as_.run_once()
        assert decision.action == "stay"
        assert decision.reason == "tick-error"

    def test_switch_error_returns_tick_error_stay(self, tmp_path: Path):
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": _usage(99.0)},
            {"num": "2", "email": "b@x.com", "usage": _usage(10.0)},
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        as_._switcher.auto_switch_to = MagicMock(side_effect=RuntimeError("switch boom"))
        with patch("claude_swap.auto_switch.notify"):
            decision = as_.run_once()
        assert decision.reason == "tick-error"

    def test_run_once_gathers_meta_exactly_once(self, tmp_path: Path):
        """run_once must build account metadata exactly once per tick."""
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": _usage(50.0)},
            {"num": "2", "email": "b@x.com", "usage": _usage(10.0)},
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        with patch.object(as_, "_gather_meta", wraps=as_._gather_meta) as spy:
            as_.run_once()
        assert spy.call_count == 1

    def test_run_once_stashes_active_usage(self, tmp_path: Path):
        """After run_once, _last_active_usage holds the ACTIVE account's usage."""
        active_usage = _usage(50.0, 30.0)
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": active_usage},
            {"num": "2", "email": "b@x.com", "usage": _usage(10.0)},
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify"):
            as_.run_once()
        assert as_._last_active_usage == active_usage


# ---------------------------------------------------------------------------
# TestConnectionLossFallback — offline detection / backoff / recovery / state
# ---------------------------------------------------------------------------

class TestConnectionLossFallback:
    """SAFE / VISIBLE / RECOVERABLE behaviour when the usage API is offline."""

    def test_run_once_detects_offline_when_all_fetch_fail(self, tmp_path: Path):
        """All fetches None → online False, failures increments, no switch."""
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": None},
            {"num": "2", "email": "b@x.com", "usage": None},
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify"):
            decision = as_.run_once()
        assert decision.action == "stay"
        assert as_._switcher.switched_to == []          # never switched
        assert as_.consecutive_failures == 1
        # State persisted to disk.
        persisted = load_state(tmp_path)
        assert persisted.consecutive_failures == 1

    def test_run_once_active_no_credentials_is_neither(self, tmp_path: Path):
        """Active uncredentialed → NEITHER (no token, didn't try): not offline.

        Active-first: the trichotomy keys on the ACTIVE probe. "no credentials"
        means we did not try, so it is NOT a network outage even if a peer is
        fine — and the others are not fetched.
        """
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": "no credentials"},
            {"num": "2", "email": "b@x.com", "usage": _usage(10.0)},
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify") as mock_notify, \
             patch("claude_swap.oauth.fetch_usage_for_account") as spy_fetch:
            # The autouse fixture already patched oauth; re-patch locally to spy.
            spy_fetch.side_effect = lambda *a, **k: None
            decision = as_.run_once()
        assert decision.reason == "active-usage-unknown"
        assert as_.consecutive_failures == 0            # NOT offline
        assert as_._switcher.switched_to == []
        # Active had no creds → engine short-circuits, never calls the API,
        # never fetches the others.
        spy_fetch.assert_not_called()
        mock_notify.assert_not_called()

    def test_no_switch_while_offline(self, tmp_path: Path):
        """Even with the active account 'over threshold' missing, offline = no switch."""
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": None},
            {"num": "2", "email": "b@x.com", "usage": None},
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        as_._switcher.auto_switch_to = MagicMock()
        with patch("claude_swap.auto_switch.notify"):
            as_.run_once()
            as_.run_once()
            as_.run_once()
        as_._switcher.auto_switch_to.assert_not_called()
        assert as_.consecutive_failures == 3

    def test_offline_notify_is_one_shot(self, tmp_path: Path):
        """>= 2 offline ticks → exactly one 'offline' notification."""
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": None},
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify") as mock_notify:
            as_.run_once()   # failures=1, no notify yet
            assert mock_notify.call_count == 0
            as_.run_once()   # failures=2 → one offline notify
            as_.run_once()   # failures=3 → still just one (already notified)
            as_.run_once()
        # Exactly one notification, and it's the offline one.
        assert mock_notify.call_count == 1
        title = mock_notify.call_args[0][0]
        assert "offline" in title.lower()
        assert as_._state.offline_notified is True

    def test_recovery_resets_and_notifies(self, tmp_path: Path):
        """Offline (notified) → online → failures 0, one 'back online' notify."""
        # BOTH accounts offline → truly offline (nothing fetched).
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": None},
            {"num": "2", "email": "b@x.com", "usage": None},
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)

        # Drive offline until notified.
        with patch("claude_swap.auto_switch.notify"):
            as_.run_once()
            as_.run_once()   # offline notify fires here
        assert as_._state.offline_notified is True
        assert as_.consecutive_failures >= 2

        # Now the API comes back — accounts fetch successfully again.
        as_._switcher._accounts[0]["usage"] = _usage(20.0)
        as_._switcher._accounts[1]["usage"] = _usage(10.0)
        with patch("claude_swap.auto_switch.notify") as mock_notify:
            as_.run_once()
        assert as_.consecutive_failures == 0
        assert as_._state.offline_notified is False
        assert as_._state.last_online_ts is not None
        # Exactly one 'back online' notification.
        assert mock_notify.call_count == 1
        assert "online" in mock_notify.call_args[0][0].lower()

    def test_no_recovery_notify_if_never_notified_offline(self, tmp_path: Path):
        """A single offline blip (failures=1, not notified) → no recovery notify."""
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": None},
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify"):
            as_.run_once()   # failures=1, NOT notified
        assert as_._state.offline_notified is False
        # Recover immediately.
        as_._switcher._accounts[0]["usage"] = _usage(10.0)
        with patch("claude_swap.auto_switch.notify") as mock_notify:
            as_.run_once()
        assert as_.consecutive_failures == 0
        mock_notify.assert_not_called()

    def test_last_switch_recorded_in_state(self, tmp_path: Path):
        """A successful switch tick records last_switch in persisted state."""
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": _usage(99.0)},
            {"num": "2", "email": "b@x.com", "usage": _usage(10.0)},
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify"):
            decision = as_.run_once()
        assert decision.action == "switch"
        sw = as_._state.last_switch
        assert sw is not None
        assert sw["account"] == "2"
        assert sw["reason"] == "5h-threshold"
        assert isinstance(sw["ts"], float)
        # Persisted to disk too.
        assert load_state(tmp_path).last_switch == sw

    def test_last_usage_not_overwritten_with_none(self, tmp_path: Path):
        """A good reading survives a later failed fetch for that account."""
        good = _usage(20.0, 15.0)
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": good},
            {"num": "2", "email": "b@x.com", "usage": _usage(10.0)},
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify"):
            as_.run_once()
        assert as_._state.last_usage["1"]["usage"] == good

        # Now the active account's fetch fails (None) → offline tick; the
        # online path (which merges last_usage) does not run, so the good
        # account-1 reading must NOT be clobbered.
        as_._switcher._accounts[0]["usage"] = None
        with patch("claude_swap.auto_switch.notify"):
            as_.run_once()
        assert as_._state.last_usage["1"]["usage"] == good

    def test_state_persisted_every_tick(self, tmp_path: Path):
        """run_once persists state even on a plain under-threshold stay."""
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": _usage(50.0)},
            {"num": "2", "email": "b@x.com", "usage": _usage(10.0)},
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify"):
            as_.run_once()
        assert (tmp_path / "auto-switch-state.json").exists()

    # ------------------------------------------------------------------
    # Trichotomy: zero accounts / all-no-credentials / mixed
    # ------------------------------------------------------------------

    def test_zero_accounts_is_not_offline(self, tmp_path: Path):
        """Empty usage map → stay, failures stays 0, NO offline notification."""
        as_ = _make_auto_switcher(tmp_path, [])  # no managed accounts
        with patch("claude_swap.auto_switch.notify") as mock_notify:
            decision = as_.run_once()
            as_.run_once()   # a second tick must still not fire a notify
        assert decision.action == "stay"
        assert as_.consecutive_failures == 0
        assert as_._state.offline_notified is False
        mock_notify.assert_not_called()

    def test_all_no_credentials_is_not_offline(self, tmp_path: Path):
        """Every account uncredentialed ("no credentials") → not a network outage."""
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": "no credentials"},
            {"num": "2", "email": "b@x.com", "usage": "no credentials"},
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify") as mock_notify:
            as_.run_once()
            as_.run_once()   # >=2 ticks: still no false offline notification
        assert as_.consecutive_failures == 0
        assert as_._state.offline_notified is False
        assert as_._switcher.switched_to == []
        mock_notify.assert_not_called()

    def test_mixed_no_credentials_and_none_is_offline(self, tmp_path: Path):
        """One "no credentials" + one None (real failure) → offline DOES fire."""
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": None},            # tried, failed
            {"num": "2", "email": "b@x.com", "usage": "no credentials"},  # didn't try
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify"):
            as_.run_once()
        # At least one token-bearing fetch failed → genuine outage signal.
        assert as_.consecutive_failures == 1
        assert as_._switcher.switched_to == []

    def test_second_outage_after_recovery_renotifies(self, tmp_path: Path):
        """offline≥2 (notify) → online (recovery) → offline≥2 → offline notify again."""
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": None},
            {"num": "2", "email": "b@x.com", "usage": None},
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        titles: list[str] = []

        def _capture(title, _msg):
            titles.append(title)

        with patch("claude_swap.auto_switch.notify", side_effect=_capture):
            # First outage → offline notify on the 2nd failure.
            as_.run_once()
            as_.run_once()
            offline_after_first = sum("offline" in t.lower() for t in titles)
            assert offline_after_first == 1

            # Recover.
            as_._switcher._accounts[0]["usage"] = _usage(20.0)
            as_._switcher._accounts[1]["usage"] = _usage(10.0)
            as_.run_once()
            assert as_._state.offline_notified is False
            assert sum("back online" in t.lower() for t in titles) == 1

            # Second outage → offline notify fires a SECOND time.
            as_._switcher._accounts[0]["usage"] = None
            as_._switcher._accounts[1]["usage"] = None
            as_.run_once()
            as_.run_once()

        assert sum("offline" in t.lower() for t in titles) == 2


# ---------------------------------------------------------------------------
# TestActiveFirstFetch — anti-spam two-phase fetch (one call/normal tick)
# ---------------------------------------------------------------------------

class TestActiveFirstFetch:
    """A normal tick probes ONLY the active account (one usage API call)."""

    @staticmethod
    def _fetched_nums(mock_fetch) -> list[str]:
        """Account nums actually passed to oauth.fetch_usage_for_account."""
        return [str(c.args[0]) for c in mock_fetch.call_args_list]

    def test_run_once_fetches_active_only_when_under_threshold(
        self, tmp_path: Path, _patch_oauth_fetch
    ):
        """Active under threshold → exactly ONE usage fetch; others NOT fetched."""
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": _usage(50.0, 40.0)},
            {"num": "2", "email": "b@x.com", "usage": _usage(10.0)},
            {"num": "3", "email": "c@x.com", "usage": _usage(10.0)},
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify"):
            decision = as_.run_once()
        assert decision.reason == "under-threshold"
        assert decision.action == "stay"
        # Exactly one usage API call — the active account only.
        assert _patch_oauth_fetch.call_count == 1
        assert self._fetched_nums(_patch_oauth_fetch) == ["1"]
        assert as_._switcher.switched_to == []

    def test_run_once_fetches_others_only_when_active_crosses(
        self, tmp_path: Path, _patch_oauth_fetch
    ):
        """Active >= threshold → others fetched; switch to soonest-7d-reset target."""
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": _usage(99.0)},                   # active over 5h
            {"num": "2", "email": "b@x.com", "usage": _usage(10.0, 0.0, _ts(+48))},   # resets in 48h
            {"num": "3", "email": "c@x.com", "usage": _usage(10.0, 0.0, _ts(+10))},   # resets in 10h → soonest
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify"):
            decision = as_.run_once()
        assert decision.action == "switch"
        assert decision.target == "3"                 # soonest 7d reset
        assert as_._switcher.switched_to == ["3"]
        # Active (1) + both others (2, 3) fetched = 3 calls.
        assert _patch_oauth_fetch.call_count == 3
        assert set(self._fetched_nums(_patch_oauth_fetch)) == {"1", "2", "3"}

    def test_run_once_offline_does_not_fetch_others(
        self, tmp_path: Path, _patch_oauth_fetch
    ):
        """Active fetch None (outage) → offline tick; others NOT fetched."""
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": None},      # active: tried, failed
            {"num": "2", "email": "b@x.com", "usage": _usage(10.0)},
            {"num": "3", "email": "c@x.com", "usage": _usage(10.0)},
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify"):
            decision = as_.run_once()
        assert decision.reason == "active-usage-unknown"
        assert as_.consecutive_failures == 1          # offline tick
        # Only the active account was probed — no peer fetches during an outage.
        assert _patch_oauth_fetch.call_count == 1
        assert self._fetched_nums(_patch_oauth_fetch) == ["1"]
        assert as_._switcher.switched_to == []

    def test_run_once_over_threshold_but_no_others_stays(
        self, tmp_path: Path, _patch_oauth_fetch
    ):
        """Active over threshold, single account → stay/single-account, 1 fetch."""
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": _usage(99.0)},
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify"):
            decision = as_.run_once()
        assert decision.reason == "single-account"
        # No switchable others → nothing else to fetch beyond the active probe.
        assert _patch_oauth_fetch.call_count == 1


# ---------------------------------------------------------------------------
# TestMonitorState — defensive load/save of the state file
# ---------------------------------------------------------------------------

class TestMonitorState:
    """MonitorState serialisation + defensive load (never crashes)."""

    def test_defaults(self):
        st = MonitorState()
        assert st.last_online_ts is None
        assert st.consecutive_failures == 0
        assert st.offline_notified is False
        assert st.last_switch is None
        assert st.last_usage == {}

    def test_round_trip(self, tmp_path: Path):
        st = MonitorState(
            last_online_ts=123.0,
            consecutive_failures=2,
            offline_notified=True,
            last_switch={"account": "2", "ts": 1.0, "reason": "5h-threshold"},
            last_usage={"1": {"usage": {"five_hour": {"pct": 1.0}}, "fetched_at": 5.0}},
        )
        save_state(st, backup_root=tmp_path)
        loaded = load_state(backup_root=tmp_path)
        assert loaded == st

    def test_state_file_missing_or_corrupt_returns_defaults(self, tmp_path: Path):
        # Missing file → defaults.
        assert load_state(backup_root=tmp_path) == MonitorState()
        # Corrupt file → defaults, no crash.
        (tmp_path / "auto-switch-state.json").write_text("not-json{{{")
        assert load_state(backup_root=tmp_path) == MonitorState()

    def test_from_dict_non_dict_returns_defaults(self):
        assert MonitorState.from_dict(None) == MonitorState()
        assert MonitorState.from_dict([1, 2, 3]) == MonitorState()
        assert MonitorState.from_dict("bad") == MonitorState()

    def test_from_dict_drops_malformed_last_usage_entries(self):
        st = MonitorState.from_dict({
            "last_usage": {
                "1": {"usage": {"five_hour": {"pct": 1.0}}, "fetched_at": 5.0},
                "2": {"usage": None},          # malformed → dropped
                "3": "not-a-dict",             # malformed → dropped
            }
        })
        assert "1" in st.last_usage
        assert "2" not in st.last_usage
        assert "3" not in st.last_usage

    def test_from_dict_negative_failures_clamped(self):
        st = MonitorState.from_dict({"consecutive_failures": -5})
        assert st.consecutive_failures == 0

    def test_save_state_sets_0o600(self, tmp_path: Path):
        import sys
        if sys.platform == "win32":
            pytest.skip("POSIX permissions not applicable on Windows")
        save_state(MonitorState(consecutive_failures=1), backup_root=tmp_path)
        stat = (tmp_path / "auto-switch-state.json").stat()
        assert oct(stat.st_mode)[-3:] == "600"

    def test_merged_usage_skips_none(self):
        st = MonitorState(
            last_usage={"1": {"usage": {"five_hour": {"pct": 1.0}}, "fetched_at": 1.0}}
        )
        merged = st.merged_usage({"1": None, "2": {"seven_day": {"pct": 2.0}}}, 9.0)
        # Existing good entry preserved; None ignored; new good entry added.
        assert merged["1"]["usage"] == {"five_hour": {"pct": 1.0}}
        assert merged["2"]["usage"] == {"seven_day": {"pct": 2.0}}
        assert merged["2"]["fetched_at"] == 9.0


# ---------------------------------------------------------------------------
# TestSingleFlight — concurrent lock acquisition
# ---------------------------------------------------------------------------

class TestSingleFlight:
    """Test that only one AutoSwitcher can acquire the lock at a time."""

    def test_second_watch_exits_when_lock_held(self, tmp_path: Path, capsys):
        """If the lock is held, watch() prints a message and returns without switching."""
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": _usage(10.0)},
        ]
        cfg = AutoSwitchConfig(enabled=True)
        fs1 = _FakeSwitcher(tmp_path, accounts)
        fs2 = _FakeSwitcher(tmp_path, accounts)
        as1 = AutoSwitcher(switcher=fs1, config=cfg)
        as2 = AutoSwitcher(switcher=fs2, config=cfg)

        from claude_swap.locking import FileLock

        # Acquire the lock as-if daemon is running.
        lock = FileLock(tmp_path / ".auto-switch.lock")
        assert lock.acquire(timeout=0)
        try:
            as2.watch()
        finally:
            lock.release()

        out = capsys.readouterr().out
        assert "already running" in out

    def test_run_daemon_exits_when_lock_held(self, tmp_path: Path):
        """run_daemon exits without switching when lock is already held."""
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": _usage(10.0)},
        ]
        cfg = AutoSwitchConfig(enabled=True)
        fs = _FakeSwitcher(tmp_path, accounts)
        as_ = AutoSwitcher(switcher=fs, config=cfg)

        from claude_swap.locking import FileLock

        lock = FileLock(tmp_path / ".auto-switch.lock")
        assert lock.acquire(timeout=0)
        try:
            as_.run_daemon()   # must return immediately
        finally:
            lock.release()

        assert fs.switched_to == []
