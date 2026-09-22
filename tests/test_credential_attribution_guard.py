"""The write-time attribution guard: no write may replace a populated slot's
stored backup with a different account's bytes.

Incident (2026-09-07): an unverified switch-time claim (an identity file's
account number, never itself confirmed against the roster) let account N's
live credential get written under slot M's key. The pin later consumed slot
N's grant while it believed it held slot M's, the server killed that
generation, and the account read as needing a fresh login inside its
documented 30-day grace window.

The check itself is ``CredentialStore._check_attribution``, shared by the
main chokepoint (``CredentialStore._write_account_credentials``, reached by
every caller directly or via the switcher's own ``_write_account_credentials``
wrapper) and by the two backend-only forwarders the switcher exposes for the
macOS-keyring-to-security migration's Keychain-only write
(``switcher.py``'s ``_kc_write_backup``/``_write_backup_enc``, PR 210 round 5
— these used to be a straight, unguarded pass-through to the store's raw
backend writers, which is exactly how ``migrations.py`` reached the Keychain
unattributed). Either path refuses to replace a *populated* slot's stored
backup with a different OAuth lineage (a differing
``oauth.credential_fingerprint``, or a backup that could not be read at all —
"unreadable" refuses like a mismatch, never permits like absent) unless the
caller attests ``attributed=True`` — an explicit, per-call-site claim that it
independently verified the new bytes belong to that slot (a uuid-verified
identity resolution, or a structural move/rotation of that same account's own
record). A first-ever write into an empty slot has nothing stored to
contradict it and is never refused by this alone.

Two tests below DERIVE their subject from the AST rather than naming it, per
the project's own lesson that a container-level or member-listing check goes
blind the day a new member (a new writer, or a new backend call) is added
without review:

- ``TestBackendWritersHaveExactlyOneCaller`` walks EVERY module under
  ``src/claude_swap`` (not just ``credentials.py`` — a single-file scan is
  blind to a caller anywhere else in the package) for every call to the two
  backend writers and asserts each one is textually inside a known guard —
  the guard cannot be routed around by a new private method that reaches the
  Keychain or the ``.enc`` file directly.
- ``TestWriteSiteRosterIsReviewed`` walks EVERY module under
  ``src/claude_swap`` (not three named files — a hand-named list is blind to
  a caller in a module nobody named) for every call into the write
  chokepoint and asserts the derived (file, enclosing function) roster
  matches exactly what this round reviewed — a new call site (or a second
  call added to an existing one, in ANY module) changes the count and fails
  the test, forcing the same review this round gave the other twenty.
"""

from __future__ import annotations

import ast
import json
import logging
import os
from collections import Counter
from pathlib import Path

import pytest

from claude_swap.credentials import CredentialStore
from claude_swap.exceptions import CredentialWriteError
from claude_swap.models import Platform

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src" / "claude_swap"


class _Host:
    """Minimal ``_StoreHost``: data only, same shape as test_credentials.py's."""

    def __init__(self, credentials_dir: Path):
        self.platform = Platform.LINUX
        self.credentials_dir = credentials_dir
        self._logger = logging.getLogger("test")

    def _get_sequence_data(self) -> dict | None:
        # Inert on this branch alone (nothing here reaches the
        # renumber-fallback read that calls it); needed once PR 286's
        # reader merges, which the roster entry below reviews.
        return {"accounts": {}}


def _creds(refresh_token: str) -> str:
    return json.dumps({"claudeAiOauth": {
        "accessToken": "sk-" + refresh_token, "refreshToken": refresh_token,
    }})


ACCOUNT_1_BACKUP = _creds("rt-account-1")
ACCOUNT_2_LIVE = _creds("rt-account-2")


class TestAttributionGuardRefusesUnattributedCrossIdentityWrite:
    def test_refuses_a_populated_slot_overwritten_by_a_different_lineage(
        self, tmp_path, caplog,
    ):
        """RED for the guard: without it, this write silently replaces
        Account-1's stored backup with Account-2's live bytes — the exact
        cross-slot poisoning shape from the incident. Fail-closed: the
        original backup must survive the refusal untouched, and the log
        must name the slot, the email and the recovery command."""
        store = CredentialStore(_Host(tmp_path))
        store._write_account_credentials("1", "test@example.com", ACCOUNT_1_BACKUP)

        with caplog.at_level(logging.ERROR, logger="claude-swap"):
            with pytest.raises(CredentialWriteError, match="cross-identity"):
                store._write_account_credentials(
                    "1", "test@example.com", ACCOUNT_2_LIVE,
                )

        assert store._read_account_credentials("1", "test@example.com") == (
            ACCOUNT_1_BACKUP
        )
        assert any(
            "Refusing to write Account-1-test@example.com" in r.message
            and "cswap add --slot 1" in r.message
            for r in caplog.records
        )

    def test_CONTROL_a_routine_refresh_needs_no_attribution(self, tmp_path):
        """Positive control: an access-token-only refresh (the refresh token
        — and so the fingerprint — unchanged) is the routine case and must
        still succeed with no attestation at all. Without this control, the
        RED test above would pass just as well for a guard that refuses
        every write."""
        store = CredentialStore(_Host(tmp_path))
        store._write_account_credentials("1", "test@example.com", ACCOUNT_1_BACKUP)
        rotated_access_token = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-fresh", "refreshToken": "rt-account-1",
        }})

        store._write_account_credentials(
            "1", "test@example.com", rotated_access_token,
        )

        assert store._read_account_credentials("1", "test@example.com") == (
            rotated_access_token
        )

    def test_attributed_true_is_honored_even_on_a_full_rotation(self, tmp_path):
        """A caller that independently verified the new lineage (a
        uuid-resolved identity, or a CAS against the exact bytes a grant was
        requested for) may pass ``attributed=True`` to write a full
        rotation — a differing fingerprint the guard cannot otherwise
        distinguish from a foreign credential."""
        store = CredentialStore(_Host(tmp_path))
        store._write_account_credentials("1", "test@example.com", ACCOUNT_1_BACKUP)

        store._write_account_credentials(
            "1", "test@example.com", ACCOUNT_2_LIVE, attributed=True,
        )

        assert store._read_account_credentials("1", "test@example.com") == (
            ACCOUNT_2_LIVE
        )

    def test_a_first_ever_write_into_an_empty_slot_needs_no_attribution(
        self, tmp_path,
    ):
        """Absence of evidence is not a match, but it is not a refusal
        either: an empty slot has nothing stored to contradict a first
        write (add / add-token / import / migrate all rely on this)."""
        store = CredentialStore(_Host(tmp_path))

        store._write_account_credentials("1", "test@example.com", ACCOUNT_1_BACKUP)

        assert store._read_account_credentials("1", "test@example.com") == (
            ACCOUNT_1_BACKUP
        )

    @pytest.mark.skipif(
        os.name == "nt" or os.geteuid() == 0,
        reason="needs POSIX permission semantics (non-root)",
    )
    def test_refuses_an_unattested_write_when_the_existing_backup_is_unreadable(
        self, tmp_path,
    ):
        """UNREADABLE must refuse like a mismatch, not permit like absent.

        The plain read the guard used to call returns ``""`` for a populated
        slot it merely could not read (permission denied, EIO) exactly as it
        does for a genuinely empty slot — so on any host where a process
        cannot read its own Keychain (ssh/launchd on macOS, per CONTEXT.md)
        the guard was silently off for every such write, precisely where
        the incident it exists for lives."""
        store = CredentialStore(_Host(tmp_path))
        store._write_account_credentials("1", "test@example.com", ACCOUNT_1_BACKUP)
        enc = store._backup_enc_path("1", "test@example.com")
        enc.chmod(0o000)
        try:
            with pytest.raises(CredentialWriteError, match="unreadable"):
                store._write_account_credentials(
                    "1", "test@example.com", ACCOUNT_2_LIVE,
                )
        finally:
            enc.chmod(0o600)

    def test_the_verification_read_is_marked_for_a_merge_partner_to_see(
        self, tmp_path,
    ):
        """PR 286's renumber-fallback read (``_read_account_credentials``,
        which this guard's own verification read no longer reaches -- see
        ``_check_attribution``'s docstring) mirrors a same-email backup it
        finds under another slot number back under the slot it was asked
        for -- a write of its own. The guard's own read now goes through
        ``_read_account_credentials_direct``, which stays a leaf read and
        never reaches that mirror-write, so the mark below is defense in
        depth rather than the only thing standing between the two PRs and
        recursion.

        ``CredentialStore._in_attribution_read`` (PR 286's own suppression
        flag, defined on ITS branch: ``False`` unless a caller marks a read
        as verification-only) is the merge partner's half of the fix; this
        half is marking the guard's read as exactly that, so the two halves
        only do anything once both are present. Nothing on this branch reads
        the flag today -- this spies on it directly, the only way to pin the
        contract from this side alone.
        """
        store = CredentialStore(_Host(tmp_path))
        store._write_account_credentials("1", "test@example.com", ACCOUNT_1_BACKUP)
        seen_during_check = []
        real_check = store._check_attribution

        def spying_check(*a, **kw):
            marker = []
            real_read_direct = store._read_account_credentials_direct

            def spying_read_direct(*ra, **rkw):
                marker.append(getattr(store, "_in_attribution_read", False))
                return real_read_direct(*ra, **rkw)

            store._read_account_credentials_direct = spying_read_direct
            try:
                return real_check(*a, **kw)
            finally:
                store._read_account_credentials_direct = real_read_direct
                seen_during_check.extend(marker)

        store._check_attribution = spying_check
        try:
            store._write_account_credentials(
                "1", "test@example.com", ACCOUNT_1_BACKUP,
            )
        finally:
            del store._check_attribution

        assert seen_during_check == [True], (
            "the guard's own verification read must be marked "
            "_in_attribution_read=True for the duration of the read, so a "
            "merge partner's converge write can suppress itself for it"
        )
        assert getattr(store, "_in_attribution_read", False) is False, (
            "the mark must not outlive the read it was taken for"
        )

    def test_the_marks_restore_does_not_clobber_an_outer_holder(self, tmp_path):
        """The mark's ``finally`` must restore whatever it found, not a
        literal ``False``. Not live with the single setter this branch ships
        today, but the moment a second one nests (a merge partner's own read
        setting the same flag around a call that reaches this guard again)
        the inner ``finally`` unmarks the OUTER holder early, and the
        converge write it exists to suppress fires for the rest of the outer
        read."""
        store = CredentialStore(_Host(tmp_path))
        store._write_account_credentials("1", "test@example.com", ACCOUNT_1_BACKUP)

        store._in_attribution_read = True  # an outer holder, already marked
        try:
            store._check_attribution(
                "1", "test@example.com", ACCOUNT_1_BACKUP, False,
            )
            assert store._in_attribution_read is True, (
                "the guard's own read cleared an OUTER caller's mark instead "
                "of restoring what it found"
            )
        finally:
            store._in_attribution_read = False


class TestAttributionGuardReadsOnlyThisSlotsOwnKey:
    def test_a_same_email_leftover_under_another_slot_does_not_block_an_empty_slots_first_write(
        self, tmp_path, monkeypatch,
    ):
        """PR 286's renumber-fallback sweep (folded into
        ``_read_account_credentials`` once that PR merges, not present on
        this branch) can answer a DIFFERENT slot's leftover credential for
        the same email. The guard's own verification read must ask "is
        THIS slot's own key populated" (``_read_account_credentials_direct``),
        never the sweep-carrying wrapper — or a leftover parked under
        another slot number makes a genuinely empty destination slot look
        populated, and the guard wrongly raises "Refusing cross-identity
        write" against the contract that an empty slot is always allowed.

        The sweep itself ships on PR 286's branch, not this one, so it is
        simulated here by monkeypatching the wrapper
        (``_read_account_credentials``) to answer as it would: nothing
        under slot 3's own key, but a leftover under a different slot for
        this email.
        """
        store = CredentialStore(_Host(tmp_path))

        def simulated_sweep(account_num, email, failed=None):
            return ACCOUNT_1_BACKUP  # "found" under some other slot number

        monkeypatch.setattr(store, "_read_account_credentials", simulated_sweep)

        store._write_account_credentials(
            "3", "leftover@example.com", ACCOUNT_2_LIVE,
        )

        assert store._read_account_credentials_direct(
            "3", "leftover@example.com"
        ) == ACCOUNT_2_LIVE


def _calls_by_enclosing_function(path: Path, target_names: set[str]) -> list[tuple[str | None, int, str]]:
    """Every call to one of ``target_names`` in ``path``, with its innermost
    enclosing function (``None`` at module scope) and line number."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.func_stack: list[str] = []
            self.hits: list[tuple[str | None, int, str]] = []

        def _visit_def(self, node):
            self.func_stack.append(node.name)
            self.generic_visit(node)
            self.func_stack.pop()

        visit_FunctionDef = _visit_def
        visit_AsyncFunctionDef = _visit_def

        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else (
                func.id if isinstance(func, ast.Name) else None
            )
            if name in target_names:
                enclosing = self.func_stack[-1] if self.func_stack else None
                self.hits.append((enclosing, node.lineno, name))
            self.generic_visit(node)

    Visitor_ = Visitor()
    Visitor_.visit(tree)
    return Visitor_.hits


class TestBackendWritersHaveExactlyOneCaller:
    """The chokepoint itself: nothing but a guarded method may reach the
    Keychain or the ``.enc`` file for a backup write.

    Scanned over EVERY module under ``src/claude_swap`` — a single-file scan
    (``credentials.py`` alone) is blind to a caller anywhere else in the
    package, which is exactly how ``migrations.py`` and ``switcher.py``'s own
    ``_kc_write_backup``/``_write_backup_enc`` forwarders reached the backend
    writers unguarded (PR 210 round 5).

    What this derivation still cannot see, named rather than left implicit:

    - it matches on ``node.func``'s attribute/name text only, so a call
      reached through ``getattr(obj, "_kc_write_backup")(...)``, a bound
      alias (``fn = store._kc_write_backup; fn(...)``), or
      ``functools.partial`` is invisible to it; and it does not follow
      control flow, so a stray call behind an ``if`` that never runs today
      would still fail this test (a false positive, not a false negative —
      the miss is only ever in the "invisible caller" direction).
    - it cannot tell WHICH object a matched call targets — the STORE's raw
      backend writer, or switcher.py's identically-named guarded forwarder
      calling the store's writer one level down. That is why
      ``migrate_macos_keyring_to_security`` (which calls the switcher's
      guarded ``_kc_write_backup``, not the store's) has to be allow-listed
      by name below rather than derived: this test can prove a call site is
      reachable, not which method resolution it took. The attribution
      itself is still enforced at runtime by ``_check_attribution``.
    """

    ALLOWED_ENCLOSING = frozenset({
        # credentials.py's own guarded chokepoint and the helper it calls.
        "_write_account_credentials", "_reconcile_enc_after_keychain_write",
        # switcher.py's forwarders: each runs the same attribution check
        # before reaching the backend writer, making them guarded
        # chokepoints in their own right (their own bodies call the
        # identically-named store method one level down).
        "_kc_write_backup", "_write_backup_enc",
        # migrations.py's one reviewed caller of the switcher's guarded
        # `_kc_write_backup` (attributed=True; see its own inline comment
        # for why the source key already establishes the slot's identity).
        "migrate_macos_keyring_to_security",
    })

    def test_kc_write_backup_and_write_backup_enc_are_only_called_from_a_guard(self):
        py_files = sorted(SRC.rglob("*.py"))
        assert len(py_files) > 3, "expected the whole package, not one file"
        hits: list[tuple[str, str | None, int, str]] = []
        for path in py_files:
            for enclosing, lineno, name in _calls_by_enclosing_function(
                path, {"_kc_write_backup", "_write_backup_enc"},
            ):
                hits.append((path.name, enclosing, lineno, name))
        # Derived, not named: whatever calls exist anywhere in the package,
        # every one of them must be inside an allowed guard — never inside
        # the two forwarder DEFINITIONS' own bodies calling the STORE's
        # backend writer a second level down (that inner call is exactly
        # what "the forwarder is itself a guard now" means), and never a
        # bare caller elsewhere in the tree (migrations.py's old bypass).
        assert hits, "expected at least the guards' own backend calls"
        stray = [h for h in hits if h[1] not in self.ALLOWED_ENCLOSING]
        assert stray == [], (
            f"a backend writer is reachable outside every known guard: {stray}"
        )
        # The reconcile helper (the only other legitimate caller of
        # `_write_backup_enc`) is itself only called from the guard.
        reconcile_callers: list[tuple[str, str | None, int, str]] = []
        for path in py_files:
            for enclosing, lineno, name in _calls_by_enclosing_function(
                path, {"_reconcile_enc_after_keychain_write"},
            ):
                reconcile_callers.append((path.name, enclosing, lineno, name))
        stray_reconcile = [
            h for h in reconcile_callers if h[1] != "_write_account_credentials"
        ]
        assert stray_reconcile == [], (
            f"the .enc reconcile helper is reachable outside the guard: {stray_reconcile}"
        )


# Reviewed 2026-09-08 (PR 210 round 4): every call into the write chokepoint,
# from any module, with its enclosing function. Excludes the chokepoint's own
# two wrapper bodies (switcher.py's `_write_account_credentials` and
# `write_account_credentials`, which only forward into the store/private
# method and carry no reasoning of their own).
EXPECTED_WRITE_SITE_ROSTER: dict[tuple[str, str], int] = {
    ("switcher.py", "_swap_accounts_locked"): 2,
    ("switcher.py", "_rollback_swap"): 1,
    ("switcher.py", "_relocate_locked"): 1,
    ("switcher.py", "persist_backup_credentials"): 1,
    ("switcher.py", "_consume_backup_grant_locked"): 2,
    ("switcher.py", "_adopt_stashed_successor"): 1,
    ("switcher.py", "_adopt_session_credential"): 1,
    ("switcher.py", "add_account"): 2,
    ("switcher.py", "add_account_from_token"): 2,
    ("switcher.py", "_fetch_active_usage"): 2,
    ("switcher.py", "_resync_rotated_backup"): 1,
    ("switcher.py", "_perform_switch_locked"): 1,
    ("transfer.py", "import_accounts"): 1,
    ("migrations.py", "migrate_windows_keyring_to_files"): 1,
}

# The chokepoint's own forwarding bodies: present in the AST but not
# independent writers, so they are excluded from the roster above.
_WRAPPER_BODIES = {"_write_account_credentials", "write_account_credentials"}

# Reviewed 2026-09-08 (the #210 x #286 rebuild): PR 286 adds ONE new call into the
# write chokepoint, in a module #210 does not touch, so it cannot go in
# EXPECTED_WRITE_SITE_ROSTER above — that roster must match exactly on THIS branch,
# where the site does not exist. Reviewed here so the review happens ONCE, in the PR
# that owns the guard, instead of refusing every rebuild.
#
#   credentials.py::_read_account_credentials — PR 286's converge write: after a bare
#   renumber it mirrors the newest-generation backup found under another slot number
#   back under this account's own number (same email, confirmed against the roster;
#   the source slot is never deleted). NOT admitted with attributed=True: it is a write
#   taken from inside a read that the write path's own verification reaches, so PR 286
#   suppresses it for a verification read (CredentialStore._in_attribution_read).
#
#   switcher.py::_register_login_as_new_slot/_adopt_login_into_slot/
#   _adopt_stashed_login_for_slot/_adopt_into_dead_slot — PR 199's bare-login-heals-
#   its-slot engine tick, in functions this round never saw. Each writes only after
#   independently matching the live login's identity against the roster (the
#   `attributed=True` comment at each call site names which read established it) —
#   admitted with attributed=True.
CROSS_PR_WRITE_SITES: dict[tuple[str, str], int] = {
    ("credentials.py", "_read_account_credentials"): 1,
    ("switcher.py", "_register_login_as_new_slot"): 1,
    ("switcher.py", "_adopt_login_into_slot"): 1,
    ("switcher.py", "_adopt_stashed_login_for_slot"): 1,
    ("switcher.py", "_adopt_into_dead_slot"): 1,
}

# A SECOND call PR 199 added inside a function this round DID review
# (_fetch_active_usage, reviewed at count 2 above): the retry six lines below its
# sibling write, both already carrying attributed=True (see switcher.py). Keyed
# separately from CROSS_PR_WRITE_SITES, which is asserted disjoint from the
# reviewed roster below, and ADDED to that key's count rather than replacing it —
# an override could silently absorb an unreviewed second write; addition cannot.
CROSS_PR_WRITE_SITE_DELTAS: dict[tuple[str, str], int] = {
    ("switcher.py", "_fetch_active_usage"): 1,
}

# The union below (`{**EXPECTED_WRITE_SITE_ROSTER, **CROSS_PR_WRITE_SITES}`) lets a
# CROSS_PR key silently OVERRIDE a reviewed count on collision — a future
# CROSS_PR_WRITE_SITES entry sharing a key with the reviewed roster would replace its
# count instead of adding a distinct site, letting an unreviewed second write in that
# function pass. Keys are disjoint today; keep them that way. A count that
# genuinely needs to grow on an EXISTING key goes in CROSS_PR_WRITE_SITE_DELTAS
# instead, which is added, never used to override.
assert EXPECTED_WRITE_SITE_ROSTER.keys().isdisjoint(CROSS_PR_WRITE_SITES), (
    "a CROSS_PR site may not override a reviewed roster count"
)


class TestWriteSiteRosterIsReviewed:
    """Every caller of the write chokepoint, derived from the AST rather
    than hand-listed, must match a roster this round actually reviewed for
    whether it may pass ``attributed=True`` and why (see switcher.py's
    inline comments at each site). A new call site — or a second call added
    to an existing one — changes the derived roster and fails this test,
    the same review gate every site already on it went through."""

    def test_every_writer_is_on_the_reviewed_roster(self):
        derived: Counter[tuple[str, str]] = Counter()
        for path in sorted(SRC.rglob("*.py")):
            filename = path.name
            hits = _calls_by_enclosing_function(
                path, {"_write_account_credentials", "write_account_credentials"},
            )
            is_switcher = filename == "switcher.py"
            for enclosing, _lineno, _name in hits:
                if is_switcher and enclosing in _WRAPPER_BODIES:
                    continue  # the guard's own forwarding, not a writer
                assert enclosing is not None, (
                    f"a module-scope call to the write chokepoint appeared in "
                    f"{filename}; give it a reviewed enclosing function"
                )
                derived[(filename, enclosing)] += 1

        admissible = [EXPECTED_WRITE_SITE_ROSTER]
        # CROSS_PR_WRITE_SITES is admissible only where the suppression it
        # names (CredentialStore._in_attribution_read) actually exists — on
        # #210 alone the flag is absent and that site cannot appear for real;
        # widening the roster unconditionally would admit it anyway.
        if hasattr(CredentialStore, "_in_attribution_read"):
            merged = dict(
                Counter(EXPECTED_WRITE_SITE_ROSTER)
                + Counter(CROSS_PR_WRITE_SITES)
                + Counter(CROSS_PR_WRITE_SITE_DELTAS)
            )
            admissible.append(merged)
        assert dict(derived) in admissible, (
            "the derived write-site roster no longer matches what this round "
            "reviewed — a writer was added, removed, or duplicated; review "
            "whether it may pass attributed=True and update the roster above "
            "(a site on neither roster is unreviewed)"
        )
