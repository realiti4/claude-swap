"""The `cswap codex ...` command surface."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_swap.cli import main
from claude_swap.codex.auth_file import account_key, file_key
from claude_swap.codex.store import CodexStore
from tests.conftest_codex import make_auth_json

ACCT_A, USER_A = "acct-a", "user-a"
KEY_A = account_key(USER_A, ACCT_A)


def _run(monkeypatch, argv: list[str]) -> None:
    monkeypatch.setattr("sys.argv", ["cswap", *argv])
    main()


def _seed_one(email: str = "a@example.com") -> CodexStore:
    store = CodexStore()
    store.upsert_slot(KEY_A, email=email, plan="pro")
    store.write_snapshot(KEY_A, make_auth_json(account_id=ACCT_A, user_id=USER_A, email=email))
    return store


@pytest.fixture
def offline(monkeypatch):
    """No codex CLI command may reach the network in these tests."""

    def boom(*a, **k):
        raise AssertionError("no network request should be made")

    monkeypatch.setattr("claude_swap.codex.usage_cache.fetch_usage", boom)


def test_bare_cswap_verbs_are_untouched(monkeypatch, capsys, temp_home: Path):
    """The whole point of the namespace: existing muscle memory and scripts
    keep meaning Claude."""
    with pytest.raises(SystemExit):
        _run(monkeypatch, ["--version"])
    assert "cswap" in capsys.readouterr().out


def test_the_main_help_advertises_the_codex_namespace(monkeypatch, capsys, temp_home: Path):
    with pytest.raises(SystemExit):
        _run(monkeypatch, ["--help"])
    assert "codex <cmd>" in capsys.readouterr().out


def test_codex_list_on_an_empty_store_says_so(monkeypatch, capsys, codex_home: Path):
    _run(monkeypatch, ["codex", "list", "--skip-api"])
    assert "No Codex accounts" in capsys.readouterr().out


def test_codex_list_shows_managed_accounts(monkeypatch, capsys, codex_home: Path, offline):
    _seed_one()
    _run(monkeypatch, ["codex", "list", "--skip-api"])
    out = capsys.readouterr().out
    assert "a@example.com" in out and "1." in out


def test_codex_list_json_is_machine_readable(monkeypatch, capsys, codex_home: Path, offline):
    _seed_one()
    _run(monkeypatch, ["codex", "list", "--json", "--skip-api"])
    data = json.loads(capsys.readouterr().out)
    assert data["provider"] == "codex"
    assert data["accounts"][0]["email"] == "a@example.com"


def test_codex_list_marks_the_active_account(monkeypatch, capsys, codex_home: Path, offline):
    _seed_one()
    (codex_home / "auth.json").write_text(
        json.dumps(make_auth_json(account_id=ACCT_A, user_id=USER_A))
    )
    _run(monkeypatch, ["codex", "list", "--skip-api"])
    assert capsys.readouterr().out.startswith("*")


def test_codex_status_reports_the_live_account(monkeypatch, capsys, codex_home: Path):
    _seed_one()
    (codex_home / "auth.json").write_text(
        json.dumps(make_auth_json(account_id=ACCT_A, user_id=USER_A, email="a@example.com"))
    )
    _run(monkeypatch, ["codex", "status"])
    assert "a@example.com" in capsys.readouterr().out


def test_codex_status_without_a_managed_login_says_so(monkeypatch, capsys, codex_home: Path):
    _seed_one()
    _run(monkeypatch, ["codex", "status"])
    assert "No managed Codex account is active" in capsys.readouterr().out


def test_first_codex_command_imports_codex_auth_accounts(
    monkeypatch, capsys, codex_home: Path, offline
):
    """The user should not have to discover an import command to see accounts
    they already have."""
    d = codex_home / "accounts"
    d.mkdir(parents=True, exist_ok=True)
    (d / "registry.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "active_account_key": KEY_A,
                "accounts": [
                    {
                        "account_key": KEY_A,
                        "chatgpt_account_id": ACCT_A,
                        "chatgpt_user_id": USER_A,
                        "email": "a@example.com",
                        "alias": "",
                        "account_name": None,
                        "plan": "pro",
                        "auth_mode": "chatgpt",
                    }
                ],
            }
        )
    )
    (d / f"{file_key(KEY_A)}.auth.json").write_text(json.dumps(make_auth_json()))

    _run(monkeypatch, ["codex", "list", "--skip-api"])

    out = capsys.readouterr().out
    assert "Imported 1" in out
    assert "a@example.com" in out


def test_the_import_does_not_repeat_on_the_next_command(
    monkeypatch, capsys, codex_home: Path, offline
):
    _seed_one()
    _run(monkeypatch, ["codex", "list", "--skip-api"])
    assert "Imported" not in capsys.readouterr().out


def test_switch_prints_a_restart_warning_when_codex_is_running(
    monkeypatch, capsys, codex_home: Path
):
    _seed_one()
    monkeypatch.setattr("claude_swap.codex.switcher.running_codex_pids", lambda: [4242])

    _run(monkeypatch, ["codex", "switch", "1"])

    out = capsys.readouterr().out
    assert "restart" in out.lower() and "4242" in out


def test_switch_is_quiet_when_nothing_is_running(monkeypatch, capsys, codex_home: Path):
    """A warning printed on every switch is a warning users stop reading."""
    _seed_one()
    monkeypatch.setattr("claude_swap.codex.switcher.running_codex_pids", lambda: [])

    _run(monkeypatch, ["codex", "switch", "1"])

    assert "restart" not in capsys.readouterr().out.lower()


def test_switch_to_an_unknown_account_exits_nonzero(monkeypatch, capsys, codex_home: Path):
    _seed_one()
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, ["codex", "switch", "99"])
    assert exc.value.code == 1
    assert "No Codex account matches" in capsys.readouterr().err


def test_alias_can_be_set_and_cleared(monkeypatch, capsys, codex_home: Path):
    _seed_one()
    _run(monkeypatch, ["codex", "alias", "1", "work"])
    assert CodexStore().slots()[0].alias == "work"
    _run(monkeypatch, ["codex", "alias", "1", "--unset"])
    assert CodexStore().slots()[0].alias == ""


def test_an_invalid_alias_is_a_clean_error_not_a_traceback(
    monkeypatch, capsys, codex_home: Path
):
    _seed_one()
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, ["codex", "alias", "1", "has space"])
    assert exc.value.code == 1
    assert "alias" in capsys.readouterr().err.lower()


def test_disable_and_enable_round_trip(monkeypatch, codex_home: Path):
    _seed_one()
    _run(monkeypatch, ["codex", "disable", "1"])
    assert CodexStore().slots()[0].disabled is True
    _run(monkeypatch, ["codex", "enable", "1"])
    assert CodexStore().slots()[0].disabled is False


def test_remove_forgets_the_account(monkeypatch, codex_home: Path):
    _seed_one()
    _run(monkeypatch, ["codex", "remove", "1", "-y"])
    assert CodexStore().slots() == []


def test_codex_help_lists_the_verbs(monkeypatch, capsys):
    with pytest.raises(SystemExit):
        _run(monkeypatch, ["codex", "--help"])
    out = capsys.readouterr().out
    for verb in ("list", "switch", "add", "login", "remove", "alias", "import"):
        assert verb in out


def test_an_unknown_codex_verb_exits_nonzero(monkeypatch):
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, ["codex", "frobnicate"])
    assert exc.value.code != 0


def test_codex_login_resolves_the_real_binary_not_a_shell_function(
    monkeypatch, capsys, codex_home: Path
):
    """A shell function named `codex` is a common setup — one injecting
    --dangerously-bypass-approvals-and-sandbox is in the wild. Running login
    through a shell would inherit flags cswap never chose."""
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["shell"] = kwargs.get("shell", False)

        class R:
            returncode = 0

        return R()

    monkeypatch.setattr("claude_swap.cli_codex.shutil.which", lambda n: "/usr/local/bin/codex")
    monkeypatch.setattr("claude_swap.cli_codex.subprocess.run", fake_run)
    (codex_home / "auth.json").write_text(
        json.dumps(make_auth_json(account_id=ACCT_A, user_id=USER_A))
    )

    _run(monkeypatch, ["codex", "login"])

    assert seen["cmd"][0] == "/usr/local/bin/codex"
    assert seen["shell"] is False
    assert CodexStore().slots()[0].account_key == KEY_A


def test_codex_login_forwards_device_auth(monkeypatch, codex_home: Path):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd

        class R:
            returncode = 0

        return R()

    monkeypatch.setattr("claude_swap.cli_codex.shutil.which", lambda n: "/usr/local/bin/codex")
    monkeypatch.setattr("claude_swap.cli_codex.subprocess.run", fake_run)
    (codex_home / "auth.json").write_text(
        json.dumps(make_auth_json(account_id=ACCT_A, user_id=USER_A))
    )

    _run(monkeypatch, ["codex", "login", "--device-auth"])

    assert "--device-auth" in seen["cmd"]


def test_codex_login_without_the_binary_fails_clearly(monkeypatch, capsys, codex_home: Path):
    monkeypatch.setattr("claude_swap.cli_codex.shutil.which", lambda n: None)
    with pytest.raises(SystemExit):
        _run(monkeypatch, ["codex", "login"])
    assert "codex" in capsys.readouterr().err.lower()


def test_a_failed_codex_login_does_not_add_an_account(monkeypatch, codex_home: Path):
    def fake_run(cmd, **kwargs):
        class R:
            returncode = 2

        return R()

    monkeypatch.setattr("claude_swap.cli_codex.shutil.which", lambda n: "/usr/local/bin/codex")
    monkeypatch.setattr("claude_swap.cli_codex.subprocess.run", fake_run)

    with pytest.raises(SystemExit):
        _run(monkeypatch, ["codex", "login"])

    assert CodexStore().slots() == []


def test_explicit_import_reports_its_counts(monkeypatch, capsys, codex_home: Path):
    _run(monkeypatch, ["codex", "import"])
    assert "Imported 0" in capsys.readouterr().out


# ---- token status (stage 2) --------------------------------------------


def test_token_status_reports_expiry_without_the_token(
    monkeypatch, capsys, codex_home: Path, offline
):
    """These diagnostics exist so people do not paste a token into an issue."""
    _seed_one()
    _run(monkeypatch, ["codex", "list", "--skip-api", "--token-status"])
    out = capsys.readouterr().out

    assert "token:" in out and "expires in" in out
    payload = make_auth_json(account_id=ACCT_A, user_id=USER_A, email="a@example.com")
    for secret in (
        payload["tokens"]["access_token"],
        payload["tokens"]["refresh_token"],
        payload["tokens"]["id_token"],
    ):
        assert secret not in out


def test_token_status_json_carries_the_block(monkeypatch, capsys, codex_home: Path, offline):
    _seed_one()
    _run(monkeypatch, ["codex", "list", "--json", "--skip-api", "--token-status"])
    data = json.loads(capsys.readouterr().out)
    entry = data["tokenStatus"][0]
    assert entry["state"] == "oauth"
    assert entry["hasRefreshToken"] is True
    assert entry["refreshDue"] is False
    assert "access_token" not in json.dumps(data)


def test_token_status_marks_an_expired_token_as_due(
    monkeypatch, capsys, codex_home: Path, offline
):
    store = CodexStore()
    store.upsert_slot(KEY_A, email="a@example.com", plan="pro")
    store.write_snapshot(
        KEY_A, make_auth_json(account_id=ACCT_A, user_id=USER_A, exp=0)
    )
    _run(monkeypatch, ["codex", "list", "--skip-api", "--token-status"])
    out = capsys.readouterr().out
    assert "refresh due" in out and "expired" in out


def test_token_status_of_an_api_key_account_says_so(
    monkeypatch, capsys, codex_home: Path, offline
):
    store = CodexStore()
    store.upsert_slot(KEY_A, email="a@example.com", auth_mode="apikey")
    store.write_snapshot(KEY_A, {"auth_mode": "apikey", "OPENAI_API_KEY": "sk-x", "tokens": None})
    _run(monkeypatch, ["codex", "list", "--skip-api", "--token-status"])
    assert "token: api key" in capsys.readouterr().out


def test_json_reports_age_and_fetch_time(monkeypatch, capsys, codex_home: Path, monkeypatch2=None):
    """A cached row must say how old it is, not pretend to be current."""
    from claude_swap.codex.usage import UsageFetch

    _seed_one()
    monkeypatch.setattr(
        "claude_swap.codex.usage_cache.fetch_usage",
        lambda *a, **k: UsageFetch(usage={"five_hour": {"pct": 3}, "plan": "pro"}),
    )
    _run(monkeypatch, ["codex", "list", "--json"])
    data = json.loads(capsys.readouterr().out)
    row = data["accounts"][0]
    assert row["fetchedAt"] is not None
    assert row["ageSeconds"] is not None
    assert row["plan"] == "pro"
