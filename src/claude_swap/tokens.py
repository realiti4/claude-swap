"""Per-prompt token counts read from Claude Code's local session logs.

Claude Code writes every conversation to a JSON Lines transcript under
``<config home>/projects/<project>/<session id>.jsonl``. Subagent
conversations go to ``<session id>/subagents/*.jsonl`` next to it. Each
assistant record carries the API ``usage`` block for the request that
produced it. This module reads those files incrementally and keeps a small
ledger of what each request cost. The layout and field names described here
are as read from Claude Code 2.1.331's transcripts.

Privacy
-------
Only numbers, model ids, timestamps, opaque session ids and the project
*folder name* are kept. Prompt text, reply text and tool output are never
stored, returned or logged. A line is parsed only to read those fields.

What the counts mean
--------------------
For each API request::

    sent     = input tokens + cache-read tokens + cache-write tokens
               (everything the request carried to the model)
    received = output tokens
    context  = "sent" of a single request (how big the conversation was
               at that moment)

Requests are grouped under the user prompt that started them, so one prompt
that ran twenty tool calls shows as one row with twenty calls.

Which account paid
------------------
A request is credited to whichever cswap account was active at its
timestamp. Switches are read from the ``Switched from account A to B`` lines
cswap writes to ``claude-swap.log``. Usage that never passed through this
computer (claude.ai, another machine on the same account) is invisible here.

Cost
----
Each file's read offset is remembered, so a refresh reads only lines
appended since the last one. The ledger is cached on disk between runs and
keeps :data:`RETENTION_S` of history. Refreshes are throttled to one per
:data:`REFRESH_MIN_S`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from claude_swap.paths import get_claude_config_home

_logger = logging.getLogger("claude-swap")

# The on-disk ledger, inside cswap's own cache directory.
CACHE_FILENAME = "tokens_ledger.json"
# Bump when the cached layout changes. A cache with another version is
# ignored and rebuilt from the transcripts.
CACHE_VERSION = 1

# How much history to keep: eight days covers a full weekly window plus a day
# of slack for a window that reset late.
RETENTION_S = 8 * 86400

# Minimum seconds between two transcript scans. The watch screen redraws
# often. Without this it would walk the projects tree on every frame.
REFRESH_MIN_S = 15.0

# How many recent message ids to remember per file. Claude Code writes one
# transcript record per content block (text, tool call, ...). Every record
# from the same API response repeats that response's message id and usage
# block, so a repeated id must be counted once. 64 is far more blocks than
# one response produces.
_SEEN_IDS_PER_FILE = 64

# The model id Claude Code writes on records it made up locally (for example
# an interruption notice). These made no API request, so they are skipped.
_SYNTHETIC_MODEL = "<synthetic>"

# A switch line in claude-swap.log, e.g.
#   2026-11-24 09:15:02,123 - INFO - Switched from account 1 to 2
# Groups: timestamp (to the second), from-slot, to-slot.
_SWITCH_LINE = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ - \w+ - "
    r"Switched from account (\d+) to (\d+)"
)


class _Request(NamedTuple):
    """One API request as kept in the ledger.

    Stored on disk as a plain JSON list in this field order, so the order is
    part of the cache format: change it only together with
    :data:`CACHE_VERSION`.
    """

    ts: float                # when the reply was written (Unix time)
    session: str             # Claude Code session id
    prompt_key: str | None   # the Prompt this request belongs to, if known
    model: str | None        # model id, e.g. "claude-opus-5-5"
    input_tokens: int        # uncached input
    cache_read: int          # input served from the prompt cache
    cache_write: int         # input written into the prompt cache
    output_tokens: int       # tokens the model generated

    @property
    def sent(self) -> int:
        """Everything this request carried to the model (its context size)."""
        return self.input_tokens + self.cache_read + self.cache_write


@dataclass
class Prompt:
    """One user prompt and the totals of every request it led to."""

    key: str                 # "<session id>:<n>", the n-th prompt of that session
    session: str
    ts: float                # when the user sent it
    project: str             # project folder name only (basename of the cwd)
    account: str | None = None  # cswap slot active at ``ts``, if known
    calls: int = 0           # API requests made while answering it
    sent: int = 0            # sum of each request's ``sent``
    received: int = 0        # sum of output tokens
    cache_read: int = 0      # sum of cache-read tokens (part of ``sent``)
    peak_context: int = 0    # largest single request's ``sent``
    models: dict[str, int] = field(default_factory=dict)  # model id -> calls


def _parse_iso_timestamp(value) -> float | None:
    """Turn a transcript timestamp ("2026-11-24T14:15:02.123Z") into Unix time."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _is_user_prompt(record: dict) -> bool:
    """Is this ``"type": "user"`` record something a person typed?

    Claude Code also writes ``"user"`` records for things that are not
    prompts. These are skipped:

    * ``isMeta`` records, which Claude Code injects itself (command output,
      reminders);
    * ``isSidechain`` records, which are a subagent's turns, not the person's;
    * records whose content is a ``tool_result``, which carry a tool's output
      back to the model.

    What is left is a prompt if it has any text or image in it.
    """
    if record.get("isMeta") or record.get("isSidechain"):
        return False
    content = (record.get("message") or {}).get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        block_types = {block.get("type") for block in content if isinstance(block, dict)}
        if "tool_result" in block_types:
            return False
        return bool(block_types & {"text", "image"})
    return False


class Ledger:
    """Incremental, thread-safe reader of Claude Code transcripts.

    Use :func:`ledger_for` rather than constructing one directly, so that
    everything in one process shares one instance and one scan.
    """

    def __init__(self, backup_root: Path, config_dir: Path | None = None):
        self.cache_path = backup_root / "cache" / CACHE_FILENAME
        self.log_path = backup_root / "claude-swap.log"
        self.projects_dir = (config_dir or get_claude_config_home()) / "projects"

        # Held while scanning. Readers do not take it: they work on whatever
        # list is current, and a scan only ever appends or swaps in new lists.
        self._scan_lock = threading.Lock()
        self._last_refresh = 0.0

        # Per transcript path: how far it has been read and what was last
        # seen in it. Keys:
        #   "ino"  - inode, to notice a file replaced under the same name
        #   "off"  - byte offset of the next unread line
        #   "n"    - prompts counted so far in this session (numbers keys)
        #   "last" - key of the latest prompt (requests that follow belong to it)
        #   "ids"  - recent message ids (see _SEEN_IDS_PER_FILE)
        self._files: dict[str, dict] = {}
        self._requests: list[_Request] = []
        # Prompt key -> [ts, session, project]. A list, not a tuple, because
        # it round-trips through JSON.
        self._prompts: dict[str, list] = {}
        # (ts, from slot, to slot) for every switch in claude-swap.log, oldest first.
        self._switches: list[tuple[float, str, str]] = []
        # mtime of the most recently written transcript, for "is a reply
        # being written right now?".
        self._newest_write_ts = 0.0

        self._load_cache()

    # -- on-disk cache ----------------------------------------------------

    def _load_cache(self) -> None:
        """Load the previous run's ledger. Anything unreadable starts empty."""
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict) or raw.get("version") != CACHE_VERSION:
            return
        self._files = raw.get("files") or {}
        self._prompts = raw.get("prompts") or {}
        requests = []
        for row in raw.get("requests") or []:
            try:
                requests.append(_Request(*row))
            except TypeError:
                continue  # wrong length: a damaged row, skip it
        self._requests = requests

    def _save_cache(self) -> None:
        """Write the ledger atomically, readable by this user only."""
        data = {
            "version": CACHE_VERSION,
            "files": self._files,
            "requests": self._requests,  # NamedTuples serialise as lists
            "prompts": self._prompts,
        }
        self.cache_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.cache_path.with_name(f".{CACHE_FILENAME}.{os.getpid()}.tmp")
        old_umask = os.umask(0o077)
        try:
            tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, self.cache_path)
        except OSError as e:
            # The ledger is a cache. Losing it means a rescan next time.
            _logger.debug("token ledger save failed: %s", e)
        finally:
            os.umask(old_umask)

    # -- scanning ---------------------------------------------------------

    def refresh(self, *, force: bool = False) -> None:
        """Read any transcript lines written since the last refresh.

        Throttled to one scan per :data:`REFRESH_MIN_S` unless ``force``.
        If another thread is already scanning, returns at once rather than
        waiting. Never raises: the counts are informational, so a failure is
        logged and the previous data is kept rather than breaking the caller.
        """
        now = time.time()
        if not force and now - self._last_refresh < REFRESH_MIN_S:
            return
        if not self._scan_lock.acquire(blocking=False):
            return
        try:
            self._last_refresh = now
            self._scan_all(now)
        except Exception as e:  # noqa: BLE001 - see docstring
            _logger.warning("token ledger refresh failed: %s", e)
        finally:
            self._scan_lock.release()

    def refresh_async(self) -> None:
        """Start :meth:`refresh` on a background thread, for the UI thread.

        Does nothing if a refresh ran recently or one is already running.
        """
        if time.time() - self._last_refresh < REFRESH_MIN_S or self._scan_lock.locked():
            return
        threading.Thread(target=self.refresh, daemon=True, name="cswap-tokens").start()

    def _scan_all(self, now: float) -> None:
        """Walk every transcript, read new lines, prune old data, save."""
        cutoff = now - RETENTION_S
        self._load_switches()
        if not self.projects_dir.is_dir():
            return  # Claude Code never ran here, or a custom config home

        changed = False
        paths_seen: set[str] = set()
        newest_mtime = 0.0

        for path in self.projects_dir.rglob("*.jsonl"):
            try:
                stat = path.stat()
            except OSError:
                continue  # deleted between listing and stat
            if stat.st_mtime < cutoff:
                continue  # untouched for longer than we keep history
            newest_mtime = max(newest_mtime, stat.st_mtime)

            key = str(path)
            paths_seen.add(key)
            info = self._files.get(key)
            file_was_replaced = info is not None and (
                info.get("ino") != stat.st_ino or stat.st_size < info.get("off", 0)
            )
            if info is None or file_was_replaced:
                # New file, or a different file now under this name (another
                # inode, or shorter than where we stopped): read it from the
                # start.
                info = {"ino": stat.st_ino, "off": 0, "n": 0, "last": None, "ids": []}
                self._files[key] = info
            if stat.st_size == info["off"]:
                continue  # nothing new
            self._scan_file(path, info)
            changed = True

        self._newest_write_ts = newest_mtime

        # Forget files that were deleted or have aged out.
        for gone in set(self._files) - paths_seen:
            del self._files[gone]
            changed = True

        if changed:
            self._prune(cutoff)
            self._save_cache()

    def _prune(self, cutoff: float) -> None:
        """Drop requests older than ``cutoff``, and prompts nothing refers to."""
        self._requests = [r for r in self._requests if r.ts >= cutoff]
        referenced = {r.prompt_key for r in self._requests}
        self._prompts = {
            key: meta
            for key, meta in self._prompts.items()
            if meta[0] >= cutoff or key in referenced
        }

    def _scan_file(self, path: Path, info: dict) -> None:
        """Read new complete lines of one transcript and record what they cost."""
        is_subagent = path.parent.name == "subagents"
        # Main transcript: <session>.jsonl. Subagent: <session>/subagents/x.jsonl.
        session = path.parent.parent.name if is_subagent else path.stem
        seen_ids: list[str] = list(info.get("ids") or [])

        with open(path, "rb") as fh:
            fh.seek(info["off"])
            data = fh.read()

        # Only whole lines. Claude Code may be partway through writing the
        # last one, and that one is read on the next refresh.
        last_newline = data.rfind(b"\n")
        if last_newline < 0:
            return
        info["off"] += last_newline + 1

        for raw_line in data[: last_newline + 1].splitlines():
            # A cheap byte test before parsing. Most lines are neither a
            # prompt nor a reply with usage, and transcripts can be large.
            # The parsed "type" below is what really decides.
            if b'"output_tokens"' not in raw_line and b'"type":"user"' not in raw_line:
                continue
            try:
                record = json.loads(raw_line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            record_type = record.get("type")
            if record_type not in ("user", "assistant"):
                continue
            ts = _parse_iso_timestamp(record.get("timestamp"))
            if ts is None:
                continue

            if record_type == "user":
                self._record_prompt(record, ts, session, info, is_subagent)
            else:
                self._record_request(record, ts, session, info, is_subagent, seen_ids)

        info["ids"] = seen_ids

    def _record_prompt(
        self, record: dict, ts: float, session: str, info: dict, is_subagent: bool
    ) -> None:
        """Start a new Prompt if this user record is something a person typed.

        In a subagent transcript, "user" records are the parent agent's
        instructions, not a person's, so they never start a prompt.
        """
        if is_subagent or not _is_user_prompt(record):
            return
        info["n"] = info.get("n", 0) + 1
        prompt_key = f"{session}:{info['n']}"
        info["last"] = prompt_key
        # Keep only the folder name of the working directory, never the full
        # path, which can contain a user name.
        cwd = record.get("cwd") or ""
        project = os.path.basename(cwd.rstrip("/"))
        self._prompts[prompt_key] = [ts, session, project]

    def _record_request(
        self,
        record: dict,
        ts: float,
        session: str,
        info: dict,
        is_subagent: bool,
        seen_ids: list[str],
    ) -> None:
        """Add the API request behind this assistant record to the ledger."""
        message = record.get("message") or {}
        usage = message.get("usage")
        model = message.get("model")
        if not isinstance(usage, dict) or model in (None, _SYNTHETIC_MODEL):
            return

        message_id = message.get("id")
        if message_id:
            if message_id in seen_ids:
                return  # another content block of a response already counted
            seen_ids.append(message_id)
            del seen_ids[:-_SEEN_IDS_PER_FILE]

        if is_subagent:
            # A subagent's file has no prompts of its own. Charge its
            # requests to the parent session's latest prompt at that time.
            prompt_key = self._prompt_before(session, ts)
        else:
            prompt_key = info.get("last")

        self._requests.append(_Request(
            ts=ts,
            session=session,
            prompt_key=prompt_key,
            model=model,
            input_tokens=int(usage.get("input_tokens") or 0),
            cache_read=int(usage.get("cache_read_input_tokens") or 0),
            cache_write=int(usage.get("cache_creation_input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
        ))

    def _prompt_before(self, session: str, ts: float) -> str | None:
        """Key of the latest prompt in ``session`` sent at or before ``ts``."""
        best_ts: float | None = None
        best_key: str | None = None
        for key, (prompt_ts, prompt_session, _project) in self._prompts.items():
            if prompt_session != session or prompt_ts > ts:
                continue
            if best_ts is None or prompt_ts > best_ts:
                best_ts, best_key = prompt_ts, key
        return best_key

    def _load_switches(self) -> None:
        """Re-read every account switch from claude-swap.log.

        ``switcher.py`` logs these lines, and ``logging_config`` stamps them
        with logging's default ``%(asctime)s``: local time, no zone. They are
        parsed as local time to match.
        """
        switches: list[tuple[float, str, str]] = []
        try:
            with open(self.log_path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    match = _SWITCH_LINE.match(line)
                    if not match:
                        continue
                    when = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
                    switches.append((when.timestamp(), match.group(2), match.group(3)))
        except OSError:
            pass  # no log yet: every request goes to the current account
        self._switches = switches

    # -- queries ----------------------------------------------------------

    def account_at(self, ts: float, current: str | None = None) -> str | None:
        """Which cswap slot was active at ``ts``?

        Before the first logged switch, the answer is that switch's *from*
        account. With no switches logged at all, it is ``current``.
        """
        account = self._switches[0][1] if self._switches else current
        for switch_ts, _from_slot, to_slot in self._switches:
            if switch_ts > ts:
                break
            account = to_slot
        return account

    def seconds_since_write(self, now: float | None = None) -> float | None:
        """Seconds since any transcript was last written, or None if none seen.

        Claude Code appends to the transcript as a reply streams in, so a
        small number here means a reply is probably being written right now.
        """
        if not self._newest_write_ts:
            return None
        return max(0.0, (now or time.time()) - self._newest_write_ts)

    def model_last_seen(self, name: str) -> float | None:
        """When was a model whose id contains ``name`` last used?

        ``name`` is a per-model window name such as ``"Fable"``, which
        matches ids like ``claude-fable-5-2``. Case is ignored.
        """
        needle = name.lower()
        latest: float | None = None
        for request in self._requests:
            if needle in (request.model or "").lower():
                if latest is None or request.ts > latest:
                    latest = request.ts
        return latest

    def recent_peak_context(self, within_s: float, now: float | None = None) -> int:
        """Largest current context among sessions active in the last ``within_s``.

        For each session with a request in that span, take its *latest*
        request's size (that is how big the conversation is now, not how big
        it once was), then return the largest of those. 0 if none.
        """
        since = (now or time.time()) - within_s
        latest_by_session: dict[str, _Request] = {}
        for request in self._requests:
            if request.ts < since:
                continue
            previous = latest_by_session.get(request.session)
            if previous is None or request.ts > previous.ts:
                latest_by_session[request.session] = request
        return max((r.sent for r in latest_by_session.values()), default=0)

    def prompts(self, since: float = 0.0, current: str | None = None) -> list[Prompt]:
        """Every prompt with a request at or after ``since``, oldest first.

        ``current`` is the active slot. It is used to credit requests when
        claude-swap.log has no switches (see :meth:`account_at`).
        """
        by_key: dict[str, Prompt] = {}
        for request in self._requests:
            if request.ts < since:
                continue
            # A request before the session's first recognised prompt (e.g. a
            # transcript first read mid-session) collects under "<session>:0".
            key = request.prompt_key or f"{request.session}:0"
            prompt = by_key.get(key)
            if prompt is None:
                prompt_ts, session, project = self._prompts.get(key) or [
                    request.ts, request.session, ""
                ]
                prompt = by_key[key] = Prompt(
                    key=key,
                    session=session,
                    ts=prompt_ts,
                    project=project,
                    account=self.account_at(prompt_ts, current),
                )
            prompt.calls += 1
            prompt.sent += request.sent
            prompt.received += request.output_tokens
            prompt.cache_read += request.cache_read
            prompt.peak_context = max(prompt.peak_context, request.sent)
            model = request.model or "?"
            prompt.models[model] = prompt.models.get(model, 0) + 1
        return sorted(by_key.values(), key=lambda p: p.ts)

    def totals(self, since: float, current: str | None = None) -> dict[str, dict]:
        """Per-slot totals since ``since``: ``{slot: {sent, received, calls, prompts, cache_read}}``.

        Prompts with no known account are listed under ``"?"``.
        """
        def empty() -> dict:
            return {"sent": 0, "received": 0, "calls": 0, "prompts": 0, "cache_read": 0}

        by_account: dict[str, dict] = defaultdict(empty)
        for prompt in self.prompts(since, current):
            row = by_account[prompt.account or "?"]
            row["sent"] += prompt.sent
            row["received"] += prompt.received
            row["calls"] += prompt.calls
            row["prompts"] += 1
            row["cache_read"] += prompt.cache_read
        return dict(by_account)


# One Ledger per backup root, shared by everything in this process.
_LEDGERS: dict[str, Ledger] = {}
_LEDGERS_LOCK = threading.Lock()


def ledger_for(backup_root: Path) -> Ledger:
    """Return the shared :class:`Ledger` for ``backup_root``, creating it once."""
    with _LEDGERS_LOCK:
        key = str(backup_root)
        if key not in _LEDGERS:
            _LEDGERS[key] = Ledger(backup_root)
        return _LEDGERS[key]


def human(n: int | float) -> str:
    """Short token count: 1234567 -> "1.2M", 45200 -> "45.2k", 812 -> "812"."""
    n = float(n)
    for suffix, divisor in (("M", 1e6), ("k", 1e3)):
        if n >= divisor:
            return f"{n / divisor:.1f}{suffix}"
    return f"{n:.0f}"


def short_model(model: str | None) -> str:
    """Short model name for tables: "claude-opus-5-5" -> "opus 5.5"."""
    if not model:
        return "?"
    name = model.removeprefix("claude-")
    return re.sub(r"-(\d+)-(\d+)$", r" \1.\2", name)
