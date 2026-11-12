"""A `standby` key left in `sequence.json` by an earlier build is inert.

PR #318 once shipped a per-account `standby` flag; the cut that narrowed it
to per-account thresholds removed the flag, the ranking pass that honoured it
and the verb that wrote it. Anyone who ran the earlier build still carries
`"standby": true` in their store, so this file pins what that leftover key
does now: nothing. The ranking ignores it, the policy reader ignores it, an
unrelated write preserves it (the store is hand-editable JSON and tolerates
unknown keys, exactly as `_disabled_from_data` does), `--list --json` does not
surface it, and `cswap standby` is no longer a command.

This is the only test file that names the key, because it has to inject it.

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
from claude_swap.autoswitch import TickOutcome
from claude_swap.models import AccountPolicy
from claude_swap.switcher import ClaudeAccountSwitcher

from tests.test_account_policy_store import PolicyStoreBase
from tests.test_autoswitch import EngineHarness, _usage

_EMAILS = {1: "a@example.com", 2: "b@example.com", 3: "c@example.com"}


def _write_stray_key(s: ClaudeAccountSwitcher, num: str) -> None:
    """Write `"standby": true` straight into the record, as the old build did."""
    data = s._get_sequence_data() or {}
    data["accounts"][num]["standby"] = True
    s._write_json(s.sequence_file, data)


class TestTheRankingIgnoresTheStrayKey:
    """The marked account is ranked like any other."""

    def test_the_marked_account_with_the_most_headroom_is_picked(self, temp_home):
        h = EngineHarness(temp_home)
        for num in (1, 2, 3):
            h.seed(num, _EMAILS[num])
        h.make_live(_EMAILS[1], 1)
        _write_stray_key(h.switcher, "2")
        h.engine = h._make_engine()

        outcome = h.tick_with_usage(
            {"1": _usage(95.0), "2": _usage(20.0), "3": _usage(40.0)}
        )

        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2


class TestThePolicyReaderIgnoresTheStrayKey(PolicyStoreBase):
    """The record loads to the default policy without raising."""

    def test_the_record_reads_as_the_default_policy(self, temp_home: Path):
        s = self._fleet(temp_home)
        _write_stray_key(s, "1")
        reloaded = s._get_sequence_data() or {}
        assert s._policy_from_data(reloaded, "1") == AccountPolicy(threshold=None)

    def test_the_policy_has_no_such_field(self):
        assert not hasattr(AccountPolicy(), "standby")


class TestAnUnrelatedWriteKeepsTheStrayKey(PolicyStoreBase):
    """Setting a threshold does not strip keys it does not own."""

    def test_setting_a_threshold_preserves_the_key(self, temp_home: Path):
        s = self._fleet(temp_home)
        _write_stray_key(s, "1")
        s.set_account_threshold("1", 70.0)
        record = self._record(ClaudeAccountSwitcher(), "1")
        assert record["standby"] is True
        assert record["threshold"] == pytest.approx(70.0)


class TestTheJsonRowDoesNotSurfaceTheStrayKey(PolicyStoreBase):
    """`--list --json`, offline (`fetch=set()`; see
    `test_account_policy_store.py`'s JSON class docstring for why)."""

    def test_the_row_has_no_such_key_and_keeps_its_threshold(self, temp_home: Path):
        s = self._fleet(temp_home)
        s.set_account_threshold("1", 60.0)
        _write_stray_key(s, "1")
        payload = s.list_accounts(json_output=True, fetch=set())
        row = next(r for r in payload["accounts"] if r["number"] == 1)
        assert "standby" not in row
        assert row["threshold"] == pytest.approx(60.0)


class TestTheVerbIsGone(PolicyStoreBase):
    """`cswap standby 1` is not a command and writes nothing."""

    def test_the_verb_is_not_in_the_subcommand_map(self):
        assert "standby" not in cli._SUBCOMMAND_FLAGS

    def test_driving_it_through_main_fails_and_writes_nothing(self, temp_home: Path):
        self._fleet(temp_home)
        with patch.object(sys, "argv", ["claude-swap", "standby", "1"]), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch("claude_swap.update_check.check_for_update", return_value=None), \
             pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code not in (0, None)
        records = (ClaudeAccountSwitcher()._get_sequence_data() or {})["accounts"]
        assert all("standby" not in r for r in records.values())
