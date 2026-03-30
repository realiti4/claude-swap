"""Tests for the printer module."""

from __future__ import annotations

import sys
from io import StringIO

import pytest

from claude_swap import printer


@pytest.fixture(autouse=True)
def _reset_color_cache():
    """Reset the color detection cache before each test."""
    printer._colors_enabled = None
    yield
    printer._colors_enabled = None


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
