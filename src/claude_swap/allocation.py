"""Startup-only Fable allocation; never switches the default login."""

from __future__ import annotations

import json
import math
import os
import sys
import time

from claude_swap.exceptions import SessionError
from claude_swap.locking import FileLock
from claude_swap.oauth import relevant_windows
from claude_swap.process_detection import (
    is_pid_alive, pid_matches_record, process_start_ticks, process_started_at,
)
from claude_swap.session import scan_live_sessions, session_dir_for
from claude_swap.settings import atomic_write_json


def _load_reservations(path):
    try:
        records = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (ValueError, UnicodeError):
        return {}
    except OSError as exc:
        raise SessionError(f"Cannot read allocation records at {path}: {exc}") from exc
    if not isinstance(records, dict):
        return {}
    clean = {}
    for pid, value in records.items():
        if not (pid.isascii() and pid.isdigit() and len(pid) <= 10 and 0 < int(pid) < 2**31):
            continue
        if not (isinstance(value, list) and len(value) in (2, 3)
                and isinstance(value[0], str) and isinstance(value[1], (str, type(None)))):
            continue
        stamp = value[2] if len(value) == 3 else None
        if stamp is not None and not isinstance(stamp, str):
            continue
        if is_pid_alive(int(pid)) and pid_matches_record(int(pid), stamp):
            clean[pid] = [value[0], value[1] or "", stamp]
    return clean


def run_allocated(manager, claude_args, threshold=90, share=True, share_history=False):
    if sys.platform == "win32" or os.environ.get("CLAUDE_CONFIG_DIR"):
        raise SessionError("Automatic allocation requires a regular macOS/Linux terminal without CLAUDE_CONFIG_DIR.")
    switcher = manager.switcher
    switcher.set_poll_policy_inputs(threshold, ("Fable",))
    snapshot = switcher.accounts_snapshot()
    lock_path = switcher.backup_dir / "allocation.lock"
    records_path = switcher.backup_dir / "allocation.json"
    pid = str(os.getpid())

    with FileLock(lock_path):
        records = _load_reservations(records_path)
        candidates = []
        reasons = []
        for account in snapshot.accounts:
            reason = None
            if account.is_active:
                reason = "default login (reserved)"
            elif account.disabled or not account.switchable or account.kind != "oauth":
                reason = "disabled or unsupported"
            windows = relevant_windows(account.usage.decision_value(), ("Fable",))
            if reason is None and (
                {name.lower() for name, _, _ in windows} != {"5h", "7d", "fable"}
                or any(not math.isfinite(pct) or pct < 0 for _, pct, _ in windows)
            ):
                reason = "unknown 5h/7d/Fable quota"
            if reason is None and max(pct for _, pct, _ in windows) >= threshold:
                reason = "quota at threshold; resets: " + ", ".join(
                    f"{name} {reset or 'unknown'}" for name, pct, reset in windows if pct >= threshold
                )
            sessions, unreadable = scan_live_sessions(session_dir_for(switcher.backup_dir, account.number, account.email))
            if reason is None and unreadable:
                reason = "unreadable session records"
            if reason:
                reasons.append(f"#{account.number}: {reason}")
                continue
            identity = [account.email, account.org_uuid]
            # POSIX exec preserves the PID: reserve before bootstrap, then
            # deduplicate with Claude's own registration once it appears.
            pids = {s.pid for s in sessions} | {int(p) for p, value in records.items() if value[:2] == identity}
            used = max(pct for _, pct, _ in windows)
            candidates.append((len(pids), used, int(account.number), account))
        if not candidates:
            raise SessionError("No isolated Fable account available. " + "; ".join(reasons))
        count, used, _, selected = min(candidates, key=lambda row: row[:3])
        stamp = process_start_ticks(int(pid))
        if stamp is None:
            started = process_started_at(int(pid))
            stamp = time.asctime(time.gmtime(started)) if started is not None else None
        records[pid] = [selected.email, selected.org_uuid, stamp]
        atomic_write_json(records_path, records)
    try:
        print(f"Allocated Fable to #{selected.number}: {count} sessions, {100-used:g}% headroom.", file=sys.stderr)
        manager.run(selected.number, ["--model", "fable", *claude_args], share=share,
                    share_history=share_history, require_session=True)
    finally:
        # exec never returns. Failed launches clean up immediately; successful
        # processes are pruned on the next allocation after their PID exits.
        try:
            with FileLock(lock_path):
                records = _load_reservations(records_path)
                records.pop(pid, None)
                atomic_write_json(records_path, records)
        except Exception as exc:
            print(f"Could not release allocation reservation at {records_path}: {exc}", file=sys.stderr)
