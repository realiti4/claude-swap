"""R11 CRITICAL: is the collector's own ACTIVE read a third collapse site?

`_build_accounts_info:3188` does `creds = active.value or ""` and records
`_active_read_degraded`, but the quarantine scan passes those bytes to
`_entry_token_dead` and `_static_usage_sentinel:3905` has only a
`keychain_unavailable` arm. A DEGRADED read returns BYTES, so every
"empty read" guard is bypassed.

The probe is INSTRUMENTED: it prints the strike state it actually built, so a
`sentinel=None` from never reaching the path cannot be mistaken for a fix.
"""
import json
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import oauth
from claude_swap.credentials import ActiveCredentials
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.json_output import USAGE_RELOGIN_REQUIRED
from claude_swap.models import Platform
from claude_swap.usage_store import FetchRecord as FR, UsageEntry

OLD = json.dumps({"claudeAiOauth": {"accessToken": "sk-old",
                                    "refreshToken": "rt-old", "expiresAt": 1000}})
NEW = json.dumps({"claudeAiOauth": {"accessToken": "sk-new",
                                    "refreshToken": "rt-new", "expiresAt": 99999999999000}})


@pytest.mark.parametrize("degraded", [False, True], ids=["CONTROL-healthy", "PROBE-degraded"])
def test_degraded_active_read_must_not_condemn_a_healed_slot(
    degraded, temp_home: Path, mock_claude_config: Path,
    sample_sequence_data: dict, monkeypatch,
):
    sample_sequence_data["accounts"]["2"]["email"] = "b@example.com"
    s = ClaudeAccountSwitcher()
    s.platform = Platform.MACOS
    s._setup_directories()
    s._write_json(s.sequence_file, sample_sequence_data)
    idents = {"2": ("b@example.com", "")}
    s._usage_store.record(
        {"2": FR(error="invalid_grant", struck_fp=oauth.credential_fingerprint(OLD))},
        idents,
    )
    # HEALED: backup now holds the new generation; the struck fp matches nothing.
    s._write_account_credentials("2", "b@example.com", NEW)

    pre = s._usage_store.entries(idents, [])["2"]
    print(f"\n  [{'degraded' if degraded else 'healthy '}] PREMISE "
          f"strikes={pre.auth_dead_strikes} token_dead={pre.token_dead()}")

    # Patch the object _build_accounts_info ACTUALLY calls (self, not _store).
    # A degraded read serves the STALE generation — that is what "degraded"
    # means: the Keychain read failed and a plaintext fallback covered it,
    # so the bytes may be the superseded ones. Serving NEW here (my first
    # cut) can never condemn anything, which is why both rows passed.
    served = OLD if degraded else NEW
    monkeypatch.setattr(s, "_read_active_credentials",
                        lambda: ActiveCredentials(served, False, degraded))
    # `_build_accounts_info` derives active_num from _get_current_account()
    # (the live IDENTITY), NOT from current_account_number(). Patching the
    # latter left every row is_active=False, so the branch under test never
    # ran and both rows "passed" for the wrong reason.
    monkeypatch.setattr(s, "_get_current_account",
                        lambda: ("b@example.com", ""))
    with patch.object(s, "current_account_number", return_value="2"):
        info = s._build_accounts_info()
        print("  info rows:", [(r[0], r[4], (r[5] or '')[:12]) for r in info])
        entries = s._collect_usage_entries(info, fetch=set())

    e = entries["2"]
    print(f"  [{'degraded' if degraded else 'healthy '}] RESULT  "
          f"sentinel={e.sentinel!r}  _active_read_degraded="
          f"{getattr(s, '_active_read_degraded', '<absent>')}")
    assert e.sentinel != USAGE_RELOGIN_REQUIRED, (
        "an already-healed active slot was condemned on a degraded read"
    )


@pytest.mark.parametrize("degraded", [False, True],
                         ids=["CONTROL-healthy", "PROBE-degraded"])
def test_post_fetch_call_site_is_guarded_too(degraded):
    """`_collect_usage_entries` calls `_entry_token_dead` TWICE: the pre-fetch
    quarantine scan, and again after a fetch returns invalid_grant. The test
    above runs with ``fetch=set()`` and so only ever reaches the first one.

    Measured: dropping ``self._active_read_degraded`` from the SECOND call
    site leaves the whole suite green (447 passed), while the same inputs
    flip the verdict True<->False here. That mutant is not equivalent, it was
    merely untested — a guard no test kills is one the next refactor removes.

    Driven at the method, not through the collector: reaching the post-fetch
    branch needs a granted claim plus a fetch that re-strikes, and a fleet
    built by hand for it silently failed to claim the slot (strikes stayed 0),
    which reads as a pass while never entering the branch.

    NOTE, measured: this pins the GUARD, not the WIRING. Because it calls the
    method directly it does not pass through either call site, so dropping
    `self._active_read_degraded` from the post-fetch call still leaves it
    green. `test_post_fetch_call_site_passes_the_flag` below is what kills
    that mutant.
    """
    struck = oauth.credential_fingerprint(OLD)
    s = ClaudeAccountSwitcher.__new__(ClaudeAccountSwitcher)
    # HEALED: the struck generation matches nothing stored any more.
    s._read_account_credentials_ex = lambda num, email: (NEW, False)
    entry = UsageEntry(auth_dead_strikes=1, struck_fingerprint=struck)

    # OLD is what a DEGRADED read serves: the superseded generation, because
    # Claude Code rotates keychain-only and the plaintext fallback lags.
    verdict = ClaudeAccountSwitcher._entry_token_dead(
        s, entry, "2", "b@example.com", OLD, True, degraded,
    )
    if degraded:
        # `is None`, not `is False`. What this test exists to pin is "must
        # not CONDEMN", and both answers satisfy it. `False` was the stronger
        # claim, and it was wrong: it is the caller's strike-CLEAR branch, so
        # asserting it here demanded that a degraded read CONFIRM a heal it
        # never observed — the round-13 I-2 defect, pinned as correct.
        # A backup diverging from the struck generation is evidence about the
        # BACKUP; the live bytes went unread, so nothing witnessed the heal.
        assert verdict is None, (
            "a degraded read serving the struck generation must reach neither "
            "verdict at the post-fetch call site: True condemns an already-"
            "healed slot, False erases a live-generation strike"
        )
    else:
        assert verdict is True, (
            "CONTROL BROKEN: a healthy read of the struck generation must "
            "still confirm dead, or this test cannot detect the guard"
        )


@pytest.mark.parametrize("degraded", [False, True],
                         ids=["healthy", "degraded"])
def test_unstruck_row_is_unaffected_by_the_degraded_flag(degraded):
    """The guard must only narrow the STRUCK path. An unstruck row answers
    False either way — the same invariant the docstring commits to."""
    s = ClaudeAccountSwitcher.__new__(ClaudeAccountSwitcher)
    s._read_account_credentials_ex = lambda num, email: (NEW, False)
    entry = UsageEntry(auth_dead_strikes=0,
                       struck_fingerprint=oauth.credential_fingerprint(OLD))
    assert ClaudeAccountSwitcher._entry_token_dead(
        s, entry, "2", "b@example.com", OLD, True, degraded) is False


def test_post_fetch_call_site_passes_the_flag():
    """The post-fetch call site must actually HAND `_active_read_degraded` in.

    The guard test above pins the method's behaviour but calls it directly,
    so it cannot see a call site that forgot the argument — measured: with
    only that test present, dropping the argument here left 451 passing.
    A guard nothing kills is one the next refactor deletes.

    Read from the source rather than driven through a fleet: reaching the
    post-fetch branch for real needs a granted claim plus a re-striking
    fetch, and every hand-built fleet for it so far failed to claim the slot
    and reported a pass without ever entering the branch. What must hold is
    structural — the argument is present at BOTH call sites — so that is
    what is asserted, with the count as its own control.
    """
    import ast
    import inspect
    from claude_swap.switcher import ClaudeAccountSwitcher

    src = inspect.getsource(ClaudeAccountSwitcher._collect_usage_entries)
    tree = ast.parse(textwrap.dedent(src))
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "_entry_token_dead"
    ]
    assert len(calls) == 2, (
        f"_collect_usage_entries has {len(calls)} _entry_token_dead call "
        "sites, not 2 — this test's premise moved; re-derive it rather than "
        "loosening the count"
    )
    for i, call in enumerate(calls):
        passed = [
            a for a in call.args
            if isinstance(a, ast.Attribute) and a.attr == "_active_read_degraded"
        ] + [
            k for k in call.keywords if k.arg == "active_read_degraded"
        ]
        assert passed, (
            f"call site {i + 1} of _entry_token_dead does not pass "
            "_active_read_degraded: a degraded active read will confirm a "
            "dead verdict against possibly-stale bytes there"
        )


class TestTheDegradedFlagIsPerPassNotPerObject:
    """The degraded verdict belongs to the READ that produced it, not to the
    switcher object.

    `_build_accounts_info` clears the verdict before reading and publishes the
    real one after. While the switcher held that on an INSTANCE attribute, a
    second pass landing inside the read window saw `False` — a value no read
    produced — and re-entered the condemn branch on possibly-stale bytes. The
    TUI's two refresh lanes and the auto engine's worker share one switcher,
    so "the main thread writes it before the fetch pool starts" was true of
    the fetch POOL and never of a second PASS.

    The verdict is thread-local now (`_active_verdict_tls`), so a pass can
    only ever clear its OWN. A pool worker never reads, so it inherits the
    verdict explicitly through `_with_active_verdict` — that wrapper is the
    only way the value crosses a thread, and the first test drives exactly
    that path.

    Real threads, not a mocked interleaving: the window is the duration of a
    real call, so a test that fakes the ordering proves nothing about it.
    """

    def test_a_concurrent_reader_never_sees_a_blank_during_the_active_read(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict, monkeypatch,
    ):
        import threading
        import time

        sample_sequence_data["accounts"]["2"]["email"] = "b@example.com"
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        s._write_json(s.sequence_file, sample_sequence_data)

        # A PRIOR pass already established DEGRADED. Nothing has read the
        # Keychain successfully since, so no read has produced `False`.
        s._record_active_verdict(ActiveCredentials(OLD, False, True))

        entered = threading.Event()
        release = threading.Event()

        def slow_degraded_read():
            entered.set()
            release.wait(2)  # the Keychain read window
            return ActiveCredentials(OLD, False, True)

        monkeypatch.setattr(s, "_read_active_credentials", slow_degraded_read)
        monkeypatch.setattr(s, "_get_current_account", lambda: ("b@example.com", ""))

        seen: list[bool] = []

        def concurrent_collector():
            entered.wait(2)
            time.sleep(0.02)  # land INSIDE the window
            seen.append(s._active_read_degraded)
            release.set()

        # WRAPPED, because that is the production shape: `_run_usage_fetches`
        # hands every pool worker to `_with_active_verdict`, which is what
        # carries the verdict across the thread boundary. An unwrapped thread
        # never read and correctly answers with a clean verdict — testing that
        # one would pin the fallback, not the invariant.
        reader = threading.Thread(target=s._with_active_verdict(concurrent_collector))
        reader.start()
        with patch("claude_swap.switcher.ClaudeAccountSwitcher.current_account_number",
                   return_value="2"):
            s._build_accounts_info()
        reader.join(3)

        assert seen == [True], (
            f"a concurrent pass read _active_read_degraded={seen} while the "
            "active Keychain read was still in flight — a value no read "
            "produced, and False routes it straight back into the condemn "
            "branch on possibly-stale bytes"
        )

    def test_a_genuinely_healthy_read_still_clears_the_flag(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict, monkeypatch,
    ):
        """CONTROL for the probe above: the flag is not simply pinned True.

        Without this, "never observed False" is satisfiable by never writing
        False at all, which would strand the consume gate closed forever
        after one transient Keychain timeout.
        """
        sample_sequence_data["accounts"]["2"]["email"] = "b@example.com"
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        s._write_json(s.sequence_file, sample_sequence_data)
        s._record_active_verdict(ActiveCredentials(OLD, False, True))  # prior pass

        monkeypatch.setattr(
            s, "_read_active_credentials",
            lambda: ActiveCredentials(NEW, False, False),  # Keychain answered
        )
        monkeypatch.setattr(s, "_get_current_account", lambda: ("b@example.com", ""))
        with patch("claude_swap.switcher.ClaudeAccountSwitcher.current_account_number",
                   return_value="2"):
            s._build_accounts_info()

        assert s._active_read_degraded is False, (
            "CONTROL BROKEN: a non-degraded read must clear the flag, or the "
            "probe above passes vacuously"
        )


class TestADegradedReadNeverCONFIRMSaSlotHEALED:
    """R13 I-2: the round-11 guard's docstring says a degraded read "falls
    through to the same backup-fallback/``None`` machinery already used for an
    unreadable backup". It does not. ``None`` arises ONLY when the backup is
    UNREADABLE; a READABLE but DIVERGENT backup returns hard ``False``.

    ``False`` is the caller's strike-CLEAR branch. A strike bound to the LIVE
    generation (``refresh_input = live``) is erased 1 -> 0 and persisted, the
    slot is reservable again once backoff lapses, the dead token is re-POSTed,
    and the user never sees the re-login prompt. That is round 8's regression
    in the one shape the guard cannot observe.

    Three rows at the method, identical except the backup.
    """

    def _verdict(self, backup, unreadable, degraded=True, stored=OLD):
        s = ClaudeAccountSwitcher.__new__(ClaudeAccountSwitcher)
        s._read_account_credentials_ex = lambda n, e: (backup, unreadable)
        entry = UsageEntry(auth_dead_strikes=1,
                           struck_fingerprint=oauth.credential_fingerprint(OLD))
        # `stored` defaults to OLD: a degraded read serving the STRUCK
        # generation, the shape the round-11 guard refuses to compare.
        return ClaudeAccountSwitcher._entry_token_dead(
            s, entry, "2", "b@example.com", stored, True, degraded,
        )

    def test_an_unreadable_backup_holds_the_strike(self):
        """CONTROL: the row the docstring already describes correctly."""
        assert self._verdict(None, True) is None

    def test_a_matching_backup_still_confirms_dead(self):
        """CONTROL: the degraded read must not disarm a confirm the BACKUP
        makes on its own — those bytes are not the degraded ones."""
        assert self._verdict(OLD, False) is True

    def test_a_divergent_readable_backup_must_not_clear_a_live_bound_strike(self):
        """PROBE: the backup diverging says nothing about the LIVE generation,
        which the guard above refused to compare. Confirmed-healed is a claim
        no source here supports."""
        v = self._verdict(NEW, False)
        assert v is None, (
            f"got {v!r}: a divergent backup on a DEGRADED active read routes "
            "into the caller's strike-CLEAR branch and erases a strike bound "
            "to the live generation"
        )

    def test_an_absent_backup_is_ambiguous_too_on_a_degraded_read(self):
        """Same shape: with no backup at all, the live bytes are the ONLY
        source, and the guard refused to trust them."""
        v = self._verdict("", False)
        assert v is None, (
            f"got {v!r}: with no backup and untrustworthy live bytes there is "
            "no source that could have healed the strike"
        )

    def test_NO_source_at_all_is_ambiguous_on_a_degraded_read(self):
        """The closing raw-count branch must stay BELOW the degraded guard.

        With both sources empty that branch answers the raw strike count --
        right when the live read was trustworthy, wrong here, because a
        degraded read examined nothing and `None` is the only honest answer.
        The ordering IS that distinction, and this is the only row that pins
        it: every sibling passes a non-empty `stored`, so the closing branch
        cannot fire for them wherever it sits.

        No caller reaches this row today: `_slot_token_dead` screens the
        degraded read itself and leaves the flag defaulted, and the collector
        sentinels an empty-creds active slot before the scan. The invariant is
        held here for the caller that stops doing one of those.
        """
        v = self._verdict("", False, stored="")
        assert v is None, (
            f"got {v!r}: a slot whose live bytes were WITHHELD and whose "
            "backup is absent was condemned on a read that examined nothing"
        )

    def test_a_HEALTHY_read_still_clears_on_a_divergent_backup(self):
        """CONTROL, and the invariant the round-9/10 tests pin: ``None`` must
        NOT widen into the non-degraded path. There the live bytes WERE
        compared (`stored=NEW`, and it did not match), so a divergent backup
        really does mean every stored source has moved on — a genuine heal
        that must stay a hard ``False``."""
        assert self._verdict(NEW, False, degraded=False, stored=NEW) is False

    def test_a_HEALTHY_read_with_an_ABSENT_backup_still_clears(self):
        """CONTROL for the widening above: this is exactly
        `test_active_strike_healed_by_absent_backup_not_unreadable`'s shape
        (round 9/10). ``None`` must not reach it."""
        assert self._verdict("", False, degraded=False, stored=NEW) is False

    def test_an_unstruck_row_is_still_never_ambiguous(self):
        """CONTROL: only a STRUCK row can go ambiguous."""
        s = ClaudeAccountSwitcher.__new__(ClaudeAccountSwitcher)
        s._read_account_credentials_ex = lambda n, e: (NEW, False)
        entry = UsageEntry(auth_dead_strikes=0,
                           struck_fingerprint=oauth.credential_fingerprint(OLD))
        assert ClaudeAccountSwitcher._entry_token_dead(
            s, entry, "2", "b@example.com", OLD, True, True) is False


class TestTheCollectorHoldsTheStrikeOnADivergentBackup:
    """The method's answer only matters through the caller. Driven through
    the real ``_collect_usage_entries`` so a verdict change that the strike
    accounting ignores cannot read as a fix.
    """

    def test_the_strike_row_survives_a_degraded_pass(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict, monkeypatch,
    ):
        sample_sequence_data["accounts"]["2"]["email"] = "b@example.com"
        s = ClaudeAccountSwitcher()
        s.platform = Platform.MACOS
        s._setup_directories()
        s._write_json(s.sequence_file, sample_sequence_data)
        idents = {"2": ("b@example.com", "")}
        # The strike is bound to the LIVE generation (OLD) — `refresh_input`
        # is the live credential on the active path.
        s._usage_store.record(
            {"2": FR(error="invalid_grant",
                     struck_fp=oauth.credential_fingerprint(OLD))},
            idents,
        )
        # The BACKUP diverges: it holds a different lineage entirely. That is
        # not evidence the LIVE generation healed.
        s._write_account_credentials("2", "b@example.com", NEW)

        # A degraded read serving the struck (live) generation.
        monkeypatch.setattr(s, "_read_active_credentials",
                            lambda: ActiveCredentials(OLD, False, True))
        monkeypatch.setattr(s, "_get_current_account",
                            lambda: ("b@example.com", ""))
        with patch.object(s, "current_account_number", return_value="2"):
            info = s._build_accounts_info()
            s._collect_usage_entries(info, fetch=set())

        after = s._usage_store.entries(idents, [])["2"]
        assert after.auth_dead_strikes == 1, (
            f"strikes went {1} -> {after.auth_dead_strikes}: a degraded read "
            "with a divergent backup erased a live-generation strike, and the "
            "dead token becomes reservable again once backoff lapses"
        )

    def test_the_relogin_warning_returns_once_the_keychain_answers(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict, monkeypatch,
    ):
        """The user-visible guarantee I-2 restores, and the reason holding the
        strike matters beyond bookkeeping.

        Every surface that warns about a dead credential keys on
        ``USAGE_RELOGIN_REQUIRED``. While the read is degraded no pass can
        honestly raise it — but with the strike ERASED it could never be
        raised again either, and the account renders healthy forever. Held,
        the warning is merely DEFERRED: the next pass that actually reads the
        credential surfaces it.
        """
        sample_sequence_data["accounts"]["2"]["email"] = "b@example.com"
        s = ClaudeAccountSwitcher()
        s.platform = Platform.MACOS
        s._setup_directories()
        s._write_json(s.sequence_file, sample_sequence_data)
        idents = {"2": ("b@example.com", "")}
        s._usage_store.record(
            {"2": FR(error="invalid_grant",
                     struck_fp=oauth.credential_fingerprint(OLD))},
            idents,
        )
        s._write_account_credentials("2", "b@example.com", NEW)  # divergent
        monkeypatch.setattr(s, "_get_current_account", lambda: ("b@example.com", ""))

        # Pass 1: DEGRADED. No verdict is honest, so no sentinel — and no
        # warning. That is the accepted cost of not guessing.
        monkeypatch.setattr(s, "_read_active_credentials",
                            lambda: ActiveCredentials(OLD, False, True))
        with patch.object(s, "current_account_number", return_value="2"):
            degraded_pass = s._collect_usage_entries(
                s._build_accounts_info(), fetch=set())["2"]
        assert degraded_pass.sentinel != USAGE_RELOGIN_REQUIRED

        # Pass 2: the Keychain answers, still serving the struck generation.
        # Now the live bytes ARE evidence, and the held strike converts.
        monkeypatch.setattr(s, "_read_active_credentials",
                            lambda: ActiveCredentials(OLD, False, False))
        with patch.object(s, "current_account_number", return_value="2"):
            healthy_pass = s._collect_usage_entries(
                s._build_accounts_info(), fetch=set())["2"]

        assert healthy_pass.sentinel == USAGE_RELOGIN_REQUIRED, (
            f"got {healthy_pass.sentinel!r}: the degraded pass consumed the "
            "strike, so the re-login prompt can never appear and a dead "
            "pinned account renders healthy forever"
        )


class TestALoginMovedTheLiveBytesTheActiveSlotWillPOST:
    """The ACTIVE slot POSTs its LIVE credential; the backup is not what goes
    on the wire. So a backup still holding the struck generation must not
    re-confirm a strike the live bytes have already moved past.

    MEASURED, and it is the loop the owner spent a night inside: slot active,
    struck on `invalid_grant`, re-login replaces the live bytes (fingerprints
    differ, confirmed on the host). The backup still matches the struck
    fingerprint because the only thing that resyncs it is
    `_resync_rotated_backup`, which runs inside `_fetch_active_usage` — the
    fetch this very strike refuses. Quarantine holds, usage reads UNKNOWN, the
    engine fails the account over, and the TUI asks for a re-login that has
    already happened.

    The strike is not lost: if the live bytes fail too, the next strike binds
    to THEM, so this costs one POST per generation and cannot loop.
    """

    # A REFRESH DOES NOT EXTEND `refreshTokenExpiresAt`; only a login mints a
    # new one. So these two differ in exactly the field that separates a
    # re-login from an ordinary rotation, and `ROTATED` below is the control.
    STRUCK = json.dumps({"claudeAiOauth": {
        "accessToken": "sk-struck", "refreshToken": "rt-struck",
        "expiresAt": 1, "refreshTokenExpiresAt": 1_000}})
    RELOGIN = json.dumps({"claudeAiOauth": {
        "accessToken": "sk-relogin", "refreshToken": "rt-relogin",
        "expiresAt": 9_999_999_999_000, "refreshTokenExpiresAt": 9_000}})
    ROTATED = json.dumps({"claudeAiOauth": {
        "accessToken": "sk-rotated", "refreshToken": "rt-rotated",
        "expiresAt": 9_999_999_999_000, "refreshTokenExpiresAt": 1_000}})

    def _verdict(self, stored, backup, is_active=True, degraded=False,
                 struck=None):
        s = ClaudeAccountSwitcher.__new__(ClaudeAccountSwitcher)
        s._read_account_credentials_ex = lambda n, e: (backup, False)
        entry = UsageEntry(
            auth_dead_strikes=1,
            struck_fingerprint=oauth.credential_fingerprint(
                struck if struck is not None else OLD))
        return ClaudeAccountSwitcher._entry_token_dead(
            s, entry, "2", "b@example.com", stored, is_active, degraded,
        )

    def test_a_relogin_releases_the_slot_even_with_a_stale_backup(self):
        v = self._verdict(self.RELOGIN, self.STRUCK, struck=self.STRUCK)
        assert v is False, (
            f"got {v!r}: the live bytes moved past the struck generation and "
            "the slot is still quarantined on a backup nothing will POST — "
            "and the fetch that would resync that backup is the one this "
            "verdict refuses"
        )

    def test_CONTROL_an_idle_slot_still_confirms_on_its_backup(self):
        """The backup IS what an idle slot POSTs, so there the confirm stands.
        Without this the change would empty the quarantine for every slot.

        An idle slot has ONE source and the caller passes it as `stored` —
        the method's own docstring says so, and feeding it the two-source
        shape tests nothing this code can see."""
        assert self._verdict(self.STRUCK, None, is_active=False,
                             struck=self.STRUCK) is True

    def test_CONTROL_live_bytes_that_still_match_are_still_dead(self):
        """The release is keyed on the live bytes having MOVED, not on the
        slot being active."""
        assert self._verdict(self.STRUCK, self.STRUCK,
                             struck=self.STRUCK) is True

    def test_CONTROL_an_ordinary_rotation_does_not_release_it(self):
        """THE CONTROL THAT NARROWED THIS FIX. A first cut released on the
        live bytes merely DIFFERING, which is true of every routine rotation
        of a still-dead lineage — it broke four existing cases that exist to
        stop exactly that. A rotation carries the SAME refresh lifetime."""
        assert self._verdict(self.ROTATED, self.STRUCK,
                             struck=self.STRUCK) is True

    def test_CONTROL_an_OLDER_login_in_the_live_slot_does_not_release_it(self):
        """DIRECTION IS THE WHOLE TEST. An earlier generation restored into the
        live store — a rolled-back copy, a stale plaintext fallback — differs
        from the struck bytes just as a re-login does, and releasing on
        "differs" would hand the fetch a credential older than the one already
        condemned. Only a LATER refresh lifetime is a login."""
        older = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-older", "refreshToken": "rt-older",
            "expiresAt": 9_999_999_999_000, "refreshTokenExpiresAt": 500}})
        assert self._verdict(older, self.STRUCK, struck=self.STRUCK) is True

    def test_CONTROL_a_degraded_read_cannot_release_it(self):
        """Degraded means the live bytes were never examined, so 'they moved'
        is a claim nothing made — the existing ambiguity rules still own it."""
        assert self._verdict(self.RELOGIN, self.STRUCK, degraded=True,
                             struck=self.STRUCK) is True
