"""Tests for embedding cswap into Claude Code (managed-session settings)."""

from __future__ import annotations

import json
from pathlib import Path

from claude_swap import embed
from claude_swap.switcher import ClaudeAccountSwitcher


class TestManagedSettings:
    def test_build_managed_settings_shape(self):
        s = embed.build_managed_settings()
        assert s["statusLine"]["type"] == "command"
        assert s["statusLine"]["command"]  # non-empty
        assert s["effortLevel"] == embed.EFFORT_LEVEL == "xhigh"

    def test_statusline_command_is_a_string(self):
        cmd = embed.cswap_statusline_command()
        assert isinstance(cmd, str) and "statusline" in cmd

    def test_statusfailure_command_is_a_string(self):
        cmd = embed.cswap_statusfailure_command()
        assert isinstance(cmd, str) and "statusfailure" in cmd

    def test_build_managed_settings_carries_stopfailure_hook(self):
        s = embed.build_managed_settings()
        groups = s["hooks"]["StopFailure"]
        cmd = groups[0]["hooks"][0]["command"]
        assert groups[0]["hooks"][0]["type"] == "command"
        assert "statusfailure" in cmd

    def test_install_into_profile_keeps_user_hooks_and_adds_stopfailure(
        self, temp_home: Path
    ):
        # A user with their own hooks (incl. a StopFailure hook) must keep them,
        # with cswap's StopFailure safety net added alongside (not clobbering).
        user_settings = Path.home() / ".claude" / "settings.json"
        user_settings.write_text(
            json.dumps(
                {
                    "hooks": {
                        "PreToolUse": [{"hooks": [{"type": "command", "command": "mine"}]}],
                        "StopFailure": [{"hooks": [{"type": "command", "command": "theirs"}]}],
                    }
                }
            )
        )
        sw = ClaudeAccountSwitcher()
        profile = sw.managed_dir / "abc123"
        embed.install_into_profile(sw, profile)
        data = json.loads((profile / "settings.json").read_text())
        # User's unrelated hook survives.
        assert data["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "mine"
        # User's StopFailure hook survives AND ours is appended.
        cmds = [
            h["command"]
            for g in data["hooks"]["StopFailure"]
            for h in g["hooks"]
        ]
        assert "theirs" in cmds
        assert any("statusfailure" in c for c in cmds)


class TestTemplate:
    def test_write_template_then_idempotent(self, temp_home: Path):
        sw = ClaudeAccountSwitcher()
        assert embed.write_managed_template(sw) is True
        # Second call: already current -> no rewrite.
        assert embed.write_managed_template(sw) is False
        path = embed.managed_template_path(sw)
        data = json.loads(path.read_text())
        assert data["effortLevel"] == "xhigh"

    def test_install_into_profile_writes_real_settings(self, temp_home: Path):
        sw = ClaudeAccountSwitcher()
        profile = sw.managed_dir / "abc123"
        embed.install_into_profile(sw, profile)
        settings = profile / "settings.json"
        assert settings.exists()
        # Must be a real file, never a symlink into ~/.claude.
        assert not settings.is_symlink()
        data = json.loads(settings.read_text())
        assert "statusLine" in data and data["effortLevel"] == "xhigh"


class TestEmbedHealth:
    def test_unhealthy_before_install(self, temp_home: Path):
        sw = ClaudeAccountSwitcher()
        health = embed.embed_health(sw)
        assert health["ok"] is False
        assert health["template_ok"] is False
        assert health["issues"]

    def test_healthy_after_install(self, temp_home: Path):
        sw = ClaudeAccountSwitcher()
        health = embed.install(sw)
        assert health["ok"] is True
        assert health["template_ok"] is True
        assert health["issues"] == []

    def test_health_flags_tampered_template(self, temp_home: Path):
        sw = ClaudeAccountSwitcher()
        embed.install(sw)
        # Corrupt the template's effort level.
        path = embed.managed_template_path(sw)
        data = json.loads(path.read_text())
        data["effortLevel"] = "low"
        path.write_text(json.dumps(data))
        assert embed.embed_health(sw)["ok"] is False

    def test_health_flags_template_missing_stopfailure_hook(self, temp_home: Path):
        sw = ClaudeAccountSwitcher()
        embed.install(sw)
        # An older template (no StopFailure hook) must read as unhealthy so the
        # upgrade migration refreshes it.
        path = embed.managed_template_path(sw)
        data = json.loads(path.read_text())
        data.pop("hooks", None)
        path.write_text(json.dumps(data))
        assert embed.embed_health(sw)["ok"] is False
