"""Tests for terminal-background detection and theme resolution."""
from __future__ import annotations

import sys

import pytest

from claude_swap import appearance


@pytest.fixture(autouse=True)
def _reset_detect_cache():
    appearance._reset_cache()
    yield
    appearance._reset_cache()


class TestParseOsc11:
    def test_parses_16bit_rgb(self):
        assert appearance._parse_osc11(b"\x1b]11;rgb:ffff/ffff/ffff\x07") == (1.0, 1.0, 1.0)

    def test_parses_8bit_rgb(self):
        r, g, b = appearance._parse_osc11(b"\x1b]11;rgb:00/00/00\x1b\\")
        assert (r, g, b) == (0.0, 0.0, 0.0)

    def test_parses_hex(self):
        r, _, b = appearance._parse_osc11(b"\x1b]11;#ff8000\x07")
        assert r == pytest.approx(1.0) and b == pytest.approx(0.0)

    def test_junk_returns_none(self):
        assert appearance._parse_osc11(b"garbage") is None

    def test_unframed_rgb_is_rejected(self):
        # A bare "rgb:..." with no OSC-11 frame must not be mistaken for a
        # reply (e.g. echoed/interleaved input containing similar text).
        assert appearance._parse_osc11(b"rgb:ffff/ffff/ffff") is None

    def test_rgb_without_leading_esc_is_rejected(self):
        # "]11;rgb:..." without the ESC that actually opens an OSC sequence
        # must not be mistaken for a reply (e.g. echoed/interleaved input).
        assert appearance._parse_osc11(b"]11;rgb:ffff/ffff/ffff\x07") is None

    def test_framed_rgb_parses(self):
        assert appearance._parse_osc11(b"\x1b]11;rgb:ffff/ffff/ffff\x07") == (1.0, 1.0, 1.0)

    def test_unframed_hex_is_rejected(self):
        assert appearance._parse_osc11(b"#ff8000") is None

    def test_framed_hex_parses(self):
        r, _, b = appearance._parse_osc11(b"\x1b]11;#ff8000\x07")
        assert r == pytest.approx(1.0) and b == pytest.approx(0.0)


class TestClassify:
    def test_white_is_light(self):
        assert appearance._classify(b"\x1b]11;rgb:ffff/ffff/ffff\x07") == "light"

    def test_black_is_dark(self):
        assert appearance._classify(b"\x1b]11;rgb:0000/0000/0000\x07") == "dark"

    def test_near_boundary_grey_cutoff_is_pinned(self):
        # 0x8080 ≈ 0.502 luminance → just over the 0.5 cutoff → light.
        assert appearance._classify(b"\x1b]11;rgb:8080/8080/8080\x07") == "light"
        # 0x7f7f ≈ 0.498 → dark.
        assert appearance._classify(b"\x1b]11;rgb:7f7f/7f7f/7f7f\x07") == "dark"

    def test_unparseable_returns_none(self):
        assert appearance._classify(b"nope") is None


class TestResolveTheme:
    def test_dark_passes_through_without_detecting(self):
        def _boom():
            raise AssertionError("detect must not be called for explicit theme")
        assert appearance.resolve_theme("dark", detect=_boom) == "dark"
        assert appearance.resolve_theme("light", detect=_boom) == "light"

    def test_auto_follows_detection(self):
        assert appearance.resolve_theme("auto", detect=lambda: "light") == "light"
        assert appearance.resolve_theme("auto", detect=lambda: "dark") == "dark"

    def test_auto_none_falls_back_to_dark(self):
        assert appearance.resolve_theme("auto", detect=lambda: None) == "dark"


class TestDetectGuards:
    def test_non_tty_returns_none(self, monkeypatch):
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
        assert appearance.detect_terminal_background() is None

    def test_result_is_cached(self, monkeypatch):
        calls = {"n": 0}
        def _fake_query():
            calls["n"] += 1
            return b"\x1b]11;rgb:ffff/ffff/ffff\x07"
        monkeypatch.setattr(appearance, "_query_terminal_background", _fake_query)
        assert appearance.detect_terminal_background() == "light"
        assert appearance.detect_terminal_background() == "light"
        assert calls["n"] == 1  # queried once, cached thereafter

    def test_none_result_is_cached(self, monkeypatch):
        calls = {"n": 0}
        def _fake_query():
            calls["n"] += 1
            return None
        monkeypatch.setattr(appearance, "_query_terminal_background", _fake_query)
        assert appearance.detect_terminal_background() is None
        assert appearance.detect_terminal_background() is None
        assert calls["n"] == 1  # queried once, cached thereafter

    def test_fileno_unsupported_operation_does_not_raise(self, monkeypatch):
        import io

        monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)

        def _boom():
            raise io.UnsupportedOperation("fileno")
        monkeypatch.setattr(sys.stdin, "fileno", _boom, raising=False)

        assert appearance.detect_terminal_background() is None

    def test_termios_error_during_setcbreak_does_not_raise(self, monkeypatch):
        termios = pytest.importorskip("termios")
        import tty

        monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
        monkeypatch.setattr(sys.stdin, "fileno", lambda: 0, raising=False)
        monkeypatch.setattr(termios, "tcgetattr", lambda fd: [], raising=False)
        monkeypatch.setattr(termios, "tcsetattr", lambda fd, when, attrs: None, raising=False)

        def _boom(fd, when=None):
            raise termios.error("device not configured")
        monkeypatch.setattr(tty, "setcbreak", _boom)

        assert appearance.detect_terminal_background() is None

    def test_isatty_raising_does_not_raise(self, monkeypatch):
        # isatty() can raise ValueError on a closed stream.
        def _boom():
            raise ValueError("I/O operation on closed file")
        monkeypatch.setattr(sys.stdin, "isatty", _boom, raising=False)

        assert appearance.detect_terminal_background() is None


class TestDrainStdin:
    def test_isatty_raising_does_not_raise(self, monkeypatch):
        def _boom():
            raise ValueError("I/O operation on closed file")
        monkeypatch.setattr(sys.stdin, "isatty", _boom, raising=False)

        appearance.drain_stdin()  # must not raise


class TestCliThemeResolution:
    def test_resolve_skips_detection_when_colors_disabled(self, monkeypatch):
        # When colors are off, auto must resolve to dark WITHOUT probing.
        def _boom():
            raise AssertionError("must not probe when colors are off")
        # resolve_theme itself doesn't gate — the caller does. This asserts the
        # gating helper the CLI uses:
        assert appearance.cli_theme("auto", detect=_boom, colors=False) == "dark"

    def test_resolve_probes_when_colors_enabled(self):
        assert appearance.cli_theme("auto", detect=lambda: "light", colors=True) == "light"

    def test_explicit_never_probes(self):
        def _boom():
            raise AssertionError("explicit theme must not probe")
        assert appearance.cli_theme("light", detect=_boom, colors=True) == "light"


class TestCliShouldProbe:
    def test_run_subcommand_never_probes(self):
        # `run` execs a child that takes over the terminal.
        assert appearance.cli_should_probe(["run", "2"], colors_enabled=True) is False

    def test_json_flag_never_probes(self):
        # --json must stay machine-readable; the OSC query can't precede it.
        assert appearance.cli_should_probe(["list", "--json"], colors_enabled=True) is False

    def test_colors_disabled_never_probes(self):
        assert appearance.cli_should_probe(["list"], colors_enabled=False) is False

    def test_plain_command_with_colors_probes(self):
        assert appearance.cli_should_probe(["list"], colors_enabled=True) is True


def test_query_short_circuits_under_tmux(monkeypatch):
    """Inside tmux the OSC 11 probe is skipped (never waits out the timeout)."""
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1,0")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)

    def _boom():
        raise AssertionError("must not probe the tty under tmux")

    monkeypatch.setattr(sys.stdin, "fileno", _boom, raising=False)
    assert appearance._query_terminal_background() is None
