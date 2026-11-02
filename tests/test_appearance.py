"""Tests for terminal-background detection and theme resolution."""
from __future__ import annotations

import sys

import pytest

from claude_swap import appearance


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

    def test_unterminated_rgb_is_rejected(self):
        assert appearance._parse_osc11(b"\x1b]11;rgb:ffff/ffff/ffff") is None

    def test_unframed_hex_is_rejected(self):
        assert appearance._parse_osc11(b"#ff8000") is None

    def test_framed_hex_parses(self):
        r, _, b = appearance._parse_osc11(b"\x1b]11;#ff8000\x07")
        assert r == pytest.approx(1.0) and b == pytest.approx(0.0)

    def test_unterminated_hex_is_rejected(self):
        assert appearance._parse_osc11(b"\x1b]11;#ff8000") is None


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


class TestQueryTerminalBackground:
    @staticmethod
    def _fake_tty(monkeypatch, chunks, *, clock=None):
        termios = pytest.importorskip("termios")
        import tty

        fd = 42
        pending = list(chunks)
        reads = []
        writes = []
        select_timeouts = []

        class FakeStdin:
            @staticmethod
            def isatty():
                return True

            @staticmethod
            def fileno():
                return fd

        class FakeStdout:
            @staticmethod
            def isatty():
                return True

            @staticmethod
            def write(value):
                writes.append(value)
                return len(value)

            @staticmethod
            def flush():
                return None

        def fake_select(readers, _writers, _errors, timeout):
            assert readers == [fd]
            select_timeouts.append(timeout)
            return ([fd], [], []) if pending else ([], [], [])

        def fake_read(read_fd, size):
            assert read_fd == fd
            assert size == 32
            chunk = pending.pop(0)
            reads.append(chunk)
            return chunk

        monkeypatch.setenv("TERM", "xterm-256color")
        monkeypatch.delenv("TMUX", raising=False)
        monkeypatch.delenv("STY", raising=False)
        monkeypatch.setattr(sys, "stdin", FakeStdin())
        monkeypatch.setattr(sys, "stdout", FakeStdout())
        monkeypatch.setattr(termios, "tcgetattr", lambda read_fd: ["old"])
        monkeypatch.setattr(termios, "tcsetattr", lambda *args: None)
        monkeypatch.setattr(tty, "setcbreak", lambda *args: None)
        monkeypatch.setattr(appearance.select, "select", fake_select)
        monkeypatch.setattr(appearance.os, "read", fake_read)
        if clock is not None:
            monkeypatch.setattr(appearance.time, "monotonic", clock)

        return reads, writes, select_timeouts

    def test_waits_for_da1_after_complete_osc_reply(self, monkeypatch):
        osc = b"\x1b]11;rgb:1e1d/1e1d/1e1d\x07"
        da1 = b"\x1b[?1;2c"
        reads, writes, _ = self._fake_tty(monkeypatch, [osc, da1])

        assert appearance._query_terminal_background() == osc + da1
        assert reads == [osc, da1]
        assert writes == [
            (appearance._QUERY + appearance._DA1_QUERY).decode("latin-1")
        ]

    def test_accepts_reply_delayed_beyond_old_150ms_window(self, monkeypatch):
        reply = b"\x1b]11;rgb:ffff/ffff/ffff\x07\x1b[?1;2c"
        times = iter((0.0, 0.2, 0.2))
        _, _, select_timeouts = self._fake_tty(
            monkeypatch, [reply], clock=lambda: next(times)
        )

        assert appearance._query_terminal_background() == reply
        assert select_timeouts == [pytest.approx(0.8)]

    def test_da1_first_means_osc11_is_unsupported(self, monkeypatch):
        da1 = b"\x1b[?62;1;2;6c"
        reads, _, _ = self._fake_tty(monkeypatch, [da1])

        reply = appearance._query_terminal_background()

        assert reads == [da1]
        assert appearance._classify(reply) is None

    def test_accepts_da1_reply_without_parameters(self, monkeypatch):
        da1 = b"\x1b[?c"
        reads, _, _ = self._fake_tty(monkeypatch, [da1])

        assert appearance._query_terminal_background() == da1
        assert reads == [da1]

    def test_does_not_mistake_echoed_da1_query_for_reply(self, monkeypatch):
        echoed_query = appearance._DA1_QUERY
        osc = b"\x1b]11;rgb:0000/0000/0000\x07"
        da1 = b"\x1b[?1;2c"
        reads, _, _ = self._fake_tty(monkeypatch, [echoed_query, osc, da1])

        assert appearance._query_terminal_background() == echoed_query + osc + da1
        assert reads == [echoed_query, osc, da1]

    def test_accepts_fragmented_da1_reply(self, monkeypatch):
        osc = b"\x1b]11;rgb:0000/0000/0000\x07"
        fragments = [osc, b"\x1b[?", b"62;1;2;6c"]
        reads, _, _ = self._fake_tty(monkeypatch, fragments)

        assert appearance._query_terminal_background() == b"".join(fragments)
        assert reads == fragments


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


@pytest.mark.parametrize("term", ["dumb", "linux"])
def test_query_short_circuits_on_known_unsupported_terminals(monkeypatch, term):
    """Known unsupported terminals must not receive escape-sequence probes."""
    monkeypatch.setenv("TERM", term)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)

    def _boom():
        raise AssertionError("must not probe a known unsupported terminal")

    monkeypatch.setattr(sys.stdin, "fileno", _boom, raising=False)
    assert appearance._query_terminal_background() is None
