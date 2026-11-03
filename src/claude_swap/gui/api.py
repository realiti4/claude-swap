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
import sys
import threading
import time
import urllib.request
from collections import deque
from pathlib import Path
from typing import Callable

from claude_swap import __version__
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.gui import launcher
from claude_swap.json_output import usage_to_json
from claude_swap.models import AccountSnapshot, AccountsSnapshot
from claude_swap.snapshot_source import SnapshotSource
from claude_swap.switcher import SENTINEL_NOTES, ClaudeAccountSwitcher, last_seen_note

RELEASES_API = "https://api.github.com/repos/xutianhao6/claude-swap-desktop/releases/latest"
RELEASES_PAGE = "https://github.com/xutianhao6/claude-swap-desktop/releases"

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")

# Engine events kept for the page's log; older ones fall off.
_EVENT_BUFFER = 500


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


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

    def _attach_window(self, window) -> None:
        self._window = window

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
        }

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
            shown = launcher.open_terminal(args, cwd=cwd or None)
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
