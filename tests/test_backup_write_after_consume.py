"""A consumed refresh whose BACKUP write fails leaves spent bytes in the slot.

`_fetch_active_usage` consumes the refresh grant and then writes the rotated
credential to two places: the slot's backup and the active store. A refresh
token is one-time-use, so the moment the POST returns, the OLD bytes are spent
forever. The active-store write has a consequence when it fails
(`USAGE_TOKEN_EXPIRED`); the backup write sets `backup_ok = False` and nothing
reads it. The fetch then reports success while the slot keeps the spent token,
and every later refresh for that slot answers `invalid_grant` — the state the
TUI renders as "re-login needed" for an account nobody did anything wrong to.
"""
import json
import pytest

from claude_swap import oauth
from claude_swap.switcher import ClaudeAccountSwitcher


def _creds(refresh: str, expires_at: int) -> str:
    return json.dumps({"claudeAiOauth": {
        "accessToken": "sk-" + refresh,
        "refreshToken": refresh,
        "expiresAt": expires_at,
        "refreshTokenExpiresAt": 99_999_999_999_999,
    }})


SPENT = _creds("rt-spent", 1)                       # expired -> a refresh is due
ROTATED = _creds("rt-rotated", 99_999_999_999_999)


@pytest.fixture
def gate(temp_home, mock_claude_config, sample_sequence_data, monkeypatch):
    s = ClaudeAccountSwitcher()
    s._setup_directories()
    s._write_json(s.sequence_file, sample_sequence_data)
    s._write_account_credentials("1", "a@example.com", SPENT)
    # WITHOUT THIS THE GATE DEFERS BEFORE THE POST and the whole file is
    # a test of nothing: measured, `calls=0` and `sentinel="token expired"`.
    monkeypatch.setattr(s, "_live_identity_matches", lambda *a, **k: True)
    s._write_credentials(SPENT)
    monkeypatch.setattr(
        oauth, "try_refresh_oauth_credentials",
        lambda *a, **k: oauth.RefreshOutcome(ROTATED, None),
    )
    monkeypatch.setattr(oauth, "request_usage_data", lambda *a, **k: {})
    monkeypatch.setattr(oauth, "build_usage_result", lambda *a, **k: None)
    return s


def test_a_failed_backup_write_does_not_strand_the_spent_token(gate, monkeypatch):
    """THE DEFECT. One transient backup-write failure and the slot keeps bytes
    the server has already retired."""
    calls = []
    real = gate._write_account_credentials

    def flaky(num, email, creds):
        calls.append(creds)
        if len(calls) == 1:
            raise OSError("transient")
        return real(num, email, creds)

    monkeypatch.setattr(gate, "_write_account_credentials", flaky)
    monkeypatch.setattr(gate, "_write_credentials", lambda c: None)
    gate._fetch_active_usage("1", "a@example.com", SPENT)

    stored = gate._read_account_credentials("1", "a@example.com")
    assert stored, "the slot lost its credential entirely"
    got = json.loads(stored)["claudeAiOauth"]["refreshToken"]
    assert got == "rt-rotated", (
        "the slot still holds the SPENT refresh token after a consumed "
        f"refresh — got {got!r}; every later refresh for this slot will "
        "answer invalid_grant and the TUI will ask for a re-login"
    )


def test_CONTROL_a_backup_write_that_succeeds_is_not_retried(gate, monkeypatch):
    """The retry must be keyed on the FAILURE, not run unconditionally: a
    second write on the happy path shifts `.prev` and costs a lock for
    nothing."""
    calls = []
    real = gate._write_account_credentials
    monkeypatch.setattr(
        gate, "_write_account_credentials",
        lambda n, e, c: (calls.append(c), real(n, e, c))[1],
    )
    monkeypatch.setattr(gate, "_write_credentials", lambda c: None)
    gate._fetch_active_usage("1", "a@example.com", SPENT)
    assert len(calls) == 1, f"wrote the backup {len(calls)} times on a clean path"
