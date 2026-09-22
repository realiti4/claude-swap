"""Existing sessions/ scanners vs managed auto-* profiles (Refs #382)."""

from __future__ import annotations

import json
import os
import sys

import pytest

from claude_swap.exceptions import SwitchError
from claude_swap.managed_sessions import (
    AccountRef,
    ManagedSessionRegistry,
    create_managed_profile,
    is_managed_session_id,
)
from claude_swap.session import session_dir_for

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="managed sessions are POSIX-only (v1)"
)


def _live_managed_profile_for_b(switcher):
    """A real, live managed profile for account "2" (b@example.com, org-2):
    a registered, running reservation with credentials and Claude's own
    live-PID record, so a scanner that decoded names under sessions/ and
    matched by identity would find it."""
    registry = ManagedSessionRegistry(switcher.backup_dir)
    registry.allocate(
        "auto-0000beef", lambda busy: (AccountRef("b@example.com", "org-2"), "backup"),
        pid=os.getpid(), proc_start=None,
    )
    d = registry.session_dir("auto-0000beef")
    create_managed_profile(d)
    (d / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "at-managed"}}))
    (d / "sessions").mkdir()
    (d / "sessions" / f"{os.getpid()}.json").write_text(json.dumps({"pid": os.getpid()}))
    return d


def test_live_session_pids_ignore_managed_profiles(managed_switcher):
    _live_managed_profile_for_b(managed_switcher)
    # Managed sessions own no lineage, so they must not look like a live
    # `cswap run 2` (which would make the engine skip freshening slot 2).
    assert managed_switcher.live_session_pids_for("2", "b@example.com") == []


def test_adoption_never_reads_managed_profiles(managed_switcher):
    _live_managed_profile_for_b(managed_switcher)
    before = managed_switcher.read_account_credentials("2", "b@example.com")
    assert managed_switcher._adopt_session_credential("2", "b@example.com", "org-2") is False
    assert managed_switcher.read_account_credentials("2", "b@example.com") == before


def test_refuse_session_shell_covers_managed_profiles(managed_switcher, monkeypatch):
    d = _live_managed_profile_for_b(managed_switcher)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(d))
    with pytest.raises(SwitchError):
        managed_switcher._refuse_session_shell()


def test_per_account_profile_names_never_look_managed(managed_switcher):
    per_account = session_dir_for(managed_switcher.backup_dir, "2", "b@example.com")
    assert not is_managed_session_id(per_account.name)


@pytest.mark.parametrize(
    "name, expected",
    [
        ("auto-0000beef", True),
        ("auto-", False),
        ("auto-1234567", False),      # one hex digit short
        ("auto-123456789", False),    # one hex digit over
        ("auto-DEADBEEF", False),     # uppercase hex is not accepted
        ("auto-deadbeef-2", False),   # trailing suffix
        ("auto-deadbeef\n", False),   # trailing newline: `$` matches before it
    ],
)
def test_is_managed_session_id_boundaries(name, expected):
    assert is_managed_session_id(name) is expected
