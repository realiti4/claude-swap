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


@pytest.mark.parametrize("foreign_org", ["", None], ids=["org-empty", "org-none"])
def test_add_refuses_a_credential_whose_owner_is_a_different_account(
    temp_home: Path, mock_claude_config: Path, foreign_org,
):
    """The config claims ax@; the live token resolves to somebody else.

    add_account must refuse rather than label the slot with the config's
    claim while storing the other account's token -- whether or not the
    resolved profile carries an organization block at all. A structurally
    absent org (``organizationUuid: None``, what a PERSONAL account's
    profile response looks like -- no ``organization`` key at all) says
    nothing about the ORG, but a disagreeing EMAIL is still a disagreement.
    """
    s = _switcher(temp_home, mock_claude_config, "ax@example.com")

    with patch.object(s, "_read_capture_credentials", return_value=CREDS), \
         patch("claude_swap.oauth.fetch_oauth_profile",
               return_value={"uuid": "u-other", "email": "other@example.com",
                             "organizationUuid": foreign_org}):
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


def test_expired_token_with_no_refresh_token_skips_the_profile_fetch(
    temp_home: Path, mock_claude_config: Path,
):
    """An access token that is expired AND carries no refresh token really is
    dead -- there is nothing left to revive it with, so the fetch is skipped
    and the registration proceeds exactly as an unresolvable profile would
    (the guard is advisory)."""
    s = _switcher(temp_home, mock_claude_config, "ax@example.com")
    EXPIRED_NO_REFRESH = json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat01-EXPIRED", "expiresAt": 1}})  # no refreshToken

    with patch.object(s, "_read_capture_credentials", return_value=EXPIRED_NO_REFRESH), \
         patch("claude_swap.oauth.fetch_oauth_profile") as mock_fetch:
        s.add_account(slot=7, assume_yes=True)

    mock_fetch.assert_not_called()
    assert s._get_sequence_data()["accounts"]["7"]["email"] == "ax@example.com"


def test_add_refuses_an_expired_foreign_credential_a_refresh_would_revive(
    temp_home: Path, mock_claude_config: Path,
):
    """An expired ACCESS token is not "already dead" when a refresh token is
    present: cswap itself revives exactly such credentials at switch time
    (``session.py``'s ``_bootstrap``). So the guard must refresh before
    giving up, then resolve identity from the REFRESHED token -- merely
    dropping the skip is not enough, since ``fetch_oauth_profile`` on the
    still-expired original token would just 401 and return None (advisory,
    fail-open), silently storing the foreign credential either way."""
    s = _switcher(temp_home, mock_claude_config, "ax@example.com")
    EXPIRED_FOREIGN = json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat01-THEIRS-STALE", "refreshToken": "rt-theirs",
        "expiresAt": 1}})
    REFRESHED_FOREIGN = json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat01-THEIRS-FRESH", "refreshToken": "rt-theirs-2",
        "expiresAt": 99999999999000}})

    with patch.object(s, "_read_capture_credentials", return_value=EXPIRED_FOREIGN), \
         patch("claude_swap.oauth.refresh_oauth_credentials",
               return_value=REFRESHED_FOREIGN) as mock_refresh, \
         patch("claude_swap.oauth.fetch_oauth_profile",
               return_value={"uuid": "u-other", "email": "other@example.com",
                             "organizationUuid": ""}) as mock_fetch:
        with pytest.raises((ConfigError, ValidationError)):
            s.add_account(slot=7, assume_yes=True)

    mock_refresh.assert_called_once_with(EXPIRED_FOREIGN)
    mock_fetch.assert_called_once_with("sk-ant-oat01-THEIRS-FRESH")
    assert "7" not in s._get_sequence_data().get("accounts", {})


def test_CONTROL_expired_own_credential_still_registers_after_a_refresh(
    temp_home: Path, mock_claude_config: Path,
):
    """Same refresh-then-verify path, agreeing identity: registration must
    still succeed. Also proves the guard's refresh never leaks into the
    stored credential -- the STORED bytes are the original (still-expired)
    ones, not the refreshed ones; the guard only borrows a fresh token to
    resolve identity, it does not persist anything."""
    s = _switcher(temp_home, mock_claude_config, "ax@example.com")
    EXPIRED_OWN = json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat01-MINE-STALE", "refreshToken": "rt-mine",
        "expiresAt": 1}})
    REFRESHED_OWN = json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat01-MINE-FRESH", "refreshToken": "rt-mine-2",
        "expiresAt": 99999999999000}})

    with patch.object(s, "_read_capture_credentials", return_value=EXPIRED_OWN), \
         patch("claude_swap.oauth.refresh_oauth_credentials", return_value=REFRESHED_OWN), \
         patch("claude_swap.oauth.fetch_oauth_profile",
               return_value={"uuid": "u-ax", "email": "ax@example.com",
                             "organizationUuid": ""}):
        s.add_account(slot=7, assume_yes=True)

    assert s._get_sequence_data()["accounts"]["7"]["email"] == "ax@example.com"
    stored = s._read_account_credentials("7", "ax@example.com")
    assert "MINE-STALE" in stored and "MINE-FRESH" not in stored, (
        "the guard's refresh must not leak into the stored credential"
    )


def test_CONTROL_expired_credential_whose_refresh_fails_still_registers(
    temp_home: Path, mock_claude_config: Path,
):
    """Fail-open direction: when the refresh itself cannot resolve anything
    (dead refresh token, network down), the guard must not block -- exactly
    like an unresolved profile fetch doesn't."""
    s = _switcher(temp_home, mock_claude_config, "ax@example.com")
    EXPIRED = json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat01-STALE", "refreshToken": "rt-dead",
        "expiresAt": 1}})

    with patch.object(s, "_read_capture_credentials", return_value=EXPIRED), \
         patch("claude_swap.oauth.refresh_oauth_credentials", return_value=None), \
         patch("claude_swap.oauth.fetch_oauth_profile") as mock_fetch:
        s.add_account(slot=7, assume_yes=True)

    mock_fetch.assert_not_called()
    assert s._get_sequence_data()["accounts"]["7"]["email"] == "ax@example.com"


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


@pytest.mark.parametrize("foreign_org", ["", None], ids=["org-empty", "org-none"])
def test_add_refuses_a_foreign_credential_on_refresh_in_place(
    temp_home: Path, mock_claude_config: Path, foreign_org,
):
    """The REFRESH-IN-PLACE branch (``slot=None``, account already registered)
    must refuse a foreign token exactly like the CREATE branch does -- again
    regardless of whether the resolved profile's org is structurally absent
    (personal account) or empty.

    Not a corner: the menu bar's "Refresh credentials", the TUI's "Add
    current login", and a plain `cswap` switch's auto-add all take this
    branch. Here the slot already carries the RIGHT label, so an unguarded
    refresh silently swaps in another account's bytes underneath a label
    that still reads correctly -- the exact field defect this module exists
    to close.
    """
    s = _switcher(temp_home, mock_claude_config, "ax@example.com")
    OWN = json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat01-MINE", "refreshToken": "rt-mine",
        "expiresAt": 99999999999000}})

    with patch.object(s, "_read_capture_credentials", return_value=OWN), \
         patch("claude_swap.oauth.fetch_oauth_profile",
               return_value={"uuid": "u-ax", "email": "ax@example.com",
                             "organizationUuid": ""}):
        s.add_account(slot=7, assume_yes=True)

    with patch.object(s, "_read_capture_credentials", return_value=CREDS), \
         patch("claude_swap.oauth.fetch_oauth_profile",
               return_value={"uuid": "u-other", "email": "other@example.com",
                             "organizationUuid": foreign_org}):
        with pytest.raises((ConfigError, ValidationError)):
            s.add_account()  # slot=None -> refresh-in-place

    stored = s._read_account_credentials("7", "ax@example.com")
    assert "THEIRS" not in stored, (
        "DEFECT: refresh-in-place stored another account's token under the "
        "slot's correct label"
    )


def test_CONTROL_refresh_in_place_still_accepts_the_owning_token(
    temp_home: Path, mock_claude_config: Path,
):
    """Same branch, agreeing identities: a routine credential refresh (e.g.
    after re-login) must still work. Without this the refusal above could
    pass by refusing every refresh-in-place."""
    s = _switcher(temp_home, mock_claude_config, "ax@example.com")
    OWN = json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat01-MINE", "refreshToken": "rt-mine",
        "expiresAt": 99999999999000}})

    with patch.object(s, "_read_capture_credentials", return_value=OWN), \
         patch("claude_swap.oauth.fetch_oauth_profile",
               return_value={"uuid": "u-ax", "email": "ax@example.com",
                             "organizationUuid": ""}):
        s.add_account(slot=7, assume_yes=True)

    OWN_ROTATED = json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat01-MINE-ROTATED", "refreshToken": "rt-mine-2",
        "expiresAt": 99999999999000}})

    with patch.object(s, "_read_capture_credentials", return_value=OWN_ROTATED), \
         patch("claude_swap.oauth.fetch_oauth_profile",
               return_value={"uuid": "u-ax", "email": "ax@example.com",
                             "organizationUuid": ""}):
        s.add_account()  # slot=None -> refresh-in-place, same account

    stored = s._read_account_credentials("7", "ax@example.com")
    assert "MINE-ROTATED" in stored


def test_org_slot_registers_when_the_resolved_profile_has_no_org(
    temp_home: Path, mock_claude_config: Path,
):
    """A resolved identity with ``organizationUuid: None`` means the profile
    response carried NO organization block -- structurally ABSENT, not "this
    token is personal". The guard's own sibling comparator
    (``_resolved_matches_slot_identity``) treats that input as unverifiable
    (``if r_org is None: return None``) and never condemns on it. The guard
    must agree: coercing ``None`` to ``""`` before comparing against an org
    slot's real org uuid turns "unverifiable" into "refused".
    """
    s = _switcher(temp_home, mock_claude_config, "ax@example.com", org="org-A")

    with patch.object(s, "_read_capture_credentials", return_value=CREDS), \
         patch("claude_swap.oauth.fetch_oauth_profile",
               return_value={"uuid": "u-ax", "email": "ax@example.com",
                             "organizationUuid": None}):
        s.add_account(slot=7, assume_yes=True)

    assert s._get_sequence_data()["accounts"]["7"]["email"] == "ax@example.com"


def test_CONTROL_org_slot_still_registers_when_the_org_matches(
    temp_home: Path, mock_claude_config: Path,
):
    """Beside the test above: a fully-resolved, AGREEING org must still
    register. Without this a blanket "never refuse" could pass the None case
    by refusing nothing at all."""
    s = _switcher(temp_home, mock_claude_config, "ax@example.com", org="org-A")

    with patch.object(s, "_read_capture_credentials", return_value=CREDS), \
         patch("claude_swap.oauth.fetch_oauth_profile",
               return_value={"uuid": "u-ax", "email": "ax@example.com",
                             "organizationUuid": "org-A"}):
        s.add_account(slot=7, assume_yes=True)

    assert s._get_sequence_data()["accounts"]["7"]["email"] == "ax@example.com"


def test_CONTROL_org_slot_still_refuses_a_different_email(
    temp_home: Path, mock_claude_config: Path,
):
    """Beside the test above: a fully-resolved identity that disagrees on
    EMAIL must still refuse. Without this a blanket "never refuse" could pass
    the None case by refusing nothing at all."""
    s = _switcher(temp_home, mock_claude_config, "ax@example.com", org="org-A")

    with patch.object(s, "_read_capture_credentials", return_value=CREDS), \
         patch("claude_swap.oauth.fetch_oauth_profile",
               return_value={"uuid": "u-o", "email": "other@example.com",
                             "organizationUuid": "org-A"}):
        with pytest.raises((ConfigError, ValidationError)):
            s.add_account(slot=7, assume_yes=True)

    assert "7" not in s._get_sequence_data().get("accounts", {})


def test_org_mismatch_message_names_both_organizations(
    temp_home: Path, mock_claude_config: Path,
):
    """When only the org differs, ``seen == email``, so a message built from
    the email alone reads as if the SAME address disagreed with itself. It
    must name the two conflicting organizations instead, the way
    ``_reject_live_api_key_capture`` and the stash path name their specific
    conflicting values."""
    s = _switcher(temp_home, mock_claude_config, "ax@example.com", org="org-A")

    with patch.object(s, "_read_capture_credentials", return_value=CREDS), \
         patch("claude_swap.oauth.fetch_oauth_profile",
               return_value={"uuid": "u-ax2", "email": "ax@example.com",
                             "organizationUuid": "org-B"}):
        with pytest.raises((ConfigError, ValidationError)) as e:
            s.add_account(slot=7, assume_yes=True)

    msg = str(e.value)
    assert "org-A" in msg and "org-B" in msg, (
        f"the org-mismatch refusal must name both organizations, got: {msg!r}"
    )


def test_email_mismatch_message_still_names_both_addresses(
    temp_home: Path, mock_claude_config: Path,
):
    """CONTROL for the message test above: when the EMAIL differs, the
    message is coherent today (it names the two different addresses) -- this
    must not regress while fixing the org arm."""
    s = _switcher(temp_home, mock_claude_config, "ax@example.com")

    with patch.object(s, "_read_capture_credentials", return_value=CREDS), \
         patch("claude_swap.oauth.fetch_oauth_profile",
               return_value={"uuid": "u-other", "email": "other@example.com",
                             "organizationUuid": ""}):
        with pytest.raises((ConfigError, ValidationError)) as e:
            s.add_account(slot=7, assume_yes=True)

    msg = str(e.value)
    assert "ax@example.com" in msg and "other@example.com" in msg


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
