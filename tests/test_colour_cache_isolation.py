"""One test must not decide the styling of the next one.

Scrubbing ``FORCE_COLOR``/``NO_COLOR`` is only half the job, because detection
CACHES. ``colors_enabled()`` latches its first answer into
``printer._colors_enabled`` and every later call returns it, so a single
earlier test that latched ``True`` styles every assertion after it no matter
how thoroughly the environment is cleaned. Same story for ``printer._theme``.

pytest runs tests in definition order within a file, so each pair below is a
real ordering: the first test latches, the second reads. THE PAIR IS THE
GUARD; EITHER ALONE PROVES NOTHING, and the ordering it needs is not
guaranteed — a run that selects only the reader, or that splits the pair
across `-n` workers, is green over a broken fixture for a reason that has
nothing to do with the fixture working.

What actually closes the hole is the post-condition in
`conftest._deterministic_colour`, which asserts both globals are unlatched on
entry to EVERY test — no ordering required, and mutation-killed directly (not
via ordering) by `TestEntryAssertionsCatchAPoisonedGlobal` in
`test_printer.py`. These pairs stay as the readable narrative of what the
leak looks like; they are not what makes the suite safe.
"""

from __future__ import annotations

import os
import sys
from io import StringIO

import pytest

from claude_swap import printer


def test_a_test_may_latch_the_colour_cache():
    """Stands in for the ordinary tests that latch it in a real run."""
    printer._colors_enabled = True
    assert printer.colors_enabled() is True


def test_the_next_test_is_not_styled_by_it(monkeypatch):
    """The reset in ``_deterministic_colour`` is what makes this pass.

    Without it this test inherits the ``True`` latched above and
    ``accent()`` returns the escape-wrapped string — the exact failure the
    eleven ``test_switcher`` assertions report.

    stdout is pinned to a StringIO as the rest of the suite does, so that with
    the cache cleared detection falls through to ``isatty()`` and answers
    False. Asserting on OUTPUT rather than on ``printer._colors_enabled``:
    checking the private flag would pass for free on any run where nothing had
    latched it yet, which is most of them.
    """
    monkeypatch.setattr(sys, "stdout", StringIO())
    assert printer.accent("Skipping") == "Skipping"
    assert "\x1b[" not in printer.muted("usage")



def test_a_test_may_latch_the_theme(monkeypatch):
    """Stands in for `tui/app.py`'s `set_theme("light")`, which nothing restores."""
    monkeypatch.setenv("FORCE_COLOR", "1")
    printer._colors_enabled = None
    printer.set_theme("light")
    assert "38;2;149;76;42" in printer.accent("x")   # premise: light is live


def test_the_next_test_is_not_themed_by_it(monkeypatch):
    """The `_theme` reset is what makes this pass.

    `printer._theme` is the other latched global in this module, and the leak
    is real: with the reset removed, tests in several later files enter with
    the light palette, so the latch outlives the file that set it. Green
    without it only because none of them asserts a palette code — which is
    exactly what was true of `_colors_enabled` until someone exported
    FORCE_COLOR.

    (The count and per-file breakdown that used to be here were hand-copied
    from `conftest.py` and were wrong in both copies; they are gone from both.
    See the note beside the `_theme` reset.)

    Asserts on the palette bytes rather than on `printer._theme`, for the same
    reason the cache guard above asserts on output: the private name being
    right proves nothing about what a caller renders.
    """
    monkeypatch.setenv("FORCE_COLOR", "1")
    printer._colors_enabled = None
    assert "38;5;173" in printer.accent("x"), (
        "the previous test's light theme outlived it"
    )


@pytest.mark.skipif(
    os.name == "nt", reason="pty/termios are POSIX; the query returns at line 95 there"
)
def test_the_suite_does_not_query_the_developers_terminal(monkeypatch):
    """`detect_terminal_background` must not reach a real tty from the suite.

    POSIX only, and not merely because `pty` is missing on Windows: the
    function's first gate is `os.name == "nt"`, so there is no terminal query
    to guard there in the first place.

    It puts the tty into cbreak, writes an OSC-11 query, and blocks reading
    stdin for up to a second. Under `pytest -s` stdin IS the developer's
    terminal, so the suite emits escape bytes at it and can swallow a keypress.

    A real pty makes both `isatty()` calls genuinely True, which removes the
    gate that masks this under plain pytest — so the only thing left standing
    between the suite and the terminal is the fixture's TERM pin, and the
    assertion reads the pty master to see whether the query actually went out.
    Asserting on the BYTES rather than on `os.environ["TERM"]`: the variable
    being right proves nothing about whether the function short-circuited, and
    a box whose TERM is already dumb would pass for free.

    Deliberately does NOT set TERM itself. Doing so overrides the fixture pin
    and turns this into a test of its own setup — measured, it flips a passing
    guard into a failing one.
    """
    import io
    import pty

    from claude_swap import appearance

    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("STY", raising=False)
    monkeypatch.setattr(appearance, "_cache", appearance._UNSET)

    master, slave = pty.openpty()
    try:
        stream = io.TextIOWrapper(
            io.FileIO(slave, "r+", closefd=False), write_through=True
        )
        monkeypatch.setattr(sys, "stdin", stream)
        monkeypatch.setattr(sys, "stdout", stream)
        assert sys.stdin.isatty() and sys.stdout.isatty(), "premise: a real tty"

        assert appearance.detect_terminal_background() is None

        # AND the pin must be the value that also keeps COLOUR off, checked
        # HERE while stdout is still the pty — outside this block stdout is
        # pytest's capture object, `isatty()` is False, and the check passes
        # for a reason that has nothing to do with TERM.
        #
        # `appearance` short-circuits on both `dumb` and `linux`
        # (appearance.py:97), so the emitted bytes cannot tell them apart:
        # measured, changing the pin to `linux` left the whole suite green on
        # seq, `-n 4` and eight seeds. `printer` tests only `== "dumb"`
        # (printer.py:97), so `linux` re-enables the styling this fixture
        # exists to suppress. The two modules disagree and only one of them
        # was covered.
        printer._colors_enabled = None
        colours_on = printer.colors_enabled()

        os.set_blocking(master, False)
        try:
            emitted = os.read(master, 4096)
        except BlockingIOError:
            emitted = b""
    finally:
        os.close(master)
        os.close(slave)

    assert b"\x1b]11;?" not in emitted, (
        f"the OSC-11 query hit the terminal: {emitted!r}"
    )
    assert colours_on is False, (
        f"TERM={os.environ.get('TERM')!r} blocks the OSC-11 query but not "
        "colour detection; under `-s` the suite styles its own assertions"
    )


class TestScopedContextDoesNotLeakTheAutouseScrub:
    """H-1 regression: pytest hands out ONE `MonkeyPatch` per test regardless
    of how many fixtures request it, so `monkeypatch.undo()` in a test body
    also unwinds `_deterministic_colour`'s scrub -- which is what
    `tests/test_move_accounts.py` and `tests/test_swap_accounts.py` did at 8
    sites before this branch moved them to a scoped `MonkeyPatch.context()`.

    `FORCE_COLOR` is exported class-scoped, the way `test_printer.py`'s
    `_exported` simulates a real developer shell (ahead of the
    function-scoped autouse fixture), so this test fails if the fix pattern
    ever regresses back to a shared-instance `.undo()`.
    """

    @pytest.fixture(autouse=True, scope="class")
    def _exported(self):
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("FORCE_COLOR", "3")
            yield

    def test_scoped_context_does_not_leak_it(self):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(sys, "platform", sys.platform)
        assert os.environ.get("FORCE_COLOR") is None, (
            "a scoped MonkeyPatch.context() must not touch the autouse scrub"
        )
