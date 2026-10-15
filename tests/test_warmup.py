"""Tests for `cswap warmup` (ClaudeAccountSwitcher.warm_up_accounts)."""

import json
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from claude_swap import oauth
from claude_swap.json_output import USAGE_API_KEY
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.usage_store import UsageEntry


def _creds(token: str) -> str:
    return json.dumps({"claudeAiOauth": {"accessToken": token}})


def _info(num: int, creds: str) -> tuple:
    return (num, f"a{num}@example.com", "", "", num == 1, creds, "")


def _resets_in(hours: float) -> dict:
    at = datetime.now(timezone.utc) + timedelta(hours=hours)
    return {"five_hour": {"pct": 10.0, "resets_at": at.isoformat()}}


def _response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode()
    resp.__enter__.return_value = resp
    return resp


class TestOauthWarmup:
    def test_find_warmup_model_picks_first_haiku(self):
        listing = {"data": [
            {"id": "claude-opus-9"},
            {"id": "claude-haiku-9"},
            {"id": "claude-haiku-8"},
        ]}
        with patch(
            "claude_swap.oauth.urllib.request.urlopen",
            return_value=_response(listing),
        ):
            assert oauth.find_warmup_model("tok") == "claude-haiku-9"

    def test_find_warmup_model_none_without_haiku(self):
        with patch(
            "claude_swap.oauth.urllib.request.urlopen",
            return_value=_response({"data": [{"id": "claude-opus-9"}]}),
        ):
            assert oauth.find_warmup_model("tok") is None

    def test_send_warmup_message_is_short_oauth_request(self):
        with patch(
            "claude_swap.oauth.urllib.request.urlopen",
            return_value=_response({}),
        ) as urlopen:
            oauth.send_warmup_message("tok", "some-model")
        req = urlopen.call_args.args[0]
        body = json.loads(req.data)
        assert req.full_url == "https://api.anthropic.com/v1/messages"
        assert req.get_header("Authorization") == "Bearer tok"
        assert req.get_header("Anthropic-beta") == oauth.OAUTH_BETA_HEADER
        assert body["model"] == "some-model"
        # Big enough to register: a 1-token reply does not open the window.
        assert body["max_tokens"] == oauth.WARMUP_MAX_TOKENS > 1


class TestWarmUpAccounts:
    def _run(self, infos, entries, model=None, disabled=(), send=None):
        switcher = ClaudeAccountSwitcher()
        seq = {"accounts": {str(i[0]): {"disabled": i[0] in disabled} for i in infos}}
        with patch.object(switcher, "_build_accounts_info", return_value=infos), \
             patch.object(switcher, "_collect_usage_entries", return_value=entries), \
             patch.object(switcher, "_get_sequence_data", return_value=seq), \
             patch("claude_swap.oauth.find_warmup_model", return_value="haiku-x") as find, \
             patch("claude_swap.oauth.send_warmup_message", side_effect=send) as sent:
            used, results = switcher.warm_up_accounts(model)
        return used, {num: outcome for num, _, outcome in results}, find, sent

    def test_warms_only_idle_accounts(self, temp_home: Path):
        infos = [_info(1, _creds("t1")), _info(2, _creds("t2")), _info(3, _creds("t3"))]
        entries = {
            "1": UsageEntry(last_good=_resets_in(2)),   # running
            "2": UsageEntry(last_good=_resets_in(-1)),  # reset already
            "3": UsageEntry(),                          # never measured
        }
        used, results, find, sent = self._run(infos, entries)

        assert used == "haiku-x"
        assert results == {"1": "already running", "2": "sent", "3": "sent"}
        assert [c.args for c in sent.call_args_list] == [
            ("t2", "haiku-x"), ("t3", "haiku-x"),
        ]
        find.assert_called_once()  # resolved once, reused for every account

    def test_explicit_model_skips_discovery(self, temp_home: Path):
        used, results, find, sent = self._run(
            [_info(1, _creds("t1"))], {"1": UsageEntry()}, model="chosen"
        )
        assert used == "chosen"
        find.assert_not_called()
        sent.assert_called_once_with("t1", "chosen")

    def test_skips_disabled_and_sentinel_accounts(self, temp_home: Path):
        infos = [_info(1, _creds("t1")), _info(2, "sk-ant-api03-x")]
        entries = {"1": UsageEntry(), "2": UsageEntry(sentinel=USAGE_API_KEY)}
        _, results, _, sent = self._run(infos, entries, disabled={1})

        assert results == {"1": "skipped: disabled", "2": f"skipped: {USAGE_API_KEY}"}
        sent.assert_not_called()

    def test_reports_failure_and_continues(self, temp_home: Path):
        error = urllib.error.HTTPError("u", 429, "busy", {}, None)
        infos = [_info(1, _creds("t1")), _info(2, _creds("t2"))]
        entries = {"1": UsageEntry(), "2": UsageEntry()}
        _, results, _, _ = self._run(infos, entries, send=[error, None])

        assert results == {"1": "failed: http-429", "2": "sent"}
