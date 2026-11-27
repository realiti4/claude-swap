"""A module or class must not define the same name twice — the later
definition silently wins and no behavioural test can see the shadowed twin.

Its own file, not `test_pin.py`, for the reason `test_real_store_guard.py`
has one: this is a package-wide STRUCTURAL invariant over every module under
`claude_swap/`, and it happened to be written while fixing a duplicate in
`pin.py`. Filed under the module that prompted it, the next person greps
`test_pin.py` for pin behaviour and finds an AST walk of the whole package.
"""
from __future__ import annotations


import ast
import collections
import pathlib


def _repeatable(node: ast.AST) -> bool:
    """Decorators under which repeating a name is the POINT, not a mistake.

    `@overload` declares one signature per `def` and the last one is the
    implementation; `@property` + `@x.setter` are one attribute in two halves.
    Read off the decorators rather than by naming the pairs the package
    happens to have today — the guard exists precisely because tomorrow's
    duplicate is one nobody listed.
    """
    for dec in getattr(node, "decorator_list", ()):
        text = ast.unparse(dec)
        if text in ("overload", "typing.overload", "property"):
            return True
        if text.endswith((".setter", ".deleter", ".getter")):
            return True
    return False


def duplicate_names(source: str, where: str = "") -> list[str]:
    """Names defined twice in one module body, or twice in one class body.

    THE one implementation: the package-wide guards below run it over every
    module in `claude_swap/`, and the control tests run it over synthetic
    source. A second copy written for the tests is how a guard drifts from
    what it claims to check.
    """
    tree = ast.parse(source)
    prefix = f"{where}: " if where else ""
    offenders: list[str] = []

    counts = collections.Counter(
        node.name for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and not _repeatable(node)
    )
    offenders += [f"{prefix}{n} x{c}" for n, c in counts.items() if c > 1]

    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        counts = collections.Counter(
            m.name for m in cls.body
            if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
            and not _repeatable(m)
        )
        offenders += [
            f"{prefix}{cls.name}.{n} x{c}" for n, c in counts.items() if c > 1
        ]
    return offenders


class TestNoModuleDefinesTheSameNameTwice:
    """A merge can leave two `def clear_pin` in one module and no test notices.

    Measured on this branch: #210 rewrote `clear_pin` from `-> bool` to
    `-> tuple[bool, str]`, this branch still carried the old one, and the
    integration merge kept BOTH — the later definition silently won, so every
    test passed and the shadowed one sat there as a trap. It is a trap because
    which definition wins is decided by ORDER, and order is decided by whichever
    side of the next merge git happens to emit first: flip it and callers doing
    `ok, msg = clear_pin(...)` get a bool to unpack.

    So this asserts the file, not the behaviour. A behavioural test cannot see
    the shadowed twin at all — it calls the winner, which is correct.

    Whole package, not just `pin.py`: the same merge shape produced a duplicate
    in `dashboard.py` on a different day, and naming one module is how the next
    one is missed. The class-level half is not redundant with the module-level
    one — only it sees a method defined twice inside one class.
    """

    def test_no_name_is_defined_twice_in_any_module_or_class(self):
        """`tests/` TOO, and that is where it actually caught one.

        Scanning `claude_swap/` alone left the suite unguarded against its own
        shape of this bug: two same-named methods landed in one test class,
        pytest collected ONE of them, and the one it dropped was the stronger
        — it drove the real `switch_to` end to end while the survivor
        hand-wrote the state. A guard that cannot see the file it lives in is
        the failure mode it exists to name.
        """
        import claude_swap

        roots = [pathlib.Path(claude_swap.__file__).parent,
                 pathlib.Path(__file__).parent]
        # THE DENOMINATOR, PER ROOT. A clean sweep over an empty set reads
        # exactly like a clean sweep: `roots = []` passes this case with
        # nothing examined, and so does one root that has moved while the
        # other still fills the total. Counted per root for that reason, and
        # never by naming a file -- `pin.py` exists on some branches and not
        # others, so a name list turns a branch difference into a failure.
        # The matcher itself has its own controls in the class below.
        per_root = {root: sorted(root.rglob("*.py")) for root in roots}
        thin = [str(root) for root, files in per_root.items() if len(files) < 5]
        assert roots and not thin, (
            f"these roots contributed almost nothing: {thin} — this guard "
            "proves nothing about a tree it never read"
        )
        offenders = [
            o
            for root in roots
            for path in sorted(root.rglob("*.py"))
            for o in duplicate_names(
                path.read_text(encoding="utf-8"), str(path.relative_to(root.parent))
            )
        ]
        assert not offenders, (
            "a name is defined more than once in one module body or one class "
            "body — the later definition silently wins, and which one that is "
            "depends on merge order:\n  " + "\n  ".join(offenders)
        )


class TestTheGuardOnlyFiresOnRealDuplicates:
    """The guard's own controls, on synthetic source rather than `src/`.

    `@typing.overload` legitimately repeats a name at MODULE level — that is
    the whole point of the decorator, the last `def` is the implementation and
    the earlier ones are signatures. The module-level half counted them and
    would have failed the suite the day someone typed the first overload in
    `claude_swap/` (measured: none there today, so this was latent). The
    class-level half already got this right by reading decorators, which is
    why it excluded `@property`/`.setter` without naming the pairs it knew of.
    """

    OVERLOADS = """
from typing import overload
import typing

@overload
def read(p: str) -> str: ...
@typing.overload
def read(p: bytes) -> bytes: ...
def read(p): return p

class C:
    @overload
    def get(self, k: str) -> str: ...
    @overload
    def get(self, k: int) -> int: ...
    def get(self, k): return k

class D:
    @property
    def x(self): return self._x
    @x.setter
    def x(self, v): self._x = v
"""

    REAL_DUPLICATE = """
def clear_pin(sw) -> bool: ...
def clear_pin(sw) -> tuple[bool, str]: ...

class C:
    def heal(self): ...
    def heal(self): ...
"""

    def test_overload_and_accessor_groups_are_not_duplicates(self):
        assert duplicate_names(self.OVERLOADS) == [], (
            "a legitimately repeated name counted as a redefinition — the "
            "guard would fail the suite on the first overload in the package"
        )

    def test_CONTROL_a_real_duplicate_still_trips_the_guard(self):
        """The control: exempting `@overload` must not exempt everything.
        Both shapes the guard was written for, undecorated."""
        assert duplicate_names(self.REAL_DUPLICATE) == [
            "clear_pin x2", "C.heal x2"
        ]
