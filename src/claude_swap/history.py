"""Usage history: every usage reading cswap takes, kept past the moment.

``cache/usage.json`` holds one reading per account and is overwritten on every
fetch, and the log rotates away after a few megabytes, so on its own cswap
cannot say how fast the accounts' weekly limits are being used, or whether
there will be room left by Thursday. This module keeps that history, inside
the backup root (``<backup_root>/history/``), owner-only like the rest of it:

``usage-YYYY-MM.jsonl``
    One row per account each time its reading changes, plus one every
    ``HEARTBEAT_S`` while it does not, so "held steady" is never confused with
    "no data". Written by :class:`UsageHistory`, which :class:`UsageStore`
    calls after every write of a new good reading (its ``record`` and
    ``adopt``), so ``cswap auto``, ``cswap list``, the TUI and the menu bar all
    feed it without knowing about it. ``prev`` is the account's previous
    successful check: unchanged readings are not written, so ``prev`` is what
    dates a change -- the use behind it happened between ``prev`` and ``ts``.

``state.json``
    What the recorder last wrote per slot, so an unchanged reading is not
    written again, and when the history started.

Rows never carry an email address. A row names its slot number and, when
cswap knows it, the account's stable ``uuid`` from ``sequence.json``; that is
enough to follow an account through ``cswap swap``/``move`` and to tell a
reused slot number's readings apart, and a history file can be shared in a
bug report without exposing who the accounts belong to.

Recording is best-effort by construction: it runs after the usage store has
released its lock, never raises into the collector, and gives up after a
short wait for its own lock rather than stall a poll. A missed row costs a
point on a chart; a stalled collector costs a switch.

Bounded: whole monthly files older than ``history.retentionDays`` are deleted,
checked at most once a day. ``history.enabled false`` stops recording (what
is already recorded stays, and ``cswap history`` still shows it).

All times in the files are UTC ISO-8601.
"""

from __future__ import annotations

import collections
import contextlib
import dataclasses
import hashlib
import json
import logging
import os
import sys
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from claude_swap.locking import FileLock
from claude_swap.settings import atomic_write_json, load_history_settings

_logger = logging.getLogger("claude-swap")

HISTORY_DIRNAME = "history"
STATE_FILENAME = "state.json"
STATE_SCHEMA_VERSION = 1

# An unchanged reading is still written at least this often, so a reader can
# tell "held steady" apart from "nobody was checking".
HEARTBEAT_S = 6 * 3600.0
# Readings further apart than this are a gap, not a held value: one missed
# heartbeat plus an hour of slack for a slow poll cadence.
GAP_S = HEARTBEAT_S + 3600.0
# A `recorded` time further ahead of the clock than this was stamped by a clock
# that has since been corrected (or by another machine): it is ignored rather
# than left to block every reading until real time catches up with it.
CLOCK_SKEW_S = 300.0
# A failure to write the history is logged as a warning at most this often;
# the repeats (one per poll, from every process) go to the debug log.
WARN_EVERY_S = 3600.0
# How long a recorder waits for another process's history write before
# skipping its own. A history write is a few small appends; anything longer
# means something is wrong, and the collector must not wait on it.
LOCK_TIMEOUT_S = 2.0
DAY_S = 86400.0
WEEK_S = 7 * DAY_S


# ---------------------------------------------------------------------------
# small helpers


def _num(value: object) -> float | None:
    """A finite number, or None. Booleans are not numbers here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    f = _num(value)
    return int(f) if f is not None and f.is_integer() else None


def _clean_pct(value: float | None) -> float | int | None:
    """26.0 -> 26, 12.345 -> 12.35: smaller files, same meaning."""
    if value is None:
        return None
    return int(value) if float(value).is_integer() else round(value, 2)


def iso_utc(ts: float, minutes: bool = False) -> str:
    fmt = "%Y-%m-%dT%H:%MZ" if minutes else "%Y-%m-%dT%H:%M:%SZ"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(fmt)


def parse_iso(value: object) -> float | None:
    """Epoch seconds from an ISO-8601 string; a naive one is taken as UTC."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _round_minute(ts: float) -> float:
    # Reset times jitter by fractions of a second between polls (07:59:59.9
    # vs 08:00:00), so they are stored to the nearest minute: otherwise every
    # poll would look like a changed reading.
    return round(ts / 60.0) * 60.0


def _window(entry: object) -> tuple[float | None, str | None]:
    if not isinstance(entry, dict):
        return None, None
    pct = _num(entry.get("pct"))
    reset = parse_iso(entry.get("resets_at"))
    return pct, (iso_utc(_round_minute(reset), minutes=True) if reset is not None else None)


def usage_row(
    acct: int,
    usage: object,
    fetched_at: float,
    *,
    account_id: str | None = None,
) -> dict | None:
    """One history row from a stored ``lastGood`` usage dict, or None when it
    holds no 5-hour or 7-day figure (an API-key account, an empty response)."""
    if not isinstance(usage, dict):
        return None
    h5, h5_reset = _window(usage.get("five_hour"))
    d7, d7_reset = _window(usage.get("seven_day"))
    if h5 is None and d7 is None:
        return None
    row: dict = {"ts": iso_utc(fetched_at), "acct": acct}
    if account_id:
        row["id"] = account_id
    row["h5"] = _clean_pct(h5)
    if h5_reset:
        row["h5_reset"] = h5_reset
    row["d7"] = _clean_pct(d7)
    if d7_reset:
        row["d7_reset"] = d7_reset
    models: dict[str, dict] = {}
    for scoped in usage.get("scoped") or []:
        if not isinstance(scoped, dict) or not isinstance(scoped.get("name"), str):
            continue
        pct, reset = _window(scoped)
        if pct is None:
            continue
        models[scoped["name"]] = {"pct": _clean_pct(pct), **({"reset": reset} if reset else {})}
    if models:
        row["models"] = models
    spend = usage.get("spend")
    if isinstance(spend, dict) and _num(spend.get("pct")) is not None:
        row["spend"] = {k: spend[k] for k in ("used", "limit", "pct", "currency") if k in spend}
    return row


def _signature(row: dict) -> str:
    """Everything about a reading except when it was taken."""
    body = {k: v for k, v in row.items() if k not in ("ts", "prev")}
    return hashlib.sha1(
        json.dumps(body, sort_keys=True).encode("utf-8"), usedforsecurity=False
    ).hexdigest()[:16]


def iter_jsonl(path: Path) -> Iterator[dict]:
    """The records of a JSONL file, one at a time; a torn last line (a crash
    mid-append) and anything that is not an object are skipped."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    yield rec
    except (FileNotFoundError, NotADirectoryError):
        return


def ensure_private_dir(path: Path) -> None:
    """Create the history directory owner-only, like the backup root."""
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass  # not ours to narrow (a symlinked dir elsewhere); files stay 0600


def _append_lines(path: Path, lines: list[str]) -> None:
    """Append whole lines to ``path``, created 0600.

    One ``write`` per call on an O_APPEND descriptor, so a concurrent reader
    sees either none of the batch or all of it on every platform cswap runs
    on. A line a crash left unfinished is ended first, so it cannot swallow
    the next record; the reader skips the torn one.
    """
    data = "".join(lines).encode("utf-8")
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    try:
        size = os.fstat(fd).st_size
        if size:
            with open(path, "rb") as f:
                f.seek(size - 1)
                if f.read(1) != b"\n":
                    data = b"\n" + data
        os.write(fd, data)
    finally:
        os.close(fd)


def _month_files(directory: Path, prefix: str) -> list[tuple[str, Path]]:
    """``(YYYY-MM, path)`` for each ``<prefix>-YYYY-MM.jsonl``, oldest first."""
    out = []
    try:
        entries = list(directory.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return []
    for p in entries:
        name = p.name
        if not (name.startswith(prefix + "-") and name.endswith(".jsonl")):
            continue
        month = name[len(prefix) + 1 : -len(".jsonl")]
        if len(month) == 7 and month[4] == "-" and month[:4].isdigit() and month[5:].isdigit():
            out.append((month, p))
    return sorted(out)


def _month_end(month: str) -> float:
    """Epoch of the first instant after ``YYYY-MM`` (UTC)."""
    year, mon = int(month[:4]), int(month[5:])
    year, mon = (year + 1, 1) if mon == 12 else (year, mon + 1)
    return datetime(year, mon, 1, tzinfo=timezone.utc).timestamp()


# ---------------------------------------------------------------------------
# recording

_last_warning = 0.0


def _warn_limited(message: str) -> None:
    """A warning at most every ``WARN_EVERY_S`` per process. An unwritable
    history fails on every poll; one line an hour says so, the rest would
    bury the log."""
    global _last_warning
    now = time.monotonic()
    if _last_warning and now - _last_warning < WARN_EVERY_S:
        _logger.debug(message)
        return
    _last_warning = now
    _logger.warning(message)



@dataclass(frozen=True)
class NewReading:
    """A good reading the usage store has just written, as the recorder needs it.

    ``prev_fetched_at`` is the store's previous ``fetchedAt`` for the same
    account (None when there was none, or it belonged to another account in
    the same slot): the exact time of the check before this one.
    """

    number: str
    usage: dict
    fetched_at: float
    prev_fetched_at: float | None


class UsageHistory:
    """Appends readings to the monthly files under ``history/``.

    ``account_ids`` returns slot number -> stable account uuid (from
    ``sequence.json``); it is called once per batch, and only when there is
    something to record. ``settings_root`` is where ``settings.json`` lives
    (the backup root); ``enabled``/``retentionDays`` are re-read per batch so
    ``cswap config set history.enabled false`` takes effect on a running
    ``cswap auto`` without a restart.
    """

    def __init__(
        self,
        backup_root: Path,
        account_ids: Callable[[], Mapping[str, str]] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.backup_root = backup_root
        self.dir = backup_root / HISTORY_DIRNAME
        self._account_ids = account_ids
        self.clock = clock

    # -- plumbing -------------------------------------------------------------

    @property
    def state_path(self) -> Path:
        return self.dir / STATE_FILENAME

    def _lock(self) -> FileLock:
        return FileLock(self.dir / ".history.lock", timeout=LOCK_TIMEOUT_S)

    def _read_state(self) -> dict:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            raw = None
        if not isinstance(raw, dict) or raw.get("schemaVersion") != STATE_SCHEMA_VERSION:
            raw = {"schemaVersion": STATE_SCHEMA_VERSION}
        if not isinstance(raw.get("accounts"), dict):
            raw["accounts"] = {}
        return raw

    def _write_state(self, state: dict) -> None:
        atomic_write_json(self.state_path, state)

    @contextlib.contextmanager
    def _locked(self) -> Iterator[bool]:
        """Hold the history lock, yielding False (and holding nothing) when
        another writer kept it past ``LOCK_TIMEOUT_S``."""
        ensure_private_dir(self.dir)
        lock = self._lock()
        got = lock.acquire()
        try:
            yield got
        finally:
            if got:
                lock.release()

    def _ids(self) -> Mapping[str, str]:
        if self._account_ids is None:
            return {}
        try:
            return self._account_ids() or {}
        except Exception as e:  # a torn sequence.json must not cost the reading
            _logger.debug("history: could not read account ids: %r", e)
            return {}

    # -- writers ------------------------------------------------------------

    def record_readings(self, readings: Iterable[NewReading]) -> int:
        """Append the readings that changed (or are due a heartbeat); returns
        how many rows were written. Never raises."""
        try:
            return self._record_readings(list(readings))
        except Exception as e:
            _warn_limited(f"Usage history: could not record readings: {e!r}")
            return 0

    def _record_readings(self, readings: list[NewReading]) -> int:
        if not readings:
            return 0
        settings = load_history_settings(self.backup_root)
        if not settings.enabled:
            return 0
        ids = self._ids()
        with self._locked() as got:
            if not got:
                _logger.debug("history: lock busy; %d reading(s) not recorded", len(readings))
                return 0
            state = self._read_state()
            seen: dict = state["accounts"]
            by_month: dict[str, list[str]] = collections.defaultdict(list)
            for reading in sorted(readings, key=lambda r: r.fetched_at):
                acct = _as_int(reading.number)
                if acct is None:
                    continue
                account_id = ids.get(reading.number) or None
                row = usage_row(acct, reading.usage, reading.fetched_at, account_id=account_id)
                if row is None:
                    continue
                last = seen.get(reading.number)
                last = last if isinstance(last, dict) else {}
                recorded = _num(last.get("recorded"))
                if recorded is not None and recorded > self.clock() + CLOCK_SKEW_S:
                    recorded = None  # stamped by a clock that ran ahead: start over
                if recorded is not None and reading.fetched_at <= recorded:
                    continue  # an older reading arriving late (adopt) or a replay
                sig = _signature(row)
                due = (
                    recorded is None
                    or sig != last.get("sig")
                    or reading.fetched_at - recorded >= HEARTBEAT_S
                )
                if not due:
                    continue
                prev = reading.prev_fetched_at
                if prev is not None and prev < reading.fetched_at:
                    row["prev"] = iso_utc(prev)
                by_month[row["ts"][:7]].append(json.dumps(row, separators=(",", ":")) + "\n")
                seen[reading.number] = {"recorded": reading.fetched_at, "sig": sig}
            if not by_month:
                return 0
            written = 0
            for month, lines in sorted(by_month.items()):
                _append_lines(self.dir / f"usage-{month}.jsonl", lines)
                written += len(lines)
            state.setdefault("since", iso_utc(min(r.fetched_at for r in readings)))
            self._prune_if_due(state, settings.retention_days)
            self._write_state(state)
            return written

    # -- retention ----------------------------------------------------------

    def _prune_if_due(self, state: dict, retention_days: int) -> None:
        """Delete monthly files that ended more than ``retention_days`` ago.

        Whole files only, so pruning is a directory listing and a few unlinks,
        never a rewrite; checked at most once per UTC day (``prunedOn`` in
        state.json). A month is kept while any of it is inside the window, so
        up to one extra month survives -- cheap, and never a hole at the edge.
        """
        now = self.clock()
        today = iso_utc(now)[:10]
        if state.get("prunedOn") == today:
            return
        state["prunedOn"] = today
        cutoff = now - retention_days * DAY_S
        for month, path in _month_files(self.dir, "usage"):
            if _month_end(month) <= cutoff:
                try:
                    path.unlink()
                except OSError as e:
                    _logger.debug("history: could not prune %s: %r", path.name, e)


# ---------------------------------------------------------------------------
# reading it back

@dataclass(frozen=True)
class Reading:
    """One recorded reading of one account. Percentages are of the limit used."""

    t: float
    h5: float | None
    d7: float | None
    model: float | None  # the view's chosen per-model weekly window, if any
    prev: float | None = None  # the account's previous check, when known
    d7_reset: float | None = None


@dataclass(frozen=True)
class Current:
    """An account's latest reading as the usage store holds it now."""

    t: float | None
    h5: float | None = None
    h5_reset: float | None = None
    d7: float | None = None
    d7_reset: float | None = None
    model: float | None = None
    model_reset: float | None = None
    error: str | None = None
    failures: int = 0


@dataclass(frozen=True)
class Account:
    number: int
    removed: bool = False  # in the history but no longer managed
    disabled: bool = False  # held out of rotation (``cswap disable``)
    priority: int = 0
    # False for an account with no 7-day limit to count (an API key, or one
    # whose readings carry no seven_day window): it has no weekly room.
    weekly: bool = True
    alias: str | None = None  # ``cswap alias``: the only name a view shows


@dataclass
class History:
    """Everything a history view needs, read once."""

    now: float
    accounts: list[Account]
    readings: dict[int, list[Reading]]
    current: dict[int, Current]
    active: int | None
    model_name: str | None = None
    recording_since: float | None = None
    strategy: str = "best"
    hidden_rows: int = 0  # rows from a slot's previous owner, not drawn
    demo: bool = False

    def account(self, number: int) -> Account | None:
        return next((a for a in self.accounts if a.number == number), None)


def _model_pct(models: object, name: str | None) -> float | None:
    if name and isinstance(models, dict) and isinstance(models.get(name), dict):
        return _num(models[name].get("pct"))
    return None


def _scoped(usage: object, name: str | None) -> tuple[float | None, float | None]:
    if not name or not isinstance(usage, dict):
        return None, None
    for s in usage.get("scoped") or []:
        if isinstance(s, dict) and s.get("name") == name:
            return _num(s.get("pct")), parse_iso(s.get("resets_at"))
    return None, None


def load_history(
    backup_root: Path,
    sequence: Mapping | None,
    entries: Mapping[str, object],
    *,
    now: float,
    since: float,
    strategy: str = "best",
) -> History:
    """Read the history files from ``since`` on, joined with the accounts
    cswap manages now (``sequence``) and their latest readings (``entries``:
    slot -> :class:`UsageEntry`, as ``UsageStore.entries`` returns them).

    A row is filed under the account its ``id`` (the account uuid) names,
    wherever that account sits now, so ``cswap swap``/``move`` carry each
    account's history to its new number. A row whose ``id`` no managed
    account holds (an account since removed, or a reused slot's previous
    owner) stays in the file but is counted in ``hidden_rows``, not drawn.
    Rows without an ``id`` (slots with no uuid yet) go by their slot number;
    for a slot cswap no longer manages they are marked ``removed``, which
    the all-account totals leave out.
    """
    directory = backup_root / HISTORY_DIRNAME
    seq = sequence if isinstance(sequence, Mapping) else {}
    accounts: dict[int, Account] = {}
    slot_of: dict[str, int] = {}  # account uuid -> the slot it holds now
    for key, info in (seq.get("accounts") or {}).items():
        n = _as_int(key)
        if n is None or not isinstance(info, dict):
            continue
        prio = _as_int(info.get("priority")) or 0
        accounts[n] = Account(
            n,
            disabled=bool(info.get("disabled")),
            priority=prio,
            weekly=info.get("kind") != "api_key",  # API keys bill per token
            alias=info.get("alias") if isinstance(info.get("alias"), str) else None,
        )
        uuid = info.get("uuid")
        if isinstance(uuid, str) and uuid.strip():
            slot_of[uuid.strip()] = n
    active = _as_int(seq.get("activeAccountNumber"))

    first_month = iso_utc(since - GAP_S)[:7]
    raw_rows: list[tuple[float, int, dict]] = []
    model_names: collections.Counter = collections.Counter()
    for month, path in _month_files(directory, "usage"):
        if month < first_month:
            continue
        for rec in iter_jsonl(path):
            t = parse_iso(rec.get("ts"))
            n = _as_int(rec.get("acct"))
            if t is None or n is None or t < since - GAP_S or t > now + 60:
                continue
            models = rec.get("models")
            if isinstance(models, dict):
                model_names.update(k for k, v in models.items() if isinstance(v, dict))
            raw_rows.append((t, n, rec))
    for entry in entries.values():
        for s in (getattr(entry, "last_good", None) or {}).get("scoped") or []:
            if isinstance(s, dict) and isinstance(s.get("name"), str):
                model_names[s["name"]] += 1
    model = model_names.most_common(1)[0][0] if model_names else None

    readings: dict[int, dict[int, Reading]] = collections.defaultdict(dict)
    hidden = 0
    for t, n, rec in raw_rows:
        row_id = rec.get("id")
        if isinstance(row_id, str) and row_id:
            if row_id not in slot_of:
                hidden += 1
                continue
            n = slot_of[row_id]
        prev = parse_iso(rec.get("prev"))
        readings[n][int(t)] = Reading(
            t=t,
            h5=_num(rec.get("h5")),
            d7=_num(rec.get("d7")),
            model=_model_pct(rec.get("models"), model),
            prev=prev if prev is not None and prev < t else None,
            d7_reset=parse_iso(rec.get("d7_reset")),
        )
        if n not in accounts:
            accounts[n] = Account(n, removed=True)

    current: dict[int, Current] = {}
    for key, entry in entries.items():
        n = _as_int(key)
        if n is None:
            continue
        last_good = getattr(entry, "last_good", None)
        fetched = _num(getattr(entry, "fetched_at", None))
        cur = Current(
            t=fetched,
            error=getattr(entry, "last_error", None),
            failures=int(getattr(entry, "consecutive_failures", 0) or 0),
        )
        row = usage_row(n, last_good, fetched) if fetched is not None else None
        if row is not None:
            m_pct, m_reset = _scoped(last_good, model)
            cur = Current(
                t=fetched,
                h5=_num(row.get("h5")),
                h5_reset=parse_iso(row.get("h5_reset")),
                d7=_num(row.get("d7")),
                d7_reset=parse_iso(row.get("d7_reset")),
                model=m_pct,
                model_reset=m_reset,
                error=cur.error,
                failures=cur.failures,
            )
            # The store's reading closes each account's line when it is newer
            # than the last row written (an unchanged reading is not written).
            if fetched > max(readings[n], default=float("-inf")):
                readings[n][int(fetched)] = Reading(
                    t=fetched, h5=cur.h5, d7=cur.d7, model=cur.model,
                    d7_reset=cur.d7_reset,
                )
        current[n] = cur
        # A reading with no 7-day window (an API key, a plan without one)
        # has no weekly room to count.
        if fetched is not None and cur.d7 is None and n in accounts:
            accounts[n] = dataclasses.replace(accounts[n], weekly=False)

    since_state = None
    try:
        state = json.loads((directory / STATE_FILENAME).read_text(encoding="utf-8"))
        since_state = parse_iso(state.get("since")) if isinstance(state, dict) else None
    except (OSError, ValueError, UnicodeDecodeError):
        pass
    if since_state is None:
        firsts = [rows[min(rows)].t for rows in readings.values() if rows]
        since_state = min(firsts) if firsts else None

    return History(
        now=now,
        accounts=[accounts[n] for n in sorted(accounts)],
        readings={n: [rows[t] for t in sorted(rows)] for n, rows in sorted(readings.items())},
        current=current,
        active=active,
        model_name=model,
        recording_since=since_state,
        strategy=strategy,
        hidden_rows=hidden,
    )
