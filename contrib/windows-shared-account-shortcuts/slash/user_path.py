"""User PATH plan for CMD launchers; no PowerShell execution-policy changes."""
from __future__ import annotations
import ctypes
import json
import os
import winreg
from pathlib import Path

def read_user_path() -> tuple[str, int]:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
        try:
            value, kind = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            return "", winreg.REG_EXPAND_SZ
    if not isinstance(value, str) or kind not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
        raise ValueError("The user PATH is not a supported string; preserved.")
    return value, kind

def normalized(value: str) -> str:
    return os.path.expandvars(value.strip().strip('"')).rstrip("\\/").casefold()

def path_plan(home: Path, *, uninstall: bool = False) -> dict:
    before = read_user_path()
    entry = str(home / "bin")
    receipt = home / ".claude-swap-backup" / "account-launcher-path.json"
    receipt_before = receipt.read_bytes() if receipt.exists() else None
    owned = False
    if receipt_before is not None:
        record = json.loads(receipt_before.decode("utf-8"))
        if record.get("owner") != "claude-swap-account-launchers" or record.get("entry") != entry or not isinstance(record.get("added"), bool):
            raise ValueError("An unrecognized PATH receipt exists; preserved.")
        owned = record["added"]
    entries = before[0].split(";")
    present = any(normalized(part) == normalized(entry) for part in entries)
    after_value = before[0]
    if uninstall:
        if owned:
            # Remove only the one literal entry this installer added.
            for index in range(len(entries) - 1, -1, -1):
                if entries[index].strip().casefold() == entry.casefold():
                    del entries[index]
                    after_value = ";".join(entries)
                    break
        receipt_after = None
    else:
        if not present:
            after_value = before[0] + ("" if not before[0] or before[0].endswith(";") else ";") + entry + ";"
            owned = True
        record = {"owner": "claude-swap-account-launchers", "entry": entry, "added": owned}
        receipt_after = (json.dumps(record, indent=2) + "\n").encode("utf-8")
    return {"before": before, "after": (after_value, before[1]), "present": present,
            "receipt": receipt, "receipt_before": receipt_before, "receipt_after": receipt_after}

def write_user_path(before: tuple[str, int], after: tuple[str, int]) -> None:
    if read_user_path() != before:
        raise ValueError("User PATH changed during installation; preserved.")
    if before == after:
        return
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "Path", 0, after[1], after[0])

def notify_environment() -> None:
    # Broadcast is best effort; existing terminal processes may retain their PATH.
    try:
        from ctypes import wintypes
        notify = ctypes.windll.user32.SendMessageTimeoutW
        notify.argtypes = (wintypes.HWND, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR,
                           wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t))
        notify.restype = ctypes.c_ssize_t
        result = ctypes.c_size_t()
        notify(0xFFFF, 0x1A, 0, "Environment", 2, 3000, ctypes.byref(result))
    except (OSError, AttributeError):
        pass
