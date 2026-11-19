"""The shared paths work under every install shape the project supports.

cswap alone, cswap + menubar, cswap + pin, cswap + menubar + pin. The menubar
and pin extras are optional, so a switch, a usage fetch, a refresh, an
auto-switch tick and a settings write must neither need nor trigger either
one: an unguarded call-time ``import cswap_pin`` / ``import rumps`` on any of
them breaks the install that lacks the extra (a guarded or module-level import
is covered by TestImportSafeWithoutTheExtra and
test_only_pin_py_names_cswap_pin_at_all in tests/test_pin.py), and a path that
wires the pin breaks the install that has it but never asked for it. The
menubar stubs are asserted never called, not never imported.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

from claude_swap import oauth, pin
from claude_swap.settings import load_ui_settings, set_setting, unset_setting
from tests import test_autoswitch as ta


class _Block:
    """``import cswap_pin`` raises, as on an install without the extra."""

    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] == "cswap_pin":
            raise ImportError(f"No module named {name!r}")
        return None


_USAGE = {
    "five_hour": {"utilization": 12.0, "resets_at": None},
    "seven_day": {"utilization": 34.0, "resets_at": None},
}


def _urlopen_returning(payload: dict):
    resp = mock.MagicMock()
    resp.read.return_value = json.dumps(payload).encode()
    resp.__enter__ = lambda s: s
    resp.__exit__ = mock.MagicMock(return_value=False)
    return mock.patch("claude_swap.oauth.urllib.request.urlopen", return_value=resp)


@pytest.mark.parametrize(
    "with_menubar, with_pin",
    [(False, False), (True, False), (False, True), (True, True)],
    ids=["cswap", "cswap+menubar", "cswap+pin", "cswap+menubar+pin"],
)
def test_shared_paths_work_in_every_install_shape(
    temp_home: Path, monkeypatch, with_menubar: bool, with_pin: bool
):
    # the post-switch usage fan-out sleeps 0.25 s per extra account
    monkeypatch.setattr("claude_swap.switcher._FETCH_STAGGER_S", 0)
    stubs = {}
    for mod in ("rumps", "AppKit"):
        stubs[mod] = mock.MagicMock() if with_menubar else None
        monkeypatch.setitem(sys.modules, mod, stubs[mod])

    wire_calls: list = []
    if with_pin:
        import cswap_pin.proxy as proxy

        monkeypatch.setattr(
            proxy, "wire_global_config", lambda *a, **k: wire_calls.append((a, k))
        )
    else:
        monkeypatch.setattr(sys, "meta_path", [_Block(), *sys.meta_path])
        for name in [m for m in sys.modules if m.startswith("cswap_pin")]:
            monkeypatch.delitem(sys.modules, name)

    h = ta.EngineHarness(temp_home)
    for num, email in ((1, "a@example.com"), (2, "b@example.com")):
        h.seed(num, email)
    h.make_live("a@example.com", 1)
    # live == slot 1's backup, so the switch backs it up with no ownership probe
    (temp_home / ".claude" / ".credentials.json").write_text(
        h.switcher._read_account_credentials("1", "a@example.com")
    )

    # usage fetch
    with _urlopen_returning(_USAGE):
        assert oauth.fetch_usage("sk-test-token")["five_hour"]["pct"] == 12.0

    # settings get/set
    assert set_setting(h.switcher.backup_dir, "ui.theme", "light") == "light"
    assert load_ui_settings(h.switcher.backup_dir).theme == "light"
    assert unset_setting(h.switcher.backup_dir, "ui.theme") is True

    # auto-switch tick
    outcome = h.tick_with_usage({"1": ta._usage(50), "2": ta._usage(10)})
    assert outcome is ta.TickOutcome.NO_ACTION

    # switch
    with mock.patch("claude_swap.oauth.request_usage_data", return_value=_USAGE):
        h.switcher.switch_to("2")
    assert h.active_number() == 2

    # refresh grant
    rotated = json.dumps({"claudeAiOauth": {
        "accessToken": "sk-new", "refreshToken": "rt-new", "expiresAt": 9999999999000}})
    old = h.switcher._read_account_credentials("1", "a@example.com")
    with mock.patch("claude_swap.oauth.try_refresh_oauth_credentials",
                    return_value=oauth.RefreshOutcome(rotated, None)):
        got = h.switcher.consume_backup_grant("1", "a@example.com", old)
    assert got.credentials == rotated

    # the extras the install lacks were never needed, the ones it has never called
    config = json.loads((temp_home / ".claude.json").read_text())
    assert "_cswapPinWiredKeys" not in config
    assert "CSWAP_PIN_PORT" not in config.get("env", {})
    for mod, stub in stubs.items():
        assert sys.modules[mod] is stub
        assert stub is None or stub.mock_calls == []
    if with_pin:
        # _impl() refuses on win32 before it looks for the package
        assert (pin._live_impl() is None) is (sys.platform == "win32")
        assert wire_calls == []
    else:
        assert pin._live_impl() is None
        assert not [m for m in sys.modules if m.startswith("cswap_pin")]
