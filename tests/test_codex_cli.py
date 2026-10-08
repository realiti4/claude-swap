"""``cswap codex <command>``: the CLI routes every command to the Codex switcher.

A leading ``codex`` token picks the provider; the rest is the ordinary command
line. Claude's spelling (no prefix) must keep building
``ClaudeAccountSwitcher(debug=False)`` exactly and print no ``provider``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from claude_swap import cli
from claude_swap.autoswitch import NoSwitchEvent, TickOutcome
from claude_swap.exceptions import ConfigError


@pytest.fixture
def codex_cls(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """``CodexAccountSwitcher`` replaced by a mock class."""
    cls = MagicMock(name="CodexAccountSwitcher")
    monkeypatch.setattr("claude_swap.codex_switcher.CodexAccountSwitcher", cls)
    return cls


@pytest.fixture
def claude_cls(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    cls = MagicMock(name="ClaudeAccountSwitcher")
    monkeypatch.setattr(cli, "ClaudeAccountSwitcher", cls)
    return cls


def _main(monkeypatch: pytest.MonkeyPatch, *argv: str):
    """Run ``cswap <argv>``; returns the exit code (None if it returned)."""
    monkeypatch.setattr(sys, "argv", ["cswap", *argv])
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr("claude_swap.update_check.check_for_update", lambda v: None)
    try:
        cli.main()
    except SystemExit as e:
        return e.code
    return None


class FakeEngine:
    """Stand-in ``AutoSwitchEngine`` whose tick emits one no-switch event."""

    instances: list = []

    def __init__(self, switcher, settings, on_event, *, dry_run=False, **_):
        self.switcher, self.settings, self.on_event = switcher, settings, on_event
        FakeEngine.instances.append(self)

    def tick(self):
        self.on_event(NoSwitchEvent(reason="below-threshold"))
        return TickOutcome.NO_ACTION


# -- routing -------------------------------------------------------------------

MAIN_ROUTES = [
    (["list"], "list_accounts", (), {"show_token_status": False, "json_output": False}),
    (["ls", "--token-status"], "list_accounts", (), {"show_token_status": True, "json_output": False}),
    (["status"], "status", (), {"json_output": False}),
    (["switch"], "switch", (), {"strategy": None, "json_output": False, "models": (), "model_source": None}),
    (["switch", "2"], "switch_to", ("2",), {"json_output": False, "force": False}),
    (["--switch-to", "2", "--force"], "switch_to", ("2",), {"json_output": False, "force": True}),
    (["add", "--slot", "3", "--alias", "w"], "add_account", (), {"slot": 3, "alias": "w"}),
    (["--add-account"], "add_account", (), {"slot": None, "alias": None}),
    (["add-token", "sk-proj-x", "--slot", "4"], "add_account_from_token", (),
     {"token": "sk-proj-x", "email": None, "slot": 4}),
    (["remove", "2"], "remove_account", ("2",), {}),
    (["disable", "2"], "set_account_disabled", ("2", True), {}),
    (["enable", "2"], "set_account_disabled", ("2", False), {}),
    (["purge"], "purge", (), {}),
]


@pytest.mark.parametrize(
    "argv,method,args,kwargs", MAIN_ROUTES, ids=[" ".join(r[0]) for r in MAIN_ROUTES]
)
def test_main_parser_commands_route_to_codex(
    monkeypatch, codex_cls, claude_cls, argv, method, args, kwargs
):
    assert _main(monkeypatch, "codex", *argv) is None
    codex_cls.assert_called_once_with(debug=False)
    getattr(codex_cls.return_value, method).assert_called_once_with(*args, **kwargs)
    claude_cls.assert_not_called()


@pytest.mark.parametrize("verb,func,arg", [
    ("export", "export_accounts", "out.json"),
    ("import", "import_accounts", "in.json"),
    ("import-usage", "import_usage", "usage.json"),
])
def test_transfer_commands_get_the_codex_switcher(
    monkeypatch, codex_cls, claude_cls, verb, func, arg
):
    seen = MagicMock()
    monkeypatch.setattr(f"claude_swap.transfer.{func}", seen)
    _main(monkeypatch, "codex", verb, arg)
    assert seen.call_args.args[:2] == (codex_cls.return_value, arg)
    claude_cls.assert_not_called()


@pytest.mark.parametrize("verb,start", [("tui", None), ("watch", "watch")])
def test_tui_and_watch_get_the_codex_switcher(
    monkeypatch, codex_cls, claude_cls, verb, start
):
    seen = MagicMock(return_value=0)
    monkeypatch.setattr("claude_swap.tui.run", seen)
    assert _main(monkeypatch, "codex", verb) == 0
    expected = {"start": start} if start else {}
    seen.assert_called_once_with(codex_cls.return_value, **expected)
    claude_cls.assert_not_called()


def test_bare_codex_on_a_tty_opens_the_codex_tui(monkeypatch, codex_cls, claude_cls):
    seen = MagicMock(return_value=0)
    monkeypatch.setattr("claude_swap.tui.run", seen)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    assert _main(monkeypatch, "codex") == 0
    seen.assert_called_once_with(codex_cls.return_value)
    claude_cls.assert_not_called()


def test_bare_codex_without_a_tty_is_a_usage_error(monkeypatch, codex_cls, capsys):
    assert _main(monkeypatch, "codex") == 2
    assert "try 'cswap codex help'" in capsys.readouterr().err
    codex_cls.assert_not_called()


def test_menubar_runs_with_the_codex_switcher(monkeypatch, codex_cls, claude_cls):
    seen = MagicMock(return_value=0)
    monkeypatch.setattr("claude_swap.menubar.run", seen)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert _main(monkeypatch, "codex", "menubar") == 0
    seen.assert_called_once_with(codex_cls.return_value)
    claude_cls.assert_not_called()


@pytest.mark.parametrize("argv,method,args", [
    (["map"], "list_mappings", ()),
    (["alias"], "list_aliases", ()),
    (["unclaimed"], "list_unclaimed_credentials", ()),
    (["swap", "1", "2"], "swap_accounts", ("1", "2")),
    (["move", "1", "3"], "move_account", ("1", "3")),
])
def test_pre_dispatched_commands_route_to_codex(
    monkeypatch, codex_cls, claude_cls, argv, method, args
):
    switcher = codex_cls.return_value
    switcher.list_aliases.return_value = []
    switcher.list_unclaimed_credentials.return_value = {}
    switcher.swap_accounts.return_value = ("1", "2")
    switcher.move_account.return_value = ("1", "3", False)
    switcher._get_sequence_data.return_value = {}
    assert _main(monkeypatch, "codex", *argv) is None
    codex_cls.assert_called_once_with(debug=False)
    getattr(switcher, method).assert_called_once_with(*args)
    claude_cls.assert_not_called()


def test_run_uses_the_codex_session_manager(monkeypatch, codex_cls, claude_cls):
    _main(monkeypatch, "codex", "run", "2", "--share-history", "--", "resume")
    manager = codex_cls.return_value.make_session_manager.return_value
    manager.run.assert_called_once_with(
        "2", ["resume"], share=True, share_history=True, require_session=False
    )
    claude_cls.assert_not_called()


def test_run_without_a_mapping_launches_plain_codex(monkeypatch, codex_cls):
    codex_cls.return_value.slot_for_directory.return_value = (None, None)
    _main(monkeypatch, "codex", "run")
    manager = codex_cls.return_value.make_session_manager.return_value
    manager.exec_default.assert_called_once_with([])


def test_auto_runs_the_engine_on_the_codex_switcher(monkeypatch, codex_home, claude_cls):
    from claude_swap.codex_switcher import CodexAccountSwitcher

    FakeEngine.instances = []
    monkeypatch.setattr("claude_swap.autoswitch.AutoSwitchEngine", FakeEngine)
    assert _main(monkeypatch, "codex", "auto", "--once") == TickOutcome.NO_ACTION.value
    assert isinstance(FakeEngine.instances[-1].switcher, CodexAccountSwitcher)
    claude_cls.assert_not_called()


def test_plain_cswap_still_builds_the_claude_switcher_exactly(
    monkeypatch, codex_cls, claude_cls
):
    _main(monkeypatch, "list")
    claude_cls.assert_called_once_with(debug=False)
    codex_cls.assert_not_called()


def test_the_codex_selection_does_not_outlive_its_invocation(
    monkeypatch, codex_cls, claude_cls
):
    _main(monkeypatch, "codex", "list")
    claude_cls.return_value.list_aliases.return_value = []
    cli._alias_command([])  # a later call that does not pass through main()
    claude_cls.assert_called_once_with(debug=False)
    codex_cls.assert_called_once()


# -- add --login ---------------------------------------------------------------

@pytest.mark.parametrize("extra,device_auth", [([], False), (["--device-auth"], True)])
def test_codex_add_login_signs_in_a_new_account(
    monkeypatch, codex_cls, extra, device_auth
):
    assert _main(monkeypatch, "codex", "add", "--login", *extra, "--slot", "3",
                 "--alias", "work") is None
    switcher = codex_cls.return_value
    switcher.add_account_via_login.assert_called_once_with(
        slot=3, alias="work", device_auth=device_auth
    )
    switcher.add_account.assert_not_called()


def test_claude_add_login_is_refused(monkeypatch, claude_cls, capsys):
    assert _main(monkeypatch, "add", "--login") == 2
    assert "only supported for codex" in capsys.readouterr().err
    claude_cls.assert_not_called()


@pytest.mark.parametrize("argv,needs", [
    (["codex", "add", "--device-auth"], "--device-auth can only be used with"),
    (["codex", "list", "--login"], "--login can only be used with 'add'"),
])
def test_login_flags_are_validated(monkeypatch, codex_cls, capsys, argv, needs):
    assert _main(monkeypatch, *argv) == 2
    assert needs in capsys.readouterr().err
    codex_cls.assert_not_called()


@pytest.mark.parametrize("argv,method,debug", [
    (["--l"], "list_accounts", False),  # unambiguous: --login is codex-only
    (["list", "--de"], "list_accounts", True),  # --debug, not --device-auth
])
def test_claude_option_abbreviations_still_resolve(
    monkeypatch, claude_cls, argv, method, debug
):
    assert _main(monkeypatch, *argv) is None
    claude_cls.assert_called_once_with(debug=debug)
    getattr(claude_cls.return_value, method).assert_called_once()


@pytest.mark.parametrize("flag", ["--device-auth", "--login=yes"])
def test_claude_refuses_every_spelling_of_the_codex_flags(
    monkeypatch, claude_cls, capsys, flag
):
    assert _main(monkeypatch, "add", flag) == 2
    assert "only supported for codex" in capsys.readouterr().err
    claude_cls.assert_not_called()


def test_login_flags_stay_out_of_claude_help(monkeypatch, capsys):
    assert _main(monkeypatch, "--help") == 0
    claude_help = capsys.readouterr().out
    assert "--device-auth" not in claude_help
    assert "cswap codex <command>" in claude_help  # the one mention of the prefix
    assert _main(monkeypatch, "codex", "--help") == 0
    codex_help = capsys.readouterr().out
    assert "--device-auth" in codex_help
    assert "usage: cswap codex <command>" in codex_help
    assert "Multi-Account Switcher for Codex" in codex_help
    assert "codex codex" not in codex_help


def test_codex_run_help_describes_codex(monkeypatch, capsys):
    assert _main(monkeypatch, "codex", "run", "--help") == 0
    out = capsys.readouterr().out
    assert "usage: cswap codex run" in out
    assert "Launch Codex" in out
    assert "AGENTS.md" in out and "CLAUDE.md" not in out
    assert "cswap codex run 2 -- resume" in out and "--resume" not in out


def test_codex_subcommand_help_has_no_claude_only_examples(monkeypatch, capsys):
    assert _main(monkeypatch, "codex", "auto", "--help") == 0
    auto = capsys.readouterr().out
    assert "cswap codex auto --once" in auto
    assert "Fable" not in auto and "Opus" not in auto
    assert _main(monkeypatch, "codex", "--help") == 0
    assert ".claude.json" not in capsys.readouterr().out
    assert _main(monkeypatch, "codex", "unclaimed", "--help") == 0
    assert "cswap codex add --login" in capsys.readouterr().out
    assert _main(monkeypatch, "auto", "--help") == 0
    assert "cswap auto --model Fable" in capsys.readouterr().out  # Claude's kept



def test_codex_purge_and_config_help_describe_codex(monkeypatch, capsys):
    assert _main(monkeypatch, "codex", "--help") == 0
    out = capsys.readouterr().out
    assert "purge                      remove all Codex accounts and their data" in out
    assert "remove all claude-swap data" not in out
    assert _main(monkeypatch, "codex", "config", "--help") == 0
    out = capsys.readouterr().out
    assert "Codex settings" in out and "codex folder of the backup root" in out
    assert "cswap codex config set autoswitch.threshold 80" in out
    assert "cswap config" not in out and "Fable" not in out
    # Claude's help keeps its own wording.
    assert _main(monkeypatch, "--help") == 0
    assert "purge                      remove all claude-swap data" in capsys.readouterr().out
    assert _main(monkeypatch, "config", "--help") == 0
    out = capsys.readouterr().out
    assert "Read and edit claude-swap settings (settings.json in the backup root)." in out
    assert "  cswap config set autoswitch.threshold 80\n" in out and "Fable" in out

# -- JSON provider -------------------------------------------------------------

def test_codex_error_envelope_names_the_provider(monkeypatch, codex_cls, capsys):
    codex_cls.return_value.status.side_effect = ConfigError("nope")
    assert _main(monkeypatch, "codex", "status", "--json") == 1
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["provider"] == "codex"
    assert envelope["error"] == {"type": "ConfigError", "message": "nope"}


def test_claude_error_envelope_has_no_provider(monkeypatch, claude_cls, capsys):
    claude_cls.return_value.status.side_effect = ConfigError("nope")
    assert _main(monkeypatch, "status", "--json") == 1
    assert "provider" not in json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("prefix,provider", [(["codex"], "codex"), ([], None)])
def test_auto_json_events_carry_the_provider_only_for_codex(
    monkeypatch, codex_home, capsys, prefix, provider
):
    monkeypatch.setattr("claude_swap.autoswitch.AutoSwitchEngine", FakeEngine)
    _main(monkeypatch, *prefix, "auto", "--once", "--json")
    event = json.loads(capsys.readouterr().out)
    assert event["event"] == "no-switch"
    assert event.get("provider") == provider


def test_auto_error_envelope_names_the_provider(monkeypatch, codex_cls, capsys):
    codex_cls.side_effect = ConfigError("nope")
    assert _main(monkeypatch, "codex", "auto", "--once", "--json") == 1
    assert json.loads(capsys.readouterr().out)["provider"] == "codex"


# -- config ----------------------------------------------------------------------

class TestCodexConfig:
    def test_list_json_reads_the_codex_settings(self, monkeypatch, codex_home, capsys):
        from claude_swap.paths import get_backup_root

        assert _main(monkeypatch, "codex", "config", "list", "--json") is None
        payload = json.loads(capsys.readouterr().out)
        assert payload["provider"] == "codex"
        assert payload["path"] == str(get_backup_root() / "codex" / "settings.json")

    def test_claude_config_json_has_no_provider(self, monkeypatch, codex_home, capsys):
        _main(monkeypatch, "config", "get", "autoswitch.threshold", "--json")
        assert "provider" not in json.loads(capsys.readouterr().out)

    def test_autoswitch_keys_are_per_provider(self, monkeypatch, codex_home, capsys):
        from claude_swap.paths import get_backup_root

        _main(monkeypatch, "codex", "config", "set", "autoswitch.threshold", "80")
        root = get_backup_root()
        codex_settings = json.loads((root / "codex" / "settings.json").read_text())
        assert codex_settings["autoswitch"]["threshold"] == 80.0
        assert not (root / "settings.json").exists()

    def test_the_theme_is_one_root_setting(self, monkeypatch, codex_home, capsys):
        from claude_swap.paths import get_backup_root

        _main(monkeypatch, "codex", "config", "set", "ui.theme", "light")
        root = get_backup_root()
        assert json.loads((root / "settings.json").read_text())["ui"]["theme"] == "light"
        assert not (root / "codex" / "settings.json").exists()
        capsys.readouterr()
        _main(monkeypatch, "codex", "config", "get", "ui.theme")
        assert capsys.readouterr().out.strip() == "light"
        _main(monkeypatch, "codex", "config", "list", "--json")
        rows = {r["key"]: r for r in json.loads(capsys.readouterr().out)["settings"]}
        assert rows["ui.theme"] == {"key": "ui.theme", "value": "light", "isSet": True}
        assert _main(monkeypatch, "codex", "config", "unset", "ui.theme") is None
        assert "ui" not in json.loads((root / "settings.json").read_text())


# -- menubar service -------------------------------------------------------------

class TestCodexMenubarService:
    def _record(self, monkeypatch) -> dict:
        calls: dict = {}

        def fake(name, payload):
            def call(*args, **kwargs):
                calls[name] = kwargs
                return payload
            return call

        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr("claude_swap.menubar.framework_build_warning", lambda *a: None)
        monkeypatch.setattr("claude_swap.launch_agent.install", fake("install", {
            "label": "com.cswap.menubar.codex", "plist": "/p", "stderr_log": "/e",
        }))
        monkeypatch.setattr("claude_swap.launch_agent.uninstall", fake(
            "uninstall", {"was_loaded": False, "removed_plist": False}))
        monkeypatch.setattr("claude_swap.launch_agent.status", fake("status", {
            "installed": False, "loaded": False, "state": None, "pid": None, "plist": "/p",
        }))
        return calls

    def test_install_uses_the_codex_label_and_arguments(self, monkeypatch, codex_cls, capsys):
        calls = self._record(monkeypatch)
        assert _main(monkeypatch, "codex", "menubar", "--install-service") == 0
        assert calls["install"] == {
            "label": "com.cswap.menubar.codex", "args": ("codex", "menubar"),
        }

    def test_status_and_uninstall_use_the_codex_label(self, monkeypatch, codex_cls, capsys):
        calls = self._record(monkeypatch)
        _main(monkeypatch, "codex", "menubar", "--service-status")
        assert "cswap codex menubar --install-service" in capsys.readouterr().out
        _main(monkeypatch, "codex", "menubar", "--uninstall-service")
        assert calls["status"] == {"label": "com.cswap.menubar.codex"}
        assert calls["uninstall"] == {"label": "com.cswap.menubar.codex"}

    def test_claude_install_keeps_its_label_and_arguments(self, monkeypatch, claude_cls):
        calls = self._record(monkeypatch)
        _main(monkeypatch, "menubar", "--install-service")
        assert calls["install"] == {"label": "com.cswap.menubar", "args": ("menubar",)}


# -- end to end ----------------------------------------------------------------

def test_codex_commands_end_to_end(monkeypatch, codex_home: Path, tmp_path, capsys):
    """The real Codex switcher behind the CLI: add, list, alias, map, unmap,
    status — all stored under ``<root>/codex``, none touching Claude's."""
    from claude_swap.paths import get_backup_root

    root = get_backup_root()
    assert _main(monkeypatch, "codex", "add") is None
    assert _main(monkeypatch, "codex", "alias", "1", "home") is None
    work = tmp_path / "work"
    work.mkdir()
    assert _main(monkeypatch, "codex", "map", "1", str(work)) is None
    capsys.readouterr()

    assert _main(monkeypatch, "codex", "list", "--json") is None
    listed = json.loads(capsys.readouterr().out)
    assert listed["provider"] == "codex"
    assert [(r["number"], r["email"], r.get("alias")) for r in listed["accounts"]] == [
        (1, "user@example.com", "home"),
    ]
    assert _main(monkeypatch, "codex", "status", "--json") is None
    assert json.loads(capsys.readouterr().out)["active"]["number"] == 1
    assert json.loads((root / "codex" / "mappings.json").read_text())["mappings"]

    assert _main(monkeypatch, "codex", "unmap", str(work)) is None
    assert "Unmapped" in capsys.readouterr().out
    assert not (root / "sequence.json").exists()  # Claude's roster untouched

    # The same roster under the Claude spelling is empty.
    assert _main(monkeypatch, "list", "--json") is None
    claude = json.loads(capsys.readouterr().out)
    assert claude["accounts"] == [] and "provider" not in claude


def test_codex_add_token_registers_an_api_key(monkeypatch, codex_home, capsys):
    assert _main(monkeypatch, "codex", "add-token", "sk-proj-abc", "--slot", "2") is None
    capsys.readouterr()
    _main(monkeypatch, "codex", "list", "--json")
    (row,) = json.loads(capsys.readouterr().out)["accounts"]
    assert row["number"] == 2 and row["usageStatus"] == "api_key"
