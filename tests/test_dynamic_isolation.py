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
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from tests._digest_harness import _run_trace

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
        every one of the four digests above. Inverts every window's
        utilization (``100 - pct``) at the one source both the headroom and
        the per-window switch lines read — flips who looks healthy vs.
        blocked, which a single anti-flap constant does not reliably do when
        a fixed fleet only ever switches once."""
        from claude_swap import oauth

        real_windows = oauth.relevant_windows
        monkeypatch.setattr(
            oauth,
            "relevant_windows",
            lambda usage, models=(): [
                (label, 100.0 - pct, resets_at)
                for label, pct, resets_at in real_windows(usage, models)
            ],
        )
        for i, ((strategy, model), golden) in enumerate(_GOLDEN.items()):
            got = _digest(tmp_path / f"m{i}", strategy, model, seed=_SEED)
            assert got != golden, (
                f"mutant control did not move {strategy}/model={model or '(none)'}"
            )


# Every (strategy, model, seed) the cross-revision tests below read. One
# driver run answers them all: the child's fixed cost (interpreter and the
# base's `claude_swap` import, 0.13 s) is paid once, not once per test.
_BASE_REV_JOBS = (
    *((strategy, model, _SEED) for strategy, model in _GOLDEN),
    ("dynamic", "Fable", _DYNAMIC_SEED),
    ("best", "Fable", _DYNAMIC_SEED),
    ("consume-first", "Fable", _DYNAMIC_SEED),
)


@pytest.fixture(scope="module")
def base_rev_digests(tmp_path_factory):
    """`_BASE_REV`'s digests for `_BASE_REV_JOBS`, once per worker."""
    return TestDynamicLeavesTheBaseRevisionAlone._digests_at_base_rev(
        tmp_path_factory.mktemp("base_rev"), _BASE_REV_JOBS
    )


# One xdist worker runs the group, so `base_rev_digests` is built once.
@pytest.mark.xdist_group("base-rev-digests")
class TestDynamicLeavesTheBaseRevisionAlone:
    """The actual adr/0009 measurement: golden values above pin FUTURE
    drift on this branch, which is not the same claim as "this round left
    `best`/`consume-first` alone". That claim needs a SECOND revision to
    compare against — `_BASE_REV`, this PR's own base before DRAIN STATE.
    `src/claude_swap` is archived from that revision into a scratch tree and
    HEAD's `tests/_digest_harness.py` (stdlib and `claude_swap` only: that
    revision's own 12.7k-line test module cost 0.43 s of the 0.66 s child to
    compile) drives it in a subprocess (a different `claude_swap` package must
    not share `sys.modules` with this process).

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
    def _digests_at_base_rev(tmp_path, jobs):
        """`{"strategy|model|seed": digest}` for each `(strategy, model,
        seed)` job, computed against `_BASE_REV` in ONE subprocess (a
        different `claude_swap` package must not share `sys.modules` with
        this process). The `base_rev_digests` fixture runs it once per
        worker for every job the tests below need, so each test states only
        WHICH job it reads and what it expects of it.
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
            ["git", "-C", str(repo_root), "archive", "--format=tar", _BASE_REV, "src"],
            capture_output=True, check=True,
        )
        with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tf:
            tf.extractall(old_root, filter="data")  # trusted: our own repo's history
        jobs_literal = json.dumps(list(jobs))
        driver = (
            "import hashlib, json, os, sys\n"
            "from pathlib import Path\n"
            f"sys.path.insert(0, {str(old_root / 'src')!r})\n"
            f"sys.path.insert(1, {str(repo_root)!r})\n"  # HEAD's tests/_digest_harness.py
            "import claude_swap, claude_swap.oauth\n"
            # conftest's hermetic stubs do not reach this interpreter.
            "claude_swap.oauth.fetch_oauth_profile = lambda token: None\n"
            "os.fsync = lambda fd: None\n"
            "from tests._digest_harness import _run_trace\n"
            "out = {'_claude_swap_file': claude_swap.__file__}\n"
            f"for i, (strategy, model, seed) in enumerate({jobs_literal}):\n"
            "    trace = _run_trace(Path(sys.argv[1]) / f'b{i}', strategy, model, seed)\n"
            "    out[f'{strategy}|{model}|{seed}'] = hashlib.sha256("
            "json.dumps(trace, sort_keys=True).encode()).hexdigest()\n"
            "print(json.dumps(out))\n"
        )
        driver_path = tmp_path / "_zz_cross_rev_driver.py"
        driver_path.write_text(driver)
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        # The child is outside this test process, so it gets a dead proxy
        # instead of ambient network access (measured: 7 non-loopback
        # lookups to api.anthropic.com — no test here needs a real answer).
        dead_proxy = "http://127.0.0.1:1"
        child_env = {
            **os.environ,
            "http_proxy": dead_proxy, "https_proxy": dead_proxy,
            "HTTP_PROXY": dead_proxy, "HTTPS_PROXY": dead_proxy,
            "no_proxy": "", "NO_PROXY": "",
        }
        result = subprocess.run(
            [sys.executable, str(driver_path), str(runs_dir)],
            capture_output=True, text=True, cwd=str(old_root), env=child_env,
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

    def test_the_driver_subprocess_gets_a_dead_proxy_env(self, tmp_path, monkeypatch):
        """The driver runs outside this process, so it must not reach the
        real network on the parent's env (measured: 7 non-loopback
        `getaddrinfo` lookups to api.anthropic.com from this class's tests
        at 8fd7dc6f with `*_proxy` unset). No test here needs a real
        answer.

        The `sys.executable` call is intercepted and answered without
        actually running a child interpreter — only the six proxy keys are
        recorded and checked, never the parent's whole environment (a
        failure here must not publish an ambient token); `git` calls still
        pass through to the real run."""
        real_run = subprocess.run
        seen = {}
        dead_proxy = "http://127.0.0.1:1"
        expected = {
            "http_proxy": dead_proxy, "https_proxy": dead_proxy,
            "HTTP_PROXY": dead_proxy, "HTTPS_PROXY": dead_proxy,
            "no_proxy": "", "NO_PROXY": "",
        }

        def spy(cmd, *args, **kwargs):
            if cmd[0] == sys.executable:
                seen["env"] = kwargs.get("env")
                stub = {
                    "_claude_swap_file": str(
                        tmp_path / "base" / "src" / "claude_swap" / "__init__.py"
                    )
                }
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=json.dumps(stub), stderr=""
                )
            return real_run(cmd, *args, **kwargs)

        monkeypatch.setattr(subprocess, "run", spy)
        self._digests_at_base_rev(tmp_path, [("best", "", _SEED)])
        env = seen.get("env") or {}
        got = {k: env.get(k) for k in expected}
        assert got == expected, (
            f"driver env's proxy keys are {got!r}, not the dead-proxy six {expected!r}"
        )

    def test_e9afe401_produces_the_same_four_digests(self, base_rev_digests):
        for (strategy, model), golden in _GOLDEN.items():
            got = base_rev_digests[f"{strategy}|{model}|{_SEED}"]
            assert got == golden, (
                f"{strategy}/model={model or '(none)'}: {_BASE_REV} gives "
                f"{got}, HEAD gives {golden} — this round moved "
                "behaviour this strategy never authorized"
            )

    def test_dynamic_actually_moved_from_the_base_revision(
        self, tmp_path, base_rev_digests
    ):
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
        base_digest = base_rev_digests[f"dynamic|Fable|{_DYNAMIC_SEED}"]
        head_digest = _digest(tmp_path / "head", "dynamic", "Fable", _DYNAMIC_SEED)
        assert base_digest != head_digest, (
            f"got the same digest ({head_digest}) on both {_BASE_REV} and "
            "HEAD for dynamic/Fable — the fleet never reached the new "
            "code, and this whole module proves nothing about it"
        )

    def test_the_discriminating_seed_still_leaves_best_and_consume_first_alone(
        self, tmp_path, base_rev_digests
    ):
        """`_DYNAMIC_SEED` was picked because its fleet reaches dynamic's
        new code (the test above) — that alone does not say `best`/
        `consume-first` are still untouched ON THIS SAME fleet, only that
        `_SEED`'s fleet says so on a fleet proven NOT to reach the new
        code at all. Same population `_DYNAMIC_SEED` exercises, both
        strategies, still byte-identical to `_BASE_REV`."""
        for i, (s, m) in enumerate([("best", "Fable"), ("consume-first", "Fable")]):
            got = base_rev_digests[f"{s}|{m}|{_DYNAMIC_SEED}"]
            assert got == _digest(tmp_path / f"h{i}", s, m, _DYNAMIC_SEED), (
                f"{s}/{m}: digest moved between {_BASE_REV} and HEAD on "
                "_DYNAMIC_SEED's own fleet"
            )
