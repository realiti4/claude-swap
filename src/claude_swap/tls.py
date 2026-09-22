"""TLS trust configuration shared by every path that opens a connection."""

from __future__ import annotations


def use_native_tls() -> None:
    """Route TLS trust decisions through the OS-native verifier.

    Claude's token endpoint (``platform.claude.com``) serves a Let's Encrypt
    chain. Python's stdlib ``ssl`` uses OpenSSL, which on Windows loads the
    system cert store as a flat set and matches CA certs by *subject name*, so a
    stale, expired duplicate of an intermediate (e.g. an old ``ISRG Root X2``
    left in the user's store) can shadow the valid path and fail verification
    with "certificate has expired" even though the served chain is valid — which
    silently breaks inactive-account token refresh. The OS-native verifiers
    (SChannel on Windows, SecureTransport on macOS) build the chain correctly
    and don't trip on the expired duplicate — the same reason Claude Code (Node,
    with its own bundled roots) is unaffected. ``truststore`` delegates to them.

    Best-effort: on any failure fall back to stdlib ``ssl`` rather than block
    the caller over a TLS-trust nicety.

    Lives here rather than in the CLI because it is not a CLI concern: the
    managed-session prompt hook installs it too, on the one side of its gate
    that can reach the network, and a per-prompt hook reaching into
    ``claude_swap.cli`` for six lines would invert the layering.
    """
    try:
        import truststore

        truststore.inject_into_ssl()
    except Exception:
        pass
