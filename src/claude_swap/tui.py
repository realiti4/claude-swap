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
import time
from typing import Callable

from claude_swap import registry
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.switcher import ClaudeAccountSwitcher


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
            ("Load balancer (Beta)", "balance"),
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
# Load balancer (Beta)
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
            f"target {cfg['targetSafety']}% · cooldown {cfg['cooldownSeconds']}s"
        )
        items: list[tuple[str, str | None]] = [
            ("Disable" if cfg["enabled"] else "Enable", "toggle"),
            (f"Set threshold (now {cfg['threshold']}%)", "threshold"),
            (f"Set target safety (now {cfg['targetSafety']}%)", "target"),
            (f"Set cooldown (now {cfg['cooldownSeconds']}s)", "cooldown"),
            ("Edit account priorities", "priorities"),
            ("Live dashboard", "dashboard"),
            ("-- Back --", None),
        ]
        choice = _select_from(
            stdscr, "Load balancer (Beta)", items=items, subtitle=subtitle
        )
        if choice is None:
            return
        if choice == "toggle":
            switcher.set_auto_balance_config(enabled=not cfg["enabled"])
        elif choice == "threshold":
            _set_balance_int(stdscr, switcher, "threshold", "Migrate when usage reaches (%): ")
        elif choice == "target":
            _set_balance_int(stdscr, switcher, "target_safety", "Target safety ceiling (%): ")
        elif choice == "cooldown":
            _set_balance_int(stdscr, switcher, "cooldown_seconds", "Min seconds between migrations: ")
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


def _balancer_dashboard(stdscr, switcher: ClaudeAccountSwitcher) -> None:
    """Read-only live view of managed sessions and accounts (event-driven).

    Refreshes on a timer for liveness; performs NO migrations (the supervisors
    do). Idle-account usage is shown only from the shared cache — no network in
    the UI loop — so the view stays snappy.
    """
    curses.curs_set(0)
    stdscr.timeout(2000)
    try:
        while True:
            reg = registry.read_registry(switcher)
            sessions = registry.live_sessions(reg)
            acct_views, _ = registry.build_world(switcher, reg, fetch_idle=False)
            _draw_dashboard(stdscr, switcher, sessions, acct_views)
            key = stdscr.getch()
            if key in (27, ord("q"), ord("Q")):
                return
    finally:
        stdscr.timeout(-1)


def _draw_dashboard(stdscr, switcher, sessions, acct_views) -> None:
    stdscr.erase()
    rows, cols = stdscr.getmaxyx()
    cfg = switcher.get_auto_balance_config()
    state = "ON" if cfg["enabled"] else "OFF"
    _draw_header(
        stdscr,
        "Load balancer dashboard (Beta)",
        f"{state} · threshold {cfg['threshold']}% · {len(sessions)} managed session(s)",
        cols,
    )
    y = 4
    if not sessions:
        stdscr.addstr(y, 2, "No managed sessions. Start one with `cswap launch`."[: cols - 4])
        y += 2
    else:
        stdscr.addstr(y, 2, "Sessions:"[: cols - 4], curses.A_BOLD)
        y += 1
        now = time.time()
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
        stdscr.addstr(y, 2, "Accounts:"[: cols - 4], curses.A_BOLD)
        y += 1
        for num in sorted(acct_views, key=lambda n: (-acct_views[n].priority, n)):
            if y >= rows - 2:
                break
            av = acct_views[num]
            pct_s = f"{av.max_pct:.0f}%" if av.max_pct is not None else "  —"
            stdscr.addstr(y, 2, f"  a{num:<3} pri {av.priority:<3} {pct_s:>5}"[: cols - 4])
            y += 1

    footer = "[q/Esc] back  ·  refreshes automatically"
    stdscr.addstr(rows - 1, 2, footer[: cols - 4], curses.A_DIM)
    stdscr.refresh()


def _ascii_bar(pct: float | None, width: int = 8) -> str:
    if pct is None:
        return "[--------] —"
    filled = max(0, min(width, round(pct / 100 * width)))
    return "[" + "#" * filled + "-" * (width - filled) + f"] {pct:.0f}%"


def _short_countdown(secs: float) -> str:
    secs = max(0, int(secs))
    hours, rem = divmod(secs, 3600)
    mins = rem // 60
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
