"""Work / personal contexts: which accounts may run in which directories.

Every directory resolves to one context, and every stored account carries one.
A *mode* decides which pairs are allowed:

``open``
    Any account anywhere. Contexts are only labels (shown to the user and to
    Claude at session start).
``asymmetric`` (default)
    A ``work`` directory runs on ``work`` accounts only. Everything else may
    use any account, so personal projects can still spill over onto the work
    licence, but work code never reaches a personal account.
``strict``
    ``work`` directories run on ``work`` accounts, ``personal`` directories on
    ``personal`` accounts. Only ``shared`` directories accept both.

Directory contexts resolve nearest-first, walking up from the directory:

1. an explicit path rule (``cswap context set work ~/work``), or a
   ``.claude-context`` marker file holding ``work``, ``personal`` or
   ``shared`` — whichever sits closest to the directory wins;
2. a git-remote hint (``cswap context remote github.com/acme work``): a
   ``host[/path]`` prefix matched, segment by segment, against the enclosing
   repository's remote URLs, SSH and HTTPS spellings alike;
3. the configured default (``personal`` unless changed).

Accounts nobody tagged count as ``personal``: a work directory never runs on
an account nobody vouched for.

Persisted to ``<backup_dir>/contexts.json``. Stdlib-only on purpose, because
the Claude Code hook (``cswap hook``) reads it on every prompt.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from claude_swap.fsutil import atomic_write_text
from claude_swap.mappings import normalize_path

SCHEMA_VERSION = 1

WORK = "work"
PERSONAL = "personal"
SHARED = "shared"

ACCOUNT_CONTEXTS = (WORK, PERSONAL)
DIRECTORY_CONTEXTS = (WORK, PERSONAL, SHARED)

MODE_OPEN = "open"
MODE_ASYMMETRIC = "asymmetric"
MODE_STRICT = "strict"
MODES = (MODE_OPEN, MODE_ASYMMETRIC, MODE_STRICT)

DEFAULT_MODE = MODE_ASYMMETRIC
DEFAULT_DIRECTORY_CONTEXT = PERSONAL

MARKER_FILE = ".claude-context"


@dataclass(frozen=True)
class Resolution:
    """Where a directory's context came from."""

    context: str
    source: str  # "path", "marker", "remote", "default"
    detail: str  # the rule's path, marker file, remote pattern, or ""

    def describe(self) -> str:
        if self.source == "path":
            return f"path rule {self.detail}"
        if self.source == "marker":
            return f"marker {self.detail}"
        if self.source == "remote":
            return f"git remote matches '{self.detail}'"
        return "default"


@dataclass(frozen=True)
class Verdict:
    """Whether an account may run in a directory, and why not."""

    allowed: bool
    reason: str = ""


def account_key(email: str, org_uuid: str | None) -> str:
    """Stable key for an account: email plus organization (same as mappings)."""
    return f"{email}|{org_uuid or ''}"


def check(directory_context: str, account_context: str | None, mode: str) -> Verdict:
    """Apply ``mode`` to one (directory, account) pair.

    ``account_context`` ``None`` means the account was never tagged; it counts
    as personal.
    """
    acct = account_context or PERSONAL
    if mode == MODE_OPEN or directory_context == SHARED:
        return Verdict(True)
    if directory_context == WORK and acct != WORK:
        return Verdict(
            False,
            "this is a work directory and the active account is not tagged work",
        )
    if mode == MODE_STRICT and directory_context == PERSONAL and acct != PERSONAL:
        return Verdict(
            False,
            "strict mode: this is a personal directory and the active account "
            "is tagged work",
        )
    return Verdict(True)


def read_marker(path: Path) -> str | None:
    """Context named in a ``.claude-context`` file, or None if absent/invalid."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    for line in text.splitlines():
        word = line.split("#", 1)[0].strip().lower()
        if word:
            return word if word in DIRECTORY_CONTEXTS else None
    return None


def _git_config_path(repo_root: Path) -> Path | None:
    """The config file of the repo whose work tree is ``repo_root``.

    Reads files only (no ``git`` subprocess: this runs inside a hook). Handles
    plain repos and worktrees/submodules whose ``.git`` is a ``gitdir:`` file.
    """
    dot_git = repo_root / ".git"
    if dot_git.is_dir():
        return dot_git / "config"
    if not dot_git.is_file():
        return None
    try:
        first = dot_git.read_text(encoding="utf-8").strip().splitlines()[0]
    except (OSError, UnicodeDecodeError, IndexError):
        return None
    if not first.startswith("gitdir:"):
        return None
    gitdir = Path(first[len("gitdir:"):].strip())
    if not gitdir.is_absolute():
        gitdir = (repo_root / gitdir).resolve()
    commondir = gitdir / "commondir"
    if commondir.is_file():
        try:
            common = Path(commondir.read_text(encoding="utf-8").strip())
        except (OSError, UnicodeDecodeError):
            return None
        gitdir = common if common.is_absolute() else (gitdir / common).resolve()
    return gitdir / "config"


def git_remote_urls(repo_root: Path) -> list[str]:
    """Every ``url =`` value in the repo's config (all remotes)."""
    config = _git_config_path(repo_root)
    if config is None:
        return []
    try:
        text = config.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    urls = []
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key.strip().lower() == "url" and value.strip():
            urls.append(value.strip())
    return urls


def normalize_remote(url: str) -> str:
    """``host/path`` form of a git remote, so SSH and HTTPS spellings compare equal.

    ``git@github.com:Acme/x.git``, ``ssh://git@github.com/Acme/x.git`` and
    ``https://github.com/Acme/x`` all become ``github.com/acme/x``. Patterns go
    through the same function, so ``github.com/acme`` matches all three.
    """
    u = url.strip().lower()
    if "://" in u:
        u = u.split("://", 1)[1]
        u = u.split("@", 1)[1] if "@" in u.split("/", 1)[0] else u
    elif ":" in u and "/" not in u.split(":", 1)[0]:
        # scp-like: [user@]host:path
        host, path = u.split(":", 1)
        u = host.split("@", 1)[-1] + "/" + path.lstrip("/")
    if u.endswith(".git"):
        u = u[:-4]
    return u.rstrip("/")


def _remote_matches(prefix: str, url: str) -> bool:
    """Whether ``url`` is ``prefix`` or lies under it, segment-wise.

    ``github.com/acme`` matches ``github.com/acme/x`` but not
    ``github.com/acme-fans/x``; both arguments are already normalized.
    """
    return url == prefix or url.startswith(prefix + "/")


class ContextStore:
    """Reads and writes ``<backup_dir>/contexts.json``."""

    def __init__(self, backup_dir: Path):
        self.path = Path(backup_dir) / "contexts.json"

    # -- persistence -------------------------------------------------------

    def load(self) -> dict:
        """The whole document, normalized (empty sections on missing/corrupt)."""
        data: dict = {}
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    data = raw
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                data = {}
        for section in ("accounts", "paths", "remotes"):
            if not isinstance(data.get(section), dict):
                data[section] = {}
        if data.get("mode") not in MODES:
            data["mode"] = DEFAULT_MODE
        if data.get("default") not in DIRECTORY_CONTEXTS:
            data["default"] = DEFAULT_DIRECTORY_CONTEXT
        return data

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            os.chmod(self.path.parent, 0o700)
        payload = {
            "schemaVersion": SCHEMA_VERSION,
            "mode": data["mode"],
            "default": data["default"],
            "accounts": data["accounts"],
            "paths": data["paths"],
            "remotes": data["remotes"],
        }
        atomic_write_text(self.path, json.dumps(payload, indent=2))

    # -- settings ----------------------------------------------------------

    def mode(self) -> str:
        return self.load()["mode"]

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}, got {mode!r}")
        data = self.load()
        data["mode"] = mode
        self._write(data)

    def default_context(self) -> str:
        return self.load()["default"]

    def set_default_context(self, context: str) -> None:
        _validate(context, DIRECTORY_CONTEXTS)
        data = self.load()
        data["default"] = context
        self._write(data)

    # -- accounts ----------------------------------------------------------

    def account_context(self, email: str, org_uuid: str | None) -> str | None:
        value = self.load()["accounts"].get(account_key(email, org_uuid))
        return value if value in ACCOUNT_CONTEXTS else None

    def set_account_context(
        self, email: str, org_uuid: str | None, context: str | None
    ) -> None:
        """Tag an account; ``None`` removes the tag (back to untagged)."""
        if context is not None:
            _validate(context, ACCOUNT_CONTEXTS)
        data = self.load()
        key = account_key(email, org_uuid)
        if context is None:
            data["accounts"].pop(key, None)
        else:
            data["accounts"][key] = context
        self._write(data)

    # -- directories -------------------------------------------------------

    def set_path(self, path: str | Path, context: str) -> str:
        _validate(context, DIRECTORY_CONTEXTS)
        data = self.load()
        key = normalize_path(path)
        data["paths"][key] = context
        self._write(data)
        return key

    def unset_path(self, path: str | Path) -> bool:
        data = self.load()
        removed = data["paths"].pop(normalize_path(path), None) is not None
        if removed:
            self._write(data)
        return removed

    def set_remote(self, pattern: str, context: str | None) -> None:
        """Map a remote ``host[/path]`` prefix to a context; ``None`` removes it."""
        pattern = normalize_remote(pattern)
        if not pattern:
            raise ValueError("remote pattern must not be empty")
        if context is not None:
            _validate(context, DIRECTORY_CONTEXTS)
        data = self.load()
        if context is None:
            data["remotes"].pop(pattern, None)
        else:
            data["remotes"][pattern] = context
        self._write(data)

    def resolve(self, cwd: str | Path, data: dict | None = None) -> Resolution:
        """Context of ``cwd``: nearest path rule or marker, then remote, then default."""
        data = data or self.load()
        paths = {
            k: v for k, v in data["paths"].items() if v in DIRECTORY_CONTEXTS
        }
        start = Path(normalize_path(cwd))
        repo_root: Path | None = None
        for directory in (start, *start.parents):
            ctx = paths.get(os.path.normcase(str(directory)))
            if ctx:
                return Resolution(ctx, "path", str(directory))
            marker = directory / MARKER_FILE
            ctx = read_marker(marker)
            if ctx:
                return Resolution(ctx, "marker", str(marker))
            if repo_root is None and (directory / ".git").exists():
                repo_root = directory

        remotes = {
            k: v for k, v in data["remotes"].items() if v in DIRECTORY_CONTEXTS
        }
        if repo_root is not None and remotes:
            urls = [normalize_remote(u) for u in git_remote_urls(repo_root)]
            # Longest prefix first, so "gitlab.acme.com/oss" beats "gitlab.acme.com".
            for pattern in sorted(remotes, key=len, reverse=True):
                if any(_remote_matches(normalize_remote(pattern), u) for u in urls):
                    return Resolution(remotes[pattern], "remote", pattern)

        return Resolution(data["default"], "default", "")

    def verdict(
        self, cwd: str | Path, email: str, org_uuid: str | None
    ) -> tuple[Resolution, str | None, Verdict]:
        """Resolve ``cwd`` and judge the account against it, from one read."""
        data = self.load()
        resolution = self.resolve(cwd, data)
        acct = data["accounts"].get(account_key(email, org_uuid))
        acct = acct if acct in ACCOUNT_CONTEXTS else None
        return resolution, acct, check(resolution.context, acct, data["mode"])

    def blocked_directories(
        self, cwds: list[str], email: str, org_uuid: str | None
    ) -> list[str]:
        """The subset of ``cwds`` where the account would not be allowed."""
        data = self.load()
        if data["mode"] == MODE_OPEN:
            return []
        acct = data["accounts"].get(account_key(email, org_uuid))
        acct = acct if acct in ACCOUNT_CONTEXTS else None
        blocked = []
        for cwd in cwds:
            if not cwd:
                continue
            ctx = self.resolve(cwd, data).context
            if not check(ctx, acct, data["mode"]).allowed:
                blocked.append(cwd)
        return blocked


def _validate(context: str, allowed: tuple[str, ...]) -> None:
    if context not in allowed:
        raise ValueError(
            f"context must be one of {', '.join(allowed)}, got {context!r}"
        )
