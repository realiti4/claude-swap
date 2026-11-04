"""Claude Desktop sign-in switching.

Claude Desktop (the Electron app) keeps its claude.ai sign-in in its own
profile, apart from Claude Code: OAuth tokens in ``config.json`` (``oauth:*``,
encrypted with Electron safeStorage) and the web session in the claude.ai
cookies and site storage. A switch swaps exactly those, with Desktop shut
down, and leaves everything else in the profile alone: settings, Cowork data,
MCP config, caches, and ``Local State`` with its encryption key.

Nothing is decrypted. A snapshot holds the same ciphertext Desktop wrote,
which only this OS user on this machine can open. A snapshot taken under
another key (Desktop reset, or its data deleted) would restore as garbage, so
each one records a fingerprint of the key and is refused when it no longer
matches.

The live sign-in belongs to whichever account its ``lastKnownAccountUuid``
names, never to a remembered "active" mark, so an account switched inside
Desktop itself is still saved under the right name.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

CONFIG = "config.json"
ACCOUNT_KEY = "lastKnownAccountUuid"
# The session that follows the account: claude.ai cookies and site storage.
# Older Electron keeps Cookies at the top of the profile, newer under Network.
SESSION_PATHS = (
    "Network/Cookies",
    "Network/Cookies-journal",
    "Cookies",
    "Cookies-journal",
    "Local Storage",
    "Session Storage",
    "IndexedDB",
)
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class DesktopError(Exception):
    """A Desktop operation that cannot go ahead; the message is user-facing."""


def is_account_key(key: str) -> bool:
    return key == ACCOUNT_KEY or key.startswith("oauth:")


# -- the Desktop profile ----------------------------------------------------------


def _candidates() -> list[Path]:
    if sys.platform == "darwin":
        return [Path.home() / "Library" / "Application Support" / "Claude"]
    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        roaming = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        # Store (MSIX) builds keep their data outside the package now; older
        # ones inside its virtualized AppData; the classic installer in Roaming.
        found = sorted((local / "Claude-Data").glob("claude_*"))
        found += sorted(local.glob("Packages/Claude_*/LocalCache/Roaming/Claude"))
        found.append(roaming / "Claude")
        return found
    return []


def find_data_dir() -> Path | None:
    """The profile Desktop signs in with: signed in first, then most recent."""
    profiles = [d for d in _candidates() if (d / "Local State").is_file() and (d / CONFIG).is_file()]
    if not profiles:
        return None
    signed_in = [d for d in profiles if signed_in_uuid(read_config(d))]
    return max(signed_in or profiles, key=lambda d: (d / CONFIG).stat().st_mtime)


def read_config(data_dir: Path) -> dict:
    try:
        config = json.loads((data_dir / CONFIG).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return config if isinstance(config, dict) else {}


def _write_config(data_dir: Path, config: dict) -> None:
    path = data_dir / CONFIG
    tmp = path.with_name(CONFIG + ".cswap-tmp")
    tmp.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def signed_in_uuid(config: dict) -> str | None:
    """The account the profile is signed in as, or None when signed out."""
    uuid = config.get(ACCOUNT_KEY)
    if not isinstance(uuid, str) or not UUID_RE.fullmatch(uuid):
        return None
    has_token = any(k.startswith("oauth:token") and v for k, v in config.items())
    return uuid if has_token else None


def key_fingerprint(data_dir: Path) -> str | None:
    """Fingerprint of the key Desktop encrypts with (Windows: in Local State).

    macOS keeps the key in the Keychain, where it survives a data reset, so
    there is nothing to compare and None is returned.
    """
    try:
        state = json.loads((data_dir / "Local State").read_text(encoding="utf-8"))
        key = state["os_crypt"]["encrypted_key"]
        return hashlib.sha256(base64.b64decode(key)).hexdigest()[:16]
    except (OSError, ValueError, KeyError, TypeError):
        return None


USAGE_FILE = "plan-usage-history.json"


def latest_usage(data_dir: Path) -> dict[str, dict]:
    """Newest non-empty reading per organization from Desktop's own usage log.

    Desktop samples the plan's limits every quarter hour while signed in:
    ``{"t": <ms>, "org": <uuid>, "u": {"fh": <5h %>, "sd": <7d %>}}``. The
    limits belong to the account, so they are the same numbers the CLI
    reads; this is the source for accounts the CLI does not manage.
    """
    try:
        data = json.loads((data_dir / USAGE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    newest: dict[str, dict] = {}
    for sample in (data.get("samples") if isinstance(data, dict) else None) or []:
        if not isinstance(sample, dict):
            continue
        org, t, u = sample.get("org"), sample.get("t"), sample.get("u")
        if not isinstance(org, str) or not isinstance(t, (int, float)) or not isinstance(u, dict) or not u:
            continue
        if org not in newest or t >= newest[org]["t"]:
            newest[org] = sample
    return newest


def usage_view(sample: dict) -> dict:
    """A Desktop sample in the shape the page uses for CLI usage."""
    view = {}
    for key, name in (("fh", "fiveHour"), ("sd", "sevenDay")):
        pct = sample["u"].get(key)
        if isinstance(pct, (int, float)) and not isinstance(pct, bool):
            view[name] = {"pct": float(pct)}
    return view


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def _copy(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dest)
    else:
        shutil.copy2(src, dest)


# -- snapshots --------------------------------------------------------------------


class DesktopStore:
    """``<backup>/desktop``: ``accounts.json`` plus one snapshot per account."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.file = root / "accounts.json"
        self.snapshots = root / "snapshots"

    def load(self) -> dict[str, dict]:
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        accounts = data.get("accounts") if isinstance(data, dict) else None
        return accounts if isinstance(accounts, dict) else {}

    def save(self, accounts: dict[str, dict]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            os.chmod(self.root, 0o700)
        tmp = self.file.with_name("accounts.json.tmp")
        tmp.write_text(json.dumps({"version": 1, "accounts": accounts}, indent=2), encoding="utf-8")
        os.replace(tmp, self.file)

    def snapshot_dir(self, uuid: str) -> Path:
        if not UUID_RE.fullmatch(uuid or ""):
            raise DesktopError("无效的账号")
        return self.snapshots / uuid

    def snapshot_meta(self, uuid: str) -> dict | None:
        try:
            return json.loads((self.snapshot_dir(uuid) / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def capture(self, data_dir: Path) -> str | None:
        """Save the live sign-in under its account. Desktop must be closed."""
        config = read_config(data_dir)
        uuid = signed_in_uuid(config)
        if uuid is None:
            return None
        final = self.snapshot_dir(uuid)
        tmp = final.with_name(uuid + ".tmp")
        _remove(tmp)
        tmp.mkdir(parents=True)
        (tmp / CONFIG).write_text(
            json.dumps({k: v for k, v in config.items() if is_account_key(k)}), encoding="utf-8"
        )
        for rel in SESSION_PATHS:
            if (data_dir / rel).exists():
                _copy(data_dir / rel, tmp / "files" / rel)
        meta = {"savedAt": time.time(), "keyFingerprint": key_fingerprint(data_dir)}
        (tmp / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        _remove(final)
        os.replace(tmp, final)
        return uuid

    def check(self, data_dir: Path, uuid: str) -> None:
        """Raise unless ``uuid``'s snapshot can be restored into ``data_dir``."""
        meta = self.snapshot_meta(uuid)
        if meta is None:
            raise DesktopError("这个账号还没有保存的登录，请重新通过浏览器登录添加")
        if meta.get("keyFingerprint") != key_fingerprint(data_dir):
            raise DesktopError("Claude Desktop 的数据已被重置，这个账号保存的登录已失效，请重新登录添加")

    def restore(self, data_dir: Path, uuid: str) -> None:
        """Make ``uuid``'s snapshot the live sign-in. Desktop must be closed."""
        self.check(data_dir, uuid)
        snap = self.snapshot_dir(uuid)
        try:
            saved = json.loads((snap / CONFIG).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise DesktopError(f"保存的登录已损坏：{e}") from e
        clear(data_dir)
        config = read_config(data_dir)
        config.update({k: v for k, v in saved.items() if is_account_key(k)})
        _write_config(data_dir, config)
        for rel in SESSION_PATHS:
            if (snap / "files" / rel).exists():
                _copy(snap / "files" / rel, data_dir / rel)

    def drop(self, uuid: str) -> None:
        _remove(self.snapshot_dir(uuid))


def clear(data_dir: Path) -> None:
    """Sign the profile out: drop the tokens and the claude.ai session."""
    config = read_config(data_dir)
    _write_config(data_dir, {k: v for k, v in config.items() if not is_account_key(k)})
    for rel in SESSION_PATHS:
        _remove(data_dir / rel)


# -- the Desktop process ----------------------------------------------------------


def _is_desktop_exe(path: str) -> bool:
    # Claude Code is claude.exe too: only the Desktop install locations count.
    p = path.replace("/", "\\").lower()
    return p.endswith("\\claude.exe") and ("\\windowsapps\\claude_" in p or "\\anthropicclaude\\" in p)


def _win_desktop_pids() -> list[int]:
    import ctypes
    from ctypes import wintypes

    psapi = ctypes.WinDLL("psapi")
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
    )
    pids = (wintypes.DWORD * 8192)()
    needed = wintypes.DWORD()
    if not psapi.EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(needed)):
        return []
    found = []
    for pid in pids[: needed.value // ctypes.sizeof(wintypes.DWORD)]:
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            continue
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                if _is_desktop_exe(buf.value):
                    found.append(int(pid))
        finally:
            kernel32.CloseHandle(handle)
    return found


def running_pids() -> list[int]:
    if sys.platform == "win32":
        return _win_desktop_pids()
    if sys.platform == "darwin":
        # The main process is named exactly "Claude"; helpers and the
        # Claude Code CLI (lowercase) are not.
        out = subprocess.run(["pgrep", "-x", "Claude"], capture_output=True, text=True)
        return [int(p) for p in out.stdout.split()]
    return []


def quit_desktop(timeout: float = 15.0) -> bool:
    """Force Desktop closed. Returns whether it was running."""
    pids = running_pids()
    if not pids:
        return False
    if sys.platform == "win32":
        for pid in pids:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, creationflags=_NO_WINDOW)
    else:
        subprocess.run(["osascript", "-e", 'quit app "Claude"'], capture_output=True)
        deadline = time.time() + 8
        while running_pids() and time.time() < deadline:
            time.sleep(0.3)
        for pid in running_pids():
            subprocess.run(["kill", "-9", str(pid)], capture_output=True)
    deadline = time.time() + timeout
    while running_pids():
        if time.time() > deadline:
            raise DesktopError("无法关闭 Claude Desktop，请手动退出后重试")
        time.sleep(0.3)
    time.sleep(0.8)  # let the OS release its file handles
    return True


def _store_app_id(data_dir: Path) -> str | None:
    """AUMID of a Store build, from the publisher hash in its data path."""
    for part in (data_dir.name, data_dir.parent.parent.parent.name):
        match = re.fullmatch(r"(?i)claude_([0-9a-z]{13})", part)
        if match:
            return f"Claude_{match.group(1)}!Claude"
    return None


def launch_desktop(data_dir: Path) -> None:
    if sys.platform == "darwin":
        if subprocess.run(["open", "-a", "Claude"], capture_output=True).returncode != 0:
            raise DesktopError("无法启动 Claude Desktop")
        return
    if sys.platform != "win32":
        raise DesktopError("桌面版仅支持 Windows 和 macOS")
    app_id = _store_app_id(data_dir)
    if app_id:
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app_id}"], creationflags=_NO_WINDOW)
        return
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    exe = local / "AnthropicClaude" / "claude.exe"
    if not exe.is_file():
        raise DesktopError("找不到 Claude Desktop，请手动启动")
    subprocess.Popen([str(exe)], creationflags=_NO_WINDOW)
