"""Tests for update_check module."""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from claude_swap.update_check import (
    CACHE_TTL,
    _detect_install_method,
    check_for_update,
    run_self_upgrade,
)


def _make_pypi_response(version: str) -> MagicMock:
    data = json.dumps({"info": {"version": version}}).encode()
    mock_resp = MagicMock()
    mock_resp.read.return_value = data
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


def _write_cache(path, version, timestamp=None):
    """Write a cache file in the shared cache format."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "timestamp": timestamp if timestamp is not None else time.time(),
        "data": version,
    }))


class TestCheckForUpdate:
    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_newer_version_available(self, mock_urlopen, tmp_path, monkeypatch):
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        mock_urlopen.return_value = _make_pypi_response("0.4.0")

        result = check_for_update("0.3.2")

        assert result is not None
        assert "0.4.0" in result
        assert "0.3.2" in result

    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_already_on_latest(self, mock_urlopen, tmp_path, monkeypatch):
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        mock_urlopen.return_value = _make_pypi_response("0.3.2")

        result = check_for_update("0.3.2")

        assert result is None

    @patch("claude_swap.update_check.urllib.request.urlopen", side_effect=OSError("network error"))
    def test_network_error_returns_none_and_caches(self, mock_urlopen, tmp_path, monkeypatch):
        cache_path = tmp_path / "cache.json"
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", cache_path)

        result = check_for_update("0.3.2")

        assert result is None
        assert cache_path.exists()
        cache = json.loads(cache_path.read_text())
        assert cache["data"] is None

    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_fresh_error_cache_skips_network(self, mock_urlopen, tmp_path, monkeypatch):
        cache_path = tmp_path / "cache.json"
        _write_cache(cache_path, None)
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", cache_path)

        result = check_for_update("0.3.2")

        mock_urlopen.assert_not_called()
        assert result is None

    def test_fresh_cache_no_network(self, tmp_path, monkeypatch):
        cache_path = tmp_path / "cache.json"
        _write_cache(cache_path, "0.5.0")
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", cache_path)

        with patch("claude_swap.update_check.urllib.request.urlopen") as mock_urlopen:
            result = check_for_update("0.3.2")
            mock_urlopen.assert_not_called()

        assert result is not None
        assert "0.5.0" in result

    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_stale_cache_fetches_from_pypi(self, mock_urlopen, tmp_path, monkeypatch):
        cache_path = tmp_path / "cache.json"
        _write_cache(cache_path, "0.3.0", timestamp=time.time() - CACHE_TTL - 1)
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", cache_path)
        mock_urlopen.return_value = _make_pypi_response("0.4.0")

        result = check_for_update("0.3.2")

        mock_urlopen.assert_called_once()
        assert result is not None
        assert "0.4.0" in result


class TestDetectInstallMethod:
    def _set_prefix(self, monkeypatch, prefix: str) -> None:
        monkeypatch.setattr("claude_swap.update_check.sys.prefix", prefix)
        # Clear env vars by default so path-based detection runs in isolation.
        monkeypatch.delenv("UV_TOOL_DIR", raising=False)
        monkeypatch.delenv("PIPX_HOME", raising=False)

    def test_uv_tool_default_path(self, monkeypatch):
        self._set_prefix(monkeypatch, "/home/me/.local/share/uv/tools/claude-swap")
        assert _detect_install_method() == "uv"

    def test_pipx_default_path(self, monkeypatch):
        self._set_prefix(monkeypatch, "/home/me/.local/pipx/venvs/claude-swap")
        assert _detect_install_method() == "pipx"

    def test_non_adjacent_uv_tools_does_not_match(self, monkeypatch):
        # Both segments present but not adjacent — must not false-positive.
        self._set_prefix(monkeypatch, "/home/me/projects/uv/some-tools/.venv")
        assert _detect_install_method() is None

    def test_non_adjacent_pipx_venvs_does_not_match(self, monkeypatch):
        self._set_prefix(monkeypatch, "/home/me/repos/pipx-clone/venvs-of-mine/.venv")
        assert _detect_install_method() is None

    def test_source_checkout_returns_none(self, monkeypatch):
        self._set_prefix(monkeypatch, "/home/me/code/claude-swap/.venv")
        assert _detect_install_method() is None

    def test_mixed_case_path_detected(self, monkeypatch):
        # Lowercasing should make matching case-insensitive (e.g. Windows).
        self._set_prefix(monkeypatch, "/Home/Me/.local/share/UV/Tools/claude-swap")
        assert _detect_install_method() == "uv"

    def test_uv_tool_dir_env_with_prefix_under_it(self, monkeypatch, tmp_path):
        custom_root = tmp_path / "uv-tools"
        prefix = custom_root / "claude-swap"
        monkeypatch.setattr("claude_swap.update_check.sys.prefix", str(prefix))
        monkeypatch.setenv("UV_TOOL_DIR", str(custom_root))
        monkeypatch.delenv("PIPX_HOME", raising=False)
        assert _detect_install_method() == "uv"

    def test_uv_tool_dir_env_set_but_prefix_elsewhere(self, monkeypatch, tmp_path):
        custom_root = tmp_path / "uv-tools"
        # Prefix lives somewhere else entirely — env var alone must not trigger.
        monkeypatch.setattr(
            "claude_swap.update_check.sys.prefix", str(tmp_path / "some-project" / ".venv")
        )
        monkeypatch.setenv("UV_TOOL_DIR", str(custom_root))
        monkeypatch.delenv("PIPX_HOME", raising=False)
        assert _detect_install_method() is None

    def test_pipx_home_env_with_prefix_under_it(self, monkeypatch, tmp_path):
        custom_root = tmp_path / "pipx-home"
        prefix = custom_root / "venvs" / "claude-swap"
        monkeypatch.setattr("claude_swap.update_check.sys.prefix", str(prefix))
        monkeypatch.setenv("PIPX_HOME", str(custom_root))
        monkeypatch.delenv("UV_TOOL_DIR", raising=False)
        assert _detect_install_method() == "pipx"


class TestCheckForUpdateMessage:
    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_uv_hint_appended(self, mock_urlopen, tmp_path, monkeypatch):
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        monkeypatch.setattr("claude_swap.update_check._detect_install_method", lambda: "uv")
        mock_urlopen.return_value = _make_pypi_response("0.4.0")

        result = check_for_update("0.3.2")

        assert result is not None
        assert "uv tool upgrade claude-swap" in result

    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_pipx_hint_appended(self, mock_urlopen, tmp_path, monkeypatch):
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        monkeypatch.setattr("claude_swap.update_check._detect_install_method", lambda: "pipx")
        mock_urlopen.return_value = _make_pypi_response("0.4.0")

        result = check_for_update("0.3.2")

        assert result is not None
        assert "pipx upgrade claude-swap" in result

    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_unknown_falls_back_to_generic(self, mock_urlopen, tmp_path, monkeypatch):
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        monkeypatch.setattr("claude_swap.update_check._detect_install_method", lambda: None)
        mock_urlopen.return_value = _make_pypi_response("0.4.0")

        result = check_for_update("0.3.2")

        assert result is not None
        assert "Consider upgrading" in result
        assert "uv tool upgrade" not in result
        assert "pipx upgrade" not in result


class TestRunSelfUpgrade:
    @patch("claude_swap.update_check.subprocess.run")
    @patch("claude_swap.update_check._detect_install_method", return_value="uv")
    def test_uv_invokes_uv_tool_upgrade(self, mock_detect, mock_run):
        mock_run.return_value = MagicMock(returncode=0)

        assert run_self_upgrade() == 0
        mock_run.assert_called_once_with(
            ["uv", "tool", "upgrade", "claude-swap"], check=False
        )

    @patch("claude_swap.update_check.subprocess.run")
    @patch("claude_swap.update_check._detect_install_method", return_value="pipx")
    def test_pipx_invokes_pipx_upgrade(self, mock_detect, mock_run):
        mock_run.return_value = MagicMock(returncode=0)

        assert run_self_upgrade() == 0
        mock_run.assert_called_once_with(
            ["pipx", "upgrade", "claude-swap"], check=False
        )

    @patch("claude_swap.update_check.subprocess.run")
    @patch("claude_swap.update_check._detect_install_method", return_value="uv")
    def test_propagates_nonzero_exit_code(self, mock_detect, mock_run):
        mock_run.return_value = MagicMock(returncode=2)

        assert run_self_upgrade() == 2

    @patch("claude_swap.update_check.subprocess.run")
    @patch("claude_swap.update_check._detect_install_method", return_value=None)
    def test_unknown_method_returns_1_and_prints_instructions(
        self, mock_detect, mock_run, capsys
    ):
        assert run_self_upgrade() == 1
        mock_run.assert_not_called()
        err = capsys.readouterr().err
        assert "uv tool upgrade claude-swap" in err
        assert "pipx upgrade claude-swap" in err
        assert "pip install --upgrade claude-swap" in err

    @patch(
        "claude_swap.update_check.subprocess.run", side_effect=FileNotFoundError
    )
    @patch("claude_swap.update_check._detect_install_method", return_value="uv")
    def test_filenotfound_returns_1(self, mock_detect, mock_run, capsys):
        assert run_self_upgrade() == 1
        err = capsys.readouterr().err
        assert "PATH" in err
