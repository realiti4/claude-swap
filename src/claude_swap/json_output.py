"""Serialization helpers for ``--json`` structured output.

Centralizes the schema-v1 shapes so ``--list``/``--status``/``--switch`` agree on
field names (camelCase, matching the export envelope in transfer.py) and on how the
internal usage dict is projected to JSON. Callers build payloads here; the CLI does
the single ``json.dumps`` (see cli.py).
"""

from __future__ import annotations

from datetime import datetime, timezone

# Bump only on a breaking change to any payload shape. Scripts key off this.
SCHEMA_VERSION = 1

# Sentinel entries that ``_collect_usage`` / ``_fetch_active_usage`` yield in place
# of a usage dict. Kept here (the serialization hub) so the human renderer and the
# JSON projection agree instead of scattering raw strings.
USAGE_NO_CREDENTIALS = "no credentials"
USAGE_TOKEN_EXPIRED = "token expired"
# API-key (``/login`` managed key) accounts have no subscription quota; usage is
# reported as this sentinel instead of being fetched from the OAuth usage API.
USAGE_API_KEY = "api key"
# The active account's macOS Keychain was unreadable (locked / denied / timeout)
# with no plaintext fallback — distinct from a genuinely empty slot, so the user
# isn't misled into an unnecessary re-login.
USAGE_KEYCHAIN_UNAVAILABLE = "keychain unavailable"


def _window_to_json(entry: dict) -> dict:
    """Project a 5h/7d usage window to JSON, preserving raw ``resetsAt``."""
    out: dict = {"pct": entry["pct"]}
    if "resets_at" in entry:
        out["resetsAt"] = entry["resets_at"]
    if "countdown" in entry:
        out["countdown"] = entry["countdown"]
    if "clock" in entry:
        out["clock"] = entry["clock"]
    return out


def _scoped_window_to_json(entry: dict) -> dict:
    """Project a per-model scoped weekly window, carrying its model name."""
    out = _window_to_json(entry)
    out["name"] = entry["name"]
    return out


def usage_to_json(usage: dict) -> dict:
    """Convert the internal usage dict to its camelCase JSON projection.

    Sub-keys are emitted only when present in the source (the API does not always
    return every window or pay-as-you-go spend).
    """
    out: dict = {}
    if "five_hour" in usage:
        out["fiveHour"] = _window_to_json(usage["five_hour"])
    if "seven_day" in usage:
        out["sevenDay"] = _window_to_json(usage["seven_day"])
    if "spend" in usage:
        spend = usage["spend"]
        spend_out: dict = {
            "used": spend["used"],
            "limit": spend["limit"],
            "pct": spend["pct"],
            "currency": spend["currency"],
        }
        if "resets_at" in spend:
            spend_out["resetsAt"] = spend["resets_at"]
        if "countdown" in spend:
            spend_out["countdown"] = spend["countdown"]
        if "clock" in spend:
            spend_out["clock"] = spend["clock"]
        out["spend"] = spend_out
    if "scoped" in usage:
        out["scoped"] = [_scoped_window_to_json(w) for w in usage["scoped"]]
    return out


def usage_fields(entry: dict | str | None) -> tuple[str, dict | None]:
    """Map a collected usage entry to ``(usageStatus, usage|None)``.

    A collected entry is one of: a usage dict, the ``USAGE_TOKEN_EXPIRED`` sentinel
    (active token expired while Claude Code owns it), the ``USAGE_API_KEY`` sentinel
    (managed API-key account, no subscription quota), the
    ``USAGE_KEYCHAIN_UNAVAILABLE`` sentinel (active Keychain unreadable), the
    ``USAGE_NO_CREDENTIALS`` sentinel, or ``None`` (fetch failed).
    """
    if isinstance(entry, dict):
        return "ok", usage_to_json(entry)
    if entry == USAGE_TOKEN_EXPIRED:
        return "token_expired", None
    if entry == USAGE_API_KEY:
        return "api_key", None
    if entry == USAGE_KEYCHAIN_UNAVAILABLE:
        return "keychain_unavailable", None
    if isinstance(entry, str):
        return "no_credentials", None
    return "unavailable", None


def account_ref(number: int | None, email: str) -> dict:
    """A minimal account reference, used for switch ``from``/``to``."""
    return {"number": number, "email": email}


def usage_freshness_fields(
    fetched_at: float | None, age_s: float | None
) -> dict:
    """Additive ``usageFetchedAt``/``usageAgeSeconds`` fields describing how
    old the served ``usage`` measurement is (the store may serve last-good
    data on fetch failure). Emitted only alongside a non-null ``usage``."""
    if fetched_at is None:
        return {}
    fields: dict = {
        "usageFetchedAt": (
            datetime.fromtimestamp(fetched_at, tz=timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
    }
    if age_s is not None:
        fields["usageAgeSeconds"] = round(age_s, 1)
    return fields


def account_row(
    number: int,
    email: str,
    org_name: str,
    org_uuid: str,
    active: bool,
    usage_entry: dict | str | None,
    *,
    usage_fetched_at: float | None = None,
    usage_age_s: float | None = None,
) -> dict:
    """A full account row for ``--list``."""
    status, usage = usage_fields(usage_entry)
    row = {
        "number": number,
        "email": email,
        "organizationName": org_name,
        "organizationUuid": org_uuid,
        "isOrganization": bool(org_uuid),
        "active": active,
        "usageStatus": status,
        "usage": usage,
    }
    if usage is not None:
        row.update(usage_freshness_fields(usage_fetched_at, usage_age_s))
    return row


def error_envelope(exc: Exception) -> dict:
    """The structured error payload emitted on a handled ClaudeSwitchError."""
    return {
        "schemaVersion": SCHEMA_VERSION,
        "error": {"type": type(exc).__name__, "message": str(exc)},
    }
