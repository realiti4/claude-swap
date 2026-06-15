"""Curses-based interactive TUI for claude-swap.

Activated via ``cswap --tui``. Provides a single-level arrow-key menu over
the existing CLI commands, so users don't have to memorize flags.

The TUI never re-implements account logic — every action shells out to the
existing ``ClaudeAccountSwitcher`` methods. It exists purely as a navigation
layer.

Design constraints:
    * Zero new runtime dependencies (uses stdlib ``curses``).
    * Falls back gracefully when terminal is too small or curses is missing.
    * After each action, returns to the main menu (does not auto-exit).
"""

from __future__ import annotations

import curses
import sys
import threading
import time
from typing import Callable

from claude_swap import balancer, registry
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.switcher import DEFAULT_QUICK_START_COMMAND, ClaudeAccountSwitcher


# Minimum terminal size we render in. Below this, we bail to plain CLI advice.
_MIN_ROWS = 12
_MIN_COLS = 60


def run(switcher: ClaudeAccountSwitcher) -> int:
    """Entry point for ``cswap --tui``. Returns process exit code."""
    try:
        return curses.wrapper(_main_loop, switcher)
    except _ExitRequested:
        return 0


# ---------------------------------------------------------------------------
# Main menu loop
# ---------------------------------------------------------------------------


class _ExitRequested(Exception):
    """Internal signal to break out of the curses loop."""


def _main_loop(stdscr: "curses._CursesWindow", switcher: ClaudeAccountSwitcher) -> int:
    rows, cols = stdscr.getmaxyx()
    if rows < _MIN_ROWS or cols < _MIN_COLS:
        curses.endwin()
        sys.stderr.write(
            f"Terminal too small for TUI ({rows}x{cols}, need at least "
            f"{_MIN_ROWS}x{_MIN_COLS}). Use the regular CLI flags instead.\n"
        )
        return 2

    curses.curs_set(0)  # hide cursor
    has_token_flow = hasattr(switcher, "add_account_from_token")

    while True:
        items: list[tuple[str, str]] = [
            ("Switch account", "switch"),
            ("Add account", "add"),
            ("Remove account", "remove"),
            ("Refresh credentials (current login, in-place)", "refresh"),
            ("List accounts (with usage)", "list"),
            ("Status", "status"),
            ("Auto-swap + multi-session load balancer (beta)", "balance"),
            ("Quick start (default `cswap` command)", "quickstart"),
            ("Quit", "quit"),
        ]
        choice = _select_from(
            stdscr,
            title="claude-swap",
            subtitle=_status_line(switcher),
            items=items,
        )
        if choice in (None, "quit"):
            return 0

        try:
            if choice == "switch":
                _do_switch(stdscr, switcher)
            elif choice == "add":
                _do_add(stdscr, switcher, has_token_flow)
            elif choice == "remove":
                _do_remove(stdscr, switcher)
            elif choice == "refresh":
                _do_refresh(stdscr, switcher)
            elif choice == "list":
                _shell_out(stdscr, lambda: switcher.list_accounts())
            elif choice == "status":
                _shell_out(stdscr, switcher.status)
            elif choice == "balance":
                _do_balancer(stdscr, switcher)
            elif choice == "quickstart":
                _do_quick_start(stdscr, switcher)
        except ClaudeSwitchError as e:
            _show_message(stdscr, f"Error: {e}", is_error=True)
        except KeyboardInterrupt:
            _show_message(stdscr, "Operation cancelled.")


# ---------------------------------------------------------------------------
# Sub-flows
# ---------------------------------------------------------------------------


def _do_switch(stdscr, switcher: ClaudeAccountSwitcher) -> None:
    items = _account_items(switcher)
    if not items:
        _show_message(stdscr, "No managed accounts. Add one first.")
        return
    items.append(("-- Cancel --", None))
    choice = _select_from(stdscr, "switch to", items=items)
    if choice is None:
        return
    _shell_out(stdscr, lambda: switcher.switch_to(choice))


def _do_add(stdscr, switcher: ClaudeAccountSwitcher, has_token_flow: bool) -> None:
    items: list[tuple[str, str]] = [
        ("From current Claude Code login   (cswap --add-account)", "login"),
    ]
    if has_token_flow:
        items.append(
            ("From a setup-token              (cswap --add-token)", "token")
        )
    items.append(("-- Cancel --", None))

    choice = _select_from(stdscr, "add account", items=items)
    if choice is None:
        return

    if choice == "login":
        _shell_out(stdscr, switcher.add_account)
        return

    # choice == "token"
    email = _prompt_text(stdscr, "Email for this token: ")
    if not email:
        return
    token = _prompt_text(stdscr, "Setup token: ", password=True)
    if not token:
        return
    _shell_out(
        stdscr,
        lambda: switcher.add_account_from_token(token=token, email=email, slot=None),
    )


def _do_remove(stdscr, switcher: ClaudeAccountSwitcher) -> None:
    items = _account_items(switcher)
    if not items:
        _show_message(stdscr, "No managed accounts.")
        return
    items.append(("-- Cancel --", None))
    choice = _select_from(stdscr, "remove which account?", items=items)
    if choice is None:
        return
    if not _confirm(stdscr, f"Remove account {choice}? Type 'y' to confirm: "):
        return
    _shell_out(stdscr, lambda: switcher.remove_account(choice))


def _do_refresh(stdscr, switcher: ClaudeAccountSwitcher) -> None:
    identity = switcher._get_current_account()
    if identity is None:
        _show_message(
            stdscr,
            "No active Claude Code login detected. Log in first, then retry.",
            is_error=True,
        )
        return
    email, _org = identity
    _shell_out(stdscr, lambda: switcher.add_account(slot=None))


# ---------------------------------------------------------------------------
# Auto-swap + multi-session load balancer (beta)
# ---------------------------------------------------------------------------


def run_balance(switcher: ClaudeAccountSwitcher) -> int:
    """Entry point for ``cswap --balance``. Opens the balancer page directly."""
    try:
        return curses.wrapper(_balance_entry, switcher)
    except _ExitRequested:
        return 0


def _balance_entry(stdscr, switcher: ClaudeAccountSwitcher) -> int:
    rows, cols = stdscr.getmaxyx()
    if rows < _MIN_ROWS or cols < _MIN_COLS:
        curses.endwin()
        sys.stderr.write(
            f"Terminal too small ({rows}x{cols}, need at least "
            f"{_MIN_ROWS}x{_MIN_COLS}).\n"
        )
        return 2
    curses.curs_set(0)
    _do_balancer(stdscr, switcher)
    return 0


def _do_balancer(stdscr, switcher: ClaudeAccountSwitcher) -> None:
    """Settings + live dashboard for the event-driven load balancer (Beta).

    The balancer is driven entirely by each managed session's statusline — there
    is no polling loop and no daemon. When an account crosses the threshold, its
    sessions migrate to a higher-priority account with headroom, or pause until
    the limit resets. This page only configures it and shows a read-only view;
    it never performs migrations itself (the per-session supervisors do).
    """
    while True:
        cfg = switcher.get_auto_balance_config()
        state = "ON" if cfg["enabled"] else "OFF"
        subtitle = (
            f"Beta · {state} · threshold {cfg['threshold']}% · "
            f"target {cfg['targetSafety']}%"
        )
        prime_state = "ON" if cfg.get("primeIdleWindows") else "OFF"
        items: list[tuple[str, str | None]] = [
            ("Disable" if cfg["enabled"] else "Enable", "toggle"),
            (f"Set threshold (now {cfg['threshold']}%)", "threshold"),
            (f"Set target safety (now {cfg['targetSafety']}%)", "target"),
            (f"Keep 5h sessions warm (spends a very small amount of credits): {prime_state}", "prime"),
            ("Edit account priorities", "priorities"),
            ("Live dashboard", "dashboard"),
            ("-- Back --", None),
        ]
        choice = _select_from(
            stdscr,
            "Auto-swap + multi-session load balancer (beta)",
            items=items,
            subtitle=subtitle,
        )
        if choice is None:
            return
        if choice == "toggle":
            switcher.set_auto_balance_config(enabled=not cfg["enabled"])
        elif choice == "threshold":
            _set_balance_int(stdscr, switcher, "threshold", "Migrate when usage reaches (%): ")
        elif choice == "target":
            _set_balance_int(stdscr, switcher, "target_safety", "Target safety ceiling (%): ")
        elif choice == "prime":
            # Default-OFF, credit-spending opt-in (feature #3): toggle the
            # dedicated primeIdleWindows flag, independent of the balancer enable.
            switcher.set_auto_balance_config(
                prime_idle_windows=not cfg.get("primeIdleWindows")
            )
        elif choice == "priorities":
            _edit_priorities(stdscr, switcher)
        elif choice == "dashboard":
            _balancer_dashboard(stdscr, switcher)


def _set_balance_int(stdscr, switcher: ClaudeAccountSwitcher, field: str, label: str) -> None:
    raw = _prompt_text(stdscr, label)
    if not raw:
        return
    try:
        value = int(raw)
    except ValueError:
        _show_message(stdscr, "Please enter a whole number.", is_error=True)
        return
    try:
        switcher.set_auto_balance_config(**{field: value})
    except ClaudeSwitchError as e:
        _show_message(stdscr, f"Invalid value: {e}", is_error=True)


def _edit_priorities(stdscr, switcher: ClaudeAccountSwitcher) -> None:
    """Set per-account balancing priority (higher = burned through first)."""
    while True:
        seq = switcher._get_sequence_data_migrated() or {}
        accounts = seq.get("accounts", {})
        if not accounts:
            _show_message(stdscr, "No managed accounts.")
            return
        items: list[tuple[str, str | None]] = []
        for num in sorted(seq.get("sequence", []), key=int):
            acc = accounts.get(str(num), {})
            email = acc.get("email", "?")
            pri = switcher.get_account_priority(str(num))
            items.append((f"{num}  {email:<28.28}  priority {pri}", str(num)))
        items.append(("-- Back --", None))
        choice = _select_from(
            stdscr,
            "Edit priorities — pick an account",
            items=items,
            subtitle="Higher priority is burned through first",
        )
        if choice is None:
            return
        raw = _prompt_text(stdscr, f"New priority for account {choice}: ")
        if not raw:
            continue
        try:
            switcher.set_account_priority(choice, int(raw))
        except ValueError:
            _show_message(stdscr, "Priority must be a whole number.", is_error=True)
        except ClaudeSwitchError as e:
            _show_message(stdscr, f"Error: {e}", is_error=True)


def _do_quick_start(stdscr, switcher: ClaudeAccountSwitcher) -> None:
    """Configure Quick start: the default command a bare ``cswap`` runs.

    Default OFF. When on, running ``cswap`` with no recognized subcommand/flag
    runs the configured command (extra args appended), like a configurable alias.
    """
    while True:
        cfg = switcher.get_quick_start_config()
        state = "ON" if cfg["enabled"] else "OFF"
        items: list[tuple[str, str | None]] = [
            ("Disable" if cfg["enabled"] else "Enable", "toggle"),
            ("Edit command", "edit"),
            ("Reset command to default", "reset"),
            ("-- Back --", None),
        ]
        choice = _select_from(
            stdscr,
            "Quick start",
            items=items,
            subtitle=f"{state} · runs on bare `cswap` · {cfg['command']}",
        )
        if choice is None:
            return
        if choice == "toggle":
            switcher.set_quick_start_config(enabled=not cfg["enabled"])
        elif choice == "edit":
            raw = _prompt_text(stdscr, "Quick-start command: ")
            if raw:
                try:
                    switcher.set_quick_start_config(command=raw)
                except ClaudeSwitchError as e:
                    _show_message(stdscr, f"Invalid command: {e}", is_error=True)
        elif choice == "reset":
            switcher.set_quick_start_config(command=DEFAULT_QUICK_START_COMMAND)


# How often the background worker refreshes idle-account usage into the shared
# cache. The UI thread itself never fetches (it would block on network I/O and
# freeze the quit key); it only ever reads the worker-warmed cache + live signals.
_IDLE_REFRESH_S = 15
_DASH_TICK_MS = 2000


def _idle_usage_refresher(switcher: ClaudeAccountSwitcher, stop: threading.Event) -> None:
    """Background worker that keeps the shared usage cache warm for the dashboard.

    Each pass calls ``build_world(fetch_idle=True)``, which fetches every idle
    (no live session) account's usage on a cache miss and writes it into the 15s
    usage cache the UI loop reads. Runs OFF the curses thread so the (per-account,
    possibly-slow) network reads never block rendering or the quit key. Best-effort
    — a failed pass is swallowed and retried next cycle; exits when ``stop`` is set.
    """
    while not stop.is_set():
        try:
            reg = registry.read_registry(switcher)
            registry.build_world(switcher, reg, fetch_idle=True)
        except Exception:  # noqa: BLE001 - usage is best-effort; keep the worker alive
            switcher._logger.debug("dashboard idle-usage refresh failed", exc_info=True)
        stop.wait(_IDLE_REFRESH_S)


def _balancer_dashboard(stdscr, switcher: ClaudeAccountSwitcher) -> None:
    """Read-only live view of managed sessions and accounts (event-driven).

    Refreshes on a timer for liveness; performs NO migrations (the supervisors
    do). Idle-account usage (for accounts not hosting a live session) is fetched
    by a background worker that warms the shared usage cache, so every account
    shows its 5h/weekly consumption without the network read ever blocking the UI
    thread (the quit key stays responsive even when an account is slow/unreachable).
    """
    curses.curs_set(0)
    stdscr.timeout(_DASH_TICK_MS)
    stop = threading.Event()
    worker = threading.Thread(
        target=_idle_usage_refresher, args=(switcher, stop), daemon=True
    )
    worker.start()
    try:
        while True:
            reg = registry.read_registry(switcher)
            sessions = registry.live_sessions(reg)
            # UI thread NEVER hits the network — it reads the worker-warmed cache
            # and live session signals only, so rendering can't stall.
            acct_views, _ = registry.build_world(switcher, reg, fetch_idle=False)
            _draw_dashboard(stdscr, switcher, sessions, acct_views)
            key = stdscr.getch()
            if key in (27, ord("q"), ord("Q")):
                return
    finally:
        stop.set()
        stdscr.timeout(-1)


def _draw_dashboard(stdscr, switcher, sessions, acct_views) -> None:
    stdscr.erase()
    rows, cols = stdscr.getmaxyx()
    cfg = switcher.get_auto_balance_config()
    state = "ON" if cfg["enabled"] else "OFF"
    prime_on = bool(cfg.get("primeIdleWindows"))
    now = time.time()
    _draw_header(
        stdscr,
        "Auto-swap + multi-session load balancer — dashboard (beta)",
        f"{state} · threshold {cfg['threshold']}% · {len(sessions)} session(s) · "
        f"warming {'ON' if prime_on else 'OFF'}",
        cols,
    )

    # How many live sessions are pinned to each account (shown per account, so
    # even idle accounts read as "0 sess").
    sess_by_account: dict[str, int] = {}
    for e in sessions:
        acct = str(e.get("account_num", ""))
        sess_by_account[acct] = sess_by_account.get(acct, 0) + 1

    y = 4
    if not sessions:
        stdscr.addstr(y, 2, "No active sessions. Start one with `cswap launch`."[: cols - 4])
        y += 2
    else:
        stdscr.addstr(y, 2, "Sessions:"[: cols - 4], curses.A_BOLD)
        y += 1
        for i, e in enumerate(sessions):
            if y >= rows - 3:
                break
            acct = e.get("account_num", "?")
            av = acct_views.get(str(acct))
            pct = av.max_pct if av else None
            paused = e.get("paused_until")
            intent = e.get("migration")
            if isinstance(paused, (int, float)) and paused > now:
                state_s = f"paused {_short_countdown(paused - now)}"
            elif isinstance(intent, dict) and intent.get("to"):
                state_s = f"-> a{intent['to']}"
            else:
                state_s = _ascii_bar(pct)
            line = f"  s{i + 1:<2} a{acct:<3} {state_s:<16} {_abbrev(e.get('cwd', ''))}"
            stdscr.addstr(y, 2, line[: cols - 4])
            y += 1
        y += 1

    if acct_views and y < rows - 2:
        header = "Accounts:  (usage% · resets-in, per 5h and 7d window)"
        stdscr.addstr(y, 2, header[: cols - 4], curses.A_BOLD)
        y += 1
        for num in sorted(acct_views, key=lambda n: (-acct_views[n].priority, _num_key(n))):
            if y >= rows - 2:
                break
            av = acct_views[num]
            stdscr.addstr(
                y, 2,
                _account_row(av, sess_by_account.get(num, 0), now, prime_on)[: cols - 4],
            )
            y += 1

    footer = "[q/Esc] back  ·  refreshes automatically"
    stdscr.addstr(rows - 1, 2, footer[: cols - 4], curses.A_DIM)
    stdscr.refresh()


def _num_key(num: str):
    """Order numeric account ids numerically, others lexically (stable display)."""
    return (0, int(num)) if str(num).isdigit() else (1, str(num))


def _account_row(av, sess_count: int, now: float, show_warm: bool) -> str:
    """One Accounts-section line: priority, session count, 5h + 7d usage/reset.

    Idle accounts (no live session) still render their cached/fetched usage, so
    the view covers every account. ``signal == "none"`` means usage is unknown
    (logged out or a failed read) — shown explicitly rather than as a fake 0%.
    """
    head = f"  a{av.num:<3} P{av.priority:<2} {sess_count} sess"
    if av.signal == "none":
        return f"{head}  usage unavailable"
    # Fixed-width reset field so the 7d column lines up across rows regardless of
    # whether a window shows a countdown or "(no reset)".
    h5 = f"5h {_fmt_pct(av.five_hour_pct)} {_fmt_reset(av.five_hour_reset, now):<14}"
    d7 = f"7d {_fmt_pct(av.seven_day_pct)} {_fmt_reset(av.seven_day_reset, now):<14}"
    row = f"{head}  {h5} {d7}"
    if show_warm:
        warm = balancer.five_hour_warm(av)
        row += f" {'warm' if warm else 'cold' if warm is False else '?'}"
    return row


def _fmt_pct(pct: float | None) -> str:
    return f"{pct:3.0f}%" if isinstance(pct, (int, float)) else "  — "


def _fmt_reset(reset: int | None, now: float) -> str:
    """``(resets 2h13m)`` / ``(resets 4d05h)`` for a future reset.

    ``(no reset)`` means the window has no clock running (unstarted/cold — what
    ``balancer.five_hour_warm`` reports as cold), reserved for ``reset is None``.
    A reset epoch that has just elapsed (the window is rolling over but the ~15s
    usage cache hasn't refreshed yet) reads ``(resetting)`` rather than
    ``(no reset)``, so the 5h column can't contradict a ``warm`` token derived
    from the same still-present reset timestamp.
    """
    if not isinstance(reset, (int, float)):
        return "(no reset)"
    if reset <= now:
        return "(resetting)"
    return f"(resets {_short_countdown(reset - now)})"


def _ascii_bar(pct: float | None, width: int = 8) -> str:
    if pct is None:
        return "[--------] —"
    filled = max(0, min(width, round(pct / 100 * width)))
    return "[" + "#" * filled + "-" * (width - filled) + f"] {pct:.0f}%"


def _short_countdown(secs: float) -> str:
    """Compact countdown: ``4d05h`` for multi-day (weekly) resets, ``2h13m`` for
    hours, ``42m`` under an hour."""
    secs = max(0, int(secs))
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return f"{days}d{hours:02d}h"
    return f"{hours}h{mins:02d}m" if hours else f"{mins}m"


def _abbrev(path: str, maxlen: int = 28) -> str:
    return path if len(path) <= maxlen else "..." + path[-(maxlen - 3):]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _status_line(switcher: ClaudeAccountSwitcher) -> str:
    """Compact one-liner: 'Active: email [org] · N managed'. Pure-local, no network."""
    seq = switcher._get_sequence_data() or {}
    n = len(seq.get("accounts", {}))
    identity = switcher._get_current_account()
    if identity is None:
        active = "(no active login)"
    else:
        email, org = identity
        tag = "personal" if not org else org[:8]
        active = f"{email} [{tag}]"
    line = f"Active: {active}  ·  {n} managed"
    cfg = switcher.get_auto_balance_config()
    if cfg["enabled"]:
        line += f"  ·  balancer ON ({cfg['threshold']}%)"
    return line


def _account_items(switcher: ClaudeAccountSwitcher) -> list[tuple[str, str]]:
    """Build (label, account_num) list for switch/remove sub-pages.

    No network — usage % is intentionally omitted to keep the picker snappy.
    """
    seq = switcher._get_sequence_data_migrated() or {}
    accounts = seq.get("accounts", {})
    if not accounts:
        return []
    active = str(seq.get("activeAccountNumber", ""))
    items: list[tuple[str, str]] = []
    for num in sorted(seq.get("sequence", []), key=int):
        acc = accounts.get(str(num), {})
        email = acc.get("email", "?")
        org = acc.get("organizationName", "") or "personal"
        marker = "  ★ active" if str(num) == active else ""
        label = f"{num}  {email:<32}  [{org}]{marker}"
        items.append((label, str(num)))
    return items


# ---------------------------------------------------------------------------
# Curses primitives — kept thin so we can mock them in tests
# ---------------------------------------------------------------------------


def _select_from(
    stdscr,
    title: str,
    items: list[tuple[str, str | None]],
    subtitle: str = "",
) -> str | None:
    """Vertical menu picker. Returns the selected value, or ``None`` on cancel.

    ``items`` is a list of ``(label, value)`` pairs. Items whose value is
    ``None`` are treated as cancel sentinels (selecting them returns ``None``).
    """
    idx = 0
    while True:
        stdscr.erase()
        rows, cols = stdscr.getmaxyx()
        _draw_header(stdscr, title, subtitle, cols)

        for i, (label, _val) in enumerate(items):
            y = 4 + i
            if y >= rows - 2:
                break
            line = label[: cols - 6]
            if i == idx:
                stdscr.addstr(y, 2, "> ", curses.A_BOLD)
                stdscr.addstr(y, 4, line, curses.A_REVERSE)
            else:
                stdscr.addstr(y, 4, line)

        footer = "[↑/↓] move  [Enter] select  [Esc/q] cancel"
        stdscr.addstr(rows - 1, 2, footer[: cols - 4], curses.A_DIM)
        stdscr.refresh()

        key = stdscr.getch()
        if key in (curses.KEY_UP, ord("k")):
            idx = (idx - 1) % len(items)
        elif key in (curses.KEY_DOWN, ord("j")):
            idx = (idx + 1) % len(items)
        elif key in (curses.KEY_ENTER, 10, 13):
            return items[idx][1]
        elif key in (27, ord("q")):  # Esc / q
            return None


def _prompt_text(stdscr, label: str, password: bool = False) -> str | None:
    """Single-line text prompt. Returns string or ``None`` on Esc.

    When ``password`` is True, keystrokes are not echoed.
    """
    curses.curs_set(1)
    try:
        stdscr.erase()
        rows, cols = stdscr.getmaxyx()
        _draw_header(stdscr, "claude-swap", "", cols)
        stdscr.addstr(4, 2, label)
        footer = "[Enter] confirm  [Esc] cancel"
        stdscr.addstr(rows - 1, 2, footer[: cols - 4], curses.A_DIM)

        buf: list[str] = []
        cursor_x = 2 + len(label)
        while True:
            stdscr.move(4, cursor_x + len(buf))
            stdscr.refresh()
            key = stdscr.getch()
            if key == 27:  # Esc
                return None
            if key in (curses.KEY_ENTER, 10, 13):
                return "".join(buf).strip()
            if key in (curses.KEY_BACKSPACE, 127, 8):
                if buf:
                    buf.pop()
                    if password:
                        # nothing to erase visually (we never echoed)
                        pass
                    else:
                        x = cursor_x + len(buf)
                        stdscr.addstr(4, x, " ")
                        stdscr.move(4, x)
                continue
            if 32 <= key < 127:  # printable ASCII
                buf.append(chr(key))
                if not password:
                    stdscr.addstr(4, cursor_x + len(buf) - 1, chr(key))
    finally:
        curses.curs_set(0)


def _confirm(stdscr, prompt: str) -> bool:
    """Y/N prompt. Returns True only on 'y' / 'Y'."""
    answer = _prompt_text(stdscr, prompt)
    return bool(answer) and answer.lower() in ("y", "yes")


def _show_message(stdscr, msg: str, is_error: bool = False) -> None:
    """Display a single-line message and wait for any key."""
    stdscr.erase()
    rows, cols = stdscr.getmaxyx()
    _draw_header(stdscr, "claude-swap", "", cols)
    attr = curses.A_BOLD if is_error else curses.A_NORMAL
    for i, line in enumerate(msg.split("\n")):
        if 4 + i >= rows - 2:
            break
        stdscr.addstr(4 + i, 2, line[: cols - 4], attr)
    stdscr.addstr(rows - 1, 2, "[Press any key to continue]", curses.A_DIM)
    stdscr.refresh()
    stdscr.getch()


def _draw_header(stdscr, title: str, subtitle: str, cols: int) -> None:
    stdscr.addstr(1, 2, title[: cols - 4], curses.A_BOLD)
    if subtitle:
        stdscr.addstr(2, 2, subtitle[: cols - 4], curses.A_DIM)


def _shell_out(stdscr, fn: Callable[[], None]) -> None:
    """Temporarily exit curses to run ``fn`` with normal stdout/stdin.

    Pauses afterwards so the user can read output, then restores the curses
    screen.
    """
    curses.def_prog_mode()  # save curses state
    curses.endwin()
    try:
        try:
            fn()
        except ClaudeSwitchError as e:
            print(f"Error: {e}")
        except KeyboardInterrupt:
            print("\nOperation cancelled.")
        print()
        try:
            input("[Press Enter to return to TUI]")
        except (EOFError, KeyboardInterrupt):
            pass
    finally:
        curses.reset_prog_mode()  # restore curses state
        stdscr.refresh()
