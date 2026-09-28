"""The quarantine WARNING is what a person reads when diagnosing, and it fired
on the RAW strike count while the product's own verdict doubted that strike.

`_strike_is_suspected_race` excuses a FIRST strike that any success preceded:
the slot keeps being fetched and `token_dead` answers False. The log said "is quarantined ... re-login may be needed" anyway, so the
line named a remedy for a slot that was never quarantined -- the consume-gate
race reads exactly like an expired token in the one place a person looks.
"""

import json
import logging
import time

import pytest

from claude_swap import oauth
from claude_swap.usage_store import (
    AUTH_DEAD_STRIKES, FetchRecord as FR, UsageStore,
)

CREDS = json.dumps({"claudeAiOauth": {"accessToken": "sk-a",
                                      "refreshToken": "rt-a",
                                      "expiresAt": 9999999999000}})
FP = oauth.credential_fingerprint(CREDS)
IDENT = {"1": ("a@example.com", "")}


def _struck(tmp_path, gap, caplog, strikes=1):
    """Land `strikes` strikes `gap` seconds after a success; return (entry, lines)."""
    store = UsageStore(tmp_path)
    now = time.time()
    store.path.write_text(json.dumps({"schemaVersion": 2, "accounts": {"1": {
        "email": "a@example.com", "organizationUuid": "",
        "fetchedAt": now - gap, "lastAttemptAt": now - gap,
        "lastGood": {"five_hour": {"pct": 5.0}},
    }}}))
    with caplog.at_level(logging.INFO, logger="claude-swap"):
        for _ in range(strikes):
            store.record({"1": FR(error="invalid_grant", struck_fp=FP)}, IDENT)
    return store.entries(IDENT)["1"], [r.getMessage() for r in caplog.records]


def test_a_doubted_strike_is_not_reported_as_a_quarantine(tmp_path, caplog):
    entry, lines = _struck(tmp_path, 338.0, caplog)

    assert entry.auth_dead_strikes == AUTH_DEAD_STRIKES  # premise: it struck
    assert entry.token_dead() is False, "premise: the race doubt applies here"

    said = " ".join(lines)
    assert "re-login may be needed" not in said, said
    assert "doubt" in said or "race" in said, (
        "the line must say the strike is doubted and a retry is permitted")


def test_a_second_strike_still_reports_a_quarantine(tmp_path, caplog):
    """THE CONTROL. Without it the assertion above passes for a build that
    stopped logging strikes at all. A wide gap used to reach it; the doubt is
    unbounded now, so the COUNT is what carries a row out of it."""
    entry, lines = _struck(tmp_path, 660.0, caplog, strikes=2)

    assert entry.token_dead() is True, "premise: no doubt after two strikes"
    assert any("is quarantined" in m for m in lines), lines
    assert any("re-login may be needed" in m for m in lines), lines
