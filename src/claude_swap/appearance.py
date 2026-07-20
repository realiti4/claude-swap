"""Terminal appearance detection and theme resolution.

Determines whether the terminal has a light or dark background by querying it
with OSC 11 (``ESC ] 11 ; ? BEL``) and reading the ``rgb:…`` reply, then
classifying by perceived luminance. Cross-cutting: both the CLI printer and the
TUI resolve their theme through here.

The query MUST happen while this process owns the terminal in cooked mode —
before Textual's input driver starts — or the reply is reissued as keystrokes.
Everything fails safe to ``None`` (→ resolved ``dark``): a terminal that doesn't
answer, a pipe, Windows, or a parse failure never blocks and never errors.
"""

from __future__ import annotations

import os
import re
import select
import sys
import time

_QUERY = b"\x1b]11;?\x07"           # OSC 11, BEL-terminated
_TIMEOUT_S = 0.15
_MAX_REPLY = 64
# The full `ESC ]11;` opener is required so interleaved or echoed input (e.g.
# a shell echoing back a pasted escape sequence) can't be misparsed as a
# background reply just because `]11;rgb:`/`]11;#` appears somewhere in the
# buffer without the ESC that actually starts an OSC sequence.
_RGB = re.compile(rb"\x1b\]11;rgb:([0-9a-fA-F]+)/([0-9a-fA-F]+)/([0-9a-fA-F]+)")
_HEX = re.compile(rb"\x1b\]11;#([0-9a-fA-F]{6})")

# Cache: the terminal background can't change within a process, so query once.
_UNSET = object()
_cache: object | str | None = _UNSET


def _reset_cache() -> None:
    """Test helper: forget any cached detection result."""
    global _cache
    _cache = _UNSET


def _parse_osc11(reply: bytes) -> tuple[float, float, float] | None:
    """Parse an OSC 11 reply into (r, g, b) each normalised to 0..1."""
    m = _RGB.search(reply)
    if m:
        return tuple(
            int(h, 16) / (16 ** len(h) - 1) for h in m.groups()
        )  # type: ignore[return-value]
    m = _HEX.search(reply)
    if m:
        h = m.group(1).decode("ascii")
        return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]
    return None


def _classify(reply: bytes) -> str | None:
    """Light/dark from an OSC 11 reply, or None if unparseable."""
    rgb = _parse_osc11(reply)
    if rgb is None:
        return None
    r, g, b = rgb
    luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b  # BT.709-weighted brightness on gamma-encoded channels (approx.)
    return "light" if luminance > 0.5 else "dark"


def _query_terminal_background() -> bytes | None:
    """Send OSC 11 and read the reply in cooked mode. None on any failure.

    Isolated so tests can substitute a canned reply without a real tty.

    tmux and screen don't give OSC 11 passthrough to the outer terminal by
    default, so a query sent inside one would just wait out the timeout with
    no reply; ``$TMUX``/``$STY`` short-circuit to ``None`` (resolving to
    ``dark``) without probing. Fails safe either way.
    """
    if os.name == "nt":
        return None
    if os.environ.get("TMUX") or os.environ.get("STY"):
        return None
    try:
        import termios
        import tty
    except ImportError:
        return None
    try:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return None
    except (ValueError, OSError):
        # isatty() can raise on a closed stream.
        return None
    try:
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
    except (termios.error, OSError):
        return None
    try:
        # TCSANOW (not TCSADRAIN): draining first can block under terminal
        # flow control, and there's no pending output to drain anyway.
        tty.setcbreak(fd, termios.TCSANOW)
        sys.stdout.write(_QUERY.decode("latin-1"))
        sys.stdout.flush()
        deadline = time.monotonic() + _TIMEOUT_S
        buf = b""
        while time.monotonic() < deadline and len(buf) < _MAX_REPLY:
            remaining = deadline - time.monotonic()
            ready, _, _ = select.select([fd], [], [], max(0.0, remaining))
            if not ready:
                break
            chunk = os.read(fd, 32)
            if not chunk:
                break
            buf += chunk
            # Only stop early once the buffer holds a complete, parseable
            # OSC-11 frame — a BEL/ST terminator alone could belong to an
            # unrelated echoed sequence (e.g. our own query bouncing back)
            # and end the read before the real reply arrives.
            if (b"\x07" in buf or b"\x1b\\" in buf) and _parse_osc11(buf) is not None:
                break
        return buf or None
    except (termios.error, OSError, ValueError):
        return None
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSANOW, old)
        except (termios.error, OSError):
            pass


def detect_terminal_background() -> str | None:
    """'light' | 'dark' from the terminal background, or None if undetectable.

    Cached per process. MUST be first called in cooked mode (before app.run()).
    """
    global _cache
    if _cache is _UNSET:
        reply = _query_terminal_background()
        _cache = _classify(reply) if reply is not None else None
    return _cache  # type: ignore[return-value]


def resolve_theme(setting: str, detect=detect_terminal_background) -> str:
    """Resolve a ui.theme setting to a concrete 'light'/'dark'.

    'dark'/'light' pass through without probing; 'auto' follows ``detect()``,
    falling back to 'dark' when detection yields None.
    """
    if setting in ("dark", "light"):
        return setting
    return detect() or "dark"


def cli_should_probe(argv: list[str], *, colors_enabled: bool) -> bool:
    """Whether the CLI should probe the terminal background before dispatch.

    False when colors are off (nothing will render the theme anyway), when
    the first token is ``run`` (execs a child that takes over the terminal),
    or when ``--json`` is present (the OSC query must never precede
    machine-readable output on stdout).
    """
    if not colors_enabled:
        return False
    if argv and argv[0] == "run":
        return False
    if "--json" in argv:
        return False
    return True


def cli_theme(setting: str, *, detect=detect_terminal_background, colors: bool) -> str:
    """Resolve a theme for a plain-CLI invocation: probe only when color will
    actually be emitted; otherwise auto degrades to dark without a tty query."""
    if setting == "auto" and not colors:
        return "dark"
    return resolve_theme(setting, detect=detect)


def drain_stdin() -> None:
    """Discard any pending terminal input (e.g. a late OSC reply) so it isn't
    reissued as keystrokes once Textual takes over. Best-effort; POSIX only.

    A reply that arrives after the detection deadline and after this drain
    can, in principle, still reach the running app as stray keystrokes —
    inherent to any finite-timeout OSC probe, not fully closeable here.
    """
    if os.name == "nt":
        return
    try:
        import termios
    except ImportError:
        return
    try:
        if not sys.stdin.isatty():
            return
    except (ValueError, OSError):
        return
    try:
        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except (termios.error, OSError):
        pass
