"""Tests for the usage/spend estimator (backlog item 4)."""

import json
import os
import time
from datetime import datetime, timezone

from claude_swap import usage_report
from claude_swap.usage_report import (
    Message,
    SwitchTimeline,
    _parse_usage_line,
    _price_for,
    _cost_usd,
    build_report,
    parse_switch_timeline,
    read_switch_logs,
    scan_transcripts,
)


def _log_line(stamp: str, frm: int, to: int) -> str:
    return f"{stamp},640 - INFO - Switched from account {frm} to {to}"


def _local(stamp: str) -> float:
    return datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").timestamp()


class TestSwitchTimeline:
    def test_attribution_across_switches(self):
        tl = parse_switch_timeline(["\n".join([
            _log_line("2026-08-20 10:00:00", 1, 2),
            _log_line("2026-08-20 12:00:00", 2, 3),
        ])])
        assert tl.account_at(_local("2026-08-20 11:00:00")) == 2
        assert tl.account_at(_local("2026-08-20 13:00:00")) == 3

    def test_before_first_switch_uses_from_account(self):
        # Coverage starts at the log's first line of ANY kind.
        text = "\n".join([
            "2026-08-19 09:00:00,000 - INFO - something unrelated",
            _log_line("2026-08-20 10:00:00", 1, 2),
        ])
        tl = parse_switch_timeline([text])
        assert tl.account_at(_local("2026-08-19 12:00:00")) == 1

    def test_before_log_coverage_is_unattributed(self):
        tl = parse_switch_timeline(["\n".join([
            _log_line("2026-08-20 10:00:00", 1, 2),
        ])])
        assert tl.account_at(_local("2026-08-01 00:00:00")) is None

    def test_no_switch_lines_means_unattributed(self):
        tl = parse_switch_timeline(["2026-08-20 10:00:00,0 - INFO - hi"])
        assert tl.account_at(_local("2026-08-21 00:00:00")) is None

    def test_rotated_logs_read_oldest_first(self, tmp_path):
        (tmp_path / "claude-swap.log").write_text("newest")
        (tmp_path / "claude-swap.log.1").write_text("middle")
        (tmp_path / "claude-swap.log.2").write_text("oldest")
        assert read_switch_logs(tmp_path) == ["oldest", "middle", "newest"]


def _usage_line(mid="msg_1", model="claude-opus-5", ts="2026-08-20T10:00:00.000Z",
                inp=10, out=20, cr=30, cc_total=40, cc_1h=None, cc_5m=None):
    usage = {
        "input_tokens": inp, "output_tokens": out,
        "cache_read_input_tokens": cr, "cache_creation_input_tokens": cc_total,
    }
    if cc_1h is not None or cc_5m is not None:
        usage["cache_creation"] = {
            "ephemeral_1h_input_tokens": cc_1h or 0,
            "ephemeral_5m_input_tokens": cc_5m or 0,
        }
    return json.dumps({
        "type": "assistant", "timestamp": ts,
        "message": {"id": mid, "model": model, "usage": usage},
    })


class TestParseUsageLine:
    def test_basic(self):
        mid, msg = _parse_usage_line(_usage_line())
        assert mid == "msg_1"
        assert (msg.input, msg.output, msg.cache_read) == (10, 20, 30)
        assert msg.cache_write_5m == 40 and msg.cache_write_1h == 0
        assert msg.epoch == datetime(2026, 8, 20, 10, tzinfo=timezone.utc).timestamp()

    def test_cache_breakdown(self):
        _, msg = _parse_usage_line(_usage_line(cc_total=40, cc_1h=30, cc_5m=10))
        assert (msg.cache_write_1h, msg.cache_write_5m) == (30, 10)

    def test_breakdown_mismatch_falls_back_to_5m(self):
        # Untrusted breakdown that doesn't account for the total: price
        # everything at the cheaper 5m rate rather than dropping tokens.
        _, msg = _parse_usage_line(_usage_line(cc_total=40, cc_1h=5, cc_5m=5))
        assert (msg.cache_write_1h, msg.cache_write_5m) == (0, 40)

    def test_garbage_and_shape_mismatches(self):
        assert _parse_usage_line("not json") is None
        assert _parse_usage_line('{"message": "usage"}') is None
        assert _parse_usage_line(json.dumps(
            {"message": {"id": "x", "usage": {}}, "timestamp": "bad"})) is None


class TestScanTranscripts:
    def test_dedupes_by_message_id_keep_last(self, tmp_path):
        d = tmp_path / "proj"
        d.mkdir()
        (d / "a.jsonl").write_text(
            _usage_line(mid="m1", out=100) + "\n" + _usage_line(mid="m1", out=200) + "\n"
        )
        msgs = list(scan_transcripts(tmp_path, since_epoch=0))
        assert len(msgs) == 1 and msgs[0].output == 200

    def test_old_mtime_files_skipped(self, tmp_path):
        d = tmp_path / "proj"
        d.mkdir()
        f = d / "old.jsonl"
        f.write_text(_usage_line() + "\n")
        past = time.time() - 30 * 86400
        os.utime(f, (past, past))
        assert list(scan_transcripts(tmp_path, since_epoch=time.time() - 86400)) == []

    def test_lines_before_window_dropped(self, tmp_path):
        d = tmp_path / "proj"
        d.mkdir()
        (d / "a.jsonl").write_text(
            _usage_line(mid="m1", ts="2026-08-01T00:00:00Z") + "\n"
            + _usage_line(mid="m2", ts="2026-08-20T00:00:00Z") + "\n"
        )
        since = datetime(2026, 8, 10, tzinfo=timezone.utc).timestamp()
        msgs = list(scan_transcripts(tmp_path, since_epoch=since))
        assert len(msgs) == 1

    def test_missing_dir(self, tmp_path):
        assert list(scan_transcripts(tmp_path / "nope", since_epoch=0)) == []


class TestPricing:
    def test_known_model(self):
        assert _price_for("claude-opus-5")["input"] == 5.0

    def test_long_context_variant_prices_at_base(self):
        assert _price_for("claude-fable-5[1m]") == _price_for("claude-fable-5")

    def test_dated_id_falls_back(self):
        assert _price_for("claude-haiku-4-5-20251001") == _price_for("claude-haiku-4-5")

    def test_unknown_is_none(self):
        assert _price_for("gpt-7") is None

    def test_one_hour_writes_cost_double_input(self):
        rates = _price_for("claude-opus-5")
        only_1h = Message(epoch=0, model="claude-opus-5", cache_write_1h=1_000_000)
        assert _cost_usd(only_1h, rates) == 10.0  # 2x the $5 input rate
        only_5m = Message(epoch=0, model="claude-opus-5", cache_write_5m=1_000_000)
        assert _cost_usd(only_5m, rates) == 6.25


class TestBuildReport:
    def _timeline(self):
        # An early unrelated line so coverage starts well before the switch.
        return parse_switch_timeline(["\n".join([
            "2026-08-15 00:00:00,000 - INFO - engine started",
            _log_line("2026-08-20 10:00:00", 1, 2),
        ])])

    def test_per_account_and_total(self):
        tl = self._timeline()
        before = Message(epoch=_local("2026-08-20 09:00:00"),
                         model="claude-opus-5", output=1_000_000)
        after = Message(epoch=_local("2026-08-20 11:00:00"),
                        model="claude-opus-5", output=2_000_000)
        report = build_report([before, after], tl, days=7,
                              labels={1: {"email": "a@x.io", "alias": "dev"}})
        assert report["schemaVersion"] == 1
        rows = {r["number"]: r for r in report["accounts"]}
        assert rows[1]["estimatedUSD"] == 25.0 and rows[1]["alias"] == "dev"
        assert rows[2]["estimatedUSD"] == 50.0
        assert report["estimatedTotalUSD"] == 75.0
        assert "unattributed" not in report

    def test_unattributed_bucket(self):
        tl = self._timeline()
        ancient = Message(epoch=_local("2026-08-01 00:00:00"),
                          model="claude-opus-5", output=1_000_000)
        report = build_report([ancient], tl, days=30)
        assert report["unattributed"]["estimatedUSD"] == 25.0
        assert report["estimatedTotalUSD"] == 25.0
        assert any("predate" in c for c in report["caveats"])

    def test_unpriced_model_counts_tokens_not_dollars(self):
        tl = self._timeline()
        weird = Message(epoch=_local("2026-08-20 11:00:00"),
                        model="claude-mystery-9", output=500)
        report = build_report([weird], tl, days=7)
        assert report["estimatedTotalUSD"] == 0.0
        assert report["unpricedTokens"] == 500
        assert report["unpricedModels"] == ["claude-mystery-9"]

    def test_daily_buckets_split_by_date_and_account(self):
        tl = self._timeline()
        msgs = [
            Message(epoch=_local("2026-08-20 09:00:00"),
                    model="claude-opus-5", output=1_000_000),   # account 1
            Message(epoch=_local("2026-08-20 11:00:00"),
                    model="claude-opus-5", output=1_000_000),   # account 2
            Message(epoch=_local("2026-08-21 11:00:00"),
                    model="claude-opus-5", output=2_000_000),   # account 2, next day
        ]
        report = build_report(msgs, tl, days=7)
        assert report["daily"] == [
            {"date": "2026-08-20", "account": 1,
             "estimatedUSD": 25.0, "messages": 1},
            {"date": "2026-08-20", "account": 2,
             "estimatedUSD": 25.0, "messages": 1},
            {"date": "2026-08-21", "account": 2,
             "estimatedUSD": 50.0, "messages": 1},
        ]

    def test_daily_unattributed_uses_null_account(self):
        tl = self._timeline()
        ancient = Message(epoch=_local("2026-08-01 00:00:00"),
                          model="claude-opus-5", output=1_000_000)
        report = build_report([ancient], tl, days=30)
        assert report["daily"] == [
            {"date": "2026-08-01", "account": None,
             "estimatedUSD": 25.0, "messages": 1},
        ]

    def test_estimate_caveat_always_present(self):
        report = build_report([], self._timeline(), days=7)
        assert any("NOT billing truth" in c for c in report["caveats"])
        assert report["priceTable"]["source"] == "models.dev"


class TestUsageCommand:
    """`cswap usage` — the CLI glue over the module."""

    def _run(self, argv, temp_home, sample_sequence_data, capsys):
        import sys
        from unittest.mock import patch

        from claude_swap import cli
        from claude_swap.switcher import ClaudeAccountSwitcher

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)
        (switcher.backup_dir / "claude-swap.log").write_text("\n".join([
            "2026-08-15 00:00:00,000 - INFO - engine started",
            _log_line("2026-08-20 10:00:00", 1, 2),
        ]))
        proj = temp_home / ".claude" / "projects" / "-w"
        proj.mkdir(parents=True)
        (proj / "s.jsonl").write_text(
            _usage_line(mid="m1", ts="2026-08-20T12:00:00Z",
                        inp=0, out=1_000_000, cr=0, cc_total=0) + "\n"
        )
        with patch.object(sys, "argv", ["cswap", "usage", *argv]):
            cli._usage_command(argv)
        return capsys.readouterr().out

    def test_json_reports_attributed_spend(
        self, temp_home, sample_sequence_data, capsys
    ):
        # Window must reach back to the fixture's 2026-08-20 messages.
        days = (datetime.now(timezone.utc)
                - datetime(2026, 8, 19, tzinfo=timezone.utc)).days + 1
        out = self._run(["--json", "--days", str(days)],
                        temp_home, sample_sequence_data, capsys)
        report = json.loads(out)
        assert report["schemaVersion"] == 1
        rows = {r["number"]: r for r in report["accounts"]}
        # Message at 12:00 follows the 10:00 switch to account 2.
        assert rows[2]["estimatedUSD"] == 25.0
        assert rows[2]["email"]  # labeled from the sequence record
        assert report["priceTable"]["source"] == "models.dev"

    def test_days_must_be_positive(self, temp_home, sample_sequence_data, capsys):
        import pytest

        with pytest.raises(SystemExit):
            self._run(["--days", "0"], temp_home, sample_sequence_data, capsys)
