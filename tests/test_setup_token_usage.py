"""Usage for setup-token accounts via a one-turn Claude Code probe."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from claude_swap import oauth
from claude_swap.session import AUTH_OVERRIDE_ENV_VARS

SAMPLE_EVENT = (
    '{"type":"rate_limit_event","rate_limit_info":{"status":"rejected",'
    '"resetsAt":1788528600,"rateLimitType":"five_hour","overageStatus":"rejected",'
    '"overageDisabledReason":"org_level_disabled","isUsingOverage":false,'
    '"unifiedWindows":{"five_hour":{"utilization":1,"resetsAt":1788528600},'
    '"seven_day":{"utilization":0.36,"resetsAt":1788541200},'
    '"seven_day_overage_included":{"utilization":0.1,"resetsAt":1788541200}}},'
    '"uuid":"u","session_id":"s"}'
)
SYSTEM_LINE = '{"type":"system","subtype":"init","session_id":"s"}'
RESULT_ERROR_LINE = '{"type":"result","is_error":true,"result":"rate limited"}'


def setup_token_creds(**extra) -> str:
    return json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat01-x", "scopes": ["user:inference"], **extra,
    }})


def full_scope_creds() -> str:
    return json.dumps({"claudeAiOauth": {
        "accessToken": "at", "refreshToken": "rt", "expiresAt": 4102444800000,
        "scopes": ["user:profile", "user:inference", "user:sessions:claude_code"],
    }})


def completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["claude"], returncode=returncode, stdout=stdout, stderr="",
    )


@pytest.fixture
def profile(tmp_path: Path) -> oauth.UsageProbeProfile:
    session_dir = tmp_path / "sessions" / "3-x"
    session_dir.mkdir(parents=True)
    return oauth.UsageProbeProfile(config_dir=session_dir, scratch_root=session_dir)


@pytest.fixture
def spawn(monkeypatch):
    """Record the probe's spawn call; ``spawn.stdout``/``spawn.returncode``
    shape the answer, ``spawn.raises`` makes it raise instead."""
    class Recorder:
        calls: list[dict] = []
        stdout = SAMPLE_EVENT + "\n"
        returncode = 0
        raises: BaseException | None = None

        def __call__(self, argv, env, cwd, timeout_s):
            self.calls.append({"argv": argv, "env": env, "cwd": cwd, "timeout_s": timeout_s})
            if self.raises is not None:
                raise self.raises
            return completed(self.stdout, self.returncode)

    rec = Recorder()
    monkeypatch.setattr(oauth, "_spawn_usage_probe", rec)
    monkeypatch.setattr(oauth.shutil, "which", lambda name: "/usr/local/bin/claude")
    return rec


class TestDetection:
    def test_exact_inference_scope_without_refresh_token(self):
        assert oauth.is_setup_token_credential(setup_token_creds())

    def test_refresh_token_present_is_not_a_setup_token(self):
        assert not oauth.is_setup_token_credential(setup_token_creds(refreshToken="rt"))

    def test_full_scopes_are_not_a_setup_token(self):
        assert not oauth.is_setup_token_credential(full_scope_creds())

    def test_superset_or_missing_scopes_are_not(self):
        assert not oauth.is_setup_token_credential(json.dumps({"claudeAiOauth": {
            "accessToken": "a", "scopes": ["user:inference", "user:profile"],
        }}))
        assert not oauth.is_setup_token_credential(json.dumps({"claudeAiOauth": {
            "accessToken": "a",
        }}))

    def test_garbage_is_not(self):
        assert not oauth.is_setup_token_credential("not json")
        assert not oauth.is_setup_token_credential("")


class TestParseRateLimitEvent:
    def test_sample_line_maps_to_usage_shape(self):
        data = oauth.parse_rate_limit_event(SYSTEM_LINE + "\n" + SAMPLE_EVENT + "\n")
        assert data["five_hour"]["utilization"] == 100.0
        assert data["seven_day"]["utilization"] == pytest.approx(36.0)
        assert data["five_hour"]["resets_at"] == datetime.fromtimestamp(
            1788528600, tz=timezone.utc
        ).isoformat()
        assert "seven_day_overage_included" not in data

    def test_builds_usage_result(self):
        data = oauth.parse_rate_limit_event(SAMPLE_EVENT)
        result = oauth.build_usage_result(data)
        assert result["five_hour"]["pct"] == 100.0
        assert result["seven_day"]["pct"] == pytest.approx(36.0)
        assert "countdown" in result["five_hour"] and "clock" in result["five_hour"]
        assert oauth.account_headroom(result) == 0.0

    def test_non_json_lines_are_skipped(self):
        stdout = "warning: something\n{not json\n" + SAMPLE_EVENT + "\n[1,2]\n"
        assert oauth.parse_rate_limit_event(stdout)["five_hour"]["utilization"] == 100.0

    def test_no_event_is_none(self):
        assert oauth.parse_rate_limit_event(SYSTEM_LINE + "\n" + RESULT_ERROR_LINE) is None
        assert oauth.parse_rate_limit_event("") is None

    def test_missing_reset_is_tolerated(self):
        line = json.dumps({"type": "rate_limit_event", "rate_limit_info": {
            "unifiedWindows": {"five_hour": {"utilization": 0.5}},
        }})
        data = oauth.parse_rate_limit_event(line)
        assert data == {"five_hour": {"utilization": 50.0, "resets_at": None}}
        assert oauth.build_usage_result(data)["five_hour"]["pct"] == 50.0


class TestProbe:
    def test_healthy_account_yields_usage(self, spawn, profile):
        spawn.stdout = SYSTEM_LINE + "\n" + SAMPLE_EVENT + '\n{"type":"result","is_error":false}\n'
        outcome = oauth.probe_usage_via_claude("3", profile)
        assert outcome.error is None
        assert outcome.usage["seven_day"]["pct"] == pytest.approx(36.0)

    def test_exhausted_account_nonzero_exit_still_yields_usage(self, spawn, profile):
        spawn.returncode = 1
        spawn.stdout = SAMPLE_EVENT + "\n" + RESULT_ERROR_LINE + "\n"
        outcome = oauth.probe_usage_via_claude("3", profile)
        assert outcome.error is None
        assert outcome.usage["five_hour"]["pct"] == 100.0

    def test_missing_binary(self, spawn, profile, monkeypatch):
        monkeypatch.setattr(oauth.shutil, "which", lambda name: None)
        outcome = oauth.probe_usage_via_claude("3", profile)
        assert outcome == oauth.UsageOutcome(None, error="no-claude-binary")
        assert spawn.calls == []

    def test_spawn_oserror_is_no_binary(self, spawn, profile):
        spawn.raises = FileNotFoundError("claude")
        assert oauth.probe_usage_via_claude("3", profile).error == "no-claude-binary"

    def test_timeout(self, spawn, profile):
        spawn.raises = subprocess.TimeoutExpired(cmd="claude", timeout=90)
        assert oauth.probe_usage_via_claude("3", profile).error == "probe-timeout"

    def test_no_event(self, spawn, profile):
        spawn.returncode = 1
        spawn.stdout = SYSTEM_LINE + "\n" + RESULT_ERROR_LINE + "\n"
        assert oauth.probe_usage_via_claude("3", profile).error == "no-rate-limit-event"

    def test_env_cwd_and_argv(self, spawn, profile, monkeypatch):
        for var in AUTH_OVERRIDE_ENV_VARS:
            monkeypatch.setenv(var, "leak")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/elsewhere")
        oauth.probe_usage_via_claude("3", profile)
        call = spawn.calls[0]
        assert call["env"]["CLAUDE_CONFIG_DIR"] == str(profile.config_dir)
        assert not any(var in call["env"] for var in AUTH_OVERRIDE_ENV_VARS)
        assert call["timeout_s"] == oauth.USAGE_PROBE_TIMEOUT_S == 90.0
        scratch = profile.scratch_root / oauth.USAGE_PROBE_DIRNAME
        assert call["cwd"] == scratch and scratch.is_dir()
        argv = call["argv"]
        assert argv[0] == "/usr/local/bin/claude"
        assert argv[1:3] == ["-p", "ok"]
        assert "stream-json" in argv and "--verbose" in argv
        assert argv[argv.index("--model") + 1] == oauth.USAGE_PROBE_MODEL
        assert argv[argv.index("--max-turns") + 1] == "1"
        assert argv[argv.index("--tools") + 1] == ""

    def test_default_profile_leaves_config_dir_untouched(self, spawn, tmp_path, monkeypatch):
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        default = oauth.UsageProbeProfile(config_dir=None, scratch_root=tmp_path)
        oauth.probe_usage_via_claude("1", default)
        assert "CLAUDE_CONFIG_DIR" not in spawn.calls[0]["env"]

    def test_logs_one_info_line_without_email(self, spawn, profile, caplog):
        import logging
        with caplog.at_level(logging.INFO, logger="claude-swap"):
            oauth.probe_usage_via_claude("3", profile)
        lines = [r.getMessage() for r in caplog.records if "Usage via Claude Code turn" in r.getMessage()]
        assert lines == ["Usage via Claude Code turn for account 3"]


class TestFetchEntryPoint:
    def test_setup_token_routes_to_probe(self, spawn, profile):
        outcome = oauth.try_fetch_usage_for_account(
            "3", "setup-token-3@token.local", setup_token_creds(), is_active=False,
            probe_profile_for=lambda num, email: profile,
        )
        assert outcome.error is None
        assert outcome.usage["five_hour"]["pct"] == 100.0
        assert len(spawn.calls) == 1

    def test_setup_token_without_profile(self, spawn, monkeypatch):
        seen = []
        outcome = oauth.try_fetch_usage_for_account(
            "3", "e", setup_token_creds(), is_active=False,
            probe_profile_for=lambda num, email: seen.append((num, email)),
        )
        assert outcome.error == "no-session-profile"
        assert seen == [("3", "e")]
        assert spawn.calls == []

    def test_setup_token_without_callable(self, spawn):
        outcome = oauth.try_fetch_usage_for_account("3", "e", setup_token_creds(), is_active=True)
        assert outcome.error == "no-session-profile"
        assert spawn.calls == []

    def test_full_scope_never_spawns(self, spawn, profile, monkeypatch):
        def fake_request(token):
            return {"five_hour": {"utilization": 5.0, "resets_at": None}}
        monkeypatch.setattr(oauth, "request_usage_data", fake_request)
        outcome = oauth.try_fetch_usage_for_account(
            "1", "e", full_scope_creds(), is_active=True,
            probe_profile_for=lambda num, email: profile,
        )
        assert outcome.usage["five_hour"]["pct"] == 5.0
        assert spawn.calls == []

    def test_setup_token_never_hits_usage_endpoint(self, spawn, profile, monkeypatch):
        def boom(token):
            raise AssertionError("usage endpoint must not be called for setup-tokens")
        monkeypatch.setattr(oauth, "request_usage_data", boom)
        oauth.try_fetch_usage_for_account(
            "3", "e", setup_token_creds(), is_active=False,
            probe_profile_for=lambda num, email: profile,
        )

    def test_probe_errors_are_not_permanent_auth_errors(self):
        from claude_swap.usage_store import PERMANENT_AUTH_ERRORS
        for kind in ("no-claude-binary", "probe-timeout", "no-rate-limit-event", "no-session-profile"):
            assert kind not in PERMANENT_AUTH_ERRORS

    def test_error_notes_cover_probe_kinds(self):
        from claude_swap.switcher import ERROR_NOTES
        for kind in ("no-claude-binary", "probe-timeout", "no-rate-limit-event", "no-session-profile"):
            assert kind in ERROR_NOTES


class TestSwitcherProfiles:
    def test_session_profile_only_when_bootstrapped(self, temp_home, monkeypatch):
        from claude_swap.switcher import ClaudeAccountSwitcher
        sw = ClaudeAccountSwitcher()
        assert sw._usage_probe_profile_session("3", "x@y") is None
        d = sw._session_dir("3", "x@y")
        d.mkdir(parents=True)
        prof = sw._usage_probe_profile_session("3", "x@y")
        assert prof == oauth.UsageProbeProfile(config_dir=d, scratch_root=d)

    def test_default_profile_uses_claude_home_without_config_dir(self, temp_home):
        from claude_swap.paths import get_claude_config_home
        from claude_swap.switcher import ClaudeAccountSwitcher
        prof = ClaudeAccountSwitcher()._usage_probe_profile_default("1", "x@y")
        assert prof.config_dir is None
        assert prof.scratch_root == get_claude_config_home()
