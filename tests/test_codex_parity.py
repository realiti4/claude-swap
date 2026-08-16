"""Parity with the Claude side: status, rotate, swap/move, export/import, purge.

The standard these tests hold the code to is not "Codex has a status command"
but "Codex's status looks like Claude's status". A user should not have to learn
a second output format for the same information.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_swap.codex.auth_file import account_key
from claude_swap.codex.store import CodexStore
from claude_swap.codex.switcher import CodexSwitcher
from claude_swap.codex.transfer import (
    export_codex_accounts,
    import_codex_accounts,
    purge_codex_data,
)
from claude_swap.exceptions import ClaudeSwitchError
from tests.conftest_codex import make_auth_json

ACCT_A, USER_A = "acct-a", "user-a"
ACCT_B, USER_B = "acct-b", "user-b"
ACCT_C, USER_C = "acct-c", "user-c"
KEY_A = account_key(USER_A, ACCT_A)
KEY_B = account_key(USER_B, ACCT_B)
KEY_C = account_key(USER_C, ACCT_C)

USAGE = {"five_hour": {"pct": 20}, "seven_day": {"pct": 40}}


@pytest.fixture
def seeded(codex_home: Path) -> CodexSwitcher:
    """Three accounts, A live."""
    store = CodexStore()
    for key, acct, user, email in (
        (KEY_A, ACCT_A, USER_A, "a@x"),
        (KEY_B, ACCT_B, USER_B, "b@x"),
        (KEY_C, ACCT_C, USER_C, "c@x"),
    ):
        store.upsert_slot(key, email=email, plan="pro")
        store.write_snapshot(key, make_auth_json(account_id=acct, user_id=user, email=email))
    store.set_active(KEY_A)
    (codex_home / "auth.json").write_text(
        json.dumps(make_auth_json(account_id=ACCT_A, user_id=USER_A, email="a@x"))
    )
    return CodexSwitcher()


@pytest.fixture
def usage(monkeypatch):
    """Give every account a measurement, without touching the network."""
    from claude_swap.codex.usage import UsageFetch

    monkeypatch.setattr(
        "claude_swap.codex.usage_cache.fetch_usage",
        lambda *a, **k: UsageFetch(usage=USAGE),
    )


# ---- status ------------------------------------------------------------


def test_status_renders_through_the_same_helper_as_claude(
    seeded: CodexSwitcher, usage, capsys
):
    """Not "it prints something" — it prints the tree-drawn usage block the
    Claude side prints, because it calls the same renderer."""
    seeded.status()
    out = capsys.readouterr().out
    assert "Status:" in out
    assert "Total managed Codex accounts: 3" in out
    assert "5h:" in out and "7d:" in out
    assert "├" in out and "└" in out  # the shared tree glyphs


def test_status_names_the_active_account(seeded: CodexSwitcher, usage, capsys):
    seeded.status()
    out = capsys.readouterr().out
    assert "Codex-1" in out and "a@x" in out


def test_status_without_an_active_account_says_so(codex_home: Path, capsys):
    CodexSwitcher().status()
    assert "No active Codex account" in capsys.readouterr().out


def test_status_json_matches_the_claude_envelope(seeded: CodexSwitcher, usage):
    """Same schemaVersion, same active/usage/usageStatus shape — a script that
    reads one can read the other."""
    payload = seeded.status(json_output=True)
    assert payload["schemaVersion"] == 1
    assert payload["provider"] == "codex"
    active = payload["active"]
    assert active["number"] == 1
    assert active["email"] == "a@x"
    assert active["managed"] is True
    assert active["usageStatus"] == "ok"
    assert active["usage"]["fiveHour"]["pct"] == 20
    assert active["usage"]["sevenDay"]["pct"] == 40


def test_status_json_without_an_account_mirrors_claudes_null(codex_home: Path):
    payload = CodexSwitcher().status(json_output=True)
    assert payload == {"schemaVersion": 1, "provider": "codex", "active": None}


# ---- rotate / best -----------------------------------------------------


def test_bare_switch_rotates_to_the_next_account(seeded: CodexSwitcher):
    """`cswap switch` rotates on the Claude side; the Codex one must not error
    on a missing argument."""
    assert seeded.rotate().number == "2"


def test_rotation_wraps_around(seeded: CodexSwitcher, codex_home: Path):
    (codex_home / "auth.json").write_text(
        json.dumps(make_auth_json(account_id=ACCT_C, user_id=USER_C))
    )
    assert CodexSwitcher().rotate().number == "1"


def test_rotation_skips_disabled_accounts(seeded: CodexSwitcher):
    CodexStore().set_disabled(KEY_B, True)
    assert CodexSwitcher().rotate().number == "3"


def test_rotation_with_a_single_account_refuses_rather_than_no_ops(codex_home: Path):
    store = CodexStore()
    store.upsert_slot(KEY_A, email="a@x", plan="pro")
    store.write_snapshot(KEY_A, make_auth_json(account_id=ACCT_A, user_id=USER_A))
    (codex_home / "auth.json").write_text(
        json.dumps(make_auth_json(account_id=ACCT_A, user_id=USER_A))
    )
    with pytest.raises(ClaudeSwitchError, match="Only one"):
        CodexSwitcher().rotate()


def test_strategy_best_picks_the_account_with_the_most_headroom(
    seeded: CodexSwitcher, monkeypatch
):
    from claude_swap.codex.usage import UsageFetch

    pcts = {ACCT_B: 80, ACCT_C: 5}

    def fake(access_token, account_id, timeout_s=10.0):
        return UsageFetch(usage={"five_hour": {"pct": pcts.get(account_id, 50)}})

    monkeypatch.setattr("claude_swap.codex.usage_cache.fetch_usage", fake)
    assert seeded.switch_best().number == "3"


def test_strategy_best_without_measurements_refuses(seeded: CodexSwitcher, monkeypatch):
    """Switching on unknown data would move the user for no established reason."""
    from claude_swap.codex.usage import UsageFetch

    monkeypatch.setattr(
        "claude_swap.codex.usage_cache.fetch_usage",
        lambda *a, **k: UsageFetch(sentinel="http 500"),
    )
    with pytest.raises(ClaudeSwitchError, match="known measurement"):
        seeded.switch_best()


# ---- swap / move -------------------------------------------------------


def test_swap_exchanges_two_slot_numbers(seeded: CodexSwitcher):
    seeded.swap_accounts("1", "2")
    numbers = {s.account_key: s.number for s in CodexStore().slots()}
    assert numbers[KEY_A] == "2" and numbers[KEY_B] == "1"


def test_swap_keeps_every_credential_readable(seeded: CodexSwitcher):
    """Snapshots are keyed by account_key precisely so renumbering never has to
    move a secret. If that ever changes, this is what breaks."""
    seeded.swap_accounts("1", "2")
    store = CodexStore()
    assert store.read_snapshot(KEY_A)["tokens"]["account_id"] == ACCT_A
    assert store.read_snapshot(KEY_B)["tokens"]["account_id"] == ACCT_B


def test_swap_with_itself_is_refused(seeded: CodexSwitcher):
    with pytest.raises(ClaudeSwitchError, match="itself"):
        seeded.swap_accounts("1", "1")


def test_move_to_a_free_slot_does_not_swap(seeded: CodexSwitcher):
    src, dst, swapped = seeded.move_account("1", "9")
    assert (src, dst, swapped) == ("1", "9", False)
    assert CodexStore().slot_for_key(KEY_A).number == "9"


def test_move_to_a_taken_slot_swaps_the_occupant(seeded: CodexSwitcher):
    _src, _dst, swapped = seeded.move_account("1", "2")
    assert swapped is True
    numbers = {s.account_key: s.number for s in CodexStore().slots()}
    assert numbers[KEY_A] == "2" and numbers[KEY_B] == "1"


def test_move_to_its_current_slot_is_a_no_op(seeded: CodexSwitcher):
    assert seeded.move_account("1", "1") == ("1", "1", False)


def test_move_to_an_invalid_slot_is_refused(seeded: CodexSwitcher):
    with pytest.raises(ClaudeSwitchError, match="not a valid slot"):
        seeded.move_account("1", "abc")


# ---- export / import ---------------------------------------------------


def test_export_writes_every_account_with_its_credentials(
    seeded: CodexSwitcher, tmp_path: Path
):
    target = tmp_path / "codex.json"
    assert export_codex_accounts(seeded, str(target)) == 3
    doc = json.loads(target.read_text())
    assert doc["provider"] == "codex"
    assert len(doc["accounts"]) == 3
    assert doc["accounts"][0]["auth"]["tokens"]["refresh_token"]


def test_the_export_warns_that_it_holds_live_tokens(
    seeded: CodexSwitcher, tmp_path: Path
):
    """An export you cannot log in with is not a backup — so it carries real
    tokens, and the file has to say so."""
    target = tmp_path / "codex.json"
    export_codex_accounts(seeded, str(target))
    assert "live OAuth tokens" in json.loads(target.read_text())["warning"]


@pytest.mark.skipif(
    __import__("sys").platform == "win32", reason="POSIX modes only"
)
def test_the_export_file_is_created_private(seeded: CodexSwitcher, tmp_path: Path):
    """Created 0600, not chmod'ed after: between write and chmod the tokens
    would be world-readable."""
    import os

    target = tmp_path / "codex.json"
    export_codex_accounts(seeded, str(target))
    assert (os.stat(target).st_mode & 0o777) == 0o600


def test_export_can_be_limited_to_one_account(seeded: CodexSwitcher, tmp_path: Path):
    target = tmp_path / "one.json"
    assert export_codex_accounts(seeded, str(target), account="2") == 1
    assert json.loads(target.read_text())["accounts"][0]["email"] == "b@x"


def test_export_to_stdout(seeded: CodexSwitcher, capsys):
    export_codex_accounts(seeded, "-")
    assert json.loads(capsys.readouterr().out)["provider"] == "codex"


def test_import_restores_accounts_into_an_empty_store(
    seeded: CodexSwitcher, tmp_path: Path, codex_home: Path
):
    target = tmp_path / "codex.json"
    export_codex_accounts(seeded, str(target))
    purge_codex_data(assume_yes=True)
    assert CodexStore().slots() == []

    assert import_codex_accounts(CodexSwitcher(), str(target)) == 3
    store = CodexStore()
    assert len(store.slots()) == 3
    assert store.read_snapshot(KEY_A)["tokens"]["account_id"] == ACCT_A


def test_import_carries_aliases_and_disabled_flags(
    seeded: CodexSwitcher, tmp_path: Path
):
    store = CodexStore()
    store.set_alias(KEY_B, "work")
    store.set_disabled(KEY_C, True)
    target = tmp_path / "codex.json"
    export_codex_accounts(CodexSwitcher(), str(target))
    purge_codex_data(assume_yes=True)

    import_codex_accounts(CodexSwitcher(), str(target))

    restored = {s.account_key: s for s in CodexStore().slots()}
    assert restored[KEY_B].alias == "work"
    assert restored[KEY_C].disabled is True


def test_import_skips_existing_accounts_unless_forced(
    seeded: CodexSwitcher, tmp_path: Path
):
    target = tmp_path / "codex.json"
    export_codex_accounts(seeded, str(target))
    assert import_codex_accounts(CodexSwitcher(), str(target)) == 0
    assert import_codex_accounts(CodexSwitcher(), str(target), force=True) == 3


def test_import_refuses_a_claude_export(tmp_path: Path, codex_home: Path):
    """Refusing beats half-applying: a Claude export's rows describe entirely
    different fields."""
    target = tmp_path / "claude.json"
    target.write_text(json.dumps({"provider": "claude", "accounts": [{}]}))
    with pytest.raises(ClaudeSwitchError, match="not a Codex one"):
        import_codex_accounts(CodexSwitcher(), str(target))


def test_import_refuses_a_newer_export_version(tmp_path: Path, codex_home: Path):
    target = tmp_path / "future.json"
    target.write_text(
        json.dumps({"provider": "codex", "version": 99, "accounts": [{}]})
    )
    with pytest.raises(ClaudeSwitchError, match="newer than"):
        import_codex_accounts(CodexSwitcher(), str(target))


def test_import_of_junk_is_a_clean_error(tmp_path: Path, codex_home: Path):
    target = tmp_path / "junk.json"
    target.write_text("{ not json")
    with pytest.raises(ClaudeSwitchError, match="not valid JSON"):
        import_codex_accounts(CodexSwitcher(), str(target))


def test_import_of_a_missing_file_is_a_clean_error(tmp_path: Path, codex_home: Path):
    with pytest.raises(ClaudeSwitchError, match="Cannot read"):
        import_codex_accounts(CodexSwitcher(), str(tmp_path / "nope.json"))


# ---- purge -------------------------------------------------------------


def test_purge_removes_slots_and_snapshots(seeded: CodexSwitcher, capsys):
    assert purge_codex_data(assume_yes=True) is True
    store = CodexStore()
    assert store.slots() == []
    assert store.read_snapshot(KEY_A) is None


def test_purge_leaves_the_live_codex_login_alone(seeded: CodexSwitcher, codex_home: Path):
    """cswap manages copies; the user's actual ~/.codex login is not ours."""
    before = (codex_home / "auth.json").read_text()
    purge_codex_data(assume_yes=True)
    assert (codex_home / "auth.json").read_text() == before


def test_purge_asks_first(seeded: CodexSwitcher, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    assert purge_codex_data() is False
    assert len(CodexStore().slots()) == 3


def test_purge_on_an_empty_store_says_so(codex_home: Path, capsys):
    assert purge_codex_data(assume_yes=True) is False
    assert "No cswap Codex data" in capsys.readouterr().out
