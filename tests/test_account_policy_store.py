"""Per-account policy persistence and accessors (switcher.py).

MEU-PAP-02 - AC-6 ... AC-15 of
`.agent/plans/2026-09-02-per-account-threshold-backup/implementation-plan.md`.

Mirrors the `disabled` machinery exactly: `_policy_from_data` reads from
already-loaded data the way `_disabled_from_data` does, the setters mirror
`set_account_disabled`, and both honour the omit-when-default record
convention so an untouched `sequence.json` stays byte-identical.

Synthetic fixtures only. The real store at the user home is never read or
written; `tests/test_real_store_guard.py` enforces that boundary and a failure
there is a P0 stop.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_swap.exceptions import AccountNotFoundError, ConfigError
from claude_swap.models import AccountPolicy, Platform
from claude_swap.switcher import ClaudeAccountSwitcher


class PolicyStoreBase:
    """Shared synthetic-store scaffolding, modelled on `TestDisableEnableAccount`."""

    def _setup(self, temp_home: Path) -> ClaudeAccountSwitcher:
        s = ClaudeAccountSwitcher()
        s.platform = Platform.LINUX
        s._setup_directories()
        s._init_sequence_file()
        return s

    def _seed(self, s: ClaudeAccountSwitcher, num: int, email: str) -> None:
        """Add a fully switchable slot (credential + config backups present)."""
        s._write_account_credentials(
            str(num),
            email,
            json.dumps({"claudeAiOauth": {
                "accessToken": f"sk-{num}", "refreshToken": f"rt-{num}"}}),
        )
        s._write_account_config(
            str(num),
            email,
            json.dumps({"oauthAccount": {
                "emailAddress": email, "accountUuid": f"uuid-{num}"}}),
        )
        data = s._get_sequence_data() or {
            "activeAccountNumber": None, "lastUpdated": "",
            "sequence": [], "accounts": {},
        }
        data["accounts"][str(num)] = {
            "email": email, "uuid": f"uuid-{num}",
            "organizationUuid": "", "organizationName": "",
            "added": "2024-01-01T00:00:00Z",
        }
        if num not in data["sequence"]:
            data["sequence"].append(num)
            data["sequence"].sort()
        if data["activeAccountNumber"] is None:
            data["activeAccountNumber"] = num
        # Fixture-only: `get_timestamp()` has one-second resolution, so a seed
        # written in the same second as the call under test would make a
        # correct restamp indistinguishable from no restamp at all. Backdating
        # the seed makes the "did it stamp `lastUpdated`?" oracle real.
        data["lastUpdated"] = "2024-01-01T00:00:00Z"
        s._write_json(s.sequence_file, data)

    def _fleet(self, temp_home: Path, count: int = 3) -> ClaudeAccountSwitcher:
        s = self._setup(temp_home)
        for n in range(1, count + 1):
            self._seed(s, n, f"account-{n}@example.test")
        return s

    @staticmethod
    def _record(s: ClaudeAccountSwitcher, num: str) -> dict:
        return (s._get_sequence_data() or {})["accounts"][num]


class TestPolicyFromData(PolicyStoreBase):
    """AC-6 - reading a policy out of already-loaded sequence data."""

    def test_missing_record_yields_the_default(self, temp_home: Path):
        s = self._fleet(temp_home)
        data = s._get_sequence_data() or {}
        assert s._policy_from_data(data, "99") == AccountPolicy()

    def test_record_without_policy_keys_yields_the_default(self, temp_home: Path):
        s = self._fleet(temp_home)
        data = s._get_sequence_data() or {}
        assert s._policy_from_data(data, "1") == AccountPolicy()

    def test_reads_threshold_and_backup(self, temp_home: Path):
        s = self._fleet(temp_home)
        s.set_account_threshold("1", 85.0)
        s.set_account_backup("1", True)
        data = s._get_sequence_data() or {}
        assert s._policy_from_data(data, "1") == AccountPolicy(threshold=85.0, backup=True)

    def test_is_a_staticmethod_like_disabled_from_data(self):
        """Mirrors `_disabled_from_data`: no instance state, so it is safe to
        call inside a single-read loop without re-entering the store."""
        assert isinstance(
            ClaudeAccountSwitcher.__dict__["_policy_from_data"], staticmethod
        )

    @pytest.mark.parametrize(
        "garbage", ["garbage", "", None, [], {}, True, float("nan"), float("inf"), 20, 100]
    )
    def test_unreadable_threshold_degrades_to_none_and_does_not_raise(
        self, temp_home: Path, garbage
    ):
        """A corrupt record must degrade, not explode.

        This is the *read* path. The strict validator runs at the *write*
        boundary (AC-38); by the time a value is in the file it may have been
        hand-edited, and the engine must keep running on the global default
        rather than crash the auto-switch loop.
        """
        s = self._fleet(temp_home)
        data = s._get_sequence_data() or {}
        data["accounts"]["1"]["threshold"] = garbage
        s._write_json(s.sequence_file, data)

        reloaded = s._get_sequence_data() or {}
        assert s._policy_from_data(reloaded, "1") == AccountPolicy(threshold=None)

    @pytest.mark.parametrize("truthy,expected", [(True, True), ("yes", True), (1, True),
                                                 (False, False), ("", False), (0, False),
                                                 (None, False)])
    def test_backup_is_read_as_a_bool(self, temp_home: Path, truthy, expected):
        """Mirrors `_disabled_from_data`'s `bool(record.get(...))` exactly."""
        s = self._fleet(temp_home)
        data = s._get_sequence_data() or {}
        data["accounts"]["1"]["backup"] = truthy
        s._write_json(s.sequence_file, data)
        reloaded = s._get_sequence_data() or {}
        assert s._policy_from_data(reloaded, "1").backup is expected

    def test_numeric_string_threshold_is_read(self, temp_home: Path):
        """JSON hand-edited to a string should still be usable, not discarded."""
        s = self._fleet(temp_home)
        data = s._get_sequence_data() or {}
        data["accounts"]["1"]["threshold"] = "85"
        s._write_json(s.sequence_file, data)
        reloaded = s._get_sequence_data() or {}
        assert s._policy_from_data(reloaded, "1").threshold == pytest.approx(85.0)


class TestSetAccountThreshold(PolicyStoreBase):
    """AC-7, AC-8 - the threshold setter, mirroring `set_account_disabled:1860`."""

    def test_writes_the_value_and_stamps_last_updated(self, temp_home: Path):
        s = self._fleet(temp_home)
        before = (s._get_sequence_data() or {})["lastUpdated"]
        s.set_account_threshold("2", 85.0)
        data = s._get_sequence_data() or {}
        assert data["accounts"]["2"]["threshold"] == pytest.approx(85.0)
        assert data["lastUpdated"] != before

    def test_resolves_an_alias_or_email(self, temp_home: Path):
        """AC-37's mechanism, exercised at the store layer via `resolve_account`."""
        s = self._fleet(temp_home)
        s.set_account_threshold("account-3@example.test", 90.0)
        assert self._record(s, "3")["threshold"] == pytest.approx(90.0)

    def test_unknown_identifier_raises_and_writes_nothing(self, temp_home: Path):
        s = self._fleet(temp_home)
        before = s.sequence_file.read_bytes()
        with pytest.raises(AccountNotFoundError):
            s.set_account_threshold("nope@example.test", 85.0)
        assert s.sequence_file.read_bytes() == before, (
            "a failed resolve must leave sequence.json byte-identical"
        )

    def test_no_managed_accounts_raises_config_error(self, temp_home: Path):
        s = self._setup(temp_home)
        s.sequence_file.unlink()
        with pytest.raises(ConfigError):
            s.set_account_threshold("1", 85.0)

    @pytest.mark.parametrize("bad", [20, 100, "abc", float("nan"), float("inf")])
    def test_invalid_value_raises_and_writes_nothing(self, temp_home: Path, bad):
        """AC-38 at the store boundary: validation happens before any write.

        Python `assert` and type hints are not runtime validation (AGENTS.md
        §Boundary Input Contract); this setter must call the normalizer.
        """
        s = self._fleet(temp_home)
        before = s.sequence_file.read_bytes()
        with pytest.raises(ValueError):
            s.set_account_threshold("1", bad)
        assert s.sequence_file.read_bytes() == before

    def test_value_is_normalized_to_float(self, temp_home: Path):
        s = self._fleet(temp_home)
        s.set_account_threshold("1", "85")
        stored = self._record(s, "1")["threshold"]
        assert isinstance(stored, float) and stored == pytest.approx(85.0)

    def test_none_pops_the_key(self, temp_home: Path):
        s = self._fleet(temp_home)
        s.set_account_threshold("1", 85.0)
        assert "threshold" in self._record(s, "1")
        s.set_account_threshold("1", None)
        assert "threshold" not in self._record(s, "1"), (
            "unset must remove the key, not write null - AC-10's omit-when-default"
        )

    def test_popping_an_absent_key_is_a_no_op_not_a_keyerror(self, temp_home: Path):
        s = self._fleet(temp_home)
        s.set_account_threshold("1", None)
        assert "threshold" not in self._record(s, "1")

    def test_overwrite_replaces_rather_than_accumulates(self, temp_home: Path):
        s = self._fleet(temp_home)
        s.set_account_threshold("1", 85.0)
        s.set_account_threshold("1", 90.0)
        assert self._record(s, "1")["threshold"] == pytest.approx(90.0)


class TestSetAccountBackup(PolicyStoreBase):
    """AC-9 - the backup flag, same shape as `disabled`."""

    def test_true_writes_and_false_pops(self, temp_home: Path):
        s = self._fleet(temp_home)
        s.set_account_backup("2", True)
        assert self._record(s, "2")["backup"] is True
        s.set_account_backup("2", False)
        assert "backup" not in self._record(s, "2")

    def test_clearing_an_unset_flag_is_a_no_op(self, temp_home: Path):
        s = self._fleet(temp_home)
        s.set_account_backup("1", False)
        assert "backup" not in self._record(s, "1")

    def test_unknown_identifier_raises_and_writes_nothing(self, temp_home: Path):
        s = self._fleet(temp_home)
        before = s.sequence_file.read_bytes()
        with pytest.raises(AccountNotFoundError):
            s.set_account_backup("nope@example.test", True)
        assert s.sequence_file.read_bytes() == before

    def test_resolves_an_email(self, temp_home: Path):
        s = self._fleet(temp_home)
        s.set_account_backup("account-3@example.test", True)
        assert self._record(s, "3")["backup"] is True


class TestOmitWhenDefault(PolicyStoreBase):
    """AC-10 - an untouched record is unchanged by a write to a different one."""

    def test_untouched_record_is_byte_identical(self, temp_home: Path):
        s = self._fleet(temp_home)
        before = json.loads(s.sequence_file.read_text(encoding="utf-8"))["accounts"]["2"]
        s.set_account_threshold("1", 85.0)
        s.set_account_backup("1", True)
        after = json.loads(s.sequence_file.read_text(encoding="utf-8"))["accounts"]["2"]
        assert after == before

    def test_no_policy_keys_appear_anywhere_in_an_untouched_store(self, temp_home: Path):
        """`sequence.json` stays clean for a fleet that never sets a policy."""
        s = self._fleet(temp_home)
        raw = s.sequence_file.read_text(encoding="utf-8")
        assert "threshold" not in raw
        assert "backup" not in raw

    def test_setting_then_unsetting_restores_the_record_exactly(self, temp_home: Path):
        s = self._fleet(temp_home)
        before = json.loads(s.sequence_file.read_text(encoding="utf-8"))["accounts"]["1"]
        s.set_account_threshold("1", 85.0)
        s.set_account_backup("1", True)
        s.set_account_threshold("1", None)
        s.set_account_backup("1", False)
        after = json.loads(s.sequence_file.read_text(encoding="utf-8"))["accounts"]["1"]
        assert after == before, "round-tripping a policy must leave no residue"


class TestBackupAccountNumbers(PolicyStoreBase):
    """AC-11 - mirrors `disabled_account_numbers():1852`."""

    def test_returns_backup_slots_in_sequence_order(self, temp_home: Path):
        s = self._fleet(temp_home, count=4)
        s.set_account_backup("3", True)
        s.set_account_backup("1", True)
        assert s.backup_account_numbers() == ["1", "3"], "sequence order, not call order"

    def test_empty_when_none_marked(self, temp_home: Path):
        assert self._fleet(temp_home).backup_account_numbers() == []

    def test_excludes_non_switchable_slots(self, temp_home: Path):
        """A slot without usable stored backups is not a candidate for anything."""
        s = self._fleet(temp_home)
        s.set_account_backup("2", True)
        for path in s.credentials_dir.glob("*2*"):
            path.unlink()
        for path in s.configs_dir.glob("*2*"):
            path.unlink()
        assert "2" not in s.backup_account_numbers()

    def test_excludes_disabled_slots(self, temp_home: Path):
        """AC-31's store half: `disabled` wins over `backup`.

        Applied earlier in `switchable_account_numbers()`, so an account that
        is both must appear in neither pass of the two-pass filter.
        """
        s = self._fleet(temp_home)
        s.set_account_backup("2", True)
        s.set_account_disabled("2", True)
        assert s.backup_account_numbers() == []
        assert "2" in s.disabled_account_numbers()


class TestAccountPolicies(PolicyStoreBase):
    """AC-12 - the bulk read the engine uses once per tick."""

    def test_covers_every_slot_in_sequence(self, temp_home: Path):
        s = self._fleet(temp_home, count=3)
        policies = s.account_policies()
        assert set(policies) == {"1", "2", "3"}
        assert all(isinstance(p, AccountPolicy) for p in policies.values())

    def test_reflects_written_values(self, temp_home: Path):
        s = self._fleet(temp_home)
        s.set_account_threshold("2", 85.0)
        s.set_account_backup("3", True)
        policies = s.account_policies()
        assert policies["1"] == AccountPolicy()
        assert policies["2"] == AccountPolicy(threshold=85.0)
        assert policies["3"] == AccountPolicy(backup=True)

    def test_reads_the_sequence_once(self, temp_home: Path, monkeypatch):
        """Single-read pattern, as `switchable_account_numbers:1824` uses.

        The engine calls this on every tick of the auto-switch loop; one read
        per call is the difference between a poll and a file-system hammer.
        """
        s = self._fleet(temp_home, count=5)
        calls = []
        original = s._get_sequence_data
        monkeypatch.setattr(
            s, "_get_sequence_data", lambda *a, **k: (calls.append(1), original(*a, **k))[1]
        )
        s.account_policies()
        assert len(calls) == 1, f"expected exactly one sequence read, got {len(calls)}"

    def test_empty_store_yields_an_empty_mapping(self, temp_home: Path):
        assert self._setup(temp_home).account_policies() == {}


class TestSnapshotCarriesPolicy(PolicyStoreBase):
    """AC-13 - every surface that already carries `disabled` also carries policy.

    *Plan discrepancy, recorded here rather than resolved silently:* the AC
    calls `switcher.py:1765` and `switcher.py:5353` "both `AccountsSnapshot`
    build sites". Only the first is one - `AccountSnapshot(` appears exactly
    once in the package (now `switcher.py:1756`). `:5353` is the `--json`
    `account_row(...)` call. Both *sites* are correctly identified and both
    already pass `disabled=`, so the intent - mirror `disabled` everywhere it
    is surfaced - is unambiguous; only the label is wrong. Both are covered.
    """

    def test_snapshot_row_carries_a_written_policy(self, temp_home: Path):
        s = self._fleet(temp_home)
        s.set_account_threshold("2", 85.0)
        s.set_account_backup("2", True)
        snapshot = s.accounts_snapshot()
        row = next(a for a in snapshot.accounts if a.number == "2")
        assert row.policy == AccountPolicy(threshold=85.0, backup=True)

    def test_plain_account_row_carries_the_default(self, temp_home: Path):
        s = self._fleet(temp_home)
        s.set_account_backup("2", True)
        snapshot = s.accounts_snapshot()
        assert next(a for a in snapshot.accounts if a.number == "1").policy == AccountPolicy()

    def test_json_row_omits_policy_when_unset(self, temp_home: Path):
        """Additive-field convention, exactly as `disabled` uses it.

        Existing consumers keying on the base schema must be unaffected, so an
        untouched account's row is byte-identical to today's.
        """
        from claude_swap.json_output import account_row

        row = account_row(1, "a@example.test", "", "", True, None)
        assert "threshold" not in row
        assert "backup" not in row

    def test_json_row_emits_policy_when_set(self, temp_home: Path):
        from claude_swap.json_output import account_row

        row = account_row(
            1, "a@example.test", "", "", True, None,
            policy=AccountPolicy(threshold=85.0, backup=True),
        )
        assert row["threshold"] == pytest.approx(85.0)
        assert row["backup"] is True


class TestExportImportIgnorePolicy(PolicyStoreBase):
    """AC-14 - policy is machine-local, like `disabled`, and does not travel.

    `transfer.py:262-275` builds each export entry from an explicit field list
    and adds `alias` deliberately; `disabled` is just as deliberately absent.
    A threshold tuned to one machine's usage pattern is not a property of the
    *account*, so it must not ride along on an export.
    """

    def test_export_omits_policy_keys(self, temp_home: Path):
        from claude_swap.transfer import export_accounts

        s = self._fleet(temp_home)
        s.set_account_threshold("1", 85.0)
        s.set_account_backup("2", True)

        out_file = temp_home / "backup.cswap"
        export_accounts(s, str(out_file))
        envelope = json.loads(out_file.read_text(encoding="utf-8"))

        for entry in envelope["accounts"]:
            assert "threshold" not in entry, f"export leaked a threshold: {entry['email']}"
            assert "backup" not in entry, f"export leaked a backup flag: {entry['email']}"

    def test_import_ignores_foreign_policy_keys(self, temp_home: Path, monkeypatch):
        """A hand-edited or future-version export must not inject policy.

        `_validate_imported_account` copies a fixed field list; anything else
        in the payload is data the importer does not know about and must drop,
        not persist.
        """
        import os
        from unittest.mock import patch as _patch

        from claude_swap.transfer import export_accounts, import_accounts

        s = self._fleet(temp_home, count=2)
        out_file = temp_home / "backup.cswap"
        export_accounts(s, str(out_file))

        envelope = json.loads(out_file.read_text(encoding="utf-8"))
        for entry in envelope["accounts"]:
            entry["threshold"] = 85.0
            entry["backup"] = True
        out_file.write_text(json.dumps(envelope), encoding="utf-8")

        dst_home = temp_home.parent / "policy-import-dst"
        dst_home.mkdir()
        with _patch("pathlib.Path.home", return_value=dst_home):
            with _patch.dict(os.environ, {"HOME": str(dst_home)}):
                dst = ClaudeAccountSwitcher()
                dst.platform = Platform.LINUX
                dst._setup_directories()
                dst._init_sequence_file()
                import_accounts(dst, str(out_file))

                seq = dst._get_sequence_data() or {}
                assert seq["accounts"], "import produced no accounts - fixture is degenerate"
                for num, record in seq["accounts"].items():
                    assert "threshold" not in record, f"import injected a threshold at {num}"
                    assert "backup" not in record, f"import injected a backup flag at {num}"
                    assert dst._policy_from_data(seq, num) == AccountPolicy()


class TestPolicySurvivesRenumber(PolicyStoreBase):
    """AC-15 - `swap` and `move` carry policy with the account record.

    This is the decisive argument for storing policy *in the account record*
    rather than in a parallel settings map keyed by slot number: a map keyed by
    slot silently reassigns every policy the moment a user renumbers.
    """

    def test_swap_carries_threshold_and_backup(self, temp_home: Path):
        s = self._fleet(temp_home, count=3)
        s.set_account_threshold("1", 85.0)
        s.set_account_backup("1", True)

        s.swap_accounts("1", "3")

        assert self._record(s, "3")["threshold"] == pytest.approx(85.0)
        assert self._record(s, "3")["backup"] is True
        assert "threshold" not in self._record(s, "1"), "policy was left behind on slot 1"
        assert "backup" not in self._record(s, "1")

    def test_swap_does_not_invent_policy_on_the_other_slot(self, temp_home: Path):
        s = self._fleet(temp_home, count=3)
        s.set_account_threshold("1", 85.0)
        s.swap_accounts("1", "2")
        assert s.account_policies()["2"] == AccountPolicy(threshold=85.0)
        assert s.account_policies()["1"] == AccountPolicy()

    def test_move_carries_policy(self, temp_home: Path):
        s = self._fleet(temp_home, count=3)
        s.set_account_backup("2", True)
        s.set_account_threshold("2", 90.0)

        s.move_account("2", "5")

        assert self._record(s, "5")["backup"] is True
        assert self._record(s, "5")["threshold"] == pytest.approx(90.0)
        assert "2" not in (s._get_sequence_data() or {})["accounts"]

    def test_backup_numbers_follow_the_renumber(self, temp_home: Path):
        """The accessor must agree with the record after a move."""
        s = self._fleet(temp_home, count=3)
        s.set_account_backup("2", True)
        assert s.backup_account_numbers() == ["2"]
        s.move_account("2", "5")
        assert s.backup_account_numbers() == ["5"]


class TestTheJsonListPayloadCarriesPolicy(PolicyStoreBase):
    """VR-2 - AC-13's JSON half, asserted at the caller instead of the helper.

    `TestSnapshotCarriesPolicy::test_json_row_emits_policy_when_set` passes
    `policy=` to `account_row()` by hand. That proves the *helper* honours the
    argument; it says nothing about whether the one production caller actually
    supplies it. Validation reproduced the gap: deleting
    `policy=self._policy_from_data(...)` from `_build_list_payload`
    (`switcher.py:5562`) left the entire suite green while the real
    `--list --json` output silently lost both fields.

    So this drives `list_accounts(json_output=True)` - the actual runtime entry
    point - and asserts on the row it returns.

    `fetch=set()` is load-bearing, not decoration. The CLI default `fetch=None`
    means "every stale account is eligible" (`switcher.py:5031-5036`), so an
    unqualified call here would issue real usage-endpoint requests with the
    fixture's synthetic tokens - slow, flaky, and a charge against a shared
    budget. An empty set satisfies `fetch is None or num in fetch` for nobody
    (`switcher.py:5084`), so nothing is collected and this stays a pure
    payload-assembly oracle.
    """

    @staticmethod
    def _payload(s: ClaudeAccountSwitcher) -> dict:
        """`--list --json`, offline. See the class docstring on `fetch`."""
        return s.list_accounts(json_output=True, fetch=set())

    def _policy_fleet(self, temp_home: Path) -> ClaudeAccountSwitcher:
        s = self._fleet(temp_home)
        s.set_account_threshold("2", 85.0)
        s.set_account_backup("2", True)
        return s

    @staticmethod
    def _row(payload: dict, num: int) -> dict:
        return next(r for r in payload["accounts"] if r["number"] == num)

    def test_the_policied_row_carries_both_fields(self, temp_home: Path):
        payload = self._payload(self._policy_fleet(temp_home))
        row = self._row(payload, 2)
        assert row["threshold"] == pytest.approx(85.0)
        assert row["backup"] is True

    def test_a_threshold_only_account_carries_only_the_threshold(self, temp_home: Path):
        s = self._fleet(temp_home)
        s.set_account_threshold("1", 60.0)
        row = self._row(self._payload(s), 1)
        assert row["threshold"] == pytest.approx(60.0)
        assert "backup" not in row

    def test_a_backup_only_account_carries_only_the_flag(self, temp_home: Path):
        s = self._fleet(temp_home)
        s.set_account_backup("1", True)
        row = self._row(self._payload(s), 1)
        assert row["backup"] is True
        assert "threshold" not in row

    def test_an_untouched_row_in_a_policied_fleet_stays_bare(self, temp_home: Path):
        """The negative half, and the one that makes the pair discriminating.

        A caller that hard-coded a policy instead of reading each account's own
        would pass every positive above and fail here.
        """
        row = self._row(self._payload(self._policy_fleet(temp_home)), 1)
        assert "threshold" not in row
        assert "backup" not in row

    def test_a_fleet_with_no_policy_emits_rows_identical_to_before(self, temp_home: Path):
        """AC-13's omit-when-default contract at the caller, not the helper."""
        payload = self._payload(self._fleet(temp_home))
        for row in payload["accounts"]:
            assert "threshold" not in row
            assert "backup" not in row
