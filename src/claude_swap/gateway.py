"""Gateway credentials, settings activation, and a separate probe budget.

OAuth polling policy deliberately does not participate in this module.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import random
import re
import shlex
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

FIRST_PARTY_URL = "https://api.anthropic.com"
DEFAULT_MODEL = "claude-haiku-4-5"
ACTIVE_INTERVAL_S = 120
CANDIDATE_INTERVAL_S = 600
EXHAUSTED_INTERVAL_S = 900
COOLING_INTERVAL_S = 60
COOLING_MAX_S = 600
TRANSIENT_MAX_S = 600
DEFAULT_RETRY_S = 3600
JITTER_FRAC = 0.1
TIMEOUT_S = 15
RECORD_FIELDS = ("baseUrl", "auth", "usageUrl", "probeModel", "modelDiscovery")
AUTH_OVERRIDE_ENV_VARS = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR", "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR",
)


def validate_url(value: str, *, base: bool = False) -> str:
    """Validate without ever echoing a URL that might contain credentials."""
    try:
        parts = urlsplit(value)
        if (
            parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or not parts.hostname
            or parts.port == 0
            or (
                parts.scheme != "https"
                and not (
                    parts.scheme == "http"
                    and parts.hostname in ("127.0.0.1", "localhost")
                )
            )
        ):
            raise ValueError
        path = parts.path.rstrip("/")
        if base and path.endswith("/v1"):
            path = path[:-3]
        return urlunsplit((parts.scheme, parts.netloc, path, "", ""))
    except (ValueError, TypeError, AttributeError):
        raise ValueError(
            "Gateway URL must use HTTPS (HTTP is allowed on localhost or 127.0.0.1), without credentials, query, or fragment"
        ) from None


def account_fields(
    base_url: str,
    auth: str = "bearer",
    usage_url: str | None = None,
    probe_model: str = DEFAULT_MODEL,
    model_discovery: bool = False,
) -> dict:
    if auth not in ("bearer", "x-api-key"):
        raise ValueError("Gateway auth must be bearer or x-api-key")
    if not isinstance(probe_model, str) or not probe_model.strip():
        raise ValueError("Gateway probe model must be nonempty")
    if not isinstance(model_discovery, bool):
        raise ValueError("Gateway modelDiscovery must be a boolean")
    result = dict(
        kind="gateway",
        baseUrl=validate_url(base_url, base=True),
        auth=auth,
        probeModel=probe_model,
        modelDiscovery=model_discovery,
    )
    if usage_url is not None:
        result["usageUrl"] = validate_url(usage_url)
    return result


def executable() -> str:
    if Path(sys.argv[0]).name in ("cswap", "claude-swap"):
        return os.path.abspath(sys.argv[0])
    found = shutil.which("cswap") or shutil.which("claude-swap")
    if not found:
        raise ValueError("Cannot locate the cswap executable for gateway-credential")
    return os.path.abspath(found)


def helper(slot: str) -> str:
    return shlex.join([executable(), "gateway-credential", str(int(slot))])


def owned_slot(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parts = shlex.split(value)
        if (
            len(parts) == 3
            and Path(parts[0]).name in ("cswap", "claude-swap", "cswap.exe", "claude-swap.exe")
            and parts[1] == "gateway-credential"
            and parts[2].isdigit()
            and int(parts[2]) > 0
        ):
            # Both installed entry points own this command. The invoking
            # alias (or its current location on PATH) need not be the one
            # that wrote the settings file.
            return str(int(parts[2]))
    except (ValueError, OSError):
        pass
    return None


def read_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(result, dict) or not isinstance(result.get("env", {}), dict):
            raise ValueError
        return result
    except (ValueError, OSError):
        raise ValueError(
            "Cannot read Claude settings.json; repair it before switching a gateway"
        ) from None


def active_slot(path: Path, accounts: dict) -> str | None:
    try:
        settings = read_settings(path)
    except ValueError:
        return None
    slot = owned_slot(settings.get("apiKeyHelper"))
    record = accounts.get(slot, {})
    if record.get("kind") == "gateway" and settings.get("env", {}).get(
        "ANTHROPIC_BASE_URL"
    ) == record.get("baseUrl"):
        return slot
    return None


def atomic_bytes(path: Path, data: bytes) -> None:
    # Follow a session-profile settings symlink; replace its target, not the link.
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".cswap-settings-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        if path.read_bytes() != data:
            raise ValueError(
                "Claude settings changed while switching; retry the switch"
            )
    finally:
        if os.path.exists(name):
            os.unlink(name)


def restore_settings(path: Path, previous: bytes | None) -> None:
    if previous is None:
        path.resolve().unlink(missing_ok=True)
    else:
        atomic_bytes(path, previous)


def refuse_auth_overrides(settings: dict, path: Path) -> None:
    if any(settings.get("env", {}).get(name) for name in AUTH_OVERRIDE_ENV_VARS):
        raise ValueError(
            f"Claude settings file {path} contains authentication overrides; remove them before activating a gateway"
        )


def write_settings(
    path: Path,
    slot: str | None,
    record: dict | None = None,
    *,
    remove_last: bool = False,
) -> None:
    settings = read_settings(path)
    if remove_last:
        # Only the first-party value left by deactivation belongs to cswap.
        # A user may since have configured another gateway/helper themselves.
        env = settings.get("env", {})
        if env.get("ANTHROPIC_BASE_URL") == FIRST_PARTY_URL:
            env.pop("ANTHROPIC_BASE_URL")
            if not env:
                settings.pop("env", None)
            atomic_bytes(path, (json.dumps(settings, indent=2) + "\n").encode())
        return
    old_helper = settings.get("apiKeyHelper")
    if old_helper is not None and owned_slot(old_helper) is None:
        raise ValueError(
            "Claude settings.json has a foreign apiKeyHelper; refusing to replace it"
        )
    env = settings.setdefault("env", {})
    if slot is not None:
        refuse_auth_overrides(settings, path)
        settings["apiKeyHelper"] = helper(slot)
        env["ANTHROPIC_BASE_URL"] = record["baseUrl"]
        if record.get("modelDiscovery"):
            env["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"] = "1"
        else:
            env.pop("CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY", None)
    else:
        settings.pop("apiKeyHelper", None)
        env.pop("CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY", None)
        env["ANTHROPIC_BASE_URL"] = FIRST_PARTY_URL
    if not env:
        settings.pop("env", None)
    atomic_bytes(path, (json.dumps(settings, indent=2) + "\n").encode())


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        # A provider must never redirect an authenticated probe to another host.
        return None


def _retry_at(value: str | None, now: float) -> float:
    try:
        seconds = float(value)
        if not math.isfinite(seconds):
            raise ValueError
        return now + max(0, seconds)
    except (ValueError, TypeError):
        try:
            return max(now, parsedate_to_datetime(value).timestamp())
        except (ValueError, TypeError, OverflowError):
            return now + DEFAULT_RETRY_S


def probe(
    record: dict, key: str, *, now: float | None = None, previous: dict | None = None
) -> dict:
    """Return a credential-free verdict, including on malformed HTTP/errors."""
    now = time.time() if now is None else now
    previous = previous or {}
    result = dict(probeStatus="transient", probedAt=now, retryAt=None, budget=None)
    try:
        url = validate_url(record["baseUrl"], base=True) + "/v1/messages"
        headers = {
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        headers[
            "Authorization" if record.get("auth", "bearer") == "bearer" else "x-api-key"
        ] = "Bearer " + key if record.get("auth", "bearer") == "bearer" else key
        body = json.dumps(
            dict(
                model=record.get("probeModel", DEFAULT_MODEL),
                max_tokens=1,
                messages=[dict(role="user", content=".")],
            )
        ).encode()
        request = Request(url, data=body, headers=headers, method="POST")
        try:
            with build_opener(_NoRedirect).open(request, timeout=TIMEOUT_S) as response:
                status, raw, retry = (
                    response.status,
                    response.read(65536),
                    response.headers.get("Retry-After"),
                )
        except HTTPError as error:
            status, raw, retry = (
                error.code,
                error.read(65536),
                error.headers.get("Retry-After"),
            )
            error.close()
        try:
            error_data = json.loads(raw).get("error", {})
            error_type = str(error_data.get("type", ""))
            message = str(error_data.get("message", ""))
        except (ValueError, AttributeError, TypeError):
            error_type, message = "", ""
        # Redact BEFORE truncation; even a hostile server echoing the key is safe.
        result["errorType"] = (
            re.sub(r"[^a-zA-Z0-9_-]", "", error_type.replace(key, "[redacted]"))[:80]
            if key
            else ""
        )
        result["message"] = message.replace(key, "[redacted]")[:120] if key else ""
        if status == 200 or (
            status == 400
            and "model" in message.lower()
            and any(
                word in message.lower()
                for word in ("unknown", "not found", "does not exist", "invalid model")
            )
        ):
            result["probeStatus"] = "ok"
        elif status == 429:
            result.update(probeStatus="exhausted", retryAt=_retry_at(retry, now))
        elif status in (503, 529):
            count = int(previous.get("coolingCount", 0)) + 1
            result.update(
                probeStatus="cooling",
                coolingCount=count,
                coolingSince=previous.get("coolingSince", now),
                retryAt=now
                + min(COOLING_MAX_S, COOLING_INTERVAL_S * 2 ** min(count - 1, 4)),
            )
        elif status in (401, 403):
            result["probeStatus"] = "unauthorized"
    except Exception:
        # Never return repr/str of an exception containing a Request or header.
        result.update(errorType="request_error", message="Gateway probe failed")
    if result["probeStatus"] in ("cooling", "transient"):
        # A continuously failing host can alternate 500/network and 503/529.
        # Changing error class must not restart its departure grace period.
        since = now
        if previous.get("probeStatus") in ("cooling", "transient"):
            since = previous.get("failureSince", previous.get(
                "coolingSince", previous.get("transientSince", now)
            ))
        result["failureSince"] = since
        result[result["probeStatus"] + "Since"] = since
    return result


def usage(record: dict, key: str) -> str:
    # TODO(design WHAT CSWAP DOES TODAY §4): implement the usage adapter against
    # the mayor's recorded authenticated payload. No field-name guesses or
    # speculative requests with a real key; budget stays null until configured.
    return "unconfigured"


def next_probe_at(verdict: dict, *, active: bool, now: float) -> float | None:
    status = verdict.get("probeStatus")
    if status == "unauthorized":
        return None  # re-adding/replacing the key clears this quarantine
    interval = ACTIVE_INTERVAL_S if active else CANDIDATE_INTERVAL_S
    if status == "exhausted":
        interval = EXHAUSTED_INTERVAL_S
    elif status == "cooling":
        # The probe computes exponential backoff. Respect that deadline;
        # jitter may spread retries later, never consume the grant early.
        retry_at = verdict.get("retryAt")
        interval = max(0, retry_at - now) if retry_at is not None else COOLING_INTERVAL_S
        return now + interval * random.uniform(1, 1 + JITTER_FRAC)
    deadline = now + interval * random.uniform(1 - JITTER_FRAC, 1 + JITTER_FRAC)
    if status == "exhausted":
        deadline = min(deadline, max(now, verdict.get("retryAt") or now) + 60)
    return deadline


def json_fields(record: dict, verdict: dict | None) -> dict:
    verdict = verdict or {}
    return {
        "baseUrl": record["baseUrl"],
        "auth": record.get("auth", "bearer"),
        **{k: verdict.get(k) for k in ("probeStatus", "probedAt", "retryAt", "budget")},
    }


def tag(record: dict) -> str:
    return f"gateway · {urlsplit(record['baseUrl']).netloc} · {record.get('auth', 'bearer')}"


def usage_label(verdict: dict | None, now: float | None = None) -> str:
    verdict = verdict or {}
    budget = verdict.get("budget")
    if budget:
        return f"budget {budget['pct']:.0f}% of ${budget['limitUsd']:g}/wk"
    status = verdict.get("probeStatus") or "unprobed"
    if status == "ok":
        age = max(0, (time.time() if now is None else now) - verdict["probedAt"])
        return f"gateway ok · {int(age // 60)}m ago"
    if status == "exhausted" and verdict.get("retryAt"):
        try:
            clock = datetime.fromtimestamp(verdict["retryAt"], timezone.utc).strftime(
                "%H:%MZ"
            )
        except (ValueError, OverflowError, OSError):
            clock = "later"
        return f"gateway exhausted · retry {clock}"
    return f"gateway {status}"
