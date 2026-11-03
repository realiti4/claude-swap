"""Tests for update_check module."""

from __future__ import annotations

import json
import subprocess
import time
from unittest.mock import MagicMock, patch

import pytest

from claude_swap.update_check import (
    CACHE_TTL,
    FAILURE_TTL,
    _detect_install_method,
    _installed_version_on_disk,
    check_for_update,
    held_back_release_notice,
    run_self_upgrade,
)


def _make_pypi_response(version: str) -> MagicMock:
    data = json.dumps({"info": {"version": version}}).encode()
    mock_resp = MagicMock()
    mock_resp.read.return_value = data
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


def _uv_pip_list(rows: list[dict]) -> MagicMock:
    """A completed `uv pip list --outdated --format json` run."""
    return MagicMock(returncode=0, stdout=json.dumps(rows))


_OUTDATED = [{"name": "claude-swap", "version": "0.27.2", "latest_version": "0.28.0"}]


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

    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_failed_lookup_is_retried_after_an_hour(self, mock_urlopen, tmp_path, monkeypatch):
        # A cached failure outlives a flaky moment, not a working day.
        cache_path = tmp_path / "cache.json"
        _write_cache(cache_path, None, timestamp=time.time() - FAILURE_TTL - 1)
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", cache_path)
        mock_urlopen.return_value = _make_pypi_response("0.4.0")

        result = check_for_update("0.3.2")

        mock_urlopen.assert_called_once()
        assert result is not None
        assert "0.4.0" in result

    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_successful_lookup_outlives_the_failure_backoff(
        self, mock_urlopen, tmp_path, monkeypatch
    ):
        cache_path = tmp_path / "cache.json"
        _write_cache(cache_path, "0.4.0", timestamp=time.time() - FAILURE_TTL - 1)
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", cache_path)

        result = check_for_update("0.3.2")

        mock_urlopen.assert_not_called()
        assert result is not None
        assert "0.4.0" in result

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


class TestCheckForUpdatePrereleases:
    """claude-swap ships every cycle as a pre-release first, so the version
    cli.main hands to check_for_update is routinely something like 0.27.0b1."""

    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_prerelease_is_told_about_later_release(self, mock_urlopen, tmp_path, monkeypatch):
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        mock_urlopen.return_value = _make_pypi_response("0.28.0")

        result = check_for_update("0.27.0b1")

        assert result is not None
        assert "0.28.0" in result
        assert "0.27.0b1" in result

    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_prerelease_is_told_about_its_own_final_release(
        self, mock_urlopen, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        mock_urlopen.return_value = _make_pypi_response("0.27.0")

        result = check_for_update("0.27.0b1")

        assert result is not None
        assert "0.27.0" in result

    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_prerelease_is_told_about_later_prerelease(self, mock_urlopen, tmp_path, monkeypatch):
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        mock_urlopen.return_value = _make_pypi_response("0.27.0rc1")

        result = check_for_update("0.27.0b2")

        assert result is not None
        assert "0.27.0rc1" in result

    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_final_release_is_not_pushed_onto_a_prerelease(
        self, mock_urlopen, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        mock_urlopen.return_value = _make_pypi_response("0.28.0b1")

        assert check_for_update("0.27.0") is None

    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_unparseable_version_stays_silent(self, mock_urlopen, tmp_path, monkeypatch):
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        mock_urlopen.return_value = _make_pypi_response("0.28.0")

        assert check_for_update("main") is None


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
    @patch("claude_swap.update_check.sys.platform", "linux")
    @patch("claude_swap.update_check.subprocess.run")
    def test_detected_method_non_windows_suggests_cswap_upgrade(
        self, mock_run, tmp_path, monkeypatch
    ):
        # uv/pipx on macOS/Linux: cswap upgrade actually upgrades, so advertise it.
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        monkeypatch.setattr("claude_swap.update_check.sys.prefix", str(tmp_path))
        monkeypatch.delenv("UV_EXCLUDE_NEWER", raising=False)
        monkeypatch.setattr("claude_swap.update_check._detect_install_method", lambda: "uv")
        mock_run.return_value = _uv_pip_list([{"name": "claude-swap", "latest_version": "0.4.0"}])

        result = check_for_update("0.3.2")

        assert result is not None
        assert "cswap upgrade" in result
        assert "uv tool upgrade" not in result

    @patch("claude_swap.update_check.sys.platform", "win32")
    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_detected_method_windows_suggests_direct_command(
        self, mock_urlopen, tmp_path, monkeypatch
    ):
        # Windows: cswap upgrade only prints, so point at the real command.
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        monkeypatch.setattr("claude_swap.update_check._detect_install_method", lambda: "pipx")
        mock_urlopen.return_value = _make_pypi_response("0.4.0")

        result = check_for_update("0.3.2")

        assert result is not None
        assert "pipx upgrade claude-swap" in result
        assert "cswap upgrade" not in result

    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_unknown_method_suggests_cswap_instructions(
        self, mock_urlopen, tmp_path, monkeypatch
    ):
        # Unknown install method: cswap upgrade can only show instructions.
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        monkeypatch.setattr("claude_swap.update_check._detect_install_method", lambda: None)
        mock_urlopen.return_value = _make_pypi_response("0.4.0")

        result = check_for_update("0.3.2")

        assert result is not None
        assert "cswap upgrade` for upgrade instructions" in result
        assert "uv tool upgrade" not in result
        assert "pipx upgrade" not in result


class _UvInstall:
    """A uv tool install under tmp_path, with the receipt written per test."""

    @pytest.fixture(autouse=True)
    def _uv_install(self, tmp_path, monkeypatch):
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        monkeypatch.setattr("claude_swap.update_check.sys.prefix", str(tmp_path))
        monkeypatch.setattr("claude_swap.update_check.sys.platform", "linux")
        monkeypatch.setattr("claude_swap.update_check._detect_install_method", lambda: "uv")
        monkeypatch.delenv("UV_EXCLUDE_NEWER", raising=False)
        # PyPI is unreachable unless a test says otherwise, so a uv failure
        # that falls back to it cannot leave the sandbox.
        monkeypatch.setattr(
            "claude_swap.update_check.urllib.request.urlopen",
            MagicMock(side_effect=OSError("network error")),
        )

    @staticmethod
    def _write_receipt(
        prefix, options: str = "", requirements: str = '{ name = "claude-swap" }'
    ) -> None:
        (prefix / "uv-receipt.toml").write_text(
            f"[tool]\nrequirements = [{requirements}]\n\n[tool.options]\n{options}\n"
        )

    @staticmethod
    def _uv_command(mock_run) -> list[str]:
        mock_run.assert_called_once()
        return mock_run.call_args.args[0]


class TestUvInstall(_UvInstall):
    """uv installs ask uv, not PyPI, which release is installable: a tool
    installed with --exclude-newer (a release cooldown) keeps that setting in
    the receipt, `uv tool upgrade` reapplies it, and a release younger than
    the cooldown is invisible to uv. Advertising it would send the user to
    `cswap upgrade` only to read "Nothing to upgrade"."""

    @patch("claude_swap.update_check.urllib.request.urlopen")
    @patch("claude_swap.update_check.subprocess.run")
    def test_asks_uv_with_the_receipt_span(self, mock_run, mock_urlopen, tmp_path):
        self._write_receipt(
            tmp_path, 'exclude-newer = "2026-01-01T00:00:00Z"\nexclude-newer-span = "P14D"'
        )
        mock_run.return_value = _uv_pip_list(_OUTDATED)

        result = check_for_update("0.27.2")

        assert result is not None
        assert "(0.28.0)" in result
        assert "cswap upgrade" in result
        cmd = self._uv_command(mock_run)
        assert cmd[:5] == ["uv", "pip", "list", "--outdated", "--format"]
        assert cmd[-2:] == ["--exclude-newer", "P14D"]
        mock_urlopen.assert_not_called()

    @patch("claude_swap.update_check.subprocess.run")
    def test_absolute_cutoff_without_span(self, mock_run, tmp_path):
        self._write_receipt(tmp_path, 'exclude-newer = "2026-01-01T00:00:00Z"')
        mock_run.return_value = _uv_pip_list([])

        check_for_update("0.27.2")

        assert self._uv_command(mock_run)[-2:] == ["--exclude-newer", "2026-01-01T00:00:00Z"]

    @patch("claude_swap.update_check.subprocess.run")
    def test_env_var_overrides_receipt_and_is_passed_explicitly(
        self, mock_run, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("UV_EXCLUDE_NEWER", "1 week")
        self._write_receipt(tmp_path, 'exclude-newer-span = "P14D"')
        mock_run.return_value = _uv_pip_list([])

        check_for_update("0.27.2")

        assert self._uv_command(mock_run)[-2:] == ["--exclude-newer", "1 week"]
        assert "UV_EXCLUDE_NEWER" not in mock_run.call_args.kwargs["env"]

    @patch("claude_swap.update_check.subprocess.run")
    def test_asks_uv_from_the_tool_directory(self, mock_run, tmp_path):
        # uv reads the uv.toml of whatever project the shell is in, and a
        # project's own exclude-newer would hide releases that `uv tool
        # upgrade`, which ignores project config, is happy to install.
        mock_run.return_value = _uv_pip_list(_OUTDATED)

        check_for_update("0.27.2")

        assert mock_run.call_args.kwargs["cwd"] == str(tmp_path)

    @patch("claude_swap.update_check.subprocess.run")
    def test_no_cooldown_passes_no_cutoff(self, mock_run, tmp_path):
        mock_run.return_value = _uv_pip_list(_OUTDATED)

        result = check_for_update("0.27.2")

        assert result is not None
        assert "--exclude-newer" not in self._uv_command(mock_run)

    @patch("claude_swap.update_check.subprocess.run")
    def test_nothing_outdated_is_quiet_and_cached(self, mock_run, tmp_path):
        mock_run.return_value = _uv_pip_list(
            [{"name": "platformdirs", "latest_version": "4.13.5"}]
        )

        assert check_for_update("0.27.2") is None
        cache = json.loads((tmp_path / "cache.json").read_text())
        assert cache["data"] == "0.27.2"

    @patch("claude_swap.update_check.urllib.request.urlopen")
    @patch("claude_swap.update_check.subprocess.run", side_effect=FileNotFoundError)
    def test_uv_missing_without_cooldown_asks_pypi(self, mock_run, mock_urlopen, tmp_path):
        # `cswap auto --once` under cron or launchd runs without uv on PATH.
        # Nothing hides releases from this install, so PyPI's answer is the
        # one uv would have given, and it is cached like any other.
        mock_urlopen.return_value = _make_pypi_response("0.28.0")

        result = check_for_update("0.27.2")

        assert result is not None
        assert "(0.28.0)" in result
        mock_urlopen.assert_called_once()
        cache = json.loads((tmp_path / "cache.json").read_text())
        assert cache["data"] == "0.28.0"

    @patch("claude_swap.update_check.subprocess.run", side_effect=FileNotFoundError)
    def test_uv_missing_with_cooldown_is_quiet_and_retried(self, mock_run, tmp_path):
        # PyPI would advertise the release the cooldown hides, so the run
        # stays quiet. Caching that for a day would silence the interactive
        # runs that follow, which do have uv, so nothing is written.
        self._write_receipt(tmp_path, 'exclude-newer-span = "P14D"')

        assert check_for_update("0.27.2") is None
        assert not (tmp_path / "cache.json").exists()

        mock_run.side_effect = None
        mock_run.return_value = _uv_pip_list(_OUTDATED)
        assert check_for_update("0.27.2") is not None

    @patch("claude_swap.update_check.subprocess.run")
    def test_uv_network_failure_with_cooldown_backs_off_for_an_hour(self, mock_run, tmp_path):
        # Only a missing uv retries immediately. A timeout or a failed
        # resolve under a cooldown would otherwise cost every following
        # command up to the 10 s subprocess timeout.
        self._write_receipt(tmp_path, 'exclude-newer-span = "P14D"')
        mock_run.side_effect = subprocess.TimeoutExpired("uv", 10)

        assert check_for_update("0.27.2") is None
        assert check_for_update("0.27.2") is None
        assert mock_run.call_count == 1
        cache = json.loads((tmp_path / "cache.json").read_text())
        assert cache["data"] is None

        _write_cache(tmp_path / "cache.json", None, timestamp=time.time() - FAILURE_TTL - 1)
        mock_run.side_effect = None
        mock_run.return_value = _uv_pip_list(_OUTDATED)
        assert check_for_update("0.27.2") is not None
        assert mock_run.call_count == 2

    @patch("claude_swap.update_check.urllib.request.urlopen")
    @patch("claude_swap.update_check.subprocess.run")
    def test_uv_failure_without_cooldown_asks_pypi(self, mock_run, mock_urlopen, tmp_path):
        mock_run.side_effect = subprocess.CalledProcessError(2, "uv")
        mock_urlopen.return_value = _make_pypi_response("0.28.0")

        assert check_for_update("0.27.2") is not None

    @patch("claude_swap.update_check.subprocess.run")
    def test_uv_and_pypi_both_failing_is_quiet_and_cached(self, mock_run, tmp_path):
        mock_run.side_effect = subprocess.CalledProcessError(2, "uv")

        assert check_for_update("0.27.2") is None
        cache = json.loads((tmp_path / "cache.json").read_text())
        assert cache["data"] is None

    @patch("claude_swap.update_check.subprocess.run")
    def test_fresh_cache_skips_uv(self, mock_run, tmp_path):
        _write_cache(tmp_path / "cache.json", "0.28.0")

        result = check_for_update("0.27.2")

        assert result is not None
        mock_run.assert_not_called()

    @patch("claude_swap.update_check.subprocess.run")
    def test_cache_in_an_unknown_shape_is_refetched(self, mock_run, tmp_path):
        _write_cache(tmp_path / "cache.json", {"latest": "0.28.0"})
        mock_run.return_value = _uv_pip_list(_OUTDATED)

        assert check_for_update("0.27.2") is not None
        mock_run.assert_called_once()

    @patch("claude_swap.update_check.subprocess.run")
    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_pipx_install_asks_pypi(self, mock_urlopen, mock_run, monkeypatch):
        monkeypatch.setattr("claude_swap.update_check._detect_install_method", lambda: "pipx")
        mock_urlopen.return_value = _make_pypi_response("0.28.0")

        assert check_for_update("0.27.2") is not None
        mock_run.assert_not_called()


class TestHeldBackReleaseNotice(_UvInstall):
    """`uv tool upgrade` prints "Nothing to upgrade" both when the install is
    current and when the newest release is younger than the cooldown, so
    `cswap upgrade` asks uv what it would install without the cooldown and
    explains the second case itself."""

    @patch("claude_swap.update_check.subprocess.run")
    def test_held_back_release_is_explained(self, mock_run, tmp_path):
        self._write_receipt(tmp_path, 'exclude-newer-span = "P14D"')
        mock_run.return_value = _uv_pip_list(_OUTDATED)

        notice = held_back_release_notice("0.27.2")

        assert notice is not None
        assert "claude-swap 0.28.0 is on PyPI" in notice
        assert 'uv-receipt.toml: exclude-newer-span = "P14D"' in notice
        assert 'To take it now:          uv tool upgrade claude-swap --exclude-newer "0 days"' in notice
        assert "To restore the cooldown: uv tool upgrade claude-swap --exclude-newer P14D" in notice
        assert "uv tool install" not in notice
        assert "--exclude-newer" not in self._uv_command(mock_run)

    @patch("claude_swap.update_check.subprocess.run")
    def test_absolute_cutoff_is_restored_verbatim(self, mock_run, tmp_path):
        self._write_receipt(tmp_path, 'exclude-newer = "2026-01-01T00:00:00Z"')
        mock_run.return_value = _uv_pip_list(_OUTDATED)

        notice = held_back_release_notice("0.27.2")

        assert notice is not None
        assert "--exclude-newer 2026-01-01T00:00:00Z" in notice

    @patch("claude_swap.update_check.subprocess.run")
    def test_receipt_value_with_shell_characters_gets_no_restore_line(self, mock_run, tmp_path):
        # uv validates what it writes, but the value is pasted into a shell
        # unquoted, so anything outside a duration or timestamp stays out.
        self._write_receipt(tmp_path, 'exclude-newer-span = "P14D; echo $HOME"')
        mock_run.return_value = _uv_pip_list(_OUTDATED)

        notice = held_back_release_notice("0.27.2")

        assert notice is not None
        assert "To take it now:" in notice
        assert "To restore the cooldown" not in notice
        assert "echo" not in notice.split("To take it now")[1]

    @patch("claude_swap.update_check.subprocess.run")
    def test_env_var_source_is_overridden_by_the_flag(self, mock_run, tmp_path, monkeypatch):
        # The command line flag wins over UV_EXCLUDE_NEWER for that one run,
        # and the variable keeps applying to later upgrades, so there is
        # nothing to unset and nothing to restore.
        monkeypatch.setenv("UV_EXCLUDE_NEWER", "1 week")
        mock_run.return_value = _uv_pip_list(_OUTDATED)

        notice = held_back_release_notice("0.27.2")

        assert notice is not None
        assert "UV_EXCLUDE_NEWER=1 week" in notice
        assert 'uv tool upgrade claude-swap --exclude-newer "0 days"' in notice
        assert "overrides UV_EXCLUDE_NEWER" in notice
        assert "unset" not in notice
        assert "To restore the cooldown" not in notice

    @patch("claude_swap.update_check.subprocess.run")
    def test_no_cooldown_means_no_notice_and_no_uv_call(self, mock_run):
        assert held_back_release_notice("0.27.2") is None
        mock_run.assert_not_called()

    @patch("claude_swap.update_check.subprocess.run")
    def test_nothing_newer_is_quiet(self, mock_run, tmp_path):
        self._write_receipt(tmp_path, 'exclude-newer-span = "P14D"')
        mock_run.return_value = _uv_pip_list([])

        assert held_back_release_notice("0.27.2") is None

    @patch("claude_swap.update_check.subprocess.run")
    def test_final_release_is_not_told_about_a_prerelease(self, mock_run, tmp_path):
        self._write_receipt(tmp_path, 'exclude-newer-span = "P14D"')
        mock_run.return_value = _uv_pip_list(
            [{"name": "claude-swap", "latest_version": "0.28.0b1"}]
        )

        assert held_back_release_notice("0.27.2") is None

    @patch("claude_swap.update_check.subprocess.run", side_effect=FileNotFoundError)
    def test_uv_failure_is_quiet(self, mock_run, tmp_path):
        self._write_receipt(tmp_path, 'exclude-newer-span = "P14D"')

        assert held_back_release_notice("0.27.2") is None

    @patch("claude_swap.update_check.subprocess.run")
    @patch("claude_swap.update_check._installed_version_on_disk", return_value="0.28.0")
    def test_cswap_upgrade_prints_notice_after_uv(
        self, mock_on_disk, mock_run, tmp_path, capsys
    ):
        self._write_receipt(tmp_path, 'exclude-newer-span = "P14D"')
        mock_run.side_effect = [MagicMock(returncode=0), _uv_pip_list(_OUTDATED)]

        assert run_self_upgrade("0.27.2") == 0

        out = capsys.readouterr().out
        # The before -> after report is printed ahead of the held-back notice.
        assert "claude-swap upgraded: 0.27.2 -> 0.28.0" in out
        assert "claude-swap 0.28.0 is on PyPI" in out
        assert out.index("upgraded:") < out.index("is on PyPI")
        assert 'uv tool upgrade claude-swap --exclude-newer "0 days"' in out
        upgrade, listing = (c.args[0] for c in mock_run.call_args_list)
        assert upgrade == ["uv", "tool", "upgrade", "claude-swap"]
        assert listing[:4] == ["uv", "pip", "list", "--outdated"]

    @patch("claude_swap.update_check.subprocess.run")
    def test_failed_uv_upgrade_gets_no_notice(self, mock_run, tmp_path, capsys):
        self._write_receipt(tmp_path, 'exclude-newer-span = "P14D"')
        mock_run.return_value = MagicMock(returncode=2)

        assert run_self_upgrade("0.27.2") == 2

        assert "is on PyPI" not in capsys.readouterr().out
        mock_run.assert_called_once()

    @patch("claude_swap.update_check.subprocess.run")
    @patch("claude_swap.update_check._installed_version_on_disk", return_value=None)
    def test_pipx_install_gets_no_notice(
        self, mock_on_disk, mock_run, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setattr("claude_swap.update_check._detect_install_method", lambda: "pipx")
        self._write_receipt(tmp_path, 'exclude-newer-span = "P14D"')
        mock_run.return_value = MagicMock(returncode=0)

        assert run_self_upgrade("0.27.2") == 0

        assert "is on PyPI" not in capsys.readouterr().out
        mock_run.assert_called_once()

    @patch("claude_swap.update_check.subprocess.run")
    def test_windows_prints_notice_with_the_command(
        self, mock_run, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setattr("claude_swap.update_check.sys.platform", "win32")
        self._write_receipt(tmp_path, 'exclude-newer-span = "P14D"')
        mock_run.return_value = _uv_pip_list(_OUTDATED)

        assert run_self_upgrade("0.27.2") == 1

        out = capsys.readouterr().out
        assert "uv tool upgrade claude-swap" in out
        assert "is on PyPI" in out
        assert self._uv_command(mock_run)[:4] == ["uv", "pip", "list", "--outdated"]


@patch("claude_swap.update_check.sys.platform", "linux")
class TestRunSelfUpgrade:
    @patch("claude_swap.update_check.subprocess.run")
    @patch("claude_swap.update_check._installed_version_on_disk", return_value=None)
    @patch("claude_swap.update_check._detect_install_method", return_value="uv")
    def test_uv_invokes_uv_tool_upgrade(self, mock_detect, mock_on_disk, mock_run):
        mock_run.return_value = MagicMock(returncode=0)

        assert run_self_upgrade() == 0
        mock_run.assert_called_once_with(
            ["uv", "tool", "upgrade", "claude-swap"], check=False
        )

    @patch("claude_swap.update_check.subprocess.run")
    @patch("claude_swap.update_check._installed_version_on_disk", return_value=None)
    @patch("claude_swap.update_check._detect_install_method", return_value="pipx")
    def test_pipx_invokes_pipx_upgrade(self, mock_detect, mock_on_disk, mock_run):
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


@patch("claude_swap.update_check.sys.platform", "linux")
class TestRunSelfUpgradeReport:
    """After a successful upgrade, report what actually landed on disk so the
    user is not misled by a package manager that exits 0 without changing
    anything (#491). Verification is best effort: it never masks the
    upgrade's own exit status."""

    @patch("claude_swap.update_check.subprocess.run")
    @patch("claude_swap.update_check._detect_install_method", return_value="uv")
    def test_reports_successful_upgrade(self, mock_detect, mock_run, capsys):
        mock_run.return_value = MagicMock(returncode=0)
        with patch(
            "claude_swap.update_check._installed_version_on_disk", return_value="0.28.0"
        ):
            assert run_self_upgrade("0.27.2") == 0
        assert "claude-swap upgraded: 0.27.2 -> 0.28.0" in capsys.readouterr().out

    @patch("claude_swap.update_check.subprocess.run")
    @patch("claude_swap.update_check._detect_install_method", return_value="uv")
    def test_reports_unchanged_upgrade(self, mock_detect, mock_run, capsys):
        mock_run.return_value = MagicMock(returncode=0)
        with patch(
            "claude_swap.update_check._installed_version_on_disk", return_value="0.27.2"
        ):
            assert run_self_upgrade("0.27.2") == 0
        assert "claude-swap is still at 0.27.2" in capsys.readouterr().out

    @patch("claude_swap.update_check.subprocess.run")
    @patch("claude_swap.update_check._detect_install_method", return_value="uv")
    def test_no_result_line_when_verification_fails(self, mock_detect, mock_run, capsys):
        mock_run.return_value = MagicMock(returncode=0)
        with patch(
            "claude_swap.update_check._installed_version_on_disk", return_value=None
        ):
            assert run_self_upgrade("0.27.2") == 0
        out = capsys.readouterr().out
        assert "upgraded:" not in out
        assert "is still at" not in out

    @patch("claude_swap.update_check.subprocess.run")
    @patch("claude_swap.update_check._detect_install_method", return_value="uv")
    def test_no_result_line_when_verification_times_out(self, mock_detect, mock_run, capsys):
        # The verification subprocess.run times out; _installed_version_on_disk
        # catches subprocess.SubprocessError and returns None, so we stay
        # silent and keep the upgrade's exit status.
        mock_run.side_effect = [
            MagicMock(returncode=0),
            subprocess.TimeoutExpired("py", 10),
            MagicMock(returncode=0, stdout="[]"),
        ]
        assert run_self_upgrade("0.27.2") == 0
        out = capsys.readouterr().out
        assert "upgraded:" not in out
        assert "is still at" not in out

    @patch("claude_swap.update_check.subprocess.run")
    @patch("claude_swap.update_check._detect_install_method", return_value="uv")
    def test_failed_upgrade_reports_nothing(self, mock_detect, mock_run, capsys):
        mock_run.return_value = MagicMock(returncode=2)
        assert run_self_upgrade("0.27.2") == 2
        out = capsys.readouterr().out
        assert "upgraded:" not in out
        assert "is still at" not in out


@patch("claude_swap.update_check.sys.platform", "win32")
class TestRunSelfUpgradeWindows:
    """On Windows the running .exe is locked, so we never upgrade in place --
    we print the command for the user to run themselves and exit 1."""

    @patch("claude_swap.update_check.subprocess.run")
    @patch("claude_swap.update_check._detect_install_method", return_value="uv")
    def test_uv_prints_command_and_does_not_run(self, mock_detect, mock_run, capsys):
        assert run_self_upgrade() == 1
        mock_run.assert_not_called()
        out = capsys.readouterr().out
        assert "uv tool upgrade claude-swap" in out

    @patch("claude_swap.update_check.subprocess.run")
    @patch("claude_swap.update_check._detect_install_method", return_value="pipx")
    def test_pipx_prints_command_and_does_not_run(self, mock_detect, mock_run, capsys):
        assert run_self_upgrade() == 1
        mock_run.assert_not_called()
        out = capsys.readouterr().out
        assert "pipx upgrade claude-swap" in out

    @patch("claude_swap.update_check.subprocess.run")
    @patch("claude_swap.update_check._detect_install_method", return_value=None)
    def test_unknown_method_hits_generic_fallback(self, mock_detect, mock_run, capsys):
        assert run_self_upgrade() == 1
        mock_run.assert_not_called()
        err = capsys.readouterr().err
        assert "uv tool upgrade claude-swap" in err
        assert "pipx upgrade claude-swap" in err
        assert "pip install --upgrade claude-swap" in err
