"""Project-local account pins for `cswap run` auto-resolution.

A directory tree can name the account `cswap run` should launch by dropping a
small, committable file at (or above) the working directory — so a repo checkout
always opens under the right Claude account without a global `cswap map` entry:

* ``.claude-account`` — a dedicated file whose first non-blank, non-comment line
  is an account identifier (slot number, alias, or email).
* ``.env`` — a ``CLAUDE_SWAP_ACCOUNT=<identifier>`` assignment (optionally
  ``export``-prefixed), for teams that already keep one and would rather not add
  another dotfile.

Resolution walks from the working directory up to the filesystem root and stops
at the first directory that carries a pin, so a nested folder can override an
ancestor. Within a single directory ``.claude-account`` wins over ``.env``.

This module only *reads the identifier string*; it never imports ``switcher``.
Callers resolve the identifier to a live slot themselves (see
``ClaudeAccountSwitcher.slot_for_identifier``), which keeps the precedence and
the "account no longer exists" messaging in one place in the CLI.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

PIN_FILENAME = ".claude-account"
DOTENV_FILENAME = ".env"
DOTENV_KEY = "CLAUDE_SWAP_ACCOUNT"


@dataclass(frozen=True)
class AccountPin:
    """A resolved pin: the raw account identifier and where it came from."""

    identifier: str
    source: Path
    mechanism: str  # "file" (.claude-account) or "dotenv" (.env)

    def display_source(self) -> str:
        """Human-readable origin for CLI messages."""
        if self.mechanism == "dotenv":
            return f"{DOTENV_KEY} in {self.source}"
        return str(self.source)


def _read_pin_file(path: Path) -> str | None:
    """First non-blank, non-comment line of a ``.claude-account`` file.

    ``#`` starts a comment (whole-line or trailing). Returns ``None`` when the
    file is unreadable or holds no identifier.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if stripped:
            return stripped
    return None


def _read_dotenv_account(path: Path) -> str | None:
    """Value of ``CLAUDE_SWAP_ACCOUNT`` in a ``.env`` file, or ``None``.

    Accepts an optional ``export`` prefix and surrounding single/double quotes.
    Last assignment wins, matching dotenv precedence. Only this one key is read;
    the file is never otherwise interpreted.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    found: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        if not sep or key.strip() != DOTENV_KEY:
            continue
        value = value.strip()
        if value and value[0] in "\"'":
            # Quoted: the value ends at the matching quote; a trailing comment
            # (`="1"  # main`) is outside it and ignored.
            quote = value[0]
            end = value.find(quote, 1)
            value = value[1:end] if end != -1 else value[1:]
        else:
            # Unquoted: a ` # note` trailing comment is not part of the value.
            value = value.split("#", 1)[0].strip()
        if value:
            found = value
    return found


def find_account_pin(start: str | Path) -> AccountPin | None:
    """Nearest project account pin at or above ``start``, or ``None``.

    Walks ``start`` and each parent directory once; the first directory holding
    a usable pin wins (``.claude-account`` before ``.env`` within a directory).
    """
    try:
        current = Path(start).expanduser().resolve()
    except OSError:
        return None
    if current.is_file():
        current = current.parent
    for directory in [current, *current.parents]:
        pin_file = directory / PIN_FILENAME
        if pin_file.is_file():
            identifier = _read_pin_file(pin_file)
            if identifier:
                return AccountPin(identifier, pin_file, "file")
        dotenv = directory / DOTENV_FILENAME
        if dotenv.is_file():
            identifier = _read_dotenv_account(dotenv)
            if identifier:
                return AccountPin(identifier, dotenv, "dotenv")
    return None
