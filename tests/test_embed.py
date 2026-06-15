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
