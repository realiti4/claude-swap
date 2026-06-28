"""Serialization helpers for ``--json`` structured output.

Centralizes the schema-v1 shapes so ``--list``/``--status``/``--switch`` agree on
field names (camelCase, matching the export envelope in transfer.py) and on how the
internal usage dict is projected to JSON. Callers build payloads here; the CLI does
the single ``json.dumps`` (see cli.py).
"""

from __future__ import annotations

# Bump only on a breaking change to any payload shape. Scripts key off this.
SCHEMA_VERSION = 1


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
    return out


def usage_fields(entry: dict | str | None) -> tuple[str, dict | None]:
    """Map a collected usage entry to ``(usageStatus, usage|None)``.

    ``_collect_usage`` yields one of: a usage dict, the string ``"no credentials"``,
    or ``None`` (fetch failed).
    """
    if isinstance(entry, dict):
        return "ok", usage_to_json(entry)
    if isinstance(entry, str):
        return "no_credentials", None
    return "unavailable", None


def account_ref(number: int | None, email: str) -> dict:
    """A minimal account reference, used for switch ``from``/``to``."""
    return {"number": number, "email": email}


def account_row(
    number: int,
    email: str,
    org_name: str,
    org_uuid: str,
    active: bool,
    usage_entry: dict | str | None,
) -> dict:
    """A full account row for ``--list``."""
    status, usage = usage_fields(usage_entry)
    return {
        "number": number,
        "email": email,
        "organizationName": org_name,
        "organizationUuid": org_uuid,
        "isOrganization": bool(org_uuid),
        "active": active,
        "usageStatus": status,
        "usage": usage,
    }


def error_envelope(exc: Exception) -> dict:
    """The structured error payload emitted on a handled ClaudeSwitchError."""
    return {
        "schemaVersion": SCHEMA_VERSION,
        "error": {"type": type(exc).__name__, "message": str(exc)},
    }
