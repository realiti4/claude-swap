"""`cswap order` CLI verb, `--json` rows — MEU-ORD-05 (AC-33 … AC-38).

AC-33  `cswap order N R` sets, `cswap order N --unset` clears, bare
       `cswap order` lists. Passing both a rank and `--unset` is an error, and
       so is an identifier with neither.
AC-34  The long-flag spellings `--order-account` / `--unorder-account` exist and
       write bytes identical to the verb path.
AC-35  The identifier is a number, an email, or an alias.
AC-36  An invalid rank names the range on stderr, exits 1, and leaves
       `sequence.json` byte-unchanged.
AC-37  Bare `cswap order` prints the resolved chain: pinned accounts in rank
       order, then a line saying the rest follow the active strategy.
AC-38  `--json` account rows gain `order` only when set.

**One deviation from AC-34, recorded here rather than left implicit.** AC-34
asks for `_SUBCOMMAND_FLAGS` entries alongside `--order-account`. There must be
none: AC-33 requires a bare `cswap order` to *list*, which forces the verb
through the pre-dispatch block at `cli.py:1068` — and pre-dispatch runs before
`_translate_subcommand`, so an entry would be unreachable. `cli.py:45-49` states
that convention itself ("`run`/`auto` keep their own pre-dispatch parsers, so
none of those are listed here"), and `threshold`, the verb this one mirrors, is
likewise absent from the map. AC-34's *intent* — both spellings work, and both
write the same bytes — is met in full by the long flags on the main parser;
`TestTheSubcommandMapIsDeliberatelyUntouched` pins the exclusion so it reads as
a decision instead of an oversight.

Synthetic fixtures only. The real store at the user home is never read or
written; `tests/test_real_store_guard.py` enforces that boundary and a failure
there is a P0 stop.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import cli
from claude_swap.models import ACCOUNT_ORDER_MAX, ACCOUNT_ORDER_MIN
from claude_swap.switcher import ClaudeAccountSwitcher

from tests.test_account_order_store import OrderStoreBase


class OrderCliBase(OrderStoreBase):
    """`OrderStoreBase`'s synthetic fleet, driven through the CLI surface."""

    @staticmethod
    def _bytes(s: ClaudeAccountSwitcher) -> bytes:
        """`sequence.json` verbatim - the AC-36 byte-identity oracle."""
        return Path(s.sequence_file).read_bytes()

    @staticmethod
    def _order(argv: list[str]) -> None:
        """Drive `cswap order ...` at its pre-dispatch entry point."""
        with patch("os.geteuid", return_value=1000, create=True):
            cli._order_command(argv)

    @staticmethod
    def _main(argv: list[str]) -> None:
        """Drive a full `cswap ...` argv through `main()`'s dispatch."""
        with patch.object(sys, "argv", ["claude-swap", *argv]), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch("claude_swap.update_check.check_for_update", return_value=None):
            cli.main()

    @staticmethod
    def _alias(s: ClaudeAccountSwitcher, num: str, name: str) -> None:
        data = s._get_sequence_data() or {}
        data["accounts"][num]["alias"] = name
        s._write_json(s.sequence_file, data)


class TestOrderSet(OrderCliBase):
    """AC-33, first third - `cswap order 2 5` pins and confirms."""

    def test_it_writes_the_pin_and_prints_a_confirmation(
        self, temp_home: Path, capsys
    ):
        self._fleet(temp_home)
        self._order(["2", "5"])

        assert self._record(ClaudeAccountSwitcher(), "2")["order"] == 5
        out = capsys.readouterr().out
        assert "Account-2" in out
        assert "5" in out

    def test_it_stores_an_int_not_the_raw_string(self, temp_home: Path, capsys):
        self._fleet(temp_home)
        self._order(["2", "5"])
        stored = self._record(ClaudeAccountSwitcher(), "2")["order"]
        assert isinstance(stored, int) and not isinstance(stored, bool)

    def test_the_boundary_values_are_accepted(self, temp_home: Path, capsys):
        self._fleet(temp_home)
        self._order(["1", str(ACCOUNT_ORDER_MIN)])
        self._order(["2", str(ACCOUNT_ORDER_MAX)])
        s = ClaudeAccountSwitcher()
        assert self._record(s, "1")["order"] == ACCOUNT_ORDER_MIN
        assert self._record(s, "2")["order"] == ACCOUNT_ORDER_MAX

    def test_only_the_named_account_is_pinned(self, temp_home: Path, capsys):
        self._fleet(temp_home)
        self._order(["2", "5"])
        s = ClaudeAccountSwitcher()
        assert "order" not in self._record(s, "1")
        assert "order" not in self._record(s, "3")


class TestOrderUnset(OrderCliBase):
    """AC-33, second third - `--unset` pops the key, not writes null."""

    def test_it_removes_the_pin(self, temp_home: Path, capsys):
        s = self._fleet(temp_home)
        s.set_account_order("2", 5)
        self._order(["2", "--unset"])
        assert "order" not in self._record(ClaudeAccountSwitcher(), "2")

    def test_unset_on_an_unpinned_account_is_not_an_error(
        self, temp_home: Path, capsys
    ):
        self._fleet(temp_home)
        self._order(["2", "--unset"])
        assert "order" not in self._record(ClaudeAccountSwitcher(), "2")

    def test_a_cleared_pin_leaves_the_record_as_it_started(
        self, temp_home: Path, capsys
    ):
        """The omit-when-default contract, end to end through the CLI.

        Set then clear must return the *record* to its pre-feature shape. The
        whole file is not compared because `lastUpdated` restamps on write,
        which is correct behaviour rather than a regression.
        """
        s = self._fleet(temp_home)
        before = dict(self._record(s, "2"))
        self._order(["2", "7"])
        self._order(["2", "--unset"])
        assert self._record(ClaudeAccountSwitcher(), "2") == before


class TestOrderArgumentErrors(OrderCliBase):
    """AC-33's negative half - the two impossible argument shapes."""

    def test_a_rank_with_unset_is_rejected(self, temp_home: Path):
        self._fleet(temp_home)
        with pytest.raises(SystemExit) as e:
            self._order(["2", "5", "--unset"])
        assert e.value.code == 2  # argparse.error

    def test_an_identifier_with_neither_is_rejected(self, temp_home: Path):
        self._fleet(temp_home)
        with pytest.raises(SystemExit) as e:
            self._order(["2"])
        assert e.value.code == 2

    def test_unset_without_an_identifier_is_rejected(self, temp_home: Path):
        self._fleet(temp_home)
        with pytest.raises(SystemExit) as e:
            self._order(["--unset"])
        assert e.value.code == 2

    def test_a_rejected_argv_writes_nothing(self, temp_home: Path):
        """argparse exits before the switcher is even constructed."""
        s = self._fleet(temp_home)
        before = self._bytes(s)
        with pytest.raises(SystemExit):
            self._order(["2"])
        assert self._bytes(ClaudeAccountSwitcher()) == before


class TestLongFlagSpellings(OrderCliBase):
    """AC-34 - `--order-account` / `--unorder-account` on the main parser."""

    def test_the_flag_path_pins(self, temp_home: Path, capsys):
        self._fleet(temp_home)
        self._main(["--order-account", "2", "5"])
        assert self._record(ClaudeAccountSwitcher(), "2")["order"] == 5

    def test_the_unflag_path_clears(self, temp_home: Path, capsys):
        s = self._fleet(temp_home)
        s.set_account_order("2", 5)
        self._main(["--unorder-account", "2"])
        assert "order" not in self._record(ClaudeAccountSwitcher(), "2")

    def test_both_paths_write_identical_bytes(self, temp_home: Path, capsys):
        """AC-34's named oracle, and the reason both spellings can coexist.

        `lastUpdated` restamps on every write, so the two stores are compared
        with that one field normalised - it differs for a reason unrelated to
        the feature, and pinning it would make the oracle a clock test.
        """
        def _canonical(raw: bytes) -> dict:
            data = json.loads(raw)
            data["lastUpdated"] = ""
            return data

        s = self._fleet(temp_home)
        self._order(["2", "5"])
        via_verb = _canonical(self._bytes(ClaudeAccountSwitcher()))

        self._order(["2", "--unset"])
        self._main(["--order-account", "2", "5"])
        via_flag = _canonical(self._bytes(ClaudeAccountSwitcher()))

        assert via_verb == via_flag


class TestTheFlagPathFailsLikeTheVerbPath(OrderCliBase):
    """Post-review correction (execution review round 1, out-of-contract obs).

    `set_account_order` raises `ValueError` for a bad rank, but `main()`'s
    dispatch handler catches `ClaudeSwitchError` only, so
    `cswap --order-account 2 0` surfaced an uncaught traceback. It did exit 1
    and did leave the store untouched, so AC-36's letter held - but AC-34 is
    the claim that the two spellings are the *same* command, and a spelling
    that answers bad input with a stack trace is not the same command. The
    verb path already funnels this through `error()` + `sys.exit(1)`.
    """

    BAD = ["0", "1000", "abc"]

    @pytest.mark.parametrize("bad", BAD)
    def test_the_flag_path_exits_1_without_a_traceback(
        self, temp_home: Path, capsys, bad: str
    ):
        self._fleet(temp_home)
        with pytest.raises(SystemExit) as exc:
            self._main(["--order-account", "2", bad])
        assert exc.value.code == 1

    @pytest.mark.parametrize("bad", BAD)
    def test_the_flag_path_names_the_range_on_stderr(
        self, temp_home: Path, capsys, bad: str
    ):
        self._fleet(temp_home)
        with pytest.raises(SystemExit):
            self._main(["--order-account", "2", bad])
        err = capsys.readouterr().err
        assert f"{ACCOUNT_ORDER_MIN}" in err and f"{ACCOUNT_ORDER_MAX}" in err
        assert "Traceback" not in err

    def test_the_rejected_flag_write_leaves_the_store_byte_identical(
        self, temp_home: Path, capsys
    ):
        s = self._fleet(temp_home)
        before = self._bytes(s)
        with pytest.raises(SystemExit):
            self._main(["--order-account", "2", "0"])
        assert self._bytes(ClaudeAccountSwitcher()) == before

    def test_both_spellings_report_the_same_message(
        self, temp_home: Path, capsys
    ):
        """The point of the correction: one command, two spellings."""
        self._fleet(temp_home)
        with pytest.raises(SystemExit):
            self._order(["2", "0"])
        via_verb = capsys.readouterr().err
        with pytest.raises(SystemExit):
            self._main(["--order-account", "2", "0"])
        via_flag = capsys.readouterr().err
        assert via_verb == via_flag


class TestTheSubcommandMapIsDeliberatelyUntouched(OrderCliBase):
    """The AC-34 deviation, pinned so it cannot regress into an oversight.

    See the module docstring: `order` is pre-dispatched, and pre-dispatch runs
    before `_translate_subcommand`, so a `_SUBCOMMAND_FLAGS` entry would be
    dead code. `threshold` - the verb this one mirrors - is absent for exactly
    the same reason.
    """

    def test_order_is_not_in_the_subcommand_map(self):
        assert "order" not in cli._SUBCOMMAND_FLAGS

    def test_neither_is_threshold_the_verb_it_mirrors(self):
        assert "threshold" not in cli._SUBCOMMAND_FLAGS

    def test_backup_still_is_because_it_is_not_pre_dispatched(self):
        """The control: the map is not simply empty of policy verbs."""
        assert cli._SUBCOMMAND_FLAGS["backup"] == "--backup-account"


class TestIdentifierForms(OrderCliBase):
    """AC-35 - number, email, or alias, like every other account verb."""

    def test_by_number(self, temp_home: Path, capsys):
        self._fleet(temp_home)
        self._order(["2", "5"])
        assert self._record(ClaudeAccountSwitcher(), "2")["order"] == 5

    def test_by_email(self, temp_home: Path, capsys):
        self._fleet(temp_home)
        self._order(["account-2@example.test", "5"])
        assert self._record(ClaudeAccountSwitcher(), "2")["order"] == 5

    def test_by_alias(self, temp_home: Path, capsys):
        s = self._fleet(temp_home)
        self._alias(s, "2", "dev")
        self._order(["dev", "5"])
        assert self._record(ClaudeAccountSwitcher(), "2")["order"] == 5

    def test_an_unknown_identifier_exits_naming_it(self, temp_home: Path, capsys):
        self._fleet(temp_home)
        with pytest.raises(SystemExit) as e:
            self._order(["nobody@example.test", "5"])
        assert e.value.code == 1
        assert "nobody@example.test" in capsys.readouterr().err


class TestInvalidRankIsRejectedAtTheWriteBoundary(OrderCliBase):
    """AC-36 - names the range on stderr, exits 1, writes nothing."""

    BAD = ["0", "-1", "1000", "2.5", "abc", "", "1e3"]

    @pytest.mark.parametrize("bad", BAD)
    def test_it_exits_one(self, temp_home: Path, bad: str):
        self._fleet(temp_home)
        with pytest.raises(SystemExit) as e:
            self._order(["2", bad])
        assert e.value.code == 1

    @pytest.mark.parametrize("bad", BAD)
    def test_the_message_names_the_range(self, temp_home: Path, capsys, bad: str):
        self._fleet(temp_home)
        with pytest.raises(SystemExit):
            self._order(["2", bad])
        err = capsys.readouterr().err
        assert str(ACCOUNT_ORDER_MIN) in err and str(ACCOUNT_ORDER_MAX) in err

    @pytest.mark.parametrize("bad", BAD)
    def test_sequence_json_is_byte_unchanged(self, temp_home: Path, bad: str):
        """The AC-36 oracle proper: bytes, not fields.

        A field comparison would pass even if the write happened and was
        rolled back, or if `lastUpdated` restamped. Neither is acceptable -
        the value must never reach the file at all.
        """
        s = self._fleet(temp_home)
        before = self._bytes(s)
        with pytest.raises(SystemExit):
            self._order(["2", bad])
        assert self._bytes(ClaudeAccountSwitcher()) == before

    def test_an_invalid_rank_on_an_unknown_account_still_reports_the_range(
        self, temp_home: Path, capsys
    ):
        """Validation precedes identifier resolution, as in `set_account_order`."""
        self._fleet(temp_home)
        with pytest.raises(SystemExit):
            self._order(["nobody@example.test", "0"])
        err = capsys.readouterr().err
        assert str(ACCOUNT_ORDER_MAX) in err


class TestBareOrderListsTheChain(OrderCliBase):
    """AC-37 - the resolved chain, pinned first, then the strategy remainder."""

    def test_the_empty_state_says_so(self, temp_home: Path, capsys):
        self._fleet(temp_home)
        self._order([])
        assert "No per-account order set" in capsys.readouterr().out

    def test_it_lists_pins_in_rank_order_not_slot_order(
        self, temp_home: Path, capsys
    ):
        s = self._fleet(temp_home)
        s.set_account_order("1", 9)
        s.set_account_order("3", 2)
        capsys.readouterr()
        self._order([])
        out = capsys.readouterr().out
        assert out.index("Account-3") < out.index("Account-1")

    def test_it_prints_each_pinned_rank(self, temp_home: Path, capsys):
        s = self._fleet(temp_home)
        s.set_account_order("2", 7)
        capsys.readouterr()
        self._order([])
        out = capsys.readouterr().out
        assert "Account-2" in out and "7" in out

    def test_unpinned_accounts_are_listed_under_a_strategy_line(
        self, temp_home: Path, capsys
    ):
        s = self._fleet(temp_home)
        s.set_account_order("2", 1)
        capsys.readouterr()
        self._order([])
        out = capsys.readouterr().out
        assert "strategy" in out.lower()
        assert "Account-1" in out and "Account-3" in out

    def test_a_fully_pinned_fleet_prints_no_strategy_remainder(
        self, temp_home: Path, capsys
    ):
        """The line is about accounts, so it must not print with none left."""
        s = self._fleet(temp_home)
        for num, rank in (("1", 1), ("2", 2), ("3", 3)):
            s.set_account_order(num, rank)
        capsys.readouterr()
        self._order([])
        out = capsys.readouterr().out
        assert "No per-account order set" not in out
        assert "strategy" not in out.lower()

    def test_listing_writes_nothing(self, temp_home: Path, capsys):
        s = self._fleet(temp_home)
        s.set_account_order("2", 1)
        before = self._bytes(ClaudeAccountSwitcher())
        capsys.readouterr()
        self._order([])
        assert self._bytes(ClaudeAccountSwitcher()) == before


class TestJsonRowCarriesTheOrder(OrderCliBase):
    """AC-38 - omit-when-default, exactly as `threshold` and `backup` are."""

    def test_a_pinned_row_gains_the_key(self):
        from claude_swap.json_output import account_row
        from claude_swap.models import AccountPolicy

        row = account_row(
            2, "a@example.test", "", "", False, None,
            policy=AccountPolicy(order=5),
        )
        assert row["order"] == 5

    def test_an_unpinned_row_omits_it(self):
        from claude_swap.json_output import account_row

        row = account_row(2, "a@example.test", "", "", False, None)
        assert "order" not in row

    def test_an_unpinned_fleet_emits_rows_identical_to_pr_one(self):
        """AC-38's named oracle: the key is additive, not merely defaulted.

        Compared as a full dict rather than by absence of one key, so a row
        that gained `"order": None` - the other plausible implementation, and
        the one that breaks every consumer keying on the base schema - fails
        here rather than in someone's script.
        """
        from claude_swap.json_output import account_row
        from claude_swap.models import AccountPolicy

        plain = account_row(2, "a@example.test", "", "", False, None)
        explicit_none = account_row(
            2, "a@example.test", "", "", False, None,
            policy=AccountPolicy(order=None),
        )
        assert plain == explicit_none

    def test_it_composes_with_the_pr_one_keys(self):
        from claude_swap.json_output import account_row
        from claude_swap.models import AccountPolicy

        row = account_row(
            2, "a@example.test", "", "", False, None,
            policy=AccountPolicy(threshold=85.0, backup=True, order=3),
        )
        assert (row["threshold"], row["backup"], row["order"]) == (85.0, True, 3)

    def test_the_stored_type_survives_serialisation(self):
        from claude_swap.json_output import account_row
        from claude_swap.models import AccountPolicy

        row = account_row(
            2, "a@example.test", "", "", False, None,
            policy=AccountPolicy(order=5),
        )
        assert json.loads(json.dumps(row))["order"] == 5
