"""Per-account chain-order persistence and accessors (switcher.py).

MEU-ORD-02 — AC-9 … AC-16 of
`.agent/plans/2026-09-03-per-account-order/implementation-plan.md`.

Mirrors the PR 1 policy machinery exactly: `_policy_from_data` gains one more
tolerantly-read key, `set_account_order` mirrors `set_account_threshold`, and
`account_orders()` mirrors `account_policies()`. The omit-when-default record
convention is what keeps an untouched `sequence.json` byte-identical.

The asymmetry under test throughout is **strict at the write boundary, tolerant
on read**: `set_account_order("1", 0)` raises before the file is opened, while a
record already holding `"order": 0` loads as unpinned rather than crashing the
auto-switch loop mid-tick.

Synthetic fixtures only. The real store at the user home is never read or
written; `tests/test_real_store_guard.py` enforces that boundary and a failure
there is a P0 stop.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_swap.exceptions import AccountNotFoundError
from claude_swap.models import AccountPolicy, Platform
from claude_swap.switcher import ClaudeAccountSwitcher


class OrderStoreBase:
    """Shared synthetic-store scaffolding, identical to `PolicyStoreBase`."""

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

    @staticmethod
    def _write_raw_order(s: ClaudeAccountSwitcher, num: str, raw: object) -> None:
        """Put a value straight into the record, bypassing the validator.

        The only way to test read-tolerance: `set_account_order` refuses every
        value AC-9 cares about, so a hand-edited store has to be simulated.
        """
        data = s._get_sequence_data() or {}
        data["accounts"][num]["order"] = raw
        s._write_json(s.sequence_file, data)


class TestOrderReadTolerance(OrderStoreBase):
    """AC-9 — `_policy_from_data` degrades, never raises."""

    def test_record_without_order_yields_none(self, temp_home: Path):
        s = self._fleet(temp_home)
        data = s._get_sequence_data() or {}
        assert s._policy_from_data(data, "1").order is None

    def test_missing_record_yields_the_default(self, temp_home: Path):
        s = self._fleet(temp_home)
        data = s._get_sequence_data() or {}
        assert s._policy_from_data(data, "99") == AccountPolicy()

    def test_reads_a_valid_order(self, temp_home: Path):
        s = self._fleet(temp_home)
        s.set_account_order("1", 3)
        data = s._get_sequence_data() or {}
        assert s._policy_from_data(data, "1").order == 3

    @pytest.mark.parametrize(
        "raw",
        [
            "banana", "", "1x", None, 0, -1, 1000, 1.5, "1.5",
            True, False, [], {}, float("nan"), float("inf"),
        ],
    )
    def test_unusable_order_reads_as_unpinned(self, temp_home: Path, raw):
        """Hand-edited garbage, out of range, fractional, or the wrong type:
        every one of them reads as `None`, which means "unpinned", which is
        exactly the pre-feature behaviour for that account."""
        s = self._fleet(temp_home)
        self._write_raw_order(s, "1", raw)
        data = s._get_sequence_data() or {}
        assert s._policy_from_data(data, "1").order is None

    def test_a_corrupt_order_does_not_disturb_the_other_fields(self, temp_home: Path):
        """Degradation is per-key. A bad `order` must not discard a good
        `threshold` sitting in the same record."""
        s = self._fleet(temp_home)
        s.set_account_threshold("1", 85.0)
        s.set_account_backup("1", True)
        self._write_raw_order(s, "1", "banana")
        data = s._get_sequence_data() or {}
        assert s._policy_from_data(data, "1") == AccountPolicy(
            threshold=85.0, backup=True, order=None
        )

    def test_a_corrupt_order_does_not_disturb_other_accounts(self, temp_home: Path):
        s = self._fleet(temp_home)
        s.set_account_order("2", 4)
        self._write_raw_order(s, "1", {"nested": "garbage"})
        assert s.account_orders() == {"2": 4}

    def test_integral_float_in_the_store_reads_as_int(self, temp_home: Path):
        """A JSON round-trip can widen 2 to 2.0. The read must not treat that
        as corruption, and must not hand the engine a float rank."""
        s = self._fleet(temp_home)
        self._write_raw_order(s, "1", 2.0)
        data = s._get_sequence_data() or {}
        order = s._policy_from_data(data, "1").order
        assert order == 2
        assert type(order) is int


class TestSetAccountOrder(OrderStoreBase):
    """AC-10, AC-15 — the write boundary."""

    def test_sets_the_key(self, temp_home: Path):
        s = self._fleet(temp_home)
        s.set_account_order("1", 3)
        assert self._record(s, "1")["order"] == 3

    def test_stores_an_int_even_when_given_a_string(self, temp_home: Path):
        """The CLI hands over argv. A string in the store would compare wrong
        against `ORDER_UNSET_RANK` and would serialize as `"3"` in JSON."""
        s = self._fleet(temp_home)
        s.set_account_order("1", "3")
        stored = self._record(s, "1")["order"]
        assert stored == 3
        assert type(stored) is int

    def test_unset_removes_the_key(self, temp_home: Path):
        s = self._fleet(temp_home)
        s.set_account_order("1", 3)
        s.set_account_order("1", None)
        assert "order" not in self._record(s, "1"), (
            "unset must remove the key, not write null — AC-10's omit-when-default"
        )

    def test_resolves_by_email_and_alias(self, temp_home: Path):
        s = self._fleet(temp_home)
        s.set_account_order("account-2@example.test", 5)
        assert self._record(s, "2")["order"] == 5

    def test_unknown_identifier_raises(self, temp_home: Path):
        s = self._fleet(temp_home)
        with pytest.raises(AccountNotFoundError):
            s.set_account_order("99", 3)

    def test_stamps_last_updated(self, temp_home: Path):
        s = self._fleet(temp_home)
        before = (s._get_sequence_data() or {})["lastUpdated"]
        s.set_account_order("1", 3)
        assert (s._get_sequence_data() or {})["lastUpdated"] != before

    @pytest.mark.parametrize("bad", [0, -1, 1000, 1.5, "abc", "", True, [], object()])
    def test_invalid_value_raises_before_the_file_is_opened(self, temp_home: Path, bad):
        """AC-15 — validation precedes resolution *and* the write, so a
        rejected value leaves `sequence.json` byte-identical."""
        s = self._fleet(temp_home)
        before = s.sequence_file.read_bytes()
        with pytest.raises(ValueError):
            s.set_account_order("1", bad)
        assert s.sequence_file.read_bytes() == before

    def test_invalid_value_is_rejected_even_for_an_unknown_account(self, temp_home: Path):
        """Validation is first, so the *value* error wins over the identifier
        error — the same ordering `set_account_threshold` uses."""
        s = self._fleet(temp_home)
        with pytest.raises(ValueError):
            s.set_account_order("99", 0)

    def test_clearing_an_unset_order_is_a_no_op(self, temp_home: Path):
        s = self._fleet(temp_home)
        before = s.sequence_file.read_bytes()
        s.set_account_order("1", None)
        assert s.sequence_file.read_bytes() == before

    def test_setting_the_same_order_twice_is_a_no_op(self, temp_home: Path):
        s = self._fleet(temp_home)
        s.set_account_order("1", 3)
        before = s.sequence_file.read_bytes()
        s.set_account_order("1", 3)
        assert s.sequence_file.read_bytes() == before


class TestByteIdenticalAfterSetThenUnset(OrderStoreBase):
    """AC-11 — the headline compatibility property, asserted on bytes.

    A dict comparison would pass even if the writer left `"order": null` behind
    or reordered keys. The claim in the PR body is that a fleet which set and
    then cleared an order has a file indistinguishable from one that never
    touched the feature, and only a byte comparison tests that claim.
    """

    def test_set_then_unset_restores_the_exact_bytes(self, temp_home: Path):
        s = self._fleet(temp_home)
        before = s.sequence_file.read_bytes()

        s.set_account_order("1", 3)
        assert s.sequence_file.read_bytes() != before, "the set must actually write"

        s.set_account_order("1", None)
        after = s.sequence_file.read_bytes()

        # `lastUpdated` is restamped by both writes, so it is normalised out —
        # it is the *record* that must be byte-restored, not the timestamp.
        import re
        norm = lambda b: re.sub(rb'"lastUpdated":\s*"[^"]*"', b'"lastUpdated":""', b)
        assert norm(after) == norm(before)

    def test_no_order_key_survives_anywhere_in_the_file(self, temp_home: Path):
        s = self._fleet(temp_home)
        s.set_account_order("1", 3)
        s.set_account_order("2", 7)
        s.set_account_order("1", None)
        s.set_account_order("2", None)
        assert b'"order"' not in s.sequence_file.read_bytes()

    def test_an_untouched_fleet_has_no_order_key(self, temp_home: Path):
        s = self._fleet(temp_home)
        assert b'"order"' not in s.sequence_file.read_bytes()


class TestAccountOrders(OrderStoreBase):
    """AC-12, AC-16 — the accessor."""

    def test_unpinned_fleet_returns_empty(self, temp_home: Path):
        s = self._fleet(temp_home)
        assert s.account_orders() == {}

    def test_returns_only_explicitly_pinned_accounts(self, temp_home: Path):
        s = self._fleet(temp_home, count=3)
        s.set_account_order("2", 1)
        assert s.account_orders() == {"2": 1}

    def test_keys_are_str_values_are_int(self, temp_home: Path):
        s = self._fleet(temp_home)
        s.set_account_order("2", 1)
        (key, value), = s.account_orders().items()
        assert type(key) is str and type(value) is int

    def test_returns_in_sequence_order(self, temp_home: Path):
        s = self._fleet(temp_home, count=3)
        s.set_account_order("3", 1)
        s.set_account_order("1", 2)
        assert list(s.account_orders()) == ["1", "3"], (
            "keyed by slot in sequence order, like account_policies()"
        )

    def test_duplicate_orders_are_permitted(self, temp_home: Path):
        """AC-16 — equal orders are a *group*, not an error: they tie on the
        leading key element and fall through to the ranking strategy, which is
        litellm's load-balancing-within-an-order-group semantics."""
        s = self._fleet(temp_home, count=4)
        s.set_account_order("2", 1)
        s.set_account_order("4", 1)
        assert s.account_orders() == {"2": 1, "4": 1}

    def test_reads_the_store_once(self, temp_home: Path, monkeypatch):
        """The engine calls this every tick; re-entering the store per account
        would turn one file read into N."""
        s = self._fleet(temp_home, count=3)
        s.set_account_order("1", 1)
        calls = []
        original = s._get_sequence_data
        monkeypatch.setattr(s, "_get_sequence_data",
                            lambda *a, **k: (calls.append(1), original(*a, **k))[1])
        s.account_orders()
        assert len(calls) == 1


class TestOrderSurvivesRenumber(OrderStoreBase):
    """AC-13 — `swap` and `move` carry `order` with the record.

    The decisive argument for storing policy *in the account record* rather
    than a parallel slot-keyed map: a slot-keyed map silently reassigns every
    pin the moment a user renumbers, so account 1's "go first" would become
    account 3's after `cswap swap 1 3`.
    """

    def test_swap_carries_the_order(self, temp_home: Path):
        s = self._fleet(temp_home, count=3)
        s.set_account_order("1", 2)

        s.swap_accounts("1", "3")

        assert self._record(s, "3")["order"] == 2
        assert "order" not in self._record(s, "1"), "the pin was left behind on slot 1"

    def test_swap_does_not_invent_an_order_on_the_other_slot(self, temp_home: Path):
        s = self._fleet(temp_home, count=3)
        s.set_account_order("1", 2)
        s.swap_accounts("1", "2")
        assert s.account_orders() == {"2": 2}

    def test_swap_exchanges_two_orders(self, temp_home: Path):
        s = self._fleet(temp_home, count=3)
        s.set_account_order("1", 1)
        s.set_account_order("3", 9)
        s.swap_accounts("1", "3")
        assert s.account_orders() == {"1": 9, "3": 1}

    def test_move_carries_the_order(self, temp_home: Path):
        s = self._fleet(temp_home, count=3)
        s.set_account_order("2", 4)

        s.move_account("2", "5")

        assert self._record(s, "5")["order"] == 4
        assert "2" not in (s._get_sequence_data() or {})["accounts"]

    def test_accessor_agrees_with_the_record_after_a_move(self, temp_home: Path):
        s = self._fleet(temp_home, count=3)
        s.set_account_order("2", 4)
        assert s.account_orders() == {"2": 4}
        s.move_account("2", "5")
        assert s.account_orders() == {"5": 4}

    def test_order_travels_with_threshold_and_backup(self, temp_home: Path):
        """All three policy keys are record-keyed, so all three move together."""
        s = self._fleet(temp_home, count=3)
        s.set_account_threshold("1", 85.0)
        s.set_account_backup("1", True)
        s.set_account_order("1", 2)

        s.swap_accounts("1", "3")

        record = self._record(s, "3")
        assert record["threshold"] == pytest.approx(85.0)
        assert record["backup"] is True
        assert record["order"] == 2


class TestExportOmitsOrder(OrderStoreBase):
    """AC-14 — `order` is machine-local and does not travel.

    `transfer.py:262-275` builds each export entry from an explicit allowlist.
    That allowlist is why this test passes **without `transfer.py` being
    modified** — and it is worth an explicit test precisely because the
    mechanism is absence rather than code.
    """

    def test_export_omits_order(self, temp_home: Path):
        from claude_swap.transfer import export_accounts

        s = self._fleet(temp_home)
        s.set_account_order("1", 2)
        s.set_account_order("2", 1)

        out_file = temp_home / "backup.cswap"
        export_accounts(s, str(out_file))
        envelope = json.loads(out_file.read_text(encoding="utf-8"))

        for entry in envelope["accounts"]:
            assert "order" not in entry, f"export leaked an order: {entry['email']}"

    def test_transfer_module_is_unmodified_for_order(self):
        """The allowlist, not a filter, is the mechanism. If a future change
        turns the export into a record passthrough, this fails loudly."""
        from pathlib import Path as _P
        source = _P("src/claude_swap/transfer.py").read_text(encoding="utf-8")
        assert "order" not in source.split("def export_accounts")[1][:2000], (
            "export must not learn about `order`"
        )
