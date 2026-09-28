"""`cswap add` must not store a credential that belongs to another account.

MEASURED IN THE FIELD (2026-08-03, work-mac): a session registered
one address and the slot received a DIFFERENT account's credential.
The shape: an ssh session had `.claude.json` renamed to the new profile while
the live keychain item still held the ORIGINAL account's token. `add_account`
reads the identity from `.claude.json`'s `oauthAccount` and the credential
from the keychain/file store, and nothing asks whether the two agree.

The damage is silent and durable: the slot is LABELLED one account and CONTAINS the
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

from claude_swap.models import Platform
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

    Not a corner: the menu bar's "Refresh current credentials"
    (`on_refresh_creds`) and "From current login" (`on_add_login`), and the
    TUI's "Add current login", all call `add_account` with no slot and take
    this branch. `cswap`'s auto-add does
    NOT -- it fires only when the active account is unmanaged, and this branch
    requires that it IS managed, the same predicate on the same two arguments.
    Here the slot already carries the RIGHT label, so an unguarded
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


def test_an_expired_credential_is_unresolvable_and_never_consumes_a_grant(
    temp_home: Path, mock_claude_config: Path,
):
    """The guard must not POST a refresh grant.

    Everywhere else that act is a coordinated transition: under the account
    FileLock plus CC's credential locks, with the successor written to BOTH
    the backup and the active store, because an accepted rotation retires its
    predecessor server-side. A bare refresh here rotates the lineage and then
    stores the successor only in the slot backup -- the active store keeps the
    spent generation and the running session's next refresh gets
    ``invalid_grant``. On the refusal path it is worse: the guard raises and
    the rotated credential is dropped, so the only usable copy of that
    lineage's live generation is gone while the error says nothing changed.

    Expired therefore means unresolvable, exactly like an offline profile
    fetch: register, fail-open, and leave the grant alone.
    """
    s = _switcher(temp_home, mock_claude_config, "ax@example.com")
    EXPIRED = json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat01-STALE", "refreshToken": "rt-live",
        "expiresAt": 1}})

    with patch.object(s, "_read_capture_credentials", return_value=EXPIRED), \
         patch("claude_swap.oauth.refresh_oauth_credentials") as mock_refresh, \
         patch("claude_swap.oauth.fetch_oauth_profile") as mock_fetch:
        s.add_account(slot=7, assume_yes=True)

    mock_refresh.assert_not_called()
    mock_fetch.assert_not_called()
    assert "7" in s._get_sequence_data().get("accounts", {})


def test_a_matching_uuid_accepts_even_when_the_email_changed(
    temp_home: Path, mock_claude_config: Path,
):
    """Uuid first: an account whose email changed is still that account.

    The config carries the address it was registered under; the profile
    carries today's. Refusing on the address alone rejects a legitimate add
    after an email change, which the codebase's own comparator
    (``_resolved_matches_slot_identity``) avoids by comparing uuids first.
    """
    s = ClaudeAccountSwitcher()
    s._setup_directories()
    s._init_sequence_file()
    s._get_claude_config_path().write_text(json.dumps({"oauthAccount": {
        "emailAddress": "old@example.com", "organizationUuid": "",
        "accountUuid": "u-same"}}), encoding="utf-8")

    with patch.object(s, "_read_capture_credentials", return_value=CREDS), \
         patch("claude_swap.oauth.fetch_oauth_profile",
               return_value={"uuid": "u-same", "email": "new@example.com",
                             "organizationUuid": ""}):
        s.add_account(slot=7, assume_yes=True)

    assert "7" in s._get_sequence_data().get("accounts", {})


def test_a_recycled_email_under_a_different_uuid_is_refused(
    temp_home: Path, mock_claude_config: Path,
):
    """The other direction: the same address can belong to another account.

    Email-only accepts a deleted-and-recreated account under a recycled
    address, which is precisely the drift this guard exists to catch.
    """
    s = ClaudeAccountSwitcher()
    s._setup_directories()
    s._init_sequence_file()
    s._get_claude_config_path().write_text(json.dumps({"oauthAccount": {
        "emailAddress": "ax@example.com", "organizationUuid": "",
        "accountUuid": "u-registered"}}), encoding="utf-8")

    with patch.object(s, "_read_capture_credentials", return_value=CREDS), \
         patch("claude_swap.oauth.fetch_oauth_profile",
               return_value={"uuid": "u-recreated", "email": "ax@example.com",
                             "organizationUuid": ""}):
        with pytest.raises((ConfigError, ValidationError)):
            s.add_account(slot=7, assume_yes=True)

    assert "7" not in s._get_sequence_data().get("accounts", {})


def test_an_expired_FOREIGN_credential_registers_and_that_is_deliberate(
    temp_home: Path, mock_claude_config: Path,
):
    """The case this guard knowingly does NOT catch, so nobody "fixes" it back.

    An expired foreign token could be revived and unmasked by a refresh, and
    an earlier revision did exactly that. It was removed: the POST consumes a
    grant, and consuming one is a coordinated transition everywhere else in
    this codebase -- under the account FileLock and CC's credential locks,
    with the successor persisted to BOTH stores because an accepted rotation
    retires its predecessor server-side. A bare refresh here strands the
    active store on the spent generation, and on the refusal path throws away
    the only live copy of that lineage.

    So expired means unresolvable and the add proceeds, fail-open, exactly
    like an offline profile fetch. Catching expired-foreign belongs on the
    consume-gate machinery, not here. The field incident's shape -- a LIVE
    token -- is still caught, which is what this guard is for.
    """
    s = _switcher(temp_home, mock_claude_config, "ax@example.com")
    EXPIRED_FOREIGN = json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat01-THEIRS-STALE", "refreshToken": "rt-theirs",
        "expiresAt": 1}})

    with patch.object(s, "_read_capture_credentials", return_value=EXPIRED_FOREIGN), \
         patch("claude_swap.oauth.refresh_oauth_credentials") as mock_refresh, \
         patch("claude_swap.oauth.fetch_oauth_profile") as mock_fetch:
        s.add_account(slot=7, assume_yes=True)

    # The point is not just that it registers -- it is that no grant moved.
    mock_refresh.assert_not_called()
    mock_fetch.assert_not_called()
    assert "7" in s._get_sequence_data().get("accounts", {})


def test_an_unverifiable_ownership_check_says_so_out_loud(
    temp_home: Path, mock_claude_config: Path, capsys,
):
    """Fail-open must not be silent.

    The guard proceeds on an unresolvable answer by design -- offline, a 401,
    schema drift -- but registering with the ownership question unanswered is
    exactly the state the field incident was in. Say it, so the user can
    re-run somewhere the check can complete.
    """
    s = _switcher(temp_home, mock_claude_config, "ax@example.com")

    with patch.object(s, "_read_capture_credentials", return_value=CREDS), \
         patch("claude_swap.oauth.fetch_oauth_profile", return_value=None):
        s.add_account(slot=7, assume_yes=True)

    out = capsys.readouterr().out
    assert "7" in s._get_sequence_data().get("accounts", {}), "premise: it must still register"
    assert "could not" in out.lower() or "unverified" in out.lower(), (
        f"fail-open was silent; stdout was: {out!r}"
    )


def test_a_login_landing_during_the_guards_network_window_is_refused(
    temp_home: Path, mock_claude_config: Path,
):
    """The identity verified must be the identity stored.

    `add_account` reads `.claude.json` for the identity, verifies the
    credential against it over the network, and reads the config AGAIN for the
    fields it writes. A `/login` landing in that window pairs one account's
    token with another's metadata -- the same LABELLED-one/CONTAINS-another
    shape this guard exists to close, arriving by a different door.

    A config snapshot alone cannot see it: the credential can move too. The
    fingerprint of what was verified must still be what is about to be stored.
    """
    s = _switcher(temp_home, mock_claude_config, "ax@example.com")
    cfg = s._get_claude_config_path()

    def login_lands(token):
        # A different account signs in while the profile lookup is in flight.
        cfg.write_text(json.dumps({"oauthAccount": {
            "emailAddress": "someone-else@example.com", "organizationUuid": "",
            "accountUuid": "u-someone-else"}}), encoding="utf-8")
        return {"uuid": "u-ax", "email": "ax@example.com", "organizationUuid": ""}

    with patch.object(s, "_read_capture_credentials", return_value=CREDS), \
         patch("claude_swap.oauth.fetch_oauth_profile", side_effect=login_lands):
        with pytest.raises((ConfigError, ValidationError)):
            s.add_account(slot=7, assume_yes=True)

    assert "7" not in s._get_sequence_data().get("accounts", {})


def test_a_refresh_landing_in_the_guards_window_is_refused(
    temp_home: Path, mock_claude_config: Path,
):
    """The credential verified must be the credential stored.

    A `/login` moves `oauthAccount`, so the identity guard sees it. A plain
    refresh of the SAME account does not: the identity is unchanged and only
    the credential moved. Storing the pre-refresh bytes hands the slot a
    generation the server has already retired, so the slot's next refresh gets
    `invalid_grant` -- the failure this PR's own refusal path exists to avoid.

    A config snapshot cannot see this; only the credential's own fingerprint
    can.
    """
    s = _switcher(temp_home, mock_claude_config, "ax@example.com")

    def creds(tag):
        return json.dumps({"claudeAiOauth": {
            "accessToken": "sk-ant-oat01-" + tag,
            "refreshToken": "sk-ant-ort01-" + tag,
            "expiresAt": 9999999999999}})

    live = {"v": creds("OLD")}

    def rotates_during_lookup(token):
        live["v"] = creds("NEW")
        return {"uuid": "u-ax", "email": "ax@example.com",
                "organizationUuid": ""}

    with patch.object(s, "_read_capture_credentials",
                      side_effect=lambda *a, **k: live["v"]), \
         patch("claude_swap.oauth.fetch_oauth_profile",
               side_effect=rotates_during_lookup):
        with pytest.raises((ConfigError, ValidationError)):
            s.add_account(slot=7, assume_yes=True)

    assert "7" not in s._get_sequence_data().get("accounts", {})


def test_a_matching_uuid_does_not_excuse_a_different_org(
    temp_home: Path, mock_claude_config: Path,
):
    """I2: the uuid arm must not skip the org check.

    `_resolved_matches_slot_identity` ANDs the org in and calls "a matching
    email under a different org" definitively another account. Returning early
    on a uuid match accepts exactly that, and it also makes the org-mismatch
    message unreachable for any config Claude Code wrote -- every such config
    carries an accountUuid.
    """
    s = ClaudeAccountSwitcher()
    s._setup_directories(); s._init_sequence_file()
    s._get_claude_config_path().write_text(json.dumps({"oauthAccount": {
        "emailAddress": "ax@example.com", "organizationUuid": "",
        "accountUuid": "u-ax"}}), encoding="utf-8")

    with patch.object(s, "_read_capture_credentials", return_value=CREDS), \
         patch("claude_swap.oauth.fetch_oauth_profile",
               return_value={"uuid": "u-ax", "email": "ax@example.com",
                             "organizationUuid": "org-X"}):
        with pytest.raises((ConfigError, ValidationError)):
            s.add_account(slot=7, assume_yes=True)

    assert "7" not in s._get_sequence_data().get("accounts", {})


def test_a_null_account_uuid_does_not_trip_the_commit_time_recheck(
    temp_home: Path, mock_claude_config: Path,
):
    """I3: `null` on one side and "" on the other must not read as a change.

    `_get_current_identity_triple` normalises with `or ""`; the second read did
    not, so a JSON null made the recheck refuse with a message naming the SAME
    address as both before and after -- and no re-run could clear it.
    """
    s = ClaudeAccountSwitcher()
    s._setup_directories(); s._init_sequence_file()
    s._get_claude_config_path().write_text(json.dumps({"oauthAccount": {
        "emailAddress": "ax@example.com", "organizationUuid": "",
        "accountUuid": None}}), encoding="utf-8")

    with patch.object(s, "_read_capture_credentials", return_value=CREDS), \
         patch("claude_swap.oauth.fetch_oauth_profile",
               return_value={"uuid": "u-ax", "email": "ax@example.com",
                             "organizationUuid": ""}):
        s.add_account(slot=7, assume_yes=True)

    acct = s._get_sequence_data().get("accounts", {}).get("7")
    assert acct is not None
    # And the null must not reach the roster either: `"uuid": None` in
    # sequence.json is absorbed downstream today, which is exactly how it
    # would survive unnoticed until something stops absorbing it.
    assert acct.get("uuid") == "", f"stored uuid is {acct.get('uuid')!r}, not ''"


def test_the_refresh_in_place_path_also_refuses_a_login_in_the_window(
    temp_home: Path, mock_claude_config: Path,
):
    """C1: the recheck must cover BOTH write paths, not just the create one.

    `slot=None` on an already-registered account is the branch the menu bar,
    the TUI and a bare `cswap --add-account` all take -- the dominant one. It
    reads `.claude.json` a second time for the blob it stores, and a later
    switch installs that blob's `oauthAccount` as the identity. So a `/login`
    in the guard's window puts account B's identity on slot A's credential:
    the field incident, through the door the other path already closed.
    """
    s = _switcher(temp_home, mock_claude_config, "ax@example.com")
    with patch.object(s, "_read_capture_credentials", return_value=CREDS), \
         patch("claude_swap.oauth.fetch_oauth_profile",
               return_value={"uuid": "u-ax", "email": "ax@example.com",
                             "organizationUuid": ""}):
        s.add_account(slot=7, assume_yes=True)
    assert "7" in s._get_sequence_data().get("accounts", {}), "premise: registered"

    cfg = s._get_claude_config_path()

    def login_lands(token):
        cfg.write_text(json.dumps({"oauthAccount": {
            "emailAddress": "someone-else@example.com", "organizationUuid": "",
            "accountUuid": "u-other"}}), encoding="utf-8")
        return {"uuid": "u-ax", "email": "ax@example.com", "organizationUuid": ""}

    with patch.object(s, "_read_capture_credentials", return_value=CREDS), \
         patch("claude_swap.oauth.fetch_oauth_profile", side_effect=login_lands):
        with pytest.raises((ConfigError, ValidationError)):
            s.add_account(assume_yes=True)          # slot=None -> refresh in place

    blob = s.configs_dir / ".claude-config-7-ax@example.com.json"
    stored = json.loads(blob.read_text())
    assert stored["oauthAccount"]["emailAddress"] == "ax@example.com", (
        f"slot 7's stored config now says {stored['oauthAccount']['emailAddress']}"
    )


def test_the_guard_receives_the_triple_THAT_WAS_READ_not_a_rebuild(
    temp_home: Path, mock_claude_config: Path, monkeypatch: pytest.MonkeyPatch
):
    """Identity, not equality, and the distinction is the whole point.

    The caller unpacks the config triple into three names and a sibling change
    overwrites two of them with an un-spliced email and org while leaving the
    third literal. Rebuilding the tuple from those names then hands this guard
    a mix describing no real account -- one account's address with another's
    uuid -- which cannot match a fresh read, so the guard refuses every time
    rather than only on a race.

    A rebuilt tuple can still compare EQUAL here, so `==` would pass against
    the defect. `is` does not.
    """
    s = _switcher(temp_home, mock_claude_config, "a@e.com")
    read = s._get_current_identity_triple()
    assert read is not None, "fixture must produce a readable identity"

    got: list = []
    monkeypatch.setattr(
        type(s), "_reject_identity_drift_since_verify",
        lambda self, verified: got.append(verified),
    )
    monkeypatch.setattr(type(s), "_get_current_identity_triple", lambda self: read)
    with patch.object(s, "_read_capture_credentials", return_value=CREDS), \
         patch("claude_swap.oauth.fetch_oauth_profile",
               return_value={"uuid": read[2], "email": read[0],
                             "organizationUuid": read[1]}):
        try:
            s.add_account(slot=7, assume_yes=True)
        except Exception:
            pass

    assert got, "the guard was never called"
    assert got[0] is read, (
        "the guard was handed a REBUILT tuple; it must receive the object "
        "_get_current_identity_triple returned, or a sibling change that "
        "overwrites one of the unpacked names silently poisons it"
    )


def test_adopting_a_keychain_login_syncs_the_stale_plaintext_file(
    temp_home: Path, mock_claude_config: Path,
):
    """On macOS, ``add_account`` reads the identity/credential the way claude
    resolves them for capture -- the Keychain, not ``.credentials.json`` --
    and stores that into the slot. It never touches the plaintext file, so a
    file left behind by a PREVIOUS login stays on disk holding that older
    account until an unrelated refresh happens to write both stores. An
    ssh-born reader that falls back to the file (no GUI Keychain access)
    would then serve the superseded login.
    """
    s = _switcher(temp_home, mock_claude_config, "ax@example.com")
    s.platform = Platform.MACOS

    login_a = json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat01-LOGIN-A", "refreshToken": "rt-login-a",
        "expiresAt": 99999999999000}})
    login_b = json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat01-LOGIN-B", "refreshToken": "rt-login-b",
        "expiresAt": 99999999999000}})

    cred_file = temp_home / ".claude" / ".credentials.json"
    cred_file.write_text(login_a, encoding="utf-8")

    with patch.object(s, "_read_capture_credentials", return_value=login_b), \
         patch("claude_swap.oauth.fetch_oauth_profile",
               return_value={"uuid": "u-ax", "email": "ax@example.com",
                             "organizationUuid": ""}):
        s.add_account(slot=3, assume_yes=True)

    assert cred_file.read_text(encoding="utf-8") == login_b, (
        "DEFECT: the plaintext file still held login A's tokens after login "
        "B was adopted from the Keychain into the slot"
    )

    entries = s._store._list_unclaimed_credentials()
    stashed = [
        s._store._read_unclaimed_credential(eid)[0] for eid in entries
    ]
    assert any("rt-login-a" in c for c in stashed), (
        "DEFECT: login A's only copy was overwritten with nothing stashed"
    )


def test_sync_skips_when_secure_storage_profile_diverges_from_config_dir(
    temp_home: Path, mock_claude_config: Path, monkeypatch,
):
    """``_read_capture_credentials`` reads the credential from the profile
    ``CLAUDE_SECURESTORAGE_CONFIG_DIR`` names when that var is set -- never
    from ``get_claude_config_home()``'s file. With the two diverged, syncing
    that file to the captured credential is the cross-profile write
    ``_read_capture_credentials``'s own docstring refuses: it would write
    one profile's login over a DIFFERENT profile's file.
    """
    s = _switcher(temp_home, mock_claude_config, "ax@example.com")
    s.platform = Platform.MACOS

    other_profile = temp_home / "other-profile"
    other_profile.mkdir()
    monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", str(other_profile))

    login_a = json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat01-LOGIN-A", "refreshToken": "rt-login-a",
        "expiresAt": 99999999999000}})
    login_b = json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat01-LOGIN-B", "refreshToken": "rt-login-b",
        "expiresAt": 99999999999000}})

    cred_file = temp_home / ".claude" / ".credentials.json"
    cred_file.write_text(login_a, encoding="utf-8")

    with patch.object(s, "_read_capture_credentials", return_value=login_b), \
         patch("claude_swap.oauth.fetch_oauth_profile",
               return_value={"uuid": "u-ax", "email": "ax@example.com",
                             "organizationUuid": ""}):
        s.add_account(slot=3, assume_yes=True)

    assert cred_file.read_text(encoding="utf-8") == login_a, (
        "DEFECT: the config-dir profile's file was overwritten with a "
        "credential captured from a DIFFERENT profile named by "
        "CLAUDE_SECURESTORAGE_CONFIG_DIR"
    )


def test_sync_never_overwrites_a_fresher_plaintext_login_with_an_older_keychain_one(
    temp_home: Path, mock_claude_config: Path, monkeypatch,
):
    """MEASURED BY THE REVIEWER: ``CLAUDE_SECURESTORAGE_CONFIG_DIR=""`` (passes
    the profile guard -- the default profile IS the config home), Keychain
    holds an OLDER generation (``refreshTokenExpiresAt`` 427ms earlier, well
    inside the same-lineage jitter, and an earlier ``expiresAt``), the file
    holds the NEWER one. ``_read_capture_credentials`` routes through
    ``session.read_config_dir_credentials`` (Keychain-first, no freshness
    arbitration), so the captured credential is the OLDER Keychain value --
    and the sync must not let it clobber the file's newer, still-live
    refresh token with a spent one. ``_write_active_credentials_file`` is
    mkstemp+replace with no backup: once overwritten, the newer token is
    gone from disk for good.
    """
    s = _switcher(temp_home, mock_claude_config, "ax@example.com")
    s.platform = Platform.MACOS
    monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", "")

    kc_refresh = 88_888_888_888_000  # arbitrary, not a wall-clock epoch
    file_refresh = kc_refresh + 427  # same lineage, inside the 5s jitter
    kc_old = json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat01-OLD", "refreshToken": "rt-OLD-spent",
        "expiresAt": 99999999999000,
        "refreshTokenExpiresAt": kc_refresh}})
    file_new = json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat01-NEW", "refreshToken": "rt-NEW-live",
        "expiresAt": 99999999999000 + 3_600_000,
        "refreshTokenExpiresAt": file_refresh}})

    cred_file = temp_home / ".claude" / ".credentials.json"
    cred_file.write_text(file_new, encoding="utf-8")

    with patch.object(s, "_read_capture_credentials", return_value=kc_old), \
         patch("claude_swap.oauth.fetch_oauth_profile",
               return_value={"uuid": "u-ax", "email": "ax@example.com",
                             "organizationUuid": ""}):
        s.add_account(slot=3, assume_yes=True)

    assert cred_file.read_text(encoding="utf-8") == file_new, (
        "DEFECT: the file's newer, still-live refresh token was overwritten "
        "by an older, already-spent Keychain login"
    )


def test_sync_writes_through_an_older_generation_of_the_same_login_with_no_stash(
    temp_home: Path, mock_claude_config: Path, monkeypatch,
):
    """The file holds an OLDER generation of the SAME login being adopted --
    different access/refresh tokens (a refresh rotates both), but
    ``refreshTokenExpiresAt`` within the lineage jitter and an older
    ``expiresAt``. ``credential_fingerprint`` hashes the refresh token, which
    rotates same as a new login would, so this generation must not be
    scored as a different login and stashed; it must write through with no
    stash.
    """
    s = _switcher(temp_home, mock_claude_config, "ax@example.com")
    s.platform = Platform.MACOS
    monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", "")

    kc_refresh = 88_888_888_888_000
    file_refresh = kc_refresh - 721  # same lineage, well inside the 5s jitter
    kc_new = json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat01-NEW", "refreshToken": "rt-NEW",
        "expiresAt": 99999999999000 + 3_600_000,
        "refreshTokenExpiresAt": kc_refresh}})
    file_old = json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat01-OLD", "refreshToken": "rt-OLD",
        "expiresAt": 99999999999000,
        "refreshTokenExpiresAt": file_refresh}})  # same lineage, older generation

    cred_file = temp_home / ".claude" / ".credentials.json"
    cred_file.write_text(file_old, encoding="utf-8")

    with patch.object(s, "_read_capture_credentials", return_value=kc_new), \
         patch("claude_swap.oauth.fetch_oauth_profile",
               return_value={"uuid": "u-ax", "email": "ax@example.com",
                             "organizationUuid": ""}):
        s.add_account(slot=3, assume_yes=True)

    assert cred_file.read_text(encoding="utf-8") == kc_new, (
        "DEFECT: the file's older generation of the same login was not "
        "synced to the adopted generation"
    )
    assert s._store._list_unclaimed_credentials() == {}, (
        "DEFECT: an older generation of the SAME login was stashed as a "
        "displaced different login"
    )


def test_sync_never_raises_out_of_add_account_on_an_unparseable_file(
    temp_home: Path, mock_claude_config: Path,
):
    """The file's oauth payload must never be trusted to be a dict.
    ``extract_oauth_data`` calls ``.get`` on whatever ``json.loads`` returns,
    so a file holding a bare JSON scalar (``42``) raises ``AttributeError`` --
    and unhandled, that propagates out of ``add_account`` BETWEEN the slot
    write and the config/sequence write, leaving the roster half-updated."""
    s = _switcher(temp_home, mock_claude_config, "ax@example.com")
    s.platform = Platform.MACOS

    cred_file = temp_home / ".claude" / ".credentials.json"
    cred_file.write_text("42", encoding="utf-8")

    with patch.object(s, "_read_capture_credentials", return_value=CREDS), \
         patch("claude_swap.oauth.fetch_oauth_profile",
               return_value={"uuid": "u-ax", "email": "ax@example.com",
                             "organizationUuid": ""}):
        s.add_account(slot=3, assume_yes=True)

    assert s._get_sequence_data()["accounts"]["3"]["email"] == "ax@example.com", (
        "DEFECT: add_account raised before the slot was fully registered"
    )
    assert cred_file.read_text(encoding="utf-8") == CREDS, (
        "DEFECT: an unparseable file was left unrepaired instead of being "
        "written through"
    )


def test_sync_writes_through_a_file_with_undecodable_bytes(
    temp_home: Path, mock_claude_config: Path,
):
    """A file that isn't even valid UTF-8 holds no login to compare or stash
    either -- same write-through as an unparseable JSON scalar."""
    s = _switcher(temp_home, mock_claude_config, "ax@example.com")
    s.platform = Platform.MACOS

    cred_file = temp_home / ".claude" / ".credentials.json"
    cred_file.write_bytes(b"\xff\xfe\x00\x01")

    with patch.object(s, "_read_capture_credentials", return_value=CREDS), \
         patch("claude_swap.oauth.fetch_oauth_profile",
               return_value={"uuid": "u-ax", "email": "ax@example.com",
                             "organizationUuid": ""}):
        s.add_account(slot=3, assume_yes=True)

    assert cred_file.read_text(encoding="utf-8") == CREDS, (
        "DEFECT: a file with undecodable bytes was left unrepaired instead "
        "of being written through"
    )


def test_rotation_resync_syncs_the_stale_plaintext_file_to_the_active_slot(
    temp_home: Path, mock_claude_config: Path, monkeypatch,
):
    """``_resync_rotated_backup`` writes a rotation that completed outside a
    collect pass into the ACTIVE slot's Keychain backup, but never touched
    ``.credentials.json`` -- so an ssh-born reader falling back to the
    plaintext file kept serving the superseded generation until an unrelated
    switch happened to write both stores.
    """
    s = ClaudeAccountSwitcher()
    s.platform = Platform.MACOS
    s._setup_directories()
    s._init_sequence_file()
    cfg = s._get_claude_config_path()
    cfg.write_text(json.dumps({"oauthAccount": {
        "emailAddress": "ax@example.com", "organizationUuid": "",
        "accountUuid": "u-ax"}}), encoding="utf-8")
    seq = s._get_sequence_data()
    seq["accounts"]["1"] = {
        "email": "ax@example.com", "uuid": "u-ax", "organizationUuid": ""}
    seq["sequence"] = [1]
    seq["activeAccountNumber"] = 1
    s._write_json(s.sequence_file, seq)

    old_backup = json.dumps({"claudeAiOauth": {
        "accessToken": "sk-old-backup", "refreshToken": "rt-OLD-lineage",
        "expiresAt": 99999999999000, "refreshTokenExpiresAt": 1_000}})
    s._write_account_credentials("1", "ax@example.com", old_backup)

    kc_new = json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat01-KC-N-PLUS-1", "refreshToken": "rt-NEW",
        "expiresAt": 99999999999000 + 3_600_000,
        "refreshTokenExpiresAt": 50_000}})  # generation N+1
    file_n = json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat01-FILE-N", "refreshToken": "rt-NEW",
        "expiresAt": 99999999999000,
        "refreshTokenExpiresAt": 48_000}})  # same lineage, generation N

    cred_file = temp_home / ".claude" / ".credentials.json"
    cred_file.write_text(file_n, encoding="utf-8")

    monkeypatch.setattr(s, "_read_credentials", lambda: kc_new)

    with patch("claude_swap.oauth.fetch_oauth_profile",
               return_value={"uuid": "u-ax", "email": "ax@example.com",
                             "organizationUuid": ""}):
        s._resync_rotated_backup("1", "ax@example.com", "", kc_new)

    assert cred_file.read_text(encoding="utf-8") == kc_new, (
        "DEFECT: the plaintext file still held generation N after the "
        "rotation resync wrote generation N+1 into the slot backup"
    )


def test_resync_stashes_a_different_login_the_file_alone_held_before_overwriting(
    temp_home: Path, mock_claude_config: Path, monkeypatch,
):
    """MEASURED BY THE REVIEWER: the resync site reaches the SAME
    different-login arm of ``_sync_active_credentials_file_to_adopted_login``
    the add_account arm does -- the fingerprint pin compares the Keychain
    generation to ``creds``, never to the file, so a file holding a
    different, older login is unaffected by the pin. A file login the resync
    is about to overwrite must be stashed first, same as the add_account
    arm.
    """
    s = ClaudeAccountSwitcher()
    s.platform = Platform.MACOS
    s._setup_directories()
    s._init_sequence_file()
    cfg = s._get_claude_config_path()
    cfg.write_text(json.dumps({"oauthAccount": {
        "emailAddress": "ax@example.com", "organizationUuid": "",
        "accountUuid": "u-ax"}}), encoding="utf-8")
    seq = s._get_sequence_data()
    seq["accounts"]["1"] = {
        "email": "ax@example.com", "uuid": "u-ax", "organizationUuid": ""}
    seq["sequence"] = [1]
    seq["activeAccountNumber"] = 1
    s._write_json(s.sequence_file, seq)

    old_backup = json.dumps({"claudeAiOauth": {
        "accessToken": "sk-old-backup", "refreshToken": "rt-OLD-lineage",
        "expiresAt": 99999999999000, "refreshTokenExpiresAt": 1_000}})
    s._write_account_credentials("1", "ax@example.com", old_backup)

    kc_new = json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat01-KC-N-PLUS-1", "refreshToken": "rt-NEW",
        "expiresAt": 99999999999000 + 3_600_000,
        "refreshTokenExpiresAt": 50_000}})
    file_l = json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat01-FILE-L", "refreshToken": "rt-B-FILE-ONLY",
        "expiresAt": 99999999999000,
        "refreshTokenExpiresAt": 40_000}})  # a DIFFERENT login, 10s older

    cred_file = temp_home / ".claude" / ".credentials.json"
    cred_file.write_text(file_l, encoding="utf-8")

    monkeypatch.setattr(s, "_read_credentials", lambda: kc_new)

    with patch("claude_swap.oauth.fetch_oauth_profile",
               return_value={"uuid": "u-ax", "email": "ax@example.com",
                             "organizationUuid": ""}):
        s._resync_rotated_backup("1", "ax@example.com", "", kc_new)

    assert cred_file.read_text(encoding="utf-8") == kc_new, (
        "DEFECT: the plaintext file did not receive the rotated Keychain "
        "generation"
    )

    entries = s._store._list_unclaimed_credentials()
    bodies = [
        s._store._read_unclaimed_credential(eid)[0] for eid in entries
    ]
    assert any("rt-B-FILE-ONLY" in b for b in bodies), (
        "DEFECT: the resync overwrote login L's only copy without stashing it"
    )


def test_resync_writes_through_a_same_lineage_older_generation_with_no_stash(
    temp_home: Path, mock_claude_config: Path, monkeypatch,
):
    """A same-LINEAGE older generation (same ``refreshToken``, the file just
    hasn't caught up to the Keychain's later rotation) is not a different
    login to preserve -- its refresh token is the one about to be persisted
    to the slot backup by this same resync. The stash arm must gate on the
    ``refreshTokenExpiresAt`` stamp (the lineage; ``credential_fingerprint``
    is a generation, used only when a stamp is missing), not on a
    byte-for-byte payload diff, or the ordinary rotation-resync case writes
    a stash row for nothing every time.
    """
    s = ClaudeAccountSwitcher()
    s.platform = Platform.MACOS
    s._setup_directories()
    s._init_sequence_file()
    cfg = s._get_claude_config_path()
    cfg.write_text(json.dumps({"oauthAccount": {
        "emailAddress": "ax@example.com", "organizationUuid": "",
        "accountUuid": "u-ax"}}), encoding="utf-8")
    seq = s._get_sequence_data()
    seq["accounts"]["1"] = {
        "email": "ax@example.com", "uuid": "u-ax", "organizationUuid": ""}
    seq["sequence"] = [1]
    seq["activeAccountNumber"] = 1
    s._write_json(s.sequence_file, seq)

    old_backup = json.dumps({"claudeAiOauth": {
        "accessToken": "sk-old-backup", "refreshToken": "rt-OLD-lineage",
        "expiresAt": 99999999999000, "refreshTokenExpiresAt": 1_000}})
    s._write_account_credentials("1", "ax@example.com", old_backup)

    kc_new = json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat01-KC-N-PLUS-1", "refreshToken": "rt-NEW",
        "expiresAt": 99999999999000 + 3_600_000,
        "refreshTokenExpiresAt": 50_000}})
    file_n = json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat01-FILE-N", "refreshToken": "rt-NEW",
        "expiresAt": 99999999999000,
        "refreshTokenExpiresAt": 48_000}})  # same lineage, generation N

    cred_file = temp_home / ".claude" / ".credentials.json"
    cred_file.write_text(file_n, encoding="utf-8")

    monkeypatch.setattr(s, "_read_credentials", lambda: kc_new)

    with patch("claude_swap.oauth.fetch_oauth_profile",
               return_value={"uuid": "u-ax", "email": "ax@example.com",
                             "organizationUuid": ""}):
        s._resync_rotated_backup("1", "ax@example.com", "", kc_new)

    assert cred_file.read_text(encoding="utf-8") == kc_new, (
        "DEFECT: the plaintext file did not receive the rotated Keychain "
        "generation"
    )
    entries = s._store._list_unclaimed_credentials()
    assert entries == {}, (
        "DEFECT: a same-lineage older generation was stashed as if it were "
        f"a displaced different login: {entries!r}"
    )


def test_resync_writes_through_a_same_lineage_older_generation_even_if_the_stash_write_would_fail(
    temp_home: Path, mock_claude_config: Path, monkeypatch,
):
    """Same setup as above, but ``_write_unclaimed_credential`` is made to
    raise. A same-lineage older generation must never reach the stash at
    all, so the raise must never matter and the file must still sync.
    """
    s = ClaudeAccountSwitcher()
    s.platform = Platform.MACOS
    s._setup_directories()
    s._init_sequence_file()
    cfg = s._get_claude_config_path()
    cfg.write_text(json.dumps({"oauthAccount": {
        "emailAddress": "ax@example.com", "organizationUuid": "",
        "accountUuid": "u-ax"}}), encoding="utf-8")
    seq = s._get_sequence_data()
    seq["accounts"]["1"] = {
        "email": "ax@example.com", "uuid": "u-ax", "organizationUuid": ""}
    seq["sequence"] = [1]
    seq["activeAccountNumber"] = 1
    s._write_json(s.sequence_file, seq)

    old_backup = json.dumps({"claudeAiOauth": {
        "accessToken": "sk-old-backup", "refreshToken": "rt-OLD-lineage",
        "expiresAt": 99999999999000, "refreshTokenExpiresAt": 1_000}})
    s._write_account_credentials("1", "ax@example.com", old_backup)

    kc_new = json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat01-KC-N-PLUS-1", "refreshToken": "rt-NEW",
        "expiresAt": 99999999999000 + 3_600_000,
        "refreshTokenExpiresAt": 50_000}})
    file_n = json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat01-FILE-N", "refreshToken": "rt-NEW",
        "expiresAt": 99999999999000,
        "refreshTokenExpiresAt": 48_000}})  # same lineage, generation N

    cred_file = temp_home / ".claude" / ".credentials.json"
    cred_file.write_text(file_n, encoding="utf-8")

    monkeypatch.setattr(s, "_read_credentials", lambda: kc_new)

    def boom(*a, **kw):
        raise OSError("stash write failed")

    monkeypatch.setattr(s._store, "_write_unclaimed_credential", boom)

    with patch("claude_swap.oauth.fetch_oauth_profile",
               return_value={"uuid": "u-ax", "email": "ax@example.com",
                             "organizationUuid": ""}):
        s._resync_rotated_backup("1", "ax@example.com", "", kc_new)

    assert cred_file.read_text(encoding="utf-8") == kc_new, (
        "DEFECT: a same-lineage older generation was routed into the stash "
        "arm at all -- a failed stash then blocked a write-through that "
        "never needed to preserve anything"
    )


def test_sync_overwrites_a_different_login_left_by_a_failed_switch_time_refresh(
    temp_home: Path, mock_claude_config: Path, monkeypatch,
):
    """The fresher-file skip must fire only on a NEWER GENERATION of the SAME
    login, never on a DIFFERENT login the file happens to name with a later
    ``refreshTokenExpiresAt``. A file left on account B by a failed
    switch-time refresh must still be brought to account A once A is the one
    just adopted -- B's later stamp is not evidence that B belongs there.
    """
    s = _switcher(temp_home, mock_claude_config, "a@example.com")
    s.platform = Platform.MACOS
    monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", "")

    login_a = json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat01-A", "refreshToken": "rt-A",
        "expiresAt": 99999999999000, "refreshTokenExpiresAt": 10_000}})
    login_b = json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat01-B", "refreshToken": "rt-B",
        "expiresAt": 99999999999000,
        "refreshTokenExpiresAt": 10_000 + 50_000}})  # a DIFFERENT login

    cred_file = temp_home / ".claude" / ".credentials.json"
    cred_file.write_text(login_b, encoding="utf-8")

    with patch.object(s, "_read_capture_credentials", return_value=login_a), \
         patch("claude_swap.oauth.fetch_oauth_profile",
               return_value={"uuid": "u-a", "email": "a@example.com",
                             "organizationUuid": ""}):
        s.add_account(slot=3, assume_yes=True)

    assert cred_file.read_text(encoding="utf-8") == login_a, (
        "DEFECT: the plaintext file still held a different login (B) after "
        "login A was adopted into the slot"
    )

    entries = s._store._list_unclaimed_credentials()
    stashed = [
        s._store._read_unclaimed_credential(eid)[0] for eid in entries
    ]
    assert any("rt-B" in c for c in stashed), (
        "DEFECT: login B's only copy was overwritten with nothing stashed"
    )


def test_sync_skips_the_write_when_the_stash_fails(
    temp_home: Path, mock_claude_config: Path, monkeypatch,
):
    """A failed stash is not a licence to overwrite: :meth:`_write_unclaimed_credential`
    raising must leave the plaintext file exactly as it was, and ``add_account``
    must still return normally -- the credential the file alone held survives
    to be stashed on a later pass rather than being destroyed silently.
    """
    s = _switcher(temp_home, mock_claude_config, "a@example.com")
    s.platform = Platform.MACOS
    monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", "")

    kc_refresh = 88_888_888_888_000
    file_refresh = kc_refresh + 8_000  # beyond the 5s lineage jitter: a DIFFERENT login
    login_a = json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat01-A", "refreshToken": "rt-A",
        "expiresAt": 99999999999000, "refreshTokenExpiresAt": kc_refresh}})
    login_l = json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat01-L", "refreshToken": "rt-L-ONLY-COPY",
        "expiresAt": 99999999999000, "refreshTokenExpiresAt": file_refresh}})

    cred_file = temp_home / ".claude" / ".credentials.json"
    cred_file.write_text(login_l, encoding="utf-8")

    def boom(*a, **kw):
        raise OSError("stash write failed")

    monkeypatch.setattr(s._store, "_write_unclaimed_credential", boom)

    with patch.object(s, "_read_capture_credentials", return_value=login_a), \
         patch("claude_swap.oauth.fetch_oauth_profile",
               return_value={"uuid": "u-a", "email": "a@example.com",
                             "organizationUuid": ""}):
        s.add_account(slot=3, assume_yes=True)

    assert cred_file.read_text(encoding="utf-8") == login_l, (
        "DEFECT: failed stash still overwrote L"
    )


def test_sync_lets_the_real_store_guard_propagate_out_of_the_stash(
    temp_home: Path, mock_claude_config: Path, monkeypatch,
):
    """The suite's real-store guard (``tests.conftest.RealStoreWriteBlocked``,
    a plain ``Exception``, deliberately not an ``OSError``/``PermissionError``
    subclass) must never be contained by the stash's own error handling --
    a containment there could hide a write into the REAL store the same way
    it would hide any other failure. Every sibling ``_write_unclaimed_credential``
    call site (switcher.py) narrows its ``except`` for the same reason; this
    one must too.
    """
    from tests.conftest import RealStoreWriteBlocked

    s = _switcher(temp_home, mock_claude_config, "a@example.com")
    s.platform = Platform.MACOS
    monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", "")

    kc_refresh = 88_888_888_888_000
    file_refresh = kc_refresh + 8_000  # beyond the 5s lineage jitter: a DIFFERENT login
    login_a = json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat01-A", "refreshToken": "rt-A",
        "expiresAt": 99999999999000, "refreshTokenExpiresAt": kc_refresh}})
    login_l = json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat01-L", "refreshToken": "rt-L-ONLY-COPY",
        "expiresAt": 99999999999000, "refreshTokenExpiresAt": file_refresh}})

    cred_file = temp_home / ".claude" / ".credentials.json"
    cred_file.write_text(login_l, encoding="utf-8")

    def guard_trip(*a, **kw):
        raise RealStoreWriteBlocked("refused: the REAL store")

    monkeypatch.setattr(s._store, "_write_unclaimed_credential", guard_trip)

    with patch.object(s, "_read_capture_credentials", return_value=login_a), \
         patch("claude_swap.oauth.fetch_oauth_profile",
               return_value={"uuid": "u-a", "email": "a@example.com",
                             "organizationUuid": ""}), \
         pytest.raises(RealStoreWriteBlocked):
        s.add_account(slot=3, assume_yes=True)
