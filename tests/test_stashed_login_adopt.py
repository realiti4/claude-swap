"""A foreign login parked while its own slot was still healthy.

``_adopt_into_dead_slot`` decides at STASH time, and a live slot takes only a
credential dated later than its own; the rest is refused, because a live
slot's own refresh token must not be overwritten. Nothing looked at the stash
again, so when that slot's token died hours later the login sitting in the
stash was never reached and the slot asked for a re-login it did not need.

Measured on a real machine: the login was stashed as ``foreign`` five minutes
after the slot's last good fetch, and the ``invalid_grant`` strike landed most
of a day later -- all of it time in which the remedy was already on disk.
"""

import json
import time

import pytest

from claude_swap import oauth
from claude_swap.locking import FileLock
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.json_output import USAGE_RELOGIN_REQUIRED
from claude_swap.usage_store import AUTH_DEAD_STRIKES, FetchRecord as FR


def _creds(refresh: str) -> str:
    return json.dumps({
        "claudeAiOauth": {
            "accessToken": "sk-" + refresh,
            "refreshToken": refresh,
            "expiresAt": 9999999999000,
        }
    })


def _dated(refresh: str, expires_at_ms: int) -> str:
    blob = json.loads(_creds(refresh))
    blob["claudeAiOauth"]["refreshTokenExpiresAt"] = expires_at_ms
    return json.dumps(blob)


# OFFSETS FROM NOW. Epoch constants (1970, the year 5138) sit either side of
# every plausible bug, so a guard comparing ms against seconds still passes.
_DAY_MS = 86_400_000
_NOW_MS = int(time.time() * 1000)

DEAD = _creds("rt-dead")
FRESH = _creds("rt-fresh-login")           # no refreshTokenExpiresAt at all
EXPIRED = _dated("rt-expired", _NOW_MS - _DAY_MS)
LIVE_DATED = _dated("rt-live-dated", _NOW_MS + 30 * _DAY_MS)
NEARLY_SPENT = _dated("rt-nearly-spent", _NOW_MS + 60_000)


def _strike(sw, creds=DEAD, org=""):
    """Quarantine slot 2 against ``creds``' generation.

    ``org`` must match slot 2's OWN roster ``organizationUuid`` for the
    strike to be seen at all: ``_slot_token_dead`` builds its lookup key
    from the roster's org and ``UsageStore._matches`` compares it for
    EQUALITY, so a blank-org strike row silently misses a slot whose
    roster record carries a real org.
    """
    path = sw._usage_store.path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schemaVersion": 2,
        "accounts": {
            "2": {
                "email": "owner@example.com",
                "organizationUuid": org,
                "authDeadStrikes": AUTH_DEAD_STRIKES,
                "struckFingerprint": oauth.credential_fingerprint(creds),
                "consecutiveFailures": 2,
                "lastError": "invalid_grant",
                "lastGood": {"five_hour": {"pct": 10.0}},
            }
        },
    }))


class TestAdoptStashedLoginForSlot:
    """The stash gets a LATER adopter, at the moment the slot becomes dead."""

    @pytest.fixture
    def switcher(self, temp_home, mock_claude_config, sample_sequence_data):
        sw = ClaudeAccountSwitcher()
        sw._setup_directories()
        sample_sequence_data["accounts"]["2"]["email"] = "owner@example.com"
        sample_sequence_data["accounts"]["2"]["uuid"] = "uuid-owner"
        sw._write_json(sw.sequence_file, sample_sequence_data)
        sw._write_account_credentials("2", "owner@example.com", DEAD)
        return sw


    def _stash(self, sw, creds=FRESH, email="owner@example.com",
               uuid="uuid-owner"):
        return sw._store._write_unclaimed_credential(creds, {
            "reason": "foreign",
            "configSlot": "1",
            "fingerprint": oauth.credential_fingerprint(creds),
            "resolvedIdentity": {"uuid": uuid, "email": email,
                                 "organizationUuid": None},
        })

    def test_a_dead_slot_adopts_the_login_stashed_for_it(self, switcher):
        """The whole point: the slot holds the stashed login afterwards."""
        entry_id = self._stash(switcher)
        _strike(switcher)

        assert switcher._adopt_stashed_login_for_slot(
            "2", "owner@example.com") is True

        stored, unreadable = switcher._read_account_credentials_ex(
            "2", "owner@example.com")
        assert not unreadable
        assert oauth.credential_fingerprint(stored) == \
            oauth.credential_fingerprint(FRESH)
        assert entry_id not in switcher._store._list_unclaimed_credentials()

    def test_the_adopt_lifts_the_quarantine_it_wrote_over(self, switcher):
        """A strike left standing keeps the healed slot out of every pass."""
        self._stash(switcher)
        _strike(switcher)

        switcher._adopt_stashed_login_for_slot("2", "owner@example.com")

        ident = {"2": ("owner@example.com", "")}
        entry = switcher._usage_store.entries(ident)["2"]
        assert entry.token_dead() is False

    def test_a_live_slot_keeps_its_own_credential(self, switcher):
        """No strike: the stored refresh token is the newer one to protect."""
        entry_id = self._stash(switcher)

        assert switcher._adopt_stashed_login_for_slot(
            "2", "owner@example.com") is False

        stored, _ = switcher._read_account_credentials_ex(
            "2", "owner@example.com")
        assert oauth.credential_fingerprint(stored) == \
            oauth.credential_fingerprint(DEAD)
        assert entry_id in switcher._store._list_unclaimed_credentials()

    def test_the_struck_generation_is_not_adopted_back_in(self, switcher):
        """A stash of the very bytes the endpoint condemned heals nothing, and
        adopting it would clear the strike that describes it."""
        entry_id = self._stash(switcher, creds=DEAD)
        _strike(switcher)

        assert switcher._adopt_stashed_login_for_slot(
            "2", "owner@example.com") is False
        assert entry_id in switcher._store._list_unclaimed_credentials()

    def test_bytes_the_slot_already_holds_are_not_written_back(self, switcher):
        """`_adopt_login_into_slot` refuses this for its own reason — rewriting
        the identical credential only shifts `.prev`. Here it is worse: an
        UNBOUND strike (a row written before fingerprints were recorded) binds
        unconditionally, so the comparison against the struck generation
        cannot exclude these bytes, and adopting would clear a verdict that is
        accurate about what the slot still holds."""
        entry_id = self._stash(switcher, creds=DEAD)
        path = switcher._usage_store.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "schemaVersion": 2,
            "accounts": {
                "2": {
                    "email": "owner@example.com",
                    "organizationUuid": "",
                    "authDeadStrikes": AUTH_DEAD_STRIKES,   # no struckFingerprint
                    "consecutiveFailures": 2,
                    "lastError": "invalid_grant",
                    "lastGood": {"five_hour": {"pct": 10.0}},
                }
            },
        }))

        assert switcher._adopt_stashed_login_for_slot(
            "2", "owner@example.com") is False
        assert entry_id in switcher._store._list_unclaimed_credentials(), (
            "a stash entry was spent on bytes the slot already held")
        ident = {"2": ("owner@example.com", "")}
        assert switcher._usage_store.entries(ident)["2"].token_dead() is True, (
            "an accurate strike was cleared by rewriting the same credential")

    def test_a_slot_whose_own_credential_cannot_be_READ_is_left_alone(
            self, switcher, monkeypatch):
        """An unreadable stored credential is not an empty slot: on a Mac a
        locked Keychain reads as a failure, and adopting on that answer would
        overwrite a credential nobody could see.

        `_slot_token_dead` is what refuses — it returns False on an unreadable
        read for both the idle and the active slot, so the adopt stops at its
        pre-check. This pins the BEHAVIOUR, not that check: a later refactor
        that moves the refusal must keep the answer."""
        entry_id = self._stash(switcher)
        _strike(switcher)
        monkeypatch.setattr(switcher, "_read_account_credentials_ex",
                            lambda num, email: ("", True))

        assert switcher._adopt_stashed_login_for_slot(
            "2", "owner@example.com") is False
        assert entry_id in switcher._store._list_unclaimed_credentials()

    def test_a_write_that_landed_is_an_adopt_even_if_the_strike_cannot_lift(
            self, switcher, monkeypatch):
        """The write ADVANCES the slot. Reporting "could not adopt" after it
        lands makes the caller announce a re-login for a slot that now holds a
        working credential, and leaves a stash row pointing at bytes the slot
        already has — which the identical-bytes guard then refuses forever.
        The sibling adopt gets this right: it logs the unlifted quarantine and
        carries on."""
        entry_id = self._stash(switcher)
        _strike(switcher)

        def boom(*a, **k):
            raise OSError("usage store is unwritable")

        monkeypatch.setattr(switcher._usage_store, "clear_dead_token", boom)

        assert switcher._adopt_stashed_login_for_slot(
            "2", "owner@example.com") is True
        stored, _ = switcher._read_account_credentials_ex(
            "2", "owner@example.com")
        assert oauth.credential_fingerprint(stored) == \
            oauth.credential_fingerprint(FRESH), "the write did not land"
        assert entry_id not in switcher._store._list_unclaimed_credentials(), (
            "the adopted entry stayed in the stash")

    def test_a_write_that_FAILED_is_not_an_adopt(self, switcher, monkeypatch):
        """THE CONTROL. Without it the assertion above passes for a build that
        calls every failure an adopt."""
        entry_id = self._stash(switcher)
        _strike(switcher)

        def boom(*a, **k):
            raise OSError("backup is unwritable")

        monkeypatch.setattr(switcher, "_write_account_credentials", boom)

        assert switcher._adopt_stashed_login_for_slot(
            "2", "owner@example.com") is False
        assert entry_id in switcher._store._list_unclaimed_credentials()

    def test_an_EXPIRED_stash_is_not_adopted(self, switcher):
        """A refresh token already past its own expiry mints nothing, so
        adopting it writes a dead credential into a dead slot AND lifts the
        quarantine that was accurate about the slot."""
        entry_id = self._stash(switcher, creds=EXPIRED)
        _strike(switcher)

        assert switcher._adopt_stashed_login_for_slot(
            "2", "owner@example.com") is False

        stored, _ = switcher._read_account_credentials_ex(
            "2", "owner@example.com")
        assert oauth.credential_fingerprint(stored) == \
            oauth.credential_fingerprint(DEAD), "a spent grant was written in"
        ident = {"2": ("owner@example.com", "")}
        assert switcher._usage_store.entries(ident)["2"].token_dead() is True, (
            "a credential that mints nothing lifted an accurate quarantine")
        assert entry_id in switcher._store._list_unclaimed_credentials()

    def test_an_UNREADABLE_stash_entry_is_not_adopted(
            self, switcher, monkeypatch):
        """Unreadable WITH BYTES. `("", True)` is caught by `not creds`, so
        the flag itself never runs and deleting it leaves such a test green;
        this returns a payload that would otherwise adopt, so only the flag
        can refuse it."""
        entry_id = self._stash(switcher, creds=FRESH)
        _strike(switcher)
        monkeypatch.setattr(switcher._store, "_read_unclaimed_credential",
                            lambda *a, **k: (FRESH, True))

        assert switcher._adopt_stashed_login_for_slot(
            "2", "owner@example.com") is False
        assert entry_id in switcher._store._list_unclaimed_credentials()
        stored, _ = switcher._read_account_credentials_ex(
            "2", "owner@example.com")
        assert oauth.credential_fingerprint(stored) == \
            oauth.credential_fingerprint(DEAD)

    def test_a_refresh_token_inside_the_expiry_buffer_is_refused(
            self, switcher):
        """`_sweep_unclaimed_stash` asks `oauth.is_oauth_token_expired`, which
        subtracts a 5-minute buffer, so it would DROP this row. Adopting it
        lifts the quarantine for bytes the sweep is about to delete and the
        slot re-strikes next pass. Both sides ask the same predicate."""
        entry_id = self._stash(switcher, creds=NEARLY_SPENT)
        _strike(switcher)

        assert switcher._adopt_stashed_login_for_slot(
            "2", "owner@example.com") is False
        assert entry_id in switcher._store._list_unclaimed_credentials()

    def test_CONTROL_an_unexpired_refreshTokenExpiresAt_is_adopted(
            self, switcher):
        """Without this the refusal above passes for a build that refuses
        every entry carrying the field. The no-field case is what `FRESH` is,
        so every other test in this class already covers it."""
        entry_id = self._stash(switcher, creds=LIVE_DATED)
        _strike(switcher)

        assert switcher._adopt_stashed_login_for_slot(
            "2", "owner@example.com") is True
        assert entry_id not in switcher._store._list_unclaimed_credentials()

    def test_another_account_s_login_is_left_alone(self, switcher):
        """Identity is what authorizes the write; a dead slot is not a
        licence to absorb whatever is in the stash."""
        entry_id = self._stash(switcher, email="someone@else.example",
                               uuid="uuid-else")
        _strike(switcher)

        assert switcher._adopt_stashed_login_for_slot(
            "2", "owner@example.com") is False

        stored, _ = switcher._read_account_credentials_ex(
            "2", "owner@example.com")
        assert oauth.credential_fingerprint(stored) == \
            oauth.credential_fingerprint(DEAD)
        assert entry_id in switcher._store._list_unclaimed_credentials()

    def _sibling_org_setup(self, sample_sequence_data, stash_org):
        """Two slots share ONE account uuid under two different orgs — the
        same person's account, in two organizations (queue row #474). Slot
        1 owns org-A, slot 2 (dead, the adopt target) owns org-B; the
        stashed login resolves to `stash_org`."""
        sample_sequence_data["accounts"]["1"]["uuid"] = "uuid-owner"
        sample_sequence_data["accounts"]["1"]["organizationUuid"] = "org-A"
        sample_sequence_data["accounts"]["2"]["email"] = "owner@example.com"
        sample_sequence_data["accounts"]["2"]["uuid"] = "uuid-owner"
        sample_sequence_data["accounts"]["2"]["organizationUuid"] = "org-B"
        sw = ClaudeAccountSwitcher()
        sw._setup_directories()
        sw._write_json(sw.sequence_file, sample_sequence_data)
        sw._write_account_credentials("2", "owner@example.com", DEAD)
        entry_id = sw._store._write_unclaimed_credential(FRESH, {
            "reason": "foreign",
            "configSlot": "1",
            "fingerprint": oauth.credential_fingerprint(FRESH),
            "resolvedIdentity": {"uuid": "uuid-owner",
                                 "email": "owner@example.com",
                                 "organizationUuid": stash_org},
        })
        # org="org-B": slot 2's roster record carries a real org, and
        # `_slot_token_dead` matches it for EQUALITY (`UsageStore._matches`)
        # — the default blank-org strike would silently miss it.
        _strike(sw, org="org-B")
        return sw, entry_id

    def test_a_sibling_org_s_login_under_a_shared_uuid_is_left_alone(
        self, temp_home, mock_claude_config, sample_sequence_data
    ):
        """ONE UUID CAN NAME TWO SLOTS: the same account uuid, in two
        different orgs, is two roster records (`_slot_owning_resolved_
        identity`'s own docstring). Matching on uuid alone — the naive
        check before this fix — adopted the OTHER org's login into this
        slot, planting the same refresh token behind two slots at once and
        burning it twice as fast (issue #400, queue row #474)."""
        sw, entry_id = self._sibling_org_setup(sample_sequence_data,
                                               stash_org="org-A")

        assert sw._adopt_stashed_login_for_slot(
            "2", "owner@example.com") is False

        stored, _ = sw._read_account_credentials_ex("2", "owner@example.com")
        assert oauth.credential_fingerprint(stored) == \
            oauth.credential_fingerprint(DEAD)
        assert entry_id in sw._store._list_unclaimed_credentials()

    def test_this_slot_s_own_org_under_a_shared_uuid_still_adopts(
        self, temp_home, mock_claude_config, sample_sequence_data
    ):
        """CONTROL for the refusal above: the same shared-uuid roster, but
        the stashed login's org matches THIS slot's own (org-B) — the org
        check must not turn into a blanket refusal whenever a uuid is
        shared, only when the login belongs to the sibling."""
        sw, entry_id = self._sibling_org_setup(sample_sequence_data,
                                               stash_org="org-B")

        assert sw._adopt_stashed_login_for_slot(
            "2", "owner@example.com") is True

        stored, _ = sw._read_account_credentials_ex("2", "owner@example.com")
        assert oauth.credential_fingerprint(stored) == \
            oauth.credential_fingerprint(FRESH)
        assert entry_id not in sw._store._list_unclaimed_credentials()

    def test_a_blank_org_login_under_a_shared_uuid_is_left_alone(
        self, temp_home, mock_claude_config, sample_sequence_data
    ):
        """The stash entry predates org capture (blank org) but the uuid is
        shared by two slots with distinct, non-blank orgs: neither slot
        corroborates it, so `_slot_owning_resolved_identity`'s blank-org
        tolerance matches BOTH and the whole-roster resolver reports
        ambiguous (None) — same refusal as an outright org mismatch, not a
        different code path from the two tests above."""
        sw, entry_id = self._sibling_org_setup(sample_sequence_data,
                                               stash_org=None)

        assert sw._adopt_stashed_login_for_slot(
            "2", "owner@example.com") is False

        stored, _ = sw._read_account_credentials_ex("2", "owner@example.com")
        assert oauth.credential_fingerprint(stored) == \
            oauth.credential_fingerprint(DEAD)
        assert entry_id in sw._store._list_unclaimed_credentials()

    def _uuidless_slot_setup(self, sample_sequence_data, resolved_uuid):
        """This slot (2) has NO uuid recorded at all — an add-token
        placeholder — so the uuid branch above never runs and the match
        falls to the email-only `elif`. Slot 1 shares the same email under
        its own uuid+org (opus review, round 400 pass 2: the email-only arm
        had no equivalent org check at all)."""
        sample_sequence_data["accounts"]["1"]["email"] = "owner@example.com"
        sample_sequence_data["accounts"]["1"]["uuid"] = "uuid-owner"
        sample_sequence_data["accounts"]["1"]["organizationUuid"] = "org-A"
        sample_sequence_data["accounts"]["2"]["email"] = "owner@example.com"
        sample_sequence_data["accounts"]["2"].pop("uuid", None)
        sample_sequence_data["accounts"]["2"]["organizationUuid"] = "org-B"
        sw = ClaudeAccountSwitcher()
        sw._setup_directories()
        sw._write_json(sw.sequence_file, sample_sequence_data)
        sw._write_account_credentials("2", "owner@example.com", DEAD)
        entry_id = sw._store._write_unclaimed_credential(FRESH, {
            "reason": "foreign",
            "configSlot": "1",
            "fingerprint": oauth.credential_fingerprint(FRESH),
            "resolvedIdentity": {"uuid": resolved_uuid,
                                 "email": "owner@example.com",
                                 "organizationUuid": "org-A" if resolved_uuid
                                 else None},
        })
        _strike(sw, org="org-B")
        return sw, entry_id

    def test_a_sibling_s_uuid_owned_login_is_not_adopted_on_email_alone(
        self, temp_home, mock_claude_config, sample_sequence_data
    ):
        """This slot has no uuid on file, so the uuid branch is skipped —
        but the STASH entry names a uuid the roster resolves unambiguously
        to slot 1. Matching this slot on the shared email alone would plant
        slot 1's refresh token into slot 2 too; the roster positively says
        the login is slot 1's, so it must be refused here even though this
        slot's own record carries no uuid to compare against."""
        sw, entry_id = self._uuidless_slot_setup(
            sample_sequence_data, resolved_uuid="uuid-owner")

        assert sw._adopt_stashed_login_for_slot(
            "2", "owner@example.com") is False

        stored, _ = sw._read_account_credentials_ex("2", "owner@example.com")
        assert oauth.credential_fingerprint(stored) == \
            oauth.credential_fingerprint(DEAD)
        assert entry_id in sw._store._list_unclaimed_credentials()

    def test_a_truly_uuid_less_login_still_heals_on_address(
        self, temp_home, mock_claude_config, sample_sequence_data
    ):
        """CONTROL: the stash entry carries no uuid AT ALL (not even a
        sibling's) — the roster resolver can name nobody, so heal-on-address
        must still work. The org check on the email-only arm must refuse
        only a POSITIVE claim on a different slot, never turn into a
        blanket refusal for the uuid-less case this arm exists for."""
        sw, entry_id = self._uuidless_slot_setup(
            sample_sequence_data, resolved_uuid="")

        assert sw._adopt_stashed_login_for_slot(
            "2", "owner@example.com") is True

        stored, _ = sw._read_account_credentials_ex("2", "owner@example.com")
        assert oauth.credential_fingerprint(stored) == \
            oauth.credential_fingerprint(FRESH)
        assert entry_id not in sw._store._list_unclaimed_credentials()


class TestTheAdoptIsASlotMutation:
    """Every other path that writes a slot credential holds the slot lock, and
    `_adopt_login_into_slot` says why in its own words: identity, verdict and
    write must be one transaction, or a switch persisting a rotated refresh
    token in the gap is overwritten by a guard that had already passed.

    This adopt is reached from `_collect_usage_entries`, a READ path that holds
    no lock — so it has to take one itself.
    """

    @pytest.fixture
    def switcher(self, temp_home, mock_claude_config, sample_sequence_data):
        sw = ClaudeAccountSwitcher()
        sw._setup_directories()
        sample_sequence_data["accounts"]["2"]["email"] = "owner@example.com"
        sample_sequence_data["accounts"]["2"]["uuid"] = "uuid-owner"
        sw._write_json(sw.sequence_file, sample_sequence_data)
        sw._write_account_credentials("2", "owner@example.com", DEAD)
        sw._usage_store.record(
            {"2": FR(error="invalid_grant",
                     struck_fp=oauth.credential_fingerprint(DEAD))},
            {"2": ("owner@example.com", "")},
        )
        sw._store._write_unclaimed_credential(FRESH, {
            "reason": "foreign",
            "configSlot": "1",
            "fingerprint": oauth.credential_fingerprint(FRESH),
            "resolvedIdentity": {"uuid": "uuid-owner",
                                 "email": "owner@example.com",
                                 "organizationUuid": None},
        })
        return sw

    def test_it_does_not_write_while_another_holder_has_the_slot_lock(
            self, switcher):
        """A held lock means a slot mutation is in flight. The adopt must
        stand down for this pass, not write beside it, and not raise into a
        display refresh.

        AND IT MUST STAND DOWN QUICKLY. `_collect_usage_entries` runs on every
        list, status and TUI refresh and calls this once per dead slot, so the
        lock's default 10s wait would freeze the display for 10s PER SLOT
        against any concurrent switch. Measured on a held lock: the default
        acquire returns False after 10.01s."""
        held = FileLock(switcher.lock_file)
        assert held.acquire(timeout=5), "premise: the test could take the lock"
        try:
            start = time.monotonic()
            assert switcher._adopt_stashed_login_for_slot(
                "2", "owner@example.com") is False
            waited = time.monotonic() - start
            assert waited < 2.0, (
                "a display refresh waited %.1fs on a held lock" % waited)
            stored, _ = switcher._read_account_credentials_ex(
                "2", "owner@example.com")
            assert oauth.credential_fingerprint(stored) == \
                oauth.credential_fingerprint(DEAD), "wrote past a held lock"
        finally:
            held.release()

    def test_it_adopts_once_the_lock_is_free(self, switcher):
        """THE CONTROL. Without it the assertion above passes for a build whose
        adopt never writes at all."""
        assert switcher._adopt_stashed_login_for_slot(
            "2", "owner@example.com") is True
        stored, _ = switcher._read_account_credentials_ex(
            "2", "owner@example.com")
        assert oauth.credential_fingerprint(stored) == \
            oauth.credential_fingerprint(FRESH)


    def test_a_slot_that_heals_while_the_lock_is_waited_out_is_left_alone(
            self, switcher, monkeypatch):
        """The pre-check is lock-free, so its answer can be stale by the time
        the lock is granted. A slot that healed in the gap must not be written
        over: the credential it now holds is newer than anything in the stash.
        """
        calls = []

        def dead(num, email):
            calls.append(num)
            return len(calls) == 1        # true for the pre-check, false under the lock

        monkeypatch.setattr(switcher, "_slot_token_dead", dead)

        assert switcher._adopt_stashed_login_for_slot(
            "2", "owner@example.com") is False
        assert len(calls) == 2, (
            "the verdict was not re-derived under the lock", calls)
        stored, _ = switcher._read_account_credentials_ex(
            "2", "owner@example.com")
        assert oauth.credential_fingerprint(stored) == \
            oauth.credential_fingerprint(DEAD)

    def test_a_slot_the_roster_moved_under_is_left_alone(
            self, switcher, monkeypatch):
        """`remove_account` holds no lock and the swap/move paths hold this
        one, so the roster can change while the lock is waited out. A stale
        (slot, address) pair would write a live credential into a slot that is
        now somebody else's."""
        real = switcher._get_sequence_data
        seen = {"n": 0}

        def moved():
            seen["n"] += 1
            data = real()
            if seen["n"] > 1:             # the read under the lock
                data["accounts"]["2"]["email"] = "someone@else.example"
            return data

        monkeypatch.setattr(switcher, "_get_sequence_data", moved)

        assert switcher._adopt_stashed_login_for_slot(
            "2", "owner@example.com") is False
        stored, _ = switcher._read_account_credentials_ex(
            "2", "owner@example.com")
        assert oauth.credential_fingerprint(stored) == \
            oauth.credential_fingerprint(DEAD)



class TestTheCollectPassReachesTheStash:
    """A method nobody calls heals nothing. The pass that decides to SAY
    "re-login needed" is the one that must look in the stash first."""

    def _switcher(self, sample_sequence_data):
        sw = ClaudeAccountSwitcher()
        sw._setup_directories()
        sample_sequence_data["accounts"]["2"]["email"] = "owner@example.com"
        sample_sequence_data["accounts"]["2"]["uuid"] = "uuid-owner"
        sw._write_json(sw.sequence_file, sample_sequence_data)
        sw._write_account_credentials("2", "owner@example.com", DEAD)
        sw._usage_store.record(
            {"2": FR(error="invalid_grant",
                     struck_fp=oauth.credential_fingerprint(DEAD))},
            {"2": ("owner@example.com", "")},
        )
        return sw

    def test_a_stashed_login_is_adopted_instead_of_asking_for_a_re_login(
        self, temp_home, mock_claude_config, sample_sequence_data
    ):
        sw = self._switcher(sample_sequence_data)
        sw._store._write_unclaimed_credential(FRESH, {
            "reason": "foreign",
            "configSlot": "1",
            "fingerprint": oauth.credential_fingerprint(FRESH),
            "resolvedIdentity": {"uuid": "uuid-owner",
                                 "email": "owner@example.com",
                                 "organizationUuid": None},
        })

        entries = sw._collect_usage_entries(sw._build_accounts_info(),
                                            fetch=set())

        assert entries["2"].sentinel != USAGE_RELOGIN_REQUIRED
        stored, _ = sw._read_account_credentials_ex("2", "owner@example.com")
        assert oauth.credential_fingerprint(stored) == \
            oauth.credential_fingerprint(FRESH)

    def test_the_adopt_runs_before_the_sweep_that_follows_it(
        self, temp_home, mock_claude_config, sample_sequence_data, monkeypatch
    ):
        """Consumer: `_adopt_stashed_login_for_slot`, ordered against arm A
        of the same pass. A dead slot's stashed login is adopted (and its
        stash row removed) inside the per-slot loop, BEFORE
        `_sweep_unclaimed_stash` runs once at the end — so the row is
        already gone by the time the sweep looks, and nothing is left for
        arm A to have to drop."""
        sw = self._switcher(sample_sequence_data)
        entry_id = sw._store._write_unclaimed_credential(FRESH, {
            "reason": "foreign",
            "configSlot": "1",
            "fingerprint": oauth.credential_fingerprint(FRESH),
            "resolvedIdentity": {"uuid": "uuid-owner",
                                 "email": "owner@example.com",
                                 "organizationUuid": None},
        })
        seen_at_sweep_time = {}
        real_sweep = sw._sweep_unclaimed_stash

        def _spy(*args, **kwargs):
            seen_at_sweep_time["ids"] = set(sw.list_unclaimed_credentials())
            return real_sweep(*args, **kwargs)

        monkeypatch.setattr(sw, "_sweep_unclaimed_stash", _spy)

        sw._collect_usage_entries(sw._build_accounts_info(), fetch=set())

        assert "ids" in seen_at_sweep_time, "the sweep must still run"
        assert entry_id not in seen_at_sweep_time["ids"], (
            "the adopt must remove the row before the sweep is even called"
        )

    def test_with_nothing_stashed_the_slot_still_asks(
        self, temp_home, mock_claude_config, sample_sequence_data
    ):
        """THE CONTROL. Without it the assertion above passes for a pass that
        never reached the dead branch at all."""
        sw = self._switcher(sample_sequence_data)

        entries = sw._collect_usage_entries(sw._build_accounts_info(),
                                            fetch=set())

        assert entries["2"].sentinel == USAGE_RELOGIN_REQUIRED


class TestTheWriteSideTwinRefusesTheSameBytes:
    """`_adopt_into_dead_slot` gets the SAME string `_stash_live_credential`
    parked one line earlier, and decides alone: that stash sweeps its own
    expired row, so no reader ever reaches those bytes."""

    @pytest.fixture
    def switcher(self, temp_home, mock_claude_config, sample_sequence_data):
        sw = ClaudeAccountSwitcher()
        sw._setup_directories()
        sample_sequence_data["accounts"]["2"]["email"] = "owner@example.com"
        sw._write_json(sw.sequence_file, sample_sequence_data)
        sw._write_account_credentials("2", "owner@example.com", DEAD)
        _strike(sw)
        return sw

    def _dead(self, sw):
        ident = {"2": ("owner@example.com", "")}
        return sw._usage_store.entries(ident)["2"].token_dead()

    def test_an_EXPIRED_foreign_credential_does_not_heal_a_dead_slot(
            self, switcher):
        """Spent bytes written into the slot AND the accurate quarantine
        lifted — the exact pair the reader-side guard forbids."""
        assert switcher._adopt_into_dead_slot(
            "2", EXPIRED, switcher._get_sequence_data() or {}) is False

        stored, _ = switcher._read_account_credentials_ex(
            "2", "owner@example.com")
        assert oauth.credential_fingerprint(stored) == \
            oauth.credential_fingerprint(DEAD), "a spent grant was written in"
        assert self._dead(switcher) is True, (
            "a credential that mints nothing lifted an accurate quarantine")

    def test_CONTROL_an_unexpired_foreign_credential_still_heals(
            self, switcher):
        """Without this the refusal passes for a build that heals nothing.
        This is issue #136's whole point: a dead slot has no freshness left to
        protect, so resolved bytes that can still mint are strictly better."""
        assert switcher._adopt_into_dead_slot(
            "2", LIVE_DATED, switcher._get_sequence_data() or {}) is True

        stored, _ = switcher._read_account_credentials_ex(
            "2", "owner@example.com")
        assert oauth.credential_fingerprint(stored) == \
            oauth.credential_fingerprint(LIVE_DATED)
        assert self._dead(switcher) is False, "the quarantine was not lifted"


class TestALaterLoginDoesNotWaitForTheSlotToDie:
    """A login is never older than what the slot already holds.

    `refreshTokenExpiresAt` moves ONLY on a login, so a credential dated later
    than the slot's is proof of a later login. Waiting for the slot to die
    before accepting it is what lets that login rot until it is dead too.
    """

    @pytest.fixture
    def switcher(self, temp_home, mock_claude_config, sample_sequence_data):
        sw = ClaudeAccountSwitcher()
        sw._setup_directories()
        sample_sequence_data["accounts"]["2"]["email"] = "owner@example.com"
        sw._write_json(sw.sequence_file, sample_sequence_data)
        return sw

    def _stored(self, sw):
        stored, _ = sw._read_account_credentials_ex("2", "owner@example.com")
        return oauth.credential_fingerprint(stored)

    def test_CONTROL_an_undated_credential_never_displaces_a_live_slot(
            self, switcher):
        """No date is no evidence. `FRESH` carries no
        `refreshTokenExpiresAt`, so nothing proves it is the later login and
        the healthy slot keeps what it has."""
        switcher._write_account_credentials("2", "owner@example.com",
                                            LIVE_DATED)
        assert switcher._adopt_into_dead_slot(
            "2", FRESH, switcher._get_sequence_data() or {}) is False
        assert self._stored(switcher) == \
            oauth.credential_fingerprint(LIVE_DATED)

    def test_CONTROL_an_older_but_UNEXPIRED_credential_is_still_refused(
            self, switcher):
        """THE CONTROL THAT ACTUALLY TESTS THE COMPARISON. The first draft
        used `EXPIRED` here, which the spent guard already refuses -- so it
        passed a build with the recency check deleted. Both sides must be
        live-dated for the comparison to be the only thing deciding."""
        older = _dated("rt-older-live", _NOW_MS + 10 * _DAY_MS)
        newer = _dated("rt-newer-live", _NOW_MS + 40 * _DAY_MS)
        switcher._write_account_credentials("2", "owner@example.com", newer)
        assert switcher._adopt_into_dead_slot(
            "2", older, switcher._get_sequence_data() or {}) is False
        assert self._stored(switcher) == oauth.credential_fingerprint(newer)

    def test_CONTROL_the_SAME_login_does_not_overwrite_its_own_slot(
            self, switcher):
        """`<=`, not `<`. One login mints one `refreshTokenExpiresAt`, so
        equal dates are the same login and prove nothing about which of its
        two credentials is the newer rotation."""
        same_login = _NOW_MS + 20 * _DAY_MS
        held = _dated("rt-held", same_login)
        other = _dated("rt-other-rotation", same_login)
        switcher._write_account_credentials("2", "owner@example.com", held)
        assert switcher._adopt_into_dead_slot(
            "2", other, switcher._get_sequence_data() or {}) is False
        assert self._stored(switcher) == oauth.credential_fingerprint(held)

    def test_CONTROL_a_SPENT_incoming_credential_is_refused_by_a_live_slot(
            self, switcher):
        """The spent guard's LIVE-door half, which nothing else reaches.

        A refresh token can be spent with no strike against the slot, so a
        healthy slot can be offered later-dated bytes that mint nothing."""
        stored = _dated("rt-stored-spent", _NOW_MS - 2 * _DAY_MS)
        incoming = _dated("rt-incoming-spent", _NOW_MS - _DAY_MS)
        switcher._write_account_credentials("2", "owner@example.com", stored)
        assert not switcher._slot_token_dead("2", "owner@example.com"), (
            "premise: no strike -- spent is not the same as quarantined"
        )
        assert switcher._adopt_into_dead_slot(
            "2", incoming, switcher._get_sequence_data() or {}) is False
        assert self._stored(switcher) == oauth.credential_fingerprint(stored)

    def test_CONTROL_an_empty_slot_email_is_never_written_to(self, switcher):
        """No address, no slot to write into. `_slot_token_dead` answers True
        for a struck row whose identity is the empty pair, so the door would
        otherwise write a live credential into a backup keyed on no email."""
        data = switcher._get_sequence_data() or {}
        data["accounts"]["2"]["email"] = ""
        switcher._write_json(switcher.sequence_file, data)
        path = switcher._usage_store.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "schemaVersion": 2,
            "accounts": {"2": {
                "email": "", "organizationUuid": "",
                "authDeadStrikes": AUTH_DEAD_STRIKES,
                "struckFingerprint": None,
            }},
        }))
        assert switcher._slot_token_dead("2", "") is True, (
            "premise: the struck empty-identity row does read as dead, so "
            "only the email guard stands between it and a write"
        )
        assert switcher._adopt_into_dead_slot(
            "2", LIVE_DATED, switcher._get_sequence_data() or {}) is False
        stored, _ = switcher._read_account_credentials_ex("2", "")
        assert stored == "", "a credential was written to an empty-email slot"

    def test_CONTROL_an_unreadable_slot_is_never_overwritten(
            self, switcher, monkeypatch):
        """A read that FAILED is not a read that found older bytes. The
        keychain on a Mac loses individual reads to contention -- measured, 12
        failures inside 300ms with successful fetches one second later. The
        plain reader answers "" for that too, so the refusal holds either way;
        what this pins is that a failed read stays a refusal rather than
        becoming evidence of an older slot. Staged through `_ex` because that
        is the seam the door reads."""
        switcher._write_account_credentials("2", "owner@example.com",
                                            _dated("rt-held", _NOW_MS + _DAY_MS))
        monkeypatch.setattr(
            switcher, "_read_account_credentials_ex",
            lambda *a, **k: ("", True))
        assert switcher._adopt_into_dead_slot(
            "2", LIVE_DATED, switcher._get_sequence_data() or {}) is False

    def test_a_LATER_login_lands_in_its_slot_while_that_slot_is_healthy(
            self, switcher):
        """THE CASE. The slot is alive and its credential is older; the
        incoming one is dated later, which only a login can do. It must land
        now rather than wait in the stash for the slot to die."""
        switcher._write_account_credentials(
            "2", "owner@example.com",
            _dated("rt-older-live", _NOW_MS + 10 * _DAY_MS))
        assert not switcher._slot_token_dead("2", "owner@example.com"), (
            "premise: the slot is HEALTHY, so this is the later-login door "
            "and not the quarantine one"
        )
        assert switcher._adopt_into_dead_slot(
            "2", LIVE_DATED, switcher._get_sequence_data() or {}) is True
        assert self._stored(switcher) == \
            oauth.credential_fingerprint(LIVE_DATED)


class TestTheResyncAdoptDeliberatelyHasNoSpentGuard:
    """The fifth writer into a dead slot, and the one that must NOT refuse a
    spent refresh token.

    IT RESTS ON ONE LEG. Its caller reaches it only past `outcome.usage is
    not None`, so the usage endpoint served from these exact bytes on this
    pass; refusing would forfeit that access token's remaining life to avoid a
    quarantine that re-forms by itself. That leg is pinned below, separately
    from this conclusion, because widening the gate would quietly turn the
    exception into the defect the four guarded paths were fixed for.

    NOT the `_refresh_expiry` recency compare, which was given as the reason
    here and is false. It orders two LOGINS: it short-circuits whenever the
    stored side is undated, and with both sides dated and the STORED one more
    spent it still adopts. Measured both ways -- it never asks whether either
    credential can mint, so it cannot be doing the spent guard's work.

    What `_adopt_into_dead_slot` has that this does not is a decision already
    taken in the same call: `_stash_live_credential` parks those bytes and its
    sweep deletes the row when they are spent, so adopting them there would
    write what that very call discarded. Nothing here discards anything.
    """

    @pytest.fixture
    def switcher(self, temp_home, mock_claude_config, sample_sequence_data):
        sw = ClaudeAccountSwitcher()
        sw._setup_directories()
        sample_sequence_data["accounts"]["2"]["email"] = "owner@example.com"
        sample_sequence_data["accounts"]["2"]["uuid"] = "uuid-owner"
        sw._write_json(sw.sequence_file, sample_sequence_data)
        sw._write_account_credentials("2", "owner@example.com", DEAD)
        _strike(sw)
        return sw

    def test_a_spent_refresh_token_with_a_live_access_token_is_adopted(
            self, switcher):
        """`EXPIRED` is exactly that shape: `expiresAt` far future,
        `refreshTokenExpiresAt` a day past."""
        assert switcher._adopt_login_into_slot(
            "1", EXPIRED,
            {"uuid": "uuid-owner", "email": "owner@example.com",
             "organizationUuid": ""},
        ) is True

        stored, _ = switcher._read_account_credentials_ex(
            "2", "owner@example.com")
        assert oauth.credential_fingerprint(stored) == \
            oauth.credential_fingerprint(EXPIRED), (
                "the resync adopt grew a spent-token guard. Its caller has "
                "already served usage from these bytes, so this forfeits a "
                "working access token; see the class docstring for why the "
                "recency compare is NOT a reason either way")

    def _resync_reached(self, sw, monkeypatch, usage):
        """Drive `_fetch_active_usage`'s fast path; report whether the resync
        (and so the adopt below it) was reached."""
        reached = []
        monkeypatch.setattr(
            oauth, "try_fetch_usage_for_account",
            lambda *a, **k: oauth.UsageOutcome(usage=usage, error=None))
        monkeypatch.setattr(oauth, "build_usage_result", lambda *a, **k: None)
        monkeypatch.setattr(sw, "_resync_rotated_backup",
                            lambda *a, **k: reached.append(1))
        sw._fetch_active_usage("2", "owner@example.com", LIVE_DATED)
        return bool(reached)

    def test_the_server_accepted_these_bytes_is_what_gates_the_resync(
            self, switcher, monkeypatch):
        """THE PREMISE, PINNED SEPARATELY FROM THE CONCLUSION. Widen this gate
        and a credential the endpoint never accepted reaches the adopt, and
        the exception above quietly becomes the defect the other four paths
        were fixed for — while its own test goes on passing and defending it.
        Measured before this test existed: `if outcome.usage is not None` to
        `if True` left the whole suite green."""
        assert self._resync_reached(
            switcher, monkeypatch, usage=None) is False

    def test_CONTROL_a_usage_dict_does_reach_the_resync(
            self, switcher, monkeypatch):
        """Without this the assertion above passes for a build where the
        resync is unreachable altogether."""
        assert self._resync_reached(
            switcher, monkeypatch, usage={"five_hour": {"pct": 1.0}}) is True
