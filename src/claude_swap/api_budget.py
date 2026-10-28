"""Prepaid-credit budgets for managed API-key accounts (opt-in).

API keys have no subscription usage endpoint, so ``cswap list`` could only say
"API key (no quota)". When an operator knows a key draws on a fixed prepaid credit
(e.g. startup-programme credits), ``api-budgets.json`` (next to ``sequence.json``)
declares it per ORGANIZATION — every key in an org spends from the same pool::

    {
      "capPct": 95,
      "refreshSeconds": 900,
      "orgs": {
        "acme": {
          "budgetUsd": 500,
          "since": "2026-10-01T00:00:00Z",
          "expiresAt": "2027-04-01T00:00:00Z",
          "costKeySlot": "6",
          "slots": ["5", "6"]
        }
      }
    }

Spend is read from the Admin API ``cost_report`` (amounts are cents) with
``costKeySlot``'s key, which must be organization-scoped. Results are cached under
``cache/api-budget.json`` for ``refreshSeconds``; a failed refresh serves the last
reading. Without the file nothing changes.

Protection: a slot whose org is at/over ``capPct`` of its budget, or past
``expiresAt``, is held out of automatic rotation (the same way ``cswap disable``
does), so the auto-switch engine never lands on exhausted or expired credit.
Unknown spend (never read) fails OPEN — credits are prepaid, so the API itself
is the hard stop.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

COST_URL = "https://api.anthropic.com/v1/organizations/cost_report"
DEFAULT_CAP_PCT = 95.0
DEFAULT_REFRESH_S = 900
_TIMEOUT_S = 20


@dataclass(frozen=True)
class BudgetStatus:
    org: str
    budget_usd: float
    spent_usd: float | None  # None = never read successfully
    expires_at: datetime | None
    cap_pct: float
    read_at: float | None
    error: str | None = None

    @property
    def pct(self) -> float | None:
        if self.spent_usd is None or self.budget_usd <= 0:
            return None
        return 100.0 * self.spent_usd / self.budget_usd

    @property
    def expired(self) -> bool:
        return self.expires_at is not None and datetime.now(timezone.utc) >= self.expires_at

    @property
    def exhausted(self) -> bool:
        pct = self.pct
        return pct is not None and pct >= self.cap_pct

    @property
    def blocked(self) -> bool:
        return self.expired or self.exhausted


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


class ApiBudgets:
    """Config + cached spend for API-key slots. One instance per switcher."""

    def __init__(self, backup_root: Path, read_slot_key):
        self._config_path = backup_root / "api-budgets.json"
        self._cache_path = backup_root / "cache" / "api-budget.json"
        self._read_slot_key = read_slot_key  # (slot) -> raw key string or ""
        self._memo: dict[str, BudgetStatus] = {}

    def _config(self) -> dict:
        try:
            return json.loads(self._config_path.read_text())
        except (OSError, ValueError):
            return {}

    def _org_for_slot(self, cfg: dict, slot: str) -> tuple[str, dict] | None:
        for name, org in (cfg.get("orgs") or {}).items():
            if str(slot) in [str(s) for s in org.get("slots", [])]:
                return name, org
        return None

    def _load_cache(self) -> dict:
        try:
            return json.loads(self._cache_path.read_text())
        except (OSError, ValueError):
            return {}

    def _save_cache(self, cache: dict) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(cache, indent=2))
            tmp.replace(self._cache_path)
        except OSError:
            pass

    def _fetch_spent(self, key: str, since: str) -> float:
        """Sum of cost_report amounts (cents) since ``since``, in USD."""
        total_cents = 0.0
        page: str | None = None
        for _ in range(50):  # pagination guard
            params = {"starting_at": since, "limit": "31"}
            if page:
                params["page"] = page
            req = urllib.request.Request(
                f"{COST_URL}?{urllib.parse.urlencode(params)}",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            )
            with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
                data = json.loads(resp.read())
            for bucket in data.get("data", []):
                for result in bucket.get("results", []):
                    total_cents += float(result.get("amount") or 0)
            if not data.get("has_more"):
                break
            page = data.get("next_page")
        return total_cents / 100.0

    def status(self, slot: str) -> BudgetStatus | None:
        """Budget status for a slot, or None when it has no configured budget."""
        slot = str(slot)
        if slot in self._memo:
            return self._memo[slot]
        cfg = self._config()
        found = self._org_for_slot(cfg, slot)
        if not found:
            return None
        name, org = found
        refresh_s = float(cfg.get("refreshSeconds", DEFAULT_REFRESH_S))
        since = org.get("since") or "2026-01-01T00:00:00Z"
        cache = self._load_cache()
        row = cache.get(name) or {}
        if row.get("since") != since:
            row = {}
        spent = row.get("spentUsd")
        read_at = row.get("readAt")
        error = None
        if read_at is None or time.time() - float(read_at) >= refresh_s:
            key = self._read_slot_key(str(org.get("costKeySlot", "")))
            if not key:
                error = "no cost key"
            else:
                try:
                    spent = self._fetch_spent(key, since)
                    read_at = time.time()
                    cache[name] = {"since": since, "spentUsd": spent, "readAt": read_at}
                    self._save_cache(cache)
                except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                    error = getattr(exc, "code", None) and f"http-{exc.code}" or type(exc).__name__
        st = BudgetStatus(
            org=name,
            budget_usd=float(org.get("budgetUsd") or 0),
            spent_usd=None if spent is None else float(spent),
            expires_at=_parse_ts(org.get("expiresAt")),
            cap_pct=float(cfg.get("capPct", DEFAULT_CAP_PCT)),
            read_at=None if read_at is None else float(read_at),
            error=error,
        )
        self._memo[slot] = st
        return st


def describe(st: BudgetStatus) -> str:
    """One-line credit summary for ``cswap list`` / ``status``."""
    parts = [f"credit ({st.org})"]
    if st.spent_usd is None:
        parts.append("spend unknown" + (f" ({st.error})" if st.error else ""))
    elif st.budget_usd > 0:
        parts.append(f"{st.pct:.0f}% used  ${st.spent_usd:,.2f} / ${st.budget_usd:,.0f}")
    else:
        parts.append(f"${st.spent_usd:,.2f} spent")
    if st.expires_at is not None:
        parts.append(f"expires {st.expires_at:%Y-%m-%d}")
    if st.expired:
        parts.append("EXPIRED — held out of rotation")
    elif st.exhausted:
        parts.append(f"≥{st.cap_pct:.0f}% — held out of rotation")
    elif st.error and st.spent_usd is not None:
        parts.append(f"stale ({st.error})")
    return " · ".join(parts)
