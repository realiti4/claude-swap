"""The usage history as one self-contained HTML page (``cswap history --html``).

The page (``history_report.html``, shipped as package data) carries no
external references: its script and styles are inline, a Content Security
Policy forbids anything else, and the data goes in as two JSON payloads, the
user's history and the made-up demo behind its "Demo data" switch, so it
opens from disk in any browser, offline. It shows what the terminal view
shows, with hover readouts and a range selector:

- weekly room left: the figure now, the pace table (each pace a day, the
  accounts it would take to keep up with it, and where it leads), and the
  chart with the forecasts at recent paces, the refill line and the
  per-model weekly line;
- room on each account now.

Every number on the page is worked out here, by ``history_forecast``: the
room at evenly spaced moments across each range, the room now and the resets
ahead, the forecasts, and the account list. The page only draws them, so it
and ``cswap history`` cannot disagree.

**Staying current.** ``<backup_root>/history/report.html`` is the live copy:
once it exists, a running ``cswap auto`` rebuilds it between ticks (outside
every lock, at most once a minute, from the last 30 days unless it was
built with another ``--days``), and an open tab reloads itself while nobody
is using it. Writing it once is the opt-in; deleting it opts out. No other
cswap process touches it. A copy written with ``--out`` is a snapshot.
"""

from __future__ import annotations

import json
import logging
import os
import time
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

from claude_swap.fsutil import atomic_write_text
from claude_swap.history import HISTORY_DIRNAME, History, ensure_private_dir
from claude_swap.history_forecast import (
    DAY_S,
    FORECAST_AHEAD_S,
    Analysis,
    analyse,
    extra_accounts,
    resets_of,
    room_now_of,
    room_samples,
)
from claude_swap.settings import atomic_write_json

if TYPE_CHECKING:
    from claude_swap.switcher import ClaudeAccountSwitcher

_logger = logging.getLogger("claude-swap")

TEMPLATE_NAME = "history_report.html"
REPORT_FILENAME = "report.html"
DEMO_REPORT_FILENAME = "demo-report.html"
# The --days the live copy was last built with, which its rebuilds keep.
REPORT_SETTINGS_FILENAME = "report.json"
# Touched when a rebuild fails, so the next attempt waits like a success would.
FAILED_MARKER = ".report-failed"
DATA_PLACEHOLDER = "/*__CSWAP_HISTORY_DATA__*/null"
DEMO_PLACEHOLDER = "/*__CSWAP_HISTORY_DEMO__*/null"
# The live copy is rebuilt at most this often.
LIVE_MIN_INTERVAL_S = 60.0
# The live copy's range unless it was built with --days: a month of history
# keeps every rebuild small and quick, however much history is kept.
LIVE_DEFAULT_DAYS = 30.0
# The page's range buttons (history_report.html's RANGES), and the samples
# drawn across each: one per 2 pixels of a wide chart.
RANGES = (("1d", DAY_S), ("7d", 7 * DAY_S), ("30d", 30 * DAY_S), ("all", None))
SAMPLES = 721

_demo_cache: dict | None = None


def default_report_path(backup_root: Path) -> Path:
    return backup_root / HISTORY_DIRNAME / REPORT_FILENAME


def _num(v: float | None, digits: int = 2) -> float | int | None:
    """Short JSON: 12.0 -> 12, 0.123456 -> 0.12."""
    if v is None:
        return None
    r = round(float(v), digits)
    return int(r) if r.is_integer() else r


def _t(v: float | None) -> int | None:
    return None if v is None else int(v)


def _range_start(span: float | None, now: float, data_start: float) -> float:
    """The page's ``range()``: where a range button's chart starts."""
    start = max(now - span, data_start) if span else data_start
    return min(start, now - 3600)


def report_payload(
    history: History,
    *,
    days: float | None,
    live: bool = False,
    analysis: Analysis | None = None,
) -> dict:
    """The page's data, in the shape ``history_report.html`` reads."""
    a = analysis or analyse(history)
    now = history.now
    since = now - days * DAY_S if days else None
    starts = [rows[0].t for n, rows in history.readings.items() if rows and n in a.fleet]
    data_start = min(starts) if starts else now
    if since is not None:
        data_start = max(data_start, since)

    series = {}
    room_now, k = room_now_of(history, a.fleet, "d7")
    model_now, _ = room_now_of(history, a.fleet, "model")
    for rid, span in RANGES:
        if span is not None and days and span > days * DAY_S + 60:
            continue  # the page offers only the ranges the data covers
        samples = room_samples(history, a.fleet, _range_start(span, now, data_start), now, SAMPLES)
        # The last sample is the headline figure itself.
        samples[-1] = (now, room_now, model_now if history.model_name else None, k)
        series[rid] = [[_t(t), _num(d7, 3), _num(m, 3), n] for t, d7, m, n in samples]
    until = now + FORECAST_AHEAD_S + 3600

    def resets(key: str) -> list[dict]:
        return [{"t": _t(e.t), "n": e.number, "frees": _num(e.frees, 4)}
                for e in resets_of(history, a.fleet, key, until)]

    forecasts = []
    for f in a.forecasts:
        item: dict = {"id": f.pace.key, "ok": f.ok}
        if f.ok and f.sim is not None:
            item.update({
                "rate": f.rate,
                "extra": extra_accounts(f.per_day, a.room_now.counted),
                "out": _t(f.sim.out),
                "low": {"t": _t(f.sim.low[0]), "v": _num(f.sim.low[1], 4)},
                "path": [[_t(t), _num(v, 4)] for t, v in f.sim.path],
            })
        else:
            item.update({"reason": f.reason, "since": _t(f.since), "needs": _num(f.needs, 0)})
        forecasts.append(item)

    return {
        "generatedAt": _t(now),
        "rangeDays": days,
        "demo": history.demo,
        "live": live,
        "dataStart": _t(data_start),
        "recordingSince": _t(history.recording_since),
        "modelName": history.model_name,
        "activeNow": history.active,
        "hiddenReadings": history.hidden_rows,
        "accounts": [
            {"acct": acct.number, "label": acct.alias, "removed": acct.removed}
            for acct in history.accounts
        ],
        "fleet": a.fleet,
        "counted": a.room_now.counted,
        "room": {
            "now": {"d7": _num(room_now, 4), "m": _num(model_now, 4) if history.model_name else None,
                    "k": k},
            "series": series,
            "resets": {"d7": resets("d7"), "m": resets("model") if history.model_name else []},
        },
        "board": [
            {"n": r.number, "active": r.active, "disabled": r.disabled, "weekly": r.weekly,
             "counted": r.counted, "d7": _num(r.d7), "h5": _num(r.h5), "m": _num(r.model),
             "d7Reset": _t(r.d7_reset), "t": _t(r.fetched_at), "staleReset": r.stale_reset,
             "error": r.error}
            for r in a.accounts_now
        ],
        "forecasts": forecasts,
        "autoswitch": {"strategy": history.strategy},
    }


def _embed(payload: dict | None) -> str:
    # "<" escaped so no string in the data (an alias, an error) can close the
    # <script> element; NaN/inf refused so the page's JSON.parse never chokes.
    return json.dumps(payload, separators=(",", ":"), allow_nan=False).replace("<", "\\u003c")


def render_report(real: dict | None, demo: dict | None) -> str:
    template = resources.files("claude_swap").joinpath(TEMPLATE_NAME).read_text(encoding="utf-8")
    for placeholder in (DATA_PLACEHOLDER, DEMO_PLACEHOLDER):
        if template.count(placeholder) != 1:
            raise RuntimeError(f"{TEMPLATE_NAME} is missing its {placeholder} placeholder")
    return template.replace(DATA_PLACEHOLDER, _embed(real)).replace(DEMO_PLACEHOLDER, _embed(demo))


def demo_payload(now: float) -> dict:
    """The demo behind the page's switch, built once per process: four weeks
    of 15 accounts is the costliest part of a build, and a long-running
    ``cswap auto`` would otherwise rebuild the same made-up data every
    minute. It is complete in itself (its own "now"), so reusing it is safe."""
    global _demo_cache
    if _demo_cache is None:
        from claude_swap.history_demo import demo_history

        _demo_cache = report_payload(demo_history(now), days=None)
    return _demo_cache


def _report_days(backup_root: Path) -> float | None:
    try:
        raw = json.loads((backup_root / HISTORY_DIRNAME / REPORT_SETTINGS_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return LIVE_DEFAULT_DAYS
    days = raw.get("days") if isinstance(raw, dict) else None
    if isinstance(days, (int, float)) and not isinstance(days, bool) and days > 0:
        return float(days)
    return LIVE_DEFAULT_DAYS


def write_report(
    switcher: ClaudeAccountSwitcher,
    *,
    days: float | None,
    out: Path | None = None,
    now: float | None = None,
) -> tuple[Path, dict]:
    """Build the report and write it: to the live copy when ``out`` is None
    (remembering ``days`` for its rebuilds; 30 when not given), else to
    ``out`` as a snapshot (all of the history when ``days`` is None).
    Returns the path written and the payload."""
    now = time.time() if now is None else now
    root = switcher.backup_dir
    live_path = default_report_path(root)
    live = out is None or Path(out).resolve() == live_path.resolve()
    if live and days is None:
        days = LIVE_DEFAULT_DAYS
    # Paces look back a day whatever the range.
    history = switcher.usage_history(
        since=now - max(days, 1.0) * DAY_S if days else 0.0, now=now
    )
    payload = report_payload(history, days=days, live=live)
    html = render_report(payload, demo_payload(now))
    if live:
        ensure_private_dir(live_path.parent)
        atomic_write_text(live_path, html)
        atomic_write_json(live_path.parent / REPORT_SETTINGS_FILENAME, {"days": days})
        return live_path, payload
    # A path the user typed: write there, never through a link planted in it.
    target = Path(out).expanduser()
    atomic_write_text(target, html, follow_symlinks=False)
    return target, payload


def write_demo_report(out: Path, now: float | None = None) -> Path:
    """The demo alone, for ``cswap history --html --demo``."""
    now = time.time() if now is None else now
    atomic_write_text(out, render_report(None, demo_payload(now)), follow_symlinks=False)
    return out


def refresh_live_report(switcher: ClaudeAccountSwitcher, now: float | None = None) -> bool:
    """Rebuild the live copy if it exists and its last build (or failed
    attempt) is over a minute old. ``cswap auto`` calls this between ticks,
    with no lock held; it never raises, and until someone has built the
    report once it is a ``stat`` and nothing more."""
    now = time.time() if now is None else now
    path = default_report_path(switcher.backup_dir)
    marker = path.parent / FAILED_MARKER
    try:
        last = path.stat().st_mtime
    except OSError:
        return False  # not built yet (or unreadable): nothing to keep current
    try:
        last = max(last, marker.stat().st_mtime)
    except OSError:
        pass
    if now - last < LIVE_MIN_INTERVAL_S:
        return False
    try:
        write_report(switcher, days=_report_days(switcher.backup_dir), now=now)
    except Exception as e:
        _logger.warning("Usage history: could not rebuild %s: %r", path.name, e)
        try:
            marker.touch()
            os.utime(marker, (now, now))
        except OSError:
            pass
        return False
    try:
        marker.unlink()
    except OSError:
        pass
    return True
