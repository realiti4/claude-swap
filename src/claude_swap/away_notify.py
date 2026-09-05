"""Push "which account is live now" to the user's phone after a switch.

With several accounts auto-switching, a user away from the Mac cannot know
which account to open the Claude mobile app with — macOS notifications die on
the Mac's screen, and item 6a's `/rc` sweep rebinds every session exactly when
the answer changes. This module closes that gap (backlog item 7): after a
switch and its `/rc` sweep, push the new account's alias to Slack and/or
Telegram.

One interface, per-channel backends. A failed channel is logged (by name and
HTTP status only) and never blocks the switch, the sweep, or another channel.

Secrets: a webhook URL / bot token is a capability — anyone holding it can
post as the user. They live in ``notify.json`` beside the credential backups,
mode 0600, deliberately OUTSIDE settings.json (which tooling prints freely via
``cswap config list``) and outside ``cswap export`` bundles (export reads
per-account records, never arbitrary backup-root files). Nothing in this
module ever logs, prints, or embeds a secret in an error or event.

Ordering rule (backlog item 7): the push runs AFTER the `/rc` sweep, never on
the switch itself. Today's body carries no session URLs, so the sweep's
*outcome* does not gate the push — when 6b's URL scrape lands, it must.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

_logger = logging.getLogger("claude-swap")

NOTIFY_FILENAME = "notify.json"
_PUSH_TIMEOUT_S = 10.0


def notify_path(backup_dir: Path) -> Path:
    return Path(backup_dir) / NOTIFY_FILENAME


def load_channels(backup_dir: Path) -> dict:
    """The stored channel config, ``{}`` when absent or unreadable.

    Read fresh on every push (never cached at engine start) so a webhook
    added while the engine runs takes effect on the next switch.
    """
    try:
        raw = json.loads(notify_path(backup_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def save_channels(backup_dir: Path, config: dict) -> None:
    """Write the channel config with credential-grade permissions (0600)."""
    path = notify_path(backup_dir)
    if not config:
        path.unlink(missing_ok=True)
        return
    tmp = path.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
    os.replace(tmp, path)
    os.chmod(path, 0o600)  # replace kept tmp's mode; belt for pre-existing files


def masked(secret: str) -> str:
    """A displayable stand-in: host (for URLs) plus the last 4 characters."""
    if not secret:
        return "(unset)"
    host = urlsplit(secret).netloc if "://" in secret else ""
    return f"{host}…{secret[-4:]}" if host else f"…{secret[-4:]}"


def _post_json(url: str, payload: dict, opener) -> None:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener(request, timeout=_PUSH_TIMEOUT_S):
        pass


def push(backup_dir: Path, text: str, *, opener=None) -> list[str]:
    """Send ``text`` to every configured channel; the names that took it.

    Never raises. Failures log the channel name and HTTP status only — an
    exception repr could carry the secret URL into the engine's event log,
    which both frontends render on screen.
    """
    opener = opener or urllib.request.urlopen
    config = load_channels(backup_dir)
    delivered: list[str] = []

    slack_url = config.get("slackWebhookUrl")
    if slack_url:
        try:
            _post_json(slack_url, {"text": text}, opener)
            delivered.append("slack")
        except Exception as e:
            _logger.warning(
                "away-notify slack push failed: %s %s",
                type(e).__name__, getattr(e, "code", ""),
            )

    token = config.get("telegramBotToken")
    chat_id = config.get("telegramChatId")
    if token and chat_id:
        try:
            _post_json(
                f"https://api.telegram.org/bot{token}/sendMessage",
                {"chat_id": chat_id, "text": text},
                opener,
            )
            delivered.append("telegram")
        except Exception as e:
            _logger.warning(
                "away-notify telegram push failed: %s %s",
                type(e).__name__, getattr(e, "code", ""),
            )

    return delivered


def switch_text(
    label: str, number: str | int, rearmed: int, urls=(), fleet=()
) -> str:
    """The push body for a completed switch.

    ``label`` should be the account's alias, falling back to the email
    LOCAL PART — never the full address. The destination channel is private
    by policy, but a push body shows on lock screens — which is also why the
    session URLs go BELOW the first line: lock screens preview only the
    head, and the URLs grant transcript access (the channel must be treated
    as private; backlog item 7). ``fleet`` is the whole roster's status,
    one line per account (see ``fleet_lines``) — between head and URLs so
    the lock-screen preview stays the switch itself.
    """
    text = f"cswap: switched to account {number} ({label})"
    if rearmed:
        text += f" — remote control re-armed on {rearmed} session(s)"
    if fleet:
        text += "\n" + "\n".join(fleet)
    shown = list(urls)[:6]
    if shown:
        text += "\n" + "\n".join(shown)
    if len(urls) > len(shown):
        text += f"\n(+{len(urls) - len(shown)} more)"
    return text


def fleet_lines(rows) -> list[str]:
    """Per-account status lines for the switch push body.

    ``rows`` is ``(number, label, usage, is_active)`` per enabled account —
    ``usage`` the internal last-good dict or ``None``. Display-grade on
    purpose: a push is a glance, not a switching decision, so last-good
    numbers beat "unavailable" (staleness rules stay the switcher's job).
    Labels follow the alias-or-local-part rule above.
    """
    out = []
    for number, label, usage, is_active in rows:
        marker = "→" if is_active else "·"
        out.append(f"{marker} {number} {label}: {_fleet_cell(usage)}")
    return out


def _fleet_cell(usage) -> str:
    """One account's windows, compact: "5h 45% · 7d 12% · Fable 3%";
    a maxed window flips the cell to "out (7d 100%)"."""
    if not isinstance(usage, dict):
        return "no data"
    alive: list[str] = []
    dead: list[str] = []
    def add(name: str, window) -> None:
        if isinstance(window, dict) and isinstance(window.get("pct"), (int, float)):
            pct = window["pct"]
            (dead if pct >= 100 else alive).append(f"{name} {round(pct)}%")
    add("5h", usage.get("five_hour"))
    add("7d", usage.get("seven_day"))
    for w in usage.get("scoped") or []:
        if isinstance(w, dict):
            add(str(w.get("name", "?")), w)
    if dead:
        return "out (" + ", ".join(dead) + ")"
    return " · ".join(alive) if alive else "no data"
