"""The suite's own network isolation must actually hold: a lookup of a real
host is refused before it ever reaches a resolver.
"""
from __future__ import annotations

import os
import socket
import urllib.error
import urllib.request
from unittest.mock import patch

import pytest

from claude_swap import oauth
from tests.test_cli import _subprocess_env


def test_non_loopback_lookup_is_blocked_loopback_still_resolves():
    with pytest.raises(socket.gaierror, match="network guard"):
        socket.getaddrinfo("example.com", 443)

    # A numeric literal never needs a resolver, so on a host with no DNS this
    # one still succeeds unless the guard blocks it itself -- unlike the
    # hostname case above, which a broken resolver would fail on its own.
    with pytest.raises(socket.gaierror, match="network guard"):
        socket.getaddrinfo("192.0.2.1", 443)

    # Unblocked: a local test server can still bind to and be reached at
    # loopback.
    socket.getaddrinfo("127.0.0.1", 0)


def test_subprocess_env_proxies_a_spawned_cli_off_the_real_network(tmp_path):
    # HOME set explicitly: the session-scoped default in test_cli.py's own
    # `_isolated_subprocess_home` fixture is module-private and never runs
    # for a test outside that module.
    with patch.dict(os.environ, _subprocess_env(HOME=str(tmp_path)), clear=True):
        assert urllib.request.getproxies()["https"] == "http://127.0.0.1:1"
        assert not urllib.request.proxy_bypass("pypi.org")


def test_the_suite_never_reaches_the_vendors_usage_api(monkeypatch):
    # The getaddrinfo guard cannot see a loopback egress proxy: urllib then
    # resolves and connects to the proxy alone.
    lookups = []

    def refuse(*args, **kwargs):
        lookups.append(args)
        raise OSError("lookup refused")

    monkeypatch.setattr("socket.getaddrinfo", refuse)
    with pytest.raises(urllib.error.URLError):
        oauth.request_usage_data("sk-x")
    assert lookups == []
