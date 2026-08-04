"""`cswap add` must not store a credential that belongs to another account.

MEASURED IN THE FIELD (2026-08-03, work-mac): a session registered
j.lee8@ax.samsung.com and the slot received j.lee8@samsung.com's credential.
The shape: an ssh session had `.claude.json` renamed to the ax profile while
the live keychain item still held the ORIGINAL account's token. `add_account`
reads the identity from `.claude.json`'s `oauthAccount` and the credential
from the keychain/file store, and nothing asks whether the two agree.

The damage is silent and durable: the slot is LABELLED ax and CONTAINS the
other account, so every later switch to that slot logs the wrong user in,
and `cswap --status` shows a name that is not whose token is stored.

`oauth.fetch_oauth_profile` already answers exactly this question ("whose
token is this") and is used by the autoswitch identity oracle. add_account
does not call it.
"""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.exceptions import ConfigError, ValidationError

CREDS = json.dumps({"claudeAiOauth": {
    "accessToken": "sk-ant-oat01-THEIRS", "refreshToken": "rt-theirs",
    "expiresAt": 99999999999000}})


def _switcher(temp_home, mock_claude_config, email, org=""):
    s = ClaudeAccountSwitcher()
    s._setup_directories()
    s._init_sequence_file()
    cfg = s._get_claude_config_path()
    cfg.write_text(json.dumps({"oauthAccount": {
        "emailAddress": email, "organizationUuid": org}}), encoding="utf-8")
    return s


def test_add_refuses_a_credential_whose_owner_is_a_different_account(
    temp_home: Path, mock_claude_config: Path,
):
    """The config claims ax@; the live token resolves to somebody else.

    add_account must refuse rather than label the slot with the config's
    claim while storing the other account's token.
    """
    s = _switcher(temp_home, mock_claude_config, "ax@example.com")

    with patch.object(s, "_read_capture_credentials", return_value=CREDS), \
         patch("claude_swap.oauth.fetch_oauth_profile",
               return_value={"uuid": "u-other", "email": "other@example.com",
                             "organizationUuid": ""}):
        with pytest.raises((ConfigError, ValidationError)) as e:
            s.add_account(slot=7)

    msg = str(e.value).lower()
    assert "other@example.com" in msg or "does not" in msg or "mismatch" in msg, (
        f"the refusal must name the conflict, got: {e.value!r}"
    )
    data = s._get_sequence_data()
    assert "7" not in data.get("accounts", {}), (
        "DEFECT: the slot was created for ax@example.com while holding "
        "another account's credential"
    )


def test_CONTROL_add_accepts_when_the_token_is_the_claimed_account(
    temp_home: Path, mock_claude_config: Path,
):
    """Same path, agreeing identities: must still register normally.

    Without this the refusal above could pass by refusing everything.
    """
    s = _switcher(temp_home, mock_claude_config, "ax@example.com")

    with patch.object(s, "_read_capture_credentials", return_value=CREDS), \
         patch("claude_swap.oauth.fetch_oauth_profile",
               return_value={"uuid": "u-ax", "email": "ax@example.com",
                             "organizationUuid": ""}):
        s.add_account(slot=7, assume_yes=True)

    data = s._get_sequence_data()
    assert data["accounts"]["7"]["email"] == "ax@example.com"


def test_CONTROL_unresolvable_profile_still_registers(
    temp_home: Path, mock_claude_config: Path,
):
    """The oracle is ADVISORY, as its own docstring states: None means
    'unresolvable', never 'wrong'. Offline or a 401 must not block a
    registration that worked before this guard existed."""
    s = _switcher(temp_home, mock_claude_config, "ax@example.com")

    with patch.object(s, "_read_capture_credentials", return_value=CREDS), \
         patch("claude_swap.oauth.fetch_oauth_profile", return_value=None):
        s.add_account(slot=7, assume_yes=True)

    data = s._get_sequence_data()
    assert data["accounts"]["7"]["email"] == "ax@example.com"


def test_same_email_different_org_is_still_a_mismatch(
    temp_home: Path, mock_claude_config: Path,
):
    """Two managed slots may share an EMAIL across organizations -- the
    codebase says so where it compares identities elsewhere:

        `_live_identity_matches`: "Compares the organization too: two managed
        slots may share an email across orgs."

    So an email-only comparison here would accept the personal account's token
    for the org slot (and vice versa). They are different accounts with
    different quota, and the slot would be labelled with the org it is not in.

    MUTATION-DRIVEN: dropping `and seen_org == (org_uuid or "")` from the guard
    left 477 tests passing. This is the test that kills it.
    """
    s = _switcher(temp_home, mock_claude_config, "ax@example.com", org="org-A")

    with patch.object(s, "_read_capture_credentials", return_value=CREDS), \
         patch("claude_swap.oauth.fetch_oauth_profile",
               return_value={"uuid": "u-ax", "email": "ax@example.com",
                             "organizationUuid": "org-B"}):
        with pytest.raises((ConfigError, ValidationError)):
            s.add_account(slot=8)

    assert "8" not in s._get_sequence_data().get("accounts", {}), (
        "DEFECT: a token from a DIFFERENT organization was accepted for this "
        "slot because only the email was compared"
    )


def test_CONTROL_personal_account_empty_org_still_registers(
    temp_home: Path, mock_claude_config: Path,
):
    """The org comparison must not break the ordinary personal case, where
    both sides are the empty string. Without this the test above could pass by
    refusing every personal account."""
    s = _switcher(temp_home, mock_claude_config, "ax@example.com", org="")

    with patch.object(s, "_read_capture_credentials", return_value=CREDS), \
         patch("claude_swap.oauth.fetch_oauth_profile",
               return_value={"uuid": "u-ax", "email": "ax@example.com",
                             "organizationUuid": None}):
        s.add_account(slot=8, assume_yes=True)

    assert s._get_sequence_data()["accounts"]["8"]["email"] == "ax@example.com"
