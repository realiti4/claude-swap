"""The CI shards must cover every test file, exactly once.

Sharding the Windows job splits the suite across parallel jobs by PATH, and a
path list in a workflow is a list that goes stale: add `tests/test_foo.py`,
name it in no shard, and it silently stops running in CI while the suite still
passes locally and every job stays green. That failure is invisible in exactly
the place a test suite is supposed to be loud.

So the split is data, read back out of the workflow and checked here. The
shards are parsed from the matrix rather than restated, so this cannot drift
from what CI actually runs the way a hand-copied list would.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_WORKFLOW = _ROOT / ".github" / "workflows" / "ci.yml"
_TESTS = _ROOT / "tests"


def _windows_shards() -> list[dict[str, str]]:
    """The matrix entries, parsed without PyYAML.

    PyYAML is not a project dependency, and `importorskip` would make this a
    guard that SKIPS — which is the same silent hole it exists to close, just
    one level up. The shape being read is two keys of a `- name:` list, so a
    small reader beats adding a dependency to the host project for one test.
    """
    text = _WORKFLOW.read_text(encoding="utf-8")
    block = re.search(
        r"\n  test-windows:.*?\n        include:\n(.*?)\n    name:", text, re.S
    )
    assert block, "the test-windows matrix is gone or was restructured"
    shards, current = [], None
    for raw in block.group(1).splitlines():
        line = raw.strip()
        if line.startswith("#") or not line:
            continue
        if line.startswith("- name:"):
            current = {"name": line.split(":", 1)[1].strip(), "paths": ""}
            shards.append(current)
        elif line.startswith("paths:"):
            rest = line.split(":", 1)[1].strip()
            current["paths"] = "" if rest == ">-" else rest
        elif current is not None:
            current["paths"] += " " + line
    assert shards, "no shards parsed out of the matrix"
    return shards


def _split(paths: str) -> tuple[set[str], set[str]]:
    """(files this shard RUNS by name, files it IGNORES)."""
    runs, ignores = set(), set()
    for word in paths.split():
        if word.startswith("--ignore="):
            ignores.add(word.split("=", 1)[1])
        else:
            runs.add(word)
    return runs, ignores


def test_every_test_file_runs_in_exactly_one_windows_shard():
    """Each test file must run in one shard — not zero, not two.

    Zero is a file that quietly left CI. Two is the same file paid for twice
    on the slowest platform, which is the cost this sharding exists to cut.
    """
    on_disk = {f"tests/{p.name}" for p in _TESTS.glob("test_*.py")}
    assert on_disk, "no test files found — the glob or the layout moved"

    runs_per_file: dict[str, list[str]] = {p: [] for p in on_disk}
    for shard in _windows_shards():
        named, ignored = _split(shard["paths"])
        stale = (named | ignored) - on_disk
        assert not stale, (
            f"shard {shard['name']!r} names {sorted(stale)}, which do not "
            f"exist — a rename left the workflow behind"
        )
        # A shard with no named paths is the catch-all: everything except
        # what it ignores.
        covered = named if named else on_disk - ignored
        for path in covered:
            runs_per_file[path].append(shard["name"])

    missing = sorted(p for p, s in runs_per_file.items() if not s)
    assert not missing, f"{missing} run in NO Windows shard — they left CI silently"

    doubled = sorted((p, s) for p, s in runs_per_file.items() if len(s) > 1)
    assert not doubled, f"these run in more than one Windows shard: {doubled}"


def test_the_macos_job_names_files_that_exist():
    """The macOS job runs two files by name; a rename must not silently
    reduce it to running nothing."""
    text = _WORKFLOW.read_text(encoding="utf-8")
    step = re.search(r"Run macOS Keychain tests.*?\n(?:.*\n)*?\s+run: (.*)", text)
    assert step, "the macOS keychain step is gone or was renamed"
    named = re.findall(r"(tests/test_\w+\.py)", step.group(1))
    assert named, "the macOS job stopped naming any test file"
    for path in named:
        assert (_ROOT / path).exists(), f"the macOS job names {path}, which does not exist"
