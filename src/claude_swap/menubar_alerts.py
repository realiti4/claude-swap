"""Re-login alerts for the macOS menu bar.

An account whose refresh token is dead ("re-login needed") drops out of
auto-switch rotation, and nothing recovers it except a human logging in with
it again. Shown only as a note on its dropdown row, the state goes unnoticed
until auto-switch runs out of other accounts to move to — so the menu bar also
badges its title, lists the account at the top of the menu, and posts a
notification when an account enters the state.

Pure helpers (no rumps), so they can be unit-tested without AppKit.
"""

from __future__ import annotations

ALERT_ICON = "⚠️"

#: ``(number, email, alias)`` of an account that needs re-login.
ReloginAccount = tuple[str, str, str]

#: An account as observed in one snapshot: ``confirmed`` is True when it
#: needs re-login, False when this pass couldn't tell (no readable credential).
ReloginObservation = tuple[ReloginAccount, bool]


def relogin_menu_label(account: ReloginAccount) -> str:
    """Top-of-menu line for an account that needs re-login."""
    num, email, alias = account
    return f"{ALERT_ICON} {num}  {alias or email} needs re-login…"


def relogin_notice(account: ReloginAccount) -> tuple[str, str]:
    """``(headline, body)`` shared by the notification and the menu item's alert."""
    num, email, alias = account
    return (
        f"Re-login needed: {alias or email}",
        f"Account {num} ({email}) has a dead refresh token, so auto-switch "
        f"skips it. Log in as {email} with Claude Code, then run: cswap add",
    )


def _key(account: ReloginAccount) -> tuple[str, str]:
    # (number, email): a slot re-used by a different login is a new account,
    # while an alias rename is not.
    return account[0], account[1]


class ReloginAlerts:
    """Tracks which accounts need re-login across snapshots.

    ``accounts`` is what the title badge and menu show. An account enters it
    when a snapshot confirms the state, and stays through passes that can't
    tell (a credential-read failure hides the dead-token verdict, and dropping
    the alert there would make it flicker and re-notify). It leaves
    once a snapshot shows anything else: recovered, disabled, or removed.
    """

    def __init__(self) -> None:
        self.accounts: list[ReloginAccount] = []

    def update(self, observed: list[ReloginObservation]) -> list[ReloginAccount]:
        """Apply one snapshot's observations; return accounts to announce.

        An account is announced once on entering the state (including one
        already there at startup), and again only after it has left it.
        """
        previous = {_key(acc) for acc in self.accounts}
        self.accounts = [
            acc for acc, confirmed in observed if confirmed or _key(acc) in previous
        ]
        return [
            acc for acc, confirmed in observed if confirmed and _key(acc) not in previous
        ]
