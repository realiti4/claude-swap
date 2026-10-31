"""The 5h rule applies only while the API reports a RUNNING 5h window.

Running means ``five_hour`` carries a utilization above 0; its ``resets_at``
may be withheld. Everything else (idle ``{0, null}``, a rolling ``{0, +1h}``, a
null or an absent key) is "no running 5h window" and must neither bind a
decision, be drawn as a bar, nor be synthesized as full by a 429 wall.

Shapes ``live``, ``idle`` and ``removed`` are real captures. ``live`` and
``idle`` (2026-10-30) are trimmed to the keys the parser reads: a running-5h
account (10% / 29%) and a weekly-walled one (7d 100%) with an idle 5h.
``removed`` (slot 8, 2026-10-31) is the raw body of an account whose 5h limit
was removed: ``five_hour`` null AND no ``session`` entry in ``limits``, 7d 97%.
So ``null`` is MEASURED; the ``null`` shape stays too, as the same value on the
walled body. ``withheld`` is the running capture with its reset left out.
``absent`` and ``rolling`` are HYPOTHESES: no capture has shown either, so they
are built on the walled capture.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_swap import menubar, oauth, poll_policy, usage_store
from claude_swap.autoswitch import PollEvent, _window_pcts, consume_first_rank_key
from claude_swap.lines import binding_window
from claude_swap.models import AccountSnapshot
from claude_swap.switcher import ClaudeAccountSwitcher, _format_usage_lines
from claude_swap.tui import data as tui_data, widgets
from claude_swap.usage_store import WALL_FALLBACK_S, FetchRecord, UsageEntry, UsageStore

FIXTURES = Path(__file__).parent / "fixtures"
CAPTURED_AT = datetime(2026, 10, 31, 1, 8, 56, tzinfo=timezone.utc)
SHIFT = timedelta(0)
IDENT = {"1": ("a@x.com", "")}
NOT_LIVE = ["idle", "removed", "absent", "null", "rolling"]
# shape, its 429 wall span (the capture's 5h reset was 4h51m04s ahead)
LIVE = [("live", 4 * 3600 + 51 * 60 + 4), ("withheld", WALL_FALLBACK_S)]


def _iso(seconds: float) -> str:
    when = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return when.isoformat(timespec="seconds").replace("+00:00", "Z")


@pytest.fixture(autouse=True)
def _moved_on():
    """Fix, for one test, how far on from the capture the resets are moved."""
    global SHIFT
    SHIFT = datetime.now(timezone.utc) - CAPTURED_AT


def _capture(name: str) -> dict:
    """A captured body with every reset moved on by SHIFT, so its windows are
    as far ahead of now as they were then."""

    def moved(window: dict) -> dict:
        if window.get("resets_at"):
            window["resets_at"] = (datetime.fromisoformat(window["resets_at"]) + SHIFT).isoformat()
        return window

    return json.loads((FIXTURES / name).read_text(encoding="utf-8"), object_hook=moved)


def _api(shape: str) -> dict:
    """A raw usage-endpoint body with the named ``five_hour`` shape."""
    if shape == "removed":
        return _capture("usage_removed_5h.json")
    if shape in ("live", "withheld"):
        body = _capture("usage_running_5h.json")
        if shape == "withheld":
            body["five_hour"]["resets_at"] = None
        return body
    body = _capture("usage_weekly_walled_idle_5h.json")
    if shape == "absent":
        del body["five_hour"]
    elif shape == "null":
        body["five_hour"] = None
    elif shape == "rolling":
        body["five_hour"] = {"utilization": 0.0, "resets_at": _iso(3600)}
    return body


def _usage(shape: str) -> dict:
    usage = oauth.build_usage_result(_api(shape))
    assert usage is not None
    return usage


def _snapshot(usage: dict, plan: str | None = None) -> AccountSnapshot:
    entry = UsageEntry(last_good=usage, fetched_at=time.time(), age_s=1.0, plan=plan)
    return AccountSnapshot(
        number="2", email="b@x.com", org_name="", org_uuid="", is_active=False,
        kind="oauth", switchable=True, usage=entry,
    )


def _describe(value: dict) -> str:
    event = PollEvent(
        active=None, headroom={}, threshold=90.0,
        windows={"1": _window_pcts(value, ("all",))},
    )
    return event._describe("1")


@pytest.fixture
def store(tmp_path):
    return UsageStore(tmp_path / "cache")


# -- the predicate and the plan label --------------------------------------


@pytest.mark.parametrize("shape", [s for s, _ in LIVE] + NOT_LIVE)
def test_only_a_running_window_is_live(shape):
    assert oauth.five_hour_live(_usage(shape)) is (shape in dict(LIVE))


@pytest.mark.parametrize("usage", [None, "token expired", {}, {"five_hour": "x"}])
def test_a_reading_without_a_5h_window_is_not_live(usage):
    assert oauth.five_hour_live(usage) is False


@pytest.mark.parametrize(
    "subscription,tier,label",
    [
        ("max", "default_claude_max_20x", "Ultra"),
        ("max", "default_claude_ultra", "Ultra"),
        ("max", "default_claude_max_5x", "Max 5x"),
        ("max", "default_claude_unheard_of", "Max"),
        ("pro", None, "Pro"),
        ("enterprise", "default_claude_zero", "Enterprise"),
        (None, "default_claude_new_tier", "default_claude_new_tier"),
        ("weird", None, "weird"),
        (None, None, ""),
        (5, {}, ""),
    ],
)
def test_plan_label(subscription, tier, label):
    assert oauth.plan_label(subscription, tier) == label


# -- ranking and rendering ---------------------------------------------------


@pytest.mark.parametrize("shape", NOT_LIVE)
def test_non_live_shapes_rank_and_render_like_an_absent_window(shape):
    # The reference is the same body with its 5h key dropped: "removed" is a
    # different account from the walled one, so another shape's 7d is no match.
    ref_body = _api(shape)
    ref_body.pop("five_hour", None)
    ref, got, now = oauth.build_usage_result(ref_body), _usage(shape), time.time()
    # The two rank lines guard no-regression: they hold on the old code too.
    assert oauth.account_headroom(got) == oauth.account_headroom(ref)
    assert consume_first_rank_key(got, 90.0, now) == consume_first_rank_key(ref, 90.0, now)
    assert _describe(got) == _describe(ref)  # the poll event's windows
    assert menubar.format_usage_log("a@x.com", got) == menubar.format_usage_log("a@x.com", ref)
    assert _format_usage_lines(got) == _format_usage_lines(ref)
    assert widgets.usage_rows(got, now) == widgets.usage_rows(ref, now)
    assert menubar.usage_summary(got, now) == menubar.usage_summary(ref, now)
    both = menubar.MenuBarSettings(title_pct="both")
    assert menubar.format_title("a@x.com", got, both, now) == menubar.format_title(
        "a@x.com", ref, both, now
    )
    assert (
        widgets.mini_account_text(_snapshot(got), now).plain
        == widgets.mini_account_text(_snapshot(ref), now).plain
    )
    assert "5h" not in widgets.account_card_text(_snapshot(got), 80, now=now).plain
    assert tui_data.window_pct(got, "five_hour") is None  # no Next-best chip


@pytest.mark.parametrize("shape", [s for s, _ in LIVE])
def test_a_running_window_still_renders(shape):
    got, now = _usage(shape), time.time()
    assert [label for label, *_ in widgets.usage_rows(got, now)] == ["5h", "7d", "Fable"]
    assert _format_usage_lines(got)[0].startswith("5h:")
    assert menubar.usage_summary(got, now).startswith("5h 10%")
    assert ":10%" in widgets.mini_account_text(_snapshot(got), now).plain
    assert tui_data.window_pct(got, "five_hour") == 10.0
    assert "5h 10% · 7d 29%" in menubar.format_title(
        "a@x.com", got, menubar.MenuBarSettings(title_pct="both"), now
    )


def test_the_weekly_walled_capture_renders_no_5h_and_its_7d_at_100():
    got, now = _usage("idle"), time.time()
    assert oauth.five_hour_live(got) is False
    assert [label for label, *_ in widgets.usage_rows(got, now)] == ["7d", "Fable"]


def test_the_both_title_labels_its_numbers():
    both = menubar.MenuBarSettings(title_pct="both", show_account_name=False)
    # A 7d figure must never sit where the 5h one used to.
    assert menubar.format_title("a@x.com", _usage("idle"), both) == f"{menubar.ICON} 7d 100%"


def test_a_missing_5h_line_is_not_a_preference():
    # Guards no-regression (holds on the old code too). Same 7d: one account
    # with a live low 5h, one without. Same rank key.
    seven = {"pct": 67.0, "resets_at": _iso(5 * 86400)}
    live_low = {"five_hour": {"pct": 10.0, "resets_at": _iso(3600)}, "seven_day": seven}
    now = time.time()
    assert consume_first_rank_key(live_low, 90.0, now) == consume_first_rank_key(
        {"seven_day": seven}, 90.0, now
    )


@pytest.mark.parametrize("shape", NOT_LIVE)
def test_a_threshold_with_no_weekly_line_still_leaves_a_no_5h_account_at_its_7d(shape):
    # Guards no-regression: holds on the old code too.
    usage = _usage(shape)
    for pct, over in ((97.0, True), (96.0, False)):
        usage["seven_day"]["pct"] = pct
        binding = binding_window(oauth.relevant_windows(usage), 97.0, None)
        assert (binding.label, binding.over, binding.line) == ("7d", over, 97.0)


# -- the 429 wall --------------------------------------------------------------


@pytest.mark.parametrize("shape", NOT_LIVE)
def test_a_429_on_a_no_5h_account_marks_its_binding_window(store, shape):
    usage = _usage(shape)
    usage["seven_day"]["pct"] = 80.0
    store.record({"1": FetchRecord(usage=usage)}, IDENT)
    store.mark_at_limit("1", IDENT)

    entry = store.entries(IDENT)["1"]
    value = entry.decision_value()
    assert "five_hour" not in value
    assert value["seven_day"]["pct"] == 100.0
    # A guess lifts with the mark, not at the real reset days away.
    assert value["seven_day"]["resets_at"] == usage_store._reset_ts_to_resets_at(entry.walled_until)
    assert poll_policy.limiting_reset_ts(value) == pytest.approx(entry.walled_until, abs=1)
    assert _describe(value) == "7d 100% · Fable 0%"
    assert oauth.binding_window_label(value, ("all",)) == "7d"
    # Nothing 5h-shaped sets the span, not even a phantom reset an hour out.
    assert store.entries(IDENT)["1"].walled_until == pytest.approx(
        time.time() + WALL_FALLBACK_S, abs=5
    )


def test_a_legacy_mark_on_a_no_5h_account_does_not_read_5h_full(store):
    usage = _usage("idle")
    store.record({"1": FetchRecord(usage=usage)}, IDENT)
    store.mark_at_limit("1", IDENT)
    rows = json.loads(store.path.read_text(encoding="utf-8"))
    del rows["accounts"]["1"]["walledWindow"]  # a mark written before the window was kept
    store.path.write_text(json.dumps(rows), encoding="utf-8")

    assert "five_hour" not in store.entries(IDENT)["1"].decision_value()


@pytest.mark.parametrize("dropped", [False, True])
def test_a_429_on_a_scoped_window_reads_7d_full_for_every_reader(store, dropped):
    usage = _usage("idle")
    usage["seven_day"]["pct"] = 40.0
    usage["scoped"] = [{"name": "Fable", "pct": 90.0, "resets_at": _iso(2 * 86400)}]
    store.record({"1": FetchRecord(usage=usage)}, IDENT)
    store.mark_at_limit("1", IDENT, ("all",))
    for _ in range(2 if dropped else 0):  # a vanished window is held for one fetch
        store.record({"1": FetchRecord(usage={**usage, "scoped": []})}, IDENT)

    entry = store.entries(IDENT)["1"]
    value = entry.decision_value()
    deadline = usage_store._reset_ts_to_resets_at(entry.walled_until)
    stored = {k: v for k, v in usage["seven_day"].items() if k not in ("clock", "countdown")}
    assert value["seven_day"] == {**stored, "pct": 100.0, "resets_at": deadline}
    assert [(w["pct"], w["resets_at"]) for w in value["scoped"]] == (
        [] if dropped else [(100.0, deadline)]
    )
    # No stale reset string sits beside the mark's deadline.
    assert not any("clock" in w or "countdown" in w for w in value["scoped"])
    # A reader whose models leave the name out still reads the slot full.
    assert oauth.account_headroom(value, ()) == 0.0
    assert oauth.account_headroom(value, ("all",)) == 0.0
    # Both read 100, but the escape ranks peers on the window that walled us.
    assert oauth.binding_window_label(value, ("all",)) == ("7d" if dropped else "Fable")
    assert poll_policy.limiting_reset_ts(value, ("all",)) == pytest.approx(entry.walled_until, abs=1)


@pytest.mark.parametrize("shape,span", LIVE)
def test_a_429_on_a_running_5h_account_still_reads_5h_full(store, shape, span):
    store.record({"1": FetchRecord(usage=_usage(shape))}, IDENT)
    store.mark_at_limit("1", IDENT)

    entry = store.entries(IDENT)["1"]
    assert entry.walled_until == pytest.approx(time.time() + span, abs=5)
    value = entry.decision_value()
    assert value["five_hour"] == {
        "pct": 100.0, "resets_at": usage_store._reset_ts_to_resets_at(entry.walled_until),
    }
    assert value["seven_day"]["pct"] == 29.0


def test_a_429_marks_the_window_already_at_100_even_beside_a_live_5h(store):
    usage = _usage("live")
    usage["seven_day"]["pct"] = 100.0
    store.record({"1": FetchRecord(usage=usage)}, IDENT)
    store.mark_at_limit("1", IDENT)

    value = store.entries(IDENT)["1"].decision_value()
    assert (value["five_hour"]["pct"], value["seven_day"]["pct"]) == (10.0, 100.0)
    assert value["seven_day"]["resets_at"] == usage["seven_day"]["resets_at"]  # a real 100 keeps its reset
    assert oauth.binding_window_label(value, ("all",)) == "7d"


# -- header readings -------------------------------------------------------------


def test_a_reply_without_a_5h_header_carries_no_5h_window(store):
    scoped = [{"name": "Fable", "pct": 33.0}]
    store.record({"1": FetchRecord(usage={**_usage("live"), "scoped": scoped})}, IDENT)

    assert store.record_header_reading(
        "1", IDENT, {usage_store.USAGE_HEADER_7D_PCT: "0.55"}
    ) is True
    last_good = store.entries(IDENT)["1"].last_good
    assert last_good["seven_day"]["pct"] == pytest.approx(55.0)
    assert "five_hour" not in last_good  # absent means absent: not the last poll's
    assert last_good["scoped"] == scoped


# -- the plan reaches the screens ----------------------------------------------


def test_a_record_stamps_the_plan_beside_lastgood(store):
    store.record({"1": FetchRecord(usage=_usage("live"), plan="Ultra")}, IDENT)
    row = json.loads(store.path.read_text(encoding="utf-8"))["accounts"]["1"]
    assert row["plan"] == "Ultra"
    assert "plan" not in row["lastGood"]
    assert store.entries(IDENT)["1"].plan == "Ultra"
    # A record that carries no plan leaves the stamped one alone.
    store.record({"1": FetchRecord(usage=_usage("live"))}, IDENT)
    assert store.entries(IDENT)["1"].plan == "Ultra"


PLAN_BLOBS = [
    ({"subscriptionType": "max", "rateLimitTier": "default_claude_max_20x"}, "Ultra"),
    ({"rateLimitTier": "default_claude_new_tier"}, "default_claude_new_tier"),
    ({}, None),
]


def _list_with(blob: dict, sample_sequence_data: dict) -> None:
    """``list_accounts`` on a switcher whose slots hold ``blob``'s credential
    and whose usage endpoint answers a running 5h window."""
    sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
    active, backup = (
        json.dumps({"claudeAiOauth": {"accessToken": token, **blob}})
        for token in ("sk-active", "sk-backup")
    )
    response = MagicMock()
    response.read.return_value = json.dumps(_api("live")).encode()
    response.__enter__ = lambda s: s
    response.__exit__ = MagicMock(return_value=False)
    switcher = ClaudeAccountSwitcher()
    switcher._setup_directories()
    switcher._write_json(switcher.sequence_file, sample_sequence_data)
    with patch.object(switcher, "_read_credentials", return_value=active), \
         patch.object(switcher, "_read_account_credentials", return_value=backup), \
         patch("claude_swap.oauth.urllib.request.urlopen", return_value=response):
        switcher.list_accounts()


@pytest.mark.parametrize("blob,plan", PLAN_BLOBS)
def test_list_shows_the_plan_after_the_tag(
    temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict, capsys, blob, plan
):
    _list_with(blob, sample_sequence_data)
    header = next(
        line for line in capsys.readouterr().out.splitlines() if "account2@example.com" in line
    )
    assert header.rstrip().endswith(f"[personal] {plan}" if plan else "[personal]")


def test_the_tui_row_and_card_show_the_plan():
    plan, now = "Ultra", time.time()
    snap, bare = _snapshot(_usage("live"), plan), _snapshot(_usage("live"))
    assert f"[personal]  {plan}" in widgets.mini_account_text(snap, now).plain
    assert f"[personal]  {plan}" in widgets.account_card_text(snap, 80, now=now).plain
    assert plan not in widgets.mini_account_text(bare, now).plain
    assert plan not in widgets.account_card_text(bare, 80, now=now).plain


def test_the_menubar_row_shows_the_plan():
    row = menubar.format_account_label("2", "b@x.com", _usage("live"), plan="Ultra")
    assert row.startswith("2  b@x.com  Ultra  5h 10%")
    assert "Ultra" not in menubar.format_account_label("2", "b@x.com", _usage("live"))
