"""The JS-facing API of the desktop GUI.

One object serves both shells: pywebview exposes its public methods as
``window.pywebview.api.*``, and the browser fallback (``gui.server``) routes
``POST /api/<name>`` to the same methods. Everything public here is reachable
from the page, so internal state lives in underscore attributes (pywebview
skips those when it walks the object).

Every method is blocking and is called on a worker thread by either shell —
never on the GUI loop. Mutating actions are single-flight and captured the
same way the TUI captures them: stdout/stderr go to a buffer that is returned
to the page, and stdin is empty so a stray prompt fails instead of hanging.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque
from pathlib import Path
from typing import Callable

from claude_swap import __version__
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.gui import desktop, launcher
from claude_swap.json_output import usage_to_json
from claude_swap.models import AccountSnapshot, AccountsSnapshot
from claude_swap.snapshot_source import SnapshotSource
from claude_swap.switcher import SENTINEL_NOTES, ClaudeAccountSwitcher, last_seen_note

RELEASES_API = "https://api.github.com/repos/xutianhao6/claude-swap-desktop/releases/latest"
RELEASES_PAGE = "https://github.com/xutianhao6/claude-swap-desktop/releases"

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")

# Engine events kept for the page's log; older ones fall off.
_EVENT_BUFFER = 500

_LOGIN_ID_RE = re.compile(r"\d+-[0-9a-f]{8}")
# The manual-flow link `auth login` prints (wrapped in an OSC 8 hyperlink):
# its page shows a code to paste back instead of calling a local port.
_LOGIN_URL_RE = re.compile(r"https://[^\s\x07\x1b]+")
# A sign-in left open this long is abandoned.
LOGIN_TIMEOUT_S = 15 * 60


_UI_PREFS = {"simple": True, "view": "cli", "theme": "auto", "tray": True}
_UI_PREF_CHOICES = {"view": ("cli", "desktop"), "theme": ("auto", "light", "dark")}


def _valid_pref(key: str, value) -> bool:
    return value in _UI_PREF_CHOICES.get(key, (value,))


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _win_set_clipboard(text: str) -> None:
    """CF_UNICODETEXT via user32 (clip.exe would keep a BOM in the text)."""
    import ctypes
    from ctypes import wintypes

    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalLock.argtypes = (wintypes.HGLOBAL,)
    kernel32.GlobalUnlock.argtypes = (wintypes.HGLOBAL,)
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = (wintypes.UINT, wintypes.HANDLE)
    data = text.encode("utf-16-le") + b"\0\0"
    for _ in range(10):  # another app may hold the clipboard for a moment
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        raise OSError("剪贴板被占用")
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
        pointer = kernel32.GlobalLock(handle)
        ctypes.memmove(pointer, data, len(data))
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(13, handle):  # CF_UNICODETEXT
            raise OSError("无法写入剪贴板")
    finally:
        user32.CloseClipboard()


class _Login:
    """One `claude auth login` running windowless against a staging profile."""

    def __init__(self, proc: subprocess.Popen, staging: Path) -> None:
        self.proc = proc
        self.staging = staging
        self.started = time.time()
        self.output: list[str] = []
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in iter(self.proc.stdout.readline, ""):
            self.output.append(line)

    def text(self) -> str:
        return "".join(self.output)

    def url(self) -> str | None:
        match = _LOGIN_URL_RE.search(self.text())
        return match.group(0) if match else None

    def send(self, line: str) -> None:
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def kill(self) -> None:
        if self.proc.poll() is not None:
            return
        if sys.platform == "win32":
            # claude is often an npm .cmd shim: take node down with it.
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(self.proc.pid)],
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            with contextlib.suppress(OSError):
                os.killpg(self.proc.pid, 15)
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.proc.wait(timeout=5)


class GuiApi:
    """Methods the page calls. Return values are JSON-serializable."""

    def __init__(self, switcher: ClaudeAccountSwitcher, *, mode: str) -> None:
        self._switcher = switcher
        self._mode = mode  # "webview" | "browser"
        self._source = SnapshotSource(switcher)
        self._snap_lock = threading.Lock()
        self._last_snapshot: dict | None = None
        self._action_lock = threading.Lock()
        self._engine = None
        self._engine_dry_run = False
        self._events: deque[dict] = deque(maxlen=_EVENT_BUFFER)
        self._event_seq = 0
        self._events_lock = threading.Lock()
        self._window = None  # pywebview window, for native file dialogs
        self._last_plan_refresh = 0.0
        self._last_ping = time.time()
        self._logins: dict[str, _Login] = {}
        self._logins_lock = threading.Lock()
        self._desktop_lock = threading.Lock()  # Claude Desktop, apart from the CLI's
        self._desktop_login: dict | None = None

    def _attach_window(self, window) -> None:
        self._window = window

    def _show_window(self) -> None:
        """Bring the window forward; replaced once the window runs."""

    # -- boot / housekeeping --------------------------------------------------

    def boot(self) -> dict:
        from claude_swap.settings import settings_path

        return {
            "version": __version__,
            "platform": sys.platform,
            "mode": self._mode,
            "backupDir": str(self._switcher.backup_dir),
            "settingsPath": str(settings_path(self._switcher.backup_dir)),
            "frozen": bool(getattr(sys, "frozen", False)),
            "prefs": self.ui_prefs(),
        }

    # The webview runs in private mode, so page storage does not outlive the
    # window; view preferences live beside the settings instead.
    def _prefs_path(self) -> Path:
        return self._switcher.backup_dir / "gui-prefs.json"

    def ui_prefs(self) -> dict:
        prefs = dict(_UI_PREFS)
        try:
            saved = json.loads(self._prefs_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        if isinstance(saved, dict):
            for key, default in _UI_PREFS.items():
                if isinstance(saved.get(key), type(default)) and _valid_pref(key, saved[key]):
                    prefs[key] = saved[key]
        return prefs

    def set_ui_pref(self, key: str, value) -> dict:
        if key not in _UI_PREFS or not isinstance(value, type(_UI_PREFS[key])) or not _valid_pref(key, value):
            return {"ok": False, "output": f"无效的界面设置：{key}"}
        prefs = self.ui_prefs()
        prefs[key] = value
        path = self._prefs_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(prefs), encoding="utf-8")
        os.replace(tmp, path)
        return {"ok": True, "output": ""}

    def ping(self) -> bool:
        """Browser-mode heartbeat; the server exits once pings stop."""
        self._last_ping = time.time()
        return True

    # -- read path --------------------------------------------------------------

    def snapshot(self, full: bool = False) -> dict:
        """One snapshot pass, or the last one while a pass is still running.

        The page polls every few seconds; a slow pass (network, keychain)
        must not pile up threads behind the lock.
        """
        if not self._snap_lock.acquire(blocking=False):
            return self._last_snapshot or self._empty_snapshot()
        try:
            engine_running = self._engine is not None
            snap = self._source.take(full=bool(full), store_only=engine_running)
            if engine_running and time.time() - self._last_plan_refresh > 600:
                # Store-only snapshots never reach the network; keep the plan
                # labels current the way the TUI's auto view does.
                self._last_plan_refresh = time.time()
                threading.Thread(target=self._source.refresh_plans, daemon=True).start()
            self._last_snapshot = self._serialize(snap)
            return self._last_snapshot
        except Exception as e:  # keep the last good view, report the failure
            result = dict(self._last_snapshot or self._empty_snapshot())
            result["error"] = strip_ansi(str(e))
            return result
        finally:
            self._snap_lock.release()

    def _empty_snapshot(self) -> dict:
        return {"accounts": [], "activeNumber": None, "takenAt": None, "engine": self._engine_state()}

    def _serialize(self, snap: AccountsSnapshot) -> dict:
        from claude_swap.settings import load_settings

        try:
            settings = load_settings(self._switcher.backup_dir)
            threshold, weekly = settings.threshold, settings.weekly_threshold
        except Exception:
            threshold, weekly = None, None
        return {
            "accounts": [self._account_dict(acc) for acc in snap.accounts],
            "activeNumber": snap.active_number,
            "takenAt": snap.taken_at,
            "threshold": threshold,
            "weeklyThreshold": weekly,
            "engine": self._engine_state(),
        }

    @staticmethod
    def _account_dict(acc: AccountSnapshot) -> dict:
        from claude_swap import subscription

        entry = acc.usage
        usage = None
        if isinstance(entry.last_good, dict):
            try:
                usage = usage_to_json(entry.last_good, entry.fetched_at)
            except Exception:
                usage = None
        plan = None
        if acc.subscription_plan:
            try:
                plan = subscription.plan_label(acc.subscription_plan.get("plan"))
            except Exception:
                plan = None
        return {
            "number": acc.number,
            "email": acc.email,
            "org": acc.display_tag,
            "isActive": acc.is_active,
            "kind": acc.kind,
            "switchable": acc.switchable,
            "alias": acc.alias,
            "disabled": acc.disabled,
            "threshold": acc.policy.threshold,
            "priority": acc.policy.priority,
            "plan": plan,
            "usage": usage,
            "sentinel": entry.sentinel,
            "sentinelNote": SENTINEL_NOTES.get(entry.sentinel, entry.sentinel)
            if entry.sentinel
            else None,
            "lastSeen": last_seen_note(entry) if entry.sentinel else None,
            "ageS": entry.age_s,
            "fetchedAt": entry.fetched_at,
            "lastError": entry.last_error,
        }

    # -- captured actions -------------------------------------------------------

    def _run(self, fn: Callable[[], object], *, stdin: str = "") -> dict:
        """Run one mutating action, single-flight, with output captured."""
        if not self._action_lock.acquire(blocking=False):
            return {"ok": False, "output": "另一个操作仍在进行中，请稍候。"}
        buf = io.StringIO()
        saved_stdin = sys.stdin
        sys.stdin = io.StringIO(stdin)
        result: object = None
        ok = True
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                try:
                    result = fn()
                except ClaudeSwitchError as e:
                    print(f"Error: {e}")
                    ok = False
                except (ValueError, OSError) as e:
                    print(f"Error: {e}")
                    ok = False
                except EOFError:
                    print("Error: this action asked for interactive input.")
                    ok = False
                except SystemExit as e:
                    # A few core paths sys.exit() on refusal; never let that
                    # take the GUI down.
                    ok = e.code in (0, None)
        finally:
            sys.stdin = saved_stdin
            self._action_lock.release()
        out: dict = {"ok": ok, "output": strip_ansi(buf.getvalue()).strip()}
        if isinstance(result, dict):
            out["payload"] = _jsonable(result)
        elif result is not None and not isinstance(result, bool):
            out["result"] = _jsonable(result)
        return out

    def switch_to(self, number: str, force: bool = False) -> dict:
        return self._run(
            lambda: self._switcher.switch_to(str(number), json_output=True, force=bool(force))
        )

    def switch_strategy(self, strategy: str | None) -> dict:
        """Bare ``switch``: rotate (None), or ``best`` / ``next-available``."""
        from claude_swap.settings import load_settings, parse_model_names

        def go():
            if strategy:
                models = parse_model_names(load_settings(self._switcher.backup_dir).model)
                source = "autoswitch.model" if models else None
            else:
                models, source = (), None
            return self._switcher.switch(
                strategy=strategy or None, json_output=True, models=models, model_source=source
            )

        return self._run(go)

    def add_current(self, slot: int | None = None, alias: str | None = None) -> dict:
        return self._run(
            lambda: self._switcher.add_account(
                slot=_opt_int(slot), assume_yes=True, alias=alias or None
            )
        )

    def add_token(self, token: str, email: str | None = None, slot: int | None = None) -> dict:
        token = (token or "").strip()
        if not token:
            return {"ok": False, "output": "Token 不能为空。"}
        return self._run(
            lambda: self._switcher.add_account_from_token(
                token=token, email=email or None, slot=_opt_int(slot), assume_yes=True
            )
        )

    # -- browser login ------------------------------------------------------------
    #
    # Adds an account without touching the live login: `claude auth login`
    # runs windowless against a throwaway CLAUDE_CONFIG_DIR, then `cswap add`
    # captures that profile in a child process (never by flipping this
    # process's environment, which the snapshot poller and auto-switch engine
    # read). `auth login` opens the browser itself and exits once signed in,
    # so no claude is left behind to rotate the refresh token the slot just
    # stored; the profile is deleted right after capture for the same reason.
    # When the browser cannot reach its local callback, the page shows the
    # printed manual link and passes the code it yields to `auth login`'s
    # stdin.

    def _staging_root(self) -> Path:
        return self._switcher.backup_dir / "login-staging"

    def _staging_dir(self, login_id: str) -> Path:
        if not _LOGIN_ID_RE.fullmatch(login_id or ""):
            raise ValueError("invalid login id")
        return self._staging_root() / login_id

    def login_start(self, email: str | None = None) -> dict:
        from claude_swap.session import AUTH_OVERRIDE_ENV_VARS

        claude = launcher.find_claude()
        if not claude:
            return {"ok": False, "output": "没有找到 claude 命令，请先安装 Claude Code。"}
        email = (email or "").strip()
        if email and not re.fullmatch(r"[^\s\"'&|<>^%!]+@[^\s\"'&|<>^%!]+", email):
            return {"ok": False, "output": "邮箱格式不正确"}
        self._cleanup_staging()
        login_id = f"{int(time.time())}-{secrets.token_hex(4)}"
        staging = self._staging_root() / login_id
        staging.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            os.chmod(staging, 0o700)
        argv = [claude, "auth", "login", "--claudeai"]
        if email:
            argv += ["--email", email]
        env = launcher.child_env(
            {"CLAUDE_CONFIG_DIR": str(staging), "NO_COLOR": "1"},
            # An exported API key or token would bypass the login, and a
            # secure-storage override would send the credential elsewhere.
            (*AUTH_OVERRIDE_ENV_VARS, "CLAUDE_SECURESTORAGE_CONFIG_DIR", "BROWSER"),
        )
        env["PATH"] = launcher.tool_path(claude, env.get("PATH", ""))
        try:
            proc = subprocess.Popen(
                argv,
                env=env,
                cwd=str(staging),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                start_new_session=sys.platform != "win32",
            )
        except OSError as e:
            self._remove_staging(staging)
            return {"ok": False, "output": f"无法启动登录：{e}"}
        with self._logins_lock:
            self._logins[login_id] = _Login(proc, staging)
        return {"ok": True, "id": login_id}

    def login_poll(self, login_id: str) -> dict:
        """``waiting`` while signing in, then ``ready``, ``failed`` or ``gone``."""
        from claude_swap.session import read_config_dir_credentials

        staging = self._staging_dir(login_id)
        with self._logins_lock:
            login = self._logins.get(login_id)
        if login is None or not staging.is_dir():
            return {"state": "gone"}
        code = login.proc.poll()
        if code is None:
            if time.time() - login.started > LOGIN_TIMEOUT_S:
                self.login_cancel(login_id)
                return {"state": "failed", "output": "登录超时，已取消。"}
            return {"state": "waiting", "url": login.url()}
        email = None
        try:
            config = json.loads((staging / ".claude.json").read_text(encoding="utf-8"))
            email = (config.get("oauthAccount") or {}).get("emailAddress")
        except (OSError, ValueError):
            pass
        try:
            creds = read_config_dir_credentials(str(staging))
        except Exception:
            creds = None
        if code == 0 and creds and email:
            return {"state": "ready", "email": email}
        output = strip_ansi(login.text()).strip()
        self.login_cancel(login_id)
        return {"state": "failed", "output": output or f"登录未完成（退出码 {code}）"}

    def login_code(self, login_id: str, code: str) -> dict:
        """Hand the code from the manual-flow page to the waiting `auth login`."""
        self._staging_dir(login_id)  # validates the id
        code = (code or "").strip()
        if not code or any(c.isspace() for c in code):
            return {"ok": False, "output": "授权码不正确"}
        with self._logins_lock:
            login = self._logins.get(login_id)
        if login is None or login.proc.poll() is not None:
            return {"ok": False, "output": "登录已结束"}
        try:
            login.send(code)
        except OSError as e:
            return {"ok": False, "output": f"无法提交授权码：{e}"}
        return {"ok": True, "output": ""}

    def login_finish(
        self, login_id: str, slot: int | None = None, alias: str | None = None, overwrite: bool = False
    ) -> dict:
        from claude_swap.locking import FileLock

        staging = self._staging_dir(login_id)
        if not self._action_lock.acquire(blocking=False):
            return {"ok": False, "output": "另一个操作仍在进行中，请稍候。"}
        try:
            before = (self._switcher._get_sequence_data() or {}).get("activeAccountNumber")
            argv = launcher.cli_capture_command() + ["add"]
            if slot not in (None, ""):
                argv += ["--slot", str(int(slot))]
            if alias:
                argv += ["--alias", alias]
            env = launcher.child_env(
                {"CLAUDE_CONFIG_DIR": str(staging), "NO_COLOR": "1"},
                ("CLAUDE_SECURESTORAGE_CONFIG_DIR",),
            )
            try:
                proc = subprocess.run(
                    argv,
                    env=env,
                    input="y\n" if overwrite else "",  # the slot-overwrite prompt
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=180,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                ok, output = proc.returncode == 0, proc.stdout + proc.stderr
            except (OSError, subprocess.TimeoutExpired) as e:
                ok, output = False, f"Error: {e}"
            # `add` records the captured account as active, but the live login
            # never moved; put the record back.
            with FileLock(self._switcher.lock_file):
                data = self._switcher._get_sequence_data()
                if data is not None and data.get("activeAccountNumber") != before:
                    data["activeAccountNumber"] = before
                    self._switcher._write_json(self._switcher.sequence_file, data)
        finally:
            self._action_lock.release()
        if ok:
            with self._logins_lock:
                self._logins.pop(login_id, None)
            self._remove_staging(staging)
        return {"ok": ok, "output": strip_ansi(output).strip()}

    def login_cancel(self, login_id: str) -> dict:
        staging = self._staging_dir(login_id)
        with self._logins_lock:
            login = self._logins.pop(login_id, None)
        if login is not None:
            login.kill()
        self._remove_staging(staging)
        return {"ok": True, "output": ""}

    def _remove_staging(self, staging: Path) -> None:
        from claude_swap.session import delete_macos_keychain_entry

        delete_macos_keychain_entry(staging)
        shutil.rmtree(staging, ignore_errors=True)

    def _cleanup_staging(self, max_age_s: float = 3600) -> None:
        """Drop abandoned staging profiles (a crash or a closed window)."""
        root = self._staging_root()
        if not root.is_dir():
            return
        with self._logins_lock:
            running = set(self._logins)
        for child in root.iterdir():
            try:
                if child.name not in running and time.time() - child.stat().st_mtime > max_age_s:
                    self._remove_staging(child)
            except OSError:
                pass

    def remove(self, number: str) -> dict:
        return self._run(lambda: self._switcher.remove_account(str(number), assume_yes=True))

    def set_disabled(self, number: str, disabled: bool) -> dict:
        return self._run(lambda: self._switcher.set_account_disabled(str(number), bool(disabled)))

    def set_alias(self, number: str, alias: str | None) -> dict:
        if alias:
            return self._run(lambda: self._switcher.set_alias(str(number), alias))
        return self._run(lambda: self._switcher.unset_alias(str(number)))

    def set_threshold(self, number: str, value: str | float | None) -> dict:
        value = None if value in (None, "") else value
        return self._run(lambda: self._switcher.set_account_threshold(str(number), value))

    def set_priority(self, number: str, value: str | int | None) -> dict:
        value = None if value in (None, "") else value
        return self._run(lambda: self._switcher.set_account_priority(str(number), value))

    def move(self, number: str, target: str) -> dict:
        return self._run(lambda: self._switcher.move_account(str(number), str(target)))

    def swap(self, first: str, second: str) -> dict:
        return self._run(lambda: self._switcher.swap_accounts(str(first), str(second)))

    def status_text(self) -> dict:
        return self._run(lambda: self._switcher.status())

    def token_status_text(self) -> dict:
        return self._run(lambda: self._switcher.list_accounts(show_token_status=True))

    def purge_all(self) -> dict:
        # purge() asks for its own y/N; the page has already confirmed twice.
        return self._run(self._switcher.purge, stdin="y\n")

    # -- session mode -----------------------------------------------------------

    def run_session(
        self,
        number: str | None,
        share: bool = True,
        share_history: bool | None = None,
        require_session: bool = False,
        extra_args: str = "",
        cwd: str | None = None,
    ) -> dict:
        """Open a terminal running ``cswap run`` — session mode owns a TTY."""
        args = ["run"]
        if number:
            args.append(str(number))
        if not share:
            args.append("--no-share")
        if share_history is True:
            args.append("--share-history")
        elif share_history is False:
            args.append("--no-share-history")
        if require_session:
            args.append("--require-session")
        extra = launcher.split_args(extra_args or "")
        if extra:
            args += ["--", *extra]
        try:
            shown = launcher.open_terminal(launcher.cli_command() + args, cwd=cwd or None)
        except launcher.LaunchError as e:
            return {"ok": False, "output": str(e)}
        return {"ok": True, "output": f"已在新终端中启动：{shown}"}

    # -- directory mappings -----------------------------------------------------

    def mappings(self) -> list[dict]:
        from claude_swap.mappings import MappingStore

        seq = self._switcher._get_sequence_data_migrated() or {}
        rows = []
        for path, entry in sorted(MappingStore(self._switcher.backup_dir).all().items()):
            email = entry.get("email", "")
            org_uuid = entry.get("organizationUuid", "") or ""
            slot = self._switcher._find_account_slot(seq, email, org_uuid)
            rows.append({"path": path, "email": email, "number": slot})
        return rows

    def map_dir(self, number: str, path: str) -> dict:
        from claude_swap.mappings import MappingStore, normalize_path

        def go():
            if not path:
                raise ValueError("请选择一个目录")
            account_num, email, org_uuid = self._switcher.resolve_account(str(number))
            if not os.path.isdir(path):
                print(f"Warning: {path} is not an existing directory (mapping it anyway)")
            MappingStore(self._switcher.backup_dir).set(path, email, org_uuid)
            print(f"Mapped {normalize_path(path)} → Account-{account_num} ({email})")

        return self._run(go)

    def unmap_dir(self, path: str) -> dict:
        from claude_swap.mappings import MappingStore, normalize_path

        def go():
            if MappingStore(self._switcher.backup_dir).remove(path):
                print(f"Unmapped {normalize_path(path)}")
            else:
                print(f"No mapping for {normalize_path(path)}")

        return self._run(go)

    # -- settings -----------------------------------------------------------------

    def settings(self) -> list[dict]:
        from claude_swap.settings import effective_settings

        rows = []
        for spec, value, is_set in effective_settings(self._switcher.backup_dir):
            rows.append(
                {
                    "key": spec.dotted,
                    "kind": spec.kind,
                    "value": value,
                    "isSet": is_set,
                    "default": spec.default,
                    "lo": spec.lo,
                    "hi": spec.hi,
                    "choices": list(spec.choices),
                    "help": spec.help,
                }
            )
        return rows

    def set_setting(self, key: str, value: str) -> dict:
        from claude_swap.settings import format_setting_value, set_setting

        def go():
            stored = set_setting(self._switcher.backup_dir, key, str(value))
            print(f"{key} = {format_setting_value(stored)}")

        result = self._run(go)
        if result["ok"] and key.startswith("autoswitch."):
            self._restart_engine()
        return result

    def unset_setting(self, key: str) -> dict:
        from claude_swap.settings import format_setting_value, setting_spec, unset_setting

        def go():
            if unset_setting(self._switcher.backup_dir, key):
                print(f"{key} unset (default: {format_setting_value(setting_spec(key).default)})")
            else:
                print(f"{key} is not set; nothing to do")

        result = self._run(go)
        if result["ok"] and key.startswith("autoswitch."):
            self._restart_engine()
        return result

    # -- backup / migration -------------------------------------------------------

    def export_accounts(self, path: str, account: str | None = None, full: bool = False) -> dict:
        from claude_swap.transfer import export_accounts

        return self._run(
            lambda: export_accounts(self._switcher, path, account=account or None, full=bool(full))
        )

    def import_accounts(self, path: str, force: bool = False) -> dict:
        from claude_swap.transfer import import_accounts

        return self._run(lambda: import_accounts(self._switcher, path, force=bool(force)))

    def import_usage(self, path: str, hold: float | None = None) -> dict:
        from claude_swap.transfer import import_usage

        hold_s = None if hold in (None, "") else float(hold)
        return self._run(lambda: import_usage(self._switcher, path, hold_s=hold_s))

    def unclaimed(self) -> list[dict]:
        entries = self._switcher.list_unclaimed_credentials()
        return [
            {
                "id": entry_id,
                "slot": meta.get("configSlot") or "?",
                "reason": meta.get("reason") or "orphaned (no manifest row)",
            }
            for entry_id, meta in sorted(entries.items())
        ]

    def purge_unclaimed(self, entry_id: str) -> dict:
        def go():
            if entry_id not in self._switcher.list_unclaimed_credentials():
                raise ValueError(f"no unclaimed entry {entry_id}")
            self._switcher._store._remove_unclaimed_credential(entry_id)
            print(f"Purged {entry_id}")

        return self._run(go)

    # -- auto-switch engine -------------------------------------------------------

    def _engine_state(self) -> dict:
        return {"running": self._engine is not None, "dryRun": self._engine_dry_run}

    def engine_start(self, dry_run: bool = False) -> dict:
        from claude_swap.autoswitch import AutoSwitchEngine
        from claude_swap.settings import load_settings

        if self._engine is not None:
            return {"ok": True, "output": "自动切换已在运行。", "engine": self._engine_state()}
        try:
            engine = AutoSwitchEngine(
                self._switcher,
                load_settings(self._switcher.backup_dir),
                self._on_engine_event,
                dry_run=bool(dry_run),
            )
        except Exception as e:
            return {"ok": False, "output": f"自动切换启动失败：{e}"}
        self._engine = engine
        self._engine_dry_run = bool(dry_run)
        threading.Thread(target=self._run_engine, args=(engine,), daemon=True).start()
        self._push_event({"event": "gui", "text": "自动切换已启动" + ("（演练模式）" if dry_run else "")})
        return {"ok": True, "output": "", "engine": self._engine_state()}

    def engine_stop(self) -> dict:
        engine, self._engine = self._engine, None
        if engine is not None:
            engine.stop()
            self._push_event({"event": "gui", "text": "自动切换已停止"})
        return {"ok": True, "output": "", "engine": self._engine_state()}

    def _restart_engine(self) -> None:
        if self._engine is not None:
            dry_run = self._engine_dry_run
            self.engine_stop()
            self.engine_start(dry_run)

    def _run_engine(self, engine) -> None:
        try:
            engine.run_loop()
        except Exception as e:
            self._push_event({"event": "error", "text": f"自动切换引擎异常退出：{e}"})
            if self._engine is engine:
                self._engine = None

    def _on_engine_event(self, event) -> None:
        # Engine thread; must not raise.
        try:
            data = event.to_json()
            data["text"] = event.human()
        except Exception:
            data = {"event": getattr(event, "kind", "event"), "text": str(event)}
        self._push_event(data)

    def _push_event(self, data: dict) -> None:
        with self._events_lock:
            self._event_seq += 1
            data = dict(_jsonable(data))
            data["seq"] = self._event_seq
            data.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S"))
            self._events.append(data)

    def events(self, since: int = 0) -> dict:
        with self._events_lock:
            items = [e for e in self._events if e["seq"] > int(since or 0)]
            return {"seq": self._event_seq, "events": items, "engine": self._engine_state()}

    def shutdown(self) -> None:
        if self._engine is not None:
            self._engine.stop()
            self._engine = None
        with self._logins_lock:
            pending = list(self._logins)
        for login_id in pending:
            self.login_cancel(login_id)

    # -- Claude Desktop accounts ---------------------------------------------------
    #
    # Kept apart from the Claude Code accounts above: a Desktop switch never
    # touches the CLI login and a CLI switch never touches Desktop. The one
    # link is display: a uuid the CLI also manages lends its email.

    def _desktop_store(self) -> desktop.DesktopStore:
        return desktop.DesktopStore(self._switcher.backup_dir / "desktop")

    def _desktop_do(self, fn: Callable[[Path, desktop.DesktopStore], dict | None]) -> dict:
        """Run one Desktop action, single-flight, with errors as output."""
        if not self._desktop_lock.acquire(blocking=False):
            return {"ok": False, "output": "另一个 Desktop 操作仍在进行中，请稍候。"}
        try:
            data_dir = desktop.find_data_dir()
            if data_dir is None:
                raise desktop.DesktopError("没有找到 Claude Desktop 的数据，请先安装并打开一次 Claude Desktop")
            return {"ok": True, "output": "", **(fn(data_dir, self._desktop_store()) or {})}
        except (desktop.DesktopError, OSError, ValueError) as e:
            return {"ok": False, "output": str(e)}
        finally:
            self._desktop_lock.release()

    def _cli_emails(self) -> dict[str, str]:
        data = self._switcher._get_sequence_data() or {}
        return {
            acc["uuid"]: acc.get("email")
            for acc in (data.get("accounts") or {}).values()
            if isinstance(acc, dict) and acc.get("uuid")
        }

    def _desktop_register(self, store: desktop.DesktopStore, uuid: str, name: str | None = None) -> dict:
        accounts = store.load()
        record = accounts.setdefault(uuid, {"addedAt": time.time()})
        if name:
            record["name"] = name.strip()
        email = self._cli_emails().get(uuid)
        if email:
            record["email"] = email
        store.save(accounts)
        return record

    def _desktop_capture(self, data_dir: Path, store: desktop.DesktopStore) -> str | None:
        """Save the live sign-in (Desktop closed); an unknown one is added."""
        uuid = store.capture(data_dir)
        if uuid:
            self._desktop_register(store, uuid)
        return uuid

    def desktop_state(self) -> dict:
        data_dir = desktop.find_data_dir()
        store = self._desktop_store()
        accounts = store.load()
        emails = self._cli_emails()
        live = desktop.signed_in_uuid(desktop.read_config(data_dir)) if data_dir else None
        fingerprint = desktop.key_fingerprint(data_dir) if data_dir else None
        rows = []
        for uuid, record in accounts.items():
            meta = store.snapshot_meta(uuid)
            rows.append({
                "uuid": uuid,
                "name": record.get("name"),
                "email": record.get("email") or emails.get(uuid),
                "isLive": uuid == live,
                "savedAt": meta.get("savedAt") if meta else None,
                "stale": bool(meta) and meta.get("keyFingerprint") != fingerprint,
                "addedAt": record.get("addedAt") or 0,
            })
        rows.sort(key=lambda r: r["addedAt"])
        return {
            "available": data_dir is not None,
            "dataDir": str(data_dir) if data_dir else None,
            "running": bool(data_dir) and bool(desktop.running_pids()),
            "live": live and {
                "uuid": live,
                "email": emails.get(live) or (accounts.get(live) or {}).get("email"),
                "known": live in accounts,
            },
            "accounts": rows,
        }

    def desktop_import_current(self, name: str | None = None) -> dict:
        def go(data_dir: Path, store: desktop.DesktopStore) -> dict:
            uuid = desktop.signed_in_uuid(desktop.read_config(data_dir))
            if uuid is None:
                raise desktop.DesktopError("Claude Desktop 当前没有登录")
            record = self._desktop_register(store, uuid, name)
            if not desktop.running_pids():
                store.capture(data_dir)  # closed: the files are settled, save now
            return {"uuid": uuid, "email": record.get("email")}

        return self._desktop_do(go)

    def desktop_switch(self, uuid: str) -> dict:
        def go(data_dir: Path, store: desktop.DesktopStore) -> dict:
            if uuid not in store.load():
                raise desktop.DesktopError("未知的 Desktop 账号")
            if desktop.signed_in_uuid(desktop.read_config(data_dir)) == uuid:
                if not desktop.running_pids():
                    desktop.launch_desktop(data_dir)
                return {"same": True}
            store.check(data_dir, uuid)  # before closing anything
            desktop.quit_desktop()
            saved = self._desktop_capture(data_dir, store)
            try:
                store.restore(data_dir, uuid)
            except Exception:
                if saved:
                    store.restore(data_dir, saved)
                raise
            finally:
                desktop.launch_desktop(data_dir)
            return {"same": False}

        return self._desktop_do(go)

    def desktop_login_start(self) -> dict:
        """Restart Desktop signed out; the sign-in it then does is the new account."""

        def go(data_dir: Path, store: desktop.DesktopStore) -> dict:
            desktop.quit_desktop()
            previous = self._desktop_capture(data_dir, store)
            desktop.clear(data_dir)
            desktop.launch_desktop(data_dir)
            self._desktop_login = {"dataDir": data_dir, "previous": previous}
            return {}

        return self._desktop_do(go)

    def desktop_login_poll(self) -> dict:
        login = self._desktop_login
        if login is None:
            return {"state": "gone"}
        uuid = desktop.signed_in_uuid(desktop.read_config(login["dataDir"]))
        if uuid is None:
            return {"state": "waiting", "running": bool(desktop.running_pids())}
        record = self._desktop_store().load().get(uuid) or {}
        return {
            "state": "ready",
            "uuid": uuid,
            "email": self._cli_emails().get(uuid) or record.get("email"),
            "known": bool(record),
            "name": record.get("name"),
        }

    def desktop_login_finish(self, name: str | None = None) -> dict:
        def go(data_dir: Path, store: desktop.DesktopStore) -> dict:
            uuid = desktop.signed_in_uuid(desktop.read_config(data_dir))
            if uuid is None:
                raise desktop.DesktopError("Claude Desktop 还没有登录")
            self._desktop_login = None
            record = self._desktop_register(store, uuid, name)
            return {"uuid": uuid, "email": record.get("email")}

        return self._desktop_do(go)

    def desktop_login_cancel(self) -> dict:
        """Give up on the new sign-in and put the previous account back."""
        login, self._desktop_login = self._desktop_login, None

        def go(data_dir: Path, store: desktop.DesktopStore) -> dict:
            previous = login and login["previous"]
            if not previous or desktop.signed_in_uuid(desktop.read_config(data_dir)) == previous:
                return {}
            desktop.quit_desktop()
            self._desktop_capture(data_dir, store)
            store.restore(data_dir, previous)
            desktop.launch_desktop(data_dir)
            return {}

        return self._desktop_do(go)

    def desktop_rename(self, uuid: str, name: str) -> dict:
        def go(data_dir: Path, store: desktop.DesktopStore) -> dict:
            accounts = store.load()
            if uuid not in accounts:
                raise desktop.DesktopError("未知的 Desktop 账号")
            if (name or "").strip():
                accounts[uuid]["name"] = name.strip()
            else:
                accounts[uuid].pop("name", None)
            store.save(accounts)
            return {}

        return self._desktop_do(go)

    def desktop_remove(self, uuid: str) -> dict:
        """Forget an account; a live sign-in stays signed in."""

        def go(data_dir: Path, store: desktop.DesktopStore) -> dict:
            accounts = store.load()
            store.snapshot_dir(uuid)  # validates
            accounts.pop(uuid, None)
            store.save(accounts)
            store.drop(uuid)
            return {}

        return self._desktop_do(go)

    def desktop_launch(self) -> dict:
        def go(data_dir: Path, store: desktop.DesktopStore) -> dict:
            if not desktop.running_pids():
                desktop.launch_desktop(data_dir)
            return {}

        return self._desktop_do(go)

    # -- tray menu (underscored: not exposed to the page) ---------------------------

    def _tray_cli_accounts(self) -> list[dict]:
        data = self._switcher._get_sequence_data() or {}
        active = str(data.get("activeAccountNumber"))
        rows = []
        for number, acc in sorted((data.get("accounts") or {}).items(), key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 0):
            if not isinstance(acc, dict):
                continue
            label = f"{number}. {acc.get('email') or '?'}"
            if acc.get("alias"):
                label += f" (@{acc['alias']})"
            rows.append({
                "number": str(number),
                "label": label,
                "active": str(number) == active,
                "disabled": bool(acc.get("disabled")),
            })
        return rows

    def _tray_desktop_live(self) -> dict | None:
        """Desktop's sign-in when it is not one of the managed accounts yet."""
        live = self.desktop_state()["live"]
        if not live or live["known"]:
            return None
        return {"label": live["email"] or f"账号（{live['uuid'][:8]}）"}

    def _tray_desktop_accounts(self) -> list[dict] | None:
        """None when Claude Desktop is not installed here."""
        state = self.desktop_state()
        if not state["available"]:
            return None
        return [
            {
                "uuid": a["uuid"],
                "label": a["name"] or a["email"] or f"未命名账号（{a['uuid'][:8]}）",
                "live": a["isLive"],
                "switchable": not a["isLive"] and bool(a["savedAt"]) and not a["stale"],
            }
            for a in state["accounts"]
        ]

    # -- desktop helpers ----------------------------------------------------------

    def pick_path(self, kind: str, filename: str = "") -> str | None:
        """Native open/save/folder dialog; None when unavailable or cancelled.

        The browser shell has no native dialogs; the page then asks for a
        typed path instead.
        """
        if self._window is None:
            return None
        import webview

        dialog = {
            "open": webview.FileDialog.OPEN,
            "save": webview.FileDialog.SAVE,
            "folder": webview.FileDialog.FOLDER,
        }[kind]
        picked = self._window.create_file_dialog(dialog, save_filename=filename or "")
        if not picked:
            return None
        return picked if isinstance(picked, str) else picked[0]

    def open_folder(self, path: str | None = None) -> dict:
        target = path or str(self._switcher.backup_dir)
        try:
            launcher.reveal(target)
        except launcher.LaunchError as e:
            return {"ok": False, "output": str(e)}
        return {"ok": True, "output": ""}

    def open_url(self, url: str) -> dict:
        import webbrowser

        if not url.startswith("https://"):
            return {"ok": False, "output": "只允许打开 https 链接"}
        webbrowser.open(url)
        return {"ok": True, "output": ""}

    def copy_text(self, text: str) -> dict:
        """System clipboard, for pages whose origin has no Clipboard API."""
        try:
            if sys.platform == "win32":
                _win_set_clipboard(str(text))
            elif sys.platform == "darwin":
                subprocess.run(["pbcopy"], input=str(text).encode("utf-8"), check=True, timeout=10)
            else:
                return {"ok": False, "output": "不支持的平台"}
        except (OSError, subprocess.SubprocessError) as e:
            return {"ok": False, "output": str(e)}
        return {"ok": True, "output": ""}

    def check_update(self) -> dict:
        try:
            req = urllib.request.Request(
                RELEASES_API, headers={"Accept": "application/vnd.github+json"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            return {"ok": False, "output": f"检查更新失败：{e}"}
        latest = str(data.get("tag_name") or "").lstrip("v")
        return {
            "ok": True,
            "current": __version__,
            "latest": latest,
            "newer": bool(latest) and latest != __version__,
            "url": data.get("html_url") or RELEASES_PAGE,
        }


def _opt_int(value) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _jsonable(value):
    """Best-effort conversion to something json.dumps accepts."""
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        pass
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    return str(value)
