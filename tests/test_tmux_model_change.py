"""`examples/tmux-model-change.sh`, run for real against a scripted tmux.

The script is what stands between `cswap auto --fallback-model` and a room
full of sessions nobody is watching, so it is tested as it ships: the real
file, under the real bash, with only `tmux` (and the two process-table tools
it consults) replaced by stubs first on ``PATH``. The stub serves a screen per
pane from a file, records every ``send-keys``, and can swap a pane's screen
after the Nth Enter — which is how a *Switch model?* dialog that appears only
AFTER `/model` was typed gets played back.

Every screen below is what Claude Code 2.1.278 actually drew (captured with
``tmux capture-pane -e``), trimmed — including the two shapes that were wrong
the first time round: dim suggestion text that looked like typed input, and a
dialog with empty rows beneath it that a plain ``tail`` saw as nothing at all.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "examples" / "tmux-model-change.sh"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="a bash script driving tmux; neither exists on stock Windows",
)

ESC = "\x1b"
NBSP = " "
RULE = "─" * 40
STATUS = "  [Fable 5.1]  session 27%  |  week 56%  |  Fable 98%"

EMPTY_PROMPT = f"● Done.\n{RULE}\n{ESC}[39m❯{NBSP}\n{RULE}\n{STATUS}\n"
TYPED_PROMPT = f"● Done.\n{RULE}\n{ESC}[39m❯{NBSP}half a thought I have not sent\n{RULE}\n{STATUS}\n"
# Claude Code's own suggestion, drawn dim. Typing replaces it; nobody loses it.
SUGGESTION_PROMPT = (
    f"● Done.\n{RULE}\n{ESC}[39m❯{NBSP}{ESC}[2mYeah,{ESC}[0m{ESC}[39m{ESC}[49m "
    f"{ESC}[2madd{ESC}[0m{ESC}[39m{ESC}[49m {ESC}[2mit{ESC}[0m\n{RULE}\n{STATUS}\n"
)
TRUST_DIALOG = (
    " Quick safety check: Is this a project you created or one you trust?\n"
    " ❯ No, exit\n   Yes, I trust this folder\n Enter to confirm · Esc to cancel\n"
)
NO_PROMPT = "● Reading 3 files…\n\n"
# Empty rows BELOW the dialog, as on a real pane: `tail -n 20` of this is blank.
SWITCH_DIALOG_YES = (
    f"❯ /model opus\n{RULE}\n  Switch model?\n"
    "  Your next response will be slower and use more tokens\n"
    "  ❯ 1. Yes, switch to Opus 5\n    2. No, go back\n" + "\n" * 30
)
SWITCH_DIALOG_NO = SWITCH_DIALOG_YES.replace("  ❯ 1. Yes", "    1. Yes").replace(
    "    2. No", "  ❯ 2. No"
)
ACKNOWLEDGED = (
    "❯ /model opus\n  ⎿  Set model to Opus 5 and saved as your default for new sessions\n"
    f"{RULE}\n{ESC}[39m❯{NBSP}\n{RULE}\n{STATUS}\n"
)
STALLED = (
    "● Working on it.\n  ⎿  You've hit your Fable limit · resets Sep 22, 6pm\n"
    f"{RULE}\n{ESC}[39m❯{NBSP}\n{RULE}\n{STATUS}\n"
)
BUSY_AND_STALLED = STALLED.replace(
    "● Working on it.", "✽ Flambéing… (13s · ↓ 121 tokens)"
)

TMUX_STUB = r"""#!/bin/sh
# Scripted tmux: screens from files, keystrokes to a log.
d="$FAKE_TMUX_DIR"
cmd="$1"; shift
target=""
prev=""
for a in "$@"; do [ "$prev" = "-t" ] && target="$a"; prev="$a"; done
case "$cmd" in
  list-panes) cat "$d/panes" ;;
  capture-pane) cat "$d/screen.$target" 2>/dev/null ;;
  send-keys)
    [ -f "$d/fail-send" ] && exit 1
    printf '%s\n' "$*" >>"$d/sent"
    for last in "$@"; do :; done
    if [ "$last" = "Enter" ]; then
      n=$(( $(cat "$d/enters.$target" 2>/dev/null || echo 0) + 1 ))
      echo "$n" >"$d/enters.$target"
      [ -f "$d/after-enter-$n.$target" ] && cp "$d/after-enter-$n.$target" "$d/screen.$target"
    fi ;;
esac
exit 0
"""


class Tmux:
    """A fake tmux server on disk, and the script run against it."""

    def __init__(self, tmp_path: Path):
        self.dir = tmp_path / "tmux"
        self.dir.mkdir()
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.state = tmp_path / "state"
        self._panes: list[str] = []
        self._stub("tmux", TMUX_STUB)
        # The process table the --model check reads: no children, and whatever
        # command line the test gives the pane process.
        self._stub("pgrep", "#!/bin/sh\nexit 1\n")
        self._stub("ps", '#!/bin/sh\ncat "$FAKE_TMUX_DIR/ps-args" 2>/dev/null\nexit 0\n')
        (self.dir / "panes").write_text("")

    def _stub(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(body)
        path.chmod(0o755)

    def pane(self, pane_id: str, screen: str, *, command: str = "claude",
             after_enter: dict[int, str] | None = None) -> None:
        self._panes.append(f"{pane_id} {4000 + len(self._panes)} {command}")
        (self.dir / "panes").write_text("\n".join(self._panes) + "\n")
        (self.dir / f"screen.{pane_id}").write_text(screen, encoding="utf-8")
        for n, later in (after_enter or {}).items():
            (self.dir / f"after-enter-{n}.{pane_id}").write_text(later, encoding="utf-8")

    def run(self, event: str | None = "fallback", **env: str) -> subprocess.CompletedProcess:
        full_env = {
            "PATH": f"{self.bin}{os.pathsep}/usr/bin{os.pathsep}/bin",
            "HOME": str(self.state),
            "XDG_STATE_HOME": str(self.state),
            "FAKE_TMUX_DIR": str(self.dir),
            "CSWAP_TMUX_TICK_S": "0.05",
            "CSWAP_TMUX_RETRY_S": "0",
            "LC_ALL": "C.UTF-8",
        }
        if event is not None:
            full_env["CSWAP_MODEL_EVENT"] = event
        full_env.update(env)
        result = subprocess.run(
            ["bash", str(SCRIPT)], env=full_env, capture_output=True, text=True,
            timeout=60,
        )
        self._wait_for_retry_loop()
        return result

    def _wait_for_retry_loop(self) -> None:
        # The retry loop is backgrounded on purpose; with RETRY_S=0 it exits at
        # once, but tmp_path must not be torn down under it.
        pidfile = self.state / "cswap-tmux" / "retry.pid"
        deadline = time.monotonic() + 10
        while pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.05)

    @property
    def sent(self) -> list[str]:
        path = self.dir / "sent"
        return path.read_text().splitlines() if path.exists() else []

    @property
    def log(self) -> str:
        path = self.state / "cswap-tmux" / "tmux-model-change.log"
        return path.read_text() if path.exists() else ""


@pytest.fixture
def tmux(tmp_path: Path) -> Tmux:
    return Tmux(tmp_path)


def typed(pane_id: str, text: str) -> list[str]:
    """The two send-keys a line costs: the literal text, then Enter."""
    return [f"-t {pane_id} -l -- {text}", f"-t {pane_id} Enter"]


class TestEvent:
    @pytest.mark.parametrize("event", [None, "", "sideways"])
    def test_anything_but_fallback_or_restored_is_refused(self, tmux, event):
        tmux.pane("%1", EMPTY_PROMPT)
        result = tmux.run(event)
        assert result.returncode == 2
        assert "CSWAP_MODEL_EVENT" in result.stderr
        assert tmux.sent == []

    def test_fallback_types_the_fallback_model(self, tmux):
        tmux.pane("%1", EMPTY_PROMPT, after_enter={1: ACKNOWLEDGED})
        assert tmux.run("fallback").returncode == 0
        assert tmux.sent == typed("%1", "/model opus")
        assert "ok %1: Set model to Opus 5" in tmux.log

    def test_restored_types_the_primary_model(self, tmux):
        tmux.pane("%1", EMPTY_PROMPT)
        tmux.run("restored")
        assert tmux.sent == typed("%1", "/model fable")

    def test_model_ids_come_from_the_environment(self, tmux):
        # `[1m]` and friends: whatever string /model wants, verbatim.
        tmux.pane("%1", EMPTY_PROMPT)
        tmux.run("fallback", CSWAP_TMUX_FALLBACK_ID="claude-opus-5[1m]")
        assert tmux.sent == typed("%1", "/model claude-opus-5[1m]")


class TestWhichPanes:
    def test_only_panes_running_claude(self, tmux):
        tmux.pane("%1", EMPTY_PROMPT, command="zsh")
        tmux.pane("%2", EMPTY_PROMPT, command="nvim")
        tmux.pane("%3", EMPTY_PROMPT)
        tmux.run()
        assert tmux.sent == typed("%3", "/model opus")

    def test_targets_narrow_it_further(self, tmux):
        for pane_id in ("%1", "%2", "%3"):
            tmux.pane(pane_id, EMPTY_PROMPT)
        tmux.run(CSWAP_TMUX_TARGETS="%2")
        assert tmux.sent == typed("%2", "/model opus")

    def test_a_session_started_with_model_is_left_alone(self, tmux):
        # Someone pinned it on purpose. Skipped, not deferred: no retry owed.
        tmux.pane("%1", EMPTY_PROMPT)
        (tmux.dir / "ps-args").write_text(
            "claude --dangerously-skip-permissions --model claude-opus-5[1m] --resume abc\n"
        )
        tmux.run()
        assert tmux.sent == []
        assert "skip %1: started with --model" in tmux.log
        assert "retrying" not in tmux.log

    def test_model_inside_another_flags_value_is_not_a_pin(self, tmux):
        tmux.pane("%1", EMPTY_PROMPT)
        (tmux.dir / "ps-args").write_text("claude --resume fix-the--model-picker\n")
        tmux.run()
        assert tmux.sent == typed("%1", "/model opus")


class TestNeverTypeOverSomeone:
    def test_typed_text_defers_the_pane(self, tmux):
        tmux.pane("%1", TYPED_PROMPT)
        assert tmux.run().returncode == 0
        assert tmux.sent == []
        assert "defer %1: prompt holds text" in tmux.log

    def test_dim_suggestion_text_is_not_typed_text(self, tmux):
        tmux.pane("%1", SUGGESTION_PROMPT)
        tmux.run()
        assert tmux.sent == typed("%1", "/model opus")

    def test_an_open_dialog_defers_the_pane(self, tmux):
        # Its cursor is the prompt glyph too; Enter here would answer "No, exit".
        tmux.pane("%1", TRUST_DIALOG)
        tmux.run()
        assert tmux.sent == []
        assert "defer %1" in tmux.log

    def test_no_prompt_on_screen_defers_the_pane(self, tmux):
        tmux.pane("%1", NO_PROMPT)
        tmux.run()
        assert tmux.sent == []
        assert "defer %1: no prompt on screen" in tmux.log

    def test_one_deferred_pane_does_not_hold_up_the_rest(self, tmux):
        tmux.pane("%1", TYPED_PROMPT)
        tmux.pane("%2", EMPTY_PROMPT)
        tmux.run()
        assert tmux.sent == typed("%2", "/model opus")
        assert "retrying in background: %1" in tmux.log

    def test_a_failed_send_is_deferred_not_lost(self, tmux):
        tmux.pane("%1", EMPTY_PROMPT)
        (tmux.dir / "fail-send").write_text("")
        assert tmux.run().returncode == 0
        assert "defer %1: send-keys failed" in tmux.log

    def test_dry_run_sends_nothing_and_says_what_it_would(self, tmux):
        tmux.pane("%1", EMPTY_PROMPT)
        tmux.run(CSWAP_TMUX_DRY_RUN="1")
        assert tmux.sent == []
        assert "dry-run %1 <- /model opus" in tmux.log


class TestSwitchModelConfirmation:
    """A session with history asks before switching, and waits. Unattended,
    that is forever — the one failure that defeats the whole point."""

    def test_yes_is_confirmed_and_the_acknowledgement_checked(self, tmux):
        tmux.pane("%1", EMPTY_PROMPT, after_enter={1: SWITCH_DIALOG_YES, 2: ACKNOWLEDGED})
        tmux.run()
        assert tmux.sent == [*typed("%1", "/model opus"), "-t %1 Enter"]
        assert "confirmed %1: Switch model? -> Yes" in tmux.log
        assert "ok %1: Set model to Opus 5" in tmux.log

    def test_enter_is_never_pressed_on_no(self, tmux):
        tmux.pane("%1", EMPTY_PROMPT, after_enter={1: SWITCH_DIALOG_NO})
        tmux.run()
        assert tmux.sent == typed("%1", "/model opus")
        assert "WARNING %1" in tmux.log and "Yes is not selected" in tmux.log

    def test_a_switch_nobody_acknowledged_is_flagged(self, tmux):
        tmux.pane("%1", EMPTY_PROMPT)  # screen never changes after the send
        tmux.run()
        assert "WARNING %1: no 'Set model to' acknowledgement seen" in tmux.log


class TestNudge:
    """A session that had already stopped on the limit needs telling to go on;
    one that is merely idle must not be told anything."""

    NUDGE = "You hit a limit and you have recovered, continue"

    def test_a_stalled_session_is_nudged_after_the_switch(self, tmux):
        tmux.pane("%1", STALLED, after_enter={1: ACKNOWLEDGED})
        tmux.run("fallback")
        assert tmux.sent == [*typed("%1", "/model opus"), *typed("%1", self.NUDGE)]
        assert "nudged %1" in tmux.log

    def test_an_idle_session_is_not(self, tmux):
        tmux.pane("%1", EMPTY_PROMPT, after_enter={1: ACKNOWLEDGED})
        tmux.run("fallback")
        assert tmux.sent == typed("%1", "/model opus")

    def test_a_session_still_working_is_not(self, tmux):
        # An old limit banner in view, but the turn is running: leave it be.
        tmux.pane("%1", BUSY_AND_STALLED, after_enter={1: ACKNOWLEDGED})
        tmux.run("fallback")
        assert tmux.sent == typed("%1", "/model opus")

    def test_coming_back_never_nudges(self, tmux):
        tmux.pane("%1", STALLED, after_enter={1: ACKNOWLEDGED})
        tmux.run("restored")
        assert tmux.sent == typed("%1", "/model fable")

    def test_an_empty_nudge_turns_it_off(self, tmux):
        tmux.pane("%1", STALLED, after_enter={1: ACKNOWLEDGED})
        tmux.run("fallback", CSWAP_TMUX_NUDGE="")
        assert tmux.sent == typed("%1", "/model opus")

    def test_the_stall_pattern_is_configurable(self, tmux):
        tmux.pane("%1", STALLED, after_enter={1: ACKNOWLEDGED})
        tmux.run("fallback", CSWAP_TMUX_STALL_REGEX="quota exceeded")
        assert tmux.sent == typed("%1", "/model opus")


class TestRetryLoop:
    def test_a_newer_change_supersedes_an_older_retry_loop(self, tmux):
        # Else "restored" could be followed, minutes later, by a stale loop
        # still typing the fallback model into the panes it had deferred.
        older = subprocess.Popen(["sleep", "60"])
        try:
            pidfile = tmux.state / "cswap-tmux" / "retry.pid"
            pidfile.parent.mkdir(parents=True)
            pidfile.write_text(f"{older.pid}\n")
            tmux.pane("%1", EMPTY_PROMPT)
            tmux.run("restored")
            assert older.wait(timeout=10) != 0  # killed, not run to completion
        finally:
            older.kill()

    def test_giving_up_is_logged_with_the_panes_it_gave_up_on(self, tmux):
        tmux.pane("%1", TYPED_PROMPT)
        tmux.run()  # RETRY_S=0: the loop's deadline has already passed
        assert "gave up on: %1" in tmux.log
