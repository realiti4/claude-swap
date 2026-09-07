"""CodexSwitcher: the verbs, and the two rules that make them correct."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_swap.codex import paths as cpaths
from claude_swap.codex.auth_file import account_key
from claude_swap.codex.oauth import RefreshOutcome
from claude_swap.codex.store import CodexStore
from claude_swap.codex.switcher import CodexSwitcher
from claude_swap.codex.usage import UsageFetch
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.locking import FileLock
from claude_swap.providers.base import ProviderSwitcher
from tests.conftest_codex import make_auth_json

ACCT_A, USER_A = "acct-a", "user-a"
ACCT_B, USER_B = "acct-b", "user-b"
KEY_A = account_key(USER_A, ACCT_A)
KEY_B = account_key(USER_B, ACCT_B)


@pytest.fixture
def seeded(codex_home: Path) -> CodexSwitcher:
    """Two managed accounts, A live."""
    store = CodexStore()
    for key, acct, user, email in (
        (KEY_A, ACCT_A, USER_A, "a@x"),
        (KEY_B, ACCT_B, USER_B, "b@x"),
    ):
        store.upsert_slot(key, email=email, plan="pro")
        store.write_snapshot(key, make_auth_json(account_id=acct, user_id=user, email=email))
    store.set_active(KEY_A)
    (codex_home / "auth.json").write_text(
        json.dumps(make_auth_json(account_id=ACCT_A, user_id=USER_A, email="a@x"))
    )
    return CodexSwitcher()


@pytest.fixture
def no_usage(monkeypatch):
    """Neutralise the network for snapshot tests."""
    monkeypatch.setattr(
        "claude_swap.codex.usage_cache.fetch_usage", lambda *a, **k: UsageFetch(usage={})
    )


def _held_lock(monkeypatch, switcher: CodexSwitcher) -> FileLock:
    """Take the store lock from 'another process' and make the switcher impatient."""
    held = FileLock(cpaths.get_codex_lock_path(), timeout=0.1)
    assert held.acquire() is True
    monkeypatch.setattr(
        switcher,
        "_lock",
        lambda timeout=0.1: FileLock(cpaths.get_codex_lock_path(), timeout=0.1),
    )
    return held


# ---- the seam ----------------------------------------------------------


def test_codex_switcher_satisfies_the_provider_protocol():
    assert issubclass(CodexSwitcher, ProviderSwitcher)
    assert CodexSwitcher.provider_id == "codex"


# ---- rule 1: the live file decides who is active ------------------------


def test_active_account_is_derived_from_the_live_file(seeded: CodexSwitcher):
    """The codex CLI can rewrite auth.json behind us. Trusting our own registry
    would make `status` and the TUI lie after every such write."""
    CodexStore().set_active(KEY_B)  # registry says B, live file still holds A
    assert seeded.current_account_number() == "1"


def test_active_is_none_when_the_live_login_is_unmanaged(seeded: CodexSwitcher, codex_home: Path):
    (codex_home / "auth.json").write_text(
        json.dumps(make_auth_json(account_id="stranger", user_id="user-z"))
    )
    assert seeded.current_account_number() is None


def test_active_is_none_when_there_is_no_live_login(seeded: CodexSwitcher, codex_home: Path):
    (codex_home / "auth.json").unlink()
    assert seeded.current_account_number() is None


def test_switch_captures_the_outgoing_login_into_its_own_slot(
    seeded: CodexSwitcher, codex_home: Path
):
    """The captured tokens go to the slot the LIVE file's identity matches —
    not to whatever the registry believed was active. That is what repairs a
    clobber instead of writing one account's tokens over another's."""
    CodexStore().set_active(KEY_B)  # registry is wrong; live file is A
    rotated = make_auth_json(
        account_id=ACCT_A, user_id=USER_A, email="a@x", refresh_token="rt-rotated"
    )
    (codex_home / "auth.json").write_text(json.dumps(rotated))

    seeded.switch_to("2")

    assert CodexStore().read_snapshot(KEY_A)["tokens"]["refresh_token"] == "rt-rotated"
    # ...and B's own snapshot was not overwritten with A's tokens
    assert CodexStore().read_snapshot(KEY_B)["tokens"]["account_id"] == ACCT_B


def test_an_unmanaged_live_login_is_not_captured_into_any_slot(
    seeded: CodexSwitcher, codex_home: Path
):
    before = CodexStore().read_snapshot(KEY_A)
    (codex_home / "auth.json").write_text(
        json.dumps(make_auth_json(account_id="stranger", user_id="user-z"))
    )
    seeded.switch_to("2")
    assert CodexStore().read_snapshot(KEY_A) == before


# ---- switching ---------------------------------------------------------


def test_switch_writes_the_target_snapshot_to_the_live_file(
    seeded: CodexSwitcher, codex_home: Path
):
    seeded.switch_to("2")
    live = json.loads((codex_home / "auth.json").read_text())
    assert live["tokens"]["account_id"] == ACCT_B


def test_switch_records_the_new_active_slot(seeded: CodexSwitcher):
    seeded.switch_to("2")
    assert CodexStore().active_key() == KEY_B


def test_switch_reports_running_codex_sessions(seeded: CodexSwitcher, monkeypatch):
    monkeypatch.setattr("claude_swap.codex.switcher.running_codex_pids", lambda: [4242])
    assert seeded.switch_to("2").running_pids == [4242]


def test_switch_to_an_unknown_account_raises(seeded: CodexSwitcher):
    with pytest.raises(ClaudeSwitchError):
        seeded.switch_to("99")


def test_switch_to_an_account_without_credentials_raises(seeded: CodexSwitcher):
    CodexStore().delete_snapshot(KEY_B)
    with pytest.raises(ClaudeSwitchError, match="no stored credentials"):
        seeded.switch_to("2")


def test_switch_rolls_back_when_writing_the_live_file_fails(
    seeded: CodexSwitcher, monkeypatch, codex_home: Path
):
    before = (codex_home / "auth.json").read_text()
    calls: list[int] = []

    def flaky(payload):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("disk full")
        # the rollback write is allowed through
        (codex_home / "auth.json").write_text(json.dumps(payload))
        return codex_home / "auth.json"

    monkeypatch.setattr("claude_swap.codex.switcher.write_live_auth", flaky)

    with pytest.raises(ClaudeSwitchError):
        seeded.switch_to("2")

    assert json.loads((codex_home / "auth.json").read_text()) == json.loads(before)
    assert CodexStore().active_key() == KEY_A


def test_switch_is_serialized_by_the_store_lock(seeded: CodexSwitcher, monkeypatch):
    """A TUI refresh and a CLI switch are concurrent by construction; an
    interleaved capture/write puts one account's tokens in another's slot."""
    held = _held_lock(monkeypatch, seeded)
    try:
        with pytest.raises(ClaudeSwitchError, match="Another cswap process"):
            seeded.switch_to("2")
    finally:
        held.release()


# ---- rule 2: never refresh the active account from its snapshot ---------


def test_the_active_account_is_never_refreshed_from_its_snapshot(
    seeded: CodexSwitcher, monkeypatch, no_usage
):
    """Refreshing the active account's stored copy races the codex CLI holding
    the same refresh token. Whoever loses the rotation is logged out."""
    refreshed: list[str] = []

    def fake_refresh(payload, **kw):
        refreshed.append(payload["tokens"]["account_id"])
        return RefreshOutcome(payload=payload)

    monkeypatch.setattr("claude_swap.codex.switcher.try_refresh", fake_refresh)
    monkeypatch.setattr("claude_swap.codex.switcher.needs_refresh", lambda p, **k: True)

    seeded.accounts_snapshot(fetch={"1", "2"})

    assert ACCT_A not in refreshed  # active: read live, never refreshed
    assert ACCT_B in refreshed


def test_a_fresh_token_is_not_refreshed_at_all(seeded: CodexSwitcher, monkeypatch, no_usage):
    refreshed: list[str] = []
    monkeypatch.setattr(
        "claude_swap.codex.switcher.try_refresh",
        lambda p, **k: refreshed.append(1) or RefreshOutcome(payload=p),
    )
    monkeypatch.setattr("claude_swap.codex.switcher.needs_refresh", lambda p, **k: False)

    seeded.accounts_snapshot(fetch={"2"})

    assert refreshed == []


def test_a_rotated_refresh_token_is_persisted_immediately(
    seeded: CodexSwitcher, monkeypatch, no_usage
):
    """A rotated token that is never written down is an account lost."""

    def fake_refresh(payload, **kw):
        out = json.loads(json.dumps(payload))
        out["tokens"]["refresh_token"] = "rt-rotated"
        return RefreshOutcome(payload=out)

    monkeypatch.setattr("claude_swap.codex.switcher.try_refresh", fake_refresh)
    monkeypatch.setattr("claude_swap.codex.switcher.needs_refresh", lambda p, **k: True)

    seeded.accounts_snapshot(fetch={"2"})

    assert CodexStore().read_snapshot(KEY_B)["tokens"]["refresh_token"] == "rt-rotated"


def test_a_busy_store_skips_the_refresh_rather_than_losing_the_token(
    seeded: CodexSwitcher, monkeypatch, no_usage
):
    """A refresh that cannot be persisted may have already rotated the token
    server-side. Refusing to refresh costs a stale row; refreshing without
    persisting can cost the account."""
    refreshed: list[int] = []
    monkeypatch.setattr("claude_swap.codex.switcher.needs_refresh", lambda p, **k: True)
    monkeypatch.setattr(
        "claude_swap.codex.switcher.try_refresh",
        lambda p, **k: refreshed.append(1) or RefreshOutcome(payload=p),
    )

    held = _held_lock(monkeypatch, seeded)
    try:
        seeded.accounts_snapshot(fetch={"2"})
    finally:
        held.release()

    assert refreshed == []


def test_a_failed_refresh_still_serves_the_stored_payload(
    seeded: CodexSwitcher, monkeypatch, no_usage
):
    monkeypatch.setattr("claude_swap.codex.switcher.needs_refresh", lambda p, **k: True)
    monkeypatch.setattr(
        "claude_swap.codex.switcher.try_refresh",
        lambda p, **k: RefreshOutcome(kind="transient"),
    )
    snap = seeded.accounts_snapshot(fetch={"2"})
    assert snap.accounts[1].usage.sentinel is None  # served, not blanked


# ---- the read model ----------------------------------------------------


def test_snapshot_tags_every_row_with_the_codex_provider(seeded: CodexSwitcher, no_usage):
    snap = seeded.accounts_snapshot()
    assert snap.provider == "codex"
    assert {a.provider for a in snap.accounts} == {"codex"}


def test_an_empty_fetch_set_makes_no_requests(seeded: CodexSwitcher, monkeypatch):
    """`--skip-api` must genuinely skip the network, not just hide the result."""

    def boom(*a, **k):
        raise AssertionError("no request should be made")

    monkeypatch.setattr("claude_swap.codex.usage_cache.fetch_usage", boom)
    assert len(seeded.accounts_snapshot(fetch=set()).accounts) == 2


def test_fetch_none_makes_every_account_eligible(seeded: CodexSwitcher, monkeypatch):
    """The Claude side documents `fetch=None` as "every stale account is
    eligible", and SnapshotSource — hence the whole TUI — depends on it. Having
    it backwards is invisible from the CLI (which always passes an explicit set)
    and would make the dashboard silently never refresh Codex usage."""
    from claude_swap.codex.usage import UsageFetch

    calls: list[int] = []
    monkeypatch.setattr(
        "claude_swap.codex.usage_cache.fetch_usage",
        lambda *a, **k: calls.append(1) or UsageFetch(usage={"five_hour": {"pct": 1}}),
    )

    seeded.accounts_snapshot(fetch=None)

    assert len(calls) == 2


def test_a_fetch_set_restricts_which_accounts_are_fetched(
    seeded: CodexSwitcher, monkeypatch
):
    from claude_swap.codex.usage import UsageFetch

    calls: list[int] = []
    monkeypatch.setattr(
        "claude_swap.codex.usage_cache.fetch_usage",
        lambda *a, **k: calls.append(1) or UsageFetch(usage={"five_hour": {"pct": 1}}),
    )

    seeded.accounts_snapshot(fetch={"2"})

    assert len(calls) == 1


def test_a_usage_failure_becomes_a_sentinel_not_an_exception(
    seeded: CodexSwitcher, monkeypatch
):
    monkeypatch.setattr(
        "claude_swap.codex.usage_cache.fetch_usage",
        lambda *a, **k: UsageFetch(sentinel="http 401"),
    )
    snap = seeded.accounts_snapshot(fetch={"1"})
    assert snap.accounts[0].usage.sentinel == "http 401"


def test_a_slot_whose_snapshot_is_gone_reports_no_credentials(
    seeded: CodexSwitcher, no_usage
):
    CodexStore().delete_snapshot(KEY_B)
    snap = seeded.accounts_snapshot(fetch={"2"})
    assert snap.accounts[1].usage.sentinel == "no credentials"


def test_an_api_key_account_renders_a_sentinel_and_is_not_switchable(codex_home: Path):
    store = CodexStore()
    store.upsert_slot(KEY_A, email="a@x", plan="", auth_mode="apikey")
    store.write_snapshot(KEY_A, {"auth_mode": "apikey", "OPENAI_API_KEY": "sk-x", "tokens": None})
    switcher = CodexSwitcher()
    row = switcher.accounts_snapshot().accounts[0]
    assert row.usage.sentinel == "api key"
    assert row.kind == "api_key"
    assert switcher.switchable_account_numbers() == []


def test_a_disabled_account_is_not_a_rotation_candidate(seeded: CodexSwitcher):
    CodexStore().set_disabled(KEY_B, True)
    assert CodexSwitcher().switchable_account_numbers() == ["1"]
    # ...but it is still listed and still an explicit target
    assert CodexSwitcher().account_numbers() == ["1", "2"]


# ---- verbs -------------------------------------------------------------


def test_add_captures_the_current_live_login(codex_home: Path):
    (codex_home / "auth.json").write_text(
        json.dumps(make_auth_json(account_id=ACCT_A, user_id=USER_A, email="a@x"))
    )
    slot = CodexSwitcher().add_account()
    assert slot.number == "1"
    assert CodexStore().read_snapshot(KEY_A) is not None
    assert CodexStore().active_key() == KEY_A


def test_add_accepts_an_alias(codex_home: Path):
    (codex_home / "auth.json").write_text(
        json.dumps(make_auth_json(account_id=ACCT_A, user_id=USER_A))
    )
    CodexSwitcher().add_account(alias="Work")
    assert CodexStore().slots()[0].alias == "work"  # normalized


def test_add_without_a_live_login_raises(codex_home: Path):
    with pytest.raises(ClaudeSwitchError, match="No Codex login"):
        CodexSwitcher().add_account()


def test_add_refuses_an_unidentifiable_login(codex_home: Path):
    """An API-key login has no account id, so it cannot be told apart from any
    other — storing it would create a slot nothing can ever match."""
    (codex_home / "auth.json").write_text(
        json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "sk-x", "tokens": None})
    )
    with pytest.raises(ClaudeSwitchError, match="no account id"):
        CodexSwitcher().add_account()


def test_add_is_idempotent_for_the_same_account(codex_home: Path):
    (codex_home / "auth.json").write_text(
        json.dumps(make_auth_json(account_id=ACCT_A, user_id=USER_A))
    )
    CodexSwitcher().add_account()
    CodexSwitcher().add_account()
    assert len(CodexStore().slots()) == 1


def test_remove_drops_the_slot_and_its_snapshot(seeded: CodexSwitcher):
    seeded.remove_account("2", assume_yes=True)
    assert [s.account_key for s in CodexStore().slots()] == [KEY_A]
    assert CodexStore().read_snapshot(KEY_B) is None


def test_remove_asks_before_deleting(seeded: CodexSwitcher, monkeypatch):
    """Removal deletes the Keychain item too. An unprompted destructive verb
    would diverge from the Claude side's `remove` for no reason."""
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    seeded.remove_account("2")
    assert len(CodexStore().slots()) == 2
    assert CodexStore().read_snapshot(KEY_B) is not None


def test_remove_proceeds_when_the_prompt_is_answered_yes(seeded: CodexSwitcher, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    seeded.remove_account("2")
    assert [s.account_key for s in CodexStore().slots()] == [KEY_A]


def test_remove_warns_when_the_target_is_the_active_account(
    seeded: CodexSwitcher, monkeypatch, capsys
):
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    seeded.remove_account("1")
    assert "currently active" in capsys.readouterr().out


def test_remove_of_an_unknown_account_raises(seeded: CodexSwitcher):
    with pytest.raises(ClaudeSwitchError):
        seeded.remove_account("99", assume_yes=True)


def test_resolve_account_accepts_number_email_and_alias(seeded: CodexSwitcher):
    CodexStore().set_alias(KEY_B, "work")
    switcher = CodexSwitcher()
    assert switcher.resolve_account("2")[0] == "2"
    assert switcher.resolve_account("b@x")[0] == "2"
    assert switcher.resolve_account("work")[0] == "2"
    assert switcher.resolve_account("WORK")[0] == "2"


def test_resolve_account_rejects_an_empty_identifier(seeded: CodexSwitcher):
    """An empty needle must not match the empty alias every slot starts with."""
    with pytest.raises(ClaudeSwitchError):
        seeded.resolve_account("")


def test_aliases_round_trip_through_the_switcher(seeded: CodexSwitcher):
    seeded.set_alias("2", "work")
    assert CodexStore().slots()[1].alias == "work"
    seeded.unset_alias("2")
    assert CodexStore().slots()[1].alias == ""


def test_disable_and_enable_round_trip(seeded: CodexSwitcher):
    seeded.set_account_disabled("2", True)
    assert CodexStore().slots()[1].disabled is True
    seeded.set_account_disabled("2", False)
    assert CodexStore().slots()[1].disabled is False


# ---- usage caching (stage 2) -------------------------------------------


def test_two_consecutive_snapshots_cost_one_round_of_requests(
    seeded: CodexSwitcher, monkeypatch
):
    """The reason the usage cache exists. If someone later 'simplifies' it
    away, this is the test that fails."""
    from claude_swap.codex.usage import UsageFetch

    calls: list[int] = []

    def counted(*a, **k):
        calls.append(1)
        return UsageFetch(usage={"five_hour": {"pct": 5}})

    monkeypatch.setattr("claude_swap.codex.usage_cache.fetch_usage", counted)

    seeded.accounts_snapshot(fetch={"1", "2"})
    first = len(calls)
    seeded.accounts_snapshot(fetch={"1", "2"})

    assert first == 2
    assert len(calls) == first  # the second pass added nothing


def test_a_cached_value_is_served_to_a_later_snapshot_without_fetching(
    seeded: CodexSwitcher, monkeypatch
):
    from claude_swap.codex.usage import UsageFetch

    monkeypatch.setattr(
        "claude_swap.codex.usage_cache.fetch_usage",
        lambda *a, **k: UsageFetch(usage={"five_hour": {"pct": 7}}),
    )
    seeded.accounts_snapshot(fetch={"1", "2"})

    # no fetch set at all: pure cache read
    snap = seeded.accounts_snapshot()

    assert snap.accounts[0].usage.last_good == {"five_hour": {"pct": 7}}
