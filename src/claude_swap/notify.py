"""Run a user command when the auto-switch engine reports something worth knowing.

``notify.command`` in settings.json names a shell command. For each event
whose kind is listed in ``notify.events`` the engine hands it to
:class:`EventNotifier`, which runs the command with:

- the event's ``--json`` payload (``AutoSwitchEvent.to_json``) on stdin;
- ``CSWAP_EVENT`` (the kind), ``CSWAP_TITLE`` (a short heading) and
  ``CSWAP_MESSAGE`` (the line ``cswap auto`` prints) in the environment.

The command is how the event leaves the machine: a push service, a chat
webhook, Messages on macOS, a desktop notifier. cswap ships no transport of
its own and adds no dependency for one.

Every frontend that hosts the engine (``cswap auto``, the TUI's auto view,
the menu bar) gets this, because the engine itself calls the notifier. A
dry-run engine never notifies.

The command runs on its own thread, so a slow or hung command never delays a
tick, and is killed after ``COMMAND_TIMEOUT_S``. The thread is not a daemon:
``cswap auto --once`` exits right after its tick, and the notification it
just triggered must still go out. Exit then waits at most the timeout.

Failures are logged, never raised: a broken notifier must not stop
switching. Events that recur while a condition lasts (``all-exhausted`` on
every blocked tick, a repeated ``error``) are sent once per distinct message
and then held back for ``REPEAT_S``; digits are ignored when comparing, so a
changing reset time or count does not defeat that. A switch is always sent.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from claude_swap import spawn
from claude_swap.settings import NotifySettings, load_notify_settings

_logger = logging.getLogger("claude-swap")

COMMAND_TIMEOUT_S = 30.0
REPEAT_S = 3600.0

# Kinds that report a distinct change each time and are never held back.
_ALWAYS_SEND = frozenset({"switch", "account-unquarantined"})

_TITLES = {
    "switch": "Auto-switched account",
    "account-quarantined": "Account quarantined",
    "account-unquarantined": "Account back in rotation",
    "all-exhausted": "All accounts exhausted",
    "error": "Auto-switch error",
    "config-warning": "Configuration warning",
}


def parse_event_kinds(value: str | None) -> frozenset[str]:
    """Split ``notify.events`` into a set of kinds (trimmed, lowercased)."""
    if not value:
        return frozenset()
    return frozenset(p.strip().lower() for p in value.split(",") if p.strip())


def event_title(kind: str) -> str:
    return _TITLES.get(kind, f"claude-swap: {kind}")


def _run_shell(command: str, payload: str, env: dict[str, str]) -> int:
    """Run ``command`` through the platform shell; return its exit code."""
    kwargs = dict(
        input=payload,
        text=True,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=COMMAND_TIMEOUT_S,
    )
    if sys.platform == "win32":
        result = subprocess.run(command, shell=True, **kwargs)
    else:
        # An argv list, not shell=True, so spawn.run can avoid fork on macOS.
        result = spawn.run(["/bin/sh", "-c", command], **kwargs)
    if result.returncode != 0:
        err = (result.stderr or "").strip().splitlines()
        _logger.warning(
            "notify.command exited %d%s",
            result.returncode,
            f": {err[-1][:200]}" if err else "",
        )
    return result.returncode


class EventNotifier:
    """Select, de-duplicate and dispatch auto-switch events to ``notify.command``."""

    def __init__(
        self,
        settings: NotifySettings,
        *,
        clock: Callable[[], float] = time.monotonic,
        runner: Callable[[str, str, dict[str, str]], int] = _run_shell,
        background: bool = True,
    ):
        self.command = settings.command
        self.kinds = parse_event_kinds(settings.events)
        self._clock = clock
        self._runner = runner
        self._background = background
        self._sent: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()

    @classmethod
    def for_backup_dir(cls, backup_dir: Path) -> EventNotifier | None:
        """The notifier settings.json asks for, or None when none is configured."""
        try:
            settings = load_notify_settings(backup_dir)
        except Exception as e:  # settings trouble must not stop the engine
            _logger.warning("notify: could not read settings: %s", e)
            return None
        if not settings.command:
            return None
        return cls(settings)

    def wants(self, event) -> bool:
        if not self.command or event.kind not in self.kinds:
            return False
        return not getattr(event, "dry_run", False)

    def _first_in_window(self, event, message: str) -> bool:
        if event.kind in _ALWAYS_SEND:
            return True
        key = (event.kind, re.sub(r"\d+", "#", message))
        now = self._clock()
        with self._lock:
            for k in [k for k, t in self._sent.items() if now - t >= REPEAT_S]:
                del self._sent[k]
            if key in self._sent:
                return False
            self._sent[key] = now
            return True

    def handle(self, event) -> None:
        """Send ``event`` if selected and not a recent repeat. Never raises."""
        try:
            if not self.wants(event):
                return
            message = event.human()
            if not self._first_in_window(event, message):
                return
            payload = json.dumps(event.to_json())
            env = {
                **os.environ,
                "CSWAP_EVENT": event.kind,
                "CSWAP_TITLE": event_title(event.kind),
                "CSWAP_MESSAGE": message,
            }
            if self._background:
                threading.Thread(
                    target=self._dispatch,
                    args=(payload, env),
                    name="cswap-notify",
                    daemon=False,
                ).start()
            else:
                self._dispatch(payload, env)
        except Exception as e:  # never let notification break switching
            _logger.warning("notify: could not send %s event: %s", event.kind, e)

    def _dispatch(self, payload: str, env: dict[str, str]) -> None:
        try:
            self._runner(self.command, payload, env)
        except subprocess.TimeoutExpired:
            _logger.warning(
                "notify.command timed out after %.0fs and was killed", COMMAND_TIMEOUT_S
            )
        except Exception as e:
            _logger.warning("notify.command failed: %s", e)
