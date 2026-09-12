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
PANEL_JS = (MENUBAR / "web" / "panel.js").read_text(encoding="utf-8")
APP_PY = (MENUBAR / "app.py").read_text(encoding="utf-8")
BRIDGE_PY = (MENUBAR / "bridge.py").read_text(encoding="utf-8")


def panel_actions() -> set[str]:
    """Every action literal the panel can send to Python."""
    sent = set(re.findall(r'bridge\.send\(\s*"([A-Za-z]+)"', PANEL_JS))
    # doAction(btn?, "action", payload, ...) — the fixture-mode paths
    sent |= set(re.findall(r'doAction\((?:[^,]+,\s*)?"([A-Za-z]+)"', PANEL_JS))
    return sent


def registered_handlers() -> set[str]:
    block = APP_PY[APP_PY.index("return {") : APP_PY.index("def _add_from_token")]
    return set(re.findall(r'"([A-Za-z]+)":\s', block))


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
        # actions the panel always calls with a meaningful payload
        payload_actions = {"switch", "disable", "enable", "remove", "setAutoSwitch"}
        missing = payload_actions - payload_spec_keys()
        assert not missing, f"payload actions without validation specs: {sorted(missing)}"


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
