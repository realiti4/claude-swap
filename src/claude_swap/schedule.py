"""Daily quota-window activation and strict 99% failover.

The provider owns reset times. A small Messages request attempts to start an
idle window; it cannot move an existing window. Claims persist before network I/O,
so restarts wait before retrying ambiguous requests. A process-lifetime lock
serializes this service only; account writes use the switcher's normal locks.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
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
from claude_swap.settings import AutoSwitchSettings, atomic_write_json, load_settings, parse_model_names
from claude_swap.snapshot_source import SnapshotSource
from claude_swap.switcher import ClaudeAccountSwitcher

TIMEZONE = "Asia/Shanghai"
SLOTS = (("1", 7, 12), ("2", 12, 17), ("3", 17, 22))
ACTIVATION_START_HOUR = 7
THRESHOLD = 99.0
INTERVAL = 15
RETRY_SECONDS = 300
MAX_AGE = 300
MAX_ATTEMPTS = 3
ACTIVATION_MODEL = "claude-sonnet-5-5"
ACTIVATION_MAX_TOKENS = 16
ACTIVATION_PROMPT = "Reply with OK."
WEEKLY_RESET_TIE_SECONDS = 1800
SELECTION_COOLDOWN_SECONDS = 600
FIVE_HOUR_RESERVE_THRESHOLD = 80.0
ALLOCATION_MIN_HOURS = 0.25
ALLOCATION_SWITCH_GAIN = 1.2


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
    """Validate a bounded Messages response; headers never prove activation."""
    opener = urllib.request.build_opener(NoRedirect())
    request = oauth._probe_request(token, ACTIVATION_MODEL)
    body = json.loads(request.data)
    body.update(max_tokens=ACTIVATION_MAX_TOKENS,
                messages=[{"role": "user", "content": ACTIVATION_PROMPT}])
    request.data = json.dumps(body).encode()
    with opener.open(request, timeout=20) as response:
        body = response.read(65537)
        if len(body) > 65536:
            raise ValueError("invalid_message_response")
        message = json.loads(body)
        usage = message.get("usage") if isinstance(message, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if (not isinstance(message, dict) or message.get("type") != "message"
                or message.get("role") != "assistant" or not isinstance(usage, dict)
                or type(usage.get("input_tokens")) is not int or usage["input_tokens"] < 0
                or type(usage.get("output_tokens")) is not int or usage["output_tokens"] <= 0
                or not isinstance(content, list)
                or not any(isinstance(part, dict) and part.get("type") == "text"
                           and isinstance(part.get("text"), str) and part["text"].strip()
                           for part in content)):
            raise ValueError("invalid_message_response")
        return oauth.parse_unified_ratelimit_headers(response.headers)


def activation_percentages(account, now):
    """The activation request uses Sonnet, independent of failover model policy."""
    data = account.usage.last_good or {}
    scoped = data.get("scoped") or []
    models = ("Sonnet",) if any(isinstance(row, dict) and str(row.get("name", "")).lower() == "sonnet"
                               for row in scoped) else ()
    return percentages(account, now, models)


def weekly_reset(account, now):
    data = account.usage.decision_value()
    window = data.get("seven_day") if isinstance(data, dict) else None
    reset = timestamp(window.get("resets_at")) if isinstance(window, dict) else None
    return reset if reset is not None and reset > now else None


def rank_available(accounts, now, eligible, models):
    metrics = {a.number: allocation_metrics(a, now, models) for a in accounts}
    ranked = sorted((a for a in accounts if metrics[a.number] is not None),
                    key=lambda a: (metrics[a.number]["reserve"], -metrics[a.number]["score"],
                                   weekly_reset(a, now), int(a.number)))
    ranked.extend(sorted((a for a in accounts if weekly_reset(a, now) is None),
                         key=lambda a: (a not in eligible, max(percentages(a, now, models)), int(a.number))))
    return ranked


def allocation_metrics(account, now, models):
    reset = weekly_reset(account, now)
    values = percentages(account, now, models)
    if reset is None or values is None or max(values) >= THRESHOLD:
        return None
    data = account.usage.decision_value()
    five_reset = timestamp((data.get("five_hour") or {}).get("resets_at"))
    last_cycle = reset <= (five_reset if five_reset is not None and five_reset > now else now + 5 * 3600)
    hours = max(ALLOCATION_MIN_HOURS, (reset - now) / 3600)
    required = (THRESHOLD - values[1]) / hours
    headroom = (THRESHOLD - values[0]) / THRESHOLD
    return {"weekly_remaining_pct": 100 - values[1], "weekly_budget_pct": THRESHOLD - values[1],
            "hours_to_weekly_reset": (reset - now) / 3600,
            "required_weekly_pct_per_hour": required, "five_hour_headroom_pct": THRESHOLD - values[0],
            "score": required * headroom, "reserve": values[0] >= FIVE_HOUR_RESERVE_THRESHOLD and not last_cycle,
            "last_five_hour_cycle": last_cycle}


class ScheduledSwitcher(ClaudeAccountSwitcher):
    """Scheduling polls never initiate inference, even if a UI opts into probes."""

    def _usage_settings(self):
        return replace(super()._usage_settings(), header_probe=False, fable_probe=False)


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
        self.selection = {}
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
        # Events contain only fixed descriptions and slot numbers. Never log
        # credentials, exception messages, response bodies or account identities.
        try:
            fd = os.open(self.root / "schedule-events.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as stream:
                (self.root / "schedule-events.jsonl").chmod(0o600)
                stream.write(json.dumps(self.last_event, ensure_ascii=True) + "\n")
        except OSError:
            # A diagnostic-log failure must not erase a persisted send claim.
            pass

    def matches(self, account):
        return self.config.get("identities", {}).get(account.number) == identity(account)

    def activate(self, account, start, end):
        now = self.clock()
        day = datetime.fromtimestamp(now, ZoneInfo(TIMEZONE)).date().isoformat()
        target_day = datetime.fromtimestamp(start, ZoneInfo(TIMEZONE)).date().isoformat()
        if day != target_day:
            return
        eligible = now >= start
        key = f"{day}:{account.number}"
        if not eligible and key not in self.state["activations"]:
            return
        record = self.state["activations"].setdefault(key, {
            "status": "pending", "attempted_at": None,
            "actual_resets_at": None, "error": None, "attempt_count": 0,
        })
        # Older claims were persisted before network I/O. Count them as an
        # attempt even when no completion was recorded, to bound restart retries.
        if "attempt_count" not in record:
            record["attempt_count"] = 1 if record.get("attempted_at") else 0
            self.save()
        record.setdefault("cycle_number", 1)
        record.setdefault("cycle_attempt_count", record["attempt_count"])
        attempts = record["cycle_attempt_count"]
        data = account.usage.last_good or {}
        window = data.get("five_hour") or {}
        reset_value = window.get("resets_at")
        reset = timestamp(reset_value)
        values = activation_percentages(account, now)
        fetched = account.usage.fetched_at
        fresh = (values is not None and data.get("source") != oauth.USAGE_SOURCE_HEADERS
                 and fetched is not None and 0 <= now - fetched <= MAX_AGE
                 and account.usage.age_s is not None and account.usage.age_s <= MAX_AGE)
        after = record.get("confirmation_after")
        if after is None:
            after = timestamp(record.get("completed_at")) or timestamp(record.get("attempted_at"))
        post_request = fresh and (after is None or fetched > after)
        active = reset is not None and reset > now
        # A missing reset is idle only with a measured zero. An expired reset
        # needs a sample taken after the reset (checked by percentages()).
        idle = fresh and not active and window.get("pct") == 0 and (not reset_value or reset is not None)
        confirmed_reset = timestamp(record.get("actual_resets_at"))
        if (record.get("status") in ("activated", "already_active")
                and record.get("confirmed_sample_at") and confirmed_reset is not None
                and now >= confirmed_reset):
            if not fresh or fetched <= confirmed_reset or not (idle or active):
                return
            history = record.setdefault("completed_cycles", [])
            history.append({k: record[k] for k in (
                "cycle_number", "cycle_attempt_count", "status", "attempted_at",
                "actual_resets_at", "confirmed_at", "confirmed_sample_at", "model",
            ) if k in record})
            record.update(status="pending", cycle_number=record["cycle_number"] + 1,
                          cycle_attempt_count=0, cycle_due_at=iso(confirmed_reset),
                          attempted_at=None, completed_at=None, confirmation_after=None,
                          actual_resets_at=None, confirmed_at=None, confirmed_sample_at=None,
                          retry_at=0, error=None, reset_offset_seconds=None, model=None,
                          late_seconds=None)
            attempts = 0
            self.event("window_expired", f"Account {account.number}: checking next five-hour window")
            if active:
                record.update(status="already_active", actual_resets_at=reset_value,
                              confirmed_at=iso(now), confirmed_sample_at=iso(fetched))
                self.save()
                return
        if attempts:
            if not post_request:
                # Neither headers nor a cached pre-request reading can resolve
                # an ambiguous request. Keep the claim without sending again.
                return
            if active:
                if (record.get("status") != "activated" or record.get("actual_resets_at") != reset_value
                        or not record.get("confirmed_sample_at")):
                    record.update(status="activated", error=None, actual_resets_at=reset_value,
                                  confirmed_at=iso(now), confirmed_sample_at=iso(fetched),
                                  reset_offset_seconds=round(reset - end))
                    self.event("activated", f"Account {account.number}: endpoint usage confirmed an active window; reset offset {round(reset - end)}s")
                return
            if not idle:
                record.update(status="unavailable", error="usage_unavailable")
                self.save()
                return
            if record.get("status") in ("activated", "request_accepted", "activating", "already_active"):
                record.update(status="unconfirmed", actual_resets_at=reset_value,
                              error="window_not_started", confirmed_at=None, confirmed_sample_at=None)
                self.event("activation_unconfirmed", f"Account {account.number}: endpoint usage shows no active window")
        elif record.get("status") == "already_active" and not fresh:
            return
        if not eligible:
            return
        if not fresh:
            record.update(status="unavailable", error="usage_unavailable")
            self.save()
            return
        record["actual_resets_at"] = reset_value
        if active and (abs(reset - end) <= 300 or record.get("confirmed_sample_at")):
            record.update(status="already_active", error=None,
                          confirmed_at=iso(now), confirmed_sample_at=iso(fetched))
            self.save()
            return
        if max(values) >= THRESHOLD:
            record.update(status="unavailable", error="quota_at_threshold")
            self.save()
            return
        if active:
            record.update(status="waiting_reset", error="existing_window_cannot_be_moved",
                          retry_at=min(reset + 5, now + RETRY_SECONDS))
            self.save()
            return
        if not idle:
            record.update(status="unavailable", error="usage_unavailable")
            self.save()
            return
        if attempts >= MAX_ATTEMPTS:
            if record.get("status") != "attempt_limit":
                record.update(status="attempt_limit", error="window_attempt_limit")
                self.event("activation_limit", f"Account {account.number}: window activation attempt limit reached")
            return
        attempted = timestamp(record.get("attempted_at"))
        retry_at = max(record.get("retry_at", 0), (attempted + RETRY_SECONDS) if attempted is not None else 0)
        if now < retry_at:
            return
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
            now = self.clock()
            if (now < start or datetime.fromtimestamp(now, ZoneInfo(TIMEZONE)).date().isoformat() != day):
                record.update(status="missed", error="schedule_day_changed")
                self.event("activation_missed", f"Account {account.number}: schedule date changed before the request")
                return
            if now - fetched > MAX_AGE:
                record.update(status="unavailable", error="usage_unavailable")
                self.save()
                return
            record.update(status="activating", attempted_at=iso(now), error=None,
                          attempt_count=record["attempt_count"] + 1, cycle_attempt_count=attempts + 1,
                          confirmation_after=now, model=ACTIVATION_MODEL,
                          retry_at=now + RETRY_SECONDS,
                          late_seconds=max(0, round(now - (timestamp(record.get("cycle_due_at")) or start))))
            self.event("activation_attempt", f"Account {account.number}: sending activation request {attempts + 1}/{MAX_ATTEMPTS}")
            raw = self.probe(token_data["accessToken"])
            finished = self.clock()
            record.update(completed_at=iso(finished), confirmation_after=finished)
            if not isinstance(raw, dict):
                raise ValueError("invalid_probe_response")
            if "rejected" in raw:
                record.update(status="failed", error="request_rejected")
                rejected = raw.get("rejected") or {}
                rejected_reset = timestamp(rejected.get("resets_at")) if isinstance(rejected, dict) else None
                if rejected_reset is not None:
                    record["retry_at"] = max(record["retry_at"], rejected_reset)
                self.event("activation_failed", f"Account {account.number}: activation request rejected")
            else:
                record.update(status="request_accepted", error=None, actual_resets_at=None)
                self.event("request_accepted", f"Account {account.number}: request accepted; awaiting endpoint usage confirmation")
        except urllib.error.HTTPError as exc:
            # Never persist exception text or response bodies (may contain auth).
            record.update(status="failed", error=f"http_{exc.code}")
            retry = exc.headers.get("Retry-After") if exc.headers else None
            retry_seconds = RETRY_SECONDS
            if retry and retry.isdigit():
                retry_seconds = max(RETRY_SECONDS, int(retry))
            record["retry_at"] = self.clock() + retry_seconds
            if record.get("cycle_attempt_count", 0) > attempts:
                record["confirmation_after"] = self.clock()
            self.event("activation_failed", f"Account {account.number}: HTTP {exc.code}")
        except Exception as exc:
            record.update(status="failed", error=type(exc).__name__)
            record["retry_at"] = max(record.get("retry_at", 0), self.clock() + RETRY_SECONDS)
            if record.get("cycle_attempt_count", 0) > attempts:
                record["confirmation_after"] = self.clock()
            self.event("activation_failed", f"Account {account.number}: request unavailable")
        self.save()

    def switch(self, account, reason):
        if self.freshener._freshen_target(account.number, account.email) != "ok":
            self.event("switch_blocked", f"Account {account.number}: credential unavailable")
            return False
        values = percentages(account, self.clock(), self.models)
        if values is None or max(values) >= THRESHOLD:
            self.event("switch_blocked", f"Account {account.number}: usage no longer available")
            return False
        try:
            result = self.switcher.switch_to(account.number, json_output=True)
            if result and result.get("switched"):
                self.state["selection_active_account"] = account.number
                self.state["selection_changed_at"] = iso(self.clock())
                self.selection.update(preferred_account_id=account.number,
                                      preferred_weekly_resets_at=iso(weekly_reset(account, self.clock())),
                                      last_changed_at=self.state["selection_changed_at"],
                                      hold_until=iso(self.clock() + SELECTION_COOLDOWN_SECONDS))
                self.event("switched", f"Account {account.number}: {reason}")
                return True
            return bool(result and result.get("reason") == "already-active")
        except Exception:
            self.event("switch_blocked", f"Account {account.number}: switch failed")
            return False

    def tick(self):
        self.error = None
        snap = self.source.take()
        now = self.clock()
        accounts = {a.number: a for a in snap.accounts}
        day = datetime.fromtimestamp(now, ZoneInfo(TIMEZONE)).date().isoformat()
        activation_start, activation_end = boundaries(now, ACTIVATION_START_HOUR, ACTIVATION_START_HOUR + 5)
        for number, _, _ in SLOTS:
            account = accounts.get(number)
            if account is None or not self.matches(account):
                self.error = "configured_account_missing_or_changed"
                continue
            if (account.switchable and not account.disabled
                    and (now >= activation_start or f"{day}:{number}" in self.state["activations"])):
                self.activate(account, activation_start, activation_end)
        self.select_account(self.source.take(store_only=True))
        self.publish(self.source.take(store_only=True))

    def select_account(self, snap):
        now = self.clock()
        accounts = {a.number: a for a in snap.accounts}
        due = None
        eligible = []
        available = []
        day = datetime.fromtimestamp(now, ZoneInfo(TIMEZONE)).date().isoformat()
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
        self.selection = {"policy": "weekly_expiry", "reason": "waiting_usage",
                          "preferred_account_id": None, "preferred_weekly_resets_at": None,
                          "ranked_account_ids": [], "cooldown_seconds": SELECTION_COOLDOWN_SECONDS,
                          "deadline_tolerance_seconds": WEEKLY_RESET_TIE_SECONDS,
                          "allocation": "remaining_per_hour_with_five_hour_headroom",
                          "reserve_threshold": FIVE_HOUR_RESERVE_THRESHOLD,
                          "minimum_score_gain": ALLOCATION_SWITCH_GAIN, "allocation_accounts": []}
        current = accounts.get(snap.active_number)
        if current is not None and not self.matches(current):
            self.error = self.error or "active_account_not_bound_to_schedule"
            return
        if self.state.get("selection_active_account") != snap.active_number:
            if "selection_active_account" in self.state:
                self.state["selection_changed_at"] = iso(now)
            self.state["selection_active_account"] = snap.active_number
            self.save()
        changed_at = timestamp(self.state.get("selection_changed_at"))
        hold_until = changed_at + SELECTION_COOLDOWN_SECONDS if changed_at is not None else 0
        self.selection["last_changed_at"] = iso(changed_at)
        self.selection["hold_until"] = iso(hold_until) if hold_until > now else None
        ranked = rank_available(available, now, eligible, self.models)
        self.selection["ranked_account_ids"] = [a.number for a in ranked]
        metrics = {a.number: allocation_metrics(a, now, self.models) for a in ranked}
        self.selection["allocation_accounts"] = [{"id": a.number, **metrics[a.number]}
                                                 for a in ranked if metrics[a.number] is not None]
        if ranked:
            self.selection["preferred_account_id"] = ranked[0].number
            self.selection["preferred_weekly_resets_at"] = iso(weekly_reset(ranked[0], now))
        values = percentages(current, now, self.models) if current else None
        if current and values is not None and max(values) >= THRESHOLD:
            self.selection["reason"] = "usage_reached_99"
            switched = False
            for account in ranked:
                if account.is_active:
                    continue
                reason = "usage reached 99%"
                if weekly_reset(account, now) is not None:
                    reason += "; weekly quota allocation"
                elif account not in eligible:
                    reason += "; future slot used early, actual reset may shift"
                if self.switch(account, reason):
                    switched = True
                    break
            if not switched:
                self.event("all_unavailable", "No available account with fresh usage below 99%")
            return
        known = [a for a in ranked if weekly_reset(a, now) is not None]
        if known:
            best = known[0]
            best_metrics = metrics[best.number]
            current_reset = weekly_reset(current, now) if current in available else None
            current_metrics = metrics.get(current.number) if current is not None else None
            if (current_metrics is not None
                    and current_metrics["reserve"] <= best_metrics["reserve"]
                    and current_metrics["score"] * ALLOCATION_SWITCH_GAIN >= best_metrics["score"]):
                same_deadline = abs(current_reset - weekly_reset(best, now)) <= WEEKLY_RESET_TIE_SECONDS
                self.selection.update(reason="current_earliest_group" if same_deadline else "allocation_balanced",
                                      preferred_account_id=current.number,
                                      preferred_weekly_resets_at=iso(current_reset))
                return
            if current in available and now < hold_until:
                self.selection["reason"] = "selection_cooldown"
                return
            self.selection["reason"] = "weekly_quota_allocation"
            for account in known:
                if account.is_active:
                    continue
                reset = weekly_reset(account, now)
                candidate = metrics[account.number]
                if current_metrics is not None:
                    if candidate["reserve"] > current_metrics["reserve"]:
                        continue
                    if (candidate["reserve"] == current_metrics["reserve"]
                            and candidate["score"] <= current_metrics["score"] * ALLOCATION_SWITCH_GAIN):
                        continue
                if self.switch(account, "weekly quota allocation; expiry and 5h headroom"):
                    self.selection.update(preferred_account_id=account.number, preferred_weekly_resets_at=iso(reset))
                    return
            self.selection["reason"] = "weekly_selection_blocked"
            return
        self.selection["reason"] = "scheduled_fallback"
        if current in available and now < hold_until:
            self.selection["reason"] = "selection_cooldown"
            return
        if due and due in eligible and not due.is_active:
            selected = f"{day}:{due.number}"
            # Select the scheduled slot once. A subsequent manual/failover
            # switch is respected rather than being undone on every tick.
            if self.state.get("selected_slot") != selected and self.switch(due, "scheduled time slot"):
                self.state["selected_slot"] = selected
                self.save()
        elif due and due.is_active:
            self.state["selected_slot"] = f"{day}:{due.number}"
            self.save()

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
                                        if k in {"status", "attempted_at", "actual_resets_at", "error", "late_seconds",
                                                 "attempt_count", "retry_at", "confirmed_at", "confirmed_sample_at", "reset_offset_seconds", "model",
                                                 "cycle_number", "cycle_attempt_count", "cycle_due_at"}}})
        atomic_write_json(self.root / "schedule-status.json", {
            "schemaVersion": 1, "updated_at": iso(now), "active_account_id": snap.active_number,
            "accounts": rows, "scheduler": {"enabled": True, "timezone": TIMEZONE,
            "activation_start": f"{ACTIVATION_START_HOUR:02}:00",
            "selection": self.selection,
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
    parser.add_argument("--configure", action="store_true", help="Bind slots 1/2/3 for 07:00 activation and balanced weekly quota allocation")
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
        print("Configured all slots for 07:00 activation in Asia/Shanghai; weekly quota allocation with 5h headroom; threshold 99%.")
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
