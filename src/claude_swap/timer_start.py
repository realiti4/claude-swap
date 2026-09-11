"""Start a Claude subscription's five-hour window with one inert message.

The usage endpoint is read-only: once the auto engine observes that a
five-hour window rolled over, a real Claude request is required to start the
next rolling timer.  This module makes that request through the installed
Claude Code CLI instead of reproducing its private OAuth message protocol.

Inactive accounts run in their existing ``cswap run`` profile (or a freshly
bootstrapped private one), so the user's active default login never changes.
The prompt is non-interactive, has no tools, and is not added to conversation
history.  Account selection and reset idempotency live in ``autoswitch.py``;
this module performs exactly one requested attempt and reports its result.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass

from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.session import (
    AUTH_OVERRIDE_ENV_VARS,
    SessionManager,
    session_dir_for,
    session_identity_drifted,
)

_logger = logging.getLogger("claude-swap")

TIMER_PROMPT = "Reply OK."
TIMER_SYSTEM_PROMPT = "Reply with exactly OK."
TIMER_TIMEOUT_S = 90.0


@dataclass(frozen=True)
class TimerStartResult:
    """Outcome of one headless Claude request."""

    success: bool
    error: str | None = None


def _account_env(session_dir: str | None) -> dict[str, str]:
    """Build an OAuth-only environment for the selected Claude profile."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in AUTH_OVERRIDE_ENV_VARS
    }
    if session_dir is not None:
        # Claude 2.1.220+ lets this override CLAUDE_CONFIG_DIR for credential
        # storage. An inherited value would silently authenticate the
        # one-shot process as the override profile instead of this account.
        env.pop("CLAUDE_SECURESTORAGE_CONFIG_DIR", None)
        env["CLAUDE_CONFIG_DIR"] = session_dir
    return env


def start_five_hour_timer(
    switcher,
    number: str,
    email: str,
    *,
    timeout_s: float = TIMER_TIMEOUT_S,
) -> TimerStartResult:
    """Send one minimal Claude message as ``number`` without switching login.

    Exit status is the primary success signal.  Newer Claude Code versions
    also return an ``is_error`` JSON member; honor it when present, while
    remaining compatible with versions whose successful JSON shape differs.
    No response text is surfaced because it can include provider diagnostics
    that do not belong in the auto-switch event stream.
    """
    claude_bin = shutil.which("claude")
    if not claude_bin:
        return TimerStartResult(
            False, "'claude' was not found on PATH; install Claude Code first"
        )

    try:
        used_active_profile = switcher.current_account_number() == number
        if used_active_profile:
            # The active account already has one authoritative credential
            # store.  Reuse it rather than creating a second refresh-token
            # copy; auth override variables are scrubbed so an exported API
            # key cannot silently send the request as somebody else.
            env = _account_env(None)
        else:
            profile = session_dir_for(switcher.backup_dir, number, email)
            if (
                switcher.live_session_pids_for(number, email)
                and session_identity_drifted(
                    profile,
                    email,
                    switcher.account_identity(number).get("organizationUuid", ""),
                )
            ):
                return TimerStartResult(
                    False,
                    "the account's live session profile is logged in as a "
                    "different account",
                )

            # Do not alter an existing interactive profile's share/no-share
            # choice merely to send this internal one-shot prompt.
            profile, resolved_number, resolved_email = SessionManager(
                switcher
            ).setup_session(
                number,
                share=False,
                share_history=False,
                sync_sharing=False,
            )
            if resolved_number != number or resolved_email != email:
                return TimerStartResult(
                    False, "the stored account identity changed before the request"
                )
            env = _account_env(str(profile))

        argv = [
            claude_bin,
            "--print",
            "--output-format",
            "json",
            "--no-session-persistence",
            "--tools",
            "",
            "--system-prompt",
            TIMER_SYSTEM_PROMPT,
            TIMER_PROMPT,
        ]
        # A blank trusted directory prevents project CLAUDE.md files from
        # inflating or influencing this deliberately inert request.
        with tempfile.TemporaryDirectory(prefix="claude-swap-timer-") as cwd:
            completed = subprocess.run(
                argv,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout_s,
                check=False,
            )
    except subprocess.TimeoutExpired:
        return TimerStartResult(False, f"Claude Code timed out after {timeout_s:g}s")
    except ClaudeSwitchError as exc:
        return TimerStartResult(False, str(exc))
    except OSError as exc:
        return TimerStartResult(False, f"could not launch Claude Code: {exc}")

    if completed.returncode != 0:
        _logger.debug(
            "Five-hour timer prompt for account %s failed (rc=%s): %s",
            number,
            completed.returncode,
            completed.stderr[:500],
        )
        return TimerStartResult(
            False, f"Claude Code exited with status {completed.returncode}"
        )

    # A concurrent auto surface can switch the default profile while the
    # child is starting. We cannot prove which credential it read in that
    # race, so do not mark this account's reset handled; the fenced retry will
    # use its isolated profile now that it is inactive.
    if used_active_profile and switcher.current_account_number() != number:
        return TimerStartResult(
            False, "the active account changed while Claude Code was starting"
        )

    try:
        payload = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError):
        payload = None
    if isinstance(payload, dict) and payload.get("is_error") is True:
        return TimerStartResult(False, "Claude Code reported an API error")
    return TimerStartResult(True)
