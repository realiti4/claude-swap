"""Tests for the opt-in five-hour window warmer."""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from claude_swap import cli
from claude_swap.models import AccountSnapshot, AccountsSnapshot
from claude_swap.exceptions import PromptOutcomeUnknown, SessionError
from claude_swap.locking import FileLock
from claude_swap.session import AUTH_OVERRIDE_ENV_VARS, SessionManager
from claude_swap.usage_store import UsageEntry


NOW = 1_800_000_000.0


class _MutableClock:
    def __init__(self, now: float = NOW):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _iso(seconds_from_now: float) -> str:
    return datetime.fromtimestamp(NOW + seconds_from_now, timezone.utc).isoformat()


def _account(
    number: str,
    usage: dict | None,
    *,
    age_s: float = 0.0,
    last_error: str | None = None,
    trust_extended: bool = False,
    kind: str = "oauth",
    switchable: bool = True,
    disabled: bool = False,
) -> AccountSnapshot:
    return AccountSnapshot(
        number=number,
        email=f"user{number}@example.com",
        org_name="",
        org_uuid=f"org-{number}",
        is_active=number == "1",
        kind=kind,
        switchable=switchable,
        disabled=disabled,
        usage=UsageEntry(
            last_good=usage,
            fetched_at=NOW - age_s,
            age_s=age_s,
            last_error=last_error,
            trust_extended=trust_extended,
        ),
    )


class _FakeSwitcher:
    def __init__(self, backup_dir: Path, accounts: list[AccountSnapshot]):
        self.backup_dir = backup_dir
        self._accounts = tuple(accounts)
        self._logger = logging.getLogger("test-warmup")
        self.snapshot_calls = 0

    def accounts_snapshot(self, fetch=None):
        self.snapshot_calls += 1
        return AccountsSnapshot("1", self._accounts, NOW)


class _FakeSessions:
    def __init__(self, returncodes: dict[str, int] | None = None):
        self.calls: list[tuple[str, list[str], float]] = []
        self.returncodes = returncodes or {}

    def run_prompt(
        self, identifier, claude_args, *, timeout, expected_identity=None
    ):
        self.calls.append((identifier, claude_args, timeout))
        return SimpleNamespace(
            returncode=self.returncodes.get(identifier, 0),
            stdout="OK\n",
            stderr="simulated failure\n" if self.returncodes.get(identifier) else "",
        )


def _engine(
    tmp_path,
    accounts,
    *,
    sessions=None,
    dry_run=False,
    model="claude-haiku-4-5",
    clock=None,
):
    from claude_swap.warmup import WarmupEngine

    switcher = _FakeSwitcher(tmp_path, accounts)
    sessions = sessions or _FakeSessions()
    events = []
    engine = WarmupEngine(
        switcher,
        emit=events.append,
        session_manager=sessions,
        dry_run=dry_run,
        model=model,
        clock=clock or (lambda: NOW),
    )
    return engine, switcher, sessions, events


def test_tick_warms_cold_account_and_skips_live_window(tmp_path):
    cold = _account("1", {"seven_day": {"pct": 12.0}})
    live = _account(
        "2",
        {
            "five_hour": {"pct": 1.0, "resets_at": _iso(4 * 3600)},
            "seven_day": {"pct": 12.0},
        },
    )
    engine, switcher, sessions, events = _engine(tmp_path, [cold, live])

    summary = engine.tick()

    assert summary.warmed == 1
    assert summary.failed == 0
    assert switcher.snapshot_calls == 1
    assert [call[0] for call in sessions.calls] == ["1"]
    args = sessions.calls[0][1]
    assert args == [
        "--print",
        "--model",
        "claude-haiku-4-5",
        "--effort",
        "low",
        "--safe-mode",
        "--tools",
        "",
        "--no-session-persistence",
        "Reply only: OK",
    ]
    assert [event.kind for event in events] == ["warmed", "live"]
    state = json.loads((tmp_path / "warmup_state.json").read_text(encoding="utf-8"))
    assert state["schemaVersion"] == 2
    key = "org:org-1|email:user1@example.com"
    assert state["accounts"][key]["email"] == "user1@example.com"
    assert state["accounts"][key]["lastWarmAt"] == NOW


def test_tick_forces_one_fresh_probe_for_stale_cold_account(tmp_path):
    stale = _account("1", {"five_hour": {"pct": 0.0}, "seven_day": {"pct": 2}}, age_s=600)
    fresh = _account("1", {"five_hour": {"pct": 0.0}, "seven_day": {"pct": 2}})

    class RefreshingSwitcher(_FakeSwitcher):
        def __init__(self):
            super().__init__(tmp_path, [stale])
            self.fetches = []

        def accounts_snapshot(self, fetch=None):
            self.fetches.append(fetch)
            accounts = (stale,) if len(self.fetches) == 1 else (fresh,)
            return AccountsSnapshot("1", accounts, NOW)

    from claude_swap.warmup import WarmupEngine

    switcher = RefreshingSwitcher()
    sessions = _FakeSessions()
    engine = WarmupEngine(
        switcher,
        emit=lambda _event: None,
        session_manager=sessions,
        clock=lambda: NOW,
    )

    summary = engine.tick()

    assert switcher.fetches == [None, {"1"}]
    assert summary.warmed == 1


def test_tick_does_not_reprobe_stale_but_still_live_window(tmp_path):
    live = _account(
        "1",
        {
            "five_hour": {"pct": 1.0, "resets_at": _iso(3600)},
            "seven_day": {"pct": 2},
        },
        age_s=600,
    )
    engine, switcher, sessions, events = _engine(tmp_path, [live])

    engine.tick()

    assert switcher.snapshot_calls == 1
    assert sessions.calls == []
    assert events[0].kind == "live"


@pytest.mark.parametrize("trust_extended", [False, True])
def test_stale_nonzero_window_without_reset_is_probed_then_warmed(
    tmp_path, trust_extended
):
    stale = _account(
        "1",
        {"five_hour": {"pct": 1.0}, "seven_day": {"pct": 2}},
        age_s=600,
        last_error="http-429",
        trust_extended=trust_extended,
    )

    class UnavailableSwitcher(_FakeSwitcher):
        def __init__(self):
            super().__init__(tmp_path, [stale])
            self.fetches = []

        def accounts_snapshot(self, fetch=None):
            self.fetches.append(fetch)
            return AccountsSnapshot("1", (stale,), NOW)

    from claude_swap.warmup import WarmupEngine

    switcher = UnavailableSwitcher()
    sessions = _FakeSessions()
    engine = WarmupEngine(
        switcher,
        emit=lambda _event: None,
        session_manager=sessions,
        clock=lambda: NOW,
    )

    assert engine.tick().warmed == 1
    assert switcher.fetches == [None, {"1"}]
    assert [call[0] for call in sessions.calls] == ["1"]


@pytest.mark.parametrize(
    "usage",
    [
        {
            "seven_day": {"pct": 100.0, "resets_at": _iso(3600)},
        },
        {
            "seven_day": {"pct": 20.0},
            "scoped": [
                {"name": "Haiku", "pct": 100.0, "resets_at": _iso(3600)}
            ],
        },
    ],
)
def test_stale_known_weekly_exhaustion_still_skips(tmp_path, usage):
    account = _account("1", usage, age_s=600, last_error="http-429")
    engine, switcher, sessions, events = _engine(tmp_path, [account])

    engine.tick()

    assert switcher.snapshot_calls == 1
    assert sessions.calls == []
    assert events[0].kind == "weekly-exhausted"


@pytest.mark.parametrize(
    ("account", "reason"),
    [
        (_account("1", {"seven_day": {"pct": 100}}), "weekly-exhausted"),
        (_account("1", {"seven_day": {"pct": 2}}, disabled=True), "disabled"),
        (
            _account("1", {"seven_day": {"pct": 2}}, kind="api_key"),
            "not-oauth",
        ),
        (
            _account("1", {"seven_day": {"pct": 2}}, switchable=False),
            "unavailable",
        ),
    ],
)
def test_tick_fails_closed_for_ineligible_accounts(tmp_path, account, reason):
    engine, _switcher, sessions, events = _engine(tmp_path, [account])

    summary = engine.tick()

    assert summary.warmed == 0
    assert sessions.calls == []
    assert events[0].kind == reason


@pytest.mark.parametrize(
    "account",
    [
        _account("1", None),
        _account("1", {"seven_day": {"pct": 2}}, age_s=301),
        _account("1", {"seven_day": {"pct": 2}}, last_error="http-429"),
    ],
)
def test_tick_warms_when_usage_is_unavailable(tmp_path, account):
    engine, _switcher, sessions, events = _engine(tmp_path, [account])

    summary = engine.tick()

    assert summary.warmed == 1
    assert [call[0] for call in sessions.calls] == ["1"]
    assert events[0].kind == "warmed"


def test_tick_skips_exhausted_selected_model_window(tmp_path):
    account = _account(
        "1",
        {
            "seven_day": {"pct": 20},
            "scoped": [{"name": "Haiku", "pct": 100}],
        },
    )
    engine, _switcher, sessions, events = _engine(tmp_path, [account], model="haiku")

    engine.tick()

    assert sessions.calls == []
    assert events[0].kind == "weekly-exhausted"


def test_zero_percent_five_hour_without_reset_is_warmed(tmp_path):
    account = _account(
        "1", {"five_hour": {"pct": 0.0}, "seven_day": {"pct": 1.0}}
    )
    engine, _switcher, sessions, events = _engine(tmp_path, [account])

    summary = engine.tick()

    assert summary.warmed == 1
    assert [call[0] for call in sessions.calls] == ["1"]
    assert events[0].kind == "warmed"


def test_hollow_all_zero_usage_is_warmed(tmp_path):
    account = _account(
        "1",
        {
            "five_hour": {"pct": 0.0},
            "seven_day": {"pct": 0.0},
            "scoped": [{"name": "Haiku", "pct": 0.0}],
        },
    )
    engine, _switcher, sessions, events = _engine(tmp_path, [account])

    summary = engine.tick()

    assert summary.warmed == 1
    assert [call[0] for call in sessions.calls] == ["1"]
    assert events[0].kind == "warmed"


def test_nonzero_five_hour_without_reset_is_treated_as_live(tmp_path):
    account = _account(
        "1", {"five_hour": {"pct": 1.0}, "seven_day": {"pct": 1.0}}
    )
    engine, _switcher, sessions, events = _engine(tmp_path, [account])

    engine.tick()

    assert sessions.calls == []
    assert events[0].kind == "live"


def test_unknown_usage_after_five_hour_guard_is_warmed_again(tmp_path):
    (tmp_path / "warmup_state.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "accounts": {
                    "1": {
                        "email": "user1@example.com",
                        "orgUuid": "org-1",
                        "lastWarmAt": NOW - 6 * 3600,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    engine, _switcher, sessions, events = _engine(
        tmp_path, [_account("1", None)]
    )

    summary = engine.tick()

    assert summary.warmed == 1
    assert [call[0] for call in sessions.calls] == ["1"]
    assert events[0].kind == "warmed"


def test_expired_five_hour_window_is_warmed(tmp_path):
    account = _account(
        "1",
        {
            "five_hour": {"pct": 3.0, "resets_at": _iso(-1)},
            "seven_day": {"pct": 1.0},
        },
    )
    engine, _switcher, sessions, _events = _engine(tmp_path, [account])

    summary = engine.tick()

    assert summary.warmed == 1
    assert [call[0] for call in sessions.calls] == ["1"]


def test_persisted_recent_warm_prevents_duplicate_spend(tmp_path):
    (tmp_path / "warmup_state.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "accounts": {
                    "1": {
                        "email": "user1@example.com",
                        "orgUuid": "org-1",
                        "lastWarmAt": NOW - 60,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    account = _account("1", {"seven_day": {"pct": 1.0}})
    engine, _switcher, sessions, events = _engine(tmp_path, [account])

    engine.tick()

    assert sessions.calls == []
    assert events[0].kind == "recently-warmed"


def test_state_is_bound_to_identity_not_reused_slot(tmp_path):
    (tmp_path / "warmup_state.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "accounts": {
                    "1": {
                        "email": "old@example.com",
                        "orgUuid": "old-org",
                        "lastWarmAt": NOW - 60,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    account = _account("1", {"seven_day": {"pct": 1.0}})
    engine, _switcher, sessions, _events = _engine(tmp_path, [account])

    engine.tick()

    assert [call[0] for call in sessions.calls] == ["1"]


def test_legacy_state_protection_follows_identities_after_slots_swap(tmp_path):
    (tmp_path / "warmup_state.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "accounts": {
                    "1": {
                        "email": "user1@example.com",
                        "orgUuid": "org-1",
                        "lastWarmAt": NOW - 60,
                    },
                    "2": {
                        "email": "user2@example.com",
                        "orgUuid": "org-2",
                        "lastWarmAt": NOW - 60,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    first = _account("1", None)
    second = _account("2", None)
    first = replace(first, email="user2@example.com", org_uuid="org-2")
    second = replace(second, email="user1@example.com", org_uuid="org-1")
    engine, _switcher, sessions, events = _engine(tmp_path, [first, second])

    engine.tick()

    assert sessions.calls == []
    assert [event.kind for event in events] == ["recently-warmed", "recently-warmed"]


def test_state_keeps_separate_guards_for_users_in_one_organization(tmp_path):
    first = replace(_account("1", None), org_uuid="shared-org")
    second = replace(_account("2", None), org_uuid="shared-org")
    engine, _switcher, sessions, _events = _engine(tmp_path, [first, second])

    assert engine.tick().warmed == 2
    assert len(sessions.calls) == 2
    state = json.loads((tmp_path / "warmup_state.json").read_text(encoding="utf-8"))
    assert len(state["accounts"]) == 2

    second_sessions = _FakeSessions()
    engine, _switcher, _sessions, events = _engine(
        tmp_path,
        [first, second],
        sessions=second_sessions,
    )
    assert engine.tick().skipped == 2
    assert second_sessions.calls == []
    assert [event.kind for event in events] == ["recently-warmed", "recently-warmed"]


def test_dry_run_never_launches_or_writes_state(tmp_path):
    account = _account("1", {"seven_day": {"pct": 1.0}})
    engine, _switcher, sessions, events = _engine(tmp_path, [account], dry_run=True)

    summary = engine.tick()

    assert summary.would_warm == 1
    assert sessions.calls == []
    assert events[0].kind == "would-warm"
    assert not (tmp_path / "warmup_state.json").exists()


def test_failed_claude_request_is_not_recorded_as_warmed(tmp_path):
    account = _account("1", {"seven_day": {"pct": 1.0}})
    sessions = _FakeSessions({"1": 7})
    engine, _switcher, _sessions, events = _engine(
        tmp_path, [account], sessions=sessions
    )

    summary = engine.tick()

    assert summary.failed == 1
    assert events[0].kind == "failed"
    assert events[0].detail == "Claude exited with code 7; retry protected"
    state = json.loads((tmp_path / "warmup_state.json").read_text(encoding="utf-8"))
    key = "org:org-1|email:user1@example.com"
    assert "lastWarmAt" not in state["accounts"][key]
    assert state["accounts"][key]["pendingAt"] == NOW


def test_ambiguous_timeout_keeps_duplicate_spend_guard(tmp_path):
    account = _account("1", {"seven_day": {"pct": 1.0}})

    class TimedOutSessions:
        def run_prompt(self, *_args, **_kwargs):
            raise PromptOutcomeUnknown("request outcome is unknown")

    engine, _switcher, _sessions, events = _engine(
        tmp_path, [account], sessions=TimedOutSessions()
    )

    summary = engine.tick()

    assert summary.failed == 1
    assert events[0].kind == "failed"
    state = json.loads((tmp_path / "warmup_state.json").read_text(encoding="utf-8"))
    key = "org:org-1|email:user1@example.com"
    assert state["accounts"][key]["pendingAt"] == NOW


def test_prelaunch_state_covers_the_full_request_timeout(tmp_path):
    key = "org:org-1|email:user1@example.com"

    class InspectingSessions(_FakeSessions):
        def run_prompt(self, *_args, **_kwargs):
            state = json.loads(
                (tmp_path / "warmup_state.json").read_text(encoding="utf-8")
            )
            assert state["accounts"][key]["pendingAt"] == NOW + 120
            raise PromptOutcomeUnknown("request outcome is unknown")

    engine, _switcher, _sessions, _events = _engine(
        tmp_path,
        [_account("1", None)],
        sessions=InspectingSessions(),
    )

    assert engine.tick().failed == 1


@pytest.mark.parametrize("outcome", ["timeout", "nonzero"])
def test_unavailable_usage_does_not_retry_ambiguous_attempt_each_poll(
    tmp_path, outcome
):
    clock = _MutableClock()

    class AmbiguousSessions(_FakeSessions):
        def run_prompt(self, *args, **kwargs):
            if outcome == "timeout":
                self.calls.append((args[0], args[1], kwargs["timeout"]))
                raise PromptOutcomeUnknown("request outcome is unknown")
            return super().run_prompt(*args, **kwargs)

    sessions = AmbiguousSessions({"1": 7} if outcome == "nonzero" else None)
    engine, _switcher, _sessions, events = _engine(
        tmp_path,
        [_account("1", None)],
        sessions=sessions,
        clock=clock,
    )

    assert engine.tick().failed == 1
    clock.advance(600)
    assert engine.tick().skipped == 1

    assert len(sessions.calls) == 1
    assert events[-1].kind == "recently-warmed"


@pytest.mark.parametrize("outcome", ["timeout", "nonzero"])
def test_ambiguous_guard_starts_at_prompt_completion(tmp_path, outcome):
    clock = _MutableClock()

    class SlowAmbiguousSessions(_FakeSessions):
        def run_prompt(self, *args, **kwargs):
            if outcome == "timeout":
                self.calls.append((args[0], args[1], kwargs["timeout"]))
                clock.advance(700)
                raise PromptOutcomeUnknown("request outcome is unknown")
            result = super().run_prompt(*args, **kwargs)
            clock.advance(700)
            return result

    sessions = SlowAmbiguousSessions({"1": 7} if outcome == "nonzero" else None)
    engine, _switcher, _sessions, _events = _engine(
        tmp_path,
        [_account("1", None)],
        sessions=sessions,
        clock=clock,
    )

    assert engine.tick().failed == 1
    key = "org:org-1|email:user1@example.com"
    state = json.loads((tmp_path / "warmup_state.json").read_text(encoding="utf-8"))
    assert state["accounts"][key]["pendingAt"] == NOW + 700

    clock.advance(5 * 3600 - 1)
    assert engine.tick().skipped == 1
    assert len(sessions.calls) == 1
    clock.advance(1)
    assert engine.tick().failed == 1
    assert len(sessions.calls) == 2


def test_each_success_is_protected_from_its_actual_completion_time(tmp_path):
    clock = _MutableClock()

    class SlowSessions(_FakeSessions):
        def run_prompt(self, *args, **kwargs):
            result = super().run_prompt(*args, **kwargs)
            clock.advance(700)
            return result

    engine, _switcher, _sessions, _events = _engine(
        tmp_path,
        [_account("1", None), _account("2", None)],
        sessions=SlowSessions(),
        clock=clock,
    )

    assert engine.tick().warmed == 2

    state = json.loads((tmp_path / "warmup_state.json").read_text(encoding="utf-8"))
    first_key = "org:org-1|email:user1@example.com"
    second_key = "org:org-2|email:user2@example.com"
    assert state["accounts"][first_key]["lastWarmAt"] == NOW + 700
    assert state["accounts"][second_key]["lastWarmAt"] == NOW + 1400


def test_stop_during_a_sweep_prevents_later_account_requests(tmp_path):
    holder = {}

    class StoppingSessions(_FakeSessions):
        def run_prompt(self, *args, **kwargs):
            result = super().run_prompt(*args, **kwargs)
            holder["engine"].stop()
            return result

    sessions = StoppingSessions()
    engine, _switcher, _sessions, _events = _engine(
        tmp_path,
        [_account("1", None), _account("2", None)],
        sessions=sessions,
    )
    holder["engine"] = engine

    assert engine.tick().warmed == 1
    assert [call[0] for call in sessions.calls] == ["1"]


def _session_switcher(tmp_path: Path, *, active: bool):
    switcher = MagicMock()
    switcher.backup_dir = tmp_path
    switcher.lock_file = tmp_path / ".lock"
    switcher._logger = logging.getLogger("test-session-prompt")
    switcher.resolve_account.return_value = ("2", "user2@example.com", "org-2")
    switcher._account_kind.return_value = "oauth"
    switcher._get_current_account.return_value = (
        ("user2@example.com", "org-2") if active else ("other@example.com", "org-x")
    )
    return switcher


def _oauth_auth_status(argv=None):
    return subprocess.CompletedProcess(
        argv or [],
        0,
        json.dumps(
            {
                "loggedIn": True,
                "authMethod": "claude.ai",
                "apiProvider": "firstParty",
                "email": "user2@example.com",
                "orgId": "org-2",
            }
        ),
        "",
    )


def test_run_prompt_uses_default_profile_for_active_account_and_scrubs_overrides(
    tmp_path, monkeypatch
):
    switcher = _session_switcher(tmp_path, active=True)
    manager = SessionManager(switcher)
    seen = {}
    for name in AUTH_OVERRIDE_ENV_VARS:
        monkeypatch.setenv(name, "must-not-leak")
    provider_routes = {
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_USE_MANTLE",
        "CLAUDE_CODE_USE_ANTHROPIC_AWS",
        "ANTHROPIC_BASE_URL",
    }
    for name in provider_routes:
        monkeypatch.setenv(name, "1")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)

    def fake_run(argv, **kwargs):
        if argv[-3:] == ["auth", "status", "--json"]:
            assert "--safe-mode" in argv
            assert all(name not in kwargs["env"] for name in provider_routes)
            return _oauth_auth_status(argv)
        seen.update(argv=argv, **kwargs)
        return subprocess.CompletedProcess(argv, 0, "OK\n", "")

    with patch("claude_swap.session.shutil.which", return_value="C:/bin/claude.exe"), patch(
        "claude_swap.session.subprocess.run", side_effect=fake_run
    ):
        result = manager.run_prompt("2", ["--print", "hi"], timeout=30)

    assert result.returncode == 0
    assert seen["argv"] == ["C:/bin/claude.exe", "--print", "hi"]
    assert seen["shell"] is False
    assert seen["capture_output"] is True
    assert seen["text"] is True
    assert seen["timeout"] == 30
    assert "CLAUDE_CONFIG_DIR" not in seen["env"]
    assert "CLAUDE_SECURESTORAGE_CONFIG_DIR" not in seen["env"]
    assert all(name not in seen["env"] for name in AUTH_OVERRIDE_ENV_VARS)
    assert all(name not in seen["env"] for name in provider_routes)


def test_run_prompt_holds_account_lock_while_child_runs(tmp_path, monkeypatch):
    switcher = _session_switcher(tmp_path, active=True)
    manager = SessionManager(switcher)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)

    def fake_run(argv, **_kwargs):
        peer = FileLock(switcher.lock_file, timeout=0)
        assert peer.acquire() is False
        if argv[-3:] == ["auth", "status", "--json"]:
            return _oauth_auth_status(argv)
        return subprocess.CompletedProcess(argv, 0, "OK", "")

    with patch("claude_swap.session.shutil.which", return_value="claude"), patch(
        "claude_swap.session.subprocess.run", side_effect=fake_run
    ):
        manager.run_prompt("2", ["--print", "hi"], timeout=30)


def test_run_prompt_rechecks_active_account_after_profile_setup(tmp_path, monkeypatch):
    switcher = _session_switcher(tmp_path, active=False)
    switcher._get_current_account.side_effect = [
        ("other@example.com", "org-x"),
        ("user2@example.com", "org-2"),
    ]
    manager = SessionManager(switcher)
    profile = tmp_path / "sessions" / "2-user2"
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)
    with patch.object(
        manager, "setup_session", return_value=(profile, "2", "user2@example.com")
    ), patch("claude_swap.session.shutil.which", return_value="claude"), patch(
        "claude_swap.session.subprocess.run",
        side_effect=[
            _oauth_auth_status(),
            subprocess.CompletedProcess([], 0, "OK", ""),
        ],
    ) as run:
        manager.run_prompt(
            "2",
            ["--print", "hi"],
            timeout=30,
            expected_identity=("user2@example.com", "org-2"),
        )

    assert "CLAUDE_CONFIG_DIR" not in run.call_args.kwargs["env"]


def test_run_prompt_rejects_changed_slot_identity(tmp_path, monkeypatch):
    switcher = _session_switcher(tmp_path, active=True)
    manager = SessionManager(switcher)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)

    with patch("claude_swap.session.shutil.which", return_value="claude"):
        with pytest.raises(SessionError, match="changed since the usage check"):
            manager.run_prompt(
                "2",
                ["--print", "hi"],
                timeout=30,
                expected_identity=("someone-else@example.com", "org-else"),
            )


def test_run_prompt_uses_isolated_profile_for_inactive_account(tmp_path, monkeypatch):
    switcher = _session_switcher(tmp_path, active=False)
    manager = SessionManager(switcher)
    profile = tmp_path / "sessions" / "2-user2"
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)
    with patch.object(
        manager,
        "setup_session",
        return_value=(profile, "2", "user2@example.com"),
    ) as setup, patch(
        "claude_swap.session.read_session_identity",
        return_value=("user2@example.com", "org-2"),
    ), patch(
        "claude_swap.session.shutil.which", return_value="claude"
    ), patch(
        "claude_swap.session.subprocess.run",
        side_effect=[
            _oauth_auth_status(),
            subprocess.CompletedProcess([], 0, "OK", ""),
        ],
    ) as run:
        manager.run_prompt("2", ["--print", "hi"], timeout=30)

    # Slot numbers remain unambiguous when two organizations share an email.
    setup.assert_called_once_with(
        "2",
        share=False,
        share_history=False,
        sync_sharing=False,
        refresh_credentials=False,
        expected_identity=("user2@example.com", "org-2"),
    )
    assert run.call_args.kwargs["env"]["CLAUDE_CONFIG_DIR"] == str(profile)


def test_run_prompt_rejects_aba_profile_identity_drift(tmp_path, monkeypatch):
    switcher = _session_switcher(tmp_path, active=False)
    manager = SessionManager(switcher)
    profile = tmp_path / "sessions" / "2-user2"
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)

    with patch.object(
        manager,
        "setup_session",
        return_value=(profile, "2", "user2@example.com"),
    ), patch(
        "claude_swap.session.read_session_identity",
        return_value=("user2@example.com", "org-other"),
    ), patch("claude_swap.session.shutil.which", return_value="claude"), patch(
        "claude_swap.session.subprocess.run",
        return_value=subprocess.CompletedProcess([], 0, "OK", ""),
    ) as run:
        with pytest.raises(SessionError, match="prepared profile identity"):
            manager.run_prompt(
                "2",
                ["--print", "hi"],
                timeout=30,
                expected_identity=("user2@example.com", "org-2"),
            )

    run.assert_not_called()


def test_run_prompt_rejects_third_party_provider_preflight(tmp_path, monkeypatch):
    switcher = _session_switcher(tmp_path, active=True)
    manager = SessionManager(switcher)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)
    third_party = subprocess.CompletedProcess(
        [],
        0,
        json.dumps(
            {
                "loggedIn": True,
                "authMethod": "third_party",
                "apiProvider": "bedrock",
            }
        ),
        "",
    )

    with patch("claude_swap.session.shutil.which", return_value="claude"), patch(
        "claude_swap.session.subprocess.run", return_value=third_party
    ) as run:
        with pytest.raises(SessionError, match="first-party Claude OAuth"):
            manager.run_prompt("2", ["--print", "hi"], timeout=30)

    run.assert_called_once()


def test_run_prompt_rejects_same_email_with_different_org(tmp_path, monkeypatch):
    switcher = _session_switcher(tmp_path, active=True)
    manager = SessionManager(switcher)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)
    wrong_org = _oauth_auth_status()
    status = json.loads(wrong_org.stdout)
    status["orgId"] = ""
    wrong_org = subprocess.CompletedProcess([], 0, json.dumps(status), "")

    with patch("claude_swap.session.shutil.which", return_value="claude"), patch(
        "claude_swap.session.subprocess.run", return_value=wrong_org
    ) as run:
        with pytest.raises(SessionError, match="authenticated identity changed"):
            manager.run_prompt("2", ["--print", "hi"], timeout=30)

    run.assert_called_once()


def test_setup_prompt_session_rejects_changed_identity_before_credentials(tmp_path):
    switcher = _session_switcher(tmp_path, active=False)
    manager = SessionManager(switcher)

    with pytest.raises(SessionError, match="refusing to touch"):
        manager.setup_session(
            "2",
            share=False,
            share_history=False,
            sync_sharing=False,
            refresh_credentials=False,
            expected_identity=("user2@example.com", "org-other"),
        )

    switcher.read_account_credentials.assert_not_called()


def test_run_prompt_rejects_nested_config_profile(tmp_path, monkeypatch):
    switcher = _session_switcher(tmp_path, active=True)
    manager = SessionManager(switcher)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "already-isolated"))

    with pytest.raises(Exception, match="outside a Claude session"):
        manager.run_prompt("2", ["--print", "hi"], timeout=30)


def test_main_dispatches_warmup_subcommand(monkeypatch):
    called = []
    monkeypatch.setattr("sys.argv", ["cswap", "warmup", "--once"])
    with patch("claude_swap.cli._warmup_command", side_effect=called.append):
        cli.main()
    assert called == [["--once"]]


def test_warmup_help_describes_unavailable_usage_fallback(capsys):
    with pytest.raises(SystemExit) as exc:
        cli._warmup_command(["--help"])

    assert exc.value.code == 0
    assert "usage remains unavailable" in capsys.readouterr().out


def test_warmup_command_builds_one_shot_dry_run_engine(tmp_path):
    from claude_swap.warmup import WarmupSummary

    fake_switcher = SimpleNamespace(
        backup_dir=tmp_path,
        _logger=logging.getLogger("test-warmup-cli"),
        _is_running_in_container=lambda: True,
    )
    fake_engine = MagicMock()
    fake_engine.tick.return_value = WarmupSummary(would_warm=2)
    with patch("claude_swap.cli.ClaudeAccountSwitcher", return_value=fake_switcher), patch(
        "claude_swap.warmup.WarmupEngine", return_value=fake_engine
    ) as engine_cls:
        with pytest.raises(SystemExit) as exc:
            cli._warmup_command(
                [
                    "--once",
                    "--dry-run",
                    "--interval",
                    "900",
                    "--timeout",
                    "45",
                ]
            )

    assert exc.value.code == 0
    kwargs = engine_cls.call_args.kwargs
    assert kwargs["dry_run"] is True
    assert kwargs["interval_seconds"] == 900
    assert kwargs["timeout_seconds"] == 45
    assert kwargs["model"] == "claude-haiku-4-5"
    fake_engine.tick.assert_called_once_with()


@pytest.mark.parametrize(
    "argv",
    [
        ["--interval", "299"],
        ["--interval", "nan"],
        ["--interval", "inf"],
        ["--timeout", "0"],
    ],
)
def test_warmup_command_rejects_unsafe_values(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        cli._warmup_command(argv)
    assert exc.value.code == 2


def test_loop_exit_code_recovers_after_a_successful_tick(tmp_path):
    from claude_swap.warmup import WarmupSummary

    account = _account("1", {"seven_day": {"pct": 1.0}})
    engine, _switcher, _sessions, _events = _engine(tmp_path, [account])
    engine.tick = MagicMock(
        side_effect=[WarmupSummary(failed=1), WarmupSummary(skipped=1)]
    )
    engine._stopped = MagicMock()
    engine._stopped.is_set.return_value = False
    engine._stopped.wait.side_effect = [False, True]

    assert engine.run_loop() == 0
