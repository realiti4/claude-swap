"""Per-account policy CLI verbs (cli.py).

MEU-PAP-05 - AC-33 ... AC-38 of
`.agent/plans/2026-09-02-per-account-threshold-backup/implementation-plan.md`.

`cswap threshold` mirrors `_alias_command` (pre-dispatched before the main
parser, because the main parser's required mutually-exclusive group cannot hold
a positional subcommand). `cswap backup` / `cswap unbackup` mirror
`disable`/`enable`: a `_SUBCOMMAND_FLAGS` entry expanding to
`--backup-account` / `--unbackup-account`, so both the memorable verb and the
long-standing flag spelling work.

The validation contract is AGENTS.md §Boundary Input Contract: an out-of-range
or non-numeric value is rejected **at the write boundary**, before any file is
touched, so a rejected write leaves `sequence.json` byte-identical. AC-38
asserts exactly that with a byte comparison, not a field comparison.

Synthetic fixtures only. The real store at the user home is never read or
written; `tests/test_real_store_guard.py` enforces that boundary and a failure
there is a P0 stop.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import cli
from claude_swap.switcher import ClaudeAccountSwitcher

from tests.test_account_policy_store import PolicyStoreBase


class PolicyCliBase(PolicyStoreBase):
    """`PolicyStoreBase`'s synthetic fleet, driven through the CLI surface."""

    @staticmethod
    def _bytes(s: ClaudeAccountSwitcher) -> bytes:
        """`sequence.json` verbatim - the AC-38 byte-identity oracle."""
        return Path(s.sequence_file).read_bytes()

    @staticmethod
    def _threshold(argv: list[str]) -> None:
        """Drive `cswap threshold ...` at its pre-dispatch entry point."""
        with patch("os.geteuid", return_value=1000, create=True):
            cli._threshold_command(argv)

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


class TestThresholdSet(PolicyCliBase):
    """AC-33 - `cswap threshold 2 85` writes the override and confirms it."""

    def test_it_writes_the_override_and_prints_a_confirmation(
        self, temp_home: Path, capsys
    ):
        self._fleet(temp_home)
        self._threshold(["2", "85"])

        assert self._record(ClaudeAccountSwitcher(), "2")["threshold"] == 85.0
        out = capsys.readouterr().out
        assert "Account-2" in out
        assert "85" in out

    def test_it_stores_a_float_not_the_raw_string(self, temp_home: Path, capsys):
        """The store's contract is a number; a string would survive JSON and
        then fail every numeric comparison in the engine."""
        self._fleet(temp_home)
        self._threshold(["2", "85"])
        assert isinstance(self._record(ClaudeAccountSwitcher(), "2")["threshold"], float)

    def test_a_nonexistent_slot_exits_nonzero_and_writes_nothing(
        self, temp_home: Path, capsys
    ):
        s = self._fleet(temp_home)
        before = self._bytes(s)

        with pytest.raises(SystemExit) as exc:
            self._threshold(["9", "85"])

        assert exc.value.code != 0
        assert self._bytes(s) == before

    def test_the_verb_is_reachable_through_main_dispatch(self, temp_home: Path, capsys):
        """`cswap threshold ...` must be pre-dispatched like `alias`, not fall
        through to the main parser (which would reject the positional)."""
        self._fleet(temp_home)
        self._main(["threshold", "2", "85"])
        assert self._record(ClaudeAccountSwitcher(), "2")["threshold"] == 85.0


class TestThresholdUnset(PolicyCliBase):
    """AC-34 - `--unset` removes the key and returns the account to the global."""

    def test_it_removes_the_key(self, temp_home: Path, capsys):
        self._fleet(temp_home)
        self._threshold(["2", "85"])
        self._threshold(["2", "--unset"])

        assert "threshold" not in self._record(ClaudeAccountSwitcher(), "2")

    def test_the_account_returns_to_the_global_default(self, temp_home: Path, capsys):
        self._fleet(temp_home)
        self._threshold(["2", "85"])
        self._threshold(["2", "--unset"])

        assert ClaudeAccountSwitcher().account_policies()["2"].threshold is None

    def test_unset_without_an_override_is_a_benign_no_op_not_an_error(
        self, temp_home: Path, capsys
    ):
        """Negative half: it must NOT raise SystemExit. An account with no
        override is already in the requested state, so the verb is idempotent."""
        s = self._fleet(temp_home)
        before = self._bytes(s)

        self._threshold(["2", "--unset"])

        assert self._bytes(s) == before
        assert capsys.readouterr().out.strip() != ""

    def test_unset_with_a_value_is_rejected(self, temp_home: Path, capsys):
        """Mirrors `alias --unset`, which refuses to take a NAME."""
        self._fleet(temp_home)
        with pytest.raises(SystemExit) as exc:
            self._threshold(["2", "85", "--unset"])
        assert exc.value.code != 0


class TestThresholdListing(PolicyCliBase):
    """AC-35 - bare `cswap threshold` lists the global default and every override."""

    def test_it_names_the_global_default(self, temp_home: Path, capsys):
        self._fleet(temp_home)
        self._threshold(["2", "85"])
        capsys.readouterr()

        self._threshold([])
        out = capsys.readouterr().out
        assert "90" in out, "the global default must be shown, not only overrides"

    def test_it_lists_every_override(self, temp_home: Path, capsys):
        self._fleet(temp_home)
        self._threshold(["1", "85"])
        self._threshold(["3", "99.9"])
        capsys.readouterr()

        self._threshold([])
        out = capsys.readouterr().out
        assert "85" in out
        assert "99.9" in out

    def test_with_no_overrides_it_says_so_rather_than_printing_an_empty_table(
        self, temp_home: Path, capsys
    ):
        self._fleet(temp_home)
        capsys.readouterr()

        self._threshold([])
        out = capsys.readouterr().out.strip()
        assert out != ""
        assert "90" in out, "the global default is still worth printing"


class TestBackupVerbs(PolicyCliBase):
    """AC-36 - `cswap backup` / `cswap unbackup` set and clear `record["backup"]`."""

    def test_backup_sets_the_flag(self, temp_home: Path, capsys):
        self._fleet(temp_home)
        self._main(["backup", "3"])
        assert self._record(ClaudeAccountSwitcher(), "3")["backup"] is True

    def test_unbackup_clears_the_flag(self, temp_home: Path, capsys):
        self._fleet(temp_home)
        self._main(["backup", "3"])
        self._main(["unbackup", "3"])
        assert "backup" not in self._record(ClaudeAccountSwitcher(), "3")

    def test_the_legacy_flag_spelling_works_too(self, temp_home: Path, capsys):
        """`--backup-account` mirrors `--disable-account`; the memorable verb is
        a `_SUBCOMMAND_FLAGS` rewrite of it, so both must reach the same code."""
        self._fleet(temp_home)
        self._main(["--backup-account", "3"])
        assert self._record(ClaudeAccountSwitcher(), "3")["backup"] is True
        self._main(["--unbackup-account", "3"])
        assert "backup" not in self._record(ClaudeAccountSwitcher(), "3")

    def test_backup_with_no_argument_exits_nonzero(self, temp_home: Path, capsys):
        self._fleet(temp_home)
        with pytest.raises(SystemExit) as exc:
            self._main(["backup"])
        assert exc.value.code != 0

    def test_the_flag_is_registered_in_the_subcommand_table(self):
        """The verb must be a table entry, not a special case - that is what
        keeps token pass-through working the way `disable` does."""
        assert cli._SUBCOMMAND_FLAGS["backup"] == "--backup-account"
        assert cli._SUBCOMMAND_FLAGS["unbackup"] == "--unbackup-account"


class TestIdentifierResolution(PolicyCliBase):
    """AC-37 - both verbs accept a number, an email, or an alias."""

    def test_threshold_accepts_an_email(self, temp_home: Path, capsys):
        self._fleet(temp_home)
        self._threshold(["account-2@example.test", "85"])
        assert self._record(ClaudeAccountSwitcher(), "2")["threshold"] == 85.0

    def test_threshold_accepts_an_alias(self, temp_home: Path, capsys):
        s = self._fleet(temp_home)
        self._alias(s, "2", "dev")
        self._threshold(["dev", "85"])
        assert self._record(ClaudeAccountSwitcher(), "2")["threshold"] == 85.0

    def test_backup_accepts_an_email(self, temp_home: Path, capsys):
        self._fleet(temp_home)
        self._main(["backup", "account-3@example.test"])
        assert self._record(ClaudeAccountSwitcher(), "3")["backup"] is True

    def test_backup_accepts_an_alias(self, temp_home: Path, capsys):
        s = self._fleet(temp_home)
        self._alias(s, "3", "spare")
        self._main(["backup", "spare"])
        assert self._record(ClaudeAccountSwitcher(), "3")["backup"] is True

    def test_an_unresolvable_identifier_exits_nonzero(self, temp_home: Path, capsys):
        s = self._fleet(temp_home)
        before = self._bytes(s)
        with pytest.raises(SystemExit) as exc:
            self._threshold(["nobody@example.test", "85"])
        assert exc.value.code != 0
        assert self._bytes(s) == before

    def test_an_unresolvable_identifier_exits_nonzero_for_backup_too(
        self, temp_home: Path, capsys
    ):
        s = self._fleet(temp_home)
        before = self._bytes(s)
        with pytest.raises(SystemExit) as exc:
            self._main(["backup", "nobody@example.test"])
        assert exc.value.code != 0
        assert self._bytes(s) == before


class TestValidationAtTheWriteBoundary(PolicyCliBase):
    """AC-38 - a rejected value names the range and leaves the store untouched.

    Byte-identity, not field-identity: a write that rejected the value but still
    restamped `lastUpdated` would pass a field check and fail this one, and it
    is the one that matters - `sequence.json` is the user's live account store.
    """

    @pytest.mark.parametrize("bad", ["20", "0", "100", "abc", "", "9e9"])
    def test_a_rejected_value_exits_nonzero_and_writes_nothing(
        self, temp_home: Path, capsys, bad: str
    ):
        s = self._fleet(temp_home)
        before = self._bytes(s)

        with pytest.raises(SystemExit) as exc:
            self._threshold(["2", bad])

        assert exc.value.code != 0
        assert self._bytes(s) == before

    def test_the_message_names_the_permitted_range(self, temp_home: Path, capsys):
        self._fleet(temp_home)
        with pytest.raises(SystemExit):
            self._threshold(["2", "20"])
        captured = capsys.readouterr()
        text = captured.out + captured.err
        assert "50" in text
        assert "99.9" in text

    @pytest.mark.parametrize("good", ["50", "99.9", "85.5"])
    def test_the_boundaries_themselves_are_accepted(
        self, temp_home: Path, capsys, good: str
    ):
        """The negative half. A validator that rejected its own endpoints would
        pass every test above."""
        self._fleet(temp_home)
        self._threshold(["2", good])
        assert self._record(ClaudeAccountSwitcher(), "2")["threshold"] == float(good)
