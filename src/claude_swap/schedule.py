"""Daily quota-window activation and strict 99% failover.

The provider owns reset times. A one-token Messages request starts an idle
window; it cannot move an existing window. Claims persist before network I/O,
so restarts wait before retrying ambiguous requests. A process-lifetime lock
serializes this service only; account writes use the switcher's normal locks.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import plistlib
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from claude_swap import oauth, subscription
from claude_swap.autoswitch import AutoSwitchEngine
from claude_swap.locking import FileLock
from claude_swap.paths import get_backup_root
from claude_swap.settings import AutoSwitchSettings, atomic_write_json, load_settings, parse_model_names, UsageSettings
from claude_swap.snapshot_source import SnapshotSource
from claude_swap.switcher import ClaudeAccountSwitcher

TIMEZONE = "Asia/Shanghai"
SLOTS = (("1", 7, 12), ("2", 12, 17), ("3", 17, 22))
THRESHOLD = 99.0
INTERVAL = 15
RETRY_SECONDS = 300
MAX_AGE = 300


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts is not None else None


def timestamp(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, TypeError, ValueError):
        return None


def identity(account):
    return hashlib.sha256(f"{account.email}\0{account.org_uuid}".encode()).hexdigest()


def masked_email(email):
    local, sep, domain = email.partition("@")
    return (local[:2] + "***@" + domain) if sep else "***"


def load_json(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def boundaries(now, start, end):
    local = datetime.fromtimestamp(now, ZoneInfo(TIMEZONE))
    return (local.replace(hour=start, minute=0, second=0, microsecond=0).timestamp(),
            local.replace(hour=end, minute=0, second=0, microsecond=0).timestamp())


def percentages(account, now=None, models=()):
    """Only trusted, complete, finite provider readings may authorize a switch."""
    entry = account.usage
    if entry.sentinel or entry.age_s is None:
        return None
    data = entry.decision_value()
    if not isinstance(data, dict) or data.get("rejected"):
        return None
    rows = [data.get("five_hour"), data.get("seven_day")]
    selected = {m.lower() for m in models}
    scoped = [row for row in data.get("scoped", []) if isinstance(row, dict)]
    reported = {str(row.get("name", "")).lower() for row in scoped}
    if "all" not in selected and not selected <= reported:
        return None
    if data.get("source") == oauth.USAGE_SOURCE_HEADERS and not oauth._header_reading_complete(data, models):
        return None
    rows.extend(row for row in scoped
                if "all" in selected or str(row.get("name", "")).lower() in selected)
    values = []
    for row in rows:
        value = row.get("pct") if isinstance(row, dict) else None
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or not 0 <= value <= 100:
            return None
        reset = timestamp(row.get("resets_at"))
        if (now is not None and reset is not None and reset <= now
                and entry.fetched_at is not None and entry.fetched_at <= reset):
            return None
        values.append(value)
    return values


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def activation_request(token):
    """A bounded one-output-token request to the fixed Anthropic endpoint."""
    opener = urllib.request.build_opener(NoRedirect())
    with opener.open(oauth._probe_request(token, oauth.USAGE_PROBE_MODEL), timeout=20) as response:
        response.read(65536)
        return oauth.parse_unified_ratelimit_headers(response.headers)


class ScheduledSwitcher(ClaudeAccountSwitcher):
    """Scheduling polls never initiate inference, even if a UI opts into probes."""

    def _usage_settings(self):
        return UsageSettings(header_probe=False, fable_probe=False)


class DailySchedule:
    def __init__(self, switcher, config, *, clock=time.time, probe=activation_request):
        self.switcher = switcher
        self.config = config
        self.clock = clock
        self.probe = probe
        self.root = switcher.backup_dir
        self.source = SnapshotSource(switcher)
        self.state_path = self.root / "schedule-state.json"
        self.state = load_json(self.state_path)
        self.state.setdefault("activations", {})
        self.last_event = self.state.get("last_event")
        self.error = None
        self.model_setting = load_settings(self.root).model
        self.models = parse_model_names(self.model_setting)
        self.freshener = AutoSwitchEngine(
            switcher, AutoSwitchSettings(threshold=99, weekly_threshold=99, model=self.model_setting),
            lambda event: None, clock=clock,
        )

    def save(self):
        self.state["last_event"] = self.last_event
        atomic_write_json(self.state_path, self.state)

    def event(self, kind, detail):
        self.last_event = {"kind": kind, "at": iso(self.clock()), "detail": detail}
        self.save()

    def matches(self, account):
        return self.config.get("identities", {}).get(account.number) == identity(account)

    def activate(self, account, start, end):
        now = self.clock()
        day = datetime.fromtimestamp(now, ZoneInfo(TIMEZONE)).date().isoformat()
        key = f"{day}:{account.number}"
        entries = self.state["activations"]
        record = entries.setdefault(key, {"status": "pending", "attempted_at": None,
                                          "actual_resets_at": None, "error": None})
        if record["status"] in ("activated", "already_active", "request_accepted"):
            if record["status"] == "request_accepted":
                measured = (account.usage.last_good or {}).get("five_hour", {}).get("resets_at")
                reset = timestamp(measured)
                attempted = timestamp(record.get("attempted_at"))
                if reset and attempted and reset > attempted + 60 and (account.usage.fetched_at or 0) >= attempted:
                    record.update(status="activated", actual_resets_at=measured)
                    self.save()
            return
        if now < record.get("retry_at", 0):
            return
        data = account.usage.last_good or {}
        reset_value = data.get("five_hour", {}).get("resets_at")
        reset = timestamp(reset_value)
        record["actual_resets_at"] = reset_value
        # Do not infer an idle window from an unavailable or stale response.
        values = percentages(account, now, self.models)
        if values is None:
            record.update(status="unavailable", error="usage_unavailable", retry_at=now + 60)
            self.save()
            return
        if max(values) >= THRESHOLD:
            record.update(status="unavailable", error="quota_at_threshold", retry_at=now + 60)
            self.save()
            return
        if reset is not None and reset > now:
            if abs(reset - end) <= 300:
                record.update(status="already_active", error=None)
            else:
                record.update(status="waiting_reset", error="existing_window_cannot_be_moved",
                              retry_at=min(reset + 5, now + RETRY_SECONDS))
            self.save()
            return
        # Persist the claim BEFORE sending: an interrupted/ambiguous response
        # is not a reason to send one request every service restart.
        record.update(status="activating", attempted_at=iso(now), error=None,
                      retry_at=now + RETRY_SECONDS)
        self.save()
        try:
            if not account.is_active:
                verdict = self.freshener._freshen_target(account.number, account.email)
                if verdict != "ok":
                    raise RuntimeError("credential_unavailable")
            # This collector selects the live credential for the active slot
            # and the current session credential for a session-owned slot.
            rows = self.switcher._build_accounts_info()
            row = next((r for r in rows if str(r[0]) == account.number and r[1] == account.email
                        and r[3] == account.org_uuid), None)
            token_data = oauth.extract_oauth_data(row[5]) if row else None
            if not token_data or not token_data.get("accessToken"):
                raise RuntimeError("credential_unavailable")
            raw = self.probe(token_data["accessToken"])
            actual_reset = raw.get("five_hour", {}).get("resets_at")
            confirmed = (timestamp(actual_reset) is not None
                         and timestamp(actual_reset) > max(now + 60, reset or 0))
            record.update(status="activated" if confirmed else "request_accepted", error=None, completed_at=iso(self.clock()),
                          actual_resets_at=actual_reset,
                          late_seconds=max(0, round(now - start)))
            self.event("activated" if confirmed else "request_accepted",
                       f"Account {account.number}: one-token request accepted" + ("; reset confirmed" if confirmed else "; reset unconfirmed"))
        except urllib.error.HTTPError as exc:
            # Never persist exception text or response bodies (may contain auth).
            record.update(status="failed", error=f"http_{exc.code}")
            retry = exc.headers.get("Retry-After") if exc.headers else None
            if retry and retry.isdigit():
                record["retry_at"] = now + max(RETRY_SECONDS, int(retry))
            self.event("activation_failed", f"Account {account.number}: HTTP {exc.code}")
        except Exception as exc:
            record.update(status="failed", error=type(exc).__name__)
            self.event("activation_failed", f"Account {account.number}: request unavailable")
        self.save()

    def switch(self, account, reason):
        if self.freshener._freshen_target(account.number, account.email) != "ok":
            self.event("switch_blocked", f"Account {account.number}: credential unavailable")
            return False
        try:
            result = self.switcher.switch_to(account.number, json_output=True)
            if result and result.get("switched"):
                self.event("switched", f"Account {account.number}: {reason}")
                return True
            return bool(result and result.get("reason") == "already-active")
        except Exception:
            self.event("switch_blocked", f"Account {account.number}: switch failed")
            return False

    def tick(self):
        self.error = None
        now = self.clock()
        snap = self.source.take()
        accounts = {a.number: a for a in snap.accounts}
        due = None
        eligible = []
        available = []
        for number, start_hour, end_hour in SLOTS:
            account = accounts.get(number)
            if account is None or not self.matches(account):
                self.error = "configured_account_missing_or_changed"
                continue
            start, end = boundaries(now, start_hour, end_hour)
            values = percentages(account, now, self.models)
            if (account.switchable and not account.disabled
                    and values is not None and max(values) < THRESHOLD):
                available.append(account)
                if now >= start:
                    eligible.append(account)
            if start <= now < end:
                due = account
                if account.switchable and not account.disabled:
                    self.activate(account, start, end)
        current = accounts.get(snap.active_number)
        if current is not None and not self.matches(current):
            self.error = self.error or "active_account_not_bound_to_schedule"
            self.publish(snap)
            return
        values = percentages(current, now, self.models) if current else None
        if current and values is not None and max(values) >= THRESHOLD:
            candidates = sorted((a for a in available if not a.is_active),
                                key=lambda a: (a not in eligible, max(percentages(a, now, self.models)), int(a.number)))
            switched = False
            for account in candidates:
                reason = "usage reached 99%"
                if account not in eligible:
                    reason += "; future slot used early, actual reset may shift"
                if self.switch(account, reason):
                    switched = True
                    break
            if not switched:
                self.event("all_unavailable", "No available account with fresh usage below 99%")
        elif due and due in eligible and not due.is_active:
            day = datetime.fromtimestamp(now, ZoneInfo(TIMEZONE)).date().isoformat()
            selected = f"{day}:{due.number}"
            # Select the scheduled slot once. A subsequent manual/failover
            # switch is respected rather than being undone on every tick.
            if self.state.get("selected_slot") != selected and self.switch(due, "scheduled time slot"):
                self.state["selected_slot"] = selected
                self.save()
        elif due and due.is_active:
            day = datetime.fromtimestamp(now, ZoneInfo(TIMEZONE)).date().isoformat()
            self.state["selected_slot"] = f"{day}:{due.number}"
            self.save()
        snap = self.source.take(store_only=True)
        self.publish(snap)

    def publish(self, snap):
        now = self.clock()
        day = datetime.fromtimestamp(now, ZoneInfo(TIMEZONE)).date().isoformat()
        slots = {n: (s, e) for n, s, e in SLOTS}
        rows = []
        for account in snap.accounts:
            if account.number not in slots:
                continue
            data = account.usage.last_good or {}
            usage = {}
            for key in ("five_hour", "seven_day"):
                window = data.get(key, {})
                value = window.get("pct")
                usage[key] = {"utilization": value if isinstance(value, (int, float)) and math.isfinite(value) else None,
                              "resets_at": window.get("resets_at")}
            status = "ok" if (percentages(account, now, self.models) is not None
                              and account.usage.age_s <= MAX_AGE) else ("stale" if data else "unavailable")
            if not self.matches(account):
                status = "identity_mismatch"
            for scoped in data.get("scoped", []):
                name = str(scoped.get("name", "")).lower()
                if name in ("sonnet", "opus", "fable", "haiku"):
                    value = scoped.get("pct")
                    usage["seven_day_" + name] = {"utilization": value if isinstance(value, (int, float)) and math.isfinite(value) else None,
                                                "resets_at": scoped.get("resets_at")}
            start, end = slots[account.number]
            plan = account.subscription_plan
            rows.append({"id": account.number, "label": account.alias or f"Account {account.number}",
                         "email": masked_email(account.email), "is_active": account.is_active,
                         "subscription_type": subscription.plan_label(plan["plan"]) if plan else None,
                         "usage": usage, "updated_at": iso(account.usage.fetched_at),
                         "status": status, "error": ("identity_mismatch" if status == "identity_mismatch" else "usage_unavailable") if status != "ok" else None,
                         "slot": {"start": f"{start:02}:00", "end": f"{end:02}:00", "timezone": TIMEZONE},
                         "activation": {k: v for k, v in self.state["activations"].get(f"{day}:{account.number}", {"status": "pending"}).items()
                                        if k in {"status", "attempted_at", "actual_resets_at", "error", "late_seconds"}}})
        atomic_write_json(self.root / "schedule-status.json", {
            "schemaVersion": 1, "updated_at": iso(now), "active_account_id": snap.active_number,
            "accounts": rows, "scheduler": {"enabled": True, "timezone": TIMEZONE,
            "threshold": THRESHOLD, "last_event": self.last_event, "error": self.error}})


def manage_launch_agent(root, *, uninstall=False):
    """Install this service without replacing another launch agent or CLI."""
    if sys.platform != "darwin":
        raise RuntimeError("Launch agents require macOS; run cswap schedule in a service manager")
    label = "com.claude-swap.daily-schedule"
    path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    domain = f"gui/{os.getuid()}"
    if uninstall:
        subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True, timeout=15)
        # bootout may return before the old process leaves the domain. Wait
        # for removal so an immediate reinstall cannot mistake it for live.
        for _ in range(30):
            check = subprocess.run(["launchctl", "print", f"{domain}/{label}"],
                                   capture_output=True, timeout=5)
            if check.returncode != 0:
                break
            time.sleep(0.2)
        else:
            raise RuntimeError("Login agent is still stopping; retry uninstall before reinstalling")
        path.unlink(missing_ok=True)
        print("Stopped and removed the daily schedule login agent.")
        return
    if not load_json(root / "schedule.json").get("enabled"):
        raise RuntimeError("Configure the schedule before installing its login agent")
    program = [sys.executable, "-m", "claude_swap", "schedule"]
    payload = {"Label": label, "ProgramArguments": program, "RunAtLoad": True,
               "KeepAlive": True, "ThrottleInterval": 30,
               "ProcessType": "Background",
               "StandardOutPath": str(root / "schedule.stdout.log"),
               "StandardErrorPath": str(root / "schedule.stderr.log"),
               "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"}}
    if path.exists():
        existing = plistlib.loads(path.read_bytes())
        if existing != payload:
            raise RuntimeError("Launch agent already exists with different settings; not overwritten")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(plistlib.dumps(payload))
        path.chmod(0o600)
    for name in ("schedule.stdout.log", "schedule.stderr.log"):
        target = root / name
        target.touch(mode=0o600, exist_ok=True)
        target.chmod(0o600)
    check = subprocess.run(["launchctl", "print", f"{domain}/{label}"], capture_output=True, timeout=15)
    if check.returncode != 0:
        subprocess.run(["launchctl", "bootstrap", domain, str(path)], check=True, capture_output=True, timeout=15)
    print("Daily schedule login agent installed and running.")


def command(argv):
    parser = argparse.ArgumentParser(prog="cswap schedule", description=__doc__)
    parser.add_argument("--configure", action="store_true", help="Bind slots 1/2/3 to 07/12/17 Asia/Shanghai")
    parser.add_argument("--once", action="store_true", help="Run one activation/failover/snapshot tick")
    parser.add_argument("--install", action="store_true", help="Install and start the macOS login agent")
    parser.add_argument("--uninstall", action="store_true", help="Stop and remove only this login agent")
    args = parser.parse_args(argv)
    root = get_backup_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    config_path = root / "schedule.json"
    if args.install or args.uninstall:
        manage_launch_agent(root, uninstall=args.uninstall)
        return
    switcher = ScheduledSwitcher()
    if args.configure:
        if config_path.exists():
            parser.error("schedule.json already exists; edit it explicitly instead of overwriting")
        snap = switcher.accounts_snapshot(fetch=set())
        selected = {a.number: identity(a) for a in snap.accounts if a.number in {"1", "2", "3"}}
        if len(selected) != 3:
            parser.error("exactly slots 1, 2, 3 must exist")
        atomic_write_json(config_path, {"schemaVersion": 1, "enabled": True, "identities": selected})
        print("Configured slots 1/2/3 at 07:00/12:00/17:00 Asia/Shanghai; threshold 99%.")
        return
    config = load_json(config_path)
    if config.get("enabled") is not True or len(config.get("identities", {})) != 3:
        parser.error("run cswap schedule --configure first; schedule must be enabled")
    lock = FileLock(root / ".schedule.lock", timeout=0)
    if not lock.acquire():
        parser.error("another schedule service is running")
    stop = False
    def terminate(*_):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    service = DailySchedule(switcher, config)
    try:
        while not stop:
            try:
                (root / "schedule-refresh.request").unlink(missing_ok=True)
                service.tick()
            except Exception as exc:
                service.error = type(exc).__name__
                service.event("error", "Schedule tick failed; retrying")
                # Retain last readings, but advance the service heartbeat and
                # expose failure; consumers independently age account readings.
                existing = load_json(root / "schedule-status.json")
                if existing:
                    existing["updated_at"] = iso(time.time())
                    existing["scheduler"]["error"] = service.error
                    atomic_write_json(root / "schedule-status.json", existing)
            if args.once:
                break
            for _ in range(INTERVAL):
                if stop:
                    break
                time.sleep(1)
    finally:
        lock.release()
