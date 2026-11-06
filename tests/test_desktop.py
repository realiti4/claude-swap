"""Tests for the desktop window: display helpers and the page bridge (DesktopApi)."""

from __future__ import annotations

import threading
from types import SimpleNamespace

from claude_swap.desktop import DesktopApi, card_dict, card_lines, colorref, event_line, strip_ansi
from claude_swap.json_output import USAGE_API_KEY
from claude_swap.models import AccountSnapshot
from claude_swap.switcher import SENTINEL_NOTES
from claude_swap.usage_store import UsageEntry


def _acc(email="a@example.com", usage=None, **kw) -> AccountSnapshot:
    return AccountSnapshot(
        number="1",
        email=email,
        org_name="",
        org_uuid="",
        is_active=kw.pop("is_active", True),
        kind="oauth",
        switchable=True,
        usage=usage or UsageEntry(),
        **kw,
    )


def test_card_active_with_usage():
    usage = UsageEntry(
        last_good={"five_hour": {"pct": 62.0}, "seven_day": {"pct": 21.0}}, age_s=0
    )
    view = card_lines(_acc(usage=usage), now=0.0)
    assert view.rows[0].label == "5h"
    assert view.rows[0].pct == 62.0
    assert view.rows[1].pct == 21.0
    assert view.active is True
    assert view.message is None


def test_card_sentinel_replaces_bars():
    view = card_lines(_acc(usage=UsageEntry(sentinel=USAGE_API_KEY)), now=0.0)
    assert view.rows == ()
    assert view.message == SENTINEL_NOTES[USAGE_API_KEY]


def test_card_no_usage():
    assert card_lines(_acc(), now=0.0).message == "no usage yet"


def test_card_disabled_flag():
    assert card_lines(_acc(disabled=True), now=0.0).disabled is True


def test_card_title_truncated():
    view = card_lines(_acc(email="x" * 60 + "@example.com"), now=0.0)
    assert len(view.title) == 32
    assert view.title.endswith("…")


def test_strip_ansi():
    assert strip_ansi("\x1b[32mok\x1b[0m") == "ok"


class _Ev:
    def __init__(self, kind):
        self.kind = kind

    def human(self):
        return "something happened"


def test_event_line_switch_no_alert():
    text, alert = event_line(_Ev("switch"))
    assert alert is False
    assert text.endswith("something happened")


def test_event_line_exhausted_alerts():
    text, alert = event_line(_Ev("all-exhausted"))
    assert alert is True
    assert text.endswith("something happened")


def test_main_replaces_none_streams(monkeypatch):
    import sys

    from claude_swap import desktop

    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(desktop, "ClaudeAccountSwitcher", lambda *a, **k: object())
    monkeypatch.setattr(desktop, "run", lambda sw: 0)
    assert desktop.main() == 0
    assert sys.stdout is not None and sys.stderr is not None


def test_main_startup_error_shows_dialog(monkeypatch):
    from claude_swap import desktop
    from claude_swap.exceptions import ClaudeSwitchError

    def _boom(*a, **k):
        raise ClaudeSwitchError("boom")

    shown = []
    monkeypatch.setattr(desktop, "ClaudeAccountSwitcher", _boom)
    monkeypatch.setattr(desktop, "_show_error", shown.append)
    assert desktop.main() == 1
    assert len(shown) == 1 and "boom" in shown[0]


def test_colorref_is_bgr():
    assert colorref("#16161a") == 0x1A1616
    assert colorref("#ff0000") == 0x0000FF


def test_main_unexpected_constructor_error_shows_dialog(monkeypatch):
    from claude_swap import desktop

    def _boom(*a, **k):
        raise OSError("disk")

    shown = []
    monkeypatch.setattr(desktop, "ClaudeAccountSwitcher", _boom)
    monkeypatch.setattr(desktop, "_show_error", shown.append)
    assert desktop.main() == 1
    assert len(shown) == 1 and "OSError" in shown[0] and "disk" in shown[0]


def test_main_run_error_shows_dialog(monkeypatch):
    from claude_swap import desktop

    def _boom(sw):
        raise RuntimeError("tcl")

    shown = []
    monkeypatch.setattr(desktop, "ClaudeAccountSwitcher", lambda *a, **k: object())
    monkeypatch.setattr(desktop, "run", _boom)
    monkeypatch.setattr(desktop, "_show_error", shown.append)
    assert desktop.main() == 1
    assert len(shown) == 1 and "RuntimeError" in shown[0] and "tcl" in shown[0]


class _FakeSource:
    def __init__(self, accounts):
        self.accounts = accounts

    def take(self, **_kw):
        return SimpleNamespace(accounts=self.accounts, taken_at=0.0)


class _FakeSwitcher:
    def __init__(self):
        self.calls = []

    def switch_to(self, num):
        self.calls.append(("switch_to", num))
        print("[32mSwitched to #2[0m")


def _api(accounts=(), switcher=None, **kw):
    return DesktopApi(switcher or _FakeSwitcher(), source=_FakeSource(list(accounts)), **kw)


def test_card_dict_is_json_ready():
    import json

    usage = UsageEntry(last_good={"five_hour": {"pct": 62.0}, "seven_day": {"pct": 21.0}}, age_s=0)
    d = card_dict(_acc(usage=usage), now=0.0)
    assert d["num"] == "1"
    assert d["rows"][0] == {"label": "5h", "pct": 62.0, "reset": None, "age": None}
    json.dumps(d)  # must serialize for the page


def test_snapshot_returns_cards():
    out = _api([_acc()]).snapshot()
    assert [c["num"] for c in out["accounts"]] == ["1"]


def test_snapshot_failure_is_reported_not_raised():
    class Broken:
        def take(self, **_kw):
            raise OSError("offline")

    out = DesktopApi(_FakeSwitcher(), source=Broken()).snapshot()
    assert out == {"error": "OSError: offline"}


def test_action_output_is_captured_and_plain():
    sw = _FakeSwitcher()
    out = _api(switcher=sw).switch(2)
    assert sw.calls == [("switch_to", "2")]
    assert out == {"ok": True, "output": "Switched to #2"}


def test_second_action_is_refused_while_one_runs():
    api = _api()
    api._action.acquire()  # an action is in flight
    try:
        out = api.switch(2)
    finally:
        api._action.release()
    assert out["ok"] is False and "still running" in out["output"]


def test_add_token_requires_both_fields():
    out = _api().add_token("  ", "tok")
    assert out["ok"] is False


def test_deferred_close_runs_after_the_action():
    api = _api()
    closed = []
    api.closing, api.on_idle = True, lambda: closed.append(True)
    api.switch(1)
    assert closed == [True]


def test_poll_reports_events_and_requests_refresh_on_switch():
    api = _api()
    api._events.put(_Ev("switch"))
    api._events.put(_Ev("all-exhausted"))
    p = api.poll()
    assert [line["alert"] for line in p["lines"]] == [False, True]
    assert p["refresh"] is True
    assert api.poll()["refresh"] is False


def test_auto_start_failure_leaves_it_off():
    def broken(*_a):
        raise ValueError("bad settings")

    out = _api(engine_factory=broken).auto(True)
    assert out["on"] is False and "bad settings" in out["output"]


def test_auto_runs_engine_until_stopped():
    stopped = threading.Event()

    class Engine:
        def run_loop(self):
            stopped.wait(5)

        def stop(self):
            stopped.set()

    api = _api(engine_factory=lambda *_a: Engine())
    assert api.auto(True) == {"on": True}
    assert api.poll()["auto"] is True
    api.shutdown()
    assert stopped.is_set() and api.poll()["auto"] is False


def test_export_cancelled_does_nothing(monkeypatch):
    called = []
    monkeypatch.setattr("claude_swap.transfer.export_accounts", lambda *a, **k: called.append(a))
    api = _api()
    api.pick_file = lambda kind: None
    assert api.export() == {"ok": True, "output": ""}
    assert called == []


def test_export_writes_to_picked_path(monkeypatch):
    called = []
    monkeypatch.setattr(
        "claude_swap.transfer.export_accounts",
        lambda sw, path, **k: (called.append(path), print(f"Exported 2 account(s) to {path}")),
    )
    api = _api()
    api.pick_file = lambda kind: "C:/tmp/a.cswap" if kind == "save" else None
    out = api.export()
    assert called == ["C:/tmp/a.cswap"]
    assert out == {"ok": True, "output": "Exported 2 account(s) to C:/tmp/a.cswap"}


def test_import_passes_replace_as_force(monkeypatch):
    called = []
    monkeypatch.setattr(
        "claude_swap.transfer.import_accounts",
        lambda sw, path, force=False: called.append((path, force)),
    )
    api = _api()
    api.pick_file = lambda kind: "C:/tmp/a.cswap" if kind == "open" else None
    api.import_(True)
    api.import_(False)
    assert called == [("C:/tmp/a.cswap", True), ("C:/tmp/a.cswap", False)]


def test_import_error_is_reported(monkeypatch):
    from claude_swap.exceptions import TransferError

    def boom(*a, **k):
        raise TransferError("export file is not valid JSON")

    monkeypatch.setattr("claude_swap.transfer.import_accounts", boom)
    api = _api()
    api.pick_file = lambda kind: "C:/tmp/bad.cswap"
    out = api.import_()
    assert out["ok"] is False and "not valid JSON" in out["output"]


def test_engine_events_reach_page_and_notifier():
    seen = []
    api = _api()
    api.notifier = SimpleNamespace(handle=seen.append)
    ev = _Ev("all-exhausted")
    api._on_event(ev)
    assert seen == [ev]
    assert api.poll()["lines"][0]["alert"] is True
