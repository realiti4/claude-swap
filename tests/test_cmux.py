"""Tests for the cmux integration (pure parts; no real cmux is ever invoked)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_swap import cmux
from claude_swap.models import Platform
from claude_swap.switcher import ClaudeAccountSwitcher


# --------------------------------------------------------------------------- #
# build_surface / command resolution
# --------------------------------------------------------------------------- #


class TestSurface:
    def test_build_surface_shape(self):
        s = cmux.build_surface()
        assert s["name"] == cmux.SURFACE_NAME
        assert s["type"] == "terminal"
        assert "launch" in s["command"]
        assert s["cwd"] == "."
        # The preserve-key env MUST include CLAUDE_CONFIG_DIR so the wrapper keeps
        # cswap's per-session profile pin.
        env = s["env"]
        assert cmux.CLAUDE_CONFIG_DIR_KEY in env[cmux.PRESERVE_KEYS_ENV]

    def test_launch_command_prefers_path(self, monkeypatch):
        monkeypatch.setattr(cmux.shutil, "which", lambda name: "/usr/local/bin/cswap")
        assert cmux.cswap_launch_command() == "cswap launch"

    def test_launch_command_interpreter_fallback(self, monkeypatch):
        monkeypatch.setattr(cmux.shutil, "which", lambda name: None)
        cmd = cmux.cswap_launch_command()
        assert "-m claude_swap launch" in cmd


# --------------------------------------------------------------------------- #
# preserve-key merge logic (shared with the supervisor)
# --------------------------------------------------------------------------- #


class TestPreserveKeys:
    def test_empty_adds_our_key(self):
        assert cmux.merge_preserve_keys(None) == "CLAUDE_CONFIG_DIR"
        assert cmux.merge_preserve_keys("") == "CLAUDE_CONFIG_DIR"

    def test_appends_without_clobbering(self):
        out = cmux.merge_preserve_keys("FOO,BAR")
        assert out.split(",") == ["FOO", "BAR", "CLAUDE_CONFIG_DIR"]

    def test_idempotent_when_already_present(self):
        assert cmux.merge_preserve_keys("CLAUDE_CONFIG_DIR") == "CLAUDE_CONFIG_DIR"
        assert (
            cmux.merge_preserve_keys("FOO CLAUDE_CONFIG_DIR")
            == "FOO,CLAUDE_CONFIG_DIR"
        )

    def test_space_separated_input(self):
        out = cmux.merge_preserve_keys("FOO BAR")
        assert out.split(",") == ["FOO", "BAR", "CLAUDE_CONFIG_DIR"]


# --------------------------------------------------------------------------- #
# find_cmux / availability
# --------------------------------------------------------------------------- #


class TestDiscovery:
    def test_find_cmux_prefers_path(self, monkeypatch):
        monkeypatch.setattr(cmux.shutil, "which", lambda name: "/opt/bin/cmux")
        assert cmux.find_cmux() == "/opt/bin/cmux"

    def test_find_cmux_app_bundle_fallback(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cmux.shutil, "which", lambda name: None)
        fake_cli = tmp_path / "cmux"
        fake_cli.write_text("#!/bin/sh\n")
        monkeypatch.setattr(cmux, "_APP_BUNDLE_CLI", fake_cli)
        assert cmux.find_cmux() == str(fake_cli)

    def test_find_cmux_absent(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cmux.shutil, "which", lambda name: None)
        monkeypatch.setattr(cmux, "_APP_BUNDLE_CLI", tmp_path / "nope")
        assert cmux.find_cmux() is None

    def test_is_available_requires_macos(self, monkeypatch):
        monkeypatch.setattr(cmux, "find_cmux", lambda: "/opt/bin/cmux")
        monkeypatch.setattr(Platform, "detect", classmethod(lambda cls: Platform.LINUX))
        assert cmux.is_available() is False
        monkeypatch.setattr(Platform, "detect", classmethod(lambda cls: Platform.MACOS))
        assert cmux.is_available() is True

    def test_require_macos_cmux_raises_off_macos(self, monkeypatch):
        from claude_swap.exceptions import SessionError

        monkeypatch.setattr(Platform, "detect", classmethod(lambda cls: Platform.LINUX))
        with pytest.raises(SessionError, match="macOS-only"):
            cmux._require_macos_cmux()

    def test_require_macos_cmux_raises_when_missing(self, monkeypatch):
        from claude_swap.exceptions import SessionError

        monkeypatch.setattr(Platform, "detect", classmethod(lambda cls: Platform.MACOS))
        monkeypatch.setattr(cmux, "find_cmux", lambda: None)
        with pytest.raises(SessionError, match="cmux was not found"):
            cmux._require_macos_cmux()


# --------------------------------------------------------------------------- #
# JSONC parsing + merge idempotency / user-content preservation
# --------------------------------------------------------------------------- #


class TestConfigMerge:
    def test_load_jsonc_with_comments(self, tmp_path):
        p = tmp_path / "cmux.json"
        p.write_text(
            """{
  // a line comment
  "schemaVersion": 1, /* block */
  "app": {"minimalMode": false},
}"""
        )
        data = cmux.load_config(p)
        assert data["schemaVersion"] == 1
        assert data["app"]["minimalMode"] is False

    def test_load_missing_returns_empty(self, tmp_path):
        assert cmux.load_config(tmp_path / "absent.json") == {}

    def test_load_empty_returns_empty(self, tmp_path):
        p = tmp_path / "cmux.json"
        p.write_text("")
        assert cmux.load_config(p) == {}

    def test_load_corrupt_returns_empty(self, tmp_path):
        p = tmp_path / "cmux.json"
        p.write_text("{ not json ]]")
        assert cmux.load_config(p) == {}

    def test_comment_marker_inside_string_preserved(self, tmp_path):
        p = tmp_path / "cmux.json"
        p.write_text('{"x": "http://example.com//path"}')
        assert cmux.load_config(p)["x"] == "http://example.com//path"

    def test_merge_into_empty(self):
        merged, changed = cmux.merge_surface({})
        assert changed is True
        assert any(c["name"] == cmux.SURFACE_NAME for c in merged["commands"])
        assert "$schema" in merged and merged["schemaVersion"] == 1

    def test_merge_preserves_user_content(self):
        config = {
            "schemaVersion": 1,
            "app": {"minimalMode": True},
            "commands": [{"name": "My Custom", "command": "echo hi"}],
            "shortcuts": {"bindings": {"quit": "cmd+q"}},
        }
        merged, changed = cmux.merge_surface(config)
        assert changed is True
        # Every pre-existing key + command survives.
        assert merged["app"] == {"minimalMode": True}
        assert merged["shortcuts"]["bindings"]["quit"] == "cmd+q"
        names = [c["name"] for c in merged["commands"]]
        assert "My Custom" in names and cmux.SURFACE_NAME in names

    def test_merge_is_idempotent(self):
        merged1, changed1 = cmux.merge_surface({})
        assert changed1 is True
        merged2, changed2 = cmux.merge_surface(merged1)
        assert changed2 is False
        assert merged2["commands"] == merged1["commands"]

    def test_merge_does_not_duplicate_on_rerun(self):
        merged, _ = cmux.merge_surface({})
        merged, _ = cmux.merge_surface(merged)
        merged, _ = cmux.merge_surface(merged)
        ours = [c for c in merged["commands"] if c["name"] == cmux.SURFACE_NAME]
        assert len(ours) == 1

    def test_merge_updates_stale_entry(self):
        # A pre-existing entry with our name but a different command is refreshed.
        config = {
            "commands": [{"name": cmux.SURFACE_NAME, "command": "old", "cwd": "/x"}]
        }
        merged, changed = cmux.merge_surface(config)
        assert changed is True
        ours = [c for c in merged["commands"] if c["name"] == cmux.SURFACE_NAME]
        assert len(ours) == 1
        assert ours[0] == cmux.build_surface()

    def test_merge_does_not_mutate_caller(self):
        config = {"commands": [{"name": "Keep", "command": "x"}]}
        original = json.loads(json.dumps(config))
        cmux.merge_surface(config)
        assert config == original  # caller's dict/list untouched


# --------------------------------------------------------------------------- #
# fanout command construction (pure)
# --------------------------------------------------------------------------- #


class TestFanoutCommand:
    def test_no_args(self, monkeypatch):
        monkeypatch.setattr(cmux.shutil, "which", lambda name: "/bin/cswap")
        assert cmux._build_launch_command([]) == "cswap launch"

    def test_with_claude_args_quoted(self, monkeypatch):
        monkeypatch.setattr(cmux.shutil, "which", lambda name: "/bin/cswap")
        cmd = cmux._build_launch_command(["--resume", "my session"])
        assert cmd == "cswap launch -- '--resume' 'my session'"

    def test_quote_escapes_single_quote(self):
        assert cmux._shell_quote("a'b") == "'a'\\''b'"


# --------------------------------------------------------------------------- #
# config_path
# --------------------------------------------------------------------------- #


class TestConfigPath:
    def test_config_path_under_home(self, temp_home: Path):
        p = cmux.config_path()
        assert p == temp_home / ".config" / "cmux" / "cmux.json"


# --------------------------------------------------------------------------- #
# setup() end-to-end with the cmux CLI mocked (no real cmux)
# --------------------------------------------------------------------------- #


class _FakeCLI:
    """Records cmux CLI calls; reports validate/reload success."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def run(self, args, **kwargs):
        import subprocess

        self.calls.append(args)
        sub = args[1] if len(args) > 1 else ""
        if args[1:3] == ["config", "validate"]:
            return subprocess.CompletedProcess(
                args, 0, stdout="JSONC syntax is valid\n", stderr=""
            )
        if sub == "reload-config":
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if sub == "list-workspaces":
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


@pytest.fixture
def _macos_with_fake_cmux(monkeypatch):
    """Force macOS + a fake cmux CLI, capturing every subprocess invocation."""
    monkeypatch.setattr(Platform, "detect", classmethod(lambda cls: Platform.MACOS))
    monkeypatch.setattr(cmux, "find_cmux", lambda: "/fake/cmux")
    fake = _FakeCLI()
    monkeypatch.setattr(cmux.subprocess, "run", fake.run)
    return fake


class TestSetup:
    def test_setup_writes_surface_and_validates(self, temp_home, _macos_with_fake_cmux):
        sw = ClaudeAccountSwitcher()
        status = cmux.setup(sw)
        assert status["ok"] is True
        assert status["changed"] is True
        assert status["validated"] is True
        assert status["reloaded"] is True
        # cmux.json now contains our surface and is strict, parseable JSON.
        cfg = json.loads(Path(status["config_path"]).read_text())
        assert any(c["name"] == cmux.SURFACE_NAME for c in cfg["commands"])

    def test_setup_idempotent_no_backup_first_run(self, temp_home, _macos_with_fake_cmux):
        sw = ClaudeAccountSwitcher()
        first = cmux.setup(sw)
        # No prior cmux.json -> nothing to back up.
        assert first["backup_path"] is None
        second = cmux.setup(sw)
        assert second["changed"] is False  # already current
        assert second["backup_path"] is not None  # now there IS a file to back up

    def test_setup_preserves_existing_user_config(self, temp_home, _macos_with_fake_cmux):
        cfg_path = cmux.config_path()
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(
            '{\n  // user comment\n  "app": {"minimalMode": true},\n'
            '  "commands": [{"name": "Mine", "command": "x"}]\n}'
        )
        sw = ClaudeAccountSwitcher()
        status = cmux.setup(sw)
        assert status["ok"] is True
        assert status["backup_path"] is not None
        assert Path(status["backup_path"]).exists()
        cfg = json.loads(cfg_path.read_text())
        assert cfg["app"]["minimalMode"] is True
        names = [c["name"] for c in cfg["commands"]]
        assert "Mine" in names and cmux.SURFACE_NAME in names

    def test_setup_off_macos_raises(self, temp_home, monkeypatch):
        from claude_swap.exceptions import SessionError

        monkeypatch.setattr(Platform, "detect", classmethod(lambda cls: Platform.LINUX))
        sw = ClaudeAccountSwitcher()
        with pytest.raises(SessionError, match="macOS-only"):
            cmux.setup(sw)


# --------------------------------------------------------------------------- #
# supervisor env: CMUX preserve-key wiring
# --------------------------------------------------------------------------- #


class TestSupervisorEnv:
    def test_preserve_key_added_under_cmux(self, temp_home, monkeypatch):
        from claude_swap.supervisor import Supervisor

        sw = ClaudeAccountSwitcher()
        sup = Supervisor(
            sw, "mid", temp_home / "p", "1", cwd=str(temp_home), share=False
        )
        monkeypatch.setenv("CMUX_SURFACE_ID", "surface:1")
        monkeypatch.setenv(cmux.PRESERVE_KEYS_ENV, "EXISTING")
        env = sup._session_env()
        keys = env[cmux.PRESERVE_KEYS_ENV].split(",")
        assert "EXISTING" in keys
        assert cmux.CLAUDE_CONFIG_DIR_KEY in keys
        assert env["CLAUDE_CONFIG_DIR"] == str(temp_home / "p")

    def test_no_preserve_key_outside_cmux(self, temp_home, monkeypatch):
        from claude_swap.supervisor import Supervisor

        sw = ClaudeAccountSwitcher()
        sup = Supervisor(
            sw, "mid", temp_home / "p", "1", cwd=str(temp_home), share=False
        )
        monkeypatch.delenv("CMUX_SURFACE_ID", raising=False)
        monkeypatch.delenv(cmux.PRESERVE_KEYS_ENV, raising=False)
        env = sup._session_env()
        assert cmux.PRESERVE_KEYS_ENV not in env
