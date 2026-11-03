"""Read the tail of a Claude Code session transcript to tell whether the
session is stopped on a usage limit, and when that limit resets.

Claude Code appends one JSON record per line to
``<config_home>/projects/<project>/<sessionId>.jsonl``. A usage-limit stop is
a synthetic assistant record (``isApiErrorMessage: true``, ``apiError:
"usage_limit_reached"``) whose ``apiErrorParams.rate_limit_info`` carries the
reset epoch and the limit type. The user-facing text changes between
versions ("You've hit your session limit · resets 2am", later "...5-hour
limit..."), so only the structured fields are trusted, with the older
``"...limit reached|<epoch>"`` text format as a fallback.

Only main-chain turn records count (``type`` user/assistant, not sidechain,
not meta); bookkeeping records (queue operations, titles, attachments) are
skipped. Reading is tail-only and never raises: an unreadable transcript is
``UNKNOWN``.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# Plenty for the last few turns even with large tool results; a tail that
# doesn't reach back far enough only ever hides older history, never the end.
TAIL_BYTES = 512 * 1024

STALLED = "stalled"  # the last turn record is a usage-limit stop
PROGRESSED = "progressed"  # Claude produced real output after the last stop
PENDING = "pending"  # a prompt is in flight: a user turn after the stop, no reply yet
UNKNOWN = "unknown"  # no readable turn records

_LEGACY_LIMIT_TEXT = re.compile(r"limit reached\|(\d{9,11})")


@dataclass(frozen=True)
class Stall:
    """One usage-limit stop as recorded in the transcript."""

    uuid: str
    at: float  # epoch seconds of the stop
    resets_at: float | None  # epoch seconds the limit lifts, when recorded
    limit_type: str | None  # "five_hour", "seven_day", ...
    text: str  # what the user saw


@dataclass(frozen=True)
class TranscriptState:
    status: str
    stall: Stall | None = None
    permission_mode: str | None = None  # last permissionMode recorded in the tail
    # Timestamps within the tail, oldest first: real assistant output, and
    # prompts a human typed (peer messages and task notifications excluded).
    progress_times: tuple[float, ...] = ()
    human_prompt_times: tuple[float, ...] = ()

    def progressed_since(self, ts: float) -> bool:
        return any(t > ts for t in self.progress_times)

    def prompted_since(self, ts: float) -> bool:
        return any(t > ts for t in self.human_prompt_times)


def _epoch(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _text_of(record: dict) -> str:
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        )
    return ""


def _limit_info(record: dict) -> dict:
    params = record.get("apiErrorParams")
    info = params.get("rate_limit_info") if isinstance(params, dict) else None
    if isinstance(info, dict):
        return info
    quota = record.get("quotaLimits")
    return quota if isinstance(quota, dict) else {}


def usage_limit_stall(record: dict) -> Stall | None:
    """The :class:`Stall` a record represents, or ``None`` if it isn't one."""
    if record.get("type") != "assistant" or not record.get("isApiErrorMessage"):
        return None
    info = _limit_info(record)
    text = _text_of(record)
    legacy = _LEGACY_LIMIT_TEXT.search(text)
    is_limit = (
        record.get("apiError") == "usage_limit_reached"
        or info.get("status") == "rejected"
        or legacy is not None
    )
    if not is_limit:
        return None
    resets_at = info.get("resetsAt")
    if not isinstance(resets_at, (int, float)) or isinstance(resets_at, bool):
        resets_at = float(legacy.group(1)) if legacy else None
    limit_type = info.get("rateLimitType")
    return Stall(
        uuid=str(record.get("uuid") or ""),
        at=_epoch(record.get("timestamp")) or 0.0,
        resets_at=float(resets_at) if resets_at is not None else None,
        limit_type=limit_type if isinstance(limit_type, str) else None,
        text=text.strip(),
    )


def _tail_records(path: Path, tail_bytes: int) -> list[dict]:
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        start = max(0, size - tail_bytes)
        fh.seek(start)
        data = fh.read()
    lines = data.split(b"\n")
    if start > 0:
        lines = lines[1:]  # the first line is a partial record
    records = []
    for raw in lines:
        if not raw.strip():
            continue
        try:
            record = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _is_turn_record(record: dict) -> bool:
    # Peer (cross-session) messages are recorded as meta user turns, so they
    # are skipped here; only the reply they provoke moves the state.
    return (
        record.get("type") in ("user", "assistant")
        and not record.get("isSidechain")
        and not record.get("isMeta")
    )


def _is_human_prompt(record: dict) -> bool:
    origin = record.get("origin")
    kind = origin.get("kind") if isinstance(origin, dict) else None
    return kind == "human" or record.get("turnOrigin") == "human"


def _inbound_text(record: dict) -> str:
    """Text of a record that can carry an inbound message, else ``""``: a
    user turn's own text (never tool results or assistant output, which may
    merely quote it), or a queued-command attachment's prompt."""
    if record.get("type") == "user":
        return _text_of(record)
    attachment = record.get("attachment")
    if record.get("type") == "attachment" and isinstance(attachment, dict):
        prompt = attachment.get("prompt")
        if isinstance(prompt, str):
            return prompt
        if isinstance(prompt, list):  # content blocks, as typed prompts are queued
            return " ".join(
                b.get("text", "")
                for b in prompt
                if isinstance(b, dict) and isinstance(b.get("text"), str)
            )
    return ""


def recorded_since(
    path: Path | str, needle: str, since: float, tail_bytes: int = TAIL_BYTES
) -> bool:
    """Whether an inbound message containing ``needle`` was recorded at or
    after ``since``.

    Used to confirm a delivered cross-session message: Claude Code records it
    as a queued-command attachment (mid-turn) or a meta user turn (idle), and
    records nothing at all for a message its inbound controls held or refused.
    """
    try:
        records = _tail_records(Path(path), tail_bytes)
    except OSError:
        return False
    for record in records:
        if needle not in _inbound_text(record):
            continue
        ts = _epoch(record.get("timestamp"))
        if ts is not None and ts >= since:
            return True
    return False


def read_transcript_state(path: Path | str, tail_bytes: int = TAIL_BYTES) -> TranscriptState:
    """Classify the session's position relative to its last usage-limit stop."""
    try:
        records = _tail_records(Path(path), tail_bytes)
    except OSError:
        return TranscriptState(UNKNOWN)

    permission_mode = None
    progress: list[float] = []
    prompts: list[float] = []
    status, stall = UNKNOWN, None
    for record in records:
        mode = record.get("permissionMode")
        if isinstance(mode, str) and mode:
            permission_mode = mode
        if not _is_turn_record(record):
            continue
        found = usage_limit_stall(record)
        if found is not None:
            status, stall = STALLED, found
        elif record.get("type") == "assistant":
            if record.get("isApiErrorMessage"):
                # Another API error (overload, ...) is not progress, and it
                # doesn't lift an earlier limit stop either.
                continue
            status, stall = PROGRESSED, None
            ts = _epoch(record.get("timestamp"))
            if ts is not None:
                progress.append(ts)
        else:
            status, stall = PENDING, None
            ts = _epoch(record.get("timestamp"))
            if ts is not None and _is_human_prompt(record):
                prompts.append(ts)
    return TranscriptState(
        status=status,
        stall=stall,
        permission_mode=permission_mode,
        progress_times=tuple(progress),
        human_prompt_times=tuple(prompts),
    )
