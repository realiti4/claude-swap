"""Tokens against weekly percentage, per account, for the Code tab (Windows desktop-follow).

Three sources are joined:

* the proxy's request ledger (``requests.jsonl``): which account each billed
  ``/v1/messages`` request went to, and the API's request id;
* Claude Code's own transcripts (``~/.claude/projects/**/*.jsonl``): the final
  token usage of each request, its model, project and session, found by the same
  request id;
* percentage samples (``usage-samples.jsonl``) taken from cswap's usage cache
  whenever it has a new reading, since cswap itself keeps no history.

Only requests that went through the proxy are counted, from the day it started
writing the ledger.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from claude_swap import paths

WEEK = timedelta(days=7)

# Weights relative to one input token, Anthropic's published multipliers: output costs 5x,
# a cache read 0.1x, a cache write 1.25x (5 minute) or 2x (1 hour). Model prices differ and are
# not modelled: this is a comparable unit, not dollars.
WEIGHTS = {"input": 1.0, "output": 5.0, "cache_read": 0.1, "cache_5m": 1.25, "cache_1h": 2.0}
KINDS = tuple(WEIGHTS)


def state_dir() -> Path:
    return paths.get_backup_root() / "desktop-follow"


def ledger_path() -> Path:
    return state_dir() / "requests.jsonl"


def samples_path() -> Path:
    return state_dir() / "usage-samples.jsonl"


def _sampled_path() -> Path:
    return state_dir() / "usage-sampled.json"


def _usage_cache_path() -> Path:
    return paths.get_backup_root() / "cache" / "usage.json"


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def read_jsonl(path: Path) -> list[dict]:
    rows = []
    try:
        with path.open(encoding="utf-8", errors="ignore") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        pass
    return rows


# --- sampling -------------------------------------------------------------

def _window(entry) -> dict | None:
    if isinstance(entry, dict) and isinstance(entry.get("pct"), (int, float)) and entry.get("resets_at"):
        return {"pct": entry["pct"], "resets_at": entry["resets_at"]}
    return None


def sample_usage() -> int:
    """Append one line per account whose usage reading in cswap's cache is new. Returns how many."""
    try:
        accounts = json.loads(_usage_cache_path().read_text(encoding="utf-8")).get("accounts") or {}
    except (OSError, ValueError):
        return 0
    try:
        seen = json.loads(_sampled_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        seen = {}
    lines = []
    for entry in accounts.values():
        email, fetched, good = entry.get("email"), entry.get("fetchedAt"), entry.get("lastGood") or {}
        if not email or not isinstance(fetched, (int, float)) or fetched <= seen.get(email, 0):
            continue
        scoped = {s["name"]: w for s in good.get("scoped") or [] if s.get("name") and (w := _window(s))}
        spend = good.get("spend")
        lines.append({
            "ts": _iso(fetched),
            "account": email,
            "five_hour": _window(good.get("five_hour")),
            "seven_day": _window(good.get("seven_day")),
            "scoped": scoped,
            "spend": {"used": spend.get("used"), "limit": spend.get("limit")} if isinstance(spend, dict) else None,
        })
        seen[email] = fetched
    if lines:
        state_dir().mkdir(parents=True, exist_ok=True)
        with samples_path().open("a", encoding="utf-8") as f:
            f.writelines(json.dumps(line) + "\n" for line in lines)
        _sampled_path().write_text(json.dumps(seen), encoding="utf-8")
    return len(lines)


# --- reading the sources --------------------------------------------------

@dataclass
class Request:
    ts: datetime
    account: str | None
    request_id: str
    status: object
    model: str = ""
    tokens: dict = field(default_factory=lambda: dict.fromkeys(KINDS, 0))
    cwd: str = ""
    session: str = ""
    entrypoint: str = ""
    sidechain: bool = False
    counted: bool = False  # tokens known: the request id was found in a transcript

    @property
    def weighted(self) -> float:
        return sum(self.tokens[k] * WEIGHTS[k] for k in KINDS)


def transcript_root() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"


def read_transcripts(root: Path | None = None, only_ids: set[str] | None = None) -> dict[str, dict]:
    """Final usage per request id. A request writes one transcript line per content block, all with
    the same usage, and a resumed session repeats old lines, so the fullest line per id wins."""
    root = root or transcript_root()
    found: dict[str, dict] = {}
    for file in root.rglob("*.jsonl"):
        try:
            handle = file.open(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        with handle:
            for line in handle:
                if '"requestId"' not in line:
                    continue
                if only_ids is not None and not any(i in line for i in only_ids):
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                rid, message = row.get("requestId"), row.get("message") or {}
                usage = message.get("usage")
                if not rid or not isinstance(usage, dict):
                    continue
                cache = usage.get("cache_creation") or {}
                tokens = {
                    "input": usage.get("input_tokens") or 0,
                    "output": usage.get("output_tokens") or 0,
                    "cache_read": usage.get("cache_read_input_tokens") or 0,
                    "cache_5m": cache.get("ephemeral_5m_input_tokens") or 0,
                    "cache_1h": cache.get("ephemeral_1h_input_tokens") or 0,
                }
                if not cache:  # older transcripts: one total, treat it as a 5 minute write
                    tokens["cache_5m"] = usage.get("cache_creation_input_tokens") or 0
                record = {
                    "tokens": tokens,
                    "model": message.get("model") or row.get("requestedModel") or "",
                    "cwd": row.get("cwd") or "",
                    "session": row.get("sessionId") or "",
                    "entrypoint": row.get("entrypoint") or "",
                    "sidechain": bool(row.get("isSidechain")),
                }
                if rid not in found or sum(tokens.values()) > sum(found[rid]["tokens"].values()):
                    found[rid] = record
    return found


def load_requests(ledger: list[dict] | None = None, transcripts: dict[str, dict] | None = None) -> list[Request]:
    ledger = read_jsonl(ledger_path()) if ledger is None else ledger
    if transcripts is None:
        transcripts = read_transcripts(only_ids={r["request_id"] for r in ledger if r.get("request_id")})
    out, seen = [], set()
    for row in ledger:
        rid = row.get("request_id") or ""
        if rid and rid in seen:
            continue
        seen.add(rid)
        req = Request(parse_time(row["ts"]), row.get("account"), rid, row.get("status"), entrypoint=row.get("entrypoint") or "")
        found = transcripts.get(rid)
        if found:
            req.tokens, req.model, req.cwd, req.session = found["tokens"], found["model"], found["cwd"], found["session"]
            req.entrypoint, req.sidechain, req.counted = found["entrypoint"] or req.entrypoint, found["sidechain"], True
        out.append(req)
    return sorted(out, key=lambda r: r.ts)


# --- the report model -----------------------------------------------------

@dataclass
class Sample:
    ts: datetime
    five: dict | None
    week: dict | None
    scoped: dict
    spend: dict | None


@dataclass
class WeekReport:
    account: str
    start: datetime
    end: datetime
    samples: list[Sample]
    requests: list[Request]

    def _pct_delta(self, pick) -> tuple[float | None, float | None, float | None]:
        values = [(s.ts, pick(s)) for s in self.samples if pick(s) is not None]
        if not values:
            return None, None, None
        delta = 0.0
        for (_, before), (_, after) in zip(values, values[1:]):
            delta += after - before if after >= before else after  # a drop means a reset: count from zero
        return values[0][1], values[-1][1], delta

    def observed(self) -> list[Request]:
        """Requests from the first sample of the week on, the interval the percentage movement covers."""
        if not self.samples:
            return []
        first = self.samples[0].ts
        return [r for r in self.requests if r.ts >= first]

    def week_pct(self) -> tuple[float | None, float | None, float | None]:
        return self._pct_delta(lambda s: s.week["pct"] if s.week else None)

    def scoped_pct(self, name: str) -> tuple[float | None, float | None, float | None]:
        return self._pct_delta(lambda s: s.scoped[name]["pct"] if name in s.scoped else None)

    def totals(self, requests: list[Request]) -> dict:
        total = dict.fromkeys(KINDS, 0)
        for r in requests:
            for k in KINDS:
                total[k] += r.tokens[k]
        total["weighted"] = sum(total[k] * WEIGHTS[k] for k in KINDS)
        total["requests"] = len(requests)
        total["uncounted"] = sum(1 for r in requests if not r.counted)
        return total

    def per_pct(self, requests: list[Request], delta: float | None) -> float | None:
        """Input-equivalent tokens per 1% of the window, None until the percentage has moved."""
        if not delta or delta < 1:
            return None
        return self.totals(requests)["weighted"] / delta


@dataclass
class Report:
    generated: datetime
    weeks: list[WeekReport]
    requests: list[Request]
    samples: dict[str, list[Sample]]
    first_request: datetime | None


def load_samples() -> dict[str, list[Sample]]:
    by_account: dict[str, dict[str, Sample]] = defaultdict(dict)
    for row in read_jsonl(samples_path()):
        try:
            sample = Sample(parse_time(row["ts"]), row.get("five_hour"), row.get("seven_day"), row.get("scoped") or {}, row.get("spend"))
        except (KeyError, ValueError):
            continue
        by_account[row.get("account") or ""][row["ts"]] = sample
    return {a: sorted(s.values(), key=lambda x: x.ts) for a, s in by_account.items() if a}


def _window_end(sample: Sample) -> datetime | None:
    if not sample.week:
        return None
    end = parse_time(sample.week["resets_at"])
    # Resets drift by fractions of a second between readings: snap to the nearest 5 minutes.
    snapped = round(end.timestamp() / 300) * 300
    return datetime.fromtimestamp(snapped, timezone.utc)


def build_report(requests: list[Request], samples: dict[str, list[Sample]], now: datetime | None = None) -> Report:
    now = now or datetime.now(timezone.utc)
    weeks = []
    for account, rows in samples.items():
        windows: dict[datetime, list[Sample]] = defaultdict(list)
        for s in rows:
            end = _window_end(s)
            if end:
                windows[end].append(s)
        for end, in_window in sorted(windows.items()):
            start = end - WEEK
            mine = [r for r in requests if r.account == account and start <= r.ts < end]
            weeks.append(WeekReport(account, start, end, in_window, mine))
    return Report(now, weeks, requests, samples, requests[0].ts if requests else None)


# --- the command ----------------------------------------------------------

def _compact(n: float) -> str:
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= limit:
            return f"{n / limit:.1f}{suffix}"
    return f"{n:.0f}"


def summary_text(report: Report) -> str:
    """The latest week of each account: percentage, tokens, and tokens per 1%."""
    lines = []
    latest: dict[str, WeekReport] = {}
    for week in report.weeks:
        latest[week.account] = week  # weeks are in order, so the last one wins
    for account, week in sorted(latest.items()):
        start, end, delta = week.week_pct()
        seen = week.observed()
        totals = week.totals(seen)
        per = week.per_pct(seen, delta)
        lines.append(f"{account}  (week to {week.end:%b %d %H:%M} UTC)")
        lines.append(f"  weekly now      {'-' if end is None else f'{end:g}%'}   gained since first reading  {'-' if delta is None else f'{delta:g} points'}")
        lines.append(f"  tokens (in-eq.) {_compact(totals['weighted'])}   requests {totals['requests']} ({totals['uncounted']} without token data)")
        lines.append(f"  per 1%          {_compact(per) if per else 'not yet (the percentage needs to move by 1 point)'}")
        for name in sorted({n for s in week.samples for n in s.scoped}):
            _, f_end, f_delta = week.scoped_pct(name)
            fable = [r for r in seen if name.lower() in r.model.lower()]
            f_per = week.per_pct(fable, f_delta)
            lines.append(f"  {name:<15} {'-' if f_end is None else f'{f_end:g}%'} now, {_compact(week.totals(fable)['weighted'])} tokens, per 1%: {_compact(f_per) if f_per else 'not yet'}")
        lines.append("")
    return "\n".join(lines).rstrip() or "No percentage readings yet. They are taken every minute while desktop-follow runs."


def run(argv: list[str]) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="cswap usage-report",
        description="Tokens against the weekly percentage, per account, for the Code tab (needs desktop-follow).",
    )
    parser.add_argument("--html", metavar="PATH", nargs="?", const="", default="",
                        help="where to write the HTML report (default: next to the proxy log)")
    parser.add_argument("--no-html", action="store_true", help="print the summary only")
    parser.add_argument("--open", action="store_true", help="open the HTML report in the default browser")
    args = parser.parse_args(argv)

    sample_usage()
    report = build_report(load_requests(), load_samples())
    print(summary_text(report))
    if args.no_html:
        return 0
    from claude_swap.desktop_usage_html import render

    out = Path(args.html) if args.html else state_dir() / "usage-report.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(report), encoding="utf-8")
    print(f"\nReport: {out}", file=sys.stderr)
    if args.open:
        try:
            os.startfile(out)  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            import webbrowser

            webbrowser.open(out.as_uri())
    return 0
