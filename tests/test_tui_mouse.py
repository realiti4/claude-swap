"""SSH launches must not enable terminal mouse reporting."""

import pytest

from claude_swap.tui import run


@pytest.mark.parametrize("start", ["dashboard", "watch"])
@pytest.mark.parametrize(
    "environment, expected_mouse",
    [
        ({}, True),
        ({"SSH_CONNECTION": "client 1234 server 22"}, False),
        ({"SSH_CLIENT": "client 1234 22"}, False),
        ({"SSH_CONNECTION": "", "SSH_CLIENT": ""}, True),
    ],
)
def test_mouse_reporting_at_launch(monkeypatch, start, environment, expected_mouse):
    for name in ("SSH_CONNECTION", "SSH_CLIENT"):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    launched = {}
    switcher = object()

    class FakeApp:
        return_code = 0

        def __init__(self, received_switcher, **kwargs):
            assert received_switcher is switcher
            assert kwargs["start"] == start

        def run(self, *, mouse):
            launched["mouse"] = mouse

    monkeypatch.setattr("claude_swap.tui.app.CswapApp", FakeApp)
    monkeypatch.setattr("claude_swap.appearance.detect_terminal_background", lambda: None)
    monkeypatch.setattr("claude_swap.appearance.drain_stdin", lambda: None)

    assert run(switcher, start=start) == 0
    assert launched["mouse"] is expected_mouse
