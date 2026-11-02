"""Tests for the printer module."""

from __future__ import annotations

import sys
from io import StringIO

import pytest

from claude_swap import printer
from tests.conftest import _deterministic_colour


class TestColorDetection:
    """Tests for color support detection."""

    def test_no_color_env_disables(self, monkeypatch):
        monkeypatch.setenv("NO_COLOR", "1")
        assert printer._detect_color_support() is False

    def test_no_color_empty_value_disables(self, monkeypatch):
        monkeypatch.setenv("NO_COLOR", "")
        assert printer._detect_color_support() is False

    def test_force_color_enables(self, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("FORCE_COLOR", "1")
        assert printer._detect_color_support() is True

    def test_non_tty_disables(self, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.delenv("FORCE_COLOR", raising=False)
        monkeypatch.setattr(sys, "stdout", StringIO())
        assert printer._detect_color_support() is False

    def test_dumb_term_disables(self, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.delenv("FORCE_COLOR", raising=False)
        monkeypatch.setenv("TERM", "dumb")
        # Need a fake tty
        fake_stdout = StringIO()
        fake_stdout.isatty = lambda: True  # type: ignore[attr-defined]
        monkeypatch.setattr(sys, "stdout", fake_stdout)
        if sys.platform != "win32":
            assert printer._detect_color_support() is False

    def test_colors_enabled_caches(self, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("FORCE_COLOR", "1")
        assert printer.colors_enabled() is True
        # Even after removing FORCE_COLOR, cached value persists
        monkeypatch.delenv("FORCE_COLOR")
        assert printer.colors_enabled() is True


class TestStyling:
    """Tests for styling functions."""

    def test_style_with_colors_disabled(self, monkeypatch):
        monkeypatch.setenv("NO_COLOR", "1")
        assert printer.accent("hello") == "hello"
        assert printer.muted("hello") == "hello"
        assert printer.dimmed("hello") == "hello"
        assert printer.bolded("hello") == "hello"
        assert printer.bold_accent("hello") == "hello"

    def test_style_with_colors_enabled(self, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("FORCE_COLOR", "1")
        result = printer.accent("hello")
        assert "hello" in result
        assert "\033[38;5;173m" in result
        assert "\033[0m" in result

    def test_muted_with_colors_enabled(self, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("FORCE_COLOR", "1")
        result = printer.muted("org name")
        assert "\033[38;5;250m" in result
        assert "org name" in result

    def test_dimmed_with_colors_enabled(self, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("FORCE_COLOR", "1")
        result = printer.dimmed("secondary")
        assert "\033[2m" in result
        assert "secondary" in result

    def test_bolded_with_colors_enabled(self, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("FORCE_COLOR", "1")
        result = printer.bolded("header")
        assert "\033[1m" in result
        assert "header" in result

    def test_bold_accent_with_colors_enabled(self, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("FORCE_COLOR", "1")
        result = printer.bold_accent("(active)")
        assert "\033[1m" in result
        assert "\033[38;5;173m" in result
        assert "(active)" in result


class TestThemePalette:
    """Tests for set_theme and the per-theme color palette."""

    def test_light_theme_changes_accent(self, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("FORCE_COLOR", "1")
        printer._colors_enabled = None
        printer.set_theme("light")
        assert "38;2;149;76;42" in printer.accent("x")   # #954c2a
        printer.set_theme("dark")
        assert "38;5;173" in printer.accent("x")

    def test_light_theme_error_uses_light_red(self, monkeypatch, capsys):
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("FORCE_COLOR", "1")
        printer._colors_enabled = None
        printer.set_theme("light")
        printer.error("boom")
        assert "38;2;173;49;40" in capsys.readouterr().err   # #ad3128

    def test_unknown_theme_falls_back_to_dark(self, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("FORCE_COLOR", "1")
        printer._colors_enabled = None
        printer.set_theme("bogus")
        assert "38;5;173" in printer.accent("x")


class TestLinePrinters:
    """Tests for line-level print functions."""

    def test_error_prints_to_stderr(self, monkeypatch, capsys):
        monkeypatch.setenv("NO_COLOR", "1")
        printer.error("something failed")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "something failed" in captured.err

    def test_error_with_color(self, monkeypatch, capsys):
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("FORCE_COLOR", "1")
        printer.error("something failed")
        captured = capsys.readouterr()
        assert "\033[31m" in captured.err

    def test_warning_prints_to_stdout(self, monkeypatch, capsys):
        monkeypatch.setenv("NO_COLOR", "1")
        printer.warning("be careful")
        captured = capsys.readouterr()
        assert "be careful" in captured.out

    def test_warning_with_color(self, monkeypatch, capsys):
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("FORCE_COLOR", "1")
        printer.warning("be careful")
        captured = capsys.readouterr()
        assert "\033[33m" in captured.out


def test_force_color_overrides_and_restores():
    from claude_swap import printer
    saved = printer._colors_enabled
    try:
        printer._colors_enabled = False
        with printer.force_color():
            assert printer.colors_enabled() is True
            assert printer.accent("X") == "\x1b[38;5;173mX\x1b[0m"
        assert printer._colors_enabled is False
    finally:
        printer._colors_enabled = saved


class TestForceUtf8Output:
    """Tests for force_utf8_output (issue #113: cp1252 console crash)."""

    def test_reconfigures_legacy_stream_to_utf8(self, monkeypatch):
        # A cp1252-encoded stdout raises on the tool's glyphs before the fix.
        import io

        stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
        with pytest.raises(UnicodeEncodeError):
            stream.write("● → ├ ─ └")
            stream.flush()

        monkeypatch.setattr(sys, "stdout", stream)
        monkeypatch.setattr(sys, "stderr", stream)
        printer.force_utf8_output()

        assert stream.encoding == "utf-8"
        # No longer raises now that the stream encodes UTF-8.
        stream.write("● → ├ ─ └")
        stream.flush()

    def test_no_op_on_streams_without_reconfigure(self, monkeypatch):
        # StringIO has no reconfigure(); the guard must skip it silently.
        monkeypatch.setattr(sys, "stdout", StringIO())
        monkeypatch.setattr(sys, "stderr", StringIO())
        printer.force_utf8_output()  # must not raise


class TestColourEnvDoesNotLeakIntoTests:
    """A developer's terminal must not decide whether the suite passes.

    See ``_deterministic_colour`` in conftest for the measurement. These
    assert on OUTPUT: checking only ``"FORCE_COLOR" not in os.environ``
    passes for free on any box that never exported it, which is most
    boxes and every CI runner.

    ASSERTING ON OUTPUT IS NOT ENOUGH ON ITS OWN, which is what ``_exported``
    below is for. With BOTH ``delenv`` lines deleted from the fixture and
    neither variable exported, the whole suite stays fully green — so in the
    environment CI actually runs in, the two scrubs were dead code no test
    could kill. Their coverage existed only on a box whose developer had
    exported the variable, i.e. only under the condition this change exists to
    remove. A guard is not covered by a test that needs the bug to be
    happening already. (The class below closes that hole directly, via
    ``TestEntryAssertionsCatchAPoisonedGlobal`` in this same file — the same
    argument applied to ``_deterministic_colour``'s own entry assertions.)
    """

    @pytest.fixture(autouse=True, scope="class")
    def _exported(self):
        """Export both variables BEFORE ``_deterministic_colour`` runs.

        A test cannot set them in its own body: the fixture has already
        scrubbed by then, so the assignment lands after the thing under test
        and proves nothing. Class scope is what buys the ordering — pytest
        instantiates higher-scoped fixtures first, so this runs ahead of the
        function-scoped conftest one. (A module-level FUNCTION-scoped autouse
        fixture would run after it instead, which is the same trap one level
        down.)

        Scoped to this class, not the module, so the export cannot reach a
        test that is not about it.
        """
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("FORCE_COLOR", "3")
            mp.setenv("NO_COLOR", "1")
            yield

    def test_styled_output_is_plain(self, monkeypatch):
        """The reduced form of the 11 test_switcher failures.

        Guards the FORCE_COLOR scrub: without it this returns the styled
        string on any machine that exported the variable.

        stdout is pinned to a StringIO, as the rest of this file does. The
        fixture guarantees the variables are gone, not that stdout is not a
        tty — so under ``pytest -s`` on a terminal, detection correctly falls
        through to isatty() and returns True. Asserting unconditionally made
        the outcome depend on how the developer invoked pytest, which is the
        failure class this whole change exists to remove.
        """
        monkeypatch.setattr(sys, "stdout", StringIO())
        assert printer.accent("Skipping") == "Skipping"
        assert "\x1b[" not in printer.muted("usage")

    def test_detection_reaches_isatty_rather_than_an_override(self, monkeypatch):
        """Guards the NO_COLOR scrub, which the test above cannot see.

        Both scrubs land on the same OUTPUT — plain — so a fixture that
        cleared only FORCE_COLOR would still pass the test above while
        leaving NO_COLOR free to steer any suite asserting that styling IS
        present. What separates them is WHY the answer is plain: with the
        variables gone, detection has to fall through to the captured-stdout
        ``isatty()`` check, so forcing that to report a TTY must flip it.
        A surviving NO_COLOR would pin it False regardless.

        TERM is pinned for the same reason the fixture scrubs the other two:
        detection consults it AFTER isatty(), so on a ``TERM=dumb`` terminal —
        Emacs M-x shell, some CI shells — this assertion fails for a reason
        that has nothing to do with what it is guarding. Removing one
        environment dependency from the suite while adding a narrower one is
        the same defect in a smaller coat.
        """
        monkeypatch.setenv("TERM", "xterm-256color")
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
        assert printer.colors_enabled() is True


class TestEntryAssertionsCatchAPoisonedGlobal:
    """The two `assert inherited[...]` lines in `_deterministic_colour`,
    mutation-killed directly rather than via test ordering.

    Calling the fixture body with a scratch `MonkeyPatch` needs no ordering
    trick and never touches this test's own fixture instance: poison the
    global, invoke the guard, and it must raise. Delete either assertion and
    the matching test below dies (measured).
    """

    @staticmethod
    def _invoke_guard():
        with pytest.MonkeyPatch.context() as mp:
            _deterministic_colour.__wrapped__(mp)

    def test_kills_colors_enabled_assertion(self, monkeypatch):
        monkeypatch.setattr(printer, "_colors_enabled", True)
        with pytest.raises(AssertionError, match="latched the colour cache"):
            self._invoke_guard()

    def test_kills_theme_assertion(self, monkeypatch):
        monkeypatch.setattr(printer, "_theme", "light")
        with pytest.raises(AssertionError, match="latched the theme"):
            self._invoke_guard()
