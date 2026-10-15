"""Regression guard for adr/0009: `dynamic`'s own admission bar must not move
`best` or `consume-first` at all — every behaviour it adds is gated on
`settings.strategy == "dynamic"`. Hashes a fixed-seed fleet across 8 real
`engine.tick()`s, per strategy and with/without a pinned model, and pins the
digest; a mutant control proves the hash is sensitive rather than a no-op.
Not a simulator — a small, deterministic fixture with a fixed answer.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import random
import subprocess
import sys
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.test_autoswitch import EngineHarness, _iso_at

# This PR's own base — before DRAIN STATE — for the cross-revision check
# below. Not "integration"/trunk; this branch's history.
_BASE_REV = "e9afe401"

# Captured against `_BASE_REV` (e9afe401), not self-referentially against
# this branch: `TestDynamicLeavesTheBaseRevisionAlone` re-derives these
# same four digests from that revision's own `autoswitch.py`, which is the
# actual adr/0009 measurement. Seed picked so `#1` starts ABOVE `threshold`
# — below it, `best`/`consume-first` never reach `_rank_candidates` at all
# and 8x NO_ACTION would pin nothing; `test_traces_actually_rank_not_just_
# hold` guards against that regressing silently.
_SEED = 2
_GOLDEN = {
    ("best", ""): "0ba84e943f68f81a84f95aea6437eb497d65ad6da9e1494b4e5cd51c3e540b46",
    ("best", "Fable"): "0a1c94aba28954d3f487cd320ca78f7dbc81584ebcfb01acad1332b6cccf64e5",
    ("consume-first", ""): "4a4bc2fffb074934516f616dd246906b98960da4e06b8550cfaceaf0af8751d7",
    ("consume-first", "Fable"): "4a4bc2fffb074934516f616dd246906b98960da4e06b8550cfaceaf0af8751d7",
}

# For `test_dynamic_actually_moved_from_the_base_revision` only: `_SEED`'s
# fleet does NOT reach the drain tier differently between `_BASE_REV` and
# HEAD (measured: identical digest both sides), which would make that
# control vacuous. Found by sweeping seeds 1-59 for one whose dynamic/Fable
# trace actually differs.
_DYNAMIC_SEED = 5


def _random_fleet(seed: int, now: float, n: int = 5) -> dict:
    rng = random.Random(seed)
    fleet = {}
    for i in range(1, n + 1):
        fleet[str(i)] = {
            "five_hour": {"pct": rng.uniform(0, 100)},
            "seven_day": {
                "pct": rng.uniform(0, 100),
                "resets_at": _iso_at(now + rng.uniform(3600, 30 * 86400)),
            },
            "scoped": [{"name": "Fable", "pct": rng.uniform(0, 100)}],
        }
    return fleet


def _run_trace(home, strategy: str, model: str, seed: int) -> list:
    """One fresh ``home`` per call — a shared root across combos leaves the
    prior combo's switched-to account and backups behind, aliasing the next
    one's decisions onto stale state (measured: reusing one ``temp_home``
    across the four combos below moved a digest that a fresh root does not).

    ``Path.home()`` stays patched for the whole call, not only construction:
    ``make_live()`` and ``tick()`` both resolve the active account through
    ``paths.*``, which reads ``Path.home()`` live — patched only around
    ``EngineHarness.__init__`` (as the conftest ``temp_home`` fixture does
    NOT do here) reads every tick's active account off the real ambient
    home instead, and every tick reads `no-active-account` (measured).
    """
    home.mkdir()
    (home / ".claude").mkdir()
    with (
        patch("pathlib.Path.home", return_value=home),
        patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)}),
    ):
        h = EngineHarness(home, model=model, threshold=90.0, strategy=strategy)
        fleet = _random_fleet(seed, h.clock.now)
        for num in fleet:
            h.seed(int(num), f"acc{num}@example.com")
        h.make_live("acc1@example.com", 1)
        trace = []
        for _ in range(8):
            n0 = len(h.events)
            outcome = h.tick_with_usage(fleet)
            events = [(e.kind, getattr(e, "reason", None)) for e in h.events[n0:]]
            trace.append([outcome.name, h.active_number(), events])
            h.clock.advance(300.0)
    return trace


def _digest(home, strategy: str, model: str, seed: int) -> str:
    trace = _run_trace(home, strategy, model, seed)
    return hashlib.sha256(json.dumps(trace, sort_keys=True).encode()).hexdigest()


class TestDynamicNeverMovesBestOrConsumeFirst:
    def test_outcome_digests_are_pinned(self, tmp_path):
        for i, ((strategy, model), golden) in enumerate(_GOLDEN.items()):
            got = _digest(tmp_path / f"h{i}", strategy, model, seed=_SEED)
            assert got == golden, (
                f"{strategy}/model={model or '(none)'}: digest moved to "
                f"{got} — dynamic's admission bar must not reach this "
                "strategy at all"
            )

    def test_traces_actually_rank_not_just_hold(self, tmp_path):
        """A digest pin over an all-``NO_ACTION`` trace proves the fleet
        never entered `_rank_candidates` at all — identical `best`/
        `consume-first` digests for model on/off would most likely mean
        this, not that dynamic correctly left them alone (with 5 accounts
        and a uniform Fable pct, the model window binds ~99.6% of draws).
        The seed is chosen so `#1` starts ABOVE `threshold`; this pins that
        choice so a future reseed cannot regress back to a vacuous guard
        silently."""
        for i, (strategy, model) in enumerate(_GOLDEN):
            trace = _run_trace(tmp_path / f"t{i}", strategy, model, seed=_SEED)
            outcomes = {tick[0] for tick in trace}
            assert outcomes != {"NO_ACTION"}, (
                f"{strategy}/model={model or '(none)'}: every tick was "
                f"NO_ACTION ({trace}) — the fleet never reached the "
                "ranking path this guard exists to protect"
            )

    def test_mutant_control_moves_every_digest(self, tmp_path, monkeypatch):
        """Not a vacuous pin: a real change to shared ranking code must move
        every one of the four digests above. Inverts every account's
        headroom (``100 - h``) — flips who looks healthy vs. blocked, which
        a single anti-flap constant does not reliably do when a fixed fleet
        only ever switches once."""
        from claude_swap import oauth

        real_headroom = oauth.account_headroom
        monkeypatch.setattr(
            oauth,
            "account_headroom",
            lambda usage, models: (
                None if (h := real_headroom(usage, models)) is None else 100.0 - h
            ),
        )
        for i, ((strategy, model), golden) in enumerate(_GOLDEN.items()):
            got = _digest(tmp_path / f"m{i}", strategy, model, seed=_SEED)
            assert got != golden, (
                f"mutant control did not move {strategy}/model={model or '(none)'}"
            )


class TestDynamicLeavesTheBaseRevisionAlone:
    """The actual adr/0009 measurement: golden values above pin FUTURE
    drift on this branch, which is not the same claim as "this round left
    `best`/`consume-first` alone". That claim needs a SECOND revision to
    compare against — `_BASE_REV`, this PR's own base before DRAIN STATE.
    `tests/test_autoswitch.py` (the `EngineHarness` this module imports)
    and `src/claude_swap` are archived from that revision into a scratch
    tree and run there, unchanged, in a subprocess (a different
    `claude_swap` package must not share `sys.modules` with this process).

    SKIPPED, not failed, when `_BASE_REV` is not in the object database —
    CI checks out at `refs/pull/N/merge` with `fetch-depth: 1` (no history,
    depth 1), and this branch's own base commit is never IN that checkout at
    all. `git archive` under that condition is a `CalledProcessError`, which
    would redden all three CI jobs today and every run forever once the PR
    branch is deleted post-merge — measured against `.github/workflows/
    ci.yml`, not assumed. `tarfile` (stdlib), not the `tar` binary: the
    Windows job has no shell-out to it.
    """

    @staticmethod
    def _digests_at_base_rev(tmp_path, combos, seed):
        """`{"strategy|model": digest}` computed against `_BASE_REV`, in a
        subprocess (a different `claude_swap` package must not share
        `sys.modules` with this process). Shared by every test in this
        class so each one states only WHICH combos it needs and what it
        expects of them.
        """
        repo_root = Path(__file__).resolve().parents[1]
        probe = subprocess.run(
            ["git", "-C", str(repo_root), "cat-file", "-e", f"{_BASE_REV}^{{commit}}"],
        )
        if probe.returncode != 0:
            pytest.skip(f"{_BASE_REV} is not in this checkout's object database")
        old_root = tmp_path / "base"
        old_root.mkdir()
        archive = subprocess.run(
            ["git", "-C", str(repo_root), "archive", "--format=tar", _BASE_REV, "src", "tests"],
            capture_output=True, check=True,
        )
        with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tf:
            tf.extractall(old_root, filter="data")  # trusted: our own repo's history
        combos_literal = json.dumps(list(combos))
        driver = (
            "import sys, json\n"
            f"sys.path.insert(0, {str(old_root)!r})\n"
            f"sys.path.insert(0, {str(old_root / 'src')!r})\n"
            f"sys.path.insert(0, {str(tmp_path)!r})\n"  # this module's own dir, for _run_trace
            "from test_dynamic_isolation import _run_trace\n"
            "from pathlib import Path\n"
            "import hashlib, json as _json\n"
            "import claude_swap\n"
            "out = {'_claude_swap_file': claude_swap.__file__}\n"
            f"for i, (strategy, model) in enumerate({combos_literal}):\n"
            "    trace = _run_trace(Path(sys.argv[1]) / f'b{i}', strategy, model, int(sys.argv[2]))\n"
            "    out[f'{strategy}|{model}'] = hashlib.sha256("
            "_json.dumps(trace, sort_keys=True).encode()).hexdigest()\n"
            "print(_json.dumps(out))\n"
        )
        (tmp_path / "test_dynamic_isolation.py").write_text(
            Path(__file__).read_text(encoding="utf-8"), encoding="utf-8"
        )
        driver_path = tmp_path / "_zz_cross_rev_driver.py"
        driver_path.write_text(driver)
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        result = subprocess.run(
            [sys.executable, str(driver_path), str(runs_dir), str(seed)],
            capture_output=True, text=True, cwd=str(old_root),
        )
        assert result.returncode == 0, (
            f"driver failed: rc={result.returncode}\nSTDOUT={result.stdout}\n"
            f"STDERR={result.stderr}"
        )
        got = json.loads(result.stdout.strip().splitlines()[-1])
        # POSITIVE CONTROL: today the editable install is a plain `.pth`
        # path line, so inserting `old_root/src` first on `sys.path` is
        # what makes the import resolve to the archived tree — a
        # setuptools `__editable___*_finder` backend would silently
        # resolve `claude_swap` to the working tree regardless, and this
        # test would compare HEAD against HEAD and pass for the wrong
        # reason. Assert the imported module actually came from `old_root`.
        assert got.pop("_claude_swap_file").startswith(str(old_root)), (
            f"the subprocess imported claude_swap from outside {old_root} — "
            "this test compared HEAD against itself, not against "
            f"{_BASE_REV}"
        )
        return got

    def test_e9afe401_produces_the_same_four_digests(self, tmp_path):
        got = self._digests_at_base_rev(tmp_path, _GOLDEN.keys(), _SEED)
        for (strategy, model), golden in _GOLDEN.items():
            key = f"{strategy}|{model}"
            assert got[key] == golden, (
                f"{strategy}/model={model or '(none)'}: {_BASE_REV} gives "
                f"{got[key]}, HEAD gives {golden} — this round moved "
                "behaviour this strategy never authorized"
            )

    def test_dynamic_actually_moved_from_the_base_revision(self, tmp_path):
        """The four `best`/`consume-first` combos above passing is equally
        consistent with "dynamic's bar correctly reaches neither strategy"
        and with "the seed-2 fleet never reaches any line this branch
        changed" — a negative guaranteed by construction either way, and
        the mutant control (`test_mutant_control_moves_every_digest`)
        cannot close it either: it inverts `oauth.account_headroom`, which
        every strategy reads every tick, so it proves sensitivity to
        ranking inputs in general, not that the drain tier is ever
        entered. This is the control: `("dynamic", "Fable")` must DIFFER
        between `_BASE_REV` and HEAD, on `_DYNAMIC_SEED`'s fleet — proving
        the fixture actually reaches the new code at all. `_SEED` itself
        was tried first and measured NOT to discriminate here (identical
        digest both sides) — reported rather than adjusting the
        assertion; `_DYNAMIC_SEED` was found by sweeping for one that
        does."""
        got = self._digests_at_base_rev(
            tmp_path, [("dynamic", "Fable")], _DYNAMIC_SEED
        )
        base_digest = got["dynamic|Fable"]
        head_digest = _digest(tmp_path / "head", "dynamic", "Fable", _DYNAMIC_SEED)
        assert base_digest != head_digest, (
            f"got the same digest ({head_digest}) on both {_BASE_REV} and "
            "HEAD for dynamic/Fable — the fleet never reached the new "
            "code, and this whole module proves nothing about it"
        )

    def test_the_discriminating_seed_still_leaves_best_and_consume_first_alone(
        self, tmp_path
    ):
        """`_DYNAMIC_SEED` was picked because its fleet reaches dynamic's
        new code (the test above) — that alone does not say `best`/
        `consume-first` are still untouched ON THIS SAME fleet, only that
        `_SEED`'s fleet says so on a fleet proven NOT to reach the new
        code at all. Same population `_DYNAMIC_SEED` exercises, both
        strategies, still byte-identical to `_BASE_REV`."""
        combos = [("best", "Fable"), ("consume-first", "Fable")]
        got = self._digests_at_base_rev(tmp_path, combos, _DYNAMIC_SEED)
        for i, (s, m) in enumerate(combos):
            key = f"{s}|{m}"
            assert got[key] == _digest(tmp_path / f"h{i}", s, m, _DYNAMIC_SEED), (
                f"{s}/{m}: digest moved between {_BASE_REV} and HEAD on "
                "_DYNAMIC_SEED's own fleet"
            )
