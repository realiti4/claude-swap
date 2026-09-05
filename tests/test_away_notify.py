"""Tests for the away-mode push (away_notify.py, backlog item 7)."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import away_notify
from claude_swap.away_notify import (
    load_channels,
    masked,
    notify_path,
    push,
    save_channels,
    switch_text,
)

WEBHOOK = "https://hooks.slack.com/services/T000/B000/secret9xQz"


class TestChannelConfig:
    def test_round_trip(self, tmp_path):
        save_channels(tmp_path, {"slackWebhookUrl": WEBHOOK})
        assert load_channels(tmp_path) == {"slackWebhookUrl": WEBHOOK}

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
    def test_file_is_owner_only(self, tmp_path):
        save_channels(tmp_path, {"slackWebhookUrl": WEBHOOK})
        mode = stat.S_IMODE(os.stat(notify_path(tmp_path)).st_mode)
        assert mode == 0o600

    def test_empty_config_removes_the_file(self, tmp_path):
        save_channels(tmp_path, {"slackWebhookUrl": WEBHOOK})
        save_channels(tmp_path, {})
        assert not notify_path(tmp_path).exists()

    def test_missing_or_garbage_reads_as_empty(self, tmp_path):
        assert load_channels(tmp_path) == {}
        notify_path(tmp_path).write_text("not json")
        assert load_channels(tmp_path) == {}
        notify_path(tmp_path).write_text("[1, 2]")
        assert load_channels(tmp_path) == {}


class TestMasked:
    def test_url_shows_host_and_tail_only(self):
        m = masked(WEBHOOK)
        assert m == "hooks.slack.com…9xQz"
        assert "secret" not in m

    def test_bare_token_shows_tail_only(self):
        assert masked("123456:AAbbCCdd") == "…CCdd"

    def test_unset(self):
        assert masked("") == "(unset)"


class _Opener:
    """Fake urlopen capturing each request; raises for URLs in `fail`."""

    def __init__(self, fail=()):
        self.requests = []
        self.fail = tuple(fail)

    def __call__(self, request, timeout=None):
        url = request.full_url
        body = json.loads(request.data.decode("utf-8"))
        self.requests.append((url, body))
        if any(f in url for f in self.fail):
            raise OSError("connection refused")

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return _Resp()


class TestPush:
    def test_no_config_is_a_noop(self, tmp_path):
        opener = _Opener()
        assert push(tmp_path, "hello", opener=opener) == []
        assert opener.requests == []

    def test_slack_receives_the_text(self, tmp_path):
        save_channels(tmp_path, {"slackWebhookUrl": WEBHOOK})
        opener = _Opener()
        assert push(tmp_path, "switched to 2", opener=opener) == ["slack"]
        assert opener.requests == [(WEBHOOK, {"text": "switched to 2"})]

    def test_telegram_receives_chat_id_and_text(self, tmp_path):
        save_channels(
            tmp_path, {"telegramBotToken": "123:abc", "telegramChatId": "42"}
        )
        opener = _Opener()
        assert push(tmp_path, "hi", opener=opener) == ["telegram"]
        url, body = opener.requests[0]
        assert url == "https://api.telegram.org/bot123:abc/sendMessage"
        assert body == {"chat_id": "42", "text": "hi"}

    def test_one_failed_channel_never_blocks_the_other(self, tmp_path, caplog):
        save_channels(tmp_path, {
            "slackWebhookUrl": WEBHOOK,
            "telegramBotToken": "123:abc",
            "telegramChatId": "42",
        })
        opener = _Opener(fail=["hooks.slack.com"])
        assert push(tmp_path, "hi", opener=opener) == ["telegram"]

    def test_failure_log_never_carries_the_secret(self, tmp_path, caplog):
        save_channels(tmp_path, {"slackWebhookUrl": WEBHOOK})
        with caplog.at_level("DEBUG", logger="claude-swap"):
            push(tmp_path, "hi", opener=_Opener(fail=["hooks.slack.com"]))
        assert caplog.records, "the failure should be logged"
        assert "secret9xQz" not in caplog.text
        assert WEBHOOK not in caplog.text

    def test_config_read_per_push_not_cached(self, tmp_path):
        # A webhook added while the engine runs must land on the next switch.
        opener = _Opener()
        assert push(tmp_path, "one", opener=opener) == []
        save_channels(tmp_path, {"slackWebhookUrl": WEBHOOK})
        assert push(tmp_path, "two", opener=opener) == ["slack"]


class TestSwitchText:
    def test_plain(self):
        assert switch_text("dev", "2", 0) == "cswap: switched to account 2 (dev)"

    def test_with_rearm_count(self):
        assert (
            switch_text("dev", "2", 8)
            == "cswap: switched to account 2 (dev)"
            " — remote control re-armed on 8 session(s)"
        )

    def test_fleet_between_head_and_urls(self):
        # Fleet status below the head (lock screens preview the head
        # only), URLs after it.
        text = switch_text(
            "dev", "2", 0,
            urls=["https://x/1"],
            fleet=["→ 2 dev: 5h 10%", "· 1 alpha: out (7d 100%)"],
        )
        assert text.split("\n") == [
            "cswap: switched to account 2 (dev)",
            "→ 2 dev: 5h 10%",
            "· 1 alpha: out (7d 100%)",
            "https://x/1",
        ]


class TestFleetLines:
    def test_windows_render_compact(self):
        usage = {
            "five_hour": {"pct": 45.4},
            "seven_day": {"pct": 12.0},
            "scoped": [{"name": "Fable", "pct": 3.0}],
        }
        assert away_notify.fleet_lines([("2", "bravo", usage, True)]) == [
            "→ 2 bravo: 5h 45% · 7d 12% · Fable 3%"
        ]

    def test_maxed_window_reads_out(self):
        usage = {"five_hour": {"pct": 20.0}, "seven_day": {"pct": 100.0}}
        assert away_notify.fleet_lines([("1", "alpha", usage, False)]) == [
            "· 1 alpha: out (7d 100%)"
        ]

    def test_missing_usage_reads_no_data(self):
        assert away_notify.fleet_lines([("3", "carol", None, False)]) == [
            "· 3 carol: no data"
        ]


class TestEngineWiring:
    """The engine pushes on its own switches, after the /rc sweep."""

    def _harness(self, temp_home, **settings_kwargs):
        sys.path.insert(0, str(Path(__file__).parent))
        from test_autoswitch import EngineHarness, _usage

        h = EngineHarness(temp_home, threshold=90.0, **settings_kwargs)
        h.seed(1, "alpha@example.com")
        h.seed(2, "bravo@example.com")
        h.make_live("alpha@example.com", 1)
        return h, {"1": _usage(95.0), "2": _usage(10.0)}

    def test_switch_pushes_and_emits_event(self, temp_home):
        h, usage = self._harness(temp_home)
        with patch.object(away_notify, "push", return_value=["slack"]) as p:
            h.tick_with_usage(usage)
        assert "away-notified" in h.kinds()
        _, text = p.call_args.args
        lines = text.split("\n")
        assert lines[0] == "cswap: switched to account 2 (bravo)"
        # The push carries the whole fleet's status (user 2026-08-30).
        assert any(line.startswith("→ 2 bravo:") for line in lines[1:])
        assert any(line.startswith("· 1 alpha:") for line in lines[1:])

    def test_alias_wins_over_email_local_part(self, temp_home):
        h, usage = self._harness(temp_home)
        h.switcher.set_alias("2", "workhorse")
        with patch.object(away_notify, "push", return_value=["slack"]) as p:
            h.tick_with_usage(usage)
        assert "(workhorse)" in p.call_args.args[1]

    def test_no_channels_no_event(self, temp_home):
        h, usage = self._harness(temp_home)
        with patch.object(away_notify, "push", return_value=[]):
            h.tick_with_usage(usage)
        assert "away-notified" not in h.kinds()
        assert "switch" in h.kinds()

    def test_push_failure_never_breaks_the_switch(self, temp_home):
        h, usage = self._harness(temp_home)
        with patch.object(away_notify, "push", side_effect=RuntimeError("boom")):
            h.tick_with_usage(usage)
        assert "switch" in h.kinds()
        assert "error" not in h.kinds()

    def test_push_runs_after_the_rc_sweep(self, temp_home):
        from claude_swap import cmux_control
        from claude_swap.cmux_control import SweepResult

        h, usage = self._harness(temp_home, rearm_remote_control=True)
        order = []
        with (
            patch.object(
                cmux_control, "rearm_remote_control",
                side_effect=lambda *a, **k: order.append("sweep")
                or SweepResult(sent=["surface:1"], skipped_self=None, no_surface=0),
            ),
            patch.object(
                away_notify, "push",
                side_effect=lambda *a, **k: order.append("push") or ["slack"],
            ) as p,
        ):
            h.tick_with_usage(usage)
        assert order == ["sweep", "push"]
        # ... and the body reports what the sweep did.
        assert "re-armed on 1 session(s)" in p.call_args.args[1]


class TestManualSwitchWiring:
    """`cswap use` pushes too (cli._away_notify_switch)."""

    def _switcher(self, tmp_path, alias=None):
        from unittest.mock import MagicMock

        switcher = MagicMock()
        switcher.backup_dir = tmp_path
        switcher.current_account_number.return_value = "2"
        switcher.list_aliases.return_value = (
            [("2", alias, "bravo@example.com")] if alias else []
        )
        switcher.account_email.return_value = "bravo@example.com"
        return switcher

    def test_pushes_and_records_in_payload(self, tmp_path):
        from claude_swap.cli import _away_notify_switch
        from claude_swap.cmux_control import SweepResult

        payload: dict = {}
        sweep = SweepResult(
            sent=["surface:1", "surface:2", "surface:3"], skipped_self=None,
            no_surface=0, confirmed=["surface:1"],
            urls=["https://claude.ai/code/session_abc"],
        )
        with patch.object(away_notify, "push", return_value=["slack"]) as p:
            _away_notify_switch(self._switcher(tmp_path, alias="dev"), payload, sweep)
        assert payload == {"awayNotified": ["slack"]}
        assert p.call_args.args[1] == (
            "cswap: switched to account 2 (dev)"
            " — remote control re-armed on 3 session(s)"
            "\nhttps://claude.ai/code/session_abc"
        )

    def test_never_breaks_the_switch(self, tmp_path):
        from claude_swap.cli import _away_notify_switch

        with patch.object(away_notify, "push", side_effect=RuntimeError("boom")):
            _away_notify_switch(self._switcher(tmp_path), {}, None)  # must not raise

    def test_unmanaged_login_is_a_noop(self, tmp_path):
        from claude_swap.cli import _away_notify_switch

        switcher = self._switcher(tmp_path)
        switcher.current_account_number.return_value = None
        with patch.object(away_notify, "push") as p:
            _away_notify_switch(switcher, {}, None)
        assert not p.called


class TestNotifyCommand:
    """`cswap notify` — config, test push, removal. Secrets come via stdin."""

    def _run(self, argv, tmp_path, stdin=None, opener=None):
        from claude_swap import cli

        with (
            patch.object(cli.paths, "get_backup_root", return_value=tmp_path),
            patch.object(sys, "stdin") as fake_stdin,
        ):
            fake_stdin.isatty.return_value = False
            fake_stdin.readline.return_value = (stdin or "") + "\n"
            fake_stdin.read.return_value = stdin or ""
            cli._notify_command(argv)

    def test_slack_saved_from_stdin(self, tmp_path, capsys):
        self._run(["slack", "-"], tmp_path, stdin=WEBHOOK)
        assert load_channels(tmp_path) == {"slackWebhookUrl": WEBHOOK}
        out = capsys.readouterr().out
        assert WEBHOOK not in out          # never echo the secret back
        assert "9xQz" in out               # masked confirmation

    def test_slack_rejects_non_https(self, tmp_path):
        with pytest.raises(SystemExit):
            self._run(["slack", "-"], tmp_path, stdin="http://x.example/hook")
        assert load_channels(tmp_path) == {}

    def test_telegram_saved_from_stdin(self, tmp_path):
        self._run(["telegram", "-", "42"], tmp_path, stdin="123:abc")
        assert load_channels(tmp_path) == {
            "telegramBotToken": "123:abc",
            "telegramChatId": "42",
        }

    def test_show_masks_secrets(self, tmp_path, capsys):
        save_channels(tmp_path, {"slackWebhookUrl": WEBHOOK})
        self._run([], tmp_path)
        out = capsys.readouterr().out
        assert "hooks.slack.com…9xQz" in out
        assert WEBHOOK not in out

    def test_off_removes_one_channel(self, tmp_path):
        save_channels(tmp_path, {
            "slackWebhookUrl": WEBHOOK,
            "telegramBotToken": "123:abc",
            "telegramChatId": "42",
        })
        self._run(["off", "slack"], tmp_path)
        assert "slackWebhookUrl" not in load_channels(tmp_path)
        assert load_channels(tmp_path)["telegramBotToken"] == "123:abc"
        self._run(["off"], tmp_path)
        assert not notify_path(tmp_path).exists()

    def test_test_pushes_through_the_real_path(self, tmp_path, capsys):
        save_channels(tmp_path, {"slackWebhookUrl": WEBHOOK})
        opener = _Opener()
        with patch.object(away_notify.urllib.request, "urlopen", opener):
            self._run(["test"], tmp_path)
        assert [u for u, _ in opener.requests] == [WEBHOOK]
        assert "slack" in capsys.readouterr().out

    def test_push_sends_argv_text(self, tmp_path, capsys):
        save_channels(tmp_path, {"slackWebhookUrl": WEBHOOK})
        opener = _Opener()
        with patch.object(away_notify.urllib.request, "urlopen", opener):
            self._run(["push", "all", "sessions", "done"], tmp_path)
        assert [u for u, _ in opener.requests] == [WEBHOOK]
        assert opener.requests[0][1]["text"] == "all sessions done"
        assert "slack" in capsys.readouterr().out

    def test_push_reads_stdin_with_dash(self, tmp_path):
        save_channels(tmp_path, {"slackWebhookUrl": WEBHOOK})
        opener = _Opener()
        with patch.object(away_notify.urllib.request, "urlopen", opener):
            self._run(["push", "-"], tmp_path, stdin="quota gone")
        assert opener.requests[0][1]["text"] == "quota gone"

    def test_push_empty_text_exits_nonzero(self, tmp_path):
        save_channels(tmp_path, {"slackWebhookUrl": WEBHOOK})
        with pytest.raises(SystemExit):
            self._run(["push", "-"], tmp_path, stdin="")

    def test_test_with_no_channels_exits_nonzero(self, tmp_path):
        with pytest.raises(SystemExit):
            self._run(["test"], tmp_path)

    def test_show_json_is_masked_only(self, tmp_path, capsys):
        save_channels(tmp_path, {
            "slackWebhookUrl": WEBHOOK,
            "telegramBotToken": "123456:AAbbCCdd",
            "telegramChatId": "42",
        })
        self._run(["--json"], tmp_path)
        out = capsys.readouterr().out
        got = json.loads(out)
        assert got["slackWebhookUrl"] == "hooks.slack.com…9xQz"
        assert got["telegramBotToken"] == "…CCdd"
        assert got["telegramChatId"] == "42"
        assert WEBHOOK not in out
        assert "AAbbCCdd" not in out

    def test_show_json_when_unset(self, tmp_path, capsys):
        self._run(["--json"], tmp_path)
        got = json.loads(capsys.readouterr().out)
        assert got["slackWebhookUrl"] is None
        assert got["telegramBotToken"] is None


class TestSwitchTextUrls:
    def test_urls_go_below_the_headline(self):
        text = switch_text("dev", "2", 1, urls=["https://claude.ai/code/session_a"])
        assert text.splitlines() == [
            "cswap: switched to account 2 (dev) — remote control re-armed on 1 session(s)",
            "https://claude.ai/code/session_a",
        ]

    def test_url_list_is_capped(self):
        urls = [f"https://claude.ai/code/session_{i}" for i in range(9)]
        text = switch_text("dev", "2", 9, urls=urls)
        lines = text.splitlines()
        assert len(lines) == 8  # headline + 6 urls + "(+3 more)"
        assert lines[-1] == "(+3 more)"
