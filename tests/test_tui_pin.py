"""The pin's TUI surface: the Cloud menu, its badge, and the dispatch.

SEPARATE FROM test_tui.py ON PURPOSE. Both this branch and the autoswitch work
grow that file at the tail, and appending a class to the same end of the same
file conflicted twice in two rounds — content that never overlapped, only
position. A file per feature drops that collision surface to zero, and neither
branch has to know what the other appended.

The shared fixtures stay where they are; this imports them.
"""

from __future__ import annotations

import contextlib
import io
import types

from functools import partial

import pytest

from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.json_output import USAGE_KEYCHAIN_UNAVAILABLE, USAGE_NO_CREDENTIALS
from tests.test_tui import FakeSwitcher, make_account, make_app, menu_select, settle


@pytest.mark.asyncio
class TestThePinTuiSurface:
    async def test_poll_does_not_move_the_root_menu_cursor(self, tmp_path):
        """A background poll must not steal the cursor from the user.

        The root menu is rebuilt on every snapshot so the pin row can appear
        when the extra is installed mid-session. `_render_menu` ends with
        `menu.index = 0`, which is right for opening or popping a menu and
        wrong for refreshing the one the user is already reading: the cursor
        jumped home every POLL_INTERVAL_S (3s), so anyone slower than one poll
        could not finish choosing.
        """
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            from textual.widgets import ListView

            menu = app.screen.query_one("#menu", ListView)
            await pilot.press("down", "down")
            await pilot.pause()
            assert menu.index == 2

            await app.screen.refresh_root_menu()
            await pilot.pause()
            assert menu.index == 2, "a poll moved the cursor"

    async def test_root_menu_rebuild_keeps_the_cursor_on_its_action(self, tmp_path):
        """When the rows DO change, follow the action, not the row number.

        Installing the extra inserts `pin-menu` above `remove-menu`, so a
        remembered integer lands on a different action than the one the user
        was pointing at. The identity to preserve is the action id.
        """
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            from textual.widgets import ListView

            from claude_swap.tui.widgets import MenuItem

            menu = app.screen.query_one("#menu", ListView)
            items = list(menu.query(MenuItem))
            target = next(
                i for i, it in enumerate(items) if it.action_id == "remove-menu"
            )
            menu.index = target
            await pilot.pause()

            # The extra becomes available: a row is inserted ABOVE the cursor.
            from claude_swap import pin

            monkey = pin.is_available
            pin.is_available = lambda: True
            try:
                await app.screen.refresh_root_menu()
                await pilot.pause()
                items = list(menu.query(MenuItem))
                assert "pin-menu" in [it.action_id for it in items]
                assert items[menu.index].action_id == "remove-menu"
            finally:
                pin.is_available = monkey

    async def test_a_failing_pin_does_not_kill_the_dashboard(self, tmp_path):
        """apply_pin failing must be an error message, not a dead TUI.

        The guard above the dispatch catches `pin._impl()` but stopped one
        line short of the call that does the work, so an exception propagated
        out of on_list_view_selected and took the app down. A real trigger
        needs no injection: a plain FILE where <backup>/pin-proxy should be a
        directory makes ensure_proxy's mkdir raise FileExistsError.
        """
        from claude_swap import pin

        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)

        class _Impl:
            def load_pin(self, _d):
                return None

            def apply_pin(self, *_a):
                raise OSError("disk full")

            def live_remote_control_sessions(self):
                return []

        real_avail, real_impl = pin.is_available, pin._impl
        pin.is_available = lambda: True
        pin._impl = lambda: _Impl()
        try:
            async with app.run_test(size=(100, 32)) as pilot:
                await settle(pilot)
                # `pin:1`, NOT `pin:clear`. This said "apply_pin failing"
                # and dispatched the ONE action that never calls it: the
                # clear branch skips `_impl()` on purpose ("CLEAR does not
                # need the package, and must not"). Measured — replacing the
                # raise with `return None` left the test passing, so the
                # injection was inert and the assertion held for a reason
                # unrelated to its name.
                #
                # `settle`, not a bare pause: this goes through
                # `_start_action`, so the failure runs on a WORKER and one
                # pause does not wait for it. The sibling below caught that
                # half the loud way — Windows CI, `assert 'clear_wiring' in []`.
                await app.screen._dispatch("pin:1")
                await settle(pilot)
                assert app.is_running, "a failing pin killed the dashboard"
        finally:
            pin.is_available, pin._impl = real_avail, real_impl

    async def test_an_informational_row_does_not_kill_the_dashboard(self, tmp_path):
        """A row with no action must do nothing, not raise KeyError.

        _pin_entries returns [(str(exc), ""), _BACK] when the package is
        present but unusable — an informational row, deliberately without an
        action. `actions[action_id]()` raised KeyError out of
        on_list_view_selected and took the app down: the same class as a
        raising apply_pin, one menu level up.
        """
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await app.screen._dispatch("")
            await pilot.pause()
            assert app.is_running, "an actionless row killed the dashboard"

    async def test_an_id_that_should_resolve_and_does_not_still_raises(
        self, tmp_path
    ):
        """AND ONLY THE ACTIONLESS ROW IS INERT — the fix for the case above
        was `elif action_id in actions`, which silences every unknown id.

        Rename a key in `_root_entries`, or typo a new one, and the menu row
        goes permanently dead with no exception, no notify and nothing in any
        log. That is a special case repaired by widening shared
        infrastructure, and the KeyError it removed is the only thing that
        surfaces the typo. An id that is SUPPOSED to resolve must still be
        loud when it does not.
        """
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            with pytest.raises(KeyError):
                await app.screen._dispatch("no-such-action-id")

    async def test_the_tui_clear_also_removes_the_wiring(self, tmp_path):
        """The TUI never got the CLI's both-configs clear.

        The package unwires through its own single-path resolver, so from a
        `cswap run` terminal it cleared the session config and left
        ~/.claude.json naming a dead port. The CLI in the identical state
        clears both.
        """
        from claude_swap import pin

        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        called = []

        class _Impl:
            def apply_pin(self, *_a):
                return None

            def live_remote_control_sessions(self):
                return []

        real = (pin.is_available, pin._impl, pin.clear_wiring, pin.pinned_email)
        pin.is_available = lambda: True
        pin._impl = lambda: _Impl()
        pin.clear_wiring = lambda *a, **k: called.append("clear_wiring") or True
        pin.pinned_email = lambda _sw: None
        try:
            async with app.run_test(size=(100, 32)) as pilot:
                await settle(pilot)
                await app.screen._dispatch("pin:clear")
                # `settle` WAITS FOR THE WORKER; one pause only yields the
                # loop. `pin:clear` is dispatched through `_start_action`
                # (clear_wiring takes a 9s lock and would freeze the
                # dashboard inline), so this asserted a thread's side effect
                # without waiting for the thread. Green on linux, and on
                # Windows CI it failed with `assert 'clear_wiring' in []` —
                # the scheduler, not the product.
                await settle(pilot)
                assert "clear_wiring" in called, (
                    "the TUI cleared the pin but left the wiring behind"
                )
                assert app.is_running
        finally:
            pin.is_available, pin._impl, pin.clear_wiring, pin.pinned_email = real

    async def test_the_tui_does_not_report_a_pin_no_proxy_serves(self, tmp_path):
        """apply_pin returning False must not read as success.

        Asserted on `_run_pin_op`, the seam between pin.py's verdict and the
        TUI's reporting: it prints the message (which `run_action` captures and
        `_action_done` toasts) and RAISES on failure, which is what routes a
        failure to the modal instead of a toast the user can miss. Driving the
        worker instead would assert on Textual's scheduling rather than on the
        contract.
        """
        import contextlib
        import io

        from claude_swap import pin

        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            screen = app.screen
            buf = io.StringIO()
            # ClaudeSwitchError specifically: run_action catches only that and
            # EOFError, so a RuntimeError escaped to the worker handler and
            # became a toast — the modal this raise exists to open never opened.
            from claude_swap.exceptions import ClaudeSwitchError

            with contextlib.redirect_stdout(buf), pytest.raises(
                ClaudeSwitchError, match="no proxy is running"
            ):
                screen._run_pin_op(lambda: (False, "no proxy is running"))
            # Carried by the RAISE, not by a print: run_action renders a
            # ClaudeSwitchError as "Error: {e}", so printing it here too put
            # the same sentence in the modal twice.
            assert buf.getvalue() == "", (
                f"the message was printed as well as raised: {buf.getvalue()!r}"
            )

            # And the success path stays a plain toast, no raise.
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                screen._run_pin_op(lambda: (True, "Pinned the cloud account"))
            assert "Pinned" in buf.getvalue()
            assert app.is_running

    async def test_the_tui_pin_verdict_comes_from_pin_py(self, tmp_path):
        """The TUI must not re-implement the verdict.

        A fix on the CLI whose sibling here keeps the
        old behaviour. `_apply_pin` therefore delegates to `pin.set_pin` and
        only adds the note it alone can produce.
        """
        from claude_swap import pin

        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        seen = []

        class _Impl:
            def live_remote_control_sessions(self):
                return ["sess-1"]

        real = pin.set_pin
        # num is asserted too: passing the slot the TUI already has is what
        # keeps a duplicate email from bypassing the API-key refusal.
        pin.set_pin = lambda sw, email, org, num=None: (
            seen.append((email, num)) or (True, f"Pinned {email}")
        )
        try:
            async with app.run_test(size=(100, 32)) as pilot:
                await settle(pilot)
                acc = app.snapshot.accounts[0]
                ok, msg = app.screen._apply_pin(acc, _Impl())
            assert seen == [(acc.email, acc.number)], (
                "the TUI did not go through pin.set_pin with its resolved slot"
            )
            assert ok and "Pinned" in msg
            assert "Reconnect open Remote Control" in msg, (
                "the RC note only this side can produce was dropped"
            )
        finally:
            pin.set_pin = real

    async def test_an_api_key_row_is_filtered_by_kind_not_sentinel(self, tmp_path):
        """The TUI filter must read the SAME fact the CLI refuses on.

        The CLI refuses on switcher._account_kind(n) == "api_key"; this filtered
        on acc.usage.sentinel == USAGE_API_KEY, and those diverge — the sentinel
        reads USAGE_NO_CREDENTIALS for an unreadable backup blob and
        USAGE_KEYCHAIN_UNAVAILABLE for an API-key slot behind a locked macOS
        keychain. Either one offered the row, and the pin went through.
        """
        from claude_swap import pin
        from claude_swap.json_output import USAGE_NO_CREDENTIALS

        acc = make_account(1, active=True)
        fake = FakeSwitcher([acc], tmp_path)
        app = make_app(fake)
        # _live_impl is what is_available() calls; stubbing both keeps this
        # off the real resolution path, which invalidates importlib caches and
        # cost 15s per run.
        real = (pin.is_available, pin._impl, pin.pinned_email)
        pin.is_available = lambda: True
        pin._impl = lambda: object()
        pin.pinned_email = lambda _sw: None
        try:
            async with app.run_test(size=(100, 32)) as pilot:
                await settle(pilot)
                snap_acc = app.snapshot.accounts[0]
                # kind says api_key while the sentinel says something else —
                # exactly the divergence that let the row through.
                object.__setattr__(snap_acc, "kind", "api_key")
                object.__setattr__(
                    snap_acc.usage, "sentinel", USAGE_NO_CREDENTIALS
                )
                ids = [aid for _label, aid in app.screen._pin_entries()]
                assert f"pin:{snap_acc.number}" not in ids, (
                    "an API-key account was offered for pinning"
                )
        finally:
            pin.is_available, pin._impl, pin.pinned_email = real

    async def test_opening_the_tui_repairs_a_pin_that_stopped_applying(
        self, tmp_path
    ):
        """A user who opens the TUI has already asked for the pin to work.

        The badge tells them the pin is set but minting nothing. Then it asks
        them to go and repair it by hand — with a command whose obvious
        candidate (`--heal`) is the one that declines this state by design. The
        product knows the pin is broken, knows how to fix it, and waits to be
        asked.

        `apply_pin` ends in `return ensure_proxy(switcher) is not None`, and
        `ensure_proxy` reads the record WITH a fingerprint, which is exactly
        the read an `unpinnable` daemon answers "nothing serving" to. So the
        repair is one call the TUI already has every prerequisite for: the
        extra is installed, an account is pinned, and the daemon has published
        that it cannot mint.

        Only that state. A healthy pin must not be recycled on every open —
        that would restart the daemon under live sessions for nothing.
        """
        from claude_swap import pin

        acc = make_account(1, active=True)
        fake = FakeSwitcher([acc], tmp_path)
        app = make_app(fake)
        repaired = []

        real = (pin.is_available, pin._impl, pin.pinned_email,
                pin.pin_is_applying, pin.repin_current)
        pin.is_available = lambda: True
        pin._impl = lambda: object()
        pin.pinned_email = lambda _sw: "cloud@example.com"
        pin.pin_is_applying = lambda _sw: False        # set, not applying
        pin.repin_current = lambda _sw: repaired.append("repin") or True
        try:
            async with app.run_test(size=(100, 32)) as pilot:
                await settle(pilot)
                assert repaired == ["repin"], (
                    "the TUI opened over a pin that mints nothing and did not "
                    "repair it — the user is told to run the command it could "
                    "have run itself"
                )
        finally:
            (pin.is_available, pin._impl, pin.pinned_email,
             pin.pin_is_applying, pin.repin_current) = real

    async def test_a_repair_that_fails_is_not_silent(self, tmp_path):
        """`repin_current` RETURNS False and never raises, so handing it
        straight to `_start_action` lost the failure entirely.

        `run_action` builds `ActionResult(True, ...)` for any fn that does not
        raise and this one prints nothing, so `_action_done` found an empty
        first line and notified nothing. Against a daemon publishing
        `unpinnable` the repair ran on mount, failed, held `app.busy` for its
        duration — the user's next keystroke answering "Another action is
        still running" — and the cloud UNPINNED badge stayed lit with no
        explanation anywhere.
        """
        from claude_swap import pin

        acc = make_account(1, active=True)
        fake = FakeSwitcher([acc], tmp_path)
        app = make_app(fake)

        # ASSERTED ON THE SEAM, like `_run_pin_op`'s sibling above: driving the
        # worker asserts on Textual's scheduling rather than on the contract.
        import contextlib
        import io

        from claude_swap.exceptions import ClaudeSwitchError

        real = pin.repin_current
        pin.repin_current = lambda _sw: False           # the failure, silent
        try:
            async with app.run_test(size=(100, 32)) as pilot:
                await settle(pilot)
                screen = app.screen
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf), pytest.raises(
                    ClaudeSwitchError, match="cloud pin"
                ):
                    screen._run_pin_op(partial(screen._repin, fake))
                assert buf.getvalue() == "", (
                    "the message was printed as well as raised, so the modal "
                    f"carries it twice: {buf.getvalue()!r}")

                # And the success path stays a plain toast, no raise — or a
                # working repair starts opening a modal at every launch.
                pin.repin_current = lambda _sw: True
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    screen._run_pin_op(partial(screen._repin, fake))
                assert buf.getvalue().strip(), (
                    "a successful repair said nothing, which is the silence "
                    "this case exists to remove, in the other direction")
        finally:
            pin.repin_current = real

    async def test_opening_the_tui_does_not_recycle_a_healthy_pin(self, tmp_path):
        """The control, and the one that keeps this from being a menace.

        Recycling on every open restarts the daemon under live sessions for no
        reason. `None` — cannot tell — counts as healthy here for the same
        reason it does at the badge: acting on "I could not look" is how a
        repair becomes the outage.
        """
        from claude_swap import pin

        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        repaired = []

        real = (pin.is_available, pin._impl, pin.pinned_email,
                pin.pin_is_applying, pin.repin_current)
        pin.is_available = lambda: True
        pin._impl = lambda: object()
        pin.pinned_email = lambda _sw: "cloud@example.com"
        pin.repin_current = lambda _sw: repaired.append("repin") or True
        try:
            for verdict in (True, None):
                repaired.clear()
                pin.pin_is_applying = lambda _sw, _v=verdict: _v
                # A fresh app per verdict: a Textual App does not survive a
                # second run_test, and reusing one hangs instead of failing.
                async with make_app(fake).run_test(size=(100, 32)) as pilot:
                    await settle(pilot)
                    assert repaired == [], (
                        f"pin_is_applying={verdict!r} triggered a repair — that "
                        f"restarts the daemon under live sessions for nothing"
                    )
        finally:
            (pin.is_available, pin._impl, pin.pinned_email,
             pin.pin_is_applying, pin.repin_current) = real

    async def test_pin_actions_run_off_the_event_loop(self, tmp_path):
        """clear_wiring takes a 9s lock; inline it froze the dashboard.

        Without the fix: 9.31s frozen, no toast, no keystrokes, while
        Claude Code held .claude.json.lock — routine during a credential
        refresh. Every sibling action in this file goes through
        app._start_action; these two did not, and nothing asserted it, so the
        next edit would reintroduce the freeze silently.
        """
        from claude_swap import pin

        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        started = []
        real = (pin.is_available, pin._impl, pin.pinned_email)
        pin.is_available = lambda: True
        pin._impl = lambda: object()
        pin.pinned_email = lambda _sw: "cloud@example.com"
        try:
            async with app.run_test(size=(100, 32)) as pilot:
                await settle(pilot)
                app._start_action = lambda label, fn, **k: started.append(label)
                await app.screen._dispatch("pin:clear")
                await pilot.pause()
                assert started, "pin:clear ran on the event loop"
                started.clear()
                await app.screen._dispatch("pin:1")
                await pilot.pause()
                assert started, "pin:<n> ran on the event loop"
        finally:
            pin.is_available, pin._impl, pin.pinned_email = real


class TestThePinBadgeDoesNotOverstate:
    def test_an_api_key_account_reads_as_broken_to_pin(self):
        """The badge must not paint ○ cloud with no qualifier for an account
        the pin can never use — the record can be written (a stale submenu row,
        an older cswap) while nothing is ever pinned."""
        import types

        from claude_swap.json_output import USAGE_KEYCHAIN_UNAVAILABLE
        from claude_swap.tui.widgets import pin_is_broken

        # kind says api_key while the sentinel says something innocuous —
        # exactly the divergence that let this through.
        acc = types.SimpleNamespace(
            kind="api_key",
            usage=types.SimpleNamespace(sentinel=USAGE_KEYCHAIN_UNAVAILABLE),
        )
        assert pin_is_broken(acc), "an API-key account read as pinnable"

    def test_a_daemon_that_cannot_mint_is_not_a_healthy_pin(self, tmp_path):
        """SET is not APPLYING, and the badge was lit on SET alone.

        Measured: `○ cloud` in the TUI, `pinned#1` in the
        statusline, `pin-coherence: OK` — settings, proxy.json, the daemon pid
        and the port all agreeing — while the daemon could not read the pinned
        account's credential and every request went out UNPINNED. The daemon
        had written the reason to its own log and marked its record
        `unpinnable`; nothing in this package ever read that flag, so every
        indicator the owner looks at said healthy.

        The proxy's own comment says where that ends: it "makes `cswap pin`
        report success forever while Remote Control sessions keep landing on
        the wrong account."

        This is the runtime half of `pin_is_broken`. That one asks whether the
        ACCOUNT could ever produce a bearer; this asks whether the daemon
        serving right now actually is. An account can be perfectly healthy and
        the answer still be no — a daemon started outside the macOS GUI session
        cannot reach the keychain, and it keeps serving regardless.
        """
        import json
        import types

        from claude_swap import pin

        certdir = tmp_path / "pin-proxy"
        certdir.mkdir(parents=True)
        sw = types.SimpleNamespace(backup_dir=tmp_path)

        # THE DECISION IS WHAT THIS CASE OWNS, not package resolution. Written
        # against the installed extra it returned None wherever cswap-pin is
        # absent, and `is False` failed there — green on linux and the pin-cli
        # shard, red on the windows `rest` shard, which is exactly the split
        # between "extra installed" and "not". Stub the resolver so the
        # three-state answer is tested on every shard.
        def _fake_read(cd):
            try:
                return json.loads((cd / "proxy.json").read_text())
            except OSError:
                return None

        real_live = pin._live_impl
        pin._live_impl = lambda: types.SimpleNamespace(read_daemon_state=_fake_read)
        try:
            # A record that says: serving, current, and CANNOT mint.
            (certdir / "proxy.json").write_text(
                json.dumps({"port": 53749, "pid": 41798, "unpinnable": True})
            )
            assert pin.pin_is_applying(sw) is False, (
                "a daemon that marked itself unable to mint the pinned token "
                "read as a healthy pin — this is the state the badge lit green on"
            )

            # Control: the same record without the flag must NOT be flagged, or
            # the badge cries wolf on every healthy machine.
            (certdir / "proxy.json").write_text(
                json.dumps({"port": 53749, "pid": 41798})
            )
            assert pin.pin_is_applying(sw) is not False, (
                "a healthy daemon read as broken — a warning that fires on the "
                "normal case is one people stop reading"
            )

            # AND NO RECORD AT ALL must not read as broken either. This is the
            # common case, not an edge: every machine between a pin being set
            # and the daemon first writing its record, and every machine
            # without the extra. Mutating the `not state` branch to False
            # survived the two assertions above — they both supply a record, so
            # neither reached it.
            (certdir / "proxy.json").unlink()
            assert pin.pin_is_applying(sw) is not False, (
                "no daemon record read as BROKEN — that lights the alarm on "
                "every machine that has not started one yet"
            )
        finally:
            pin._live_impl = real_live

    def test_the_stubbed_reader_matches_the_real_package(self):
        """The stub above is only honest while the real seam still exists.

        A fake that has drifted from the thing it stands in for passes forever
        and proves nothing — the failure mode this repo has already paid for.
        So where the extra IS installed, assert the function this feature calls
        is really there. Where it is not, there is nothing to drift from and
        the check is correctly silent — not skipped for convenience.
        """
        from claude_swap import pin

        impl = pin._live_impl()
        if impl is None:
            return
        assert callable(getattr(impl, "read_daemon_state", None)), (
            "cswap_pin no longer exposes read_daemon_state — pin_is_applying "
            "calls it, and the badge would silently fall back to 'healthy'"
        )


    def test_a_healthy_oauth_account_is_not_flagged(self):
        import types

        from claude_swap.tui.widgets import pin_is_broken

        acc = types.SimpleNamespace(
            kind="oauth", usage=types.SimpleNamespace(sentinel=None)
        )
        assert not pin_is_broken(acc), "a healthy account was flagged — cries wolf"


@pytest.mark.asyncio
class TestTheSwitchScreenBadgeIsResolvedOncePerSnapshot:
    """The switch screen's cards carry the ○ cloud badge, and had NO test.

    `AccountCard.render` used to call `pin.pinned_email` itself. `render()` is
    per-widget and off the poll — it fires on every repaint, resize and reflow
    — so N accounts cost N package resolutions per FRAME (450us each with the
    extra absent, the majority case). Steady state that is 0.15% of a 3s tick;
    a held arrow key repaints at key-repeat rate and the same work becomes
    ~13% of a core.

    Moving the question to `_on_snapshot` is only safe if the badge still
    lands on the right card, and nothing asserted that it ever did. So this
    pins BOTH halves in one trip: the badge is correct, and repainting does
    not re-ask. Either alone passes for the wrong reason — a card that never
    badges anything also never asks.
    """

    async def test_the_badge_lands_on_the_pinned_card_and_survives_repaints(
        self, tmp_path, monkeypatch
    ):
        from claude_swap.tui import dashboard as _dash
        from claude_swap.tui.widgets import AccountCard, AccountItem

        accounts = [
            make_account(1, active=True, email="one@e.com"),
            make_account(2, email="two@e.com"),
        ]
        fake = FakeSwitcher(accounts, tmp_path)

        calls = []
        # THE COMPOSITE SEAM. The badge asks `pinned_identity` now: an email
        # alone lights every slot sharing that address, so stubbing the old
        # seam here would leave this case measuring nothing.
        monkeypatch.setattr(
            _dash.pin, "pinned_identity",
            lambda _sw: calls.append(1) or ("two@e.com", ""),
        )

        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "switch")
            await settle(pilot)

            cards = {
                item.email: item.query_one(AccountCard)
                for item in app.screen.query(AccountItem)
            }
            assert set(cards) == {"one@e.com", "two@e.com"}, sorted(cards)

            badged = {e: "○ cloud" in c.render().plain for e, c in cards.items()}
            assert badged == {"one@e.com": False, "two@e.com": True}, (
                f"the badge is on the wrong card(s): {badged} — the pinned "
                f"account is two@e.com"
            )

            # NOW THE REGRESSION GUARD. Repaint every card without a new
            # snapshot: the old code asked the pin once per render, so this
            # count grew with frames rather than with snapshots.
            before = len(calls)
            for card in cards.values():
                for _ in range(5):
                    card.render()
            assert len(calls) == before, (
                f"rendering asked the pin {len(calls) - before} more time(s); "
                f"render() is per-widget and off the poll, so this scales with "
                f"frames — a held arrow key, a resize, a reflow"
            )

    async def test_the_dashboard_panel_does_not_re_ask_the_pin_per_frame(
        self, tmp_path, monkeypatch
    ):
        """THE SIBLING WIDGET, and it kept the pattern the card just lost.

        `AccountsPanel.render` resolved `pin.pinned_email` itself. It is one
        call per repaint rather than N, so the arithmetic is milder — but the
        argument in `AccountCard`'s docstring is about WHEN `render()` fires,
        not how many widgets fire it: resize and reflow, not the 3s poll.

        The panel already watches `snapshot`, so the answer had a place to
        live and simply was not put there. Fixing the card and leaving the
        panel would also leave two patterns for one question in one file,
        which is how the `clear_wiring` call sites drifted.
        """
        from claude_swap.tui import dashboard as _dash
        from claude_swap.tui.widgets import AccountsPanel

        calls = []
        monkeypatch.setattr(
            _dash.pin, "pinned_identity",
            lambda _sw: calls.append(1) or ("one@e.com", ""),
        )
        monkeypatch.setattr(
            "claude_swap.tui.widgets.pin.pinned_identity",
            lambda _sw: calls.append(1) or ("one@e.com", ""),
        )

        fake = FakeSwitcher([make_account(1, active=True, email="one@e.com")], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            panel = app.screen.query_one(AccountsPanel)
            assert "○ cloud" in panel.render().plain, (
                "the panel does not badge the pinned account at all, so a "
                "call count of zero below would prove nothing"
            )
            before = len(calls)
            for _ in range(5):
                panel.render()
            assert len(calls) == before, (
                f"the panel asked the pin {len(calls) - before} more time(s) "
                f"across 5 repaints; render() fires on resize and reflow, not "
                f"only on the 3s poll"
            )

@pytest.mark.asyncio
class TestTheStrandedWiringIsRemovableFromTheTui:
    """`--clear` is what a user reaches for precisely when they have
    UNINSTALLED the extra, and the CLI is deliberately able to do it without
    the package. The TUI was not: the row was gated on is_available() and the
    dispatcher resolved _impl() before every pin: action, so a TUI-first user
    who uninstalled the extra had a wired config and no visible way out.
    """

    async def test_the_row_appears_when_only_a_wiring_remains(self, tmp_path):
        from claude_swap import pin

        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        real = (pin.is_available, pin._wiring_present, pin.pinned_email)
        pin.is_available = lambda: False          # the extra is gone
        pin._wiring_present = lambda _sw: True    # its wiring is not
        pin.pinned_email = lambda _sw: None
        try:
            async with app.run_test(size=(100, 32)) as pilot:
                await settle(pilot)
                ids = [aid for _label, aid in app.screen._root_entries()]
                assert "pin-menu" in ids, (
                    "no way to remove a wiring the uninstalled extra left behind"
                )
        finally:
            pin.is_available, pin._wiring_present, pin.pinned_email = real

    async def test_the_row_stays_hidden_when_nothing_is_wired(self, tmp_path):
        """...and a user who never asked for the pin still sees nothing."""
        from claude_swap import pin

        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        real = (pin.is_available, pin._wiring_present)
        pin.is_available = lambda: False
        pin._wiring_present = lambda _sw: False
        try:
            async with app.run_test(size=(100, 32)) as pilot:
                await settle(pilot)
                ids = [aid for _label, aid in app.screen._root_entries()]
                assert "pin-menu" not in ids
        finally:
            pin.is_available, pin._wiring_present = real

    async def test_the_submenu_offers_the_clear_a_partial_clear_left_behind(
        self, tmp_path
    ):
        """The two gates must ask the same question.

        The root gate is `is_available() or _wiring_present(...)`; the submenu
        gated the clear row on the RECORD alone. A partial `clear_pin` drops
        the record and gets locked out of the wiring ("Could not remove the
        wiring — re-run once it frees up"), so the root menu showed the Cloud
        row, the submenu showed the accounts and `← back`, and the user
        following the TUI's own advice found nothing to press.
        """
        from claude_swap import pin

        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        real = (pin._impl, pin._wiring_present, pin.pinned_email)
        # _impl must resolve: without it `_pin_entries` takes its broken-package
        # branch, which offers the clear on `_wiring_present` already — so the
        # test would pass against the record-only gate it exists to catch.
        pin._impl = lambda: object()
        pin._wiring_present = lambda _sw: True   # the wiring survived
        pin.pinned_email = lambda _sw: None      # the record did not
        try:
            async with app.run_test(size=(100, 32)) as pilot:
                await settle(pilot)
                ids = [aid for _label, aid in app.screen._pin_entries()]
                assert "pin:clear" in ids, (
                    "the TUI told the user to re-run the clear and then "
                    f"offered no row to run it from: {ids}"
                )
        finally:
            pin._impl, pin._wiring_present, pin.pinned_email = real

    async def test_the_submenu_offers_the_clear_for_a_record_only_the_repo_can_see(
        self, tmp_path
    ):
        """The other half of the same mismatch, and the opposite direction.

        The row is gated on `pinned_email`, which asks the PACKAGE
        (`_live_impl().load_pin`) and returns None whenever the package is
        absent or broken. `clear_pin` decides from `_pinned_email_now`, which
        reads cswap's OWN settings.json. So with a live record and a package
        that cannot answer, the two disagree: `clear_pin` has real work to do
        (it clears the record itself, precisely for this case) and the TUI
        offers no row to run it from.

        Not the broken-package branch: `_impl` resolves here, so the submenu
        takes its normal path — the branch whose gate is under test. The
        wiring is absent, so `_wiring_present` cannot carry the row either and
        the record is the only thing that can.
        """
        from claude_swap import pin

        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        real = (pin._impl, pin._wiring_present, pin.pinned_email,
                pin._pinned_email_now)
        pin._impl = lambda: object()
        pin._wiring_present = lambda _sw: False        # no wiring left
        pin.pinned_email = lambda _sw: None            # the PACKAGE cannot say
        pin._pinned_email_now = lambda _sw: ("a@b.c", "")  # cswap's file CAN
        try:
            async with app.run_test(size=(100, 32)) as pilot:
                await settle(pilot)
                ids = [aid for _label, aid in app.screen._pin_entries()]
                assert "pin:clear" in ids, (
                    f"a pin record cswap can see and remove — and which keeps "
                    f"the pin live the moment the package returns — had no "
                    f"row to remove it from: {ids}"
                )
        finally:
            (pin._impl, pin._wiring_present, pin.pinned_email,
             pin._pinned_email_now) = real

    async def test_clear_reaches_pin_py_without_the_package(self, tmp_path):
        """The dispatcher resolved _impl() for EVERY pin: action, so the one
        operation still available was the one it refused."""
        from claude_swap import pin

        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        started, cleared = [], []
        real = (pin._impl, pin.clear_pin)
        pin._impl = lambda: (_ for _ in ()).throw(
            ClaudeSwitchError("The cloud pin requires 'cswap-pin'")
        )
        pin.clear_pin = lambda sw: cleared.append(sw) or (True, "Unpinned")
        try:
            async with app.run_test(size=(100, 32)) as pilot:
                await settle(pilot)
                app._start_action = lambda label, fn: (
                    started.append(label), fn()
                )[0]
                await app.screen._dispatch("pin:clear")
            assert cleared, (
                "clear never reached pin.clear_pin — the TUI refused the only "
                "pin operation that works without the package"
            )
            assert started == ["clear cloud pin"]
        finally:
            pin._impl, pin.clear_pin = real

    async def test_a_snapshot_actually_rebuilds_the_root_menu(self, tmp_path):
        """The SUBSCRIPTION, not just the method it calls.

        `refresh_root_menu` had a direct-call test, so its logic was covered —
        but nothing asserted that anything FIRES it. The old assertion grepped
        `on_mount`'s source for the name, which a comment satisfies:
        deleting `self.watch(self.app, "snapshot", ...)` and leaving the name
        in a comment kept the whole suite green, while the pin row could no
        longer appear on a mid-session install. That is the exact behaviour the
        test names.

        So drive a snapshot through the app and assert the rebuild happened.
        """
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            calls = []
            original = app.screen.refresh_root_menu

            async def _counted():
                calls.append(1)
                await original()

            app.screen.refresh_root_menu = _counted
            # A poll publishes a new snapshot; that is the app's own mechanism,
            # not a helper invented here.
            app.snapshot = types.SimpleNamespace(accounts=[make_account(1, active=True)])
            await pilot.pause()
            await settle(pilot)
            assert calls, (
                "a snapshot did not rebuild the root menu — the pin row cannot "
                "appear on a mid-session install"
            )


@pytest.mark.asyncio
class TestAnOrphanedRecordDoesNotHideItsOwnRemoval:
    """A pin RECORD with no wiring must still show the surface that clears it.

    Only `clear_pin` ever removes `settings.json -> remoteControl`. `heal`,
    `wire_launch_env` and `--ensure` all remove the WIRING and leave the
    record — by design, because the wiring is what strands a launch and the
    record is not. So "record present, wiring gone" is not a corner case, it
    is where every one of those paths lands.

    In that state the root menu asked `is_available() or _wiring_present()`
    and hid the Cloud row, while the record still named an account that
    re-pins live the moment anything reinstalls the package. The leaf gate one
    screen down already had the rule in its own comment — "a gate must ask
    what the ACTION asks, or it hides work that exists" — and the root gate
    was the copy that did not get it.

    THE SAME DRIFT AS `clear_wiring`'s three call sites, one file over: a rule
    written once at one site and not at its siblings.
    """

    def _orphaned_record(self, tmp_path, monkeypatch):
        """Record present, wiring absent, package gone — what `heal` leaves."""
        from claude_swap import pin
        from claude_swap.tui import dashboard as _dash

        monkeypatch.setattr(_dash.pin, "is_available", lambda: False)
        monkeypatch.setattr(_dash.pin, "_wiring_present", lambda _sw: False)
        monkeypatch.setattr(
            _dash.pin, "_pinned_email_now", lambda _sw: ("c@e.com", None)
        )
        return pin

    async def test_the_root_menu_still_offers_the_cloud_row(
        self, tmp_path, monkeypatch
    ):
        from claude_swap.tui.widgets import MenuItem

        self._orphaned_record(tmp_path, monkeypatch)
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            actions = [i.action_id for i in app.screen.query(MenuItem)]
            assert "pin-menu" in actions, (
                f"the Cloud row is hidden while a pin RECORD still names an "
                f"account: {actions}. That record re-pins live the moment the "
                f"package is reinstalled, and the TUI now offers no way to "
                f"remove it"
            )

    async def test_the_broken_package_submenu_still_offers_the_clear(
        self, tmp_path, monkeypatch
    ):
        """The row exists but dead-ends: same gate, one screen down.

        With the package BROKEN rather than absent, `_pin_entries` takes its
        error branch and offers the clear only when a wiring survives —
        while `clear_pin` can remove the record without the package at all.
        """
        from claude_swap.exceptions import ClaudeSwitchError
        from claude_swap.tui import dashboard as _dash
        from claude_swap.tui.widgets import MenuItem

        self._orphaned_record(tmp_path, monkeypatch)
        monkeypatch.setattr(_dash.pin, "is_available", lambda: True)

        def _boom():
            raise ClaudeSwitchError("the extra is installed but not usable")

        monkeypatch.setattr(_dash.pin, "_impl", _boom)
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "pin-menu")
            await settle(pilot)
            actions = [i.action_id for i in app.screen.query(MenuItem)]
            assert "pin:clear" in actions, (
                f"the broken-package submenu dead-ends on the error string "
                f"with a removable record still on disk: {actions}"
            )

class TestTheCloudBadgeNeedsTheOrganizationToo:
    """The badge answers "which account owns the claude.ai side", and an email
    does not identify an account here.

    Two managed slots may share one address across organizations -- the premise
    `pinned_identity` was written for. Matching on the email alone badges BOTH
    rows, and every health reading printed beside the badge (`pin_is_broken`,
    `pin_is_applying`) is then taken against whichever row matched first. A
    healthy pin renders as broken on the sibling, or a dead one renders clean.
    """

    def test_a_same_email_sibling_is_not_the_pinned_account(self):
        from claude_swap import pin

        pinned = ("shared@example.com", "org-PIN")
        assert pin.account_is_pinned(pinned, "shared@example.com", "org-PIN") is True
        assert pin.account_is_pinned(pinned, "shared@example.com", "org-OTHER") is False, (
            "an email-only comparison badges the sibling slot too, and the pin "
            "health beside it is then read from the wrong account"
        )

    def test_a_different_address_is_never_the_pinned_account(self):
        """The control: without it a predicate that returned True whenever an
        identity exists would pass the case above."""
        from claude_swap import pin

        assert pin.account_is_pinned(
            ("a@example.com", "org-1"), "b@example.com", "org-1") is False

    def test_no_pin_badges_nothing(self):
        from claude_swap import pin

        assert pin.account_is_pinned(None, "a@example.com", "org-1") is False

    def test_a_missing_org_is_compared_as_empty_not_dropped(self):
        """A roster row imported before the org fields existed carries "". It
        must still match a pin whose org is also "" -- and must NOT match one
        that has an org, which is what dropping the field would do."""
        from claude_swap import pin

        assert pin.account_is_pinned(("a@example.com", ""), "a@example.com", "") is True
        assert pin.account_is_pinned(("a@example.com", "org-1"), "a@example.com", "") is False


class TestTwoSlotsAtOneAddressLightOneBadge:
    """The behavioural half, and it is the durable one.

    The structural tripwire beside this class matches source SHAPES, and a
    matcher can be laundered: rename `AccountsPanel._pinned_identity` to
    something that does not spell a reader, then alias the address, and an
    email-only badge walks past it with the whole suite green.

    This renders instead. Two accounts at ONE address in DIFFERENT
    organizations is the entire subject of `account_is_pinned`; an email-only
    test lights both cards, and no source rewrite hides that from a reader
    counting badges. The repaint case above uses two DIFFERENT addresses, so
    it cannot see this.
    """

    @pytest.mark.asyncio
    async def test_only_the_pinned_organization_gets_the_badge(
        self, tmp_path, monkeypatch
    ):
        from claude_swap.tui import dashboard as _dash
        from claude_swap.tui.widgets import AccountCard, AccountItem

        accounts = [
            make_account(1, active=True, email="shared@e.com", org_uuid="org-A"),
            make_account(2, email="shared@e.com", org_uuid="org-B"),
        ]
        fake = FakeSwitcher(accounts, tmp_path)
        monkeypatch.setattr(
            _dash.pin, "pinned_identity", lambda _sw: ("shared@e.com", "org-B"))

        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "switch")
            await settle(pilot)

            cards = [(item.number, item.query_one(AccountCard))
                     for item in app.screen.query(AccountItem)]
            assert len(cards) == 2, f"premise: expected two cards, got {cards}"
            badged = sorted(n for n, c in cards
                            if "○ cloud" in c.render().plain)
            assert badged == ["2"], (
                f"two slots share one address across organizations and the "
                f"badge landed on {badged} — an email-only answer lights both"
            )


    @pytest.mark.asyncio
    async def test_the_accounts_panel_badges_one_card_too(
        self, tmp_path, monkeypatch
    ):
        """THE OTHER RENDER SITE. `AccountItem` is fed `cloud_pinned` by
        `dashboard.py`; `AccountsPanel.render` decides for itself. The case
        above cannot see this one, and a laundered email-only badge here —
        the reader attribute renamed, the address aliased — passed the whole
        suite with the structural tripwire green.
        """
        from claude_swap.tui import widgets as _w

        accounts = [
            make_account(1, active=True, email="shared@e.com", org_uuid="org-A"),
            make_account(2, email="shared@e.com", org_uuid="org-B"),
        ]
        fake = FakeSwitcher(accounts, tmp_path)
        monkeypatch.setattr(
            _w.pin, "pinned_identity", lambda _sw: ("shared@e.com", "org-B"))

        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            panel = app.screen.query_one(_w.AccountsPanel)
            panel._resolve_and_refresh()
            text = panel.render().plain

        assert text.count("○ cloud") == 1, (
            "two slots share one address across organizations and the panel "
            f"drew {text.count('○ cloud')} badges — an email-only answer "
            "lights both"
        )


class TestEveryBadgeSiteAsksTheSameQuestion:
    """The structural half. Three widgets each answered it by hand, and the
    third was found only after the first two were fixed -- so assert that none
    of them compares an address to a pin on its own again.
    """

    # The pin readers. A pin value is whatever one of these returns, plus
    # anything a module binds it to.
    _PIN_READERS = ("pinned_identity", "pinned_email", "_pinned_email_now")

    @classmethod
    def _pin_valued(cls, tree):
        """Names in this module that hold a pin value, aliases included.

        Seeded from the readers' own names -- `pin.pinned_identity(...)`,
        `self._pinned_identity` -- then propagated across plain assignments so
        `p = pinned_identity` does not launder it. Two passes: the widgets
        module already binds through an attribute and then re-binds locally.
        """
        import ast

        def is_pin(node):
            t = ast.unparse(node)
            return any(r in t for r in cls._PIN_READERS)

        names: set[str] = set()
        for _ in range(2):
            for node in ast.walk(tree):
                if not isinstance(node, ast.Assign):
                    continue
                src_is_pin = is_pin(node.value) or (
                    isinstance(node.value, ast.Name) and node.value.id in names)
                if not src_is_pin:
                    continue
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        names.add(tgt.id)
        return names, is_pin

    def test_no_tui_site_reads_a_pin_apart_from_the_predicate(self):
        """A badge site must ask `account_is_pinned`, never take the pin apart.

        Two shapes make a site answer on the address alone, and both are
        invisible to a rule about what a comparison's operands are CALLED:

        - `pinned_identity[0]` drops the org, so two slots sharing one address
          both render pinned. Subscripting a pin value is the whole offence;
          there is no correct reason for a TUI module to index one.
        - `mine = acc.email` one line above the comparison removes the `.email`
          attribute the address half was recognised by.

        So the invariant is what the code DOES to a pin value, not what it
        spells: in a TUI module a pin value may be tested for truth, stored,
        and rendered, but never indexed and never compared to anything but a
        constant. `is not None` stays legal; `== acc.email` does not.

        The previous cut also carried an `inside_predicate` escape that walked
        INTO the comparison looking for `account_is_pinned` -- the sanctioned
        form is a call whose ARGUMENT is the address, with no comparison in it
        at all, so the branch could never be taken by correct code. A condition
        that cannot fire is not an exemption, it is a dead operand.
        """
        import ast
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent / "src" / "claude_swap" / "tui"
        modules = sorted(root.rglob("*.py"))
        offenders = []
        for path in modules:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            pin_names, is_pin = self._pin_valued(tree)

            def pin_valued(node):
                return is_pin(node) or (
                    isinstance(node, ast.Name) and node.id in pin_names)

            for node in ast.walk(tree):
                if isinstance(node, ast.Subscript) and pin_valued(node.value):
                    offenders.append(
                        f"{path.name}:{node.lineno}  indexes a pin value: "
                        f"{ast.unparse(node)}")
                    continue
                if not isinstance(node, ast.Compare):
                    continue
                sides = [node.left, *node.comparators]
                if len(sides) != 2:
                    continue
                # BY WHAT THE SIDE SAYS, not by its node type. Keying on
                # `ast.Name` let `acc.email == pinned_identity[0]` through --
                # a Subscript -- which is exactly the shape a regression takes
                # once the composite is in scope.
                rendered = [ast.unparse(x) for x in sides]
                has_email = any(
                    isinstance(x, ast.Attribute) and x.attr == "email" for x in sides)
                pin_side = [i for i, x in enumerate(sides) if pin_valued(x)]
                # A pin compared to a literal is a presence test, not a match.
                against_constant = any(
                    isinstance(sides[1 - i], ast.Constant) for i in pin_side)
                if has_email or (pin_side and not against_constant):
                    offenders.append(
                        f"{path.name}:{node.lineno}  {rendered[0]} == {rendered[1]}")
        # THE CONTROL: an empty walk would pass vacuously.
        assert len(modules) > 2, "the walk found no TUI modules"
        assert any(self._pin_valued(ast.parse(m.read_text(encoding="utf-8")))[0]
                   for m in modules), (
            "no TUI module binds a pin value — the alias tracker matched "
            "nothing, so its half of this guard proves nothing"
        )
        assert offenders == [], (
            "a TUI site takes a pin apart instead of calling "
            "`pin.account_is_pinned`, so two slots sharing one address both "
            f"render as pinned: {offenders}"
        )

