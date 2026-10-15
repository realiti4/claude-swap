"""Prometheus text-format rendering for ``list --prometheus``.

Turns the schema-v1 payload that ``list --json`` prints into an exposition page
that Grafana's Prometheus data source, node_exporter's textfile collector and
similar tools can read. The renderer is a pure function of that payload, so the
two outputs cannot disagree; the CLI calls ``write`` once, after the payload is
complete.

Privacy: metrics land in long-lived, often shared stores, so an account is named
by slot number and alias only. The renderer never reads ``email``,
``organizationName``, ``organizationUuid``, ``duplicateAccountWarnings``,
``lockstepUsageWarnings``, ``unclaimedCredentials`` or ``usageError``.

Only decision-grade usage (a non-null ``usage``) is rendered. ``lastGoodUsage``
is never used: a stale number plotted as current is worse than a gap.
"""

from __future__ import annotations

import math
import re
import sys

from claude_swap.poll_policy import parse_reset_ts

# Order matches json_output.usage_fields; the usage_status family is a StateSet
# over these so a state change flips values instead of making series vanish.
USAGE_STATUSES: tuple[str, ...] = (
    "ok",
    "token_expired",
    "api_key",
    "keychain_unavailable",
    "relogin_required",
    "foreign_credential",
    "no_credentials",
    "unavailable",
)

_FAMILIES: tuple[tuple[str, str], ...] = (
    ("cswap_account_info",
     "Managed account (1 per slot); account is the slot number, alias is empty when unset."),
    ("cswap_account_active", "1 for the active account, else 0."),
    ("cswap_account_disabled",
     "1 when the account is held out of rotation (cswap disable), else 0."),
    ("cswap_usage_up", "1 when the account has current usage (usageStatus ok), else 0."),
    ("cswap_usage_status",
     "1 for the account's current usageStatus (as in list --json), 0 for the other known states."),
    ("cswap_usage_ratio", "Share of the window's quota used, from 0 to 1."),
    ("cswap_usage_expected_ratio",
     "Share of the weekly quota that would be used by now at an even pace, from 0 to 1."),
    ("cswap_usage_reset_timestamp_seconds", "When the window's quota resets, as a Unix timestamp."),
    ("cswap_usage_fetched_timestamp_seconds",
     "When the usage measurement was taken, as a Unix timestamp."),
    ("cswap_login_expiry_timestamp_seconds",
     "When the stored login expires and needs a new /login, as a Unix timestamp."),
)


def _escape_label(value: str) -> str:
    # Backslash first, so the escapes added below are not doubled.
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _escape_help(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n")


def _format_value(value: object) -> str | None:
    """Go-ParseFloat-safe text for a number, or None when it must be dropped."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:  # an int beyond float range
        return None
    if not math.isfinite(number):
        return None
    return str(int(number)) if number.is_integer() else repr(round(number, 6))


def _sample(name: str, labels: list[tuple[str, str]], value: object) -> str | None:
    text = _format_value(value)
    if text is None:
        return None
    inner = ",".join(f'{key}="{_escape_label(val)}"' for key, val in labels)
    return f"{name}{{{inner}}} {text}"


def _windows(usage: dict) -> list[tuple[str, dict, bool]]:
    """(label, window dict, weekly) in order: five_hour, seven_day, scoped by label."""
    found: list[tuple[str, dict, bool]] = []
    for key, label, weekly in (("fiveHour", "five_hour", False), ("sevenDay", "seven_day", True)):
        win = usage.get(key)
        if isinstance(win, dict):
            found.append((label, win, weekly))
    scoped: dict[str, dict] = {}
    for win in usage.get("scoped") or []:
        if not isinstance(win, dict) or not isinstance(win.get("name"), str):
            continue
        slug = re.sub(r"\s+", "_", win["name"].strip().lower())
        if slug:
            scoped.setdefault("seven_day_" + slug, win)  # first wins
    found.extend((label, scoped[label], True) for label in sorted(scoped))
    return found


def _ratio(pct: object) -> float | None:
    """A percentage as a 0-1 ratio, or None for anything that is not a number."""
    if isinstance(pct, bool) or not isinstance(pct, (int, float)):
        return None
    try:
        return pct / 100
    except OverflowError:
        return None


def _timestamp(value: object) -> float | None:
    return parse_reset_ts(value) if isinstance(value, str) else None


def render(payload: dict) -> str:
    """Render a ``list --json`` payload as a Prometheus exposition page."""
    samples: dict[str, list[str]] = {name: [] for name, _ in _FAMILIES}

    def add(name: str, labels: list[tuple[str, str]], value: object) -> None:
        line = _sample(name, labels, value)
        if line is not None:
            samples[name].append(line)

    rows = [r for r in (payload.get("accounts") or []) if isinstance(r, dict)]
    numbered: dict[int, dict] = {}
    for row in rows:
        number = row.get("number")
        if _format_value(number) is None:
            continue
        numbered.setdefault(int(number), row)  # duplicate numbers: first wins

    for number in sorted(numbered):
        row = numbered[number]
        acct = [("account", str(number))]
        alias = row.get("alias")
        add("cswap_account_info",
            acct + [("alias", alias if isinstance(alias, str) else "")], 1)
        add("cswap_account_active", acct, 1 if row.get("active") else 0)
        add("cswap_account_disabled", acct, 1 if row.get("disabled") else 0)
        status = row.get("usageStatus")
        add("cswap_usage_up", acct, 1 if status == "ok" else 0)
        known = list(USAGE_STATUSES)
        if isinstance(status, str) and status not in USAGE_STATUSES:
            known.append(status)  # a future status still gets its own sample
        for name in known:
            add("cswap_usage_status", acct + [("status", name)], 1 if name == status else 0)

        usage = row.get("usage")
        if isinstance(usage, dict):
            for label, win, weekly in _windows(usage):
                win_labels = acct + [("window", label)]
                add("cswap_usage_ratio", win_labels, _ratio(win.get("pct")))
                if weekly:
                    add("cswap_usage_expected_ratio", win_labels,
                        _ratio(win.get("expectedPct")))
                add("cswap_usage_reset_timestamp_seconds", win_labels,
                    _timestamp(win.get("resetsAt")))
            add("cswap_usage_fetched_timestamp_seconds", acct,
                _timestamp(row.get("usageFetchedAt")))
        add("cswap_login_expiry_timestamp_seconds", acct, _timestamp(row.get("loginExpiresAt")))

    lines: list[str] = []
    for name, help_text in _FAMILIES:
        lines.append(f"# HELP {name} {_escape_help(help_text)}")
        lines.append(f"# TYPE {name} gauge")
        lines.extend(samples[name])
    return "\n".join(lines) + "\n"


def write(text: str) -> None:
    """Write the page to stdout as UTF-8 bytes.

    A text-mode stdout on Windows turns LF into CRLF, which the exposition
    format forbids, so the bytes go to the underlying buffer when there is one.
    A character UTF-8 cannot carry (a lone surrogate in a hand-edited alias)
    becomes ``?`` rather than failing the whole page. With no stdout at all
    (pythonw, a detached process) there is nowhere to write, as with print().
    """
    if sys.stdout is None:
        return
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is None:
        sys.stdout.write(text)
        sys.stdout.flush()
        return
    sys.stdout.flush()
    buffer.write(text.encode("utf-8", errors="replace"))
    buffer.flush()
