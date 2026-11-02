"""Tests for the `cswap resume` subcommand (install/uninstall/status/now and
the internal arm/wait entry points), against an isolated home."""

from __future__ import annotations

import io
import json
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from claude_swap import cli
from claude_swap import resume as rs
from claude_swap.paths import get_backup_root
from tests.test_transcript import limit_stop, write

SID = "41fad98a-27b8-499d-bf23-4588962b72a3"


def _run(argv: list[str], capsys, stdin: str | None = None) -> tuple[int, str, str]:
    """Run `cswap resume <argv>`; returns (exit_code, stdout, stderr)."""
    code = 0
    with ExitStack() as stack:
        stack.enter_context(patch("os.geteuid", return_value=1000, create=True))
        stack.enter_context(patch.object(sys, "argv", ["claude-swap", "resume", *argv]))
        if stdin is not None:
            stack.enter_context(patch.object(sys, "stdin", io.StringIO(stdin)))
        try:
            cli.main()
        except SystemExit as e:
            code = e.code or 0
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _claude_settings(home: Path) -> Path:
    return home / ".claude" / "settings.json"


class TestInstallUninstall:
    def test_install_then_status_then_uninstall(self, temp_home, capsys):
        settings = _claude_settings(temp_home)
        settings.write_text(json.dumps({"model": "opus"}))

        code, out, _ = _run(["install", "--command", "cswap resume arm"], capsys)
        assert code == 0
        assert "Installed the StopFailure hook" in out
        data = json.loads(settings.read_text())
        assert data["model"] == "opus"
        assert data["hooks"]["StopFailure"][0]["hooks"][0]["command"] == "cswap resume arm"
        # The pre-install file was snapshotted into the backup dir.
        snaps = list((get_backup_root() / "resume" / "snapshots").glob("claude-settings.*.json"))
        assert len(snaps) == 1 and json.loads(snaps[0].read_text()) == {"model": "opus"}

        code, out, _ = _run(["status"], capsys)
        assert code == 0 and "Auto-continue: on" in out

        code, out, _ = _run(["status", "--json"], capsys)
        payload = json.loads(out)
        assert payload["hookInstalled"] is True and payload["sessions"] == []

        code, out, _ = _run(["uninstall"], capsys)
        assert code == 0 and "Removed" in out
        assert json.loads(settings.read_text()) == {"model": "opus"}

    def test_status_when_off(self, temp_home, capsys):
        code, out, _ = _run([], capsys)
        assert code == 0
        assert "Auto-continue: off" in out


class TestNow:
    def test_nothing_to_continue_exits_nonzero(self, temp_home, capsys):
        code, out, _ = _run(["now", "--all"], capsys)
        assert code == 1
        assert "No running session" in out

    def test_requires_a_target(self, temp_home, capsys):
        code, _, err = _run(["now"], capsys)
        assert code == 2 and "SESSION or pass --all" in err


class TestHookEntryPoints:
    def test_arm_reads_the_hook_event_from_stdin(self, temp_home, tmp_path, capsys):
        transcript = write(tmp_path / "t.jsonl", limit_stop("s1", 1000.0))
        event = {"session_id": SID, "transcript_path": str(transcript), "error": "rate_limit"}
        spawned: list[str] = []
        with patch.object(rs, "spawn_waiter", side_effect=spawned.append):
            code, out, err = _run(["arm"], capsys, stdin=json.dumps(event))
        assert code == 0 and out == "" and err == ""  # a hook stays silent
        assert spawned == [SID]
        assert rs.load_record(get_backup_root(), SID).stall_uuid == "s1"

    def test_arm_never_fails_on_garbage(self, temp_home, capsys):
        code, out, err = _run(["arm"], capsys, stdin="{nope")
        assert (code, out, err) == (0, "", "")

    def test_wait_with_an_invalid_id_is_a_quiet_no_op(self, temp_home, capsys):
        code, out, err = _run(["wait", "../../x"], capsys)
        assert (code, out, err) == (0, "", "")


class TestRehearsalCommand:
    def test_outside_a_session_explains_how_to_run_it(self, temp_home, capsys, monkeypatch):
        monkeypatch.delenv("CLAUDE_CODE_MESSAGING_SOCKET", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_MESSAGING_TOKEN", raising=False)
        code, out, err = _run(["test"], capsys)
        assert code == 1
        assert "inside a Claude Code session" in (out + err)
