"""Tests for `cswap reorder` (ClaudeAccountSwitcher.reorder_accounts)."""

from pathlib import Path

import pytest

from claude_swap.exceptions import (
    AccountNotFoundError,
    ConfigError,
    ValidationError,
)
from claude_swap.switcher import ClaudeAccountSwitcher


def _data(slots, active=None, aliases=None):
    """A sequence.json with the given slot numbers (sparse allowed)."""
    accounts = {
        str(n): {
            "email": f"acct{n}@example.com",
            "uuid": f"uuid-{n}",
            "added": "2024-01-01T00:00:00Z",
            **({"alias": aliases[n]} if aliases and n in aliases else {}),
        }
        for n in slots
    }
    return {
        "activeAccountNumber": active if active is not None else slots[0],
        "lastUpdated": "2024-01-01T00:00:00Z",
        "sequence": sorted(slots),
        "accounts": accounts,
    }


def _switcher(data):
    switcher = ClaudeAccountSwitcher()
    switcher._setup_directories()
    switcher._write_json(switcher.sequence_file, data)
    return switcher


def _emails(switcher):
    data = switcher._get_sequence_data()
    return {
        num: acc["email"]
        for num, acc in data["accounts"].items()
    }


class TestReorderAccounts:
    def test_full_rotation(self, temp_home: Path):
        switcher = _switcher(_data([1, 2, 3]))
        rows = switcher.reorder_accounts(["3", "1", "2"])
        assert _emails(switcher) == {
            "1": "acct3@example.com",
            "2": "acct1@example.com",
            "3": "acct2@example.com",
        }
        assert rows == [
            ("1", "acct3@example.com"),
            ("2", "acct1@example.com"),
            ("3", "acct2@example.com"),
        ]

    def test_identity_order_is_a_noop(self, temp_home: Path):
        switcher = _switcher(_data([1, 2, 3]))
        switcher.reorder_accounts(["1", "2", "3"])
        assert _emails(switcher)["1"] == "acct1@example.com"
        assert _emails(switcher)["3"] == "acct3@example.com"

    def test_shift_semantics_drag_5_to_front(self, temp_home: Path):
        # Dragging the last account to position 1 shifts everyone down one —
        # the gesture `move` (swap-on-occupied) cannot express.
        switcher = _switcher(_data([1, 2, 3, 4, 5]))
        switcher.reorder_accounts(["5", "1", "2", "3", "4"])
        assert _emails(switcher) == {
            "1": "acct5@example.com",
            "2": "acct1@example.com",
            "3": "acct2@example.com",
            "4": "acct3@example.com",
            "5": "acct4@example.com",
        }

    def test_sparse_slots_keep_their_gaps(self, temp_home: Path):
        # remove leaves gaps; reorder shifts occupants, never renumbers.
        switcher = _switcher(_data([1, 3, 6]))
        rows = switcher.reorder_accounts(["6", "3", "1"])
        assert [num for num, _ in rows] == ["1", "3", "6"]
        assert _emails(switcher) == {
            "1": "acct6@example.com",
            "3": "acct3@example.com",
            "6": "acct1@example.com",
        }

    def test_active_account_number_follows_its_account(self, temp_home: Path):
        switcher = _switcher(_data([1, 2, 3], active=1))
        switcher.reorder_accounts(["3", "1", "2"])
        data = switcher._get_sequence_data()
        # acct1 was active; it now lives in slot 2.
        assert data["activeAccountNumber"] == 2

    def test_accepts_emails_and_aliases(self, temp_home: Path):
        switcher = _switcher(_data([1, 2], aliases={2: "dev"}))
        switcher.reorder_accounts(["dev", "acct1@example.com"])
        assert _emails(switcher)["1"] == "acct2@example.com"

    def test_rejects_missing_or_duplicate_accounts(self, temp_home: Path):
        switcher = _switcher(_data([1, 2, 3]))
        with pytest.raises(ValidationError):
            switcher.reorder_accounts(["1", "2"])          # not all named
        with pytest.raises(ValidationError):
            switcher.reorder_accounts(["1", "2", "2"])     # duplicate
        with pytest.raises(AccountNotFoundError):
            switcher.reorder_accounts(["1", "2", "9"])     # unknown

    def test_no_accounts_is_an_error(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        with pytest.raises(ConfigError):
            switcher.reorder_accounts(["1"])


class TestReorderCommand:
    def test_json_output(self, temp_home: Path, capsys):
        import json as jsonlib

        from claude_swap.cli import _reorder_command

        _switcher(_data([1, 2]))
        _reorder_command(["2", "1", "--json"])
        got = jsonlib.loads(capsys.readouterr().out)
        assert got["accounts"] == [
            {"number": 1, "email": "acct2@example.com"},
            {"number": 2, "email": "acct1@example.com"},
        ]

    def test_error_exits_nonzero(self, temp_home: Path):
        from claude_swap.cli import _reorder_command

        _switcher(_data([1, 2]))
        with pytest.raises(SystemExit):
            _reorder_command(["1"])
