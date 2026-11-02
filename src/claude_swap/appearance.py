"""Terminal appearance detection and theme resolution.

Determines whether the terminal has a light or dark background by querying it
with OSC 11 (``ESC ] 11 ; ? BEL``) followed by DA1 (``ESC [ c``), reading
through the ordered DA1 reply, and classifying the preceding ``rgb:…`` reply by
perceived luminance. Cross-cutting: both the CLI printer and the TUI resolve
their theme through here.

The query MUST happen while this process owns the terminal in cooked mode —
before Textual's input driver starts — or the reply is reissued as keystrokes.
Everything fails safe to ``None`` (→ resolved ``dark``): a terminal that doesn't
answer, a pipe, Windows, or a parse failure never blocks indefinitely and never
errors.
"""

from __future__ import annotations

import os
import re
import select
import sys
import time

_QUERY = b"\x1b]11;?\x07"  # OSC 11, BEL-terminated
_DA1_QUERY = b"\x1b[c"      # ordered response boundary
# DA1 lets unsupported terminals return promptly; the cap mainly covers SSH
# latency or a non-conforming terminal that answers neither query.
_TIMEOUT_S = 1.0
_MAX_REPLY = 256
# The full `ESC ]11;` opener is required so interleaved or echoed input (e.g.
# a shell echoing back a pasted escape sequence) can't be misparsed as a
# background reply just because `]11;rgb:`/`]11;#` appears somewhere in the
# buffer without the ESC that actually starts an OSC sequence.
_RGB = re.compile(
    rb"\x1b\]11;rgb:([0-9a-fA-F]+)/([0-9a-fA-F]+)/([0-9a-fA-F]+)"
    rb"(?:\x07|\x1b\\)"
)
_HEX = re.compile(rb"\x1b\]11;#([0-9a-fA-F]{6})(?:\x07|\x1b\\)")
# A primary device-attributes reply is CSI ? Ps c. Requiring ``?`` and at
# least the private marker keeps an echoed DA1 query (CSI c) from looking
# complete; Ps itself may legally be empty.
_DA1_REPLY = re.compile(rb"(?:\x1b\[|\x9b)\?[0-9;:]*c")

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
    """Send OSC 11 + DA1 and read through the DA1 reply. None on any failure.

    Isolated so tests can substitute a canned reply without a real tty.

    Terminals process queries in order. DA1 is widely supported, so its reply
    marks the point after any OSC 11 reply and prevents a slower colour reply
    from being reissued as shell input after this process restores the tty.
    The timeout remains a safety cap for high-latency or non-conforming
    terminals.

    ``TERM=dumb`` and the Linux console don't support this colour query.
    Likewise, tmux and screen don't pass it through to the outer terminal by
    default. Those environments short-circuit to ``None`` (resolving to
    ``dark``) without probing. Fails safe either way.
    """
    if os.name == "nt":
        return None
    if os.environ.get("TERM") in ("dumb", "linux"):
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
        sys.stdout.write((_QUERY + _DA1_QUERY).decode("latin-1"))
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
            # Do not stop at the OSC reply: the DA1 response is the ordered
            # boundary proving that no colour-query bytes are still in flight.
            if _DA1_REPLY.search(buf) is not None:
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
