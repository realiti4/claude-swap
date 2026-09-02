"""Workspace-name refresh — mostly a test of when we DON'T make a request."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_swap.codex.auth_file import account_key
from claude_swap.codex.store import CodexStore
from claude_swap.codex.workspaces import refresh_workspace_names, scope_needs_refresh
from tests.conftest_codex import make_auth_json

USER = "user-a"
KEY_1 = account_key(USER, "team-1")
KEY_2 = account_key(USER, "team-2")
KEY_PERSONAL = account_key(USER, "personal-1")
KEY_OTHER_USER = account_key("user-b", "team-9")


@pytest.fixture
def store(codex_home: Path) -> CodexStore:
    return CodexStore()


@pytest.fixture
def calls(monkeypatch):
    """Record every /backend-api/accounts request and control the answer."""
    made: list[tuple[str, str]] = []
    reply = {"value": {}}

    def fake(token, account_id, timeout_s=10.0):
        made.append((token, account_id))
        return reply["value"]

    monkeypatch.setattr("claude_swap.codex.workspaces.fetch_workspace_names", fake)
    return made, reply


def _payload_for(slot):
    return CodexStore().read_snapshot(slot.account_key)


def _seed(store: CodexStore, key: str, *, plan: str, name: str = "") -> None:
    store.upsert_slot(key, email="a@x", plan=plan, workspace_name=name)
    store.write_snapshot(key, make_auth_json(account_id=key.rpartition("::")[2]))


# ---- when NOT to ask ---------------------------------------------------


def test_a_lone_account_never_triggers_a_request(store, calls):
    """A single personal account is the common case; it must cost nothing."""
    made, _ = calls
    _seed(store, KEY_PERSONAL, plan="plus")
    refresh_workspace_names(store, payload_for=_payload_for)
    assert made == []


def test_a_scope_with_no_workspace_plan_never_asks(store, calls):
    made, _ = calls
    _seed(store, KEY_PERSONAL, plan="plus")
    _seed(store, account_key(USER, "personal-2"), plan="pro")
    refresh_workspace_names(store, payload_for=_payload_for)
    assert made == []


def test_a_scope_whose_names_are_all_known_never_asks(store, calls):
    made, _ = calls
    _seed(store, KEY_1, plan="business", name="Alpha")
    _seed(store, KEY_2, plan="business", name="Beta")
    refresh_workspace_names(store, payload_for=_payload_for)
    assert made == []


def test_an_api_key_account_is_not_part_of_any_scope(store, calls):
    made, _ = calls
    store.upsert_slot(KEY_1, email="a@x", plan="business", auth_mode="apikey")
    store.upsert_slot(KEY_2, email="a@x", plan="business", auth_mode="apikey")
    refresh_workspace_names(store, payload_for=_payload_for)
    assert made == []


def test_scope_predicate_matches_the_documented_rules():
    from claude_swap.codex.store import CodexSlot

    def slot(key, plan, name=""):
        return CodexSlot(number="1", account_key=key, plan=plan, workspace_name=name)

    assert scope_needs_refresh([slot(KEY_1, "business")]) is False  # one record
    assert scope_needs_refresh([slot(KEY_1, "plus"), slot(KEY_2, "pro")]) is False
    assert scope_needs_refresh([slot(KEY_1, "business", "A"), slot(KEY_2, "business", "B")]) is False
    assert scope_needs_refresh([slot(KEY_1, "business"), slot(KEY_2, "business", "B")]) is True
    # a personal record can still ride along in a scope that needs a refresh
    assert scope_needs_refresh([slot(KEY_PERSONAL, "plus"), slot(KEY_1, "business")]) is True


# ---- when to ask, and what to do with the answer -----------------------


def test_one_request_per_scope_fills_every_workspace_in_it(store, calls):
    made, reply = calls
    _seed(store, KEY_1, plan="business")
    _seed(store, KEY_2, plan="business")
    reply["value"] = {"team-1": "Workspace Alpha", "team-2": "Workspace Beta"}

    changed = refresh_workspace_names(store, payload_for=_payload_for)

    assert len(made) == 1  # one request answered for both
    assert changed == 2
    names = {s.account_key: s.workspace_name for s in CodexStore().slots()}
    assert names[KEY_1] == "Workspace Alpha"
    assert names[KEY_2] == "Workspace Beta"


def test_a_returned_name_overwrites_an_older_one(store, calls):
    """The server is authoritative; a renamed workspace should follow."""
    made, reply = calls
    _seed(store, KEY_1, plan="business")
    _seed(store, KEY_2, plan="business", name="Old Name")
    reply["value"] = {"team-1": "Alpha", "team-2": "Sandbox"}

    refresh_workspace_names(store, payload_for=_payload_for)

    names = {s.account_key: s.workspace_name for s in CodexStore().slots()}
    assert names[KEY_2] == "Sandbox"


def test_an_in_scope_workspace_not_returned_is_cleared(store, calls):
    """Leaving a stale name for a workspace the user has lost access to is
    worse than showing none."""
    made, reply = calls
    _seed(store, KEY_1, plan="business")
    _seed(store, KEY_2, plan="business", name="Gone")
    reply["value"] = {"team-1": "Alpha"}

    refresh_workspace_names(store, payload_for=_payload_for)

    names = {s.account_key: s.workspace_name for s in CodexStore().slots()}
    assert names[KEY_2] == ""


def test_a_personal_record_in_scope_is_never_cleared(store, calls):
    """It never had a workspace name to lose."""
    made, reply = calls
    _seed(store, KEY_PERSONAL, plan="plus")
    _seed(store, KEY_1, plan="business")
    reply["value"] = {"team-1": "Alpha"}

    refresh_workspace_names(store, payload_for=_payload_for)

    names = {s.account_key: s.workspace_name for s in CodexStore().slots()}
    assert names[KEY_PERSONAL] == ""


def test_a_failed_request_leaves_every_stored_name_untouched(store, calls):
    made, reply = calls
    _seed(store, KEY_1, plan="business", name="Alpha")
    _seed(store, KEY_2, plan="business")
    reply["value"] = {}  # what fetch_workspace_names returns on any failure

    changed = refresh_workspace_names(store, payload_for=_payload_for)

    assert changed == 0
    assert CodexStore().slot_for_key(KEY_1).workspace_name == "Alpha"


def test_scopes_are_independent(store, calls):
    """A second user's accounts must not ride on the first user's request."""
    made, reply = calls
    _seed(store, KEY_1, plan="business")
    _seed(store, KEY_2, plan="business")
    _seed(store, KEY_OTHER_USER, plan="business")
    reply["value"] = {"team-1": "Alpha", "team-2": "Beta"}

    refresh_workspace_names(store, payload_for=_payload_for)

    # user-b's scope has one record, so it never asks
    assert len(made) == 1
    assert CodexStore().slot_for_key(KEY_OTHER_USER).workspace_name == ""


def test_a_scope_with_no_usable_credentials_makes_no_request(store, calls):
    made, _ = calls
    store.upsert_slot(KEY_1, email="a@x", plan="business")
    store.upsert_slot(KEY_2, email="a@x", plan="business")
    # no snapshots written -> payload_for returns None for both
    refresh_workspace_names(store, payload_for=lambda s: None)
    assert made == []
