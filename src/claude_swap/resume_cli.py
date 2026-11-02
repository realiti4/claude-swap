"""Human/JSON front end for ``cswap resume`` (install, uninstall, status, now).

The engine lives in :mod:`claude_swap.resume`; this module only renders it
and runs the one-shot actions a user asks for.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from claude_swap import transcript as tx
from claude_swap.hooks import claude_settings_path, install_hook, installed_command, uninstall_hook
from claude_swap.inbox import InboxError, peer_token, post_message
from claude_swap.paths import get_claude_config_home
from claude_swap.printer import dimmed, muted, warning
from claude_swap.process_detection import ClaudeSession, list_sessions
from claude_swap.resume import (
    MESSAGE,
    REASON_TEXT,
    RESUMED,
    WAITING,
    ResumeRecord,
    cross_session_inbound,
    delivery_confirmed,
    force_flag,
    list_records,
    load_record,
    mutate_record,
    peer_message_held,
    resume_dir,
    waiter_alive,
)

# Cross-session messaging on native Windows arrived in 2.1.234 (2.1.224
# elsewhere); StopFailure in 2.1.78.
MIN_VERSION_WINDOWS = (2, 1, 234)
MIN_VERSION_POSIX = (2, 1, 224)
FORCE_WAIT_S = 15.0


@dataclass(frozen=True)
class SessionView:
    """One row of ``cswap resume status``: a live session and/or a record."""

    session_id: str
    name: str
    cwd: str
    state: str
    detail: str
    record: ResumeRecord | None = None
    live: ClaudeSession | None = None
    transcript: tx.TranscriptState | None = None
    waiter: bool = False


def _version_tuple(version: str | None) -> tuple[int, ...] | None:
    try:
        return tuple(int(part) for part in str(version).split(".")[:3])
    except ValueError:
        return None


def _session_versions(config_home: Path) -> list[str]:
    versions = []
    sessions_dir = config_home / "sessions"
    for session in list_sessions(config_home):
        try:
            data = json.loads((sessions_dir / f"{session.pid}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("version"), str):
            versions.append(data["version"])
    return versions


def _transcript_for(config_home: Path, session_id: str) -> Path | None:
    projects = config_home / "projects"
    try:
        return next(projects.glob(f"*/{session_id}.jsonl"), None)
    except OSError:
        return None


def _fmt_ts(ts: float | None) -> str:
    if not ts:
        return "-"
    when = datetime.fromtimestamp(ts)
    if when.date() == datetime.now().date():
        return when.strftime("%H:%M")
    return when.strftime("%a %H:%M")


def collect_views(backup_root: Path) -> list[SessionView]:
    """Records joined with live sessions, plus live sessions stopped on a
    limit that nothing is waiting on yet (e.g. stopped before install)."""
    config_home = get_claude_config_home()
    live = {s.session_id: s for s in list_sessions(config_home)}
    records = {r.session_id: r for r in list_records(backup_root)}
    views = []
    for sid in sorted(set(live) | set(records)):
        record, session = records.get(sid), live.get(sid)
        path = record.transcript_path if record else _transcript_for(config_home, sid)
        state = tx.read_transcript_state(path) if path else None
        stalled = state is not None and state.status == tx.STALLED
        if record is None and not stalled:
            continue  # a live session that's fine: not ours to show
        alive = waiter_alive(backup_root, sid) if record and record.state == WAITING else False
        if record is not None:
            row_state, detail = record.state, record.detail
            if record.state == WAITING and not alive:
                detail = "waiter not running (it re-arms on the next limit stop)"
        else:
            row_state, detail = "stopped", "stopped on a usage limit; nothing armed"
        name = (session.name if session and session.name else None) or Path(
            (record.cwd if record else None) or (session.cwd if session else "") or sid
        ).name
        views.append(
            SessionView(
                session_id=sid,
                name=name or sid[:8],
                cwd=(session.cwd if session else None) or (record.cwd if record else "") or "",
                state=row_state,
                detail=detail,
                record=record,
                live=session,
                transcript=state,
                waiter=alive,
            )
        )
    return views


def _view_json(view: SessionView) -> dict[str, Any]:
    record, stall = view.record, view.transcript.stall if view.transcript else None
    return {
        "sessionId": view.session_id,
        "name": view.name,
        "cwd": view.cwd,
        "state": view.state,
        "detail": view.detail,
        "running": view.live is not None,
        "waiter": view.waiter,
        "stalledAt": (record.stalled_at if record else None) or (stall.at if stall else None),
        "resetsAt": (record.resets_at if record else None) or (stall.resets_at if stall else None),
        "limitType": (record.limit_type if record else None)
        or (stall.limit_type if stall else None),
        "lastResumeAt": record.last_resume_at if record else None,
        "consecutive": record.consecutive if record else 0,
        "permissionMode": view.transcript.permission_mode if view.transcript else None,
    }


def resume_status(backup_root: Path, json_output: bool = False) -> None:
    command = installed_command()
    views = collect_views(backup_root)
    if json_output:
        payload = {
            "schemaVersion": 1,
            "hookInstalled": command is not None,
            "hookCommand": command,
            "settingsPath": str(claude_settings_path()),
            "sessions": [_view_json(v) for v in views],
        }
        print(json.dumps(payload, indent=2))
        return
    if command:
        where = dimmed(f"(StopFailure hook in {claude_settings_path()})")
        print(f"Auto-continue: on {where}")
    else:
        print(f"Auto-continue: off {dimmed('- run `cswap resume install` to enable')}")
    if not views:
        print(muted("No sessions are waiting on, or stopped by, a usage limit."))
        return
    print()
    name_w = max(len(v.name) for v in views)
    for v in views:
        row = _view_json(v)
        resets = f"resets {_fmt_ts(row['resetsAt'])}" if row["resetsAt"] else ""
        when = _fmt_ts(row["lastResumeAt"] if v.state == RESUMED else row["stalledAt"])
        bits = [b for b in (v.detail, resets, row["limitType"] or "") if b]
        print(f"  {v.name:<{name_w}}  {v.state:<12} {when:<9} {dimmed(' · '.join(bits))}")


def resume_install(backup_root: Path, command: str | None = None) -> None:
    snapshots = resume_dir(backup_root) / "snapshots"
    change = install_hook(snapshots, command)
    if change.changed:
        print(f"Installed the StopFailure hook in {change.settings_path}")
        if change.snapshot:
            print(dimmed(f"  previous settings saved to {change.snapshot}"))
    else:
        print(f"Already installed in {change.settings_path}")
    print(dimmed(f"  hook command: {change.command}"))

    config_home = get_claude_config_home()
    minimum = MIN_VERSION_WINDOWS if os.name == "nt" else MIN_VERSION_POSIX
    versions = [v for v in (_version_tuple(x) for x in _session_versions(config_home)) if v]
    if versions and max(versions) < minimum:
        need = ".".join(map(str, minimum))
        warning(
            f"Running Claude Code is older than {need}; auto-continue needs its "
            "cross-session inbox. Update Claude Code."
        )
    inbound = cross_session_inbound(claude_settings_path())
    if inbound in ("hold", "refuse"):
        warning(
            f"crossSessionInbound is '{inbound}' in your Claude Code settings, so "
            "sessions won't accept the continue message. Remove it or set 'accept'."
        )
    print()
    print("When a usage limit stops a session, it now continues by itself as soon as")
    print("an account switch lands or the limit resets. Running sessions pick the")
    print("hook up automatically. See `cswap resume status`.")


def resume_uninstall(backup_root: Path) -> None:
    change = uninstall_hook(resume_dir(backup_root) / "snapshots")
    if change.changed:
        print(f"Removed the StopFailure hook from {change.settings_path}")
        if change.snapshot:
            print(dimmed(f"  previous settings saved to {change.snapshot}"))
    else:
        print(muted("Auto-continue hook was not installed; nothing to do."))


def _match(views: list[SessionView], wanted: list[str], all_sessions: bool) -> list[SessionView]:
    candidates = [
        v for v in views if v.live is not None and (v.state == WAITING or v.state == "stopped")
    ]
    if all_sessions:
        return candidates
    picked: list[SessionView] = []
    for token in wanted:
        hits = [
            v
            for v in candidates
            if v.session_id.startswith(token) or v.name == token or Path(v.cwd).name == token
        ]
        picked.extend(h for h in hits if h not in picked)
    return picked


def _send_as_peer(backup_root: Path, view: SessionView) -> str:
    """Deliver directly (no waiter holds this session's own-child token)."""
    session = view.live
    if session is None or not session.messaging_socket_path:
        return "no inbox (Claude Code too old, or messaging off in that session)"
    if session.status not in (None, "idle"):
        return f"not idle ({session.status}); try again when its turn ends"
    mode = view.transcript.permission_mode if view.transcript else None
    if peer_message_held(mode, get_claude_config_home()):
        return (
            "skipped: it bypasses permissions, so Claude Code would hold a message from "
            "cswap for approval (the VS Code extension can't show that dialog). Type "
            "'continue' there, or set crossSessionInbound to 'accept'"
        )
    token = peer_token(get_claude_config_home() / "sessions", session.pid, session.proc_start)
    transcript = _transcript_for(get_claude_config_home(), view.session_id)
    posted_at = time.time()
    try:
        post_message(
            session.messaging_socket_path, MESSAGE.format(why=REASON_TEXT["forced"]), token
        )
    except InboxError as e:
        return f"failed: {e}"
    if transcript is None or not delivery_confirmed(
        str(transcript), posted_at, timeout=FORCE_WAIT_S
    ):
        return "posted, but the session hasn't recorded it (held or refused?)"
    now = time.time()
    stall = view.transcript.stall if view.transcript else None

    def mutator(prev: ResumeRecord | None) -> ResumeRecord | None:
        base = prev or ResumeRecord(
            session_id=view.session_id,
            transcript_path=str(transcript),
            config_home=str(get_claude_config_home()),
            global_config_path="",
            default_profile=True,
            armed_at=now,
            cwd=view.cwd or None,
        )
        return replace(
            base,
            state=RESUMED,
            detail=REASON_TEXT["forced"],
            last_resume_at=now,
            resumed_stall_uuid=stall.uuid if stall else base.resumed_stall_uuid,
            stall_uuid=stall.uuid if stall else base.stall_uuid,
            updated_at=now,
        )

    mutate_record(backup_root, view.session_id, mutator)
    return "sent"


def resume_now(backup_root: Path, wanted: list[str], all_sessions: bool = False) -> int:
    """Continue the chosen sessions now; returns the exit code (0 = all sent)."""
    targets = _match(collect_views(backup_root), wanted, all_sessions)
    if not targets:
        print(muted("No running session is waiting on, or stopped by, a usage limit."))
        return 1
    failures = 0
    for view in targets:
        if view.waiter:
            force_flag(backup_root, view.session_id).touch()
            deadline = time.monotonic() + FORCE_WAIT_S
            outcome = "asked its waiter; it continues once the session is idle"
            while time.monotonic() < deadline:
                record = load_record(backup_root, view.session_id)
                if record is not None and record.state != WAITING:
                    outcome = "sent" if record.state == RESUMED else record.state
                    break
                time.sleep(0.5)
        else:
            outcome = _send_as_peer(backup_root, view)
        if outcome != "sent" and not outcome.startswith("asked"):
            failures += 1
        print(f"  {view.name}: {outcome}")
    return 1 if failures else 0
