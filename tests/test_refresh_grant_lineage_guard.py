"""The POST-time bytes guard: no site may consume (POST) a refresh grant
whose bytes are not the slot's own.

Incident context (2026-09-07 owner order): a refresh grant is single-use at
the server — if any site POSTs slot X's grant bytes while believing it acts
for slot Y, the server burns X's grant and account X needs a fresh login
inside its 30-day window. ``oauth.try_refresh_oauth_credentials`` is the
single socket-level POST every in-tree caller reaches (``refresh_
oauth_credentials`` and both ``switcher.py`` sites route through it), so the
guard lives there once, as an optional ``condemned`` callback consulted
right before the POST: a CONFIRMED mismatch (a caller's own
``_probe_verdicts.get(_lineage_key(...)) is False``) refuses before any
network call; absence of evidence (no ``condemned``, or ``condemned``
answering False) never refuses — refusing a legitimate refresh is the exact
harm this exists to prevent (R1).

``TestPostCallSitesAreReviewed`` below DERIVES its subject from the AST
rather than naming it, per the project's own lesson that a hand-named list
goes blind the day a new caller is added without review: it walks EVERY
module under ``src/claude_swap`` for every call to
``try_refresh_oauth_credentials`` and asserts the derived (file, enclosing
function, whether it passes ``condemned=``) roster matches exactly what this
round reviewed. A new call site — guarded or not — changes the roster and
fails the test.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src" / "claude_swap"

TARGET = "try_refresh_oauth_credentials"


def _post_call_sites(path: Path) -> list[tuple[str | None, int, bool]]:
    """Every call to ``TARGET`` in ``path``: (enclosing function or None at
    module scope, line number, whether a ``condemned=`` keyword is passed).

    Name-only match on ``node.func`` (an ``Attribute`` for
    ``oauth.try_refresh_oauth_credentials``, a ``Name`` for the bare call
    inside ``oauth.py`` itself) — exactly ``TestBackendWritersHaveExactly
    OneCaller``'s own declared boundary in
    ``test_credential_attribution_guard.py``: a call reached through
    ``getattr``, a bound alias, or ``functools.partial`` is invisible to it,
    and the miss is only ever in that direction (a call this misses is a
    call still reaching the real function with no guard proven for it — the
    project's own control for that gap is the reviewed roster below staying
    small enough to read by eye).
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.func_stack: list[str] = []
            self.hits: list[tuple[str | None, int, bool]] = []

        def _visit_def(self, node):
            self.func_stack.append(node.name)
            self.generic_visit(node)
            self.func_stack.pop()

        visit_FunctionDef = _visit_def
        visit_AsyncFunctionDef = _visit_def

        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else (
                func.id if isinstance(func, ast.Name) else None
            )
            if name == TARGET:
                enclosing = self.func_stack[-1] if self.func_stack else None
                has_condemned = any(
                    kw.arg == "condemned" for kw in node.keywords
                )
                self.hits.append((enclosing, node.lineno, has_condemned))
            self.generic_visit(node)

    visitor = Visitor()
    visitor.visit(tree)
    return visitor.hits


# Reviewed 2026-09-08: every call into the POST chokepoint, derived from the
# AST, with whether it passes `condemned=`.
#
#   oauth.py::refresh_oauth_credentials — the thin `.credentials`-only
#   wrapper (no in-tree caller, tests only). No slot/account is in scope at
#   this layer to build a `condemned` check from; left unguarded and named
#   here rather than chased (R4 — do not widen).
#
#   oauth.py::try_fetch_usage_for_account — two calls (the pre-fetch and the
#   401-retry), both reached only when `not is_active and refresh_via is
#   None`; no in-tree caller passes that pair today (`switcher.py`'s sole
#   `is_active=False` caller passes `refresh_via=self.consume_backup_grant`,
#   which routes through the guarded chokepoint instead). Genuinely
#   unreachable residue, named rather than chased (R4).
#
#   switcher.py::_consume_backup_grant_locked — the consume gate's own POST.
#   Guarded: `condemned=` checks this slot's `_probe_verdicts` for the exact
#   re-read bytes about to be consumed.
#
#   switcher.py::_fetch_active_usage — the collector's recovery-branch POST
#   (the second call site the consume gate's own docstring names). Guarded
#   the same way.
EXPECTED_POST_SITE_ROSTER: dict[tuple[str, str | None], tuple[int, int]] = {
    # (file, enclosing function) -> (calls without condemned=, calls with)
    ("oauth.py", "refresh_oauth_credentials"): (1, 0),
    ("oauth.py", "try_fetch_usage_for_account"): (2, 0),
    ("switcher.py", "_consume_backup_grant_locked"): (0, 1),
    ("switcher.py", "_fetch_active_usage"): (0, 1),
}


class TestPostCallSitesAreReviewed:
    """Every caller of the refresh-grant POST chokepoint, derived from the
    AST rather than hand-listed, must match a roster this round actually
    reviewed for whether it needs (and passes) the ``condemned`` bytes
    guard. A new call site — guarded or not — changes the derived roster and
    fails this test."""

    def test_every_post_call_site_is_on_the_reviewed_roster(self):
        py_files = sorted(SRC.rglob("*.py"))
        assert len(py_files) > 3, "expected the whole package, not one file"
        derived: Counter[tuple[str, str | None]] = Counter()
        guarded: dict[tuple[str, str | None], int] = {}
        for path in py_files:
            for enclosing, _lineno, has_condemned in _post_call_sites(path):
                # The function's own `def` line is never a Call node, so
                # oauth.py's definition of TARGET never appears here.
                key = (path.name, enclosing)
                derived[key] += 1
                if has_condemned:
                    guarded[key] = guarded.get(key, 0) + 1

        assert derived, "expected at least the reviewed call sites"
        derived_roster = {
            key: (count - guarded.get(key, 0), guarded.get(key, 0))
            for key, count in derived.items()
        }
        assert derived_roster == EXPECTED_POST_SITE_ROSTER, (
            "the derived refresh-grant POST-site roster no longer matches "
            "what this round reviewed — a call site was added, removed, or "
            "its condemned= guard was added/dropped; review the new shape "
            "and update EXPECTED_POST_SITE_ROSTER above (an entry on "
            "neither side is unreviewed)"
        )
