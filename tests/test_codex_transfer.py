"""Export / import / import-usage for Codex accounts.

A Codex export carries ``"provider": "codex"`` and each account's whole
auth.json; a Claude export is unchanged (no ``provider``). Each switcher
refuses the other provider's documents and names the command that takes them.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest

from claude_swap import codex_auth
from claude_swap.codex_switcher import CodexAccountSwitcher
from claude_swap.exceptions import TransferError
from claude_swap.json_output import account_row
from claude_swap.models import Platform
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.transfer import export_accounts, import_accounts, import_usage
from tests import codex_fixtures as cf

PERSONAL = dict(email="user@example.com", account_id="acct-personal", user_id="user-1", plan="plus")
TEAM = dict(email="user@example.com", account_id="acct-team", user_id="user-1", plan="team")
USER = "user@example.com"


def _switcher() -> CodexAccountSwitcher:
    s = CodexAccountSwitcher()
    s.platform = Platform.LINUX
    return s


def _set_live(codex_home: Path, **kw) -> str:
    text = cf.auth_json(**kw)
    (codex_home / "auth.json").write_text(text, encoding="utf-8")
    return text


def _add(s: CodexAccountSwitcher, codex_home: Path, **kw) -> str:
    text = _set_live(codex_home, **kw)
    s.add_account()
    return text


def _claude_switcher() -> ClaudeAccountSwitcher:
    c = ClaudeAccountSwitcher()
    c.platform = Platform.LINUX
    c._setup_directories()
    c._init_sequence_file()
    return c


def _claude_export(home: Path) -> Path:
    """A real Claude export of one account."""
    c = _claude_switcher()
    email = "claude@example.com"
    c._write_account_credentials("1", email, json.dumps(
        {"claudeAiOauth": {"accessToken": "at", "refreshToken": "rt", "expiresAt": 9999}}
    ))
    c._write_account_config("1", email, json.dumps(
        {"oauthAccount": {"emailAddress": email, "organizationUuid": ""}}
    ))
    data = c._get_sequence_data()
    data["accounts"]["1"] = {
        "email": email, "uuid": "u", "organizationUuid": "",
        "organizationName": "", "added": "2024-01-01T00:00:00Z",
    }
    data["sequence"] = [1]
    c._write_json(c.sequence_file, data)
    path = home / "claude.cswap"
    export_accounts(c, str(path))
    return path


@pytest.fixture
def exported(codex_home: Path) -> tuple[CodexAccountSwitcher, Path, dict[str, str]]:
    """Codex slots 1 personal, 2 team (live, rotated by Codex since its
    backup), 3 an API key; exported to a file."""
    s = _switcher()
    texts = {"1": _add(s, codex_home, **PERSONAL), "2": _add(s, codex_home, **TEAM)}
    s.add_account_from_token("sk-proj-k")
    texts["live"] = _set_live(codex_home, **TEAM, refresh_token="rt-rotated")
    path = codex_home.parent / "codex.cswap"
    export_accounts(s, str(path))
    return s, path, texts


class TestExport:
    def test_codex_export_is_tagged_and_carries_whole_logins(self, exported):
        _s, path, texts = exported
        env = json.loads(path.read_text(encoding="utf-8"))
        assert env["provider"] == "codex"
        rows = {a["number"]: a for a in env["accounts"]}

        assert rows[1]["credentials"] == json.loads(texts["1"])
        assert rows[1]["organizationUuid"] == "acct-personal" and "kind" not in rows[1]
        # The live account exports the live login, fresher than its backup.
        assert rows[2]["credentials"] == json.loads(texts["live"])
        assert rows[2]["config"]["oauthAccount"]["organizationUuid"] == "acct-team"
        assert rows[3]["kind"] == "api_key"
        assert json.loads(rows[3]["credentials"])["OPENAI_API_KEY"] == "sk-proj-k"

    def test_claude_export_has_no_provider(self, codex_home: Path):
        env = json.loads(_claude_export(codex_home.parent).read_text(encoding="utf-8"))
        assert "provider" not in env

    def test_empty_codex_store_names_the_codex_command(self, codex_home: Path):
        with pytest.raises(TransferError, match="run cswap codex --add-account first"):
            export_accounts(_switcher(), str(codex_home.parent / "x.cswap"))


class TestImport:
    def test_round_trip_into_an_empty_codex_store(self, exported, codex_home: Path, capsys):
        s, path, texts = exported
        shutil.rmtree(s.backup_dir)
        dst = _switcher()
        capsys.readouterr()

        import_accounts(dst, str(path))

        data = dst._get_sequence_data()
        assert sorted(data["accounts"]) == ["1", "2", "3"]
        assert data["accounts"]["3"]["kind"] == "api_key"
        assert json.loads(dst._read_account_credentials("1", USER)) == json.loads(texts["1"])
        assert json.loads(dst._read_account_credentials("2", USER)) == json.loads(texts["live"])
        err = capsys.readouterr().err
        assert "cswap codex --switch-to 2 --force" in err

        dst.switch_to("1")
        live = (codex_home / "auth.json").read_text(encoding="utf-8")
        assert codex_auth.identity(live)["organizationUuid"] == "acct-personal"
        dst.switch_to("3")
        live = (codex_home / "auth.json").read_text(encoding="utf-8")
        assert codex_auth.is_api_key_blob(live)

    def test_claude_refuses_a_codex_export(self, exported):
        _s, path, _ = exported
        claude = _claude_switcher()
        with pytest.raises(TransferError) as err:
            import_accounts(claude, str(path))
        msg = str(err.value)
        assert "Codex" in msg and "Claude Code" in msg
        assert f"cswap codex import {path}" in msg
        assert claude._get_sequence_data()["accounts"] == {}

    def test_codex_refuses_a_claude_export(self, codex_home: Path):
        path = _claude_export(codex_home.parent)
        codex = _switcher()
        with pytest.raises(TransferError) as err:
            import_accounts(codex, str(path))
        msg = str(err.value)
        assert "Codex" in msg and "Claude Code" in msg
        assert f"cswap import {path}" in msg
        assert not codex.sequence_file.exists() or codex._get_sequence_data()["accounts"] == {}

    @pytest.mark.parametrize("provider, claude_ok, codex_ok", [
        ("claude", True, False),
        ("codex", False, True),
        ("gemini", False, False),
    ])
    def test_the_provider_field_decides(
        self, exported, codex_home: Path, provider, claude_ok, codex_ok
    ):
        _s, codex_path, _ = exported
        # Each provider's field on that provider's own document.
        path = _claude_export(codex_home.parent) if provider == "claude" else codex_path
        env = json.loads(path.read_text(encoding="utf-8"))
        env["provider"] = provider
        doc = codex_home.parent / "edited.cswap"
        doc.write_text(json.dumps(env), encoding="utf-8")
        for switcher, ok in ((_claude_switcher(), claude_ok), (_switcher(), codex_ok)):
            if ok:
                import_accounts(switcher, str(doc), force=True)
            else:
                with pytest.raises(TransferError, match="gemini|holds"):
                    import_accounts(switcher, str(doc))


@pytest.mark.parametrize("provider", [["x"], {"k": "v"}, 3])
def test_a_provider_that_is_not_a_string_is_refused(
    exported, codex_home: Path, provider
):
    _s, path, _ = exported
    env = json.loads(path.read_text(encoding="utf-8"))
    env["provider"] = provider
    doc = codex_home.parent / "edited.cswap"
    doc.write_text(json.dumps(env), encoding="utf-8")
    usage = codex_home.parent / "usage.json"
    usage.write_text(json.dumps({"schemaVersion": 1, "accounts": [], "provider": provider}))
    for switcher in (_claude_switcher(), _switcher()):
        with pytest.raises(TransferError, match="provider must be a string"):
            import_accounts(switcher, str(doc))
        with pytest.raises(TransferError, match="provider must be a string"):
            import_usage(switcher, str(usage))


def _usage_document(provider: str | None) -> str:
    row = account_row(
        9, USER, "", "acct-personal", False,
        {"five_hour": {"pct": 42.0, "resets_at": "2099-01-01T00:00:00+00:00"}},
        usage_fetched_at=time.time() - 30, usage_age_s=30.0,
    )
    doc = {"schemaVersion": 1, "activeAccountNumber": None, "accounts": [row]}
    if provider:
        doc["provider"] = provider
    return json.dumps(doc)


class TestImportUsage:
    def test_codex_readings_feed_codex(self, codex_home: Path, capsys):
        s = _switcher()
        _add(s, codex_home, **PERSONAL)
        path = codex_home.parent / "usage.json"
        path.write_text(_usage_document("codex"), encoding="utf-8")
        import_usage(s, str(path))
        assert "Adopted usage for user@example.com → slot 1" in capsys.readouterr().err

    def test_claude_refuses_codex_readings(self, codex_home: Path):
        path = codex_home.parent / "usage.json"
        path.write_text(_usage_document("codex"), encoding="utf-8")
        with pytest.raises(TransferError, match=f"cswap codex import-usage {path}"):
            import_usage(_claude_switcher(), str(path))

    def test_codex_refuses_claude_readings(self, codex_home: Path):
        s = _switcher()
        _add(s, codex_home, **PERSONAL)
        path = codex_home.parent / "usage.json"
        path.write_text(_usage_document(None), encoding="utf-8")
        with pytest.raises(TransferError, match=f"cswap import-usage {path}"):
            import_usage(s, str(path))
        assert not s._usage_store.path.exists() or "42.0" not in s._usage_store.path.read_text()


def test_the_claude_refusal_does_not_import_the_codex_switcher(monkeypatch):
    """Claude's import path names the other provider from constants."""
    import sys
    from types import SimpleNamespace

    from claude_swap import transfer
    from claude_swap.exceptions import TransferError

    monkeypatch.setitem(sys.modules, "claude_swap.codex_switcher", None)  # import fails
    claude = SimpleNamespace(provider_name="claude", display_name="Claude Code", cli_prefix="cswap")
    with pytest.raises(TransferError, match="holds Codex accounts.*cswap codex import"):
        transfer._refuse_other_provider(claude, {"provider": "codex"}, "x.json", "import")
