"""Pytest fixtures for Claude Switch tests."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import macos_keychain as _macos_keychain
from claude_swap import paths as _paths


class RealStoreWriteBlocked(Exception):
    """A test process tried to write the REAL account store.

    Raised from a process-global ``sys.addaudithook`` — installed once,
    below, with no removal API (by CPython design: an audit hook cannot be
    uninstalled short of process exit). That permanence is the point: the
    fixture-based isolation (``_isolate_real_home``, ``temp_home``) uses
    ``patch.dict``/``monkeypatch`` as context managers, which unwind at test
    teardown. A thread started inside a test that outlives its own test's
    teardown (this repo's tests spawn threads — the auto-switch loop, the
    consume-gate lock toucher) sees the REAL, unpatched ``$HOME`` by the
    time it gets around to writing, because ``pathlib.Path.home`` and
    ``os.environ`` are process-global, not thread-local, and the patch that
    protected it is already gone. A guard that itself unwinds cannot stop
    that; this one cannot unwind.

    Measured incident: the real ``~/.local/share/claude-swap/sequence.json``
    and ``credentials/*.enc`` were overwritten with the exact
    ``a@example.com``/``b@example.com`` pair ``EngineHarness.seed`` (this
    repo, ``tests/test_autoswitch.py``) writes.

    I-3 (round 9): deliberately NOT a ``PermissionError``/``OSError``
    subclass. ``pathlib.Path.mkdir(parents=True, exist_ok=True)`` catches
    ``OSError`` internally and swallows it when the target already exists —
    so an ``OSError``-based refusal into an EXISTING protected directory
    (``cache.write_cache``, ``_atomic_b64_write``, ``_update_global_config``
    all call ``mkdir(exist_ok=True)``) fired but never reached the caller.
    A plain ``Exception`` cannot be caught by any ``except OSError:`` in the
    stdlib or this codebase, so the refusal can no longer be absorbed by
    the very shape it is meant to guard.
    """


def _freeze_real_store_specs() -> tuple[tuple[Path, bool], ...]:
    """Snapshot the REAL (non-test) account-store roots EXACTLY ONCE, here,
    at conftest import time — before any fixture has ever touched
    ``$HOME``/``CLAUDE_CONFIG_DIR``/``XDG_DATA_HOME``.

    This snapshot, not a live re-resolution, is what the audit hook always
    compares candidate write targets against. Re-resolving ``claude_swap.
    paths`` at CHECK time (this guard's first draft) is tautological and
    wrong: during an isolated test those functions correctly resolve to the
    test's OWN tmp directory, so "does this write's target match what
    paths.* resolves to right now" is true for every legitimate isolated
    write too — measured directly, it blocked ``mock_claude_config``'s own
    fixture write to its own ``temp_home``. A FROZEN reference is what makes
    the exemptions (``temp_home``, ``CLAUDE_CONFIG_DIR``/``XDG_DATA_HOME``
    overrides) fall out for free: their resolved paths are tmp paths that
    simply never equal the frozen real ones, no special-casing needed. Only
    once a test's isolation has unwound — or the rare test that constructs a
    switcher without ``temp_home`` at all — does ``paths.*`` resolve back to
    something matching this frozen set, which is exactly the moment a write
    must be refused.

    Two snapshots are unioned, because there are two notions of "real" and a
    developer can be on either one:

    * The GENUINELY-DEFAULT roots: mirrors ``_isolate_real_home``'s notion of
      "real" — ``CLAUDE_CONFIG_DIR``/``CLAUDE_SECURESTORAGE_CONFIG_DIR``/
      ``XDG_DATA_HOME`` are cleared for this resolution (then restored), so a
      developer who happens to have one of those exported in their normal
      shell still gets the default roots (``~/.claude``, ``~/.local/share/
      claude-swap``) protected — the same three vars that fixture neutralizes
      for every test, regardless of ``temp_home``.
    * The AMBIENT-OVERRIDE roots: resolved WITHOUT clearing those vars, i.e.
      exactly what ``claude_swap.paths`` resolves to under the environment as
      it actually is at conftest import time. A developer with
      ``XDG_DATA_HOME`` exported outside ``$HOME`` in their normal shell has
      their REAL account store at that override path, not at the
      cleared-env default — the defaults-only snapshot left it unprotected
      (measured: a write to the override-resolved backup root went through).
      Both must be protected: the default-profile developer and the
      override-profile developer are both real users of this same conftest.

    ``recursive=True`` roots (the cswap backup root, current-XDG and legacy)
    are exclusively cswap's own data — everything beneath them is protected,
    any depth. This applies to an override-derived backup root too: it's
    still cswap's own data regardless of which env var pointed at it.

    ``recursive=False`` roots are directories cswap shares with unrelated
    machinery — notably ``~/.claude``, which also holds Claude Code CLI's
    OWN job/worktree/project state (this worktree itself lives under
    ``~/.claude/jobs/...``, several directories deep) and can contain a
    ``.pytest_cache``. Only DIRECT children are protected — exactly the
    files ``claude_swap.paths``/``claude_locks`` ever place directly inside
    these dirs (``.credentials.json``, ``.config.json``,
    ``.oauth_refresh.lock``, sibling atomic-write tempfiles, and — via
    ``get_global_config_path().parent``, i.e. ``$HOME`` itself when no
    ``CLAUDE_CONFIG_DIR`` override is set — ``~/.claude.json``,
    ``~/.claude.json.lock``, ``~/.claude.lock``). A deeply nested path (a
    job's worktree several directories under ``~/.claude/jobs/...``) is
    correctly left alone. An override-derived config home (``CLAUDE_CONFIG_
    DIR``) gets the same treatment: it's shared with other machinery just
    like the default ``~/.claude`` is, so it stays non-recursive too.
    """

    def _resolve() -> tuple[tuple[Path, bool], ...]:
        return (
            (_paths.get_backup_root(), True),
            (_paths.get_legacy_backup_root(), True),
            (_paths.get_claude_config_home(), False),
            (_paths.get_default_claude_config_home(), False),
            (_paths.get_global_config_path().parent, False),
            (_paths.get_default_global_config_path().parent, False),
        )

    ambient_specs = _resolve()

    def _resolve_with_cleared(*names: str) -> tuple[tuple[Path, bool], ...]:
        saved = {k: os.environ.get(k) for k in names}
        for k in saved:
            os.environ.pop(k, None)
        try:
            return _resolve()
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    default_specs = _resolve_with_cleared(
        "CLAUDE_CONFIG_DIR", "CLAUDE_SECURESTORAGE_CONFIG_DIR", "XDG_DATA_HOME"
    )
    # C-0: a THIRD snapshot, additive to (not replacing) default_specs above.
    # The mandated review/CI isolation recipe sets HOME/USERPROFILE (and
    # XDG_DATA_HOME) BEFORE the interpreter starts — before conftest is even
    # imported — so neither ambient_specs (ambient HOME) nor default_specs
    # (HOME still ambient, only XDG/CLAUDE_CONFIG_DIR cleared) ever resolves
    # to the account's TRUE real-store roots in that shape: both see the
    # scratch HOME. Clearing HOME/USERPROFILE too makes `Path.home()` fall
    # back to the OS account home (`pwd` on POSIX), which is what a real
    # developer's real store sits under regardless of what the recipe
    # exported. Kept as its own pass rather than folded into default_specs:
    # default_specs' contract (XDG/CLAUDE_CONFIG_DIR cleared, HOME left
    # exactly as ambient) is relied on elsewhere in this test suite (e.g. a
    # test that clears XDG_DATA_HOME mid-run via monkeypatch and asserts on
    # the HOME-ambient-only resolution) — folding HOME into the same clear
    # collapses that distinct combination and silently drops the roots it
    # used to protect.
    home_default_specs = _resolve_with_cleared(
        "CLAUDE_CONFIG_DIR", "CLAUDE_SECURESTORAGE_CONFIG_DIR", "XDG_DATA_HOME",
        "HOME", "USERPROFILE",
    )

    seen: set[Path] = set()
    merged: list[tuple[Path, bool]] = []
    for root, recursive in (*default_specs, *home_default_specs, *ambient_specs):
        if root in seen:
            continue
        seen.add(root)
        merged.append((root, recursive))
    return tuple(merged)


_REAL_STORE_SPECS = _freeze_real_store_specs()
# Frozen alongside _REAL_STORE_SPECS, at the same moment — see
# _derive_real_store_hints for why this must be a snapshot passed in
# explicitly rather than a live Path.home() call at hint-derivation time.
_HOME_AT_FREEZE_TIME = Path.home()


def _derive_real_store_hints(
    specs: tuple[tuple[Path, bool], ...], home: Path,
) -> tuple[str, ...]:
    """Cheap pre-filter substrings, checked before any Path resolution so the
    overwhelming majority of audit events (Python's own imports, pytest's
    internals, unrelated stdlib file activity — the guard fires on EVERY
    "open" system-wide for the whole test run) are rejected with a plain
    substring scan, not a function call into claude_swap.paths.

    C2: this used to be a hardcoded guess, ``(".claude", "claude-swap")`` —
    correct for the two DEFAULT roots (the XDG backup root always ends in
    ``/claude-swap``, the config home in ``/.claude``), but a developer with
    ``CLAUDE_CONFIG_DIR=$HOME/work-profile`` exported has their real store at
    ``~/work-profile/.credentials.json``, which contains neither substring —
    so the pre-filter rejected it before the (correct) specs loop ever ran,
    and the write went through. ``CLAUDE_CONFIG_DIR`` is arbitrary; a fixed
    guess can't anticipate it. Deriving the hints from every FROZEN root's
    own basename closes that class of gap entirely — any root actually
    protected gets a hint for free, including a future one — while the two
    defaults stay unioned in as a floor (``.name`` is empty for a root
    resolving to ``/``).

    One root is deliberately EXCLUDED from this: ``Path.home()`` itself
    (``get_global_config_path().parent``/``get_default_global_config_path()
    .parent`` when no ``CLAUDE_CONFIG_DIR`` is set, or no legacy
    ``.config.json``). Its basename is the developer's OS username, and
    every path on the machine tends to contain it (``/tmp/pytest-of-
    <user>/...``, any project under their home directory) — using it as a
    pre-filter hint would make the "cheap reject" match almost everything,
    defeating the pre-filter's purpose while adding nothing: this root's
    only protected direct children are the hardcoded, always-dot-claude-
    prefixed ``~/.claude.json``/``~/.claude.json.lock``/``~/.claude.lock``
    (see ``_freeze_real_store_specs``'s docstring), which the ``.claude``
    floor hint already catches. Known residual: a developer who sets
    ``CLAUDE_CONFIG_DIR`` to their bare ``$HOME`` (not a subdirectory) would
    reintroduce a narrower version of the C2 gap for that one degenerate
    config, since a root that happens to equal ``home`` is excluded
    regardless of why it resolved there. Not fixed here: the brief's own
    reproduction (and every realistic override) points at a *subdirectory*
    of ``$HOME``, never ``$HOME`` itself.

    ``home`` is a required parameter, not a live ``Path.home()`` call inside
    this function: callers (including tests that recompute ``specs`` under a
    monkeypatched ``Path.home``) must pass the SAME home that produced
    ``specs``. A live lookup here would silently disagree with it whenever
    something ELSE patches ``Path.home`` afterward — measured directly: the
    autouse ``_isolate_real_home`` fixture does exactly that for any test
    without its own ``temp_home``, which reintroduced ``$HOME``'s basename
    into the hints for every such test despite the module-level global
    (computed once, correctly, at conftest import time) excluding it.
    """
    return tuple(
        {".claude", "claude-swap"}
        | {root.name for root, _r in specs if root.name and root != home}
    )


_REAL_STORE_HINTS = _derive_real_store_hints(_REAL_STORE_SPECS, _HOME_AT_FREEZE_TIME)

_WRITE_EVENTS = frozenset(
    {
        "open", "os.rename", "os.mkdir", "os.remove", "os.rmdir",
        "shutil.rmtree", "os.symlink", "os.truncate",
    }
)


def _is_write_open(mode: str | None, flags: int) -> bool:
    """Whether an ``open`` audit event represents a WRITE (not a plain read).

    ``mode`` is set for the ``io.open``/``pathlib`` path (e.g. ``"w"``,
    ``"r+"``); ``os.open`` calls pass ``mode=None`` and only ``flags``, so
    both must be checked — mirrors the exact two shapes measured via
    ``sys.addaudithook`` probing (``('path', 'w', flags)`` vs.
    ``('path', None, flags)``).
    """
    if mode is not None:
        return any(c in mode for c in ("w", "a", "x", "+"))
    # `os.O_ACCMODE` is POSIX-only — it does not exist on Windows, where this
    # raised AttributeError from inside the hook and took pytest down at
    # collection with an INTERNALERROR (measured: CI test-windows on a5c4b61,
    # while the same tree was 1845-green on Linux). Derive the mask from
    # O_WRONLY|O_RDWR, both of which every platform defines, instead of
    # importing a constant only some platforms have.
    _accmode = os.O_WRONLY | os.O_RDWR
    if (flags & _accmode) != os.O_RDONLY:
        return True
    return bool(flags & (os.O_CREAT | os.O_TRUNC | os.O_APPEND))


def _real_store_audit_hook(event: str, args: tuple) -> None:
    if event not in _WRITE_EVENTS:
        return
    if event == "open":
        path, mode, flags = args
        if isinstance(path, int) or not _is_write_open(mode, flags):
            return  # an already-open fd, or a plain read
        candidates = (path,)
    elif event == "os.rename":
        # Covers os.replace too (same underlying audit event) — args are
        # (src, dst, src_dir_fd, dst_dir_fd). I-1: the SOURCE matters too,
        # not only the destination — `shutil.move`'s `os.rename` (which is
        # what `migrate_legacy_backup_dir` uses) relocates the protected
        # root itself when the source is inside it and the destination is
        # not, the same "make the store disappear from where every reader
        # expects it" shape the `shutil.rmtree` branch below exists for.
        candidates = (args[0], args[1])
    elif event == "os.symlink":
        # I-2: args are (src, dst, dir_fd) — `src` is the link's TARGET
        # (arbitrary, may point anywhere) and is not itself written; `dst`
        # is the link path being CREATED, which is the write. A symlink
        # planted inside a protected root is exactly the same "make a
        # write elsewhere alias into the store" shape a hardlink or bind
        # mount would be — only the destination matters here.
        candidates = (args[1],)
    elif event == "os.truncate":
        # I-4: `os.truncate(path, length)` destroys content in place without
        # going through `open`, so it reached none of the write-mode checks
        # above. args are (path, length).
        candidates = (args[0],)
    elif event == "shutil.rmtree":
        # `sys.audit("shutil.rmtree", path, dir_fd)` fires ONCE, at the top,
        # before a single child is touched. The fd-based walk it then runs
        # (`_rmtree_safe_fd`, the default on every platform with `dir_fd`
        # support) removes children by name RELATIVE to an open directory fd
        # — `os.remove('seq.json', dir_fd=...)` — so hooking the per-child
        # `os.remove`/`os.rmdir` events (like every other write event here)
        # can't see them at all: a `dir_fd`-relative name is resolved by the
        # KERNEL against that fd, not against `os.getcwd()`, so there is no
        # candidate string this hook could even join against (unlike I-3's
        # relative paths below, which genuinely do resolve against cwd).
        # `shutil.rmtree`'s own top-level event is the one place a
        # relative-by-dir_fd deletion is still announced with an absolute
        # path — refusing here stops the walk before its first unlink,
        # instead of refusing on the final `os.rmdir(path)` after every
        # child is already gone. Measured before this branch existed:
        # 5 entries -> 0, guard raised anyway.
        #
        # Unlike every other event here, `path` is `sys.audit`'s VERBATIM
        # first argument to `shutil.rmtree()` — a caller passing a `Path`
        # (every call site in this repo does) reaches this hook as a
        # `PosixPath`, not the `str` every other event always delivers.
        # `Path` has no `__contains__`, so the substring hint-check below
        # would raise `TypeError` on the very case this branch exists for.
        # `os.fspath` normalizes both to `str`/`bytes` uniformly.
        try:
            candidates = (os.fspath(args[0]),)
        except TypeError:
            candidates = ()
    else:  # os.mkdir / os.remove / os.rmdir
        candidates = (args[0],)

    for candidate in candidates:
        if candidate is None or isinstance(candidate, int):
            continue
        if isinstance(candidate, bytes):
            # I-5: a bytes path (`open(b"/path", ...)`) reached `hint in
            # candidate` below as a `bytes in bytes` substring test against
            # a `str` hints tuple, which is never True for any real path —
            # not a false positive, a silent skip. Decode so every
            # candidate reaches the SAME str-space check regardless of how
            # the caller spelled the path; malformed bytes fail closed
            # (reported, not silently ignored) rather than bypassing the
            # filter the way the original silent mismatch did.
            try:
                candidate = os.fsdecode(candidate)
            except (UnicodeDecodeError, ValueError):
                candidate = str(candidate)
        # Cheap reject on the raw string first — the overwhelming common
        # case (an absolute path with no hint substring) never pays for a
        # cwd lookup or a Path() construction below.
        hinted = any(hint in candidate for hint in _REAL_STORE_HINTS)
        if not hinted and not os.path.isabs(candidate):
            # I-3: a RELATIVE path is resolved against os.getcwd() by every
            # syscall this hook guards — `open("sequence.json", "w")` with a
            # cwd inside a protected root writes there exactly as much as
            # the absolute spelling would. The raw relative string never
            # contains a hint substring on its own (it's just a filename),
            # so the cheap reject above cannot be trusted for it alone —
            # join against the real cwd and re-check before rejecting.
            # Relative candidates are rare (pytest/import-machinery/stdlib
            # activity — the overwhelming majority of audit events — pass
            # absolute paths), so this extra join only costs the uncommon
            # case, not the hot path the pre-filter exists for.
            joined = os.path.join(os.getcwd(), candidate)
            hinted = any(hint in joined for hint in _REAL_STORE_HINTS)
            candidate = joined
        if not hinted:
            continue  # cheap reject — the common case
        target = Path(candidate)
        for root, recursive in _REAL_STORE_SPECS:
            hit = (
                target == root or root in target.parents
                if recursive
                else target == root or target.parent == root
            )
            if hit:
                raise RealStoreWriteBlocked(
                    f"{event} refused: {target} is under the REAL account "
                    f"store ({root}), not an isolated test tmp_path. If this "
                    "test needs the real store, it doesn't — fix the "
                    "isolation instead of the guard."
                )


sys.addaudithook(_real_store_audit_hook)


class _KeychainStore:
    """In-memory ``(service, account) -> secret`` map standing in for the real
    macOS Keychain so unit tests never shell out to ``security`` or ``keyring``."""

    def __init__(self) -> None:
        self.data: dict[tuple[str, str], str] = {}

    # Mirrors the ``macos_keychain`` (security CLI) contract.
    def get_password(self, service: str, account: str) -> str | None:
        return self.data.get((service, account))

    def item_exists(self, service: str, account: str) -> bool:
        return (service, account) in self.data

    def set_password(self, service: str, account: str, password: str) -> None:
        self.data[(service, account)] = password

    def delete_password(self, service: str, account: str) -> None:
        self.data.pop((service, account), None)  # absent = no-op (rc 44)


def _make_fake_keyring() -> types.ModuleType:
    """Build an in-memory stand-in for the ``keyring`` module (which would hit the
    real Keychain on macOS) for code paths that lazily ``import keyring``."""

    class _Errors:
        class PasswordDeleteError(Exception):
            pass

        class PasswordSetError(Exception):
            pass

        class KeyringError(Exception):
            pass

    store: dict[tuple[str, str], str] = {}
    mod = types.ModuleType("keyring")
    mod.errors = _Errors  # type: ignore[attr-defined]

    def get_password(service: str, username: str):
        return store.get((service, username))

    def set_password(service: str, username: str, password: str) -> None:
        store[(service, username)] = password

    def delete_password(service: str, username: str) -> None:
        if (service, username) not in store:
            raise _Errors.PasswordDeleteError("not found")
        del store[(service, username)]

    mod.get_password = get_password  # type: ignore[attr-defined]
    mod.set_password = set_password  # type: ignore[attr-defined]
    mod.delete_password = delete_password  # type: ignore[attr-defined]
    return mod


@pytest.fixture(autouse=True)
def _isolate_real_home(request, tmp_path_factory, monkeypatch):
    """Safety net: no test may read or write the developer's real ``$HOME``.

    Some tests (CLI/TUI argument tests that call ``main()``, etc.) construct a real
    ``ClaudeAccountSwitcher`` without the ``temp_home`` fixture. Without isolation
    that switcher resolves to the real ``~/.claude-swap-backup`` — writing logs,
    running data migrations, and reading the real account list. Redirect ``$HOME``
    to a throwaway dir unless the test already uses ``temp_home`` (which sets its
    own). Runs first (autouse, before the keychain guard and other fixtures).

    Exempt the ``tmp_keychain`` fixture too: the macOS-CI integration tests that
    use it drive the real ``security`` CLI (``default-keychain`` /
    ``list-keychains``), which needs the real ``$HOME`` to locate
    ``~/Library/Keychains``. An isolated ``$HOME`` makes those commands fail. The
    fixture itself swaps the default keychain to a throwaway one and restores it.

    Always neutralize ``CLAUDE_CONFIG_DIR`` and ``XDG_DATA_HOME`` (even for
    ``temp_home`` tests): both bypass ``$HOME`` in path resolution
    (``paths.get_global_config_path``/``get_backup_root``), so a developer with
    either exported could otherwise have tests read/write real Claude config or
    backup paths — and on macOS that leads back to the real Keychain. Tests that
    exercise those vars set them explicitly, overriding this.
    """
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    if "temp_home" in request.fixturenames:
        return  # temp_home provides its own isolated home
    if "tmp_keychain" in request.fixturenames:
        return  # real-keychain integration tests need the real $HOME
    safe_home = tmp_path_factory.mktemp("isolated_home")
    (safe_home / ".claude").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(safe_home))
    monkeypatch.setenv("USERPROFILE", str(safe_home))
    monkeypatch.setattr("pathlib.Path.home", lambda: safe_home)


@pytest.fixture(autouse=True)
def block_real_keychain(request, monkeypatch):
    """Safety net: no test may touch the real macOS Keychain.

    Replaces the ``security``-CLI wrapper (``claude_swap.macos_keychain``) with an
    in-memory fake and injects a fake ``keyring`` module (for the lazy
    ``import keyring`` paths in purge/migrations). Tests marked
    ``@pytest.mark.no_keychain_fake`` opt out — either because they mock
    ``subprocess`` themselves (the wrapper's own unit tests) or because they run
    against a temporary keychain on GitHub Actions.

    Yields the in-memory :class:`_KeychainStore` so tests can seed/inspect it.
    """
    if request.node.get_closest_marker("no_keychain_fake"):
        yield None
        return
    store = _KeychainStore()
    monkeypatch.setattr(_macos_keychain, "get_password", store.get_password)
    monkeypatch.setattr(_macos_keychain, "item_exists", store.item_exists)
    monkeypatch.setattr(_macos_keychain, "set_password", store.set_password)
    monkeypatch.setattr(_macos_keychain, "delete_password", store.delete_password)
    monkeypatch.setitem(sys.modules, "keyring", _make_fake_keyring())
    yield store


@pytest.fixture
def temp_home(tmp_path: Path):
    """Create a temporary home directory for testing."""
    home = tmp_path / "home"
    home.mkdir()

    # Create .claude directory structure
    claude_dir = home / ".claude"
    claude_dir.mkdir()

    # Patch HOME environment variable (and USERPROFILE for Windows)
    env_patch = {"HOME": str(home), "USERPROFILE": str(home)}
    with patch.dict(os.environ, env_patch):
        # Also patch Path.home() directly for cross-platform compatibility
        with patch("pathlib.Path.home", return_value=home):
            yield home


@pytest.fixture
def mock_claude_config(temp_home: Path):
    """Create a mock Claude configuration file."""
    config = {
        "oauthAccount": {
            "emailAddress": "test@example.com",
            "accountUuid": "test-uuid-1234",
        }
    }
    config_path = temp_home / ".claude.json"
    config_path.write_text(json.dumps(config))
    return config_path


@pytest.fixture
def mock_credentials_file(temp_home: Path):
    """Create a mock credentials file for Linux/WSL."""
    creds = {"accessToken": "test-token", "refreshToken": "test-refresh"}
    cred_path = temp_home / ".claude" / ".credentials.json"
    cred_path.write_text(json.dumps(creds))
    return cred_path


@pytest.fixture
def sample_sequence_data():
    """Sample sequence.json data."""
    return {
        "activeAccountNumber": 1,
        "lastUpdated": "2024-01-01T00:00:00Z",
        "sequence": [1, 2],
        "accounts": {
            "1": {
                "email": "account1@example.com",
                "uuid": "uuid-1",
                "added": "2024-01-01T00:00:00Z",
            },
            "2": {
                "email": "account2@example.com",
                "uuid": "uuid-2",
                "added": "2024-01-02T00:00:00Z",
            },
        },
    }


@pytest.fixture
def mock_org_claude_config(temp_home: Path):
    """Claude config file with an active organization account."""
    config = {
        "oauthAccount": {
            "emailAddress": "user@example.com",
            "accountUuid": "user-uuid-1234",
            "organizationUuid": "org-uuid-5678",
            "organizationName": "Acme Corp",
            "organizationRole": "primary_owner",
            "displayName": "Test User",
        }
    }
    config_path = temp_home / ".claude.json"
    config_path.write_text(json.dumps(config))
    return config_path


@pytest.fixture
def mock_personal_claude_config(temp_home: Path):
    """Claude config file with a personal account (no organizationUuid)."""
    config = {
        "oauthAccount": {
            "emailAddress": "user@example.com",
            "accountUuid": "user-uuid-1234",
        }
    }
    config_path = temp_home / ".claude.json"
    config_path.write_text(json.dumps(config))
    return config_path


@pytest.fixture
def sample_sequence_data_pre_v06():
    """Pre-v0.6.0 sequence.json data without organizationUuid/Name fields."""
    return {
        "activeAccountNumber": 1,
        "lastUpdated": "2024-01-01T00:00:00Z",
        "sequence": [1, 2],
        "accounts": {
            "1": {
                "email": "user@example.com",
                "uuid": "user-uuid-1234",
                "added": "2024-01-01T00:00:00Z",
            },
            "2": {
                "email": "other@example.com",
                "uuid": "other-uuid-5678",
                "added": "2024-01-02T00:00:00Z",
            },
        },
    }


@pytest.fixture
def sample_sequence_data_with_org():
    """sequence.json data with mixed organization and personal accounts."""
    return {
        "activeAccountNumber": 1,
        "lastUpdated": "2024-01-01T00:00:00Z",
        "sequence": [1, 2],
        "accounts": {
            "1": {
                "email": "user@example.com",
                "uuid": "user-uuid",
                "organizationUuid": "org-uuid-5678",
                "organizationName": "Acme Corp",
                "added": "2024-01-01T00:00:00Z",
            },
            "2": {
                "email": "user@example.com",
                "uuid": "user-uuid",
                "organizationUuid": "",
                "organizationName": "",
                "added": "2024-01-02T00:00:00Z",
            },
        },
    }


@pytest.fixture(autouse=True)
def _deterministic_poll_jitter(monkeypatch):
    """Zero the poll-plan jitter so cadence tests are clock-exact; the jitter
    itself is exercised in test_poll_policy via an injected rng."""
    monkeypatch.setattr("claude_swap.poll_policy.JITTER_FRAC", 0.0)


@pytest.fixture(autouse=True)
def _deterministic_colour(monkeypatch):
    """A developer's terminal must not decide whether the suite passes.

    ``printer._detect_color_support`` honours ``FORCE_COLOR``/``NO_COLOR``
    before it consults ``isatty()`` — correct for the CLI, where those
    variables exist to override detection, and wrong under pytest, where
    stdout is captured and every assertion on plain output then breaks.
    Measured with ``FORCE_COLOR=3`` exported: 11 failures in test_switcher.py
    reading ``assert 'Skipping Account-2 (disabled)' in
    '\\x1b[38;5;173mSkipping\\x1b[0m Account-2 (disabled)'``, and the same
    tree green with the variable unset.

    Scrubbing the variables is not enough on its own, because detection
    CACHES. ``colors_enabled()`` latches the first answer into
    ``printer._colors_enabled`` and every later call returns it, so one test
    that latches ``True`` decides the styling for every test after it —
    whatever the environment says by then. Under ``pytest -s`` stdout stays
    the real terminal, so ``isatty()`` is True and ordinary tests latch it
    without meaning to. So the cache is reset too, on every entry, not just
    the two environment variables.

    A wrong reset can satisfy a shape check that only looks at "not latched":
    pinning `False` instead of `None` also leaves every test entering
    unlatched, while having switched from un-latching to PINNING — a
    different, wrong mechanism with the same signature. Both directions are
    covered: `test_printer.py`'s `TestColourEnvDoesNotLeakIntoTests` dies
    under either pin, not just under "reset removed".

    A number nobody can re-take is not evidence — this fixture's own comments
    went stale from hand-copied counts more than once, which is why none are
    kept here now. What's checked instead is the SHAPE: mutating the reset
    out flips the suite from green to broadly red, and both directions are
    exercised by real tests rather than recorded as a figure.

    Tests that exercise the detection itself set the variables explicitly,
    which overrides this.
    """
    # Ordering-PROOF half of the guard. `test_colour_cache_isolation.py`'s
    # latch/read pairs read as the guard, but they only fire in one ordering:
    # a run that selects only the reader, or splits the pair across workers,
    # is green over a broken fixture. These assertions run before EVERY
    # test, so no test ordering hides a latch — only a fixture that latches
    # during SETUP AFTER this one escapes them (a conftest fixture runs
    # before a module-level autouse fixture of the same scope). That is
    # exactly the shape of the two module-local fixtures this branch
    # deletes, so the fix is to not have them.
    from claude_swap import appearance as _appearance
    from claude_swap import printer as _printer

    # SNAPSHOT FIRST, ASSERT ON THE SNAPSHOT. The obvious form — asserting the
    # globals directly — depends on sitting ABOVE the resets, and nothing in
    # the code says so: moving the two `setattr` lines up would make each
    # assert read the value the reset just wrote, silently passing regardless
    # of what the previous test left behind. Reading into a local on the
    # fixture's first statement makes the detection independent of where the
    # assertions live, and is itself mutation-tested directly (not via
    # ordering) by `TestEntryAssertionsCatchAPoisonedGlobal` in
    # `test_printer.py`.
    inherited = (_printer._colors_enabled, _printer._theme)
    assert inherited[0] is None, (
        "a previous test latched the colour cache and nothing reset it, or a "
        "broader-scoped fixture latched it during setup"
    )
    assert inherited[1] == "dark", (
        "a previous test latched the theme, or a broader-scoped fixture "
        "latched it during setup"
    )
    # Both scrubs are killed individually ON A CLEAN ENVIRONMENT, which is the
    # only coverage worth having: with neither variable exported and both
    # lines deleted, the whole suite is green — so before
    # `test_printer.py`'s class-scoped `_exported` fixture these two lines
    # were dead code on every CI runner, guarded only on a box where the bug
    # was already happening. Drop either one now and its own test dies.
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr("claude_swap.printer._colors_enabled", None)
    # The OTHER latched global in the same module. `tui/app.py` calls
    # `printer.set_theme("light")`, a plain assignment with nothing restoring
    # it, so with this line removed the tests after it enter with the light
    # palette — spread across several files, not just the one that latched.
    # Green today only because none of them asserts a palette code, which is
    # exactly what was true of `_colors_enabled` until a developer exported
    # FORCE_COLOR.
    monkeypatch.setattr("claude_swap.printer._theme", "dark")
    # The third `global`-rebound name in the package. Inert under the TERM pin
    # below, which makes `detect_terminal_background` return before ever
    # writing it — but the pin is a guarantee about the tty QUERY, not about
    # the cache, and the cache then latches that `None` against any test that
    # stubs `_query_terminal_background`. `tests/test_appearance.py` carries
    # a module-local reset for exactly this reason, which is the layer this
    # fixture exists to replace.
    monkeypatch.setattr("claude_swap.appearance._cache", _appearance._UNSET)
    # And the OTHER thing a terminal decides: `appearance.detect_terminal_background`
    # puts the tty into cbreak, writes an OSC-11 query, and BLOCKS reading stdin
    # for up to a second. Under `pytest -s` stdin is the developer's real
    # terminal, so the suite emits escape bytes at it and can swallow a
    # keypress.
    #
    # `TERM=dumb` is the function's own documented short-circuit — it returns
    # None before touching termios — so this closes the path rather than
    # patching around it. It is a guarantee about the QUERY and not about the
    # cache, which is why the cache is reset above: the pin makes detection
    # return None fast, and the cache then latches that None against any test
    # that stubs `_query_terminal_background`. No test outside
    # test_appearance.py asserts on a palette code, so this was not flipping
    # results; it is the same class this fixture exists for, one module over.
    #
    # Stated as a magnitude, not a count, on purpose: the number of tests that
    # reach detection moves with every commit, so what stays true and
    # re-takeable is "zero termios entries with this pin, many without it,
    # and the suite runs measurably longer without it" (the OSC-11 read
    # timing out over and over).
    #
    # Pinned by test_the_suite_does_not_query_the_developers_terminal, which
    # opens a real pty so both isatty() calls are genuinely True and the TERM
    # check at appearance.py:97 is the only gate left, then reads the master
    # to see whether the query went out.
    #
    # That test also unsets TMUX and STY, deliberately: appearance.py:99
    # short-circuits on either, so a developer running inside tmux would get
    # a green from the WRONG gate and the TERM pin would look load-bearing
    # when it was not. This fixture does not pin TMUX/STY — nothing needs it
    # while TERM is pinned first — but the guard test has to strip them or it
    # proves nothing about the line it guards.
    monkeypatch.setenv("TERM", "dumb")


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items):
    """Pin every real-Keychain test to ONE xdist worker.

    ``no_keychain_fake`` means "this test drives the actual ``security`` CLI
    against a real keychain" -- a process-wide shared resource. Under
    ``-n auto`` two workers seed and delete the same ``Claude Code-credentials``
    item, and the reader sees "" where it just wrote a token. Measured on CI
    (macos-latest, commit 0aa9c1f), green on the previous head serially:

        FAILED test_read_credentials_finds_claude_code_seeded_entry
        AssertionError: assert '' == 'fake-token-read'

    ``xdist_group`` sends the whole group to one worker, so these serialize
    against each other while the other ~1900 tests keep running in parallel.
    A no-op when xdist is not installed: the marker is simply never consumed.

    ``tryfirst`` because xdist reads the group off the item when it builds the
    schedule. Measured: without it the marker IS applied (verified on the node)
    and the tests still scattered across 8 workers, indistinguishable from no
    grouping at all -- a direct ``@pytest.mark.xdist_group`` on the same tests
    landed them all on gw0. Applying the marker late is the same as not
    applying it.
    """
    for item in items:
        if item.get_closest_marker("no_keychain_fake"):
            item.add_marker(pytest.mark.xdist_group("real-keychain"))
