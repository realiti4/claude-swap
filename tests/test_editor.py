"""Tests for editor mode (`cswap code`: claude_swap.editor + SessionManager.open_editor)."""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from claude_swap import editor as ed
from claude_swap.exceptions import SessionError
from claude_swap.mappings import normalize_path
from claude_swap.models import Platform
from claude_swap.process_detection import IdeInstance
from claude_swap.session import (
    AUTH_OVERRIDE_ENV_VARS,
    SessionManager,
    scan_live_sessions,
    session_dir_for,
)

EMAIL = "account2@example.com"
ORG = "org-2"


@pytest.fixture(autouse=True)
def ide_ports_answer(monkeypatch):
    """Lockfiles written by these tests have no extension behind their port."""
    monkeypatch.setattr(
        "claude_swap.process_detection.is_ide_listening", lambda port, timeout=0.2: True
    )


def _window(port: int, folders: list[str], config_dir: Path, ide: str = "Visual Studio Code"):
    return ed.EditorWindow(IdeInstance(port, os.getpid(), ide, folders), config_dir)


def _write_lock(config_dir: Path, port: int, folders: list[str], pid: int | None = None):
    ide = config_dir / "ide"
    ide.mkdir(parents=True, exist_ok=True)
    (ide / f"{port}.lock").write_text(
        json.dumps({
            "pid": os.getpid() if pid is None else pid,
            "workspaceFolders": folders,
            "ideName": "Visual Studio Code",
            "transport": "ws",
        }),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class TestLaunchEnv:
    def test_drops_window_and_session_plumbing(self, monkeypatch):
        for var in ("VSCODE_IPC_HOOK", "ELECTRON_RUN_AS_NODE", "CLAUDECODE",
                    "CLAUDE_CODE_SSE_PORT", "CLAUDE_CODE_SESSION_ID"):
            monkeypatch.setenv(var, "x")
        monkeypatch.setenv("KEEP_ME", "1")
        env = ed.launch_env("/profile")
        assert env["CLAUDE_CONFIG_DIR"] == "/profile"
        assert env["KEEP_ME"] == "1"
        assert not any(
            k.upper().startswith(("VSCODE_", "ELECTRON_")) or k.upper() in
            ("CLAUDECODE", "CLAUDE_CODE_SSE_PORT", "CLAUDE_CODE_SESSION_ID")
            for k in env
        )

    def test_inherited_config_dir_is_replaced(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/old-window-profile")
        assert ed.launch_env("/new")["CLAUDE_CONFIG_DIR"] == "/new"
        assert "CLAUDE_CONFIG_DIR" not in ed.launch_env(None)

    def test_empty_value_pins_default_login(self):
        assert ed.launch_env("")["CLAUDE_CONFIG_DIR"] == ""

    def test_scrub(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
        assert "ANTHROPIC_API_KEY" not in ed.launch_env("/p", AUTH_OVERRIDE_ENV_VARS)
        assert ed.launch_env(None)["ANTHROPIC_API_KEY"] == "sk-x"


class TestWindows:
    def test_editor_windows_reads_default_and_profiles(self, temp_home):
        sessions = temp_home / ".claude-swap-backup" / "sessions"
        _write_lock(temp_home / ".claude", 1, ["/a"])
        _write_lock(sessions / "2-a", 2, ["/b"])
        found = {(w.instance.port, w.config_dir) for w in ed.editor_windows(sessions)}
        assert found == {(1, temp_home / ".claude"), (2, sessions / "2-a")}

    def test_dead_editor_is_ignored(self, temp_home, monkeypatch):
        sessions = temp_home / ".claude-swap-backup" / "sessions"
        _write_lock(temp_home / ".claude", 1, ["/a"], pid=999_999)
        monkeypatch.setattr(
            "claude_swap.process_detection.is_pid_alive", lambda pid: pid != 999_999
        )
        assert ed.editor_windows(sessions) == []

    def test_current_window_by_sse_port(self, tmp_path):
        windows = [_window(1, [], tmp_path), _window(2, [], tmp_path)]
        assert ed.current_window(windows, {"CLAUDE_CODE_SSE_PORT": "2"}) is windows[1]
        assert ed.current_window(windows, {"CLAUDE_CODE_SSE_PORT": "3"}) is None
        assert ed.current_window(windows, {}) is None

    def test_default_folder_from_subdirectory(self, temp_home, monkeypatch, tmp_path):
        root = tmp_path / "proj"
        (root / "src").mkdir(parents=True)
        _write_lock(temp_home / ".claude", 7, [str(root)])
        monkeypatch.setenv("CLAUDE_CODE_SSE_PORT", "7")
        sessions = temp_home / "sessions"
        assert normalize_path(ed.default_folder(sessions, root / "src")) == normalize_path(root)
        elsewhere = tmp_path / "other"
        elsewhere.mkdir()
        assert ed.default_folder(sessions, elsewhere) == elsewhere

    def test_resolve_launcher(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ed.shutil, "which", lambda name: f"/bin/{name}")
        assert ed.resolve_launcher(None, None) == "/bin/code"
        assert ed.resolve_launcher("windsurf", None) == "/bin/windsurf"
        cursor = _window(1, [], tmp_path, ide="Cursor")
        assert ed.resolve_launcher(None, cursor) == "/bin/cursor"
        monkeypatch.setattr(ed.shutil, "which", lambda name: None)
        with pytest.raises(SessionError, match="not found on PATH"):
            ed.resolve_launcher(None, None)

    def test_ide_names_for(self):
        assert ed.ide_names_for("/x/bin/code.cmd") == {"Visual Studio Code"}
        assert ed.ide_names_for("/x/vim") == set()


class TestLivenessCountsEditorWindows:
    """An idle editor window keeps its profile in use (no claude process runs)."""

    def test_ide_lock_is_live(self, tmp_path):
        profile = tmp_path / "2-a"
        _write_lock(profile, 5, ["/p"])
        sessions, unreadable = scan_live_sessions(profile)
        assert [(s.pid, s.kind) for s in sessions] == [(os.getpid(), "ide-window")]
        assert unreadable == 0

    def test_windows_of_one_editor_count_once(self, tmp_path):
        profile = tmp_path / "2-a"
        _write_lock(profile, 5, ["/p"])
        _write_lock(profile, 6, ["/q"])
        assert len(scan_live_sessions(profile)[0]) == 1

    def test_no_ide_dir_unchanged(self, tmp_path):
        profile = tmp_path / "2-a"
        profile.mkdir()
        assert scan_live_sessions(profile) == ([], 0)


# ---------------------------------------------------------------------------
# SessionManager.open_editor
# ---------------------------------------------------------------------------


class FakeEditor:
    """Stands in for the editor: records launches, and opens windows for them."""

    def __init__(self, home: Path):
        self.home = home
        self.windows: list[ed.EditorWindow] = []
        self.launches: list[tuple[list[str], dict]] = []
        self.reopens: list[tuple] = []
        self.opens_windows = True
        self._port = 100

    def add(self, folders, config_dir, ide="Visual Studio Code"):
        self._port += 1
        w = _window(self._port, [str(f) for f in folders], config_dir, ide)
        self.windows.append(w)
        return w

    def launch(self, launcher, args, env):
        self.launches.append((args, env))
        if self.opens_windows and args and args[0] == "-n":
            cfg = env.get("CLAUDE_CONFIG_DIR") or str(self.home / ".claude")
            self.add([args[1]], Path(cfg))

    def spawn_reopen(self, launcher, folder, port, sessions_dir, placeholder, env):
        self.reopens.append((folder, port, placeholder, env))

    def begin_reopen(self, launcher, folder, placeholder, env):
        self.launches.append((["<begin>", str(folder), str(placeholder)], env))


@pytest.fixture
def fake_editor(temp_home, monkeypatch):
    editor = FakeEditor(temp_home)
    editor.add(["/elsewhere"], temp_home / ".claude")  # the editor is running
    monkeypatch.setattr(ed, "editor_windows", lambda sessions_dir: list(editor.windows))
    monkeypatch.setattr(ed, "launch", editor.launch)
    monkeypatch.setattr(ed, "spawn_reopen", editor.spawn_reopen)
    monkeypatch.setattr(ed, "begin_reopen", editor.begin_reopen)
    monkeypatch.setattr(ed, "resolve_launcher", lambda editor_, here: "/bin/code")
    monkeypatch.setattr(ed, "wait_for", lambda predicate, timeout: predicate())
    for var in list(os.environ):
        if var.startswith("CLAUDE"):
            monkeypatch.delenv(var)
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    return editor


@pytest.fixture
def editor_manager(temp_home, tmp_path):
    backup = temp_home / ".claude-swap-backup"
    profile = session_dir_for(backup, "2", EMAIL)
    switcher = SimpleNamespace(
        backup_dir=backup,
        _logger=logging.getLogger("test"),
        platform=Platform.LINUX,
        current=("default@example.com", "org-1"),
        kind="oauth",
        setups=[],
    )
    switcher.resolve_account = lambda ident: ("2", EMAIL, ORG)
    switcher._get_current_account = lambda: switcher.current
    switcher._account_kind = lambda num: switcher.kind
    manager = SessionManager(switcher)

    def fake_setup(identifier, share, share_history=False):
        switcher.setups.append((identifier, share, share_history))
        profile.mkdir(parents=True, exist_ok=True)
        return profile, "2", EMAIL

    manager.setup_session = fake_setup
    folder = tmp_path / "proj"
    folder.mkdir()
    return SimpleNamespace(manager=manager, switcher=switcher, profile=profile, folder=folder)


class TestOpenEditor:
    def test_new_window_on_profile(self, fake_editor, editor_manager, capsys):
        m = editor_manager
        m.manager.open_editor("2", m.folder)
        (args, env), = fake_editor.launches
        assert args == ["-n", str(m.folder)]  # the folder itself: same title, same history
        assert env["CLAUDE_CONFIG_DIR"] == str(m.profile)
        assert m.switcher.setups == [("2", True, True)]  # history shared by default
        out = capsys.readouterr().out
        assert "Opening" in out and "Account-2" in out and "has not reported" not in out

    def test_history_sharing_can_be_turned_off(self, fake_editor, editor_manager):
        editor_manager.manager.open_editor("2", editor_manager.folder, share_history=False)
        assert editor_manager.switcher.setups == [("2", True, False)]

    def test_unconfirmed_window_warns(self, fake_editor, editor_manager, capsys):
        fake_editor.opens_windows = False
        editor_manager.manager.open_editor("2", editor_manager.folder)
        assert "has not reported in" in capsys.readouterr().out

    def test_auth_overrides_scrubbed(self, fake_editor, editor_manager, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
        editor_manager.manager.open_editor("2", editor_manager.folder)
        (_, env), = fake_editor.launches
        assert "ANTHROPIC_API_KEY" not in env

    def test_default_login_opens_without_profile(self, fake_editor, editor_manager):
        m = editor_manager
        m.switcher.current = (EMAIL, ORG)
        m.manager.open_editor("2", m.folder)
        (args, env), = fake_editor.launches
        assert args == ["-n", str(m.folder)]
        assert "CLAUDE_CONFIG_DIR" not in env
        assert m.switcher.setups == []  # no second copy of the default credential

    def test_no_account_is_default_login(self, fake_editor, editor_manager):
        editor_manager.manager.open_editor(None, editor_manager.folder)
        (args, env), = fake_editor.launches
        assert args == ["-n", str(editor_manager.folder)]
        assert "CLAUDE_CONFIG_DIR" not in env

    def test_already_open_on_target_focuses(self, fake_editor, editor_manager, capsys):
        m = editor_manager
        fake_editor.add([m.folder], m.profile)
        m.manager.open_editor("2", m.folder)
        (args, _), = fake_editor.launches
        assert args == [str(m.folder)]
        assert "already open" in capsys.readouterr().out

    def test_open_elsewhere_on_other_account_refuses(self, fake_editor, editor_manager, temp_home):
        m = editor_manager
        fake_editor.add([m.folder], temp_home / ".claude")
        with pytest.raises(SessionError, match="another window"):
            m.manager.open_editor("2", m.folder)
        assert fake_editor.launches == []

    def test_this_window_already_on_target(self, fake_editor, editor_manager, monkeypatch, capsys):
        m = editor_manager
        w = fake_editor.add([m.folder], m.profile)
        monkeypatch.setenv("CLAUDE_CODE_SSE_PORT", str(w.instance.port))
        m.manager.open_editor("2", m.folder)
        assert fake_editor.launches == [] and fake_editor.reopens == []
        assert "already on" in capsys.readouterr().out

    def test_reopens_this_window(self, fake_editor, editor_manager, monkeypatch, temp_home):
        m = editor_manager
        here = fake_editor.add([m.folder], temp_home / ".claude")
        monkeypatch.setenv("CLAUDE_CODE_SSE_PORT", str(here.instance.port))
        m.manager.open_editor("2", m.folder)
        (folder, port, placeholder, env), = fake_editor.reopens
        assert (folder, port) == (m.folder, here.instance.port)
        assert placeholder.name == ed.PLACEHOLDER_NAME
        assert env["CLAUDE_CONFIG_DIR"] == str(m.profile)
        assert placeholder.parent.name == str(here.instance.port)  # one per window
        (begin, _), = fake_editor.launches
        assert begin == ["<begin>", str(m.folder), str(placeholder)]

    def test_reopen_back_to_default_pins_empty(self, fake_editor, editor_manager, monkeypatch):
        m = editor_manager
        m.profile.mkdir(parents=True)
        here = fake_editor.add([m.folder], m.profile)
        monkeypatch.setenv("CLAUDE_CODE_SSE_PORT", str(here.instance.port))
        m.manager.open_editor(None, m.folder)
        (_, _, _, env), = fake_editor.reopens
        assert env["CLAUDE_CONFIG_DIR"] == ""

    def test_cold_editor_started_plain_first(self, fake_editor, editor_manager):
        fake_editor.windows.clear()
        editor_manager.manager.open_editor("2", editor_manager.folder)
        (first_args, first_env), (second_args, _) = fake_editor.launches
        assert first_args == []
        assert "CLAUDE_CONFIG_DIR" not in first_env
        assert second_args[0] == "-n"

    def test_running_editor_not_restarted(self, fake_editor, editor_manager):
        editor_manager.manager.open_editor("2", editor_manager.folder)
        assert [a[0][0] for a in fake_editor.launches] == ["-n"]

    def test_api_key_account_rejected(self, fake_editor, editor_manager):
        editor_manager.switcher.kind = "api_key"
        with pytest.raises(SessionError, match="API-key"):
            editor_manager.manager.open_editor("2", editor_manager.folder)
        assert fake_editor.launches == []

    def test_not_a_directory(self, fake_editor, editor_manager, tmp_path):
        with pytest.raises(SessionError, match="Not a directory"):
            editor_manager.manager.open_editor("2", tmp_path / "missing")


class TestReopenHelper:
    def test_placeholder_is_empty_stable_and_per_window(self, tmp_path):
        path = ed.placeholder_workspace(tmp_path, 7)
        assert json.loads(path.read_text(encoding="utf-8")) == {"folders": []}
        os.utime(path, (1, 1))
        ed.placeholder_workspace(tmp_path, 7)
        assert path.stat().st_mtime == 1  # an open editor reloads on every write
        assert ed.placeholder_workspace(tmp_path, 8) != path

    def test_begin_focuses_the_window_before_reusing_it(self, monkeypatch, tmp_path):
        calls = []
        monkeypatch.setattr(ed, "launch", lambda l, a, e: calls.append(a))
        ed.begin_reopen("/bin/code", Path("/p"), Path("/ph"), {})
        assert calls == [
            [str(Path("/p"))], ["-r", "--skip-add-to-recently-opened", str(Path("/ph"))]
        ]

    def test_finish_reopen_focuses_then_reuses(self, monkeypatch, tmp_path):
        calls = []
        ph = ed.placeholder_workspace(tmp_path, 7)
        folder = str(tmp_path / "p")
        monkeypatch.setattr(ed, "launch", lambda l, a, e: calls.append(a))
        monkeypatch.setattr(ed, "editor_windows", lambda d: [])  # window let go
        monkeypatch.setattr(ed.time, "sleep", lambda s: None)
        assert ed.finish_reopen("/bin/code", folder, 7, tmp_path / "sessions", str(ph))
        assert calls == [["--skip-add-to-recently-opened", str(ph)], ["-r", folder]]
        assert not ph.exists()  # its own placeholder is cleaned up

    def test_finish_reopen_gives_up_when_window_holds_on(self, monkeypatch, tmp_path):
        """Cancelled at the unsaved-files prompt: leave the window alone."""
        calls = []
        ph = ed.placeholder_workspace(tmp_path, 7)
        monkeypatch.setattr(ed, "launch", lambda l, a, e: calls.append(a))
        monkeypatch.setattr(ed, "editor_windows", lambda d: [_window(7, ["/p"], tmp_path)])
        monkeypatch.setattr(ed, "wait_for", lambda predicate, timeout: predicate())
        assert not ed.finish_reopen("/bin/code", "/p", 7, tmp_path / "sessions", str(ph))
        assert calls == []
        assert ph.exists()

    def test_finish_reopen_waits_while_a_window_still_shows_the_folder(
        self, monkeypatch, tmp_path
    ):
        """Only the extension restarted (new port): the window still holds the folder."""
        calls = []
        folder = tmp_path / "p"
        ph = ed.placeholder_workspace(tmp_path, 7)
        monkeypatch.setattr(ed, "launch", lambda l, a, e: calls.append(a))
        monkeypatch.setattr(ed, "editor_windows", lambda d: [_window(8, [str(folder)], tmp_path)])
        monkeypatch.setattr(ed, "wait_for", lambda predicate, timeout: predicate())
        assert not ed.finish_reopen("/bin/code", str(folder), 7, tmp_path / "sessions", str(ph))
        assert calls == []

    def test_spawn_keeps_env_off_the_command_line(self, monkeypatch, tmp_path):
        seen = {}

        def fake_popen(argv, **kwargs):
            seen["argv"], seen["env"], seen["cwd"] = argv, kwargs["env"], kwargs["cwd"]
            return SimpleNamespace()

        monkeypatch.setattr(ed.subprocess, "Popen", fake_popen)
        env = {"CLAUDE_CONFIG_DIR": "/secret-dir"}
        ed.spawn_reopen("/bin/code", tmp_path, 7, tmp_path / "sessions", tmp_path / "ph", env)
        assert seen["env"] == env
        assert "/secret-dir" not in " ".join(seen["argv"])
        # -P and cswap's own folder as cwd: no module of the user's project
        # can be imported into a process holding the account's environment.
        assert seen["argv"][1:5] == ["-P", "-m", "claude_swap.editor", "--reopen"]
        assert seen["cwd"] == str(tmp_path)


class TestEmptyWindow:
    def test_window_without_folder_refuses(self, temp_home, monkeypatch, tmp_path):
        """Not the terminal's directory (usually home) in a folderless window."""
        _write_lock(temp_home / ".claude", 9, [])
        monkeypatch.setenv("CLAUDE_CODE_SSE_PORT", "9")
        with pytest.raises(SessionError, match="No project folder"):
            ed.default_folder(temp_home / "sessions", tmp_path)

    def test_home_directory_refuses(self, temp_home, monkeypatch):
        """A folderless window's early terminal has no mark and starts in home."""
        monkeypatch.delenv("CLAUDE_CODE_SSE_PORT", raising=False)
        with pytest.raises(SessionError, match="No project folder"):
            ed.default_folder(temp_home / "sessions", temp_home)

    def test_drive_root_refuses(self, temp_home, tmp_path):
        with pytest.raises(SessionError, match="No project folder"):
            ed.default_folder(temp_home / "sessions", Path(tmp_path.anchor))


class TestUnmarkedTerminal:
    """A terminal opened before the extension activated lacks the SSE port."""

    def test_matched_by_folder(self, tmp_path):
        root = tmp_path / "proj"
        windows = [_window(1, [str(root)], tmp_path), _window(2, ["/other"], tmp_path)]
        env = {"TERM_PROGRAM": "vscode"}
        assert ed.current_window(windows, env, cwd=root / "src") is windows[0]

    def test_ambiguous_is_unknown(self, tmp_path):
        root = tmp_path / "proj"
        windows = [_window(1, [str(root)], tmp_path), _window(2, [str(root)], tmp_path / "p")]
        assert ed.current_window(windows, {"TERM_PROGRAM": "vscode"}, cwd=root) is None

    def test_outside_an_editor_terminal_is_unknown(self, tmp_path):
        windows = [_window(1, [str(tmp_path)], tmp_path)]
        assert ed.current_window(windows, {}, cwd=tmp_path) is None


def test_this_window_switches_even_if_another_shows_the_folder(
    fake_editor, editor_manager, monkeypatch, temp_home
):
    """E.g. a .code-workspace window holding the same folder: a different
    identity to the editor, so switching this window is unaffected by it."""
    m = editor_manager
    fake_editor.add([m.folder], temp_home / ".claude-other")
    here = fake_editor.add([m.folder], temp_home / ".claude")
    monkeypatch.setenv("CLAUDE_CODE_SSE_PORT", str(here.instance.port))
    m.manager.open_editor("2", m.folder)
    (_, port, _, _), = fake_editor.reopens
    assert port == here.instance.port


def test_stale_terminal_mark_falls_back_to_folder(tmp_path):
    """The extension restarted (new port) after the terminal opened."""
    root = tmp_path / "proj"
    windows = [_window(1, [str(root)], tmp_path)]
    env = {"TERM_PROGRAM": "vscode", "CLAUDE_CODE_SSE_PORT": "999"}
    assert ed.current_window(windows, env, cwd=root) is windows[0]


class TestPortLiveness:
    def test_lock_whose_port_is_closed_is_not_a_window(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "claude_swap.process_detection.is_ide_listening", lambda port, timeout=0.2: False
        )
        _write_lock(tmp_path / "2-a", 5, ["/p"])
        assert scan_live_sessions(tmp_path / "2-a") == ([], 0)


class TestRemovePlaceholder:
    """The reopen clean-up must never be able to delete anything but its placeholder."""

    def test_removes_file_and_its_empty_folder(self, tmp_path):
        ph = ed.placeholder_workspace(tmp_path, 7)
        ed.remove_placeholder(ph, tmp_path)
        assert not ph.exists() and not ph.parent.exists()
        assert (tmp_path / "workspaces").is_dir()  # nothing above it is touched

    def test_keeps_a_folder_that_has_other_files(self, tmp_path):
        ph = ed.placeholder_workspace(tmp_path, 7)
        (ph.parent / "keep.txt").write_text("x", encoding="utf-8")
        ed.remove_placeholder(ph, tmp_path)
        assert (ph.parent / "keep.txt").exists()

    def test_refuses_any_other_name(self, tmp_path):
        other = tmp_path / "workspaces" / "7" / "important.txt"
        other.parent.mkdir(parents=True)
        other.write_text("x", encoding="utf-8")
        ed.remove_placeholder(other, tmp_path)
        assert other.exists()

    def test_refuses_the_right_name_in_the_wrong_place(self, tmp_path):
        """A file named like the placeholder, but not in <backup>/workspaces/<port>/."""
        for folder in (tmp_path / "elsewhere" / "7", tmp_path / "workspaces" / "notaport",
                       tmp_path):
            folder.mkdir(parents=True, exist_ok=True)
            stray = folder / ed.PLACEHOLDER_NAME
            stray.write_text("{}", encoding="utf-8")
            ed.remove_placeholder(stray, tmp_path)
            assert stray.exists(), folder

    def test_finish_reopen_with_a_bogus_path_deletes_nothing(self, monkeypatch, tmp_path):
        """Regression: a placeholder of "/ph" once made the clean-up delete the drive root.

        No delete call is mocked: the conftest guard refuses any delete outside
        the temp dir, so if the clean-up even tried, this test would fail.
        """
        monkeypatch.setattr(ed, "launch", lambda l, a, e: None)
        monkeypatch.setattr(ed, "editor_windows", lambda d: [])
        monkeypatch.setattr(ed.time, "sleep", lambda s: None)
        assert ed.finish_reopen("/bin/code", "/p", 7, tmp_path / "sessions", "/ph")


class TestDestructiveGuard:
    """The conftest guard itself: a test may not delete outside the temp dir."""

    def test_rmtree_outside_temp_is_refused(self):
        from tests.conftest import DestructiveOpBlocked

        # A path that does not exist, so even a broken guard deletes nothing.
        outside = Path(__file__).resolve().parent / "no-such-dir-cswap-guard-check"
        with pytest.raises(DestructiveOpBlocked):
            shutil.rmtree(outside, ignore_errors=True)

    def test_inside_temp_is_allowed(self, tmp_path):
        d = tmp_path / "x"
        d.mkdir()
        shutil.rmtree(d)
        assert not d.exists()


class TestCmdQuoting:
    """Windows .cmd launchers run through cmd.exe: nothing in a path may become a command."""

    def test_metacharacters_are_quoted(self):
        cmd = ed._windows_script_command(r"C:\VS Code\bin\code.cmd", ["-n", r"E:\Work\R&D|x>y"])
        assert '"E:\\Work\\R&D|x>y"' in cmd
        assert " /d /v:off /e:on /s /c " in cmd

    def test_trailing_backslash_does_not_escape_the_quote(self):
        cmd = ed._windows_script_command("code.cmd", ["C:\\"])
        assert cmd.endswith('"C:\\\\""')

    @pytest.mark.parametrize("bad", ['E:\\a"b', "E:\\100%", "E:\\a\nb", "E:\\a\x00b"])
    def test_unquotable_characters_are_refused(self, bad):
        with pytest.raises(SessionError):
            ed._windows_script_command("code.cmd", ["-n", bad])


def test_which_never_looks_in_the_current_directory(monkeypatch):
    seen = {}

    def fake_which(name):
        seen["flag"] = os.environ.get("NoDefaultCurrentDirectoryInExePath")
        return None

    monkeypatch.setattr(ed.shutil, "which", fake_which)
    monkeypatch.setattr(ed.sys, "platform", "win32")
    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
    ed.which_no_cwd("code")
    assert seen["flag"] == "1"
    assert "NoDefaultCurrentDirectoryInExePath" not in os.environ


def test_future_session_secrets_are_not_passed_on(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SOMETHING_TOKEN", "secret")
    monkeypatch.setenv("CLAUDE_CODE_NEW_SESSION_THING", "x")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    env = ed.launch_env(None)
    assert "CLAUDE_CODE_SOMETHING_TOKEN" not in env
    assert "CLAUDE_CODE_NEW_SESSION_THING" not in env
    assert env["CLAUDE_CODE_USE_BEDROCK"] == "1"  # a user's own setting passes through


def test_window_mark_names_its_profile():
    env = ed.launch_env("/profiles/3")
    assert env[ed.EDITOR_WINDOW_MARK] == "/profiles/3"


def test_list_shows_editor_windows_on_other_accounts(temp_home, capsys, tmp_path):
    """`cswap list` marks the default login only; windows on another account
    must still show up, with the account they are on."""
    from claude_swap.switcher import ClaudeAccountSwitcher

    switcher = ClaudeAccountSwitcher()
    switcher._setup_directories()
    switcher._write_json(switcher.sequence_file, {
        "activeAccountNumber": 1, "sequence": [1, 4],
        "accounts": {"4": {"email": "four@example.com", "organizationUuid": ""}},
    })
    profile = switcher.backup_dir / "sessions" / "4-four_example.com"
    _write_lock(profile, 11, [str(tmp_path / "portfolio")])
    _write_lock(temp_home / ".claude", 12, [str(tmp_path / "app")])  # default: not listed
    switcher._print_editor_windows()
    out = capsys.readouterr().out
    assert "Editor windows on other accounts" in out
    assert "portfolio" in out and "Account-4 (four@example.com)" in out
    assert "app" not in out.split("other accounts:")[1]
