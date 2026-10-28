"""Prepaid-credit budgets for API-key accounts."""
import json

from claude_swap.api_budget import ApiBudgets, describe


def _budgets(tmp_path, spent, **org):
    cfg = {"capPct": 95, "refreshSeconds": 900, "orgs": {"acme": {
        "budgetUsd": 200, "since": "2026-10-01T00:00:00Z", "costKeySlot": "6",
        "slots": ["5", "6"], **org}}}
    (tmp_path / "api-budgets.json").write_text(json.dumps(cfg))
    b = ApiBudgets(tmp_path, lambda slot: "sk-ant-usr-key" if slot == "6" else "")
    calls = []

    def fetch(key, since):
        calls.append((key, since))
        if isinstance(spent, Exception):
            raise spent
        return spent

    b._fetch_spent = fetch
    return b, calls


def test_unconfigured_slot_has_no_budget(tmp_path):
    b, _ = _budgets(tmp_path, 10.0)
    assert b.status("9") is None


def test_pct_and_shared_org_pool(tmp_path):
    b, calls = _budgets(tmp_path, 50.0)
    st5, st6 = b.status("5"), b.status("6")
    assert st5.pct == 25.0 and st6.pct == 25.0 and not st5.blocked
    assert len(calls) == 1  # second slot served from the cache
    assert "25% used" in describe(st5)


def test_cap_blocks(tmp_path):
    b, _ = _budgets(tmp_path, 191.0)
    st = b.status("5")
    assert st.exhausted and st.blocked and "held out of rotation" in describe(st)


def test_expiry_blocks(tmp_path):
    b, _ = _budgets(tmp_path, 1.0, expiresAt="2020-01-01T00:00:00Z")
    assert b.status("5").expired and b.status("5").blocked


def test_fetch_failure_fails_open_then_serves_cache(tmp_path):
    b, _ = _budgets(tmp_path, OSError("down"))
    st = b.status("5")
    assert st.spent_usd is None and not st.blocked and "spend unknown" in describe(st)
    (tmp_path / "cache").mkdir(exist_ok=True)
    (tmp_path / "cache" / "api-budget.json").write_text(json.dumps(
        {"acme": {"since": "2026-10-01T00:00:00Z", "spentUsd": 199.0, "readAt": 0}}))
    b2, _ = _budgets(tmp_path, OSError("down"))
    assert b2.status("5").blocked  # stale-but-known reading still protects
