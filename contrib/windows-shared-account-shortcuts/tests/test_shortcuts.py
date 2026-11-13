"""Windows integration checks using fake accounts, files and registry state."""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "slash"))
import install_slash as installer
import prepare_launch as launcher
import switch_hook as hook
import user_path

ORIGINAL_ENV = dict(os.environ)


class ShortcutsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="cswap-contrib-")
        self.home = Path(self.temporary.name).resolve()
        self.assertEqual(self.home.parent, Path(tempfile.gettempdir()).resolve())
        self.assertTrue(self.home.name.startswith("cswap-contrib-"))
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(patch.dict(os.environ, {}, clear=True))
        self.registry = [r"C:\Existing;", 2]
        self.stack.enter_context(patch.object(user_path, "read_user_path", side_effect=lambda: tuple(self.registry)))
        self.stack.enter_context(patch.object(installer, "write_user_path", side_effect=self.write_registry))
        self.stack.enter_context(patch.object(installer, "notify_environment"))
        self.settings_path = self.home / ".claude" / "settings.json"
        self.settings_path.parent.mkdir()
        self.other = {"matcher": "other", "hooks": [{"type": "command", "command": "echo other"}]}
        self.settings = {"permissions": {"allow": ["Read"]}, "hooks": {"UserPromptExpansion": [self.other]}}
        self.save_settings(self.settings)

    def tearDown(self):
        self.stack.close()
        self.assertEqual(self.home.parent, Path(tempfile.gettempdir()).resolve())
        self.assertTrue(self.home.name.startswith("cswap-contrib-"))
        self.temporary.cleanup()

    def write_registry(self, before, after):
        self.assertEqual(tuple(self.registry), before)
        self.registry[:] = after

    def save_settings(self, value):
        self.settings_path.write_text(json.dumps(value), encoding="utf-8")

    def test_generic_account_mapping(self):
        self.assertEqual(hook.ACCOUNTS, {"claude-account-a": ("1", "Account 1"), "claude-account-b": ("2", "Account 2")})

    def test_hook_merge_is_narrow_and_idempotent(self):
        merged = installer.build_settings(self.settings)
        self.assertEqual(merged["permissions"], self.settings["permissions"])
        self.assertEqual(merged["hooks"]["UserPromptExpansion"][0], self.other)
        self.assertEqual(installer.build_settings(merged), merged)
        foreign = {"hooks": {"UserPromptExpansion": [{"matcher": installer.MATCHER, "hooks": []}]}}
        with self.assertRaises(ValueError):
            installer.build_settings(foreign)

    def test_install_uninstall_preserves_later_path_changes(self):
        before = self.settings_path.read_bytes()
        result = installer.install(self.home)
        self.assertTrue(result["installed"])
        self.assertTrue(installer.install(self.home, check=True)["installed"])
        self.assertIsNone(installer.install(self.home)["backup"])
        backup = Path(result["backup"])
        self.assertEqual((backup / ".claude" / "settings.json").read_bytes(), before)
        self.assertTrue((backup / "user-path-before.json").is_file())
        self.registry[0] += r"D:\Later;"
        installer.install(self.home, uninstall=True)
        self.assertEqual(self.registry[0], r"C:\Existing;D:\Later;")
        self.assertFalse(any(p.exists() for p in installer.managed_files(self.home)))
        self.assertEqual(json.loads(self.settings_path.read_bytes()), self.settings)

    def test_existing_human_command_is_preserved(self):
        target = self.home / ".claude" / "commands" / "claude-account-a.md"
        target.parent.mkdir()
        target.write_bytes(b"human content")
        with self.assertRaises(ValueError):
            installer.install(self.home)
        self.assertEqual(target.read_bytes(), b"human content")
        self.assertEqual(self.registry[0], r"C:\Existing;")

    def test_registry_failure_rolls_back_files(self):
        before = self.settings_path.read_bytes()
        with patch.object(installer, "write_user_path", side_effect=OSError("fake registry failure")):
            with self.assertRaises(OSError):
                installer.install(self.home)
        self.assertEqual(self.settings_path.read_bytes(), before)
        self.assertFalse(any(p.exists() for p in installer.managed_files(self.home)))

    def test_hook_routes_both_accounts_without_model_calls(self):
        for name, number in (("claude-account-a", "1"), ("claude-account-b", "2")):
            with self.subTest(command=name), patch.object(hook, "account_problem", return_value=None):
                runner = Mock(return_value=SimpleNamespace(returncode=0))
                result = hook.handle({"hook_event_name": "UserPromptExpansion", "command_name": name}, runner=runner)
                self.assertFalse(result["continue"])
                self.assertEqual(runner.call_args.args[0][-2:], ["switch", number])

    def test_source_guards_prevent_switching(self):
        for settings in ({"env": {"ANTHROPIC_BASE_URL": "https://example.invalid"}},
                         {"env": {"ANTHROPIC_API_KEY": "fake-key"}},
                         {"env": {"CLAUDE_CODE_USE_VERTEX": "1"}},
                         {"apiKeyHelper": "fake-helper"}):
            with self.subTest(settings=settings), patch.object(Path, "home", return_value=self.home):
                self.save_settings(settings)
                runner = Mock()
                result = hook.handle({"hook_event_name": "UserPromptExpansion", "command_name": "claude-account-a"}, runner=runner)
                self.assertFalse(result["continue"])
                runner.assert_not_called()

    def test_launcher_check_and_failure(self):
        with patch.object(launcher, "account_problem", return_value=None), patch.object(launcher.shutil, "which", return_value="fake-claude.cmd"), patch.object(Path, "is_file", return_value=True):
            runner = Mock(return_value=SimpleNamespace(returncode=7))
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(launcher.prepare("1", check=True, runner=runner), 0)
                runner.assert_not_called()
                self.assertEqual(launcher.prepare("2", runner=runner), 7)

    def test_unrelated_command_and_extra_arguments_do_not_switch(self):
        runner = Mock()
        self.assertIsNone(hook.handle({"hook_event_name": "UserPromptExpansion", "command_name": "other"}, runner=runner))
        result = hook.handle({"hook_event_name": "UserPromptExpansion", "command_name": "claude-account-b", "command_args": "invalid"}, runner=runner)
        self.assertFalse(result["continue"])
        runner.assert_not_called()

    def test_actual_batch_entrypoints_forward_arguments_and_fail_closed(self):
        scripts = self.home / "venv" / "Scripts"
        scripts.mkdir(parents=True)
        shutil.copy2(sys.executable, scripts / "python.exe")
        shutil.copy2(Path(sys.executable).parents[1] / "pyvenv.cfg", scripts.parent / "pyvenv.cfg")
        (self.home / "slash").mkdir()
        (self.home / "slash" / "prepare_launch.py").write_text(
            'import os,sys\nfrom pathlib import Path\n'
            'Path(os.environ["SELECT_LOG"]).write_text(sys.argv[1])\n'
            'raise SystemExit(int(os.environ.get("SWITCH_EXIT","0")))\n', encoding="utf-8")
        fake_bin = self.home / "fake-bin"
        fake_bin.mkdir()
        (fake_bin / "claude.cmd").write_bytes(b'@echo off\r\n> "%START_LOG%" echo %*\r\nexit /b 13\r\n')
        selected, started = self.home / "selected.txt", self.home / "started.txt"
        env = {**ORIGINAL_ENV, "PATH": str(fake_bin) + ";" + ORIGINAL_ENV["PATH"], "SELECT_LOG": str(selected), "START_LOG": str(started)}
        for name, number in (("claude-account-a", "1"), ("claude-account-b", "2")):
            with self.subTest(command=name):
                script = self.home / (name + ".cmd")
                shutil.copy2(ROOT / script.name, script)
                result = subprocess.run([ORIGINAL_ENV["COMSPEC"], "/d", "/c", str(script), "--resume", "hello world"], env=env, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 13)
                self.assertEqual(selected.read_text(), number)
                self.assertIn('--resume "hello world"', started.read_text())
                started.unlink()
                result = subprocess.run([ORIGINAL_ENV["COMSPEC"], "/d", "/c", str(script)], env={**env, "SWITCH_EXIT": "7"}, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 7)
                self.assertFalse(started.exists())


if __name__ == "__main__":
    unittest.main()
