"""Tests for transcript tail parsing (transcript.py).

Record shapes mirror real Claude Code 2.1.3xx transcripts: the usage-limit
stop, a human prompt, a peer (cross-session) message, and bookkeeping
records that must be skipped.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from claude_swap import transcript as tx

RESETS_AT = 1792336800  # 2026-10-18T15:20:00Z


def _ts(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat().replace("+00:00", "Z")


def limit_stop(uuid: str, at: float, limit_type: str = "five_hour") -> dict:
    return {
        "type": "assistant",
        "uuid": uuid,
        "timestamp": _ts(at),
        "isSidechain": False,
        "isApiErrorMessage": True,
        "error": "rate_limit",
        "apiError": "usage_limit_reached",
        "apiErrorStatus": 429,
        "apiErrorParams": {
            "rate_limit_info": {
                "status": "rejected",
                "resetsAt": RESETS_AT,
                "rateLimitType": limit_type,
            }
        },
        "message": {
            "model": "<synthetic>",
            "content": [{"type": "text", "text": "You've hit your session limit · resets 4:20pm"}],
        },
    }


def assistant(uuid: str, at: float, text: str = "working on it") -> dict:
    return {
        "type": "assistant",
        "uuid": uuid,
        "timestamp": _ts(at),
        "isSidechain": False,
        "message": {"content": [{"type": "text", "text": text}]},
    }


def human(
    uuid: str, at: float, text: str = "carry on pls", mode: str = "bypassPermissions"
) -> dict:
    return {
        "type": "user",
        "uuid": uuid,
        "timestamp": _ts(at),
        "isSidechain": False,
        "origin": {"kind": "human"},
        "turnOrigin": "human",
        "permissionMode": mode,
        "message": {"role": "user", "content": text},
    }


def peer_message(uuid: str, at: float) -> dict:
    return {
        "type": "user",
        "uuid": uuid,
        "timestamp": _ts(at),
        "isSidechain": False,
        "isMeta": True,
        "origin": {"kind": "peer", "from": "uds:x"},
        "turnOrigin": "peer",
        "message": {"role": "user", "content": "Another Claude session sent a message"},
    }


def write(path: Path, *records: dict) -> Path:
    lines = [json.dumps(r) for r in records]
    lines.insert(1, json.dumps({"type": "queue-operation", "operation": "enqueue"}))
    lines.append(json.dumps({"type": "ai-title", "title": "x"}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TestUsageLimitStall:
    def test_structured_fields_are_read(self):
        stall = tx.usage_limit_stall(limit_stop("u1", 1000.0, "seven_day"))
        assert stall is not None
        assert stall.uuid == "u1"
        assert stall.at == 1000.0
        assert stall.resets_at == RESETS_AT
        assert stall.limit_type == "seven_day"
        assert "session limit" in stall.text

    def test_legacy_text_format_supplies_the_reset(self):
        record = {
            "type": "assistant",
            "uuid": "u2",
            "timestamp": _ts(5.0),
            "isApiErrorMessage": True,
            "message": {
                "content": [{"type": "text", "text": "Claude AI usage limit reached|1760000000"}]
            },
        }
        stall = tx.usage_limit_stall(record)
        assert stall is not None and stall.resets_at == 1760000000

    def test_other_api_errors_are_not_stalls(self):
        record = {
            "type": "assistant",
            "isApiErrorMessage": True,
            "error": "overloaded",
            "message": {"content": [{"type": "text", "text": "Overloaded"}]},
        }
        assert tx.usage_limit_stall(record) is None
        assert tx.usage_limit_stall(assistant("u3", 1.0)) is None


class TestReadTranscriptState:
    def test_ends_on_a_stop_is_stalled(self, tmp_path: Path):
        path = write(
            tmp_path / "t.jsonl", human("h1", 10), assistant("a1", 20), limit_stop("s1", 30)
        )
        state = tx.read_transcript_state(path)
        assert state.status == tx.STALLED
        assert state.stall is not None and state.stall.uuid == "s1"
        assert state.permission_mode == "bypassPermissions"

    def test_output_after_the_stop_is_progressed(self, tmp_path: Path):
        path = write(
            tmp_path / "t.jsonl",
            limit_stop("s1", 30),
            human("h2", 40),
            assistant("a2", 50),
        )
        state = tx.read_transcript_state(path)
        assert state.status == tx.PROGRESSED
        assert state.progressed_since(30) and not state.progressed_since(50)
        assert state.prompted_since(30)

    def test_prompt_in_flight_is_pending(self, tmp_path: Path):
        path = write(tmp_path / "t.jsonl", limit_stop("s1", 30), human("h2", 40))
        assert tx.read_transcript_state(path).status == tx.PENDING

    def test_peer_message_turn_is_skipped(self, tmp_path: Path):
        # A delivered continue message is a meta user turn; until Claude
        # replies, the session still reads as stopped on the same limit.
        path = write(tmp_path / "t.jsonl", limit_stop("s1", 30), peer_message("p1", 35))
        state = tx.read_transcript_state(path)
        assert state.status == tx.STALLED and state.stall.uuid == "s1"
        assert not state.prompted_since(30)

    def test_non_limit_error_after_a_stop_does_not_lift_it(self, tmp_path: Path):
        overload = {
            "type": "assistant",
            "uuid": "o1",
            "timestamp": _ts(40),
            "isApiErrorMessage": True,
            "error": "overloaded",
            "message": {"content": []},
        }
        path = write(tmp_path / "t.jsonl", limit_stop("s1", 30), overload)
        state = tx.read_transcript_state(path)
        assert state.status == tx.STALLED and state.stall.uuid == "s1"

    def test_sidechain_records_are_ignored(self, tmp_path: Path):
        side = assistant("x1", 40)
        side["isSidechain"] = True
        path = write(tmp_path / "t.jsonl", limit_stop("s1", 30), side)
        assert tx.read_transcript_state(path).status == tx.STALLED

    def test_tail_window_skips_the_partial_first_line(self, tmp_path: Path):
        path = write(
            tmp_path / "t.jsonl",
            *(assistant(f"a{i}", i, "x" * 200) for i in range(50)),
            limit_stop("s1", 100),
        )
        state = tx.read_transcript_state(path, tail_bytes=2_000)
        assert state.status == tx.STALLED and state.stall.uuid == "s1"

    def test_missing_file_is_unknown(self, tmp_path: Path):
        assert tx.read_transcript_state(tmp_path / "nope.jsonl").status == tx.UNKNOWN

    def test_garbage_lines_are_tolerated(self, tmp_path: Path):
        path = tmp_path / "t.jsonl"
        path.write_text("not json\n" + json.dumps(limit_stop("s1", 30)) + "\n{broken\n")
        assert tx.read_transcript_state(path).status == tx.STALLED


class TestRecordedSince:
    NEEDLE = "[claude-swap auto-continue]"

    def test_meta_user_turn_counts(self, tmp_path: Path):
        record = human("m1", 500.0, text=f"{self.NEEDLE} continue please")
        record["isMeta"] = True
        path = write(tmp_path / "t.jsonl", record)
        assert tx.recorded_since(path, self.NEEDLE, 400.0)
        assert not tx.recorded_since(path, self.NEEDLE, 600.0)  # older than `since`

    def test_queued_command_attachment_counts(self, tmp_path: Path):
        record = {
            "type": "attachment",
            "timestamp": _ts(500.0),
            "attachment": {"type": "queued_command", "prompt": f"{self.NEEDLE} go"},
        }
        path = write(tmp_path / "t.jsonl", record)
        assert tx.recorded_since(path, self.NEEDLE, 400.0)

    def test_quotes_in_assistant_output_or_tool_results_do_not_count(self, tmp_path: Path):
        # Found live: a session editing cswap itself mentions the marker in
        # tool calls and tool results — that's not the message arriving.
        quoted = assistant("a1", 500.0, text=f'MARKER = "{self.NEEDLE}"')
        tool_result = {
            "type": "user",
            "timestamp": _ts(501.0),
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "content": [{"type": "text", "text": self.NEEDLE}]}
                ],
            },
        }
        path = write(tmp_path / "t.jsonl", quoted, tool_result)
        assert not tx.recorded_since(path, self.NEEDLE, 400.0)
