"""Tests for the menu bar's re-login alerts (pure helpers, no rumps)."""

from __future__ import annotations

from claude_swap.menubar_alerts import ReloginAlerts, relogin_menu_label, relogin_notice

YAHOO = ("2", "b@yahoo.com", "yahoo")
GMAIL = ("3", "c@gmail.com", "")


def test_announces_an_account_once_while_it_stays_broken():
    alerts = ReloginAlerts()
    assert alerts.update([(YAHOO, True)]) == [YAHOO]
    assert alerts.update([(YAHOO, True)]) == []
    assert alerts.accounts == [YAHOO]


def test_announces_only_the_newly_broken_account():
    alerts = ReloginAlerts()
    alerts.update([(YAHOO, True)])
    assert alerts.update([(YAHOO, True), (GMAIL, True)]) == [GMAIL]
    assert alerts.accounts == [YAHOO, GMAIL]


def test_recovery_clears_the_alert_and_a_relapse_is_announced_again():
    alerts = ReloginAlerts()
    alerts.update([(YAHOO, True)])
    assert alerts.update([]) == []
    assert alerts.accounts == []
    assert alerts.update([(YAHOO, True)]) == [YAHOO]


def test_an_unreadable_pass_keeps_a_known_alert_without_reannouncing():
    alerts = ReloginAlerts()
    alerts.update([(YAHOO, True)])
    assert alerts.update([(YAHOO, False)]) == []
    assert alerts.accounts == [YAHOO]
    assert alerts.update([(YAHOO, True)]) == []


def test_an_unreadable_pass_alone_raises_no_alert():
    alerts = ReloginAlerts()
    assert alerts.update([(GMAIL, False)]) == []
    assert alerts.accounts == []


def test_slot_reused_by_another_login_is_announced_as_new():
    alerts = ReloginAlerts()
    alerts.update([(YAHOO, True)])
    other = ("2", "other@x.com", "")
    assert alerts.update([(other, True)]) == [other]
    assert alerts.accounts == [other]


def test_alias_rename_alone_does_not_reannounce():
    alerts = ReloginAlerts()
    alerts.update([(YAHOO, True)])
    renamed = ("2", "b@yahoo.com", "renamed")
    assert alerts.update([(renamed, True)]) == []
    assert alerts.accounts == [renamed]


def test_notice_names_the_account_and_the_recovery_command():
    assert relogin_notice(YAHOO) == (
        "Re-login needed: yahoo",
        "Account 2 (b@yahoo.com) has a dead refresh token, so auto-switch "
        "skips it. Log in as b@yahoo.com with Claude Code, then run: cswap add",
    )


def test_notice_and_menu_label_fall_back_to_email_without_alias():
    assert relogin_notice(GMAIL)[0] == "Re-login needed: c@gmail.com"
    assert relogin_menu_label(GMAIL) == "⚠️ 3  c@gmail.com needs re-login…"
    assert relogin_menu_label(YAHOO) == "⚠️ 2  yahoo needs re-login…"
