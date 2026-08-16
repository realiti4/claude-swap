"""The TUI with two providers on screen.

The risk this file exists for: slot numbers repeat across providers, so a
keystroke aimed at "account 1" can land on the wrong CLI's account. Every test
here is ultimately about that.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_swap.models import AccountSnapshot, AccountsSnapshot
from claude_swap.tui.widgets import (
    account_card_text,
    mini_account_text,
    provider_badge,
    Palette,
)
from claude_swap.usage_store import UsageEntry


def _acc(number: str, provider: str = "claude", **kw) -> AccountSnapshot:
    base = dict(
        number=number,
        email=f"{provider}-{number}@x",
        org_name="",
        org_uuid="",
        is_active=False,
        kind="oauth",
        switchable=True,
        usage=UsageEntry(),
        provider=provider,
    )
    base.update(kw)
    return AccountSnapshot(**base)


# ---- the row badge -----------------------------------------------------


def test_a_claude_row_carries_no_badge():
    """A Claude-only install must not gain a badge on every line to say the
    only thing it could possibly say."""
    assert provider_badge(_acc("1"), Palette.DARK) is None


def test_a_codex_row_carries_a_badge():
    badge = provider_badge(_acc("1", "codex"), Palette.DARK)
    assert badge is not None and "codex" in badge.plain


def test_the_badge_appears_in_the_full_card():
    text = account_card_text(_acc("1", "codex"), 80).plain
    assert "codex" in text


def test_the_badge_appears_in_the_mini_row():
    text = mini_account_text(_acc("2", "codex"), now=0.0).plain
    assert "codex" in text


def test_a_claude_card_renders_exactly_as_before():
    """Regression guard for the Claude-only view."""
    text = account_card_text(_acc("1"), 80).plain
    assert "⟨" not in text


# ---- row identity ------------------------------------------------------


def test_two_providers_slot_one_are_different_rows():
    assert _acc("1", "claude").key != _acc("1", "codex").key


def test_the_key_is_stable_and_readable():
    assert _acc("2", "codex").key == "codex:2"


# ---- app-level dispatch ------------------------------------------------


class _FakeProvider:
    def __init__(self, provider_id: str):
        self.provider_id = provider_id
        self.switched: list[str] = []
        self.disabled: list[tuple[str, bool]] = []
        self.removed: list[str] = []

    def switch_to(self, number, **kw):
        self.switched.append(number)
        return None

    def set_account_disabled(self, number, disabled):
        self.disabled.append((number, disabled))

    def remove_account(self, number, assume_yes=False):
        self.removed.append(number)


@pytest.fixture
def app_with_two_providers(temp_home: Path):
    """A CswapApp whose owners map holds one row per provider."""
    from claude_swap.switcher import ClaudeAccountSwitcher
    from claude_swap.tui.app import CswapApp

    app = CswapApp(ClaudeAccountSwitcher())
    claude, codex = _FakeProvider("claude"), _FakeProvider("codex")
    app.providers = [claude, codex]
    app.owners = {"claude:1": claude, "codex:1": codex}
    app.snapshot = AccountsSnapshot(
        active_number="1",
        accounts=(_acc("1", "claude"), _acc("1", "codex", disabled=True)),
        taken_at=0.0,
        provider="multi",
    )
    return app, claude, codex


def test_a_bare_number_still_resolves_to_claude(app_with_two_providers):
    """Every call site that predates multi-provider keeps working."""
    app, claude, _codex = app_with_two_providers
    provider, number = app._resolve_row("1")
    assert provider is claude and number == "1"


def test_a_codex_key_resolves_to_the_codex_provider(app_with_two_providers):
    app, _claude, codex = app_with_two_providers
    provider, number = app._resolve_row("codex:1")
    assert provider is codex and number == "1"


def test_switching_a_codex_row_does_not_touch_claude(app_with_two_providers, monkeypatch):
    """The failure this whole key scheme prevents."""
    app, claude, codex = app_with_two_providers
    started: list = []
    monkeypatch.setattr(app, "_start_action", lambda label, fn: started.append(fn))

    app.do_switch("codex:1")
    started[0]()

    assert codex.switched == ["1"]
    assert claude.switched == []


def test_switching_a_claude_row_does_not_touch_codex(app_with_two_providers, monkeypatch):
    app, claude, codex = app_with_two_providers
    started: list = []
    monkeypatch.setattr(app, "_start_action", lambda label, fn: started.append(fn))

    app.do_switch("1")
    started[0]()

    assert claude.switched == ["1"]
    assert codex.switched == []


def test_toggling_a_codex_row_reads_that_rows_own_state(
    app_with_two_providers, monkeypatch
):
    """The codex row is disabled and the claude row is not; toggling must use
    the state of the row addressed, not of the same number on the other side."""
    app, claude, codex = app_with_two_providers
    started: list = []
    monkeypatch.setattr(app, "_start_action", lambda label, fn: started.append(fn))

    app.do_toggle_disabled("codex:1")
    started[0]()

    assert codex.disabled == [("1", False)]  # it was disabled -> enable
    assert claude.disabled == []


def test_removing_a_codex_row_removes_it_from_codex(app_with_two_providers, monkeypatch):
    app, claude, codex = app_with_two_providers
    started: list = []
    monkeypatch.setattr(app, "_start_action", lambda label, fn: started.append(fn))

    app._on_remove_confirm("codex:1", True)
    started[0]()

    assert codex.removed == ["1"]
    assert claude.removed == []


def test_a_declined_removal_removes_nothing(app_with_two_providers, monkeypatch):
    app, claude, codex = app_with_two_providers
    monkeypatch.setattr(app, "_start_action", lambda label, fn: pytest.fail("no action"))
    app._on_remove_confirm("codex:1", False)
    assert codex.removed == [] and claude.removed == []


def test_row_lookup_finds_the_right_provider_row(app_with_two_providers):
    app, _c, _x = app_with_two_providers
    assert app._row_for("codex:1").disabled is True
    assert app._row_for("1").disabled is False


def test_an_unknown_provider_falls_back_to_claude_instead_of_raising(
    app_with_two_providers,
):
    """Better a no-op on the default provider than a traceback in the event
    loop of a running dashboard."""
    app, _claude, _codex = app_with_two_providers
    provider, number = app._resolve_row("gemini:3")
    assert provider is app.switcher and number == "3"
