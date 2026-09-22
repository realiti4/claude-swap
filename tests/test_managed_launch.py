"""Placement and `cswap run --auto` (Refs #382)."""

from __future__ import annotations

import inspect
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from claude_swap import macos_keychain
from claude_swap import managed_launch as ml
from claude_swap import oauth
from claude_swap import session as session_mod
from claude_swap import session_credentials
from claude_swap.balance import AccountScore, BalanceParams
from claude_swap.exceptions import LockError, SessionError
from claude_swap.managed_refresh import AccessResolution
from claude_swap.managed_sessions import (
    LAUNCH_LOCK_TIMEOUT_S,
    AccountRef,
    ManagedSessionRegistry,
    is_managed_session_id,
)
from claude_swap.models import Platform
from claude_swap.session import keychain_service_name, session_dir_for
from claude_swap.usage_store import UsageEntry

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="managed sessions are POSIX-only (v1)"
)

USAGE = {"five_hour": {"pct": 10.0}, "seven_day": {"pct": 10.0}}
IDS = {
    "1": AccountRef("lane0@example.com", "org-1"),
    "2": AccountRef("b@example.com", "org-2"),
    "3": AccountRef("c@example.com", "org-3"),
}


def fake_rank(usage_by_account, *, now, models, busy_sessions, params):
    """Deterministic stand-in: every known account eligible, fewer busy wins,
    ties keep input order (as the contract's rank_accounts does)."""
    rows = [
        AccountScore(
            account=n, eligible=True, score=-float(busy_sessions.get(n, 0)),
            slack=0.0, projected_5h=0.0, headroom=90.0,
            recovery_ts=float("inf"), reason="ok",
        )
        for n, u in usage_by_account.items() if u is not None
    ]
    return sorted(rows, key=lambda r: -r.score)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


class TestChoosePlacement:
    @pytest.fixture(autouse=True)
    def _fake_rank(self, monkeypatch):
        monkeypatch.setattr(ml, "rank_accounts", fake_rank)

    @staticmethod
    def _choose(*, usage=None, lane0="1", busy=None, candidates=("1", "2", "3")):
        return ml.choose_placement(
            candidates=list(candidates), identities=IDS,
            usage=usage if usage is not None else {n: USAGE for n in IDS},
            lane0=lane0, busy=busy or {}, now=1_000_000.0, models=(),
            params=BalanceParams(),
        )

    def test_lane0_is_excluded_when_others_have_room(self):
        placement = self._choose()
        assert (placement.number, placement.source, placement.account) == ("2", "backup", IDS["2"])

    def test_busy_sessions_steer_placement(self):
        assert self._choose(busy={"2": 2}).number == "3"

    def test_lane0_is_the_last_resort(self):
        placement = self._choose(usage={"1": USAGE, "2": None, "3": None})
        assert (placement.number, placement.source) == ("1", "lane0")

    def test_lane0_outside_candidates_is_no_fallback(self):
        assert self._choose(usage={"1": USAGE, "2": None, "3": None}, candidates=("2", "3")) is None

    def test_no_known_usage_places_nothing(self):
        assert self._choose(usage={"1": None, "2": None, "3": None}) is None

    def test_an_empty_candidate_pool_places_nothing(self):
        assert self._choose(candidates=()) is None

    def test_without_a_lane0_account_every_candidate_competes(self):
        assert self._choose(lane0=None, busy={"2": 1, "3": 1}).number == "1"


class TestChoosePlacementWithRealPolicy:
    def test_account_over_the_weekly_threshold_loses(self):
        now = time.time()
        week_reset, five_reset = _iso(now + 2 * 86400), _iso(now + 3600)
        over = {"five_hour": {"pct": 5.0, "resets_at": five_reset},
                "seven_day": {"pct": 99.0, "resets_at": week_reset}}
        fine = {"five_hour": {"pct": 5.0, "resets_at": five_reset},
                "seven_day": {"pct": 40.0, "resets_at": week_reset}}
        placement = ml.choose_placement(
            candidates=["1", "2", "3"], identities=IDS,
            usage={"1": fine, "2": over, "3": fine}, lane0="1", busy={},
            now=now, models=(), params=BalanceParams(),
        )
        assert placement.number == "3"


class _ExecCalled(Exception):
    """What a launch under ``capture_exec`` raises instead of exec'ing."""

    def __init__(self, binary, argv, env):
        self.binary, self.argv, self.env = binary, argv, env


@pytest.fixture
def capture_exec(monkeypatch):
    def fake_exec(self, claude_bin, claude_args, env):
        raise _ExecCalled(claude_bin, [claude_bin, *claude_args], env)

    monkeypatch.setattr(session_mod.SessionManager, "_exec", fake_exec)
    monkeypatch.setattr(session_mod.shutil, "which", lambda name: f"/fake/bin/{name}")


def _serve_usage(monkeypatch, switcher, known: dict[str, bool]) -> list:
    """Serve fixed usage per account and record each pass's ``fetch`` argument."""
    calls: list = []

    def entries(fetch=None, *, scheduled=False):
        calls.append(fetch)
        return {
            n: UsageEntry(last_good=USAGE, fetched_at=time.time(), age_s=0.0) if ok else UsageEntry()
            for n, ok in known.items()
        }

    monkeypatch.setattr(switcher, "usage_entries_by_account", entries)
    monkeypatch.setattr(ml, "rank_accounts", fake_rank)
    return calls


@pytest.fixture
def shared_history(temp_home):
    (temp_home / ".claude" / "projects").mkdir(parents=True, exist_ok=True)
    (temp_home / ".claude" / "history.jsonl").write_text("")


def _launch(switcher, args=()):
    with pytest.raises(_ExecCalled) as exc:
        ml.run_auto(switcher, list(args))
    return exc.value


def _auto_dirs(switcher) -> list[Path]:
    root = switcher.backup_dir / "sessions"
    return [p for p in root.iterdir() if is_managed_session_id(p.name)] if root.is_dir() else []


class TestRunAuto:
    def test_launches_on_the_best_non_lane0_account(
        self, managed_switcher, capture_exec, shared_history, monkeypatch
    ):
        calls = _serve_usage(monkeypatch, managed_switcher, {"1": True, "2": True, "3": True})
        call = _launch(managed_switcher, ["--resume"])

        session_dir = Path(call.env["CLAUDE_CONFIG_DIR"])
        assert is_managed_session_id(session_dir.name)
        assert session_dir.parent == managed_switcher.backup_dir / "sessions"
        assert call.argv == [
            "/fake/bin/claude", "--settings", str(session_dir / "cswap-hooks.json"), "--resume",
        ]
        entry = ManagedSessionRegistry(managed_switcher.backup_dir).get(session_dir.name)
        assert entry.account == IDS["2"]
        assert (entry.pid, entry.source, entry.last_reason) == (os.getpid(), "backup", "launch")
        blob = json.loads((session_dir / ".credentials.json").read_text())["claudeAiOauth"]
        assert blob["accessToken"] == "at-2" and "refreshToken" not in blob
        assert blob["subscriptionType"] == "max"
        assert entry.access_fingerprint == oauth.access_token_fingerprint(
            (session_dir / ".credentials.json").read_text()
        )
        config = json.loads((session_dir / ".claude.json").read_text())
        assert config["oauthAccount"]["emailAddress"] == "b@example.com"
        assert (config["theme"], config["hasCompletedOnboarding"]) == ("light", True)
        assert (session_dir / "projects").is_symlink()
        assert (session_dir / "history.jsonl").is_symlink()
        assert json.loads((session_dir / "cswap-hooks.json").read_text()) == {"hooks": {}}
        assert calls == [set()]

    def test_second_launch_spreads_to_another_account(
        self, managed_switcher, capture_exec, shared_history, monkeypatch
    ):
        _serve_usage(monkeypatch, managed_switcher, {"1": True, "2": True, "3": True})
        first = Path(_launch(managed_switcher).env["CLAUDE_CONFIG_DIR"])
        second = Path(_launch(managed_switcher).env["CLAUDE_CONFIG_DIR"])
        registry = ManagedSessionRegistry(managed_switcher.backup_dir)
        assert {registry.get(first.name).account, registry.get(second.name).account} == {
            IDS["2"], IDS["3"],
        }

    def test_lane0_fallback_copies_the_access_token_only(
        self, managed_switcher, capture_exec, shared_history, monkeypatch, temp_home
    ):
        calls = _serve_usage(monkeypatch, managed_switcher, {"1": True, "2": False, "3": False})
        live_before = (temp_home / ".claude" / ".credentials.json").read_text()
        session_dir = Path(_launch(managed_switcher).env["CLAUDE_CONFIG_DIR"])
        blob = json.loads((session_dir / ".credentials.json").read_text())["claudeAiOauth"]
        assert blob == {"accessToken": "at-live-1", "expiresAt": blob["expiresAt"]}
        entry = ManagedSessionRegistry(managed_switcher.backup_dir).get(session_dir.name)
        assert (entry.account, entry.source) == (IDS["1"], "lane0")
        assert (temp_home / ".claude" / ".credentials.json").read_text() == live_before
        assert calls == [set(), None]  # lane-0 fallback re-checks with a fresh fetch

    def test_account_with_live_run_profile_is_held_out(
        self, managed_switcher, capture_exec, shared_history, monkeypatch
    ):
        _serve_usage(monkeypatch, managed_switcher, {"1": True, "2": True, "3": True})
        profile = session_dir_for(managed_switcher.backup_dir, "2", "b@example.com")
        (profile / "sessions").mkdir(parents=True)
        (profile / "sessions" / f"{os.getpid()}.json").write_text(json.dumps({"pid": os.getpid()}))
        session_dir = Path(_launch(managed_switcher).env["CLAUDE_CONFIG_DIR"])
        assert ManagedSessionRegistry(managed_switcher.backup_dir).get(session_dir.name).account == IDS["3"]

    def test_quarantined_account_is_held_out(
        self, managed_switcher, capture_exec, shared_history, monkeypatch
    ):
        _serve_usage(monkeypatch, managed_switcher, {"1": True, "2": True, "3": True})
        (managed_switcher.backup_dir / "autoswitch_state.json").write_text(
            json.dumps({"quarantine": {"2": {"email": "b@example.com"}}})
        )
        session_dir = Path(_launch(managed_switcher).env["CLAUDE_CONFIG_DIR"])
        assert ManagedSessionRegistry(managed_switcher.backup_dir).get(session_dir.name).account == IDS["3"]

    def test_auth_override_env_is_scrubbed(
        self, managed_switcher, capture_exec, shared_history, monkeypatch
    ):
        _serve_usage(monkeypatch, managed_switcher, {"1": True, "2": True, "3": True})
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-x")
        assert "ANTHROPIC_API_KEY" not in _launch(managed_switcher).env

    def test_the_exported_config_dir_is_what_the_keychain_item_was_hashed_from(
        self, managed_switcher, capture_exec, shared_history, monkeypatch,
        block_real_keychain,
    ):
        """Claude finds a profile's keychain item by hashing the raw
        CLAUDE_CONFIG_DIR string it was given, so the launch has to export
        exactly the path the credential was seeded under -- a resolved or
        otherwise re-spelled variant hashes elsewhere and claude sees no item."""
        _serve_usage(monkeypatch, managed_switcher, {"1": True, "2": True, "3": True})
        exported = _launch(managed_switcher).env["CLAUDE_CONFIG_DIR"]
        item = block_real_keychain.data[(
            keychain_service_name(exported), macos_keychain.keychain_account_name()
        )]
        assert item == (Path(exported) / ".credentials.json").read_text()

    def test_an_unreadable_keychain_still_launches(
        self, managed_switcher, capture_exec, shared_history, monkeypatch, capsys
    ):
        """The profile was created moments ago, so nothing is in the Keychain
        to shadow the seeded credential: claude finds no item and reads the
        plaintext, which holds the right token. Refusing here would block a
        launch that works whenever the login keychain is locked (ssh, a
        headless Mac). The user is told the token is unprotected instead."""
        _serve_usage(monkeypatch, managed_switcher, {"1": True, "2": True, "3": True})

        def unreadable(session_dir):
            raise macos_keychain.KeychainError("timed out")

        monkeypatch.setattr(session_credentials, "_read_keychain", unreadable)
        session_dir = Path(_launch(managed_switcher).env["CLAUDE_CONFIG_DIR"])
        credentials = (session_dir / ".credentials.json").read_text()
        assert json.loads(credentials)["claudeAiOauth"]["accessToken"] == "at-2"
        entry = ManagedSessionRegistry(managed_switcher.backup_dir).get(session_dir.name)
        assert entry.access_fingerprint == oauth.access_token_fingerprint(credentials)
        captured = capsys.readouterr()
        assert "not in the Keychain" in captured.out + captured.err

    def test_refuses_on_windows(self, managed_switcher, capture_exec):
        managed_switcher.platform = Platform.WINDOWS
        with pytest.raises(SessionError, match="not supported on Windows"):
            ml.run_auto(managed_switcher, [])
        assert not (managed_switcher.backup_dir / "sessions" / "managed.json").exists()

    def test_no_account_available(self, managed_switcher, capture_exec, monkeypatch):
        _serve_usage(monkeypatch, managed_switcher, {"1": False, "2": False, "3": False})
        with pytest.raises(SessionError, match="No account has room"):
            ml.run_auto(managed_switcher, [])
        assert ManagedSessionRegistry(managed_switcher.backup_dir).entries() == {}

    def test_failure_after_allocation_cleans_up(
        self, managed_switcher, capture_exec, shared_history, monkeypatch
    ):
        _serve_usage(monkeypatch, managed_switcher, {"1": True, "2": True, "3": True})
        monkeypatch.setattr(
            ml, "resolve_access_credential",
            lambda switcher, account, **kw: AccessResolution(account, "2", "transient"),
        )
        with pytest.raises(SessionError, match="transient"):
            ml.run_auto(managed_switcher, [])
        assert ManagedSessionRegistry(managed_switcher.backup_dir).entries() == {}
        assert _auto_dirs(managed_switcher) == []

    def test_a_fixed_clock_stamps_the_registry_entry(
        self, managed_switcher, capture_exec, shared_history, monkeypatch
    ):
        """The injected clock is the one the reservation is stamped with."""
        _serve_usage(monkeypatch, managed_switcher, {"1": True, "2": True, "3": True})
        with pytest.raises(_ExecCalled) as exc:
            ml.run_auto(managed_switcher, [], clock=lambda: 1_700_000_000.0)
        session_dir = Path(exc.value.env["CLAUDE_CONFIG_DIR"])
        entry = ManagedSessionRegistry(managed_switcher.backup_dir).get(session_dir.name)
        assert entry.created_at == entry.last_assigned_at == "2023-11-14T22:13:20Z"

    def test_the_lane0_borrow_is_announced(
        self, managed_switcher, capture_exec, shared_history, monkeypatch, capsys
    ):
        """Borrowing the default login's token is the one placement the user
        has to be told about: that session is spending lane 0's own window."""
        _serve_usage(monkeypatch, managed_switcher, {"1": True, "2": False, "3": False})
        _launch(managed_switcher)
        assert "borrows the default login's current token" in capsys.readouterr().out

    def test_a_banner_that_cannot_be_encoded_rolls_the_launch_back(
        self, managed_switcher, capture_exec, shared_history, monkeypatch
    ):
        """The banner prints an account's email, so a non-ASCII address under
        a byte-oriented locale fails on encoding rather than on I/O. The
        session never started either way: it must not keep its row or its
        profile."""
        _serve_usage(monkeypatch, managed_switcher, {"1": True, "2": True, "3": True})

        def unencodable(*args, **kwargs):
            raise UnicodeEncodeError("ascii", "n@exämple.test", 3, 4, "ordinal not in range")

        monkeypatch.setattr(ml, "announce_launch", unencodable)
        with pytest.raises(UnicodeEncodeError):
            ml.run_auto(managed_switcher, [])
        assert _auto_dirs(managed_switcher) == []
        assert ManagedSessionRegistry(managed_switcher.backup_dir).entries() == {}

    def test_a_colliding_profile_directory_is_left_alone(
        self, managed_switcher, capture_exec, shared_history, monkeypatch
    ):
        """A directory already at the minted id belongs to somebody else (an
        orphan inside its grace window, a session we could not see), so the
        rollback withdraws only the reservation."""
        _serve_usage(monkeypatch, managed_switcher, {"1": True, "2": True, "3": True})
        monkeypatch.setattr(ml, "new_session_id", lambda: "auto-deadbeef")
        squatter = managed_switcher.backup_dir / "sessions" / "auto-deadbeef"
        squatter.mkdir(parents=True)
        (squatter / "keep-me").write_text("not ours")
        with pytest.raises(SessionError, match="already exists"):
            ml.run_auto(managed_switcher, [])
        assert (squatter / "keep-me").read_text() == "not ours"
        assert ManagedSessionRegistry(managed_switcher.backup_dir).entries() == {}

    def test_a_squatted_profile_survives_a_failure_before_it_was_created(
        self, managed_switcher, capture_exec, shared_history, monkeypatch
    ):
        """The rollback may only delete a directory THIS launch created.
        The credential is resolved before `create_managed_profile` runs, so
        every failure in that window -- a non-ok resolution (the common
        one), a Ctrl-C during the network refresh, the theme lookup raising
        -- reaches the rollback with a path that may already belong to
        somebody else: a live session whose reservation this launch could
        not see, or an orphan still inside the sweeper's grace window. 8 hex
        digits of id are exactly the assumption `create_managed_profile`
        refuses to rely on."""
        _serve_usage(monkeypatch, managed_switcher, {"1": True, "2": True, "3": True})
        monkeypatch.setattr(ml, "new_session_id", lambda: "auto-deadbeef")
        squatter = managed_switcher.backup_dir / "sessions" / "auto-deadbeef"
        squatter.mkdir(parents=True)
        (squatter / "keep-me").write_text("not ours")
        monkeypatch.setattr(
            ml, "resolve_access_credential",
            lambda switcher, account, **kw: AccessResolution(account, "2", "transient"),
        )

        with pytest.raises(SessionError, match="transient"):
            ml.run_auto(managed_switcher, [])

        assert (squatter / "keep-me").read_text() == "not ours"
        assert ManagedSessionRegistry(managed_switcher.backup_dir).entries() == {}

    def test_the_launch_path_waits_longer_for_the_registry_lock(
        self, managed_switcher, capture_exec, shared_history, monkeypatch
    ):
        """`allocate` holds the registry lock across a `ps` per entry and a
        record read per entry, so a couple of slow probes in one launch can
        outlast the pollers' wait in a concurrent one and turn it into a
        lock error on the launch path."""
        _serve_usage(monkeypatch, managed_switcher, {"1": True, "2": True, "3": True})
        seen: dict = {}
        real = ml.ManagedSessionRegistry

        def recording(backup_dir, **kwargs):
            seen.update(kwargs)
            return real(backup_dir, **kwargs)

        monkeypatch.setattr(ml, "ManagedSessionRegistry", recording)
        _launch(managed_switcher)

        poller_default = inspect.signature(
            ManagedSessionRegistry
        ).parameters["lock_timeout"].default
        assert seen["lock_timeout"] == LAUNCH_LOCK_TIMEOUT_S > poller_default

    def test_the_placement_score_and_its_reason_are_logged(
        self, managed_switcher, capture_exec, shared_history, monkeypatch, caplog
    ):
        """The row records the constant reason "launch", so the ranking that
        actually chose this account survives nowhere else -- a user asking
        "why this account?" afterwards has only the debug log to go on."""
        _serve_usage(monkeypatch, managed_switcher, {"1": True, "2": True, "3": True})
        with caplog.at_level("DEBUG", logger="claude-swap"):
            session_dir = Path(_launch(managed_switcher).env["CLAUDE_CONFIG_DIR"])

        placed = [
            r.getMessage() for r in caplog.records if session_dir.name in r.getMessage()
        ]
        assert placed
        assert any(
            "score=" in m and "reason=" in m and "b@example.com" in m for m in placed
        )

    def test_a_failed_handover_rolls_the_launch_back(
        self, managed_switcher, capture_exec, shared_history, monkeypatch
    ):
        """exec itself failing means the session never started: it must not
        leave a reservation counting as busy load behind."""
        _serve_usage(monkeypatch, managed_switcher, {"1": True, "2": True, "3": True})

        def failing_exec(self, claude_bin, claude_args, env):
            raise OSError("exec format error")

        monkeypatch.setattr(session_mod.SessionManager, "_exec", failing_exec)
        with pytest.raises(OSError, match="exec format error"):
            ml.run_auto(managed_switcher, [])
        assert ManagedSessionRegistry(managed_switcher.backup_dir).entries() == {}
        assert _auto_dirs(managed_switcher) == []

    def test_a_failing_rollback_keeps_the_original_error(
        self, managed_switcher, capture_exec, shared_history, monkeypatch
    ):
        """A registry the rollback cannot take the row out of (a contended
        lock) must not replace the failure the user needs to see, nor cost
        the profile its removal."""
        _serve_usage(monkeypatch, managed_switcher, {"1": True, "2": True, "3": True})
        monkeypatch.setattr(
            ml, "resolve_access_credential",
            lambda switcher, account, **kw: AccessResolution(account, "2", "transient"),
        )

        def unavailable(self, session_id):
            raise LockError("registry lock busy")

        monkeypatch.setattr(ManagedSessionRegistry, "remove", unavailable)
        with pytest.raises(SessionError, match="transient"):
            ml.run_auto(managed_switcher, [])
        assert _auto_dirs(managed_switcher) == []

    def test_missing_claude_binary(self, managed_switcher, monkeypatch):
        monkeypatch.setattr(session_mod.shutil, "which", lambda name: None)
        with pytest.raises(SessionError, match="not found on PATH"):
            ml.run_auto(managed_switcher, [])
