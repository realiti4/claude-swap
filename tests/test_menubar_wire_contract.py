"""Wire-contract tests between the panel JS and the Python bridge.

The hosted panel protocol is two mutually-referencing stringly-typed
surfaces: ``panel.js`` sends ``bridge.send("<action>", ...)`` /
``doAction(..., "<action>", ...)`` and consumes ``window.cswap.reply/push``,
while ``bridge.py`` emits ``cswap.reply(...)``/``cswap.push(...)`` and
``app.py`` registers the handler table plus payload specs. Fixture mode
never exercises this (its ``send`` short-circuits), so a rename on either
side is otherwise a dead panel discoverable only in a manual macOS run.
These tests parse both sources with regexes and pin the contract on every
platform.
"""

from __future__ import annotations

import re
from pathlib import Path

MENUBAR = Path(__file__).resolve().parents[1] / "src" / "claude_swap" / "menubar"
WEB = MENUBAR / "web"
PANEL_JS = (WEB / "panel.js").read_text(encoding="utf-8")
SHEETS_JS = (WEB / "sheets.js").read_text(encoding="utf-8")
ICONS_JS = (WEB / "icons.js").read_text(encoding="utf-8")
APPEARANCE_JS = (WEB / "appearance.js").read_text(encoding="utf-8")
PANEL_CSS = (WEB / "panel.css").read_text(encoding="utf-8")
INDEX_HTML = (WEB / "index.html").read_text(encoding="utf-8")
APP_PY = (MENUBAR / "app.py").read_text(encoding="utf-8")
BRIDGE_PY = (MENUBAR / "bridge.py").read_text(encoding="utf-8")


def panel_actions() -> set[str]:
    """Every action literal the panel or its sheets can send to Python.

    sheets.js calls send(...) (and cswap.send via the same helper) with
    plain and ternary literals — findall catches both branches of a
    ternary because each string literal matches independently.
    """
    sent = set(re.findall(r'bridge\.send\(\s*"([A-Za-z]+)"', PANEL_JS))
    # appearance.js sends are plain literals (getPrefs/setPrefs); if it ever
    # gains a ternary send it needs the per-call-body scan sheets.js gets,
    # which would otherwise read payload strings as phantom actions
    sent |= set(re.findall(r'bridge\.send\(\s*"([A-Za-z]+)"', APPEARANCE_JS))
    # doAction(btn?, "action", payload, ...) — the fixture-mode paths
    sent |= set(re.findall(r'doAction\((?:[^,]+,\s*)?"([A-Za-z]+)"', PANEL_JS))
    # full send(...) call bodies, then every string literal inside — this
    # catches both branches of a ternary like `send(a ? "x" : "y", ...)`
    for body in re.findall(r'\bsend\((.*?)\)', SHEETS_JS):
        sent |= set(re.findall(r'"([A-Za-z]+)"', body))
    return sent


def registered_handlers() -> set[str]:
    block = APP_PY[APP_PY.index("def _panel_handlers") : APP_PY.index("def _add_from_token")]
    # table entries map to callables; nested return dicts ({"scheduled": True})
    # must not be mistaken for registrations
    return set(re.findall(r'"([A-Za-z]+)":\s*(?:lambda|do_switch|do_strategy|self\._)', block))


def payload_spec_keys() -> set[str]:
    start = APP_PY.index("payload_specs={")
    end = APP_PY.index("\n            )", start)  # closing paren of the Bridge(...) call
    return set(re.findall(r'"([A-Za-z]+)":\s*\{', APP_PY[start:end]))


class TestActionContract:
    def test_every_panel_action_has_a_handler(self) -> None:
        actions = panel_actions()
        assert actions, "no actions found — the regexes drifted?"
        missing = actions - registered_handlers()
        assert not missing, f"panel sends actions Python doesn't handle: {sorted(missing)}"

    def test_payload_bearing_actions_have_specs(self) -> None:
        # actions the panel/sheets always call with a meaningful payload
        payload_actions = {"switch", "disable", "enable", "remove",
                           "setAutoSwitch", "addFromToken"}
        missing = payload_actions - payload_spec_keys()
        assert not missing, f"payload actions without validation specs: {sorted(missing)}"

    def test_every_registered_action_is_reachable(self) -> None:
        """Capability honesty: a registered handler nothing can send is a
        dead surface (this diff dropped quit/addFromLogin reachability
        once already). Menu-only actions are the exception, declared here."""
        reachable = panel_actions()
        menu_only = {"quit", "getSnapshot", "refresh"}  # menu / boot paths
        dead = registered_handlers() - reachable - menu_only
        assert not dead, f"registered but unreachable from the panel: {sorted(dead)}"


class TestReplyErrorPreservation:
    """The handoff flagged this bug: the JS reply adapter forwarded
    result.data even on failure, so toasts showed a generic message instead
    of the backend's real error."""

    def test_reply_adapter_passes_error_through(self) -> None:
        m = re.search(r"reply:\s*\(([^)]*)\)\s*=>\s*bridge\.reply\(([^)]*)\)", PANEL_JS)
        assert m, "panel.js lost its cswap.reply adapter"
        body = m.group(2)
        assert ".error" in body, f"reply adapter drops error text: ...{body}..."

    def test_reply_adapter_distinguishes_ok(self) -> None:
        # pin the exact polarity: data on success, error on failure — an
        # inverted ternary must fail this
        assert re.search(
            r"result\.ok\s*\?\s*result\.data\s*:\s*result\.error", PANEL_JS
        ), "reply adapter polarity wrong or missing"


class TestAccountCard:
    """Final-handoff Account card: labels, index format, and terminology."""

    def test_account_card_row_labels(self) -> None:
        assert "<dt>Alias</dt>" in PANEL_JS
        for label in ("Email", "Team", "Account Index"):
            assert f'row("{label}"' in PANEL_JS, f"card lost its {label} row"

    def test_account_index_is_the_bare_number(self) -> None:
        # "Account 2" in the card row is the exact mistake the final handoff
        # calls out: the index value is just the slot number
        assert 'row("Account Index"' in PANEL_JS
        assert not re.search(r"Account \$\{", PANEL_JS), (
            "card renders 'Account N' instead of the bare index"
        )

    def test_missing_values_have_fallbacks(self) -> None:
        assert "Not available" in PANEL_JS
        assert "Not set" in PANEL_JS

    def test_alias_row_has_add_and_edit_affordances(self) -> None:
        assert 'data-act="alias-add"' in PANEL_JS
        assert 'data-act="alias-edit"' in PANEL_JS

    def test_usage_section_heading(self) -> None:
        assert ">Usage</" in PANEL_JS, "meters lost their Usage section heading"

    def test_no_empty_usage_card_for_windowless_accounts(self) -> None:
        # needs-login / API-key / never-measured accounts show only their
        # status note — never a bordered card holding just a heading
        assert "const hasContent" in PANEL_JS
        assert "if (!hasContent) return note;" in PANEL_JS

    def test_account_heading_matches_usage_style(self) -> None:
        # pre-review fix: both card headings share the section-h treatment
        # and the Account card carries no divider under its title
        assert 'class="section-h">Account<' in PANEL_JS

    def test_percent_values_carry_the_percent_sign(self) -> None:
        # pre-review fix: "68% USED", not "68USED"
        assert '<span class="unit">% USED</span>' in PANEL_JS

    def test_card_rows_ellipsize_with_full_value_on_hover(self) -> None:
        # pre-review fix: one line per row; long values ellipsize and the
        # full value is revealed by the native title tooltip
        assert 'class="val"' in PANEL_JS
        assert re.search(r'title="\$\{esc\(', PANEL_JS), (
            "card rows need a title tooltip with the full value"
        )
        assert "text-overflow: ellipsis" in PANEL_CSS

    def test_switch_button_shows_index_and_short_name(self) -> None:
        # pre-review fix: "Switch to 2" with an icon, matching Best/Rotate;
        # the full target name lives in the tooltip only
        assert '`${ic("swap", 12)} Switch to ${esc(acct.slot)}`' in PANEL_JS
        assert "Switch to ${esc(acct.slot)} (" not in PANEL_JS

    def test_no_workspace_terminology(self) -> None:
        assert "workspace" not in PANEL_JS.lower(), (
            "final handoff bans workspace wording; aliases are work/research/backup"
        )

    def test_fixture_aliases_are_work_research_backup(self) -> None:
        for alias in ('"work"', '"research"', '"backup"'):
            assert f"alias: {alias}," in PANEL_JS, f"fixture alias {alias} missing"


class TestNumberedSelector:
    """Index-tab selector: radiogroup semantics, stable index, status words."""

    def test_selector_is_a_radiogroup(self) -> None:
        assert 'role="radiogroup"' in PANEL_JS
        assert 'aria-label="Accounts"' in PANEL_JS

    def test_tabs_are_radios_with_checked_state(self) -> None:
        assert 'role="radio"' in PANEL_JS
        assert "aria-checked=" in PANEL_JS

    def test_tab_shows_the_stable_index(self) -> None:
        assert 'class="idx' in PANEL_JS

    def test_status_vocabulary(self) -> None:
        for word in ("Ready", "Active", "Disabled", "API key", "Needs login", "Unavailable"):
            assert word in PANEL_JS, f"selector lost the {word!r} status word"
        assert "Held out" not in PANEL_JS, "final design renames Held out to Disabled"

    def test_tab_accessible_name_carries_full_identity(self) -> None:
        # the ellipsized alias must not be the accessible name; slot + alias + status
        assert re.search(r'aria-label="\$\{[^}]*slot[^}]*\}', PANEL_JS) or re.search(
            r"aria-label=\`\$\{", PANEL_JS
        ), "tabs need an aria-label built from slot/alias/status"


class TestAliasActions:
    """The alias editor's bridge contract: specs, registration, sends, and
    the no-network push requirement from the handoff."""

    def test_alias_actions_have_payload_specs(self) -> None:
        assert '"setAlias": {"required": {"slot": str, "alias": str}}' in APP_PY
        assert '"unsetAlias": {"required": {"slot": str}}' in APP_PY

    def test_alias_actions_are_registered_and_reachable(self) -> None:
        handlers = registered_handlers()
        assert {"setAlias", "unsetAlias"} <= handlers
        assert {"setAlias", "unsetAlias"} <= panel_actions()

    def test_alias_push_avoids_the_usage_api(self) -> None:
        m = re.search(r"def _push_alias_update.*?store_only=True", APP_PY, re.S)
        assert m, "alias updates must rebuild from the store, not the usage API"

    def test_alias_sheet_sends_the_captured_slot(self) -> None:
        # the slot is captured when the dialog opens; every send must use it
        assert re.search(r"aliasCtx\.slot", SHEETS_JS), (
            "alias sends must go through the slot captured at open"
        )


class TestSettingsSegments:
    """Segmented settings: extended getPrefs reply, segments in markup, no
    stale dropdown path left behind."""

    def test_getprefs_reports_all_three_preferences(self) -> None:
        assert '"theme": self.settings.theme' in APP_PY
        assert '"refreshInterval": self.settings.refresh_interval' in APP_PY
        assert '"titlePct": self.settings.title_pct' in APP_PY

    def test_settings_sheet_uses_segments_not_a_dropdown(self) -> None:
        assert '<select id="fld-theme"' not in INDEX_HTML
        for group in ("theme", "refreshInterval", "titlePct"):
            assert f'data-seg="{group}"' in INDEX_HTML, f"missing {group} segment"

    def test_every_segment_button_is_a_radio(self) -> None:
        assert INDEX_HTML.count('class="seg-btn" role="radio"') >= 10


class TestBranding:
    """Claude Code Swap header + Interlock mark from the final handoff."""

    def test_header_reads_claude_code_swap(self) -> None:
        assert ">Claude Code Swap<" in PANEL_JS

    def test_interlock_glyph_ships_in_icons(self) -> None:
        # exact Pen-export geometry: the S-ribbon start and an arrowhead
        assert "M17.5 4.5 L8.5 4.5" in ICONS_JS
        assert "M17.5 2.6" in ICONS_JS
        # the mark is a fill glyph, not the stroke language of utility icons
        assert re.search(r"swap:\s*\{", ICONS_JS) or "fillGlyph" in ICONS_JS or \
            re.search(r'"swap":\s*\{', ICONS_JS), "swap must be a fill-glyph entry"

    def test_old_swap_arrows_are_gone(self) -> None:
        assert "M4 7h13l-3.5-3.5" not in ICONS_JS

    def test_page_title_is_branded(self) -> None:
        assert "<title>Claude Code Swap</title>" in INDEX_HTML


class TestGlobalContract:
    def test_bridge_emits_the_globals_panel_defines(self) -> None:
        # bridge.py builds cswap.reply(...) / cswap.push(...) strings
        assert 'cswap.reply(' in BRIDGE_PY
        assert 'cswap.push(' in BRIDGE_PY
        # panel.js defines exactly those entry points on window.cswap
        assert "reply:" in PANEL_JS or "reply:" in PANEL_JS.replace(" ", "")
        assert re.search(r"reply:\s*\(", PANEL_JS), "panel.js lost its cswap.reply hook"
        assert re.search(r"push:\s*\(", PANEL_JS), "panel.js lost its cswap.push hook"

    def test_panel_escapes_string_interpolations(self) -> None:
        # the numeric exceptions are explicitly wrapped; everything else in
        # template literals must go through esc( — spot-pin the sinks that
        # render view-model strings
        for sink in ("${esc(", "esc(String("):
            assert sink in PANEL_JS
