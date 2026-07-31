"""Find sessions still billing an API key cswap has already switched away from.

A managed API key stored in ``~/.claude.json`` as ``primaryApiKey`` is resolved
once and memoized for the lifetime of a Claude Code process, cleared only by
that process's own ``/login`` / ``/logout``. So a session that starts while one
is set is pinned to that key permanently: activating an OAuth account later
removes the value from the file but cannot touch the memo, and a managed key
outranks the claude.ai credential. ``cswap status`` then reports the
subscription account while those sessions keep billing metered requests.

Nothing cswap writes can undo that from outside the process — the only cure is
restarting the session. What it can do is stop the leak being *silent*, which is
what this module is for.

``credentials`` records a **spell**: the window during which a ``primaryApiKey``
that cswap wrote was readable. It opens only on the fallback path that actually
writes the field (Keychain and ``apiKeyHelper`` storage create no pin, so they
open no spell) and closes when that key is cleared. Any session whose start time
falls inside a *closed* spell is pinned. Sessions inside the open spell are
correctly on the key that is still active, and are not reported.

The ledger is advisory: every write is best-effort and every read failure is an
empty answer. A broken ledger must never fail a switch or a status.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from claude_swap.paths import get_backup_root
from claude_swap.process_detection import ClaudeSession, list_sessions

LEDGER_NAME = "api_key_spells.json"
SCHEMA_VERSION = 1
# Spells older than this are dropped on write: no session that predates it is
# plausibly still running, and the file must not grow without bound.
MAX_AGE_MS = 30 * 24 * 60 * 60 * 1000

_logger = logging.getLogger("claude-swap")


@dataclass(frozen=True)
class PinnedSession:
    """A running session pinned to a key that is no longer the active account."""

    session: ClaudeSession
    account: str  # the API-key account number whose key it is stuck on


def ledger_path() -> Path:
    return get_backup_root() / LEDGER_NAME


def _now_ms() -> int:
    return int(time.time() * 1000)


def _read() -> list[dict]:
    try:
        data = json.loads(ledger_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    spells = data.get("spells") if isinstance(data, dict) else None
    return [s for s in spells if isinstance(s, dict)] if isinstance(spells, list) else []


def _write(spells: list[dict]) -> None:
    cutoff = _now_ms() - MAX_AGE_MS
    kept = [s for s in spells if s.get("end") is None or s.get("end", 0) >= cutoff]
    path = ledger_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"schemaVersion": SCHEMA_VERSION, "spells": kept}, indent=2),
            encoding="utf-8",
        )
    except OSError as e:
        _logger.warning(f"Could not update the API-key spell ledger: {e}")


def open_spell(account: str) -> None:
    """Record that a pinning ``primaryApiKey`` is now readable. Idempotent.

    Called only from the path that actually writes the field — an already-open
    spell is left as-is so a re-activation of the same key does not split one
    window into two and lose the sessions started in between.
    """
    spells = _read()
    if any(s.get("end") is None for s in spells):
        return
    spells.append({"account": str(account), "start": _now_ms(), "end": None})
    _write(spells)


def close_spell() -> None:
    """Record that the key is cleared: sessions inside the window are now stale."""
    spells = _read()
    changed = False
    for s in spells:
        if s.get("end") is None:
            s["end"] = _now_ms()
            changed = True
    if changed:
        _write(spells)


def find_pinned(sessions: list[ClaudeSession] | None = None) -> list[PinnedSession]:
    """Running sessions that started inside a closed spell, oldest first.

    A session with no usable start time is skipped rather than guessed at: a
    false "this is costing you money" is worse than a miss, since the only
    remedy it can offer is restarting work in progress.
    """
    spells = [
        s for s in _read()
        if isinstance(s.get("start"), (int, float))
        and isinstance(s.get("end"), (int, float))
    ]
    if not spells:
        return []
    try:
        live = sessions if sessions is not None else list_sessions()
    except Exception:  # pragma: no cover - defensive; detection is best-effort
        return []
    pinned = []
    for sess in live:
        if not sess.started_at:
            continue
        for s in spells:
            if s["start"] <= sess.started_at <= s["end"]:
                pinned.append(PinnedSession(sess, str(s.get("account", "?"))))
                break
    return sorted(pinned, key=lambda p: p.session.started_at)


def warning_lines(pinned: list[PinnedSession]) -> list[str]:
    """Human lines for ``cswap status``. Empty when there is nothing to say."""
    if not pinned:
        return []
    n = len(pinned)
    lines = [
        f"{n} running session{'s' if n > 1 else ''} still billing the API key of "
        f"Account-{pinned[0].account}:",
    ]
    for p in pinned:
        started = time.strftime("%b %d %H:%M", time.localtime(p.session.started_at / 1000))
        cwd = p.session.cwd or "?"
        lines.append(f"  pid {p.session.pid}  started {started}  {cwd}")
    lines.append(
        "They started while that key was active and memoized it for their "
        "process lifetime; switching accounts cannot reach them. Restart them "
        "(or run /logout then /login inside each) to move them onto the active "
        "account."
    )
    return lines
