"""Threshold-driven automatic account switching for Claude Swap.

`ccs autoswitch` lets you set a usage-% threshold (a common/global one plus
optional per-account overrides) and run a background watcher that polls each
account's quota and swaps to the freshest account when the active one crosses
its threshold — so you keep working past a single account's limit without
switching by hand.

The actual switching is delegated to the existing, well-tested
``ClaudeAccountSwitcher.switch(strategy="best")``; this module only adds the
*gate* (don't switch until the active account is over its threshold), the
persisted rule set, the wizard, and the detached watcher process.

Caveat: ccs swaps the credentials file Claude Code reads, so a swap fully takes
effect on Claude's *next* start — a session already running keeps the
credentials it loaded.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from claude_swap import oauth
from claude_swap.exceptions import ConfigError
from claude_swap.models import get_timestamp
from claude_swap.printer import accent, bolded, dimmed, muted, warning
from claude_swap.process_detection import is_pid_alive

if TYPE_CHECKING:
    from claude_swap.switcher import ClaudeAccountSwitcher

# Default rule set. Any field missing from autoswitch.json falls back to these.
DEFAULTS: dict = {
    "enabled": False,
    "globalThreshold": 85,  # switch when max(5h%, 7d%) >= this
    "overrides": {},  # account number (str) -> usage %
    "interval": 600,  # watcher poll seconds (10 min)
    "strategy": "best",  # "best" | "next-available"
}

# Usage % must leave room both to actually trigger (< 100) and to be meaningful
# (> 0). The watcher should not poll more often than the usage cache TTL (15s)
# nor so rarely it misses a window.
MIN_THRESHOLD = 1
MAX_THRESHOLD = 99
MIN_INTERVAL = 30


# --- Config storage -------------------------------------------------------


def _config_path(switcher: ClaudeAccountSwitcher) -> Path:
    return switcher.backup_dir / "autoswitch.json"


def _pid_path(switcher: ClaudeAccountSwitcher) -> Path:
    return switcher.backup_dir / "autoswitch.pid"


def _log_path(switcher: ClaudeAccountSwitcher) -> Path:
    return switcher.backup_dir / "autoswitch.log"


def load_config(switcher: ClaudeAccountSwitcher) -> dict:
    """Load autoswitch.json, filling any missing field with its default."""
    raw = switcher._read_json(_config_path(switcher)) or {}
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in raw.items() if k in DEFAULTS})
    # Defensive copies so callers never mutate the DEFAULTS dict.
    cfg["overrides"] = dict(cfg.get("overrides") or {})
    return cfg


def save_config(switcher: ClaudeAccountSwitcher, cfg: dict) -> None:
    """Persist the rule set to autoswitch.json."""
    switcher.backup_dir.mkdir(parents=True, exist_ok=True)
    payload = {k: cfg.get(k, DEFAULTS[k]) for k in DEFAULTS}
    switcher._write_json(_config_path(switcher), payload)


def effective_threshold(cfg: dict, num: str | None) -> int:
    """Per-account override if set, else the global threshold."""
    overrides = cfg.get("overrides") or {}
    if num is not None and str(num) in overrides:
        return int(overrides[str(num)])
    return int(cfg.get("globalThreshold", DEFAULTS["globalThreshold"]))


# --- One-shot evaluation (the unit the watcher loops) ---------------------


def _active_account_num(switcher: ClaudeAccountSwitcher) -> str | None:
    """Slot number of the live Claude account, or None if none/unmanaged."""
    identity = switcher._get_current_account()
    if identity is None:
        return None
    email, org_uuid = identity
    data = switcher._get_sequence_data_migrated()
    if not data:
        return None
    return switcher._find_account_slot(data, email, org_uuid)


def evaluate_and_switch(switcher: ClaudeAccountSwitcher, cfg: dict) -> dict:
    """Check the active account once and switch if it's over its threshold.

    Returns a small result dict (used both for ``ccs autoswitch check`` output
    and the watcher log). Never raises on network/usage failure — an
    unmeasurable account is reported as a no-op, not an error.
    """
    active = _active_account_num(switcher)
    if active is None:
        return {
            "switched": False,
            "reason": "no-active-account",
            "active": None,
            "pct": None,
            "threshold": None,
            "message": "No managed Claude account is active.",
        }

    headroom = oauth.account_headroom(switcher._usage_by_account().get(str(active)))
    if headroom is None:
        return {
            "switched": False,
            "reason": "usage-unavailable",
            "active": active,
            "pct": None,
            "threshold": effective_threshold(cfg, active),
            "message": f"Account-{active}: usage unavailable, skipping.",
        }

    pct = round(100.0 - headroom, 1)
    threshold = effective_threshold(cfg, active)
    if pct < threshold:
        return {
            "switched": False,
            "reason": "under-threshold",
            "active": active,
            "pct": pct,
            "threshold": threshold,
            "message": f"Account-{active} at {pct:.0f}% (< {threshold}%), staying.",
        }

    # Over threshold: delegate to the proven "best" engine. It only lands on an
    # account with strictly more headroom, and stays put otherwise.
    result = switcher.switch(strategy=cfg.get("strategy", "best"), json_output=True) or {}
    to = result.get("to") or {}
    target = to.get("number")
    switched = bool(result.get("switched"))
    over = f"Account-{active} at {pct:.0f}% (>= {threshold}%)"
    if switched:
        reason = "switched"
        msg = f"{over} -> switched to Account-{target} ({to.get('email', '')})."
    elif result.get("reason") == "only-one-account":
        # Nothing to switch to — there's just this one managed account.
        reason = "only-one-account"
        msg = f"{over} but it's the only managed account, staying. Add more with 'ccs add'."
    else:
        # Every other account is equal or worse (or unmeasurable); staying is
        # the safe choice rather than moving onto a worse account.
        reason = "no-better-account"
        msg = f"{over} but no other account has more headroom, staying."
    return {
        "switched": switched,
        "reason": reason,
        "active": active,
        "pct": pct,
        "threshold": threshold,
        "target": target,
        "message": msg,
    }


# --- Watcher process management -------------------------------------------


def _read_pid(switcher: ClaudeAccountSwitcher) -> int | None:
    """Return the watcher PID if the file exists and the process is alive.

    Liveness uses the project's cross-platform :func:`is_pid_alive` (POSIX
    ``os.kill(pid, 0)`` / Windows ``OpenProcess``) — never a kill probe.
    """
    path = _pid_path(switcher)
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    if is_pid_alive(pid):
        return pid
    # Stale file from a crashed/killed watcher — clean it up opportunistically.
    path.unlink(missing_ok=True)
    return None


def is_running(switcher: ClaudeAccountSwitcher) -> bool:
    return _read_pid(switcher) is not None


def start_watcher(switcher: ClaudeAccountSwitcher) -> int:
    """Spawn the detached watcher process. Returns its PID.

    Refuses to start a second watcher while one is alive.
    """
    existing = _read_pid(switcher)
    if existing is not None:
        raise ConfigError(
            f"Auto-switch watcher is already running (PID {existing}). "
            "Run 'ccs autoswitch stop' first."
        )

    switcher.backup_dir.mkdir(parents=True, exist_ok=True)
    log = open(_log_path(switcher), "a", encoding="utf-8")  # noqa: SIM115 (handed to child)
    try:
        cmd = [sys.executable, "-m", "claude_swap", "autoswitch", "__run"]
        kwargs: dict = {
            "stdin": subprocess.DEVNULL,
            "stdout": log,
            "stderr": log,
            "close_fds": True,
            "cwd": str(switcher.backup_dir),
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = (
                subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
                | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(cmd, **kwargs)
    finally:
        log.close()

    cfg = load_config(switcher)
    cfg["enabled"] = True
    save_config(switcher, cfg)

    # ``proc.pid`` is the spawned interpreter, which on some platforms (e.g. the
    # Windows venv launcher shim) differs from the daemon that ends up running
    # the loop and writing the PID file. Prefer the daemon's own recorded PID so
    # the reported value matches ``status``/``stop``; fall back to ``proc.pid``.
    for _ in range(20):
        recorded = _read_pid(switcher)
        if recorded is not None:
            return recorded
        time.sleep(0.1)
    return proc.pid


def stop_watcher(switcher: ClaudeAccountSwitcher) -> bool:
    """Terminate the watcher if running. Returns True if one was stopped."""
    pid = _read_pid(switcher)
    cfg = load_config(switcher)
    cfg["enabled"] = False
    save_config(switcher, cfg)

    if pid is None:
        _pid_path(switcher).unlink(missing_ok=True)
        return False

    try:
        # POSIX: graceful SIGTERM (watcher cleans up its PID file).
        # Windows: os.kill with a non-CTRL signal calls TerminateProcess.
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass
    # The PID file may not be removed by a hard-killed watcher; clear it here.
    _pid_path(switcher).unlink(missing_ok=True)
    return True


def run_watcher(switcher: ClaudeAccountSwitcher) -> None:
    """Blocking poll loop. This is the detached child's own entry point.

    Writes its PID file, then loops ``evaluate_and_switch`` every ``interval``
    seconds, logging each outcome. Transient errors are logged, never fatal.
    """
    pid_file = _pid_path(switcher)
    switcher.backup_dir.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()), encoding="utf-8")

    stopping = {"flag": False}

    def _handle_term(_signum, _frame):
        stopping["flag"] = True

    if sys.platform != "win32":
        signal.signal(signal.SIGTERM, _handle_term)

    def _log(msg: str) -> None:
        try:
            with open(_log_path(switcher), "a", encoding="utf-8") as fh:
                fh.write(f"{get_timestamp()}  {msg}\n")
        except OSError:
            pass

    try:
        cfg = load_config(switcher)
        _log(f"watcher started (pid {os.getpid()}, interval {cfg['interval']}s, global {cfg['globalThreshold']}%)")
        while not stopping["flag"]:
            cfg = load_config(switcher)  # re-read so wizard edits take effect live
            try:
                result = evaluate_and_switch(switcher, cfg)
                _log(result["message"])
            except Exception as exc:  # never let one bad poll kill the watcher
                _log(f"poll error: {exc!r}")
            # Sleep in short slices so a SIGTERM (POSIX) ends promptly.
            slept = 0
            interval = max(MIN_INTERVAL, int(cfg.get("interval", DEFAULTS["interval"])))
            while slept < interval and not stopping["flag"]:
                time.sleep(min(2, interval - slept))
                slept += 2
    finally:
        _log("watcher stopped")
        pid_file.unlink(missing_ok=True)


def watcher_status(switcher: ClaudeAccountSwitcher) -> dict:
    """Snapshot of running state + rules for display."""
    cfg = load_config(switcher)
    return {
        "running": is_running(switcher),
        "pid": _read_pid(switcher),
        "config": cfg,
        "log": str(_log_path(switcher)),
    }


# --- Human-facing commands (called from cli.py) ---------------------------


def print_status(switcher: ClaudeAccountSwitcher) -> None:
    """`ccs autoswitch status` — running state, rules, and per-account usage."""
    st = watcher_status(switcher)
    cfg = st["config"]

    if st["running"]:
        state = f"{accent('running')} {dimmed('(PID ' + str(st['pid']) + ')')}"
    else:
        state = dimmed("stopped")
    print(f"{bolded('Auto-switch:')} {state}")
    print(f"  {dimmed('Global threshold:')} {cfg['globalThreshold']}%   "
          f"{dimmed('poll every')} {cfg['interval']}s   "
          f"{dimmed('strategy')} {cfg['strategy']}")

    overrides = cfg.get("overrides") or {}
    data = switcher._get_sequence_data() or {}
    accounts = data.get("accounts", {})
    if not accounts:
        print(f"  {dimmed('No managed accounts yet.')}")
        return

    usage = switcher._usage_by_account()
    active = _active_account_num(switcher)
    print(f"  {dimmed('Accounts (usage vs threshold):')}")
    for num in sorted(accounts, key=lambda n: int(n)):
        email = accounts[num].get("email", "")
        thr = effective_threshold(cfg, num)
        tag = " (override)" if str(num) in overrides else ""
        headroom = oauth.account_headroom(usage.get(str(num)))
        if headroom is None:
            usage_str = dimmed("usage n/a")
        else:
            pct = 100.0 - headroom
            mark = accent("OVER") if pct >= thr else muted(f"{pct:.0f}%")
            usage_str = f"{mark} / {thr}%{tag}"
        marker = bolded("*") if str(num) == str(active) else " "
        print(f"   {marker} Account-{num} {muted(email)}: {usage_str}")


def print_check(switcher: ClaudeAccountSwitcher) -> None:
    """`ccs autoswitch check` — evaluate once now and switch if over threshold."""
    cfg = load_config(switcher)
    result = evaluate_and_switch(switcher, cfg)
    if result["switched"]:
        print(f"{accent('Switched:')} {result['message']}")
    else:
        print(f"{dimmed('No switch:')} {result['message']}")


def _prompt_int(prompt: str, lo: int, hi: int) -> int | None:
    """Prompt for an int in [lo, hi]; return None if blank/cancelled."""
    raw = input(prompt).strip()
    if not raw:
        return None
    try:
        val = int(raw)
    except ValueError:
        warning(f"  Not a number: {raw!r}")
        return None
    if not (lo <= val <= hi):
        warning(f"  Must be between {lo} and {hi}.")
        return None
    return val


def run_wizard(switcher: ClaudeAccountSwitcher) -> None:
    """`ccs autoswitch` — interactive setup. Easy, tick-as-you-go."""
    print(bolded("Auto-switch setup"))
    print(dimmed(
        "Swap to the account with the most quota left when the active one "
        "crosses its limit.\n"
    ))

    while True:
        cfg = load_config(switcher)
        running = is_running(switcher)
        print(f"{dimmed('Current rules:')} global {bolded(str(cfg['globalThreshold']) + '%')}, "
              f"poll {cfg['interval']}s, watcher {accent('on') if running else dimmed('off')}")
        overrides = cfg.get("overrides") or {}
        if overrides:
            pretty = ", ".join(f"#{n}={p}%" for n, p in sorted(overrides.items(), key=lambda kv: int(kv[0])))
            print(f"{dimmed('  per-account overrides:')} {pretty}")

        print()
        print(f"  {bolded('1')}  Set global threshold %")
        print(f"  {bolded('2')}  Set / clear a per-account override")
        print(f"  {bolded('3')}  Set poll interval (seconds)")
        print(f"  {bolded('4')}  {'Stop' if running else 'Start'} the background watcher")
        print(f"  {bolded('5')}  Done")
        choice = input("Choose [1-5]: ").strip()

        if choice == "1":
            val = _prompt_int(
                f"  New global threshold % [{MIN_THRESHOLD}-{MAX_THRESHOLD}], blank to keep: ",
                MIN_THRESHOLD, MAX_THRESHOLD,
            )
            if val is not None:
                cfg["globalThreshold"] = val
                save_config(switcher, cfg)
                print(f"  {accent('✓')} Global threshold set to {val}%")
        elif choice == "2":
            _override_submenu(switcher, cfg)
        elif choice == "3":
            val = _prompt_int(
                f"  Poll interval seconds [>= {MIN_INTERVAL}], blank to keep: ",
                MIN_INTERVAL, 86400,
            )
            if val is not None:
                cfg["interval"] = val
                save_config(switcher, cfg)
                print(f"  {accent('✓')} Poll interval set to {val}s")
        elif choice == "4":
            if running:
                stop_watcher(switcher)
                print(f"  {accent('✓')} Watcher stopped")
            else:
                pid = start_watcher(switcher)
                print(f"  {accent('✓')} Watcher started (PID {pid})")
        elif choice in ("5", "q", ""):
            print(dimmed("Done. Run 'ccs autoswitch status' anytime to check."))
            return
        else:
            warning(f"  Unknown choice: {choice!r}")
        print()


def _override_submenu(switcher: ClaudeAccountSwitcher, cfg: dict) -> None:
    """Set or clear a single per-account threshold override."""
    data = switcher._get_sequence_data() or {}
    accounts = data.get("accounts", {})
    if not accounts:
        warning("  No managed accounts yet — add some with 'ccs add'.")
        return

    overrides = cfg.get("overrides") or {}
    print(dimmed("  Accounts:"))
    for num in sorted(accounts, key=lambda n: int(n)):
        cur = overrides.get(str(num))
        suffix = f"  (override {cur}%)" if cur is not None else f"  (uses global {cfg['globalThreshold']}%)"
        print(f"    {bolded(num)}  {accounts[num].get('email', '')}{dimmed(suffix)}")

    target = input("  Account number to set (blank to cancel): ").strip()
    if not target:
        return
    if target not in accounts:
        warning(f"  No such account: {target}")
        return

    val = input(
        f"  Threshold % for Account-{target} [{MIN_THRESHOLD}-{MAX_THRESHOLD}], "
        f"or 'x' to clear override: "
    ).strip()
    if val.lower() == "x":
        overrides.pop(str(target), None)
        cfg["overrides"] = overrides
        save_config(switcher, cfg)
        print(f"  {accent('✓')} Cleared override for Account-{target} "
              f"(now uses global {cfg['globalThreshold']}%)")
        return
    try:
        num_val = int(val)
    except ValueError:
        warning(f"  Not a number: {val!r}")
        return
    if not (MIN_THRESHOLD <= num_val <= MAX_THRESHOLD):
        warning(f"  Must be between {MIN_THRESHOLD} and {MAX_THRESHOLD}.")
        return
    overrides[str(target)] = num_val
    cfg["overrides"] = overrides
    save_config(switcher, cfg)
    print(f"  {accent('✓')} Account-{target} will switch at {num_val}%")
