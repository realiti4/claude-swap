"""Usage from Claude Code's status line: the `cswap-statusline` hook, the
collector that takes its readings, and `cswap statusline install`."""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import paths
from claude_swap import statusline as sl
from claude_swap.switcher import ClaudeAccountSwitcher

TOKEN_1 = "sk-ant-oat01-" + "1" * 30
TOKEN_2 = "sk-ant-oat01-" + "2" * 30


@pytest.fixture(autouse=True)
def _no_ambient_token(monkeypatch):
    """The test runner may itself run under Claude Code with these set."""
    for name in ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR", "XDG_DATA_HOME"):
        monkeypatch.delenv(name, raising=False)


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).isoformat().replace("+00:00", "Z")


def write_transcript(path: Path, *, request_at: float, response_at: float, extra: list | None = None) -> Path:
    """A prompt and a two-block response to it."""
    lines = [
        {"type": "user", "uuid": "u-old", "timestamp": iso(request_at - 600), "isSidechain": False},
        {"type": "assistant", "uuid": "a-old", "parentUuid": "u-old", "requestId": "req_old",
         "timestamp": iso(request_at - 590), "isSidechain": False},
        {"type": "user", "uuid": "u1", "parentUuid": "a-old", "timestamp": iso(request_at),
         "isSidechain": False},
        {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "requestId": "req_1",
         "timestamp": iso(response_at - 3), "isSidechain": False},
        {"type": "assistant", "uuid": "a2", "parentUuid": "a1", "requestId": "req_1",
         "timestamp": iso(response_at), "isSidechain": False},
        *(extra or []),
    ]
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    return path


def payload(transcript: Path | None, five: float = 25, seven: float = 61.4, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    out: dict = {"session_id": "s", "rate_limits": {
        "five_hour": {"used_percentage": five, "resets_at": now + 7200},
        "seven_day": {"used_percentage": seven, "resets_at": now + 300_000},
    }}
    if transcript is not None:
        out["transcript_path"] = str(transcript)
    return out


@pytest.fixture
def data_root(temp_home) -> Path:
    root = sl.backup_root()
    root.mkdir(parents=True)
    return root


def write_live_token(home: Path, token: str) -> Path:
    path = home / ".claude" / ".credentials.json"
    path.write_text(json.dumps({"claudeAiOauth": {"accessToken": token}}))
    return path


# ---------------------------------------------------------------------------
# locations: the hook mirrors claude_swap.paths without importing it
# ---------------------------------------------------------------------------


class TestLocations:
    @pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
    @pytest.mark.parametrize("xdg", [None, "/srv/xdg", "relative/xdg", "~/xdg"])
    def test_backup_root_matches_paths(self, temp_home, monkeypatch, platform, xdg):
        monkeypatch.setattr(sys, "platform", platform)
        if xdg is not None:
            monkeypatch.setenv("XDG_DATA_HOME", xdg)
        with patch("claude_swap.paths.Platform.detect", side_effect=lambda: _platform_for(platform)):
            assert sl.backup_root() == paths.get_backup_root()

    @pytest.mark.parametrize("config_dir", [None, "elsewhere"])
    @pytest.mark.parametrize("legacy", [None, "", "{}"])
    def test_config_paths_match_paths(self, temp_home, monkeypatch, config_dir, legacy):
        if config_dir:
            target = temp_home / config_dir
            target.mkdir()
            monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(target))
        if legacy is not None:
            paths.get_claude_config_home().mkdir(exist_ok=True)
            (paths.get_claude_config_home() / ".config.json").write_text(legacy)
        assert sl.config_home() == paths.get_claude_config_home()
        assert sl.global_config_path() == paths.get_global_config_path()


def _platform_for(name: str):
    from claude_swap.models import Platform

    return {"linux": Platform.LINUX, "darwin": Platform.MACOS, "win32": Platform.WINDOWS}[name]


# ---------------------------------------------------------------------------
# the payload and the transcript
# ---------------------------------------------------------------------------


class TestPayload:
    def test_rate_windows(self):
        assert sl.rate_windows(payload(None, now=1000.0)) == {
            "five_hour": {"used_percentage": 25.0, "resets_at": 8200.0},
            "seven_day": {"used_percentage": 61.4, "resets_at": 301_000.0},
        }

    @pytest.mark.parametrize("bad", [
        None, [], {}, {"rate_limits": None}, {"rate_limits": {"five_hour": "x"}},
        {"rate_limits": {"five_hour": {"used_percentage": "25"}}},
        {"rate_limits": {"five_hour": {"used_percentage": True}}},
        {"rate_limits": {"spend_limit": {"used_percentage": 5}}},
    ])
    def test_nothing_usable(self, bad):
        assert sl.rate_windows(bad) == {}

    def test_missing_reset_is_kept_as_none(self):
        got = sl.rate_windows({"rate_limits": {"seven_day": {"used_percentage": 3}}})
        assert got == {"seven_day": {"used_percentage": 3.0, "resets_at": None}}


class TestTranscript:
    def test_request_and_response_times(self, tmp_path):
        t = write_transcript(tmp_path / "t.jsonl", request_at=1_000_000.0, response_at=1_000_030.0)
        assert sl.last_exchange(str(t)) == (1_000_000.0, 1_000_030.0)

    def test_sidechain_and_other_entries_are_skipped(self, tmp_path):
        extra = [
            {"type": "assistant", "uuid": "side", "parentUuid": "x", "requestId": "req_side",
             "timestamp": iso(1_000_100.0), "isSidechain": True},
            {"type": "system", "uuid": "sys", "timestamp": iso(1_000_200.0),
             "content": 'mentions "assistant" in passing'},
        ]
        t = write_transcript(tmp_path / "t.jsonl", request_at=1_000_000.0,
                             response_at=1_000_030.0, extra=extra)
        assert sl.last_exchange(str(t)) == (1_000_000.0, 1_000_030.0)

    def test_parent_out_of_reach_leaves_request_unknown(self, tmp_path, monkeypatch):
        t = write_transcript(tmp_path / "t.jsonl", request_at=1_000_000.0, response_at=1_000_030.0)
        lines = t.read_bytes().split(b"\n")
        tail = len(b"\n".join(lines[3:]))  # cuts inside u1's line
        monkeypatch.setattr(sl, "TRANSCRIPT_TAIL_BYTES", tail + 5)
        assert sl.last_exchange(str(t)) == (None, 1_000_030.0)

    @pytest.mark.parametrize("value", [None, "", 3, "/no/such/file"])
    def test_no_transcript(self, value):
        assert sl.last_exchange(value) == (None, None)

    def test_no_response_yet(self, tmp_path):
        t = tmp_path / "t.jsonl"
        t.write_text(json.dumps({"type": "user", "uuid": "u", "timestamp": iso(5.0)}) + "\n")
        assert sl.last_exchange(str(t)) == (None, None)


# ---------------------------------------------------------------------------
# the hook's record
# ---------------------------------------------------------------------------


class TestObserve:
    def test_records_token_hash_and_times(self, temp_home, data_root, tmp_path):
        creds = write_live_token(temp_home, TOKEN_1)
        t = write_transcript(tmp_path / "t.jsonl", request_at=2_000_000.0, response_at=2_000_020.0)
        record = sl.observe(payload(t), now=2_000_021.0)
        saved = json.loads((data_root / "statusline" / "default.json").read_text())
        assert saved == record
        assert record["tokenSha256"] == sl.token_sha256(TOKEN_1)
        assert record["credentialChangedAt"] == creds.stat().st_mtime
        assert (record["requestAt"], record["responseAt"]) == (2_000_000.0, 2_000_020.0)
        assert record["profile"] is None and record["account"] is None
        assert record["rateLimits"]["five_hour"]["used_percentage"] == 25.0
        mode = (data_root / "statusline" / "default.json").stat().st_mode & 0o777
        assert mode == 0o600 or os.name == "nt"

    def test_the_token_secret_itself_is_never_written(self, temp_home, data_root, tmp_path):
        write_live_token(temp_home, TOKEN_1)
        t = write_transcript(tmp_path / "t.jsonl", request_at=2_000_000.0, response_at=2_000_020.0)
        sl.observe(payload(t))
        assert TOKEN_1 not in (data_root / "statusline" / "default.json").read_text()

    def test_env_token_wins_and_never_changes(self, temp_home, data_root, tmp_path, monkeypatch):
        write_live_token(temp_home, TOKEN_1)
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN_2)
        t = write_transcript(tmp_path / "t.jsonl", request_at=2_000_000.0, response_at=2_000_020.0)
        record = sl.observe(payload(t))
        assert record["tokenSha256"] == sl.token_sha256(TOKEN_2)
        assert record["credentialChangedAt"] is None

    def test_without_a_credential_file_records_the_live_account(self, temp_home, data_root, tmp_path):
        (temp_home / ".claude.json").write_text(json.dumps(
            {"oauthAccount": {"emailAddress": "me@example.com", "organizationUuid": "org-1"}}))
        (data_root / "sequence.json").write_text("{}")
        t = write_transcript(tmp_path / "t.jsonl", request_at=2_000_000.0, response_at=2_000_020.0)
        record = sl.observe(payload(t))
        assert record["tokenSha256"] is None
        assert record["account"] == {"email": "me@example.com", "organizationUuid": "org-1"}
        assert record["accountChangedAt"] == (data_root / "sequence.json").stat().st_mtime

    def test_another_profile_gets_its_own_file(self, temp_home, data_root, tmp_path, monkeypatch):
        profile = temp_home / "profile-a"
        profile.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(profile))
        (profile / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": TOKEN_2}}))
        t = write_transcript(tmp_path / "t.jsonl", request_at=2_000_000.0, response_at=2_000_020.0)
        record = sl.observe(payload(t))
        assert record["profile"] == str(profile.resolve())
        assert record["tokenSha256"] == sl.token_sha256(TOKEN_2)
        assert (data_root / "statusline" / sl.record_name(str(profile.resolve()))).is_file()
        assert not (data_root / "statusline" / "default.json").exists()

    @pytest.mark.parametrize("case", ["no-limits", "no-transcript", "no-data-folder"])
    def test_nothing_written(self, temp_home, tmp_path, case):
        t = write_transcript(tmp_path / "t.jsonl", request_at=2_000_000.0, response_at=2_000_020.0)
        if case != "no-data-folder":
            sl.backup_root().mkdir(parents=True)
        body = {
            "no-limits": {"transcript_path": str(t)},
            "no-transcript": payload(None),
            "no-data-folder": payload(t),
        }[case]
        assert sl.observe(body) is None
        assert not (sl.backup_root() / "statusline").exists()

    def test_a_re_render_with_nothing_new_is_not_rewritten(self, temp_home, data_root, tmp_path):
        write_live_token(temp_home, TOKEN_1)
        t = write_transcript(tmp_path / "t.jsonl", request_at=2_000_000.0, response_at=2_000_020.0)
        sl.observe(payload(t, now=5.0), now=100.0)
        sl.observe(payload(t, now=5.0), now=200.0)
        saved = json.loads((data_root / "statusline" / "default.json").read_text())
        assert saved["observedAt"] == 100.0
        sl.observe(payload(t, five=30, now=5.0), now=300.0)
        saved = json.loads((data_root / "statusline" / "default.json").read_text())
        assert saved["observedAt"] == 300.0 and saved["rateLimits"]["five_hour"]["used_percentage"] == 30


class TestShortLine:
    def test_alias_and_countdown(self, temp_home, data_root):
        (temp_home / ".claude.json").write_text(json.dumps(
            {"oauthAccount": {"emailAddress": "me@example.com", "organizationUuid": "o"}}))
        (data_root / "sequence.json").write_text(json.dumps({"accounts": {
            "1": {"email": "me@example.com", "organizationUuid": "o", "alias": "team"}}}))
        body = payload(None, five=25, seven=61.4, now=0.0)
        body["rate_limits"]["five_hour"]["resets_at"] = 7140
        assert sl.short_line(body, now=0.0) == "team · 5h 25% (1h 59m) · 7d 61%"

    def test_email_when_unmanaged_and_no_limits_yet(self, temp_home, data_root):
        (temp_home / ".claude.json").write_text(json.dumps(
            {"oauthAccount": {"emailAddress": "me@example.com"}}))
        assert sl.short_line({}) == "me@example.com"

    def test_nothing_known(self, temp_home):
        assert sl.short_line({}) == "Claude"

    @pytest.mark.parametrize(("seconds", "text"), [
        (59, "0m"), (61 * 60, "1h 1m"), (2 * 3600, "2h"), (26 * 3600, "1d 2h"), (48 * 3600, "2d"),
    ])
    def test_countdown(self, seconds, text):
        assert sl.countdown(seconds, 0.0) == text

    def test_countdown_past(self):
        assert sl.countdown(5.0, 10.0) is None and sl.countdown(None, 0.0) is None


# ---------------------------------------------------------------------------
# the hook as Claude Code runs it
# ---------------------------------------------------------------------------


def run_hook(home: Path, args: list[str], body: bytes) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_CONFIG_DIR", "XDG_DATA_HOME", "CLAUDE_CODE_OAUTH_TOKEN")}
    env.update(HOME=str(home), USERPROFILE=str(home))
    return subprocess.run(
        [sys.executable, "-m", "claude_swap.statusline", *args],
        input=body, capture_output=True, env=env, timeout=30, check=False,
    )


class TestHookProcess:
    def test_runs_the_wrapped_command_unchanged(self, temp_home, data_root, tmp_path):
        write_live_token(temp_home, TOKEN_1)
        t = write_transcript(tmp_path / "t.jsonl", request_at=2_000_000.0, response_at=2_000_020.0)
        body = json.dumps(payload(t)).encode()
        mine = "python3 -c \"import sys, json; d = json.load(sys.stdin); print('mine', d['session_id']); sys.exit(3)\""
        result = run_hook(temp_home, ["--then", mine], body)
        assert result.stdout.decode().strip() == "mine s"
        assert result.returncode == 3
        assert (data_root / "statusline" / "default.json").is_file()

    def test_a_command_that_ignores_its_input_still_works(self, temp_home, data_root, tmp_path):
        t = write_transcript(tmp_path / "t.jsonl", request_at=2_000_000.0, response_at=2_000_020.0)
        result = run_hook(temp_home, ["--then", "echo fixed"], json.dumps(payload(t)).encode())
        assert result.stdout.decode().strip() == "fixed" and result.returncode == 0

    def test_short_line_on_its_own(self, temp_home, data_root):
        result = run_hook(temp_home, [], json.dumps(payload(None, five=10, seven=20)).encode())
        assert result.returncode == 0
        assert result.stdout.decode().startswith("Claude · 5h 10% (") and "7d 20%" in result.stdout.decode()

    @pytest.mark.parametrize("body", [b"", b"not json", b"[1, 2]", b"\xff\xfe"])
    def test_garbage_in_never_breaks_it(self, temp_home, data_root, body):
        result = run_hook(temp_home, [], body)
        assert result.returncode == 0 and result.stdout.decode().strip() == "Claude"
        result = run_hook(temp_home, ["--then", "echo ok"], body)
        assert result.returncode == 0 and result.stdout.decode().strip() == "ok"

    def test_does_not_load_the_switcher(self):
        code = ("import sys, claude_swap.statusline; "
                "print(sorted(m for m in sys.modules if m.startswith('claude_swap')))")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
        assert out.stdout.strip() == "['claude_swap', 'claude_swap.statusline']"

    def test_package_attributes_still_resolve(self):
        import claude_swap

        assert claude_swap.ClaudeAccountSwitcher is ClaudeAccountSwitcher
        assert isinstance(claude_swap.__version__, str)
        with pytest.raises(AttributeError):
            claude_swap.nope  # noqa: B018


# ---------------------------------------------------------------------------
# matching a record to an account
# ---------------------------------------------------------------------------


def record(**fields) -> dict:
    base = {
        "schemaVersion": 1, "observedAt": 10_000.0, "requestAt": 9_000.0, "responseAt": 9_030.0,
        "profile": None, "tokenSha256": sl.token_sha256(TOKEN_1), "credentialChangedAt": 1_000.0,
        "account": None, "accountChangedAt": None,
        "rateLimits": {"five_hour": {"used_percentage": 40.0, "resets_at": 20_000.0},
                       "seven_day": {"used_percentage": 70.0, "resets_at": 500_000.0}},
    }
    base.update(fields)
    return base


MATCH = {
    "token_slots": {sl.token_sha256(TOKEN_1): ["1"], sl.token_sha256(TOKEN_2): ["2"]},
    "identity_slot": lambda email, org: {("one@example.com", "o"): "1"}.get((email, org)),
    "profile_slot": lambda path: {"/sessions/1-one": "1"}.get(path),
}


class TestAttribution:
    def test_by_token(self):
        assert sl.attribute(record(), **MATCH) == ["1"]

    def test_unknown_token_is_nobodys(self):
        assert sl.attribute(record(tokenSha256=sl.token_sha256("sk-other")), **MATCH) == []

    @pytest.mark.parametrize(("changed", "kept"), [
        (8_000.0, True),                     # well before the request
        (9_000.0 - sl.FILE_GRACE_S, True),   # just outside the grace
        (9_000.0 - 1, False),                # the request may still have used the old one
        (9_010.0, False),                    # switched while the response came in
    ])
    def test_credential_change_around_the_request(self, changed, kept):
        assert sl.attribute(record(credentialChangedAt=changed), **MATCH) == (["1"] if kept else [])

    def test_unknown_request_time_is_not_trusted(self):
        assert sl.attribute(record(requestAt=None), **MATCH) == []

    def test_by_live_account_with_keychain_grace(self):
        base = {"tokenSha256": None, "account": {"email": "one@example.com", "organizationUuid": "o"}}
        assert sl.attribute(record(**base, accountChangedAt=8_000.0), **MATCH) == ["1"]
        late = 9_000.0 - sl.KEYCHAIN_GRACE_S + 1
        assert sl.attribute(record(**base, accountChangedAt=late), **MATCH) == []
        other = {**base, "account": {"email": "two@example.com", "organizationUuid": "o"}}
        assert sl.attribute(record(**other), **MATCH) == []

    def test_by_session_profile(self):
        rec = record(tokenSha256=None, profile="/sessions/1-one",
                     account={"email": "whatever@example.com", "organizationUuid": ""})
        assert sl.attribute(rec, **MATCH) == ["1"]

    def test_readings_newest_per_eligible_slot(self):
        now = 10_000.0
        older = record(responseAt=8_000.0, requestAt=7_990.0)
        newer = record(responseAt=9_500.0, requestAt=9_490.0)
        two = record(tokenSha256=sl.token_sha256(TOKEN_2))
        got = sl.readings([older, newer, two], now=now, eligible={"1"}, **MATCH)
        assert list(got) == ["1"]
        usage, age = got["1"]
        assert age == 500.0
        assert usage["five_hour"]["pct"] == 40.0 and usage["seven_day"]["pct"] == 70.0
        assert usage["source"] == "headers"

    def test_too_old_reading_is_ignored(self):
        rec = record(responseAt=0.0, requestAt=-10.0, credentialChangedAt=None)
        assert sl.readings([rec], now=sl.MAX_READING_AGE_S + 1, eligible={"1"}, **MATCH) == {}

    def test_inbox_prunes_stale_files(self, tmp_path):
        folder = tmp_path / "statusline"
        folder.mkdir()
        (folder / "default.json").write_text(json.dumps(record(observedAt=100.0)))
        (folder / "profile-x.json").write_text(json.dumps(record(observedAt=100.0 + sl.PRUNE_AFTER_S)))
        (folder / "bad.json").write_text("{")
        (folder / ".tmp.json").write_text(json.dumps(record()))
        got = sl.read_inbox(tmp_path, now=101.0 + sl.PRUNE_AFTER_S)
        assert [r["observedAt"] for r in got] == [100.0 + sl.PRUNE_AFTER_S]
        assert not (folder / "default.json").exists()


# ---------------------------------------------------------------------------
# the collector
# ---------------------------------------------------------------------------


@pytest.fixture
def switcher(temp_home) -> ClaudeAccountSwitcher:
    s = ClaudeAccountSwitcher()
    s._setup_directories()
    with contextlib.redirect_stdout(io.StringIO()):
        s.add_account_from_token(TOKEN_1, email="one@example.com")
        s.add_account_from_token(TOKEN_2, email="two@example.com")
    return s


def save_record(s: ClaudeAccountSwitcher, rec: dict, name: str = "default.json") -> None:
    folder = s.backup_dir / "statusline"
    folder.mkdir(exist_ok=True)
    (folder / name).write_text(json.dumps(rec))


def collect(s: ClaudeAccountSwitcher) -> dict:
    with patch.object(s, "_live_session_pids", return_value=[]), \
         patch("claude_swap.oauth.urllib.request.urlopen") as urlopen:
        entries = s._collect_usage_entries(s._build_accounts_info())
    urlopen.assert_not_called()
    return entries


class TestCollector:
    def test_a_setup_token_reading_is_taken_without_a_request(self, switcher):
        now = time.time()
        save_record(switcher, record(
            observedAt=now, requestAt=now - 60, responseAt=now - 30, credentialChangedAt=now - 3600,
            tokenSha256=sl.token_sha256(TOKEN_2),
        ))
        entries = collect(switcher)
        assert entries["2"].last_good["five_hour"]["pct"] == 40.0
        assert 25 <= entries["2"].age_s <= 40
        assert entries["1"].last_good is None

    def test_a_reading_from_before_the_switch_is_dropped(self, switcher):
        now = time.time()
        save_record(switcher, record(
            observedAt=now, requestAt=now - 60, responseAt=now - 30, credentialChangedAt=now - 45,
            tokenSha256=sl.token_sha256(TOKEN_2),
        ))
        assert collect(switcher)["2"].last_good is None

    def test_a_refreshing_login_is_left_to_the_usage_endpoint(self, switcher):
        switcher._write_account_credentials("3", "me@example.com", json.dumps({"claudeAiOauth": {
            "accessToken": "sk-3", "refreshToken": "rt-3", "expiresAt": 99999999999000}}))
        data = switcher._get_sequence_data()
        data["accounts"]["3"] = {"email": "me@example.com", "uuid": "u3",
                                 "organizationUuid": "org", "organizationName": "Org"}
        data["sequence"].append(3)
        switcher._write_json(switcher.sequence_file, data)
        now = time.time()
        save_record(switcher, record(
            observedAt=now, requestAt=now - 60, responseAt=now - 30, credentialChangedAt=None,
            tokenSha256=sl.token_sha256("sk-3"),
        ))
        matchers = switcher._statusline_matchers(switcher._build_accounts_info())
        assert matchers["eligible"] == {"1", "2"}
        with patch.object(switcher._usage_store, "adopt") as adopt:
            switcher._ingest_statusline(switcher._build_accounts_info(), {})
        adopt.assert_not_called()

    def test_a_newer_stored_reading_is_kept(self, switcher):
        now = time.time()
        save_record(switcher, record(
            observedAt=now, requestAt=now - 600, responseAt=now - 500, credentialChangedAt=None,
            tokenSha256=sl.token_sha256(TOKEN_1),
        ))
        data = switcher._get_sequence_data()
        identities = {n: (a["email"], a.get("organizationUuid") or "") for n, a in data["accounts"].items()}
        switcher._usage_store.adopt(
            {"1": ({"five_hour": {"pct": 90.0, "resets_at": None}}, 10.0)}, {"1": identities["1"]})
        assert collect(switcher)["1"].last_good["five_hour"]["pct"] == 90.0

    def test_a_broken_inbox_never_stops_a_pass(self, switcher):
        with patch("claude_swap.statusline.read_inbox", side_effect=RuntimeError("boom")):
            entries = collect(switcher)
        assert set(entries) == {"1", "2"}

    def test_report(self, switcher):
        now = time.time()
        save_record(switcher, record(observedAt=now, requestAt=now - 60, responseAt=now - 30,
                                     credentialChangedAt=None, tokenSha256=sl.token_sha256(TOKEN_1)))
        save_record(switcher, record(observedAt=now, requestAt=now - 60, responseAt=now - 30,
                                     tokenSha256=sl.token_sha256("sk-x")), name="profile-a.json")
        report = sorted(switcher.statusline_report(), key=lambda r: r[0]["tokenSha256"] != sl.token_sha256(TOKEN_1))
        assert [slots for _rec, slots in report] == [["1"], []]


# ---------------------------------------------------------------------------
# install / uninstall
# ---------------------------------------------------------------------------

EXE = "/opt/cswap tools/bin/cswap-statusline"


def settings(home: Path) -> Path:
    return home / ".claude" / "settings.json"


class TestInstall:
    def test_wraps_an_existing_command_and_puts_it_back(self, temp_home):
        path = settings(temp_home)
        original = {"model": "opus", "statusLine": {
            "type": "command", "command": "bash -c 'echo \"it'\\''s me\"'", "padding": 2}}
        path.write_text(json.dumps(original))
        assert sl.install(path=path, executable=EXE) == ("installed", original["statusLine"]["command"])
        current = json.loads(path.read_text())
        assert current["model"] == "opus" and current["statusLine"]["padding"] == 2
        assert sl.is_hook(current["statusLine"])
        assert sl.wrapped_command(current["statusLine"]) == original["statusLine"]["command"]
        assert json.loads(path.with_name("settings.json.cswap-backup").read_text()) == original

        assert sl.install(path=path, executable=EXE)[0] == "already"
        assert sl.uninstall(path=path) == (True, original["statusLine"]["command"])
        assert json.loads(path.read_text()) == original
        assert sl.uninstall(path=path) == (False, None)

    def test_wrapped_command_survives_a_real_shell(self, temp_home, tmp_path):
        path = settings(temp_home)
        mine = "printf '%s\\n' \"it's $((1 + 1))\""
        path.write_text(json.dumps({"statusLine": {"type": "command", "command": mine}}))
        sl.install(path=path, executable=EXE)
        command = json.loads(path.read_text())["statusLine"]["command"]
        import shlex

        assert command.startswith(shlex.quote(EXE) + " ")
        echo = f"python3 -c \"import sys; print(sys.argv[1:])\" {command[len(shlex.quote(EXE)):]}"
        out = subprocess.run(["bash", "-c", echo], capture_output=True, text=True, check=True)
        assert out.stdout.strip() == repr(["--then", mine])

    def test_no_status_line_gets_the_short_one(self, temp_home):
        path = settings(temp_home)
        assert sl.install(path=path, executable=EXE) == ("installed", None)
        assert json.loads(path.read_text()) == {
            "statusLine": {"type": "command", "command": f"'{EXE}'", "padding": 0}}
        assert sl.uninstall(path=path) == (True, None)
        assert json.loads(path.read_text()) == {}

    def test_writes_through_a_symlink(self, temp_home, tmp_path):
        real = tmp_path / "real-settings.json"
        real.write_text(json.dumps({"statusLine": {"type": "command", "command": "mine"}}))
        link = settings(temp_home)
        link.symlink_to(real)
        sl.install(path=link, executable=EXE)
        assert link.is_symlink()
        assert sl.is_hook(json.loads(real.read_text())["statusLine"])

    def test_keeps_the_file_mode(self, temp_home):
        path = settings(temp_home)
        path.write_text("{}")
        path.chmod(0o600)
        sl.install(path=path, executable=EXE)
        assert path.stat().st_mode & 0o777 == 0o600 or os.name == "nt"

    @pytest.mark.parametrize("content", ["{nope", "[]"])
    def test_refuses_a_file_it_cannot_read(self, temp_home, content):
        path = settings(temp_home)
        path.write_text(content)
        with pytest.raises(sl.SetupError):
            sl.install(path=path, executable=EXE)
        assert path.read_text() == content

    @pytest.mark.parametrize("status_line", [
        {"type": "command", "command": "x.ps1", "shell": "powershell"},
        {"type": "static", "text": "hi"},
        {"type": "command", "command": "  "},
        "a string",
    ])
    def test_refuses_what_it_cannot_wrap(self, temp_home, status_line):
        path = settings(temp_home)
        path.write_text(json.dumps({"statusLine": status_line}))
        with pytest.raises(sl.SetupError):
            sl.install(path=path, executable=EXE)
        assert json.loads(path.read_text()) == {"statusLine": status_line}

    @pytest.mark.parametrize(("command", "hook"), [
        ("cswap-statusline", True),
        ("'/x/y/cswap-statusline' --then 'z'", True),
        ("C:/Users/me/.local/bin/cswap-statusline.exe", True),
        ("~/.claude/statusline.sh", False),
        ("echo cswap-statusline", False),
    ])
    def test_is_hook(self, command, hook):
        assert sl.is_hook({"type": "command", "command": command}) is hook

    def test_project_overrides(self, temp_home):
        project = temp_home / "work" / "repo"
        (project / ".claude").mkdir(parents=True)
        (project / ".claude" / "settings.json").write_text(json.dumps({"statusLine": {"command": "x"}}))
        (temp_home / "work" / ".claude").mkdir()
        (temp_home / "work" / ".claude" / "settings.local.json").write_text("{}")
        assert sl.project_overrides(project) == [
            project / ".claude" / "settings.json"]


class TestCli:
    @staticmethod
    def run(argv: list[str]) -> str:
        from claude_swap import cli

        out = io.StringIO()
        with patch.object(sys, "argv", ["cswap", "statusline", *argv]), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch("claude_swap.update_check.check_for_update", return_value=None), \
             patch.object(sl, "hook_executable", return_value=EXE), \
             contextlib.redirect_stdout(out):
            cli.main()
        return out.getvalue()

    def test_install_status_uninstall(self, switcher, temp_home, monkeypatch):
        monkeypatch.chdir(temp_home)
        settings(temp_home).write_text(json.dumps({"statusLine": {"type": "command", "command": "mine"}}))
        assert "No readings yet" in self.run([])
        out = self.run(["install"])
        assert "Installed" in out and "runs mine" in out
        assert "Already installed" in self.run(["install"])

        now = time.time()
        save_record(switcher, record(observedAt=now, requestAt=now - 60, responseAt=now - 30,
                                     credentialChangedAt=None, tokenSha256=sl.token_sha256(TOKEN_1)))
        out = self.run(["status"])
        assert "Runs: mine" in out
        assert "default profile: 5h 40% · 7d 70%" in out and "Account-1 (one@example.com)" in out

        assert "runs mine again" in self.run(["uninstall"])
        assert json.loads(settings(temp_home).read_text())["statusLine"]["command"] == "mine"

    def test_purge_puts_the_status_line_back(self, switcher, temp_home):
        settings(temp_home).write_text(json.dumps({"statusLine": {"type": "command", "command": "mine"}}))
        sl.install(path=settings(temp_home), executable=EXE)
        with patch("builtins.input", return_value="y"), contextlib.redirect_stdout(io.StringIO()) as out:
            switcher.purge()
        assert json.loads(settings(temp_home).read_text())["statusLine"]["command"] == "mine"
        assert "status line hook" in out.getvalue()
