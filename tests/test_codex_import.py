"""One-time import of codex-auth's registry.json into cswap's own store."""

from __future__ import annotations

import json
from pathlib import Path

from claude_swap.codex.auth_file import account_key, file_key
from claude_swap.codex.registry_import import ImportResult, import_codex_auth_registry
from claude_swap.codex.store import CodexStore
from tests.conftest_codex import make_auth_json

KEY_A = account_key("user-a", "acct-a")
KEY_B = account_key("user-b", "acct-b")


def _write_registry(codex_home: Path, accounts: list[dict], schema: int = 3) -> Path:
    d = codex_home / "accounts"
    d.mkdir(parents=True, exist_ok=True)
    (d / "registry.json").write_text(
        json.dumps(
            {
                "schema_version": schema,
                "active_account_key": accounts[0]["account_key"] if accounts else None,
                "accounts": accounts,
            }
        )
    )
    return d


def _seed(key: str, email: str, plan: str = "pro") -> dict:
    return {
        "account_key": key,
        "chatgpt_account_id": key.split("::")[1],
        "chatgpt_user_id": key.split("::")[0],
        "email": email,
        "alias": "",
        "account_name": None,
        "plan": plan,
        "auth_mode": "chatgpt",
        "created_at": 0,
        "last_used_at": 0,
    }


def _snapshot(d: Path, key: str, payload: dict | None = None) -> None:
    (d / f"{file_key(key)}.auth.json").write_text(
        json.dumps(payload if payload is not None else make_auth_json())
    )


def test_import_is_a_noop_when_no_registry_exists(codex_home: Path):
    assert import_codex_auth_registry() == ImportResult(imported=0, skipped=0, source=None)
    assert CodexStore().slots() == []


def test_import_creates_one_slot_per_account(codex_home: Path):
    d = _write_registry(codex_home, [_seed(KEY_A, "a@x"), _seed(KEY_B, "b@x")])
    _snapshot(d, KEY_A, make_auth_json(email="a@x"))
    _snapshot(d, KEY_B, make_auth_json(email="b@x"))

    result = import_codex_auth_registry()

    assert result.imported == 2
    slots = CodexStore().slots()
    assert [s.email for s in slots] == ["a@x", "b@x"]
    assert [s.number for s in slots] == ["1", "2"]


def test_import_copies_the_snapshots(codex_home: Path):
    d = _write_registry(codex_home, [_seed(KEY_A, "a@x")])
    payload = make_auth_json(email="a@x")
    _snapshot(d, KEY_A, payload)

    import_codex_auth_registry()

    assert CodexStore().read_snapshot(KEY_A) == payload


def test_import_renames_legacy_plans(codex_home: Path):
    d = _write_registry(codex_home, [_seed(KEY_A, "a@x", plan="team")])
    _snapshot(d, KEY_A)

    import_codex_auth_registry()

    assert CodexStore().slots()[0].plan == "business"


def test_import_carries_the_alias_across(codex_home: Path):
    row = _seed(KEY_A, "a@x")
    row["alias"] = "work"
    d = _write_registry(codex_home, [row])
    _snapshot(d, KEY_A)

    import_codex_auth_registry()

    assert CodexStore().slots()[0].alias == "work"


def test_import_carries_the_workspace_name_across(codex_home: Path):
    row = _seed(KEY_A, "a@x", plan="team")
    row["account_name"] = "Workspace Alpha"
    d = _write_registry(codex_home, [row])
    _snapshot(d, KEY_A)

    import_codex_auth_registry()

    assert CodexStore().slots()[0].workspace_name == "Workspace Alpha"


def test_import_leaves_the_source_untouched(codex_home: Path):
    d = _write_registry(codex_home, [_seed(KEY_A, "a@x")])
    _snapshot(d, KEY_A)
    snap = d / f"{file_key(KEY_A)}.auth.json"
    before = (d / "registry.json").read_text(), snap.read_text()

    import_codex_auth_registry()

    assert ((d / "registry.json").read_text(), snap.read_text()) == before


def test_an_account_without_a_snapshot_is_skipped_not_fatal(codex_home: Path):
    """A registry row whose auth file was deleted is a real state on disk; it
    must cost that one account, not the whole import."""
    d = _write_registry(codex_home, [_seed(KEY_A, "a@x"), _seed(KEY_B, "b@x")])
    _snapshot(d, KEY_A)

    result = import_codex_auth_registry()

    assert (result.imported, result.skipped) == (1, 1)
    assert [s.account_key for s in CodexStore().slots()] == [KEY_A]


def test_a_corrupt_snapshot_is_skipped_not_fatal(codex_home: Path):
    d = _write_registry(codex_home, [_seed(KEY_A, "a@x")])
    (d / f"{file_key(KEY_A)}.auth.json").write_text("{ torn")

    result = import_codex_auth_registry()

    assert (result.imported, result.skipped) == (0, 1)


def test_an_api_key_account_imports_without_tokens(codex_home: Path):
    row = _seed(KEY_A, "a@x")
    row["auth_mode"] = "apikey"
    d = _write_registry(codex_home, [row])
    _snapshot(d, KEY_A, {"auth_mode": "apikey", "OPENAI_API_KEY": "sk-x", "tokens": None})

    result = import_codex_auth_registry()

    assert result.imported == 1
    assert CodexStore().slots()[0].auth_mode == "apikey"


def test_import_reads_the_legacy_v2_email_keyed_shape(codex_home: Path):
    """v2 keyed accounts by email and had no account_key at all."""
    d = codex_home / "accounts"
    d.mkdir(parents=True, exist_ok=True)
    (d / "registry.json").write_text(
        json.dumps(
            {
                "version": 2,
                "active_email": "a@x",
                "accounts": {"a@x": {"email": "a@x", "plan": "pro"}},
            }
        )
    )
    result = import_codex_auth_registry()
    assert result.skipped == 1  # no account_key, nothing to key a snapshot by
    assert result.imported == 0


def test_a_newer_schema_is_refused_rather_than_misread(codex_home: Path):
    """Guessing at a format we do not know would corrupt the user's accounts."""
    d = codex_home / "accounts"
    d.mkdir(parents=True, exist_ok=True)
    (d / "registry.json").write_text(json.dumps({"schema_version": 99, "accounts": []}))
    result = import_codex_auth_registry()
    assert result.imported == 0
    assert result.unsupported_schema == 99


def test_schema_4_is_accepted(codex_home: Path):
    d = _write_registry(codex_home, [_seed(KEY_A, "a@x")], schema=4)
    _snapshot(d, KEY_A)
    assert import_codex_auth_registry().imported == 1


def test_import_records_the_active_account(codex_home: Path):
    d = _write_registry(codex_home, [_seed(KEY_A, "a@x")])
    _snapshot(d, KEY_A)
    import_codex_auth_registry()
    assert CodexStore().active_key() == KEY_A


def test_an_active_key_naming_a_skipped_account_is_not_recorded(codex_home: Path):
    """Pointing the active marker at a slot that failed to import would make
    every later status read resolve to nothing."""
    d = _write_registry(codex_home, [_seed(KEY_A, "a@x")])
    # no snapshot written -> the row is skipped
    import_codex_auth_registry()
    assert CodexStore().active_key() is None


def test_only_if_empty_does_not_run_twice(codex_home: Path):
    d = _write_registry(codex_home, [_seed(KEY_A, "a@x")])
    _snapshot(d, KEY_A)
    import_codex_auth_registry()
    assert import_codex_auth_registry(only_if_empty=True).imported == 0


def test_an_explicit_import_can_be_re_run(codex_home: Path):
    """`cswap codex import` is a recovery path; it must not silently no-op."""
    d = _write_registry(codex_home, [_seed(KEY_A, "a@x")])
    _snapshot(d, KEY_A)
    import_codex_auth_registry()
    assert import_codex_auth_registry().imported == 1
    assert len(CodexStore().slots()) == 1  # upsert, not duplicate
