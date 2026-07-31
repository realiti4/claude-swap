"""Detection of sessions still billing a key cswap has already switched away from.

The pin itself lives in another process's memory and cannot be undone from here
(see ``pinned_sessions``), so the whole value of this module is that it reports
the right sessions: a miss leaks money silently, and a false positive tells
someone to restart work in progress for nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

from claude_swap import pinned_sessions
from claude_swap.paths import get_backup_root
from claude_swap.process_detection import ClaudeSession

KEY = "sk-ant-api03-" + "a1b2c3d4e5" * 4


def _session(pid: int, started_at: int, cwd: str = "/work") -> ClaudeSession:
    return ClaudeSession(
        pid=pid,
        session_id=f"s{pid}",
        cwd=cwd,
        started_at=started_at,
        kind="interactive",
        entrypoint="cli",
    )


def _spells(temp_home: Path) -> list[dict]:
    return json.loads(pinned_sessions.ledger_path().read_text(encoding="utf-8"))["spells"]


class TestLedger:
    def test_open_then_close_records_one_window(self, temp_home: Path):
        get_backup_root().mkdir(parents=True, exist_ok=True)
        pinned_sessions.open_spell("5")
        pinned_sessions.close_spell()
        spells = _spells(temp_home)
        assert len(spells) == 1
        assert spells[0]["account"] == "5"
        assert spells[0]["start"] <= spells[0]["end"]

    def test_reopening_does_not_split_the_window(self, temp_home: Path):
        """A re-activation of the same key must not lose the sessions between.

        Splitting one window into two leaves a gap, and a session that started
        in the gap reads as clean while it is pinned.
        """
        get_backup_root().mkdir(parents=True, exist_ok=True)
        pinned_sessions.open_spell("5")
        pinned_sessions.open_spell("5")
        assert len(_spells(temp_home)) == 1

    def test_unreadable_ledger_is_an_empty_answer(self, temp_home: Path):
        get_backup_root().mkdir(parents=True, exist_ok=True)
        pinned_sessions.ledger_path().write_text("{ not json", encoding="utf-8")
        assert pinned_sessions.find_pinned([_session(1, 1000)]) == []


class TestFindPinned:
    def _closed_spell(self, start: int, end: int, account: str = "5") -> None:
        get_backup_root().mkdir(parents=True, exist_ok=True)
        pinned_sessions.ledger_path().write_text(
            json.dumps({
                "schemaVersion": 1,
                "spells": [{"account": account, "start": start, "end": end}],
            }),
            encoding="utf-8",
        )

    def test_session_started_inside_a_closed_spell_is_pinned(self, temp_home: Path):
        self._closed_spell(1000, 2000)
        found = pinned_sessions.find_pinned([_session(42, 1500)])
        assert [p.session.pid for p in found] == [42]
        assert found[0].account == "5"

    def test_sessions_outside_the_window_are_clean(self, temp_home: Path):
        """Both sides matter: before the key was written, and after it was cleared."""
        self._closed_spell(1000, 2000)
        found = pinned_sessions.find_pinned([_session(1, 999), _session(2, 2001)])
        assert found == []

    def test_open_spell_pins_nothing(self, temp_home: Path):
        """While the key is still active those sessions are correctly on it."""
        get_backup_root().mkdir(parents=True, exist_ok=True)
        pinned_sessions.open_spell("5")
        started = json.loads(
            pinned_sessions.ledger_path().read_text(encoding="utf-8")
        )["spells"][0]["start"]
        assert pinned_sessions.find_pinned([_session(7, started + 1)]) == []

    def test_session_without_a_start_time_is_skipped(self, temp_home: Path):
        """Guessing would tell someone to restart working sessions for nothing."""
        self._closed_spell(1000, 2000)
        assert pinned_sessions.find_pinned([_session(9, 0)]) == []

    def test_no_spells_means_no_work(self, temp_home: Path):
        assert pinned_sessions.find_pinned([_session(1, 1500)]) == []


class TestWarningLines:
    def test_silent_when_nothing_is_pinned(self):
        assert pinned_sessions.warning_lines([]) == []

    def test_names_each_session_and_the_remedy(self, temp_home: Path):
        pinned = [
            pinned_sessions.PinnedSession(_session(11, 1_600_000_000_000, "/a"), "5"),
            pinned_sessions.PinnedSession(_session(12, 1_600_000_100_000, "/b"), "5"),
        ]
        lines = pinned_sessions.warning_lines(pinned)
        body = "\n".join(lines)
        assert "2 running sessions" in lines[0]
        assert "Account-5" in lines[0]
        # The pid is what the reader acts on, so it has to be there.
        assert "pid 11" in body and "pid 12" in body
        assert "/a" in body and "/b" in body
        assert "Restart" in body
