"""Estimated token spend per account — backlog item 4's engine.

Claude Code writes one transcript line per assistant message under
``~/.claude/projects/``, each carrying the message's token usage. claude-swap
swaps credentials in place, so every session writes to that same tree no
matter which account was active — attribution comes from joining each
message's timestamp against the switch timeline the switcher logs
("Switched from account X to Y" in claude-swap.log).

The dollar figure is an ESTIMATE from public per-token list prices
(models.dev snapshot, dated below). Subscription plans don't bill per token;
never present this as billing truth — it answers "which account burns most"
and "what would this cost on the API", nothing more.
"""

import json
import re
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# USD per million tokens, from models.dev (the same catalog CodexBar prices
# from). ``cache_write`` is the 5-minute-TTL write rate (1.25x input);
# 1-hour-TTL writes bill at 2x input, derived at pricing time. Models absent
# here are counted but reported as unpriced — never guessed.
PRICE_TABLE_DATE = "2026-08-28"
PRICE_TABLE_SOURCE = "models.dev"
PRICES: dict[str, dict[str, float]] = {
    "claude-fable-5": {"input": 10.0, "output": 50.0, "cache_read": 1.0, "cache_write": 12.5},
    "claude-opus-5": {"input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25},
    "claude-sonnet-5": {"input": 2.0, "output": 10.0, "cache_read": 0.2, "cache_write": 2.5},
    "claude-opus-4-8": {"input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25},
    "claude-opus-4-7": {"input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25},
    "claude-opus-4-6": {"input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25},
    "claude-opus-4-5": {"input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25},
    "claude-sonnet-4-5": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0, "cache_read": 0.1, "cache_write": 1.25},
}

_SWITCH_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ - .*"
    r"Switched from account (\d+) to (\d+)"
)
_STAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ - ")


def _local_epoch(stamp: str) -> float:
    """Log timestamps are naive local time; interpret them in this machine's
    zone (the transcripts' UTC stamps join against the same clock)."""
    return datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").timestamp()


@dataclass
class SwitchTimeline:
    """Who was active when, from the switcher's log lines.

    ``events`` is (epoch, account-active-FROM-then), ascending. Before the
    first switch the first line's from-account applies, but only back to
    ``coverage_start`` (the oldest log line of any kind) — the log can't
    speak for time before it existed.
    """

    events: list[tuple[float, int]] = field(default_factory=list)
    first_from: int | None = None
    coverage_start: float | None = None

    def account_at(self, epoch: float) -> int | None:
        if self.events:
            i = bisect_right(self.events, (epoch, float("inf")))
            if i > 0:
                return self.events[i - 1][1]
            if self.coverage_start is not None and epoch >= self.coverage_start:
                return self.first_from
            return None
        # No switches logged at all: the log's whole span is one account,
        # but we don't know which from the log alone.
        return None


def parse_switch_timeline(log_texts: list[str]) -> SwitchTimeline:
    """Build the timeline from log file contents, oldest file first."""
    tl = SwitchTimeline()
    for text in log_texts:
        for line in text.splitlines():
            if tl.coverage_start is None:
                m = _STAMP_RE.match(line)
                if m:
                    tl.coverage_start = _local_epoch(m.group(1))
            m = _SWITCH_RE.match(line)
            if not m:
                continue
            ts = _local_epoch(m.group(1))
            if tl.first_from is None:
                tl.first_from = int(m.group(2))
            tl.events.append((ts, int(m.group(3))))
    tl.events.sort()
    return tl


def read_switch_logs(backup_dir: Path) -> list[str]:
    """claude-swap.log plus its rotations, oldest first."""
    paths = sorted(
        backup_dir.glob("claude-swap.log*"),
        key=lambda p: p.name,
        reverse=True,  # .log.2, .log.1, .log — oldest first
    )
    out = []
    for p in paths:
        try:
            out.append(p.read_text(errors="replace"))
        except OSError:
            continue
    return out


@dataclass
class Message:
    epoch: float
    model: str
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0


def _parse_usage_line(line: str) -> tuple[str, Message] | None:
    try:
        d = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    m = d.get("message")
    if not isinstance(m, dict):
        return None
    u = m.get("usage")
    ts = d.get("timestamp")
    if not isinstance(u, dict) or not m.get("id") or not isinstance(ts, str):
        return None
    try:
        epoch = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
    write_total = int(u.get("cache_creation_input_tokens") or 0)
    cc = u.get("cache_creation")
    if isinstance(cc, dict):
        w1h = int(cc.get("ephemeral_1h_input_tokens") or 0)
        w5m = int(cc.get("ephemeral_5m_input_tokens") or 0)
        # Trust the breakdown when it accounts for the total; else fall
        # back to pricing everything at the cheaper 5m rate.
        if w1h + w5m != write_total:
            w1h, w5m = 0, write_total
    else:
        w1h, w5m = 0, write_total
    return str(m["id"]), Message(
        epoch=epoch,
        model=str(m.get("model") or ""),
        input=int(u.get("input_tokens") or 0),
        output=int(u.get("output_tokens") or 0),
        cache_read=int(u.get("cache_read_input_tokens") or 0),
        cache_write_5m=w5m,
        cache_write_1h=w1h,
    )


def scan_transcripts(projects_dir: Path, since_epoch: float):
    """Yield one ``Message`` per unique message id, bounded by the window.

    Streaming: one pass per file, a cheap substring gate before any JSON
    parse, and files untouched since the window start are skipped outright
    (append-only logs — an old mtime means no in-window lines). Duplicate
    ids within a file (streamed chunks re-log the message) collapse
    keep-last; the dedupe dict is per-file so memory stays bounded.
    """
    if not projects_dir.is_dir():
        return
    for path in projects_dir.rglob("*.jsonl"):
        try:
            if path.stat().st_mtime < since_epoch:
                continue
            with open(path, errors="replace") as fh:
                per_file: dict[str, Message] = {}
                for line in fh:
                    if '"usage"' not in line:
                        continue
                    parsed = _parse_usage_line(line)
                    if parsed is None or parsed[1].epoch < since_epoch:
                        continue
                    per_file[parsed[0]] = parsed[1]  # keep-last per id
        except OSError:
            continue
        yield from per_file.values()


def _price_for(model: str) -> dict[str, float] | None:
    """Rates for a model id, tolerating variant suffixes.

    ``claude-fable-5[1m]`` (long-context variant) prices at base rates,
    ``claude-haiku-4-5-20251001`` falls back to ``claude-haiku-4-5``.
    """
    base = model.split("[", 1)[0]
    if base in PRICES:
        return PRICES[base]
    trimmed = re.sub(r"-\d{8}$", "", base)
    return PRICES.get(trimmed)


def _cost_usd(msg: Message, rates: dict[str, float]) -> float:
    return (
        msg.input * rates["input"]
        + msg.output * rates["output"]
        + msg.cache_read * rates["cache_read"]
        + msg.cache_write_5m * rates["cache_write"]
        + msg.cache_write_1h * rates["input"] * 2.0  # 1h TTL writes: 2x input
    ) / 1_000_000


def _empty_bucket() -> dict:
    return {
        "estimatedUSD": 0.0, "messages": 0,
        "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0,
        "models": {},
    }


def build_report(
    messages,
    timeline: SwitchTimeline,
    *,
    days: int,
    labels: dict[int, dict] | None = None,
) -> dict:
    """Aggregate messages into the per-account report dict.

    ``labels`` maps account number -> {"email": ..., "alias": ...} for
    display; unknown numbers still get a bucket.
    """
    accounts: dict[int, dict] = {}
    unattributed = _empty_bucket()
    unpriced_tokens = 0
    unpriced_models: set[str] = set()
    # (local date, account number | None) -> spend/count, for the app's
    # daily chart. Local dates, matching how the user thinks about "today".
    daily: dict[tuple[str, int | None], dict] = {}

    for msg in messages:
        num = timeline.account_at(msg.epoch)
        bucket = accounts.setdefault(num, _empty_bucket()) if num is not None else unattributed
        rates = _price_for(msg.model)
        cost = _cost_usd(msg, rates) if rates else 0.0
        total_tokens = (
            msg.input + msg.output + msg.cache_read
            + msg.cache_write_5m + msg.cache_write_1h
        )
        if rates is None and total_tokens:
            unpriced_tokens += total_tokens
            unpriced_models.add(msg.model or "(unknown)")
        for b in (bucket,):
            b["estimatedUSD"] += cost
            b["messages"] += 1
            b["input"] += msg.input
            b["output"] += msg.output
            b["cacheRead"] += msg.cache_read
            b["cacheWrite"] += msg.cache_write_5m + msg.cache_write_1h
            per_model = b["models"].setdefault(
                msg.model or "(unknown)", {"estimatedUSD": 0.0, "messages": 0}
            )
            per_model["estimatedUSD"] += cost
            per_model["messages"] += 1
        day = datetime.fromtimestamp(msg.epoch).strftime("%Y-%m-%d")
        slot = daily.setdefault((day, num), {"estimatedUSD": 0.0, "messages": 0})
        slot["estimatedUSD"] += cost
        slot["messages"] += 1

    def finish(bucket: dict) -> dict:
        bucket["estimatedUSD"] = round(bucket["estimatedUSD"], 2)
        bucket["models"] = [
            {"model": name, "estimatedUSD": round(v["estimatedUSD"], 2),
             "messages": v["messages"]}
            for name, v in sorted(
                bucket["models"].items(),
                key=lambda kv: kv[1]["estimatedUSD"], reverse=True,
            )
        ]
        return bucket

    rows = []
    for num in sorted(accounts):
        row = {"number": num}
        row.update((labels or {}).get(num, {}))
        row.update(finish(accounts[num]))
        rows.append(row)

    total = round(sum(r["estimatedUSD"] for r in rows) + unattributed["estimatedUSD"], 2)
    caveats = [
        "Estimated from public API list prices"
        f" ({PRICE_TABLE_SOURCE} snapshot {PRICE_TABLE_DATE}) —"
        " NOT billing truth; subscription plans don't bill per token.",
        "Counts only transcripts on this machine (~/.claude/projects).",
        "Attribution joins message times against the switch log;"
        " history is bounded by log retention, and a slot whose account"
        " was replaced inherits its predecessor's in-window history.",
    ]
    report = {
        "schemaVersion": 1,
        "days": days,
        "estimatedTotalUSD": total,
        "priceTable": {"source": PRICE_TABLE_SOURCE, "date": PRICE_TABLE_DATE},
        "accounts": rows,
        "daily": [
            {
                "date": day,
                "account": num,
                "estimatedUSD": round(v["estimatedUSD"], 2),
                "messages": v["messages"],
            }
            for (day, num), v in sorted(
                daily.items(), key=lambda kv: (kv[0][0], kv[0][1] is None, kv[0][1] or 0)
            )
        ],
        "caveats": caveats,
    }
    if unattributed["messages"]:
        report["unattributed"] = finish(unattributed)
        caveats.append(
            "Some messages predate the oldest switch-log entry and can't be"
            " attributed to an account."
        )
    if unpriced_tokens:
        report["unpricedTokens"] = unpriced_tokens
        report["unpricedModels"] = sorted(unpriced_models)
        caveats.append(
            "Some tokens are from models missing from the price table and"
            " count $0 in the estimate."
        )
    return report
