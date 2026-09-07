"""Codex (ChatGPT) provider.

Manages ``~/.codex`` accounts the way the Claude side manages ``~/.claude``.
Semantics — endpoint contracts, registry schema, plan naming, grouped
account-name refresh — follow Loongphy/codex-auth; none of its code is reused
(it is Zig).
"""

from __future__ import annotations
