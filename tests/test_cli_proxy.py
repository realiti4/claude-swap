"""Tests for the ``cswap proxy`` command line."""

from __future__ import annotations

import subprocess
import sys
from unittest.mock import patch

import pytest

from claude_swap import cli, proxy
from claude_swap.switcher import ClaudeAccountSwitcher
from tests.test_cli import _subprocess_env


class TestProxyCommand:
    @pytest.fixture(autouse=True)
    def _calls(self, monkeypatch):
        self.calls: list[dict] = []

        def fake_run_proxy(host, port, switcher, on_listening=None):
            self.calls.append({"host": host, "port": port, "switcher": switcher})
            if on_listening is not None:
                on_listening("127.0.0.1", port)
            return 0

        monkeypatch.setattr(proxy, "run_proxy", fake_run_proxy)

    def _run(self, argv: list[str]) -> int:
        with patch("os.geteuid", return_value=1000, create=True), \
             patch.object(sys, "argv", ["claude-swap", "proxy", *argv]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        return excinfo.value.code

    def test_defaults_to_loopback_on_the_documented_port(self, temp_home):
        assert self._run([]) == 0
        (call,) = self.calls
        assert (call["host"], call["port"]) == ("127.0.0.1", proxy.DEFAULT_PORT)
        assert isinstance(call["switcher"], ClaudeAccountSwitcher)

    def test_host_and_port_are_forwarded(self, temp_home):
        assert self._run(["--host", "localhost", "--port", "9001"]) == 0
        assert (self.calls[0]["host"], self.calls[0]["port"]) == ("localhost", 9001)

    def test_prints_how_to_point_claude_code_at_it(self, temp_home, capsys):
        self._run(["--port", "9001"])
        out = capsys.readouterr().out
        assert "ANTHROPIC_BASE_URL=http://127.0.0.1:9001" in out
        # Without it, Claude Code treats the proxy as a third-party host and
        # sends every MCP tool definition up front (measured: 145,866 prompt
        # tokens instead of 26,475 for the same one-word request).
        assert "ENABLE_TOOL_SEARCH=true" in out

    def test_sigterm_stops_it_like_ctrl_c(self, temp_home, monkeypatch):
        import signal

        installed = {}
        monkeypatch.setattr(signal, "signal", lambda sig, handler: installed.setdefault(sig, handler))
        self._run([])
        with pytest.raises(KeyboardInterrupt):
            installed[signal.SIGTERM](signal.SIGTERM, None)

    def test_refused_as_root_outside_a_container(self, temp_home, capsys):
        with patch("os.geteuid", return_value=0, create=True), \
             patch.object(ClaudeAccountSwitcher, "_is_running_in_container", return_value=False), \
             patch.object(sys, "argv", ["claude-swap", "proxy"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == 1
        assert self.calls == []

    def test_help(self, capsys):
        with patch.object(sys, "argv", ["claude-swap", "proxy", "--help"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == 0
        out = capsys.readouterr().out
        assert "ANTHROPIC_BASE_URL" in out
        assert "ENABLE_TOOL_SEARCH=true" in out
        assert "--port" in out


class TestProxyCommandRefusals:
    def test_a_non_loopback_host_exits_1_with_the_reason(self, temp_home, capsys):
        with patch("os.geteuid", return_value=1000, create=True), \
             patch.object(sys, "argv", ["claude-swap", "proxy", "--host", "0.0.0.0"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == 1
        assert "loopback" in capsys.readouterr().err

    def test_a_store_error_exits_1_with_the_reason(self, temp_home, capsys):
        from claude_swap.exceptions import ClaudeSwitchError

        with patch("claude_swap.cli.ClaudeAccountSwitcher", side_effect=ClaudeSwitchError("store locked")), \
             patch.object(sys, "argv", ["claude-swap", "proxy"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == 1
        assert "store locked" in capsys.readouterr().err

    def test_a_port_in_use_exits_1_with_the_reason(self, temp_home, capsys):
        import socket

        taken = socket.socket()
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        try:
            port = taken.getsockname()[1]
            with patch("os.geteuid", return_value=1000, create=True), \
                 patch.object(sys, "argv", ["claude-swap", "proxy", "--port", str(port)]):
                with pytest.raises(SystemExit) as excinfo:
                    cli.main()
        finally:
            taken.close()
        assert excinfo.value.code == 1
        assert str(port) in capsys.readouterr().err


def test_main_help_lists_proxy(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "claude_swap", "--help"],
        capture_output=True,
        text=True,
        env=_subprocess_env(HOME=str(tmp_path)),
    )
    assert "proxy" in result.stdout
