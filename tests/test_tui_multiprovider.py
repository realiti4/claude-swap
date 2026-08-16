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


# ---- menu bar ----------------------------------------------------------


def test_menubar_tags_codex_rows_and_keys_them_by_row(temp_home: Path):
    """The menu bar's tuple is 8-wide and unpacked exactly in five places, so
    the provider rides in the existing `num` and label fields rather than a
    ninth element."""
    from claude_swap.menubar import _adapt_snapshot

    snap = AccountsSnapshot(
        active_number="1",
        accounts=(_acc("1", "claude", is_active=True), _acc("1", "codex")),
        taken_at=0.0,
        provider="multi",
    )
    adapted = _adapt_snapshot(snap)

    nums = [row[0] for row in adapted["accounts"]]
    labels = [row[1] for row in adapted["accounts"]]
    assert nums == ["1", "codex:1"]
    assert "(codex)" in labels[1]
    assert "(codex)" not in labels[0]
    assert all(len(row) == 8 for row in adapted["accounts"])


def test_the_menubar_title_still_tracks_the_claude_account(temp_home: Path):
    """It is what the user's `claude` command will run as."""
    from claude_swap.menubar import _adapt_snapshot

    snap = AccountsSnapshot(
        active_number="1",
        accounts=(
            _acc("1", "claude", is_active=True, email="me@claude"),
            _acc("1", "codex", is_active=True, email="me@codex"),
        ),
        taken_at=0.0,
        provider="multi",
    )
    assert _adapt_snapshot(snap)["active_email"] == "me@claude"


def test_menubar_row_resolution_routes_to_the_owning_provider(temp_home: Path):
    from claude_swap.menubar import _resolve_menu_row

    class App:
        switcher = _FakeProvider("claude")
        providers = [switcher, _FakeProvider("codex")]

    app = App()
    assert _resolve_menu_row(app, "1") == (app.switcher, "1")
    assert _resolve_menu_row(app, "codex:2")[0].provider_id == "codex"
    assert _resolve_menu_row(app, "codex:2")[1] == "2"


# ---- the real screen, driven ------------------------------------------
#
# The dispatch-bug class this stage kept producing (fetch semantics, action
# ids) lives at integration seams that hand-rolled fakes cannot reach. These
# drive the actual dashboard.


class _PilotProvider:
    """A provider the real app can take snapshots from."""

    def __init__(self, provider_id: str, numbers, backup_dir: Path):
        self.provider_id = provider_id
        self.backup_dir = backup_dir
        self._numbers = list(numbers)
        self.calls: list[tuple] = []

    def accounts_snapshot(self, fetch=None):
        self.calls.append(("snapshot", fetch))
        return AccountsSnapshot(
            active_number=self._numbers[0],
            accounts=tuple(
                _acc(n, self.provider_id, is_active=(n == self._numbers[0]))
                for n in self._numbers
            ),
            taken_at=0.0,
            provider=self.provider_id,
        )

    def switch_to(self, number, **kw):
        self.calls.append(("switch", number))
        return None

    def set_account_disabled(self, number, disabled):
        self.calls.append(("set_disabled", number, disabled))

    def remove_account(self, number, assume_yes=False):
        self.calls.append(("remove", number, assume_yes))

    def current_account_number(self):
        return self._numbers[0]

    def switchable_account_numbers(self):
        return list(self._numbers)

    def resolve_account(self, identifier):
        return identifier, f"{self.provider_id}-{identifier}@x", ""

    def set_alias(self, identifier, alias):
        return identifier, alias

    def unset_alias(self, identifier):
        return identifier


@pytest.mark.asyncio
class TestDashboardWithTwoProviders:
    def _app(self, tmp_path: Path):
        from claude_swap.tui.app import CswapApp

        claude = _PilotProvider("claude", ["1", "2"], tmp_path)
        codex = _PilotProvider("codex", ["1", "2"], tmp_path)
        app = CswapApp(claude)
        app.providers = [claude, codex]
        from claude_swap.providers.aggregate import MultiSnapshotSource

        app.source = MultiSnapshotSource([claude, codex])
        return app, claude, codex

    async def test_both_providers_rows_reach_the_switch_screen(self, tmp_path):
        from textual.widgets import ListView

        from claude_swap.tui.widgets import AccountItem

        app, _claude, _codex = self._app(tmp_path)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            items = list(app.screen.query_one("#accounts", ListView).query(AccountItem))
            assert [i.key_id for i in items] == [
                "claude:1",
                "claude:2",
                "codex:1",
                "codex:2",
            ]

    async def test_selecting_a_codex_row_switches_codex_not_claude(self, tmp_path):
        """The failure the composite key exists to prevent, driven through the
        real screen rather than asserted against a fake."""
        from textual.widgets import ListView

        app, claude, codex = self._app(tmp_path)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            listview = app.screen.query_one("#accounts", ListView)
            listview.index = 2  # the first codex row
            await pilot.pause()
            await pilot.press("enter")
            for _ in range(6):
                await pilot.pause()

        assert ("switch", "1") in codex.calls
        assert not any(c[0] == "switch" for c in claude.calls)

    async def test_the_codex_badge_is_visible_on_screen(self, tmp_path):
        app, _claude, _codex = self._app(tmp_path)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await pilot.pause()
            rendered = app.screen.query_one("#accounts-panel").render().plain

        assert "codex" in rendered
