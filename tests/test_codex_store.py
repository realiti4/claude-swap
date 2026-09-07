"""The Codex slot registry and snapshot store."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from claude_swap.codex import paths as cpaths
from claude_swap.codex import store as store_mod
from claude_swap.codex.auth_file import account_key, file_key
from claude_swap.codex.store import CodexStore
from tests.conftest_codex import make_auth_json

KEY_A = account_key("user-a", "acct-a")
KEY_B = account_key("user-b", "acct-b")
KEY_C = account_key("user-c", "acct-c")


@pytest.fixture
def store(temp_home: Path) -> CodexStore:
    return CodexStore()


@pytest.fixture
def file_store(temp_home: Path, monkeypatch) -> CodexStore:
    """A store forced onto the on-disk path, so the file layout is testable
    on a macOS dev machine as well as on Linux/Windows CI."""
    monkeypatch.setattr(CodexStore, "_use_keychain", lambda self: False)
    return CodexStore()


def test_a_fresh_store_has_no_slots(store: CodexStore):
    assert store.slots() == []
    assert store.active_number() is None


def test_adding_a_slot_assigns_the_first_free_number(store: CodexStore):
    slot = store.upsert_slot(KEY_A, email="a@example.com", plan="pro")
    assert slot.number == "1"
    assert store.slots()[0].account_key == KEY_A


def test_a_second_slot_gets_the_next_number(store: CodexStore):
    store.upsert_slot(KEY_A, email="a@example.com", plan="pro")
    assert store.upsert_slot(KEY_B, email="b@example.com", plan="plus").number == "2"


def test_upserting_the_same_key_updates_rather_than_duplicating(store: CodexStore):
    store.upsert_slot(KEY_A, email="a@example.com", plan="pro")
    again = store.upsert_slot(KEY_A, email="renamed@example.com", plan="business")
    assert again.number == "1"
    assert len(store.slots()) == 1
    assert store.slots()[0].email == "renamed@example.com"
    assert store.slots()[0].plan == "business"


def test_slots_survive_a_reload(store: CodexStore):
    store.upsert_slot(KEY_A, email="a@example.com", plan="pro")
    assert CodexStore().slots()[0].account_key == KEY_A


def test_removing_a_slot_leaves_the_others_numbered_as_they_were(store: CodexStore):
    """Renumbering on delete would silently repoint every alias, mapping and
    muscle-memorised number the user has. Slots keep their numbers; the gap stays."""
    store.upsert_slot(KEY_A, email="a@example.com", plan="pro")
    store.upsert_slot(KEY_B, email="b@example.com", plan="plus")
    store.remove_slot(KEY_A)
    assert [s.number for s in store.slots()] == ["2"]


def test_a_freed_number_is_reused_by_the_next_add(store: CodexStore):
    store.upsert_slot(KEY_A, email="a@example.com", plan="pro")
    store.upsert_slot(KEY_B, email="b@example.com", plan="plus")
    store.remove_slot(KEY_A)
    assert store.upsert_slot(KEY_C, email="c@example.com", plan="pro").number == "1"


def test_removing_an_unknown_key_reports_that_nothing_happened(store: CodexStore):
    assert store.remove_slot("nope") is False


def test_active_number_is_recorded_and_read_back(store: CodexStore):
    store.upsert_slot(KEY_A, email="a@example.com", plan="pro")
    store.set_active(KEY_A)
    assert store.active_number() == "1"


def test_removing_the_active_slot_clears_the_active_marker(store: CodexStore):
    store.upsert_slot(KEY_A, email="a@example.com", plan="pro")
    store.set_active(KEY_A)
    store.remove_slot(KEY_A)
    assert CodexStore().active_key() is None


def test_alias_round_trips(store: CodexStore):
    store.upsert_slot(KEY_A, email="a@example.com", plan="pro")
    store.set_alias(KEY_A, "work")
    assert CodexStore().slots()[0].alias == "work"
    store.set_alias(KEY_A, "")
    assert CodexStore().slots()[0].alias == ""


def test_disabled_flag_round_trips(store: CodexStore):
    store.upsert_slot(KEY_A, email="a@example.com", plan="pro")
    store.set_disabled(KEY_A, True)
    assert CodexStore().slots()[0].disabled is True


def test_workspace_name_round_trips(store: CodexStore):
    store.upsert_slot(KEY_A, email="a@example.com", plan="business")
    store.set_workspace_name(KEY_A, "Workspace Alpha")
    assert CodexStore().slots()[0].display_label == "a@example.com [Workspace Alpha]"


def test_a_slot_without_a_workspace_displays_as_personal(store: CodexStore):
    store.upsert_slot(KEY_A, email="a@example.com", plan="pro")
    assert store.slots()[0].display_label == "a@example.com [personal]"


def test_snapshot_round_trips_through_the_credential_store(store: CodexStore):
    payload = make_auth_json(email="a@example.com")
    store.write_snapshot(KEY_A, payload)
    assert store.read_snapshot(KEY_A) == payload


def test_snapshot_is_keyed_by_account_key_not_slot_number(file_store: CodexStore):
    """Keying by slot would turn a future `codex swap`/`codex move` into a
    data migration. The key is the identity, and it never renumbers."""
    file_store.upsert_slot(KEY_A, email="a@example.com", plan="pro")
    file_store.write_snapshot(KEY_A, make_auth_json())
    assert (cpaths.get_codex_credentials_dir() / f"{file_key(KEY_A)}.json").exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="Keychain is macOS-only")
def test_macos_snapshots_go_to_the_keychain(store: CodexStore, block_real_keychain):
    store.write_snapshot(KEY_A, make_auth_json())
    assert (store_mod.KEYCHAIN_SERVICE, file_key(KEY_A)) in block_real_keychain.data
    assert list(cpaths.get_codex_credentials_dir().glob("*.json")) == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes only")
def test_on_disk_snapshots_are_private(file_store: CodexStore):
    """Directory 0700, files 0600 — codex-auth's documented posture."""
    file_store.write_snapshot(KEY_A, make_auth_json())
    d = cpaths.get_codex_credentials_dir()
    assert (os.stat(d).st_mode & 0o777) == 0o700
    f = d / f"{file_key(KEY_A)}.json"
    assert (os.stat(f).st_mode & 0o777) == 0o600


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes only")
def test_the_sequence_file_and_its_directory_are_private(store: CodexStore):
    store.upsert_slot(KEY_A, email="a@example.com", plan="pro")
    assert (os.stat(cpaths.get_codex_sequence_path()).st_mode & 0o777) == 0o600
    assert (os.stat(cpaths.get_codex_store_root()).st_mode & 0o777) == 0o700


def test_deleting_a_slot_deletes_its_snapshot(store: CodexStore):
    store.upsert_slot(KEY_A, email="a@example.com", plan="pro")
    store.write_snapshot(KEY_A, make_auth_json())
    store.remove_slot(KEY_A)
    assert store.read_snapshot(KEY_A) is None


def test_reading_a_missing_snapshot_returns_none(store: CodexStore):
    assert store.read_snapshot("nope") is None


def test_a_corrupt_snapshot_reads_as_missing_not_as_a_crash(file_store: CodexStore):
    cpaths.get_codex_credentials_dir().mkdir(parents=True, exist_ok=True)
    (cpaths.get_codex_credentials_dir() / f"{file_key(KEY_A)}.json").write_text("{ torn")
    assert file_store.read_snapshot(KEY_A) is None


def test_sequence_file_is_valid_json_on_disk(store: CodexStore):
    store.upsert_slot(KEY_A, email="a@example.com", plan="pro")
    data = json.loads(cpaths.get_codex_sequence_path().read_text())
    assert data["accounts"]["1"]["account_key"] == KEY_A


def test_a_corrupt_sequence_file_does_not_crash_the_store(store: CodexStore):
    """A torn write must degrade to 'no accounts', never to a traceback that
    makes every cswap command unusable."""
    cpaths.get_codex_sequence_path().parent.mkdir(parents=True, exist_ok=True)
    cpaths.get_codex_sequence_path().write_text("{ not json")
    assert CodexStore().slots() == []


def test_a_sequence_file_holding_a_json_array_degrades_safely(store: CodexStore):
    cpaths.get_codex_sequence_path().parent.mkdir(parents=True, exist_ok=True)
    cpaths.get_codex_sequence_path().write_text("[1, 2, 3]")
    assert CodexStore().slots() == []


def test_writing_leaves_no_temp_file_behind(store: CodexStore):
    store.upsert_slot(KEY_A, email="a@example.com", plan="pro")
    assert list(cpaths.get_codex_store_root().glob("*.tmp")) == []
