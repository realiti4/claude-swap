"""Guided setup and diagnostics for the optional Chrome extension sync.

Chrome sync is an opt-in add-on (like the menu bar app): cswap always switches
the Claude Code CLI account; enabling this also switches the "Claude in Chrome"
extension's account, via a dedicated, lean Chrome profile that runs alongside
the user's everyday browser.

``print_doctor`` is the single source of truth for "is it set up?" — it reports
each moving part and the per-account browser-session coverage. ``run_setup`` is
the interactive wizard that stands the whole thing up with a check after each
step. Both are macOS-only (chrome_session.is_supported()); callers get a clear
message elsewhere.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from claude_swap import chrome_session as cs
from claude_swap.printer import accent, dimmed, error, muted, yellowed
from claude_swap.settings import load_chrome_settings, set_chrome_enabled, settings_path

_OK = "✓"
_NO = "✗"


def _accounts(switcher) -> list[tuple[str, str, str]]:
    """(number, email, organizationUuid) per managed account, in slot order."""
    try:
        data = json.loads(Path(switcher.sequence_file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    accounts = data.get("accounts", {})
    out: list[tuple[str, str, str]] = []
    for num in sorted(accounts, key=lambda n: int(n) if n.isdigit() else 0):
        acc = accounts[num]
        out.append((num, acc.get("email", ""), acc.get("organizationUuid", "") or ""))
    return out


def _mark(ok: bool) -> str:
    return accent(_OK) if ok else yellowed(_NO)


# --------------------------------------------------------------------------- #
# Doctor
# --------------------------------------------------------------------------- #
def print_doctor(switcher) -> None:
    """Report the state of every Chrome-sync moving part + per-account coverage."""
    root = switcher.backup_dir
    settings = load_chrome_settings(root)

    print(accent("Claude Browser sync"))
    if not cs.is_supported():
        print(f"  {yellowed(_NO)} not supported on this platform (macOS only)")
        return
    print(f"  feature:     {_mark(settings.enabled)} "
          f"{'enabled' if settings.enabled else 'disabled — run `cswap chrome enable`'}")

    udd = cs.resolve_automation_dir(settings, root)
    prof_ok = (udd / "Default").exists()
    ext_ok = cs.has_claude_extension(udd)
    print(f"  profile:     {_mark(prof_ok)} {udd}")
    print(f"  extension:   {_mark(ext_ok)} "
          f"{'installed' if ext_ok else 'not installed — run `cswap chrome setup`'}")

    running = cs.automation_chrome_running(settings.debug_port)
    if running:
        ident = cs.read_identity_cdp(settings.debug_port)
        who = ident.get("email") or "(extension not connected)"
        print(f"  running:     {accent(_OK)} port {settings.debug_port} — {who}")
    else:
        print(f"  running:     {dimmed('· not running (opens on demand)')}")

    # Per-account browser-session coverage.
    vault = cs.ChromeVault(root)
    accounts = _accounts(switcher)
    print(accent("  browser sessions (per account):"))
    if not accounts:
        print(f"    {dimmed('no managed accounts yet — run `cswap add`')}")
    for num, email, _org in accounts:
        has = vault.get(num) is not None
        hint = "" if has else muted(f"  → connect + `cswap chrome capture {num}`")
        print(f"    {_mark(has)} {num} {email or '(unknown)'}{hint}")

    print(dimmed("\n  Note: connect the Claude Browser extension to Claude Code (and"))
    print(dimmed("  disconnect your everyday Chrome) so browser automation uses this profile."))


# --------------------------------------------------------------------------- #
# Setup wizard
# --------------------------------------------------------------------------- #
def _confirm(prompt: str, default_yes: bool = True) -> bool:
    suffix = "[Y/n]" if default_yes else "[y/N]"
    try:
        ans = input(f"{prompt} {suffix} ").strip().lower()
    except EOFError:
        return default_yes
    if not ans:
        return default_yes
    return ans in ("y", "yes")


def _pause(prompt: str) -> None:
    try:
        input(f"{prompt} {dimmed('(press Enter)')} ")
    except EOFError:
        pass


def run_setup(switcher) -> int:
    """Interactive wizard: stand up Chrome sync with a check after each step."""
    if not cs.is_supported():
        error("Chrome sync is macOS-only.")
        return 1
    if not sys.stdin.isatty():
        error("`cswap chrome setup` needs an interactive terminal.")
        return 1

    root = switcher.backup_dir
    print(accent("Claude Browser sync (optional)"))
    print(
        "cswap already switches your Claude Code CLI account. This add-on also\n"
        "switches the Claude browser extension's account, using a dedicated, lean\n"
        "Chrome profile that runs alongside your everyday browser.\n"
    )
    print(dimmed("Two logins are involved:"))
    print(dimmed("  • CLI     — the `claude` command's account (Keychain OAuth)"))
    print(dimmed("  • Browser — the Claude in Chrome extension's OAuth pairing\n"))
    if not _confirm("Set it up now?", default_yes=False):
        print(dimmed("Nothing changed. You can run `cswap chrome setup` any time."))
        return 0

    # Step 0 — enable the feature.
    settings = load_chrome_settings(root)
    if not settings.enabled:
        set_chrome_enabled(root, True)
        settings = load_chrome_settings(root)
        print(f"[0/4] {accent(_OK)} feature enabled (settings.json chrome.enabled)")
    udd = cs.resolve_automation_dir(settings, root)

    # Step 1 — create the dedicated profile.
    cs.ensure_automation_profile(udd)
    cs.set_profile_display_name(udd)
    print(f"[1/4] {accent(_OK)} profile ready at {udd}")

    # Step 2 — install the extension (guided; verified by polling).
    print("[2/4] Installing the Claude extension…")
    print(dimmed("      A Chrome window will open on the Web Store. Click \"Add to Chrome\"."))
    cs.launch_automation_chrome(udd, settings.debug_port, start_url=cs.CLAUDE_EXTENSION_STORE_URL)
    for _ in range(60):
        if cs.has_claude_extension(udd):
            break
        _pause("      After installing the extension,")
        if cs.has_claude_extension(udd):
            break
    if cs.has_claude_extension(udd):
        print(f"      {accent(_OK)} extension detected")
    else:
        print(f"      {yellowed('!')} extension not detected yet — you can finish it later")

    # Step 3 — connect to Claude Code (cannot be verified from here; instruct).
    print("[3/4] Connect to Claude Code")
    print(dimmed("      In the Claude Browser window, connect the extension to Claude Code,"))
    print(dimmed("      and disconnect your everyday Chrome so only this one is used."))
    _pause("      When done,")

    # Step 4 — capture each account's browser session.
    print("[4/4] Capture each account's browser session")
    accounts = _accounts(switcher)
    if not accounts:
        print(dimmed("      No managed accounts yet — add them with `cswap add`, then"))
        print(dimmed("      `cswap chrome capture <n>` for each."))
    sync = cs.ChromeSync(settings, root)
    for num, email, _org in accounts:
        print(f"      Account {num} ({email or 'unknown'}):")
        print(dimmed("        connect the extension to this account in the Claude Browser"))
        _pause("        then")
        if sync.capture_from_browser(num):
            print(f"        {accent(_OK)} captured")
        else:
            print(f"        {yellowed('!')} not captured (was it logged in?) "
                  f"— retry: cswap chrome capture {num}")

    print(accent("\nDone.") + f" Verify anytime with {accent('cswap chrome doctor')}.")
    print(dimmed(f"Settings: {settings_path(root)}"))
    return 0
