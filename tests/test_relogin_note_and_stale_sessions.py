"""Two claims the code could not support: the "re-login needed" note calling
the ACCOUNT's refresh token dead, and the post-switch "no restart needed".
Evidence is in the commit; each test states the state it drives.
"""
import json
import logging

import pytest

from claude_swap import oauth
from claude_swap.credentials import ActiveCredentials
from claude_swap.process_detection import ClaudeSession
from claude_swap.json_output import USAGE_RELOGIN_REQUIRED
from claude_swap.switcher import SENTINEL_NOTES, ClaudeAccountSwitcher
from claude_swap.usage_store import FetchRecord

OLD = json.dumps({"claudeAiOauth": {"accessToken": "sk-old",
                                    "refreshToken": "rt-old", "expiresAt": 1000}})
NEW = json.dumps({"claudeAiOauth": {"accessToken": "sk-new", "refreshToken": "rt-new",
                                    "expiresAt": 99999999999000}})


def test_the_relogin_note_does_not_condemn_a_live_credential_that_moved_on(
    temp_home, mock_claude_config, sample_sequence_data, monkeypatch,
):
    """The note renders in a state where the account is NOT dead.

    The premise assert is the load-bearing half: it drives the real collector,
    so it fails if the backup-confirms branch ever stops being reachable and
    the wording assert below stops standing on anything.
    """
    sample_sequence_data["accounts"]["2"]["email"] = "b@example.com"
    s = ClaudeAccountSwitcher()
    s._setup_directories()
    s._write_json(s.sequence_file, sample_sequence_data)
    idents = {"2": ("b@example.com", "")}
    # Struck on the generation the BACKUP still holds.
    s._usage_store.record(
        {"2": FetchRecord(error="invalid_grant",
                          struck_fp=oauth.credential_fingerprint(OLD))},
        idents,
    )
    s._write_account_credentials("2", "b@example.com", OLD)
    # The LIVE credential rotated past it — a healthy, non-degraded read.
    monkeypatch.setattr(s, "_read_active_credentials",
                        lambda: ActiveCredentials(NEW, False, False))
    monkeypatch.setattr(s, "_get_current_account", lambda: ("b@example.com", ""))

    entries = s._collect_usage_entries(s._build_accounts_info(), fetch=set())

    assert entries["2"].sentinel == USAGE_RELOGIN_REQUIRED, (
        "PREMISE BROKEN: the backup-confirms branch did not fire, so this test "
        "no longer measures the state the note is wrong about"
    )
    note = SENTINEL_NOTES[USAGE_RELOGIN_REQUIRED]
    assert "refresh token dead" not in note and "stored" in note, (
        f"{note!r} must name the STORED copy: here the live credential "
        "authenticates and only a stored one failed"
    )
    # `no_refresh_token` strikes onto this same sentinel, sends nothing and
    # rotates nothing, so one static note must be true of both errors.
    assert "rotation" not in note and "rejected" not in note, (
        f"{note!r} is false for the no_refresh_token half of this sentinel"
    )
    # The strike supports "the refresh failed" and nothing more.
    assert "if it persists" in note and "cswap add" in note, (
        f"{note!r} must keep the remedy AND keep it conditional: one "
        "invalid_grant on a single-use grant does not require a re-login"
    )


@pytest.mark.parametrize("backend", ["keychain", "file"])
@pytest.mark.parametrize("kinds,named", [
    ([], 0),
    (["bg", "daemon", "daemon-worker"], 0),
    (["interactive", "bg", "bg"], 1),
    (["interactive", ""], 2),
])
def test_switch_followup_names_only_the_sessions_that_can_read_it(
    backend, kinds, named, monkeypatch, capsys,
):
    """Every live session predates a switch that has just committed, so the
    caveat needs no timestamp — only which of them a human could act on. A
    bg/daemon session has no banner to show the symptom, so counting one
    names a population the remedy does not reach.
    The last row is the reason the filter EXCLUDES rather than matches
    "interactive": an unknown kind must still count."""
    s = ClaudeAccountSwitcher.__new__(ClaudeAccountSwitcher)
    monkeypatch.setattr(
        ClaudeAccountSwitcher, "_last_active_credentials_backend", backend,
    )
    sessions = [
        ClaudeSession(pid=0, session_id="", cwd="", started_at=0,
                      kind=k, entrypoint="cli")
        for k in kinds
    ]
    monkeypatch.setattr("claude_swap.switcher.scan_sessions",
                        lambda: (sessions, 0))

    s._print_switch_followup()
    out = capsys.readouterr().out

    plural = "" if named == 1 else "s"
    assert (f"{named} Claude session{plural}" in out) is bool(named), (
        f"backend={backend} kinds={kinds}: got {out!r}"
    )
    assert ("Not logged in" in out) is bool(named), (
        f"the caveat must name the symptom it clears: {out!r}"
    )


def test_a_failed_session_scan_does_not_undo_a_committed_switch(monkeypatch, capsys):
    """A raise here would report work that already succeeded as failed.
    Reachable: `Path.home()` with no HOME, and a lazy glob past the per-file
    handlers."""
    s = ClaudeAccountSwitcher.__new__(ClaudeAccountSwitcher)
    s._logger = logging.getLogger("test-switch-followup")
    monkeypatch.setattr(
        ClaudeAccountSwitcher, "_last_active_credentials_backend", "file",
    )

    def boom():
        raise OSError("sessions directory vanished mid-scan")

    monkeypatch.setattr("claude_swap.switcher.get_running_instances", boom)

    s._print_switch_followup()   # must not raise

    out = capsys.readouterr().out
    assert "no restart needed" in out and "already running" not in out, (
        f"a failed scan must leave the switch reported as done: {out!r}"
    )


def test_an_unreadable_session_record_still_counts(monkeypatch, capsys):
    """`list_sessions` says of itself that it cannot tell "no live sessions"
    from "no readable records", and the caveat's own comment rejects a silent
    zero. A record that could not be parsed is a session that may be showing
    the symptom, so it counts."""
    s = ClaudeAccountSwitcher.__new__(ClaudeAccountSwitcher)
    monkeypatch.setattr(
        ClaudeAccountSwitcher, "_last_active_credentials_backend", "file",
    )
    monkeypatch.setattr("claude_swap.switcher.scan_sessions", lambda: ([], 2))

    s._print_switch_followup()
    out = capsys.readouterr().out

    assert "2 Claude sessions" in out, (
        f"two unreadable session records rendered as nobody running: {out!r}"
    )
