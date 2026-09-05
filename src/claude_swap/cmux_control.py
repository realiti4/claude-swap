"""Re-arm Claude Code remote control through cmux after an account switch.

Remote control (`/rc`) is bound to the account a session was logged into, so
every switch silently breaks it — each session then shows `/rc failed` until
someone re-runs `/rc` by hand (verified across 12 sessions, 2026-08-27).

cmux can type into the PTY of any surface it hosts, which makes it a delivery
channel with none of the peer socket's failure modes: no dedup, no hold, and
the screen can be read back to confirm. This module joins the two worlds:

  cmux surface (tty)  <->  live Claude Code session (pid -> tty via ps)

and types `/rc` + Enter into every surface hosting a Claude session. Sessions
mid-turn queue the input; it lands when the turn ends.

Deliberate limits (backlog item 6):
- Only surfaces whose tty carries a LIVE Claude session are touched — typing
  `/rc` into a shell just runs a bad command.
- The surface hosting the caller is skipped (never target the sender).
- One `read-screen` per surface at most, and only when verifying — the
  2026-08-27 sweep measured two reads per surface blowing past two minutes.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from claude_swap.process_detection import list_sessions

_logger = logging.getLogger("claude-swap")

CMUX_PATHS = (
    "/Applications/cmux.app/Contents/Resources/bin/cmux",
)
_CMUX_TIMEOUT_S = 15.0


def find_cmux() -> str | None:
    """The cmux CLI binary, or None when cmux isn't installed."""
    for candidate in CMUX_PATHS:
        if os.access(candidate, os.X_OK):
            return candidate
    return None


@dataclass(frozen=True)
class Surface:
    """One cmux terminal surface that carries a tty."""

    ref: str  # "surface:12"
    tty: str  # "ttys006"
    title: str


def _run_cmux(binary: str, args: list[str]) -> str:
    """Run one cmux command; raises on failure like subprocess does."""
    result = subprocess.run(
        [binary, *args],
        capture_output=True,
        text=True,
        timeout=_CMUX_TIMEOUT_S,
        env={**os.environ, "CMUX_QUIET": "1"},
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"cmux {' '.join(args[:2])} failed: {result.stderr.strip()[:200]}"
        )
    return result.stdout


def list_surfaces(binary: str, runner=_run_cmux) -> list[Surface]:
    """Every terminal surface with a tty, across all cmux windows."""
    out = runner(binary, ["tree", "--all", "--json"])
    tree = json.loads(out)
    surfaces: list[Surface] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            if (
                node.get("type") == "terminal"
                and node.get("ref", "").startswith("surface:")
                and node.get("tty")
            ):
                surfaces.append(Surface(
                    ref=node["ref"], tty=node["tty"], title=node.get("title") or ""
                ))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(tree.get("windows", []))
    return surfaces


def _tty_of_pid(pid: int) -> str | None:
    """The controlling tty of a pid ("ttys006"), or None (no tty / no process)."""
    try:
        out = subprocess.run(
            ["ps", "-o", "tty=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5.0,
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out if out and out != "??" else None


def _tty_idle_seconds(tty: str, now: float | None = None) -> float | None:
    """Seconds since the tty last saw input OR output — `w`-style idle.

    atime moves on keystrokes (the session's process reading the tty),
    mtime on output. None when /dev/<tty> cannot be stat'd; callers treat
    that as "unknown", not "idle" (fail-open: sweeping one dead-tty session
    is the pre-filter behavior, silently skipping a live one is a new bug).
    """
    try:
        st = os.stat(f"/dev/{tty}")
    except OSError:
        return None
    return max(0.0, (now or time.time()) - max(st.st_atime, st.st_mtime))


def _own_tty() -> str | None:
    """The caller's controlling tty, to keep the sweep off its own surface."""
    try:
        return os.path.basename(os.ttyname(0))
    except OSError:
        return None


def capture_screen_for_pid(
    pid: int, *, binary: str | None = None, runner=_run_cmux
) -> tuple[str, str] | None:
    """The current screen of the cmux surface hosting ``pid``.

    Returns ``(surface_ref, screen_text)``, or None when cmux is absent, the
    pid has no tty, or no surface carries that tty. Read-only — nothing is
    typed. Exists for 6b's limit-dialog groundwork: the resume flow captures
    what a limit-stopped session actually shows, building the corpus the
    dialog recognizer will be written from (guessing the dialog's markers
    and typing at them blind is how you feed garbage to a live session).
    """
    binary = binary or find_cmux()
    if binary is None:
        return None
    tty = _tty_of_pid(pid)
    if tty is None:
        return None
    try:
        surfaces = list_surfaces(binary, runner)
    except Exception as e:
        _logger.debug("cmux surface listing failed; no screen capture: %r", e)
        return None
    surface = next((s for s in surfaces if s.tty == tty), None)
    if surface is None:
        return None
    try:
        screen = runner(
            binary, ["read-screen", "--surface", surface.ref, "--lines", "40"]
        )
    except Exception as e:
        _logger.debug("read-screen on %s failed: %r", surface.ref, e)
        return None
    return surface.ref, screen


# --- 6b: the verified nudge channel ------------------------------------------
# Screen markers extracted from the Claude Code 2.1.248 binary (the
# rate-limit-options menu, the spend-limit nudge component, and the turn
# spinner). Short fragments on purpose — read-screen text can wrap at the
# terminal width, so a long sentence may not survive as one substring; the
# matcher also collapses whitespace first. The limit-screens/ captures
# (capture_limit_screens) are the drift detector for when an update renames
# these.
_MENU_MARKERS = (
    "Wait for limit to reset",
    "Adjust monthly spend limit",
    "Usage credit balance:",
)
_RUNNING_MARKER = "esc to interrupt"  # a turn is live; touch nothing
# The armed auto-continue banner ("Press esc to cancel the wait") is
# deliberately NOT dismissed: Esc there cancels Claude Code's own wait,
# while typing is an ordinary manual-submit takeover.
_NUDGE_SETTLE_S = 1.0


def _flat(screen: str) -> str:
    return " ".join(screen.split())


def _input_captured(flat: str) -> bool:
    """Is a dialog that EATS typed input on this screen?

    Two known captors: the rate-limit/spend-limit menu (Enter SELECTS an
    option — typing into it blind could pick "upgrade") and our own /rc
    panel (the sweep's queued `/rc` can pop after a mid-turn session
    finally stops). Both close on Esc.
    """
    if any(m in flat for m in _MENU_MARKERS):
        return True
    return "Remote Control" in flat and "Esc to continue" in flat


def nudge_via_cmux(
    pid: int,
    text: str,
    *,
    binary: str | None = None,
    runner=_run_cmux,
    sleeper=time.sleep,
) -> str:
    """Type a resume nudge into the surface hosting ``pid`` — the PTY
    channel, where delivery is a fact on the screen rather than a write
    that merely succeeded (backlog 6b).

    Returns one of:
      * ``"delivered"`` — typed and read back off the screen;
      * ``"typed-unverified"`` — typed, echo not seen (still counts as a
        delivery attempt: the caller must NOT also send a socket message,
        that would queue a duplicate turn);
      * ``"captured-input"`` — a dialog holds the input and would not
        dismiss; nothing was typed;
      * ``"running"`` — the session is mid-turn; nothing was touched;
      * ``"no-surface"`` — cmux absent or the pid isn't hosted in it.
    """
    binary = binary or find_cmux()
    if binary is None:
        return "no-surface"
    got = capture_screen_for_pid(pid, binary=binary, runner=runner)
    if got is None:
        return "no-surface"
    ref, screen = got
    flat = _flat(screen)
    if _RUNNING_MARKER in flat:
        return "running"
    try:
        if _input_captured(flat):
            # Exactly ONE Esc: a second one at a plain prompt starts
            # message-history rewind ("Press esc twice to go up...").
            runner(binary, ["send", "--surface", ref, "--", "\x1b"])
            sleeper(_NUDGE_SETTLE_S)
            flat = _flat(runner(
                binary, ["read-screen", "--surface", ref, "--lines", "40"]
            ))
            if _input_captured(flat):
                return "captured-input"
        runner(binary, ["send", "--surface", ref, "--", text + "\r"])
        sleeper(_NUDGE_SETTLE_S)
        verify = _flat(runner(
            binary, ["read-screen", "--surface", ref, "--lines", "40"]
        ))
    except Exception as e:
        _logger.warning("cmux nudge on %s failed: %r", ref, e)
        return "no-surface"
    if _flat(text) in verify or "[claude-swap]" in verify:
        _logger.info("Nudge typed into %s and read back off the screen", ref)
        return "delivered"
    _logger.info("Nudge typed into %s; echo not visible on read-back", ref)
    return "typed-unverified"


@dataclass(frozen=True)
class SweepResult:
    """What one `/rc` sweep did."""

    sent: list[str]  # surface refs the /rc was typed into
    skipped_self: str | None  # the caller's own surface ref, if it hosted one
    no_surface: int  # live Claude sessions not visible in cmux
    skipped_idle: int = 0  # sessions left alone: tty idle past the threshold
    confirmed: list[str] = field(default_factory=list)  # panel seen + dismissed
    urls: list[str] = field(default_factory=list)  # scraped session URLs


_SESSION_URL_RE = re.compile(r"https://claude\.ai/code/session_[A-Za-z0-9]+")
_CONFIRM_SETTLE_S = 3.0


def _confirm_and_dismiss(
    binary: str, refs: list[str], runner, sleeper=time.sleep
) -> tuple[list[str], list[str]]:
    """Read each swept surface, scrape its session URL, close the panel.

    `/rc` does not just print — it opens an interactive Remote Control panel
    that CAPTURES input until Esc (verified live 2026-08-28). Left open, it
    would eat the next thing typed at the session, the resume nudge included,
    so dismissal is part of the sweep, not an optional nicety. The panel also
    carries the one thing item 7's push wants: the session URL.

    Sessions mid-turn still have the `/rc` queued: nothing on screen to read
    or dismiss yet — they are simply not confirmed (and their panel WILL
    appear later, un-dismissed; accepted until 6b's full nudge pass).

    The panel test is a STRICT two-marker signature, and the URL scrape is
    gated on it: a busy session's screen can legitimately contain the words
    "Remote Control" or a session URL (test output, docs — this repo's own
    sessions do), and a stray ESC into a busy Claude session interrupts its
    running turn. Never ESC, never scrape, on anything but the real panel.
    """
    sleeper(_CONFIRM_SETTLE_S)
    confirmed: list[str] = []
    urls: list[str] = []
    for ref in refs:
        try:
            screen = runner(binary, ["read-screen", "--surface", ref, "--lines", "30"])
        except Exception as e:
            _logger.warning("read-screen %s failed: %r", ref, e)
            continue
        if not ("Remote Control" in screen and "Esc to continue" in screen):
            continue
        confirmed.append(ref)
        for url in _SESSION_URL_RE.findall(screen):
            if url not in urls:
                urls.append(url)
        try:
            # A raw ESC byte: cmux send forwards text verbatim (its only
            # escapes are \r/\n/\t), and Esc closes the panel.
            runner(binary, ["send", "--surface", ref, "--", "\x1b"])
        except Exception as e:
            _logger.warning("panel dismiss on %s failed: %r", ref, e)
    return confirmed, urls


def rearm_remote_control(
    claude_dir: Path | None = None,
    *,
    binary: str | None = None,
    runner=_run_cmux,
    confirm: bool = False,
    sleeper=time.sleep,
    active_within_s: float = 0.0,
) -> SweepResult | None:
    """Type `/rc` + Enter into every cmux surface hosting a live Claude session.

    ``active_within_s`` > 0 restricts the sweep to sessions whose tty saw
    input or output that recently — an abandoned session would otherwise
    collect one `/rc` per switch as junk in its scrollback (reported
    2026-08-29). 0 sweeps every session, the original behavior.

    Returns None when cmux is absent (not an error — most machines don't run
    it), otherwise a :class:`SweepResult`. Never raises: a broken sweep must
    not take down the switch that triggered it.

    With ``confirm=True``, a second pass reads each swept surface back:
    scrapes the Remote Control panel's session URL and dismisses the panel
    (see :func:`_confirm_and_dismiss` for why that is not optional). Both
    real callers (engine and CLI switch paths) pass it; the default stays
    False so the pure sweep remains available.
    """
    binary = binary or find_cmux()
    if binary is None:
        return None
    try:
        surfaces = list_surfaces(binary, runner)
    except Exception as e:
        _logger.warning("cmux surface listing failed; /rc sweep skipped: %r", e)
        return None

    by_tty = {s.tty: s for s in surfaces}
    own = _own_tty()
    sent: list[str] = []
    skipped_self: str | None = None
    no_surface = 0
    skipped_idle = 0
    for session in list_sessions(claude_dir):
        tty = _tty_of_pid(session.pid)
        if tty is None or tty not in by_tty:
            no_surface += 1
            continue
        surface = by_tty[tty]
        if own is not None and tty == own:
            skipped_self = surface.ref
            continue
        if active_within_s > 0:
            idle = _tty_idle_seconds(tty)
            if idle is not None and idle > active_within_s:
                skipped_idle += 1
                continue
        try:
            # \r sends Enter (cmux send's escape handling). A session mid-turn
            # queues the input; /rc expands to /remote-control on arrival.
            runner(binary, ["send", "--surface", surface.ref, "--", "/rc\r"])
            sent.append(surface.ref)
        except Exception as e:
            _logger.warning("/rc into %s failed: %r", surface.ref, e)
    if sent:
        _logger.info(
            "Re-armed remote control on %d session(s) via cmux: %s",
            len(sent), ", ".join(sent),
        )
    confirmed: list[str] = []
    urls: list[str] = []
    if confirm and sent:
        confirmed, urls = _confirm_and_dismiss(binary, sent, runner, sleeper)
    return SweepResult(
        sent=sent, skipped_self=skipped_self, no_surface=no_surface,
        skipped_idle=skipped_idle, confirmed=confirmed, urls=urls,
    )
