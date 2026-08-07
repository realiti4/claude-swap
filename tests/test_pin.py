"""The pin is an optional extra: cswap must work fully without it.

Mirrors how menubar is treated — the module is import-safe without the
dependency, and the missing extra surfaces as a ClaudeSwitchError naming the
install, not as a traceback.
"""

import contextlib
import json
import os
import pathlib
import sys

import pytest

from claude_swap.exceptions import ClaudeSwitchError


def _cfg(tmp_path, name, port=None, *, marker=True):
    """A `.claude.json` under its own dir: wired to `port`, or unwired.

    Module-level because three classes had near-identical copies and the ONE
    detail that must not drift is the marker list — `_wire_mark_of` returns
    None for an empty list by design, so `"_cswapPinWiredKeys": []` is not a
    wired config at all. A fixture that wrote `[]` while its docstring claimed
    "carries the marker" made every test built on it assert about a config the
    code would never look at twice.
    """
    d = tmp_path / name
    d.mkdir()
    cfg = d / ".claude.json"
    if not marker:
        cfg.write_text(json.dumps({}))
        return cfg
    env = (
        {"HTTPS_PROXY": f"http://127.0.0.1:{port}", "CSWAP_PIN_PORT": str(port)}
        if port is not None
        else {}
    )
    cfg.write_text(json.dumps({
        "env": env,
        "_cswapPinWiredKeys": list(env) or ["HTTPS_PROXY"],
    }))
    return cfg


def _dead_port() -> int:
    """A port nothing is listening on, obtained by binding and closing.

    NOT a hardcoded number. The heal tests once used 36301, which is the port
    a real pin daemon uses — so on a machine where the pin was actually
    running they described a LIVE wiring while claiming to describe a dead
    one, and every assertion about healing was inverted. Asking the OS for a
    port and releasing it is the only way to be sure it is closed.

    ``TestNoFixtureNamesARealDaemonPort`` lints for the literal coming back.
    """
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _port_literal_offenders(directory, own_file, own_class_name: str) -> list:
    """Every line in ``directory``'s test files (and its ``conftest.py``)
    that hardcodes the pin daemon's port, ``36301`` — the regression guard
    behind ``TestNoFixtureNamesARealDaemonPort``, extracted so the guard's
    OWN correctness (self-exemption scope, which files get scanned) is
    itself testable rather than only exercisable through the one real run
    against this file.

    THIS IS A REGRESSION GUARD FOR ONE KNOWN LITERAL (``36301``), not a
    general "no port literal" lint — `36302` and any other value slip
    through by design. Generalising it was considered and rejected: a
    matcher broad enough to catch every port literal would also flag the
    legitimate ones fixtures construct throughout this file (`_dead_port`'s
    OS-assigned port compared against a wired config, deliberate 0/negative
    values exercising `OverflowError` paths, and so on), trading one honest
    known-literal guard for a noisy one nobody could keep green.

    ``own_file``/``own_class_name`` scope the self-exemption to the code that
    IS this lint, in the file that IS this lint. Matching by class name alone
    (across every file the glob visits) let a same-named class in a
    DIFFERENT file inherit the exemption for a real hardcode — a coincidence
    a routine rename makes more likely, not less, since the replacement name
    is chosen fresh and nothing stops it colliding with a class elsewhere.
    Requiring ``path == own_file`` as well closes that: the exemption now
    answers "is this the lint itself", not just "is this named like it".
    """
    import ast

    own_file = own_file.resolve()
    # THE LINT'S OWN CODE now lives in two places in ITS OWN FILE: the test
    # class (the caller) and this function (the literal comparison actually
    # lives here since Task 3 pulled it out to make it independently
    # testable). Both need the literal to describe it, so both are exempt —
    # but ONLY in own_file; the name match must never cross files.
    own_names = {own_class_name, "_port_literal_offenders"}

    paths = sorted(directory.glob("test_*.py"))
    conftest = directory / "conftest.py"
    if conftest.exists():
        paths.append(conftest)

    offenders = []
    for path in paths:
        # encoding="utf-8" EXPLICITLY. `read_text()` uses the platform
        # default, which is cp1252 on the Windows runner — this lint reads
        # every test file in the tree, and one of them carries a byte
        # cp1252 has no mapping for, so the lint died on an encoding error
        # while the file it was reading was perfectly fine:
        #
        #     UnicodeDecodeError: 'charmap' codec can't decode byte 0x8f
        #     in position 7322: character maps to <undefined>
        #
        # A source file's encoding is UTF-8 by definition (PEP 3120), so
        # the platform default is never the right answer for reading one.
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        skip = set()
        is_own_file = path.resolve() == own_file
        for n in ast.walk(tree):
            # A DOCSTRING'S FULL SPAN, not just the line with the
            # delimiter — `'"""' in line` only caught the opening/closing
            # line, so a multi-line docstring's own CONTINUATION lines
            # (the prose this lint is supposed to exempt) still matched.
            if (
                isinstance(
                    n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)
                )
                and n.body
                and isinstance(n.body[0], ast.Expr)
                and isinstance(n.body[0].value, ast.Constant)
                and isinstance(n.body[0].value.value, str)
            ):
                doc = n.body[0].value
                skip.update(range(doc.lineno, doc.end_lineno + 1))
            # THIS LINT'S OWN CODE, which has to contain the literal to look
            # for it — without this it flagged itself. Scoped to OWN_FILE:
            # a same-named class or function elsewhere describes nothing
            # about the lint and must not borrow its exemption.
            if (
                is_own_file
                and isinstance(n, (ast.ClassDef, ast.FunctionDef))
                and n.name in own_names
            ):
                skip.update(range(n.lineno, n.end_lineno + 1))
        for i, line in enumerate(text.splitlines(), 1):
            if "36301" not in line or i in skip or line.lstrip().startswith("#"):
                continue
            offenders.append(f"{path.name}:{i}: {line.strip()}")
    return offenders


class TestImportSafeWithoutTheExtra:
    def test_the_module_imports(self):
        """A top-level import of the optional dependency would make cswap
        refuse to start at all — no switching, no TUI — over a feature the
        user opted out of."""
        import importlib

        importlib.import_module("claude_swap.pin")

    def test_nothing_imports_cswap_pin_at_module_scope(self):
        """Behavioural tests pass right up until someone adds a top-level
        import; assert the seam itself. Walks the whole tree, not just
        module-level statements, so a conditional import at module scope
        (`if sys.platform != "win32": import cswap_pin`) is caught too."""
        import ast
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent / "src" / "claude_swap"
        offenders = []
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            nested = set()
            for fn in ast.walk(tree):
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    nested.update(id(c) for c in ast.walk(fn))
            for node in ast.walk(tree):
                if id(node) in nested:
                    continue
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                if any(n.startswith("cswap_pin") for n in names):
                    offenders.append(f"{path.name}:{node.lineno}")
        assert not offenders, f"module-scope import of cswap_pin: {offenders}"

    def test_the_whole_package_imports_without_it(self):
        """Runs in a subprocess with cswap_pin blocked at sys.meta_path —
        importlib.import_module does not go through builtins.__import__, so a
        __import__ patch would miss that form."""
        import subprocess
        import textwrap
        from pathlib import Path

        src = str(Path(__file__).resolve().parent.parent / "src")
        code = textwrap.dedent(
            f"""
            import pkgutil, sys
            sys.path.insert(0, {src!r})
            class Block:
                def find_spec(self, name, path=None, target=None):
                    if name.split(".")[0] == "cswap_pin":
                        raise ImportError("blocked", name=name)
                    return None
            sys.meta_path.insert(0, Block())
            import claude_swap
            for m in pkgutil.walk_packages(claude_swap.__path__, "claude_swap."):
                __import__(m.name)
            """
        )
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[-900:]


@pytest.fixture
def posix(monkeypatch):
    """Force the POSIX branch of _impl.

    Everything in this class tests how the optional dependency is RESOLVED,
    which is platform-independent — but on Windows _impl refuses before it
    gets there, so the resolution logic would go untested on exactly one CI
    runner. Pinning the platform tests the logic on all three rather than
    skipping it where it happens not to run.
    """
    import sys as _sys

    monkeypatch.setattr(_sys, "platform", "linux")


class TestTheMissingExtraIsReported:
    def test_impl_raises_the_install_hint(self, posix, monkeypatch):
        import importlib.util

        from claude_swap import pin

        monkeypatch.setattr(importlib.util, "find_spec", lambda *a, **k: None)
        with pytest.raises(ClaudeSwitchError, match=r"claude-swap\[pin\]"):
            pin._impl()

    def test_a_broken_package_ROOT_is_not_reported_as_missing(
        self, posix, tmp_path, monkeypatch
    ):
        """The breakage that actually happens is in the package ROOT, not the
        submodule: cswap_pin/__init__.py imports cryptography.

        find_spec has to IMPORT the parent to read its __path__, so that error
        comes out of find_spec — not out of the import_module below it. The
        sibling test stubs find_spec to succeed, so it proves nothing about
        this path, and the bug it missed told users to install a package they
        already had.

        Real files, no stubs: stubbing find_spec is exactly what hid this.

        In-process, not a subprocess: a subprocess would have to fake the
        platform to get past the Windows refusal, and faking it before the
        imports run makes claude_swap.locking pick the POSIX branch and die
        on `import fcntl`. The `posix` fixture reaches _impl without that.
        """
        import importlib
        import importlib.util

        pkg = tmp_path / "cswap_pin"
        pkg.mkdir()
        (pkg / "__init__.py").write_text(
            "raise ImportError(\"No module named 'cryptography'\", "
            "name='cryptography')"
        )
        (pkg / "proxy.py").write_text("")

        from claude_swap import pin

        monkeypatch.syspath_prepend(str(tmp_path))
        for name in [m for m in sys.modules if m.split(".")[0] == "cswap_pin"]:
            monkeypatch.delitem(sys.modules, name)
        importlib.invalidate_caches()

        with pytest.raises(ImportError) as exc:
            pin._impl()
        assert exc.value.name == "cryptography", (
            f"reported as {exc.value!r} — a broken package root must not be "
            "rewritten into 'install the extra'"
        )

    def test_a_broken_dependency_is_not_reported_as_missing(self, posix, monkeypatch):
        """The package is THERE and its own import fails (a missing
        cryptography). That must surface, not be rewritten into 'install the
        pin extra' — advice that would be wrong."""
        import importlib
        import importlib.util

        from claude_swap import pin

        monkeypatch.setattr(importlib.util, "find_spec", lambda *a, **k: object())

        def boom(name, package=None):
            raise ImportError("No module named 'cryptography'", name="cryptography")

        monkeypatch.setattr(importlib, "import_module", boom)
        with pytest.raises(ImportError, match="cryptography"):
            pin._impl()


class TestLaunchIsNeverBlocked:
    def test_wire_launch_env_passes_through_without_the_extra(self, monkeypatch):
        from claude_swap import pin

        monkeypatch.setattr(pin, "_impl", lambda: (_ for _ in ()).throw(
            ClaudeSwitchError("nope")))
        monkeypatch.setattr(pin, "clear_wiring", lambda *a, **k: False)
        env = {"A": "1"}
        assert pin.wire_launch_env(object(), env) == env

    def test_a_failing_pin_does_not_block_the_launch(self, monkeypatch):
        """An optional feature must never be able to stop claude from starting."""
        import types

        from claude_swap import pin

        impl = types.SimpleNamespace(
            ensure_proxy=lambda sw: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        monkeypatch.setattr(pin, "_impl", lambda: impl)
        env = {"A": "1"}
        assert pin.wire_launch_env(object(), env) == env

    def test_wire_launch_env_actually_wires_the_pinned_proxy(self, tmp_path, monkeypatch):
        """`grep -rn 'wire_env' tests/` was zero hits: every existing test
        drives a FAILURE shape (no package, ensure_proxy raising, ensure_proxy
        returning None) and none drives an active pin actually getting wired.

        Mutating the success path to `return env` (the pin wires nothing at
        all) left the suite at 150 passed — this is the test that closes that
        gap. Asserts on the RESULT `wire_env` produced, not that it was
        called: a "wire_env was called" mock assertion passes even when
        wire_env's return value is thrown away.
        """
        import types

        from claude_swap import pin

        # `tmp_path`, not a literal "/tmp/..." — a POSIX path literal renders as
        # `\tmp\pin-ca.pem` on Windows and the equality fails there while
        # passing here. this test was the whole of the first
        # red CI, on test-windows only.
        ca = tmp_path / "pin-ca.pem"
        impl = types.SimpleNamespace(
            ensure_proxy=lambda sw: (9955, ca),
            wire_env=lambda env, port, ca_path: {
                **env,
                "HTTPS_PROXY": f"http://127.0.0.1:{port}",
                "NODE_EXTRA_CA_CERTS": str(ca_path),
            },
        )
        monkeypatch.setattr(pin, "_impl", lambda: impl)
        result = pin.wire_launch_env(object(), {"A": "1"})
        assert result == {
            "A": "1",
            "HTTPS_PROXY": "http://127.0.0.1:9955",
            "NODE_EXTRA_CA_CERTS": str(ca),
        }, f"the pin was resolved and returned a proxy but the env is {result!r}"

    def test_wire_env_raising_leaves_the_launch_unpinned(self, tmp_path, monkeypatch):
        """The fail-open invariant on the SAME path as the test above: an
        `ensure_proxy` that succeeds followed by a `wire_env` that RAISES must
        still return an env that names no proxy — never partial wiring, never
        a propagated exception. Checked on the env content, not just that no
        exception escaped `wire_launch_env`.
        """
        import types

        from claude_swap import pin

        def _boom(env, port, ca_path):
            raise RuntimeError("wire_env boom")

        impl = types.SimpleNamespace(
            ensure_proxy=lambda sw: (9955, tmp_path / "pin-ca.pem"),
            wire_env=_boom,
            unwire_if_dead=lambda p: None,
        )
        monkeypatch.setattr(pin, "_impl", lambda: impl)
        # Isolate this test from the real config lock/unwire tail below the
        # guarded call — that tail is Task 2's surface, not this one's.
        monkeypatch.setattr(pin, "_config_lock_is_free", lambda budget: False)
        sw = types.SimpleNamespace(backup_dir=tmp_path)
        result = pin.wire_launch_env(sw, {"A": "1"})
        assert result == {"A": "1"}, (
            f"wire_env raised but the returned env still names a proxy: {result!r}"
        )

    def test_ensure_heals_only_when_there_is_something_to_heal(
        self, tmp_path, monkeypatch
    ):
        """`--ensure` is the SAME invariant one level out: an rc hook calls it
        before every hand-launched `claude`, where `wire_launch_env` cannot
        reach (that session execs from the user's shell, not through us).

        Three properties, all of which `--heal` deliberately does NOT promise
        — it prints its verdict and is called by a human or a status line:
        exit 0 on every path including a raise, silence on the repair path,
        and no work at all when nothing is wired.

        Driven through the REPAIR path for the silence check: an ensure that
        returns early is silent for free, so the idle machine would pass
        against a version that prints every repair.
        """
        import types

        from claude_swap import pin

        sw = types.SimpleNamespace(backup_dir=tmp_path)

        # Idle: nothing wired, nothing recorded -> no heal at all. This runs on
        # EVERY launch, and the status line already calls heal on a timer.
        healed = []
        monkeypatch.setattr(pin, "heal", lambda s, **_k: healed.append(1) or (False, ""))
        monkeypatch.setattr(pin, "_wiring_present", lambda s: False)
        monkeypatch.setattr(pin, "_pinned_email_now", lambda s: None)
        assert pin.run(sw, None, ensure=True) == 0
        assert healed == [], "ensure healed a machine that was never pinned"

        # Wired: it must actually repair, or the assertion above is satisfied
        # by an ensure that never heals anything.
        monkeypatch.setattr(pin, "_wiring_present", lambda s: True)
        monkeypatch.setattr(pin, "heal", lambda s, **_k: healed.append(1) or (True, "Restored"))
        assert pin.run(sw, None, ensure=True) == 0
        assert healed == [1], "a wired config was not healed"

    def test_ensure_re_reads_rather_than_trusting_heal(self, tmp_path, monkeypatch):
        """CASE D, one level in: the CALLER must not trust the return value.

        The lmd42 outage's fourth disaster path was an old cswap that
        REJECTED `--heal` — exit 2, the call made, the rejection unread, and
        the machine stranded for days. `pin-ensure` answers it by RE-READING
        the config afterwards instead of believing the command.

        The same shape exists inside the package, because `heal` is allowed
        to be wrong: it calls into `cswap_pin`, a PEER on its own release
        schedule, and this module's standing rule is that a verdict comes
        from the state and not from a call. A heal that returns
        (True, "Restored") while the wiring still names a dead port must not
        end the launch hook's work.
        """
        import socket
        import types

        from claude_swap import pin

        cfg = _cfg(tmp_path, "cfgdir", _dead_port())
        import claude_swap.paths as paths

        monkeypatch.setattr(paths, "get_global_config_path", lambda: cfg)
        monkeypatch.setattr(paths, "get_default_global_config_path", lambda: cfg)

        # `_write_json` is not decoration: `clear_wiring` writes through the
        # switcher, so a stub without it makes the unwire fail and the test
        # would "reproduce" a defect that is only the fixture's.
        sw = types.SimpleNamespace(
            backup_dir=tmp_path,
            _write_json=lambda p, d: p.write_text(json.dumps(d), encoding="utf-8"),
        )
        # A LYING heal: reports success, changes nothing. Exactly what an old
        # cswap's rejected --heal looks like from the caller's side.
        monkeypatch.setattr(pin, "heal", lambda s, **_k: (True, "Restored the cloud pin"))

        assert pin.run(sw, None, ensure=True) == 0
        assert not bool(pin._dead_wired_configs(sw)), (
            "ensure believed a heal that changed nothing — the config still "
            "names a dead port and every session started after this launch "
            "inherits it, which is disaster path D"
        )

    def test_a_config_write_that_fails_does_not_report_the_wiring_removed(
        self, tmp_path, monkeypatch
    ):
        """A FAILED UNWIRE MUST NOT READ AS A REMOVED WIRING.

        `_clear_wiring_locked` writes through `switcher._write_json`, and that
        write is the whole operation: the proxy vars are still in the file
        until it lands. Every existing test hands it a stub that writes
        successfully, so the failure path — a read-only home, a full disk, a
        `ConfigError` from the switcher's own validation — has never run.

        The direction matters more than the failure. Reporting "removed" when
        the vars are still there sends every caller down the wrong branch:
        `run(..., ensure=True)` stops repairing, and the user is told the pin
        is gone while every new session still dials the proxy.

        THE CONTROL is the same call with a working writer, which must report
        removal — otherwise "does not report removed" would pass for a
        `clear_wiring` that never removes anything.
        """
        import types

        import claude_swap.paths as paths

        from claude_swap import pin

        def _cleared(writer):
            # A DIRECTORY PER ATTEMPT, created first: `_cfg` writes into it
            # rather than making it, and the two attempts must not share a
            # config or the control's result answers for both.
            home = tmp_path / writer.__name__
            home.mkdir(parents=True, exist_ok=True)
            cfg = _cfg(home, "cfgdir", _dead_port())
            monkeypatch.setattr(paths, "get_global_config_path", lambda: cfg)
            monkeypatch.setattr(
                paths, "get_default_global_config_path", lambda: cfg
            )
            sw = types.SimpleNamespace(backup_dir=tmp_path, _write_json=writer)
            ok = pin.clear_wiring(sw)
            still = json.loads(cfg.read_text(encoding="utf-8"))
            return ok, "HTTPS_PROXY" in (still.get("env") or {})

        def works(p, d):
            p.write_text(json.dumps(d), encoding="utf-8")

        def fails(p, d):
            raise OSError("read-only file system")

        # CONTROL: a working writer really removes the wiring.
        ok, still_wired = _cleared(works)
        assert ok and not still_wired, (
            f"CONTROL FAILED: a normal unwire did not happen "
            f"(ok={ok} still_wired={still_wired})"
        )

        ok, still_wired = _cleared(fails)
        assert still_wired, "premise: the failed write left the vars in place"
        assert not ok, (
            "clear_wiring reported the wiring removed while HTTPS_PROXY is "
            "still in .claude.json — `--ensure` stops repairing and every new "
            "session keeps dialling the proxy"
        )

    def test_a_peer_that_returns_a_broken_env_cannot_reach_execvpe(
        self, tmp_path, monkeypatch
    ):
        """The peer's return value is the one thing taken on trust, and both
        wrong shapes are worse than a raise.

        `wire_launch_env` guards the CALL but returns whatever `wire_env`
        hands back, and `os.execvpe` sits outside `_exec`'s try. Measured:

          returns None            `execvpe(argv, None)` does NOT fail. It
                                  hands the child the PARENT's environ, so
                                  CLAUDE_CONFIG_DIR is silently dropped and
                                  the session launches against the default
                                  login instead of the selected account.
                                  Verified: the child printed the parent's
                                  value. That is an account-isolation break
                                  with no error anywhere.
          returns {"K": 41234}    `execvpe` raises TypeError out of `_exec`
                                  and kills the launch.

        This module's standing rule is that the peer may be wrong — `heal`
        re-reads state rather than believing a return value. The same rule
        belongs here: a peer that answers nonsense degrades to an UNPINNED
        launch, which is the failure mode everything else is built to
        tolerate.
        """
        import types

        from claude_swap import pin

        base = {"CLAUDE_CONFIG_DIR": "/selected/account", "PATH": "/usr/bin"}
        sw = types.SimpleNamespace(backup_dir=tmp_path)

        class _Peer:
            def __init__(self, answer):
                self._answer = answer

            def ensure_proxy(self, switcher):
                return (41234, tmp_path / "ca.pem")

            def wire_env(self, env, port, ca_path):
                return self._answer

            def unwire_if_dead(self, certdir):
                return False

        for answer, label in (
            (None, "None"),
            ({"CLAUDE_CONFIG_DIR": "/selected/account", "PORT": 41234}, "a non-str value"),
            ("not a dict", "a string"),
        ):
            monkeypatch.setattr(pin, "_impl", lambda a=answer: _Peer(a))
            out = pin.wire_launch_env(sw, dict(base))
            assert isinstance(out, dict), f"{label}: returned {type(out).__name__}"
            assert all(
                isinstance(k, str) and isinstance(v, str) for k, v in out.items()
            ), f"{label}: reached execvpe with a non-str entry — the launch dies"
            assert out.get("CLAUDE_CONFIG_DIR") == "/selected/account", (
                f"{label}: the account's config dir was lost — the session "
                f"launches against the default login, silently"
            )

    def test_ensure_gives_up_the_config_lock_rather_than_stalling_a_launch(
        self, tmp_path, monkeypatch
    ):
        """A launch hook may not wait out the DEFAULT lock timeout.

        `--ensure` runs from an rc file before EVERY hand-launched `claude`.
        Its `clear_wiring` had no budget, so it fell back to
        `DEFAULT_TIMEOUT_S` (9.0s) — and a config lock held by a routine
        credential refresh turned a launch into a measured 9.5s stall, against
        0.86s for the same state through `wire_launch_env`.

        `_LAUNCH_LOCK_BUDGET_S` exists for exactly this site and the `heal`
        one line above already used it. Giving up early is the right answer
        here: the wiring is stale, not dangerous to leave for one more launch,
        and the next launch tries again. A launch that blocks is the failure
        this module is written to avoid.
        """
        import time
        import types

        from claude_swap import pin

        cfg = _cfg(tmp_path, "cfgdir", _dead_port())
        import claude_swap.paths as paths

        monkeypatch.setattr(paths, "get_global_config_path", lambda: cfg)
        monkeypatch.setattr(paths, "get_default_global_config_path", lambda: cfg)

        seen = {}

        def _spy(switcher, timeout=None, only=None):
            seen["timeout"] = timeout
            seen["only"] = only
            # Behave like a lock we cannot take: burn the budget we were
            # given, then report nothing removed.
            time.sleep(min(timeout or pin_locks.DEFAULT_TIMEOUT_S, 1.5))
            return False

        from claude_swap import claude_locks as pin_locks

        # THE PROBE IS THE OTHER HALF OF THE SAME BUDGET, and it was the half
        # that shipped unbudgeted: `wire_launch_env` passes `_LAUNCH_PROBE_S`
        # and this site passed nothing, so the probe fell back to its 2.0s
        # default on the very path that exists to never block. Wrapped, not
        # replaced — the elapsed assertion below has to keep measuring the
        # real connect.
        # ON `_dead_wired_configs`, which is what this site now asks. It is
        # the question this site asks, and the same probe, so the budget
        # under test is unchanged. A `_wiring_is_stale` predicate used to hold
        # the same answer one bool wide; it was deleted once every call site
        # moved here, so spying on it would now patch a name that is gone.
        # (Older prose in this file still names it — that is history, and the
        # absence of any `src/` reference is what makes it readable as such.)
        real_dead = pin._dead_wired_configs

        def _dead_spy(switcher, connect_timeout=2.0):
            seen["probe"] = connect_timeout
            return real_dead(switcher, connect_timeout=connect_timeout)

        monkeypatch.setattr(pin, "_dead_wired_configs", _dead_spy)
        monkeypatch.setattr(pin, "clear_wiring", _spy)
        monkeypatch.setattr(pin, "heal", lambda s, **_k: (True, "Restored"))

        sw = types.SimpleNamespace(
            backup_dir=tmp_path,
            _write_json=lambda p, d: p.write_text(json.dumps(d), encoding="utf-8"),
        )
        started = time.monotonic()
        assert pin.run(sw, None, ensure=True) == 0
        elapsed = time.monotonic() - started

        assert seen.get("timeout") == pin._LAUNCH_LOCK_BUDGET_S, (
            f"--ensure asked for timeout={seen.get('timeout')!r}; unbudgeted "
            f"means the {pin_locks.DEFAULT_TIMEOUT_S}s default, paid before "
            f"every hand-launched claude"
        )
        assert seen.get("probe") == pin._LAUNCH_PROBE_S, (
            f"--ensure probed with connect_timeout={seen.get('probe')!r}; "
            f"unbudgeted means 2.0s against a black-holed port, on the path "
            f"whose sibling in wire_launch_env already pays "
            f"{pin._LAUNCH_PROBE_S}s"
        )
        # 2.0 is not an arbitrary ceiling: it is the probe default this site
        # used to inherit. Windows CI failed here at exactly 2.5s (0.5 lock +
        # 2.0 probe) on a port that Linux refuses instantly, which is what a
        # black-holed port costs everywhere.
        assert elapsed < 2.0, f"the launch hook blocked for {elapsed:.1f}s"
        # AND ONLY THE DEAD ONES. The machine-wide clear this replaced took a
        # live config down with a dead sibling; `only=None` here would mean
        # the launch path had drifted back to it.
        assert seen.get("only") == [cfg], (
            f"--ensure cleared only={seen.get('only')!r}; None is the "
            f"machine-wide clear that unwires a serving config because "
            f"another one names a dead port"
        )

    def test_every_probe_a_launch_arms_is_budgeted_including_heals(
        self, tmp_path, monkeypatch
    ):
        """No socket on the `--ensure` path may carry the 2.0s default.

        The sibling test above stubs `heal`, so it measured ONE probe and the
        two INSIDE `heal` stayed invisible — a launch hook that looked fixed
        while still arming 4.2s. Counting `settimeout` instead of wall clock
        is what makes that visible: a refusing port and a black-holing one arm
        the same budgets and only the second pays them, so a Linux runner
        cannot tell them apart by elapsed time. Windows already proved the
        difference is real.

        `heal` keeps its 2.0s default for the hand-run `--heal`; the launch
        path passes its own budget, the way `wire_launch_env` always has.
        """
        import socket
        import types

        from claude_swap import pin

        cfg = _cfg(tmp_path, "cfgdir", _dead_port())
        import claude_swap.paths as paths

        monkeypatch.setattr(paths, "get_global_config_path", lambda: cfg)
        monkeypatch.setattr(paths, "get_default_global_config_path", lambda: cfg)
        # No package: `heal` cannot restart, so it takes the branch that
        # probes and then unwires — the longest path a launch can walk.
        monkeypatch.setattr(pin, "_live_impl", lambda: None)
        monkeypatch.setattr(pin, "clear_wiring", lambda s, timeout=None: False)

        armed = []
        real = socket.socket.settimeout
        monkeypatch.setattr(
            socket.socket,
            "settimeout",
            lambda self, t: (armed.append(t), real(self, t))[1],
        )

        sw = types.SimpleNamespace(
            backup_dir=tmp_path,
            _write_json=lambda p, d: p.write_text(json.dumps(d), encoding="utf-8"),
        )
        assert pin.run(sw, None, ensure=True) == 0

        assert armed, "nothing probed — the fixture stopped exercising the path"
        assert max(armed) <= pin._LAUNCH_PROBE_S, (
            f"a launch armed {armed} — {sum(armed):.1f}s against a "
            f"black-holed port, before every hand-launched claude"
        )

    def test_ensure_prints_nothing_and_survives_a_raising_heal(
        self, tmp_path, monkeypatch, capsys
    ):
        """The two halves of the launch contract that need capsys/raises."""
        import types

        from claude_swap import pin

        sw = types.SimpleNamespace(backup_dir=tmp_path)
        monkeypatch.setattr(pin, "_wiring_present", lambda s: True)

        monkeypatch.setattr(pin, "heal", lambda s, **_k: (True, "Restored the cloud pin"))
        pin.run(sw, None, ensure=True)
        assert capsys.readouterr().out == "", "a launch hook printed"

        def _boom(_s, **_k):
            raise RuntimeError("heal exploded")

        monkeypatch.setattr(pin, "heal", _boom)
        assert pin.run(sw, None, ensure=True) == 0, (
            "a raising heal made --ensure exit nonzero; an rc hook "
            "propagating that fails the launch it was protecting"
        )

    @pytest.mark.skipif(
        sys.platform == "win32", reason="_guard_root's euid check is POSIX-only"
    )
    def test_ensure_is_silent_as_root_too(self, tmp_path, monkeypatch, capsys):
        """SILENCE IS THE OTHER HALF OF THE PROMISE, and only exit 0 was kept.

        `_guard_root` PRINTS before it `sys.exit(1)`s, and the handler in
        `_pin_command` catches only the SystemExit — so the exit code went
        quiet while the line above it did not. On a bare-metal root shell,
        the case that guard names, the rc hook emitted

            Error: Do not run this script as root (unless running in a container)

        before EVERY hand-launched `claude`, from the one flag whose help text
        says "Silent, never fails".

        Through the CLI, not `pin.run`: the leak is one frame ABOVE it, so the
        sibling test's `pin.run` assertions can never see it.
        """
        from claude_swap import cli
        from claude_swap.switcher import ClaudeAccountSwitcher

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
        monkeypatch.setattr(
            ClaudeAccountSwitcher, "_is_running_in_container", lambda _self: False
        )
        # THE PREMISE, and without it this test passes when the root
        # simulation does not take: a NON-root `--ensure` also exits 0 in
        # silence, so the assertions below would be describing the ordinary
        # path under the name of the root one. The control is the sibling
        # command that MUST refuse — if `cswap pin` still runs happily here,
        # nothing about this process looks like root and nothing below means
        # anything.
        with contextlib.suppress(SystemExit):
            cli._pin_command([])
        # ON THE MESSAGE, NOT THE EXIT CODE. Exit 1 does NOT discriminate:
        # measured with the euid patch reverted, plain `cswap pin` still
        # exited 1 for an unrelated reason, so an exit-code premise passes
        # against a root simulation that never took. The guard's own line is
        # the only thing that says this process looks like root.
        assert "as root" in capsys.readouterr().err, (
            "the guard did not print for the command that SHOULD print, so "
            "an empty stderr below is not evidence of silence — the root "
            "simulation did not take"
        )

        with pytest.raises(SystemExit) as exc:
            cli._pin_command(["--ensure"])
        assert exc.value.code == 0, f"--ensure failed as root: {exc.value.code}"
        captured = capsys.readouterr()
        assert (captured.out, captured.err) == ("", ""), (
            f"a launch hook printed as root: out={captured.out!r} "
            f"err={captured.err!r}"
        )

    def test_a_store_that_cannot_be_built_is_rendered_not_raised(
        self, tmp_path, monkeypatch, capsys
    ):
        """THE OTHER HALF OF THE SAME HANDLER, and it fails the opposite way.

        The construction `try` exists for `--ensure`'s silence promise, so its
        handler is `except (Exception, SystemExit): if ensure: exit 0; raise`.
        For every OTHER invocation that `raise` is the whole behaviour — and
        the `except ClaudeSwitchError` that renders `Error: …` sits on the
        SECOND try, which only wraps `pin_run`. So the exact failures the
        comment above it names (migration collision, unwritable store) reach
        the user as a traceback from `cswap pin` while `cswap run` prints one
        line for the identical fault.

        A traceback is the worst outcome for the command whose job is to work
        when things are already broken.
        """
        from claude_swap import cli

        def _boom(*_a, **_k):
            raise ClaudeSwitchError("store is unwritable")

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(cli, "ClaudeAccountSwitcher", _boom)

        with pytest.raises(SystemExit) as exc:
            cli._pin_command(["2"])
        assert exc.value.code == 1, exc.value.code
        err = capsys.readouterr().err
        # The premise and the assertion in one: the rendered line has to carry
        # the cause, or this passes against a handler that swallowed it.
        assert "store is unwritable" in err, (
            f"the failure did not reach the user as a message: {err!r}"
        )
        assert "Traceback" not in err, err


class TestTheWiringCanAlwaysBeRemoved:
    """`.claude.json` names the pin's port, and Claude Code applies that env
    block at boot. If only the optional package could remove it, uninstalling
    the pin would strand every launch dialling a dead port."""

    def _wired(self, tmp_path):
        tmp_path.mkdir(parents=True, exist_ok=True)
        cfg = tmp_path / ".claude.json"
        port = _dead_port()
        cfg.write_text(
            json.dumps(
                {
                    "env": {
                        "HTTPS_PROXY": f"http://127.0.0.1:{port}",
                        "CSWAP_PIN_PORT": str(port),
                        "UNRELATED": "keep me",
                    },
                    "_cswapPinWiredKeys": ["HTTPS_PROXY", "CSWAP_PIN_PORT"],
                    "_cswapPinWiredKeysSaved": {"HTTPS_PROXY": "http://127.0.0.1:9901"},
                }
            )
        )
        return cfg

    def test_clear_wiring_works_without_the_extra(self, tmp_path, monkeypatch):
        import claude_swap.paths as paths
        from claude_swap.pin import clear_wiring
        from claude_swap.switcher import ClaudeAccountSwitcher

        cfg = self._wired(tmp_path)
        monkeypatch.setattr(paths, "get_global_config_path", lambda: cfg)
        assert clear_wiring(ClaudeAccountSwitcher()) is True
        env = json.loads(cfg.read_text())["env"]
        assert "CSWAP_PIN_PORT" not in env
        assert env["UNRELATED"] == "keep me"
        assert env["HTTPS_PROXY"] == "http://127.0.0.1:9901"  # displaced value back

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="needs POSIX symlink semantics without developer mode",
    )
    def test_a_symlinked_config_is_written_THROUGH_not_replaced(
        self, tmp_path, monkeypatch
    ):
        """A rename swaps a directory ENTRY and does not follow links.

        `settings.atomic_write_json` resolves the target first and says why in
        its docstring — #192/#193, the same bug in `session.py`'s writer. The
        unwire publishes through `switcher._write_json`, which ends in
        `shutil.move` and resolves nothing, so on a dotfiles-managed
        `.claude.json` the clear DETACHES the link: the symlink becomes a
        regular clean file, the real target keeps the dead-port wiring, Claude
        Code's later writes land on an orphan, and the next deploy restores
        the link and resurrects the wiring.

        It reads as success at every step, which is why it needs a test rather
        than a caveat. The sibling half of one clear (`_clear_pin_record`)
        already uses the resolving writer, so today the two disagree.
        """
        import claude_swap.paths as paths
        from claude_swap.pin import clear_wiring
        from claude_swap.switcher import ClaudeAccountSwitcher

        real = self._wired(tmp_path / "real")
        link = tmp_path / ".claude.json"
        link.symlink_to(real)
        monkeypatch.setattr(paths, "get_global_config_path", lambda: link)

        assert clear_wiring(ClaudeAccountSwitcher()) is True

        assert link.is_symlink(), (
            "the clear replaced the symlink with a regular file — the next "
            "dotfiles deploy restores the link and the dead wiring with it"
        )
        env = json.loads(real.read_text())["env"]
        assert "CSWAP_PIN_PORT" not in env, (
            "the link's TARGET still carries the wiring: every launch reading "
            "the real file still dials the dead port"
        )

    def test_clear_wiring_reads_the_receipt_where_the_pin_now_writes_it(
        self, tmp_path, monkeypatch
    ):
        """Same removal, receipt in its NEW home.

        `_cswapPinWiredKeys` / `…Saved` moved out of `.claude.json` into the
        account store: the `env` block is Claude Code's boot interface and
        cannot move, but the bookkeeping is cswap's own and does not belong in
        the user's file. `_wired` above builds the OLD shape, so every other
        test here exercises the fallback; this one is the forward path.

        It is the same load-bearing property as `test_clear_wiring_works_
        without_the_extra` — an uninstalled pin must not strand a wiring — and
        it would fail silently the same way: `clear_wiring` returns False, the
        proxy vars stay, and every launch dials a dead port.
        """
        import claude_swap.paths as paths
        from claude_swap.pin import _ledger_path, clear_wiring
        from claude_swap.switcher import ClaudeAccountSwitcher

        port = _dead_port()
        cfg = tmp_path / ".claude.json"
        cfg.write_text(json.dumps({"env": {
            "HTTPS_PROXY": f"http://127.0.0.1:{port}",
            "CSWAP_PIN_PORT": str(port),
            "UNRELATED": "keep me",
        }}))  # NO receipt in the config — the new pin does not put one there
        monkeypatch.setattr(paths, "get_global_config_path", lambda: cfg)

        side = _ledger_path(cfg)
        side.parent.mkdir(parents=True, exist_ok=True)
        side.write_text(json.dumps({
            "_cswapPinWiredKeys": ["HTTPS_PROXY", "CSWAP_PIN_PORT"],
            "_cswapPinWiredKeysSaved": {"HTTPS_PROXY": "http://127.0.0.1:9901"},
        }))

        assert clear_wiring(ClaudeAccountSwitcher()) is True, (
            "the host could not see a wiring the current pin wrote — an "
            "uninstalled pin would strand it"
        )
        env = json.loads(cfg.read_text())["env"]
        assert "CSWAP_PIN_PORT" not in env
        assert env["UNRELATED"] == "keep me"
        assert env["HTTPS_PROXY"] == "http://127.0.0.1:9901"  # displaced value back

    @pytest.mark.skipif(
        sys.platform == "win32" or os.geteuid() == 0,
        reason="needs POSIX permission semantics (non-root)",
    )
    def test_a_stuck_receipt_does_not_become_a_permanent_re_run_forever(
        self, tmp_path, monkeypatch
    ):
        """The config is CLEAN; only the bookkeeping is stuck.

        `_clear_wiring_locked` returns `_clear_ledger(path)` AFTER the config
        write succeeded. So an unwritable `pin-wiring/` (root-owned parent,
        read-only mount, full disk) makes it report False over a config that
        no longer carries the wiring at all — and `_wire_mark_of` then keeps
        finding the stale sidecar marker, so `_wiring_present` stays True and
        every re-run answers "could not be removed, re-run once it frees up".
        Advice that can never come true, over a user who is not stranded.

        `_clear_ledger`'s docstring argues this return prevents a phantom
        SUCCESS. It does; it substitutes a permanent phantom FAILURE, which is
        the same defect with the sign flipped. The message has to separate
        "your launches still dial a dead port" from "our receipt is stale".
        """
        import claude_swap.paths as paths
        from claude_swap.pin import _ledger_path, clear_wiring
        from claude_swap.switcher import ClaudeAccountSwitcher

        port = _dead_port()
        cfg = tmp_path / ".claude.json"
        cfg.write_text(json.dumps({"env": {
            "HTTPS_PROXY": f"http://127.0.0.1:{port}",
            "CSWAP_PIN_PORT": str(port),
        }}))
        monkeypatch.setattr(paths, "get_global_config_path", lambda: cfg)
        side = _ledger_path(cfg)
        side.parent.mkdir(parents=True, exist_ok=True)
        side.write_text(json.dumps({
            "_cswapPinWiredKeys": ["HTTPS_PROXY", "CSWAP_PIN_PORT"],
        }))
        side.parent.chmod(0o500)  # the receipt cannot be rewritten
        try:
            sw = ClaudeAccountSwitcher()
            clear_wiring(sw)
            env = json.loads(cfg.read_text()).get("env", {})
            assert "CSWAP_PIN_PORT" not in env, (
                f"fixture did not reach the shape under test — the CONFIG "
                f"write was supposed to succeed: {env!r}"
            )
            from claude_swap import pin

            changed, message = pin.clear_pin(sw)
            assert "re-run" not in message.lower(), (
                f"advice that can never come true: the config is already "
                f"clean and no re-run can rewrite a read-only receipt dir. "
                f"{message!r}"
            )
            assert str(side) in message, (
                f"the message does not name the stale receipt, which is the "
                f"only thing left to remove: {message!r}"
            )
        finally:
            side.parent.chmod(0o700)

    def test_an_orphan_receipt_is_not_reported_as_a_broken_port_value(
        self, tmp_path, monkeypatch
    ):
        """A recreated config inherits the OLD receipt, deterministically.

        `_ledger_path` keys the sidecar on the config PATH, so a
        `.claude.json` deleted and recreated at the same path gets the same
        sha and therefore the same receipt — and Claude Code recreating that
        file at that path is the normal case, not a corner. The fresh config
        carries no proxy vars, so `_port_of_config` reads None and `heal`
        reaches the "names no readable CSWAP_PIN_PORT" arm.

        Which tells the user to fix a value that is not in the file. Same
        family as the three messages fixed above — the condition is "an
        orphan receipt", and the sentence names a different one. The remedy
        it also offers (`--clear`) does work, which is what keeps this
        bounded; naming the actual state is what makes it followable.
        """
        import types

        from claude_swap import pin
        import claude_swap.paths as paths
        from claude_swap.pin import _ledger_path

        backup = tmp_path / "b"
        backup.mkdir()
        cfg = tmp_path / ".claude.json"
        cfg.write_text(json.dumps({"env": {"UNRELATED": "keep me"}}))
        monkeypatch.setattr(paths, "get_global_config_path", lambda: cfg)
        monkeypatch.setattr(paths, "get_default_global_config_path", lambda: cfg)
        side = _ledger_path(cfg)
        side.parent.mkdir(parents=True, exist_ok=True)
        side.write_text(json.dumps({
            "_cswapPinWiredKeys": ["HTTPS_PROXY", "CSWAP_PIN_PORT"],
        }))

        sw = types.SimpleNamespace(
            backup_dir=backup,
            _write_json=lambda p, d: p.write_text(json.dumps(d), encoding="utf-8"),
        )
        monkeypatch.setattr(pin, "_live_impl", lambda: None)

        assert pin._wiring_present(sw) is True, "fixture is not the orphan shape"
        changed, message = pin.heal(sw)

        assert not changed, message
        assert "Fix that value" not in message, (
            f"heal told the user to fix a CSWAP_PIN_PORT that is not in the "
            f"file — the state is a leftover receipt: {message!r}"
        )
        assert "--clear" in message or "clear" in message, (
            f"the message does not offer the remedy that works: {message!r}"
        )

    def test_no_sidecar_is_not_the_same_answer_as_a_cleared_one(
        self, tmp_path, monkeypatch
    ):
        """ABSENT and EMPTIED are different receipts and must stay different.

        `--clear` writes `{_cswapPinWiredKeys: []}`, and that says "the
        sidecar was cleared" — which suppresses a marker the clear itself
        emptied. A sidecar that was never written says nothing at all, so a
        config marker still has to be found and removed.

        Asserted through `_saved_of`, because that is where the two answers
        actually diverge. Measured on every sidecar/config pair: with a usable
        config marker both receipts agree, and a test written that way passes
        even with absence and a clear treated identically (my first version
        did). The difference shows only in what gets RESTORED — a clear's
        receipt carries displaced values, and absence carries nothing.
        """
        import claude_swap.paths as paths
        from claude_swap.pin import _WIRE_MARK, _ledger_path, _saved_of

        cfg = tmp_path / ".claude.json"
        monkeypatch.setattr(paths, "get_global_config_path", lambda: cfg)
        raw = {"env": {"HTTPS_PROXY": f"http://127.0.0.1:{_dead_port()}"}}

        side = _ledger_path(cfg)
        side.parent.mkdir(parents=True, exist_ok=True)
        side.unlink(missing_ok=True)
        assert _saved_of(raw, cfg) == {}, (
            "a sidecar that was never written produced displaced values — "
            "absence was read as a receipt when it is the lack of one, and a "
            "clear would restore a hop this machine never had"
        )

        # The one shape that IS a receipt, same config underneath.
        side.write_text(json.dumps({
            _WIRE_MARK: [], f"{_WIRE_MARK}Saved": {"HTTPS_PROXY": "http://127.0.0.1:9901"},
        }))
        assert _saved_of(raw, cfg) == {"HTTPS_PROXY": "http://127.0.0.1:9901"}

    def test_an_emptied_sidecar_does_not_blind_us_to_an_older_pins_wiring(
        self, tmp_path, monkeypatch
    ):
        """A clear must not make the OTHER receipt location unreadable.

        The receipt moved from `.claude.json` into the account store, and an
        older `cswap-pin` still writes the config key — the compat promise
        this module states in `_wire_mark_of`. But `--clear` writes an EMPTY
        sidecar, and an empty sidecar was treated as the final answer. So
        after one clear, a wiring written by that older package became
        invisible to EVERY recovery path at once:

            _wiring_present  False      _wired_ports  []
            _wiring_is_stale False      clear_wiring  False
            heal             (False, 'Nothing to heal')

        while `.claude.json` still named a proxy port. That is precisely the
        stranding this module exists to prevent, reported as healthy by every
        probe that could have caught it. The population is not exotic: with no
        floor on the extra, an already-satisfied `cswap-pin` is not upgraded,
        so anyone who installed the pin before the sidecar existed lands here.

        The rule: an empty sidecar answers for the SIDECAR, not for the
        config. Only when neither location has a marker is the answer "not
        wired".
        """
        import claude_swap.paths as paths
        from claude_swap.pin import _ledger_path, clear_wiring
        from claude_swap.switcher import ClaudeAccountSwitcher

        port = _dead_port()
        cfg = tmp_path / ".claude.json"
        monkeypatch.setattr(paths, "get_global_config_path", lambda: cfg)

        # An emptied sidecar, exactly as `--clear` leaves it.
        side = _ledger_path(cfg)
        side.parent.mkdir(parents=True, exist_ok=True)
        side.write_text(json.dumps({"_cswapPinWiredKeys": []}))

        # ...and an OLDER cswap-pin wires again, into the config only.
        cfg.write_text(json.dumps({
            "env": {
                "HTTPS_PROXY": f"http://127.0.0.1:{port}",
                "CSWAP_PIN_PORT": str(port),
                "UNRELATED": "keep me",
            },
            "_cswapPinWiredKeys": ["HTTPS_PROXY", "CSWAP_PIN_PORT"],
            "_cswapPinWiredKeysSaved": {"HTTPS_PROXY": "http://127.0.0.1:9901"},
        }))

        assert clear_wiring(ClaudeAccountSwitcher()) is True, (
            "an emptied sidecar hid a wiring the config plainly carries — "
            "every recovery path reports healthy while sessions dial a dead "
            "port"
        )
        env = json.loads(cfg.read_text())["env"]
        assert "CSWAP_PIN_PORT" not in env
        assert env["UNRELATED"] == "keep me"
        assert env["HTTPS_PROXY"] == "http://127.0.0.1:9901"

    def test_an_older_pins_wiring_restores_ITS_values_not_the_sidecars(
        self, tmp_path, monkeypatch
    ):
        """The marker and the displaced values must come from ONE receipt.

        Same shape as the test above — emptied sidecar, older cswap-pin wires
        into the config only — but asking what gets RESTORED rather than
        whether the wiring is found. `_wire_mark_of` falls through to the
        config here; `_saved_of` has to fall through with it. Reading the
        marker from one receipt and the values from the other restores one
        wiring's values over another wiring's keys, which is worse than
        restoring nothing: it writes a proxy address that was never there.

        The sidecar deliberately carries a DIFFERENT saved value, so a
        crossed read is visible rather than accidentally right.
        """
        import claude_swap.paths as paths
        from claude_swap.pin import _ledger_path, clear_wiring
        from claude_swap.switcher import ClaudeAccountSwitcher

        port = _dead_port()
        cfg = tmp_path / ".claude.json"
        monkeypatch.setattr(paths, "get_global_config_path", lambda: cfg)

        # Emptied by `--clear`, but still carrying what THAT clear displaced.
        side = _ledger_path(cfg)
        side.parent.mkdir(parents=True, exist_ok=True)
        side.write_text(json.dumps({
            "_cswapPinWiredKeys": [],
            "_cswapPinWiredKeysSaved": {"HTTPS_PROXY": "http://127.0.0.1:1111"},
        }))

        # An OLDER cswap-pin wires again, config-only, over a DIFFERENT hop.
        cfg.write_text(json.dumps({
            "env": {
                "HTTPS_PROXY": f"http://127.0.0.1:{port}",
                "CSWAP_PIN_PORT": str(port),
            },
            "_cswapPinWiredKeys": ["HTTPS_PROXY", "CSWAP_PIN_PORT"],
            "_cswapPinWiredKeysSaved": {"HTTPS_PROXY": "http://127.0.0.1:9901"},
        }))

        assert clear_wiring(ClaudeAccountSwitcher()) is True
        env = json.loads(cfg.read_text())["env"]
        assert env["HTTPS_PROXY"] == "http://127.0.0.1:9901", (
            "the sidecar's saved value was restored over the config's keys — "
            "sessions now dial a hop this wiring never displaced"
        )

    def test_clearing_an_unwired_config_is_a_no_op(self, tmp_path, monkeypatch):
        import claude_swap.paths as paths
        from claude_swap.pin import clear_wiring
        from claude_swap.switcher import ClaudeAccountSwitcher

        cfg = tmp_path / ".claude.json"
        cfg.write_text(json.dumps({"env": {"UNRELATED": "keep me"}}))
        monkeypatch.setattr(paths, "get_global_config_path", lambda: cfg)
        assert clear_wiring(ClaudeAccountSwitcher()) is False
        assert json.loads(cfg.read_text())["env"] == {"UNRELATED": "keep me"}

    def test_clear_also_reaches_the_default_profile_from_inside_a_session(
        self, tmp_path, monkeypatch
    ):
        """`cswap run` sets CLAUDE_CONFIG_DIR in the CHILD's env, so a launch
        from a normal terminal wires ~/.claude.json while one from inside a
        session terminal wires the session's copy. Resolving one path clears
        whichever the caller happens to sit in and reports success over a
        config that still names a dead port."""
        import claude_swap.paths as paths
        from claude_swap.pin import clear_wiring
        from claude_swap.switcher import ClaudeAccountSwitcher

        session = self._wired(tmp_path / "session")
        default = self._wired(tmp_path / "home")
        monkeypatch.setattr(paths, "get_global_config_path", lambda: session)
        monkeypatch.setattr(paths, "get_default_global_config_path", lambda: default)

        assert clear_wiring(ClaudeAccountSwitcher()) is True
        for cfg in (session, default):
            assert "_cswapPinWiredKeys" not in json.loads(cfg.read_text()), (
                f"{cfg.parent.name} left wired — its sessions still dial a dead port"
            )

    def test_a_contended_first_path_does_not_starve_a_free_second(
        self, tmp_path, monkeypatch
    ):
        """Only the session lock held, at the REAL
        production budget ``_LAUNCH_LOCK_BUDGET_S = 0.5``): `clear_wiring`
        returned False with BOTH configs still wired. The first path waited
        the WHOLE budget on a lock nobody released, so `left <= 0` by the
        time the loop reached the second path — free the entire time — and
        it was `continue`d without ever being tried.

        ASSERTS THE OBSERVABLE PROPERTY, not a wall clock. A prior version of
        this test asserted `elapsed < BUDGET_S * 2` at an inflated 3.0s
        test-only budget. Two facts killed that version: deleting
        its `elapsed` assertion changed nothing (the mutant this test names,
        `share = left / (len(paths) - i)` -> `share = left`, survives it and
        is killed by a different test), and starvation is directly
        observable as "path 2 was never attempted" — so assert that,
        via a recording wrapper around
        ``claude_swap.claude_locks.proper_lockfile`` that records every
        ``lock_dir`` it is asked to acquire.

        Runs at the REAL `_LAUNCH_LOCK_BUDGET_S` (0.5s), not a test-only
        constant: with the lock's own retry sleep now clamped to the
        remaining budget (see test_claude_locks.py), a 0.5s total split two
        ways gives each path a real 0.25s rather than one jittered sleep
        (0.25-0.5s) swallowing it whole — the fair-share arithmetic this test
        guards is only meaningful once the sleep itself respects a deadline.
        """
        from contextlib import contextmanager

        import claude_swap.paths as paths
        from claude_swap import claude_locks, pin
        from claude_swap.switcher import ClaudeAccountSwitcher

        session = self._wired(tmp_path / "session")
        default = self._wired(tmp_path / "home")
        monkeypatch.setattr(paths, "get_global_config_path", lambda: session)
        monkeypatch.setattr(paths, "get_default_global_config_path", lambda: default)

        # PIN THE JITTER TO ITS WORST CASE, deterministically. Left random,
        # `proper_lockfile`'s retry sleep (0.25-0.5s) only OCCASIONALLY draws
        # long enough to exceed a 0.25s fair share, so an unclamped sleep
        # (the very mutant this test exists to kill — see the reinstate-and
        # -show-red proof in the task report) starves path 2 on some runs and
        # not others: 5/5 green with the mutant in and the jitter
        # left to chance. Forcing `random.random() == 1.0` makes every retry
        # sleep exactly 0.5s — the full budget on the FIRST path alone — so
        # the unclamped mutant fails every time, not by luck of the draw.
        monkeypatch.setattr(claude_locks.random, "random", lambda: 1.0)

        real_proper_lockfile = claude_locks.proper_lockfile
        attempted = []

        @contextmanager
        def _recording_lockfile(lock_dir, **kwargs):
            attempted.append(lock_dir)
            with real_proper_lockfile(lock_dir, **kwargs):
                yield

        # `clear_wiring` does `from claude_swap.claude_locks import
        # proper_lockfile` INSIDE its own body, so patching the module
        # attribute (not a `pin.proper_lockfile` name that does not exist)
        # is what every call re-resolves to.
        monkeypatch.setattr(claude_locks, "proper_lockfile", _recording_lockfile)

        # A live holder on the SESSION lock only — fresh mtime, so it is never
        # taken over as stale, and it is held for the whole call.
        held = session.parent / (session.name + ".lock")
        held.mkdir()
        try:
            changed = pin.clear_wiring(
                ClaudeAccountSwitcher(), timeout=pin._LAUNCH_LOCK_BUDGET_S
            )
        finally:
            held.rmdir()

        session_lock = session.parent / (session.name + ".lock")
        default_lock = default.parent / (default.name + ".lock")
        assert session_lock in attempted, "the contended path was never even tried"
        assert default_lock in attempted, (
            "the free default profile was starved by the contended session "
            "lock — its lock was never even attempted"
        )
        assert changed is True, (
            "the free default profile was starved by the contended session "
            "lock — clear_wiring reported nothing removed"
        )
        assert "_cswapPinWiredKeys" not in json.loads(default.read_text()), (
            "the uncontended config was never attempted"
        )
        # The contended one was correctly skipped, not broken.
        assert "_cswapPinWiredKeys" in json.loads(session.read_text())

    def test_the_untimed_deadline_is_a_total_not_a_per_path_allowance(
        self, tmp_path, monkeypatch
    ):
        """Two live-held locks: 9.29s unmutated vs 18.03s
        with ``timeout = DEFAULT_TIMEOUT_S * len(paths)`` — exactly the 2x
        regression the shared-deadline comment says this was added to fix, on
        the UNTIMED call (``clear_wiring(sw)``, no explicit ``timeout``).

        Every other test here passes an explicit ``timeout``, so none of them
        exercises the branch that resolves ``DEFAULT_TIMEOUT_S`` itself. Fast
        by construction: ``DEFAULT_TIMEOUT_S`` is shrunk before the call, so
        the whole test still runs in a couple of seconds rather than 9-18.
        """
        import time

        import claude_swap.claude_locks as claude_locks
        import claude_swap.paths as paths
        from claude_swap import pin
        from claude_swap.switcher import ClaudeAccountSwitcher

        session = self._wired(tmp_path / "session")
        default = self._wired(tmp_path / "home")
        monkeypatch.setattr(paths, "get_global_config_path", lambda: session)
        monkeypatch.setattr(paths, "get_default_global_config_path", lambda: default)
        # Shrink the DEFAULT this function resolves internally when no
        # timeout is passed — the mutation under test doubles WHATEVER this
        # is, so a small value keeps the doubled case fast too.
        monkeypatch.setattr(claude_locks, "DEFAULT_TIMEOUT_S", 1.0)

        # BOTH locks live-held, for real: a stubbed lock would not exercise
        # clear_wiring's own per-path share arithmetic.
        session_lock = session.parent / (session.name + ".lock")
        default_lock = default.parent / (default.name + ".lock")
        session_lock.mkdir()
        default_lock.mkdir()
        try:
            start = time.monotonic()
            changed = pin.clear_wiring(ClaudeAccountSwitcher())  # no timeout
            elapsed = time.monotonic() - start
        finally:
            session_lock.rmdir()
            default_lock.rmdir()

        assert not changed, "fixture invalid: nothing should have been removed"
        # 1x the (shrunk) default plus slack, never 2x it — the doubled
        # mutation reliably clears this bar (~2.2s against a 1.0s
        # DEFAULT_TIMEOUT_S here; the fix stays under ~1.3s).
        assert elapsed < 1.9, (
            f"the untimed deadline behaved as a PER-PATH allowance, not a "
            f"total: {elapsed:.2f}s against a shrunk default of 1.0s"
        )

    def test_the_launch_path_does_not_wait_on_the_config_lock(
        self, tmp_path, monkeypatch
    ):
        """clear_wiring takes Claude Code's config lock, whose default wait is
        9s, and the launch path calls it on EVERY `cswap run` for users who
        will never install the pin. Claude Code itself holds that lock while
        refreshing credentials, so an unbounded wait stalls the launch."""
        import time

        import claude_swap.paths as paths
        from claude_swap import pin

        cfg = self._wired(tmp_path)
        monkeypatch.setattr(paths, "get_global_config_path", lambda: cfg)
        monkeypatch.setattr(paths, "get_default_global_config_path", lambda: cfg)
        monkeypatch.setattr(
            pin, "_impl", lambda: (_ for _ in ()).throw(ClaudeSwitchError("absent"))
        )

        # The same name clear_wiring derives, held for real. Nothing is
        # patched here: a monkeypatch of config_lock_dir in this
        # test and was dead — clear_wiring stopped calling it, and the test
        # stayed green with that function rigged to raise on call.
        held = cfg.parent / (cfg.name + ".lock")
        # A live holder: fresh mtime, so the staleness takeover does not fire.
        held.mkdir()
        try:
            start = time.monotonic()
            env = pin.wire_launch_env(object(), {"A": "1"})
            waited = time.monotonic() - start
        finally:
            held.rmdir()

        assert env == {"A": "1"}
        assert waited < 3.0, f"a contended launch blocked for {waited:.2f}s"

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="NTFS has no POSIX mode bits; switcher._write_json skips the "
        "chmod on win32 for the same reason",
    )
    def test_the_config_is_not_left_world_readable(self, tmp_path, monkeypatch):
        """It can hold primaryApiKey and inline MCP credentials; a plain write
        takes the umask and rename publishes that mode."""
        import os
        import stat as _stat

        import claude_swap.paths as paths
        from claude_swap.pin import clear_wiring
        from claude_swap.switcher import ClaudeAccountSwitcher

        cfg = self._wired(tmp_path)
        os.chmod(cfg, 0o644)
        monkeypatch.setattr(paths, "get_global_config_path", lambda: cfg)
        old = os.umask(0o022)
        try:
            assert clear_wiring(ClaudeAccountSwitcher()) is True
        finally:
            os.umask(old)
        assert not _stat.S_IMODE(cfg.stat().st_mode) & 0o077


class TestTheLaunchPathIsWired:
    """`cswap run` must route its child through the proxy, not only
    hand-launched sessions that read .claude.json.

    wire_launch_env existed with zero production callers — session.py never
    mentioned the pin — so `cswap run 2` launched unpinned while `cswap pin`
    reported success. Found by a complexity review flagging it as dead code;
    it was a missing call, not spare code.
    """

    def _manager(self, temp_home):
        # The real switcher, not a stub: _exec touches enough of it that a
        # SimpleNamespace only proves the stub matches itself.
        from claude_swap.session import SessionManager
        from claude_swap.switcher import ClaudeAccountSwitcher

        return SessionManager(ClaudeAccountSwitcher())

    def test_exec_routes_the_child_through_the_pin(self, temp_home, monkeypatch):
        from claude_swap import pin as pin_mod
        from claude_swap import session as session_mod

        monkeypatch.setattr(
            pin_mod,
            "wire_launch_env",
            lambda sw, env: {**env, "HTTPS_PROXY": "http://127.0.0.1:9955"},
        )
        captured = {}

        def fake_execvpe(binary, argv, env):
            captured["env"] = env
            raise SystemExit(0)

        # _exec forks two ways — execvpe on POSIX, subprocess.run on Windows —
        # and the pin has to be wired on BOTH. Stub whichever this platform
        # actually takes, rather than skipping the Windows runner and leaving
        # that branch unasserted.
        monkeypatch.setattr(session_mod.os, "execvpe", fake_execvpe)
        monkeypatch.setattr(
            session_mod.subprocess,
            "run",
            lambda argv, env=None, **kw: fake_execvpe(argv[0], argv, env),
        )
        with pytest.raises(SystemExit):
            self._manager(temp_home)._exec("claude", [], env={"A": "1"})
        assert captured["env"].get("HTTPS_PROXY") == "http://127.0.0.1:9955", (
            "the launch path does not wire the pin — `cswap run` goes out unpinned"
        )

    def test_a_pin_failure_still_launches(self, temp_home, monkeypatch):
        from claude_swap import pin as pin_mod
        from claude_swap import session as session_mod

        def boom(sw, env):
            raise RuntimeError("pin exploded")

        monkeypatch.setattr(pin_mod, "wire_launch_env", boom)

        def launched(*a, **kw):
            raise SystemExit(0)

        monkeypatch.setattr(session_mod.os, "execvpe", launched)
        monkeypatch.setattr(session_mod.subprocess, "run", launched)
        with pytest.raises((SystemExit, RuntimeError)) as exc:
            self._manager(temp_home)._exec("claude", [], env={"A": "1"})
        assert exc.type is SystemExit, "a pin failure blocked the launch"


class TestWindowsIsRejectedCleanly:
    """cswap advertises Windows support; the pin cannot honour it.

    The proxy takes its daemon lock with fcntl.flock and refcounts sessions
    through os.mkfifo. Without this guard a Windows user gets a
    ModuleNotFoundError from inside the dependency instead of a sentence they
    can act on — and only at first use, after `pip install claude-swap[pin]`
    appeared to succeed.
    """

    def test_it_says_so_rather_than_failing_inside_the_dependency(self, monkeypatch):
        import importlib.util
        import sys as _sys

        from claude_swap import pin

        monkeypatch.setattr(_sys, "platform", "win32")
        # Even with the package apparently installed, it must refuse.
        monkeypatch.setattr(importlib.util, "find_spec", lambda *a, **k: object())
        with pytest.raises(ClaudeSwitchError, match="Windows"):
            pin._impl()


class TestClearReachesBothConfigsWithTheExtraINSTALLED:
    """The two-path clear must hold for users who HAVE the pin.

    clear_wiring only ran in the except branch, so on the happy path the
    unwiring was done entirely by the package's own single-path resolver:
    `cswap pin --clear` from inside a session terminal cleared that session's
    config and left ~/.claude.json naming a dead port, while printing
    "Unpinned". Everyone has the extra at the moment they unpin, so the
    guarantee held for exactly nobody.
    """

    def test_the_default_profile_is_cleared_too(self, tmp_path, monkeypatch):
        import types

        import claude_swap.paths as paths
        from claude_swap import pin
        from claude_swap.switcher import ClaudeAccountSwitcher

        def wired(where):
            where.mkdir(parents=True, exist_ok=True)
            cfg = where / ".claude.json"
            cfg.write_text(
                json.dumps(
                    {
                        "env": {"HTTPS_PROXY": "http://127.0.0.1:44444"},
                        "_cswapPinWiredKeys": ["HTTPS_PROXY"],
                    }
                )
            )
            return cfg

        session = wired(tmp_path / "session")
        default = wired(tmp_path / "home")
        monkeypatch.setattr(paths, "get_global_config_path", lambda: session)
        monkeypatch.setattr(paths, "get_default_global_config_path", lambda: default)

        # The extra IS installed and apply_pin succeeds — the path that had no
        # clear_wiring call at all. It unwires only what its own resolver sees.
        def apply_pin(switcher, email, org):
            session.write_text(json.dumps({"env": {}}))
            return False

        monkeypatch.setattr(
            pin,
            "_impl",
            lambda: types.SimpleNamespace(
                apply_pin=apply_pin, load_pin=lambda d: ("a@b.c", None)
            ),
        )
        pin.run(ClaudeAccountSwitcher(), None, clear=True)
        assert "_cswapPinWiredKeys" not in json.loads(default.read_text()), (
            "the default profile stayed wired to a dead port while --clear "
            "reported success"
        )


class TestTheTuiSurfaceSurvivesTheSplit:
    """The pin has a TUI half, and the split dropped it once already.

    The old in-tree pin wired three files — dashboard.py (menu row + submenu +
    action), widgets.py (the ○ cloud badge), autoview.py (the same badge on
    the auto view). The first cut of this seam carried the CLI and launch
    paths and none of that, and every check stayed green: the CLI probe
    passed, the daemon answered, and the running TUIs were serving code they
    had exec'd 16 hours before the cutover, so the badge was still on screen.
    A human looking at the screen is what caught it.

    These assert the surface exists at all. A check that exercises only one
    surface reports the other as healthy.
    """

    def test_no_extra_means_no_pin_row(self, tmp_path, monkeypatch):
        """A user who never asked for the pin must not see a row for it."""
        import types

        from claude_swap.tui import dashboard

        monkeypatch.setattr(dashboard.pin, "is_available", lambda: False)
        # A REAL `backup_dir`, empty. The gate also asks `_pinned_email_now`
        # now — the record is the state every unwire leaves behind — and that
        # reads `settings.json` under it. `object()` was enough while the gate
        # only asked `_wiring_present`, which ignores its switcher entirely;
        # pointing it at an empty dir keeps the answer "nothing pinned" while
        # letting the call be the real one.
        monkeypatch.setattr(
            dashboard.DashboardScreen,
            "app",
            property(lambda self: types.SimpleNamespace(
                switcher=types.SimpleNamespace(backup_dir=tmp_path), snapshot=None
            )),
            raising=False,
        )
        screen = object.__new__(dashboard.DashboardScreen)
        ids = [a for _l, a in screen._root_entries()]
        assert "pin-menu" not in ids, f"pin offered without the extra: {ids}"

        monkeypatch.setattr(dashboard.pin, "is_available", lambda: True)
        monkeypatch.setattr(dashboard.pin, "pinned_email", lambda sw: None)
        assert "pin-menu" in [a for _l, a in screen._root_entries()]

    def test_installing_the_extra_is_seen_without_a_restart(self, tmp_path):
        """A TUI open across an install must start offering the pin.

        A long-lived process caches each sys.path directory by mtime, so a
        package installed after start can stay invisible — usually
        visible but not when the install lands inside the same mtime tick.
        That is the "I installed it and the menu is still missing" report,
        and invalidate_caches is what closes it.
        """
        import subprocess
        import textwrap
        from pathlib import Path

        pkg = tmp_path / "late" / "cswap_pin"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "proxy.py").write_text("def load_pin(d):\n    return None\n")
        src = str(Path(__file__).resolve().parent.parent / "src")
        late = str(tmp_path / "late")
        code = textwrap.dedent(
            f"""
            import sys
            sys.path.insert(0, {src!r})
            # THE PREMISE HAS TO HOLD WHETHER OR NOT THE EXTRA IS INSTALLED.
            # This asserted "not resolvable" and then installed a fake — true
            # only while cswap-pin was absent from every environment, which
            # stopped being true when `claude-swap[pin]` joined the dev group
            # so the peer contract could be tested at all. An INSTALLED
            # package broke the premise, not the behaviour.
            #
            # Refuse the real one until the "install" lands; after that the
            # late directory sits at sys.path[0] and wins on its own, so what
            # decides the second assertion is still invalidate_caches seeing
            # a NEW path entry — the thing under test.
            class OnlyLate:
                def find_spec(self, name, path=None, target=None):
                    if name.split(".")[0] != "cswap_pin":
                        return None
                    if {late!r} in sys.path:
                        return None
                    raise ImportError("not installed here", name=name)
            sys.meta_path.insert(0, OnlyLate())
            from claude_swap import pin
            # _impl refuses on win32 BEFORE it looks for the package, so
            # is_available is False there no matter what gets installed —
            # a cross-platform claim cannot be tested through a
            # platform-gated function. Assert on the resolution step, which
            # is what invalidate_caches actually affects — the axis on
            # which Windows differs from linux and macos here.
            import importlib, importlib.util
            def resolvable():
                importlib.invalidate_caches()
                try:
                    return importlib.util.find_spec("cswap_pin.proxy") is not None
                except ImportError:
                    return False
            assert resolvable() is False, "saw a package that is not there"
            # The install: a path entry that did not exist when we started.
            sys.path.insert(0, {str(tmp_path / "late")!r})
            assert resolvable() is True, "a restart should not be required"
            # And the seam honours it on any platform that has the pin at all.
            if sys.platform != "win32":
                assert pin.is_available() is True
            sys.exit(0)
            """
        )
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[-400:]

    def test_the_menu_rebuilds_on_the_poll(self):
        """The root menu was built once at mount, so a row that appears on
        install could not appear until a restart.

        The METHOD only — that something fires it is asserted by driving a real
        snapshot through a real app in
        ``test_tui_pin.py::test_a_snapshot_actually_rebuilds_the_root_menu``.
        Grepping ``on_mount``'s source for the name is satisfied by a comment
        satisfies: deleting the subscription and leaving the word behind kept
        the suite green while the pin row could no longer appear.
        """
        from claude_swap.tui import dashboard

        assert hasattr(dashboard.DashboardScreen, "refresh_root_menu")

    def test_opening_the_pin_submenu_lists_the_accounts(self, monkeypatch):
        """The row existing is not the same as the row WORKING.

        Every other guard here asserts the pin surface is present; none of
        them called it. A stray `del impl` on the last line shipped an
        UnboundLocalError into the one function the menu row opens — the
        submenu raised on every successful call, and the whole suite was
        green. Call it.
        """
        import types

        from claude_swap.tui import dashboard

        acc = types.SimpleNamespace(number="2", email="a@b.c", alias=None)
        monkeypatch.setattr(dashboard.pin, "_impl", lambda: types.SimpleNamespace())
        monkeypatch.setattr(dashboard.pin, "pinned_email", lambda sw: "a@b.c")
        monkeypatch.setattr(
            dashboard.DashboardScreen,
            "app",
            property(lambda self: types.SimpleNamespace(
                switcher=object(), snapshot=types.SimpleNamespace(accounts=[acc]))),
            raising=False,
        )
        screen = object.__new__(dashboard.DashboardScreen)
        entries = screen._pin_entries()
        labels = [label for label, _a in entries]
        actions = [a for _l, a in entries]
        assert "pin:2" in actions, f"the account is not pinnable: {actions}"
        assert any("○ cloud" in label for label in labels), "the pinned account is unmarked"
        assert "pin:clear" in actions, "no way to unpin from the TUI"

    def test_the_badge_helper_is_reachable_and_fails_open(self, monkeypatch):
        """pinned_email answers the TUI's one question and never raises: no
        extra, no pin, and a malformed pin file all render as no badge."""
        from claude_swap import pin

        monkeypatch.setattr(
            pin, "_impl", lambda: (_ for _ in ()).throw(ClaudeSwitchError("absent"))
        )
        assert pin.pinned_email(object()) is None

        monkeypatch.setattr(
            pin, "_impl", lambda: (_ for _ in ()).throw(RuntimeError("broken"))
        )
        assert pin.pinned_email(object()) is None

    def test_the_dashboard_root_menu_still_offers_the_pin(self, monkeypatch):
        """The ROOT MENU ROW, not merely the handler behind it.

        Grepping the module for "pin-menu" passes with the row deleted — the
        action handler still contains the string — so the row can vanish while
        the check stays green. Call _root_entries and look at the
        ids it actually returns.
        """
        import types

        from claude_swap.tui import dashboard

        # `app` is a read-only Textual property, so patch it on the class.
        monkeypatch.setattr(
            dashboard.DashboardScreen,
            "app",
            property(lambda self: types.SimpleNamespace(switcher=object(), snapshot=None)),
            raising=False,
        )
        # With the extra present — its absence hiding the row is the sibling
        # test; this one is about the row existing at all when it should.
        monkeypatch.setattr(dashboard.pin, "is_available", lambda: True)
        monkeypatch.setattr(dashboard.pin, "pinned_email", lambda sw: None)
        screen = object.__new__(dashboard.DashboardScreen)
        ids = [action for _label, action in screen._root_entries()]
        assert "pin-menu" in ids, (
            f"the cloud pin is unreachable from the dashboard menu: {ids}"
        )
        labels = [label for label, action in screen._root_entries()
                  if action == "pin-menu"]
        # THE LABEL MUST NAME THE PIN, not merely exist. "Cloud account" alone
        # passes with the email interpolation gone, and that string is the
        # entire reason the row is worth reading without opening it.
        assert "Cloud account" in labels[0]
        assert "none" in labels[0], (
            f"the row does not say where the pin points: {labels[0]!r}"
        )
        monkeypatch.setattr(dashboard.pin, "pinned_email", lambda sw: "a@b.c")
        pinned = [label for label, action in screen._root_entries()
                  if action == "pin-menu"][0]
        assert "a@b.c" in pinned, (
            f"the row does not name the pinned account: {pinned!r}"
        )

    def test_both_account_renderers_actually_render_the_badge(self):
        """The badge rides on the account rows, and there are two renderers —
        the full card and the minimised line. Losing it from one is the half
        that reads as healthy.

        RENDERED TEXT, not signatures and not source text. The previous version
        asserted `"cloud_pinned" in inspect.signature(...)` plus
        `"○ cloud" in inspect.getsource(...)`, and both are satisfiable with
        the feature gone: a parameter can exist and be ignored, and the glyph
        also appears in a comment: with the minimised renderer's
        badge block changed to `if False and cloud_pinned:`, the whole TUI
        suite stayed green.
        """
        from claude_swap.tui.widgets import account_card_text, mini_account_text
        from tests.test_tui import make_account

        acc = make_account(1, active=True)
        for name, render in (
            ("account_card_text", lambda **kw: account_card_text(acc, 80, **kw)),
            ("mini_account_text", lambda **kw: mini_account_text(acc, 0.0, **kw)),
        ):
            on = render(cloud_pinned=True).plain
            off = render(cloud_pinned=False).plain
            assert "○ cloud" in on, f"{name} does not render the cloud badge"
            assert "○ cloud" not in off, f"{name} renders the badge unpinned"

    def test_the_auto_switch_view_renders_the_badge_on_the_pinned_row(
        self, monkeypatch
    ):
        """Same assertion for the third renderer, which had only a source grep.

        Deleting the badge block from `_candidates_text` and leaving
        the glyph in a comment kept the suite green.
        """
        import types

        from claude_swap.tui import autoview
        from claude_swap.tui.theme import CSWAP_LIGHT
        from tests.test_tui import make_account

        accounts = [make_account(1, active=True), make_account(2)]
        pinned = accounts[1].email
        snap = types.SimpleNamespace(accounts=accounts)

        screen = object.__new__(autoview.AutoScreen)
        screen._settings = None
        # `app` is a read-only property on the Textual screen, so the stub goes
        # on the CLASS. Nothing here touches the real app.
        monkeypatch.setattr(
            autoview.AutoScreen,
            "app",
            property(
                lambda self: types.SimpleNamespace(
                    current_theme=CSWAP_LIGHT, switcher=types.SimpleNamespace()
                )
            ),
            raising=False,
        )

        monkeypatch.setattr(autoview.pin, "pinned_email", lambda sw: pinned)
        with_pin = screen._candidates_text(snap, accounts[0].number).plain
        monkeypatch.setattr(autoview.pin, "pinned_email", lambda sw: None)
        without = screen._candidates_text(snap, accounts[0].number).plain

        assert "○ cloud" in with_pin, "the auto-switch view lost the cloud badge"
        assert "○ cloud" not in without, "badged a row with nothing pinned"
        # And on the RIGHT row: the badge exists to save you matching an email
        # against the list below it.
        badged = [ln for ln in with_pin.splitlines() if "○ cloud" in ln]
        assert len(badged) == 1, badged
        assert accounts[1].number in badged[0], (
            f"badge landed on the wrong account row: {badged[0]!r}"
        )

    def test_a_BROKEN_pin_says_so_in_both_renderers(self):
        """The fail-open warning is the whole point of `pin_is_broken`.

        A pinned account with no usable credential still shows the cloud badge,
        so without this the one place claiming "your claude.ai side lives here"
        is the one place not admitting it no longer does.

        `pin_is_broken` was unit-tested, but nothing asserted its result ever
        reached TEXT — with both `(not applying)` renders changed to
        `if False and pin_is_broken(acc):`, the whole TUI suite stayed green.
        """
        import dataclasses

        from claude_swap.tui.widgets import (
            account_card_text,
            mini_account_text,
            pin_is_broken,
        )
        from tests.test_tui import make_account

        healthy = make_account(1, active=True)
        broken = dataclasses.replace(healthy, kind="api_key")
        assert pin_is_broken(broken) and not pin_is_broken(healthy), (
            "fixture does not describe the state under test"
        )

        for name, render in (
            ("account_card_text", lambda a: account_card_text(a, 80, cloud_pinned=True)),
            ("mini_account_text", lambda a: mini_account_text(a, 0.0, cloud_pinned=True)),
        ):
            assert "(not applying)" in render(broken).plain, (
                f"{name} shows a cloud badge over a pin that cannot apply"
            )
            assert "(not applying)" not in render(healthy).plain, (
                f"{name} warns about a healthy pin"
            )

    def test_a_pinned_account_actually_renders_the_badge(self):
        """Not just the parameter — the glyph has to reach the text."""
        from claude_swap.tui.widgets import account_card_text
        from tests.test_tui import make_account

        acc = make_account(1, active=True)
        plain = account_card_text(acc, 80, cloud_pinned=True).plain
        assert "○ cloud" in plain
        assert "○ cloud" not in account_card_text(acc, 80).plain


class TestAMidSessionInstallNeedsNoRestart:
    """``_live_impl`` backs ``is_available``/``pinned_email``, both called on
    every TUI render (AccountsPanel, AccountCard, dashboard._root_entries) —
    not just on the poll.

    It used to memoise behind a 1.0s TTL, which cost a module-level cache, a
    ``global``, and an autouse conftest fixture to reset between tests. What
    it bought, measured: 185us per uncached call, 6 calls per render tick with
    3 accounts and 13 with 10 — 1.11ms and 2.40ms against a POLL_INTERVAL_S of
    3000ms, i.e. 0.04-0.08% of one tick. The state was not worth 0.08%.

    What actually has to hold is the USER-VISIBLE contract the TTL existed to
    protect: installing the extra while the TUI is open must be seen without a
    restart. That is asserted directly here, so it stays covered no matter how
    the resolution is (or is not) memoised.
    """

    def test_an_install_mid_session_is_seen_with_no_restart(self, monkeypatch):
        from claude_swap import pin

        installed = [False]

        def resolves_once_installed():
            if not installed[0]:
                raise ClaudeSwitchError("not installed yet")
            return object()

        monkeypatch.setattr(pin, "_impl", resolves_once_installed)
        assert pin._live_impl() is None, "resolved before the install happened"
        assert not pin.is_available(), "the pin surface showed with no extra"

        installed[0] = True  # the install lands while the TUI is open
        assert pin._live_impl() is not None, (
            "the extra was installed but the TUI still needs a restart to see "
            "it — a feature that needs a restart to appear reads as broken"
        )
        assert pin.is_available(), "is_available did not follow _live_impl"

        # AND THE OTHER DIRECTION, which is what makes this test able to fail.
        # A memo that never expires still passes the half above: the first
        # call caches None, the second resolves fresh. Only an UNINSTALL —
        # a resolution that must go stale in the opposite direction — proves
        # nothing is being held. Measured: a never-expiring memo passes the
        # first half in isolation and fails here.
        installed[0] = False  # e.g. `uv tool install` without the extra
        assert pin._live_impl() is None, (
            "the extra went away and the pin surface is still showing — the "
            "resolution is memoised somewhere and the menu row is a lie"
        )
        assert not pin.is_available()

    def test_a_broken_extra_is_no_pin_rather_than_a_raise(self, monkeypatch):
        """The render path must never take the view down.

        `_impl` raises for a broken install (its whole reason for existing is
        to tell "absent" from "broken"), and every caller here is a badge or a
        menu row. None is the honest answer for both.
        """
        from claude_swap import pin

        def boom():
            raise RuntimeError("cryptography is broken")

        monkeypatch.setattr(pin, "_impl", boom)
        assert pin._live_impl() is None
        assert pin.is_available() is False

class TestSafeRedaction:
    """``_safe`` is the only security-relevant helper in this file, and
    nothing pinned its behaviour: a prior diff replaced its whole regex with
    ``str(exc)`` and every test stayed green."""

    def test_url_userinfo_is_scrubbed(self):
        from claude_swap.pin import _safe

        exc = RuntimeError("could not reach http://user:secret@127.0.0.1:9901/health")
        rendered = _safe(exc)
        assert "user:secret@" not in rendered
        assert "***@127.0.0.1:9901" in rendered

    def test_a_bare_email_address_is_left_alone(self):
        """The ``(?<=://)`` anchoring is the interesting part: a naive 'strip
        anything before @' would also eat an email address that has nothing
        to do with a URL."""
        from claude_swap.pin import _safe

        exc = RuntimeError("no account found for user@example.com")
        assert _safe(exc) == str(exc)


class TestTheRollbackVerdictIsNotFooledByShape:
    """The seam's reader and the package's writer must agree on shape.

    ``_pinned_email_now`` returned the org uuid raw (None when the key is
    absent) while ``cswap_pin.save_pin`` always writes ``org_uuid or ""``. So
    restoring a record that had no org key produced ``(email, "")`` against a
    ``before`` of ``(email, None)`` — unequal — and a SUCCESSFUL rollback
    reported itself as a failure, sending the user to check a state the code
    could already disprove. Exactly what _restore_pin was written to stop.
    """

    def test_a_record_with_no_org_key_rolls_back_cleanly(self, tmp_path):
        import json as _json
        import types

        from claude_swap import pin

        backup = tmp_path / "b"
        backup.mkdir()
        settings = backup / "settings.json"
        # No org key at all — what an older writer or a hand-edit leaves.
        settings.write_text(_json.dumps({"remoteControl": {"pinnedEmail": "old@e.com"}}))

        def _apply(sw, email, org):
            # Faithful to the package: it always writes `org_uuid or ""`.
            raw = _json.loads(settings.read_text())
            if email:
                raw["remoteControl"] = {
                    "pinnedEmail": email, "pinnedOrganizationUuid": org or "",
                }
            else:
                raw.pop("remoteControl", None)
            settings.write_text(_json.dumps(raw))
            raise RuntimeError("pin-proxy")

        sw = types.SimpleNamespace(
            backup_dir=backup,
            resolve_account=lambda a: ("2", "new@e.com", "org"),
            _account_kind=lambda n: "oauth",
        )
        real = pin._impl
        pin._impl = lambda: types.SimpleNamespace(apply_pin=_apply)
        try:
            ok, msg = pin.set_pin(sw, "new@e.com", "org", num="2")
        finally:
            pin._impl = real

        assert not ok
        assert pin._pinned_email_now(sw)[0] == "old@e.com", "the rollback failed"
        # The verdict must MATCH the record it just re-read.
        assert "may still name" not in msg, (
            "a successful rollback was reported as a failure — the reader and "
            f"the writer disagree on shape: {msg}"
        )
        assert "the previous pin is unchanged" in msg, msg

    def test_a_rollback_that_does_not_land_says_so(self, tmp_path):
        """The real case, not the shape mismatch above: the rollback
        ATTEMPT itself fails to reach the proxy, so the record never moves
        off the failed pin. `_restore_pin`'s verdict must come from re-reading
        the file, not from having made the call — mutating its last line to
        `return True` leaves the record naming `new@e.com` while the message
        claims the previous pin is unchanged.
        """
        import json as _json
        import types

        from claude_swap import pin

        backup = tmp_path / "b"
        backup.mkdir()
        settings = backup / "settings.json"
        settings.write_text(
            _json.dumps({"remoteControl": {"pinnedEmail": "old@e.com", "pinnedOrganizationUuid": "org"}})
        )

        class _I:
            n = 0

            def apply_pin(self, sw, email, org):
                _I.n += 1
                if _I.n == 1:
                    # The ORIGINAL set_pin call: writes the new pin, then the
                    # proxy dies.
                    settings.write_text(
                        _json.dumps(
                            {"remoteControl": {"pinnedEmail": email, "pinnedOrganizationUuid": org or ""}}
                        )
                    )
                    raise RuntimeError("proxy exploded")
                # THE ROLLBACK ATTEMPT (_restore_pin calling apply_pin with
                # `before`). It also fails to reach the proxy and must NOT be
                # believed just because it was called — the file is untouched.
                raise RuntimeError("rollback could not reach the proxy either")

        sw = types.SimpleNamespace(
            backup_dir=backup,
            resolve_account=lambda a: ("2", "new@e.com", "org"),
            _account_kind=lambda n: "oauth",
        )
        real = pin._impl
        pin._impl = lambda: _I()
        try:
            ok, msg = pin.set_pin(sw, "new@e.com", "org", num="2")
        finally:
            pin._impl = real

        assert not ok
        # THE FACT the mutation hides: the record never moved off the pin
        # that just failed.
        assert pin._pinned_email_now(sw)[0] == "new@e.com", (
            "fixture invalid: the rollback attempt actually wrote the file"
        )
        assert "the previous pin is unchanged" not in msg, (
            f"claimed the old pin survived while the record names new@e.com: {msg}"
        )
        assert "may still name new@e.com" in msg, msg


class TestTheTwoWiringPredicatesAgree:
    """"Is it wired" is asked in two places, and they must not disagree.

    ``_wiring_present`` gates the launch path and the TUI row;
    ``_clear_wiring_locked`` decides whether there is anything to remove. One
    accepted any truthy marker, the other required a non-empty list — so a
    malformed marker satisfied the first and not the second, and `--clear`
    reported "could not remove the wiring, re-run once it frees up" forever:
    nothing contended, nothing converging.
    """

    def _cfg(self, tmp_path, monkeypatch, mark):
        import json as _json

        import claude_swap.paths as paths

        cfg = tmp_path / ".claude.json"
        cfg.write_text(_json.dumps({"env": {"HTTPS_PROXY": "x"}, "_cswapPinWiredKeys": mark}))
        monkeypatch.setattr(paths, "get_global_config_path", lambda: cfg)
        monkeypatch.setattr(paths, "get_default_global_config_path", lambda: cfg)
        return cfg

    @pytest.mark.parametrize(
        "mark", ["NOT-A-LIST", [], {}, 7], ids=["str", "empty", "dict", "int"]
    )
    def test_a_malformed_marker_is_not_wired_to_either(
        self, tmp_path, monkeypatch, mark
    ):
        import types

        from claude_swap import pin

        self._cfg(tmp_path, monkeypatch, mark)
        sw = types.SimpleNamespace(backup_dir=tmp_path)
        assert pin._wiring_present(sw) is False, (
            f"{mark!r} reads as wired, but clear_wiring cannot remove it — "
            "the clear never converges"
        )
        assert pin.clear_wiring(sw) is False

    def test_a_real_marker_is_wired_to_both(self, tmp_path, monkeypatch):
        import types

        from claude_swap import pin

        self._cfg(tmp_path, monkeypatch, ["HTTPS_PROXY"])
        # _write_json is what the REAL switcher writes through; a stub
        # without it makes clear_wiring return False for a reason that has
        # nothing to do with the marker (the loop swallows the AttributeError).
        sw = types.SimpleNamespace(
            backup_dir=tmp_path,
            _write_json=lambda path, data: path.write_text(
                __import__("json").dumps(data), encoding="utf-8"
            ),
        )
        assert pin._wiring_present(sw) is True
        assert pin.clear_wiring(sw) is True
        assert pin._wiring_present(sw) is False, "the clear did not converge"


class TestPurgeDoesNotStrandTheWiring:
    """purge deletes backup_dir — the pin record, the cert dir, the daemon
    state — but .claude.json's env block is not in there, and Claude Code
    applies it at boot. Left behind it points every hand-launched `claude` at
    a dead port with nothing remaining that knows how to remove it: the exact
    stranding clear_wiring lives in this repo to prevent."""

    def _purge_with(self, tmp_path, monkeypatch, cfg, default_cfg=None):
        """Drive a real purge against ``cfg``. Returns stdout.

        NO STUB: `clear_wiring` is the real one, so the only way a test can
        make the unwire fail is the way the machine does it.

        ``default_cfg`` points the OTHER getter somewhere else. Both getters
        resolving to one file is the common shape and the one every test here
        used, which is exactly why a message that names only one of them read
        as correct.
        """
        import io
        from contextlib import redirect_stdout
        from unittest.mock import patch

        import claude_swap.paths as paths
        import claude_swap.switcher as _sw_mod
        from claude_swap.switcher import ClaudeAccountSwitcher

        monkeypatch.setattr(paths, "get_global_config_path", lambda: cfg)
        monkeypatch.setattr(
            paths, "get_default_global_config_path", lambda: default_cfg or cfg
        )
        # switcher.py imports the name at module scope, so patching the
        # module it came FROM does not reach it.
        monkeypatch.setattr(_sw_mod, "get_global_config_path", lambda: cfg)
        monkeypatch.setenv("HOME", str(tmp_path))

        sw = ClaudeAccountSwitcher()
        sw.backup_dir.mkdir(parents=True, exist_ok=True)
        buf = io.StringIO()
        with patch("builtins.input", return_value="y"), redirect_stdout(buf):
            sw.purge()
        return buf.getvalue()

    def test_purge_stops_the_daemon_while_its_state_is_still_on_disk(
        self, tmp_path, monkeypatch
    ):
        """UNWIRING IS NOT STOPPING, and only the unwire was here.

        `clear_wiring` touches "no proxy, no daemon and no credential — only a
        record cswap left", by its own docstring. So a purge that only unwires
        leaves the MITM proxy RUNNING: a live process holding OAuth bearers,
        still listening, after the user asked to remove ALL claude-swap data.

        Then `rmtree(backup_dir)` deletes `pin-proxy/` out from under it — the
        cert dir, `proxy.json`, the daemon state — and nothing left on the
        machine names that port. `cswap pin --clear` cannot find it and `kill`
        is the only cure left for a process the user has no way to identify.

        BEFORE THE RMTREE IS THE WHOLE FIX, so that is what this asserts: the
        teardown reads the state the rmtree deletes, and a stop ordered after
        it is a stop with nothing left to read.
        """
        from claude_swap import pin

        seen = []

        class _Impl:
            @staticmethod
            def apply_pin(switcher, email, org_uuid):
                # `backup_dir` IS what the rmtree takes, and `pin-proxy/` is
                # inside it — so its presence dates the call against the
                # deletion without seeding anything.
                seen.append((email, org_uuid, switcher.backup_dir.is_dir()))

        monkeypatch.setattr(pin, "_impl", lambda: _Impl)
        self._purge_with(tmp_path, monkeypatch, _cfg(tmp_path, "cfgdir", _dead_port()))

        assert seen, (
            "purge never asked the pin to stop: it unwired the configs and "
            "deleted the daemon's state, leaving the proxy listening"
        )
        assert seen[0][:2] == (None, None), (
            f"purge called apply_pin with {seen[0][:2]}, which pins rather "
            f"than unpins"
        )
        assert seen[0][2], (
            "the stop ran AFTER the rmtree had taken pin-proxy/ — by then "
            "there is no daemon state left to stop the daemon with"
        )

    @pytest.mark.skipif(
        sys.platform == "win32" or os.geteuid() == 0,
        reason="needs POSIX permission semantics (non-root): chmod is a no-op "
        "on win32 and root writes into a 0o500 dir regardless, so the lock "
        "never fails",
    )
    def test_a_FAILED_unwire_tells_the_user_instead_of_saying_complete(
        self, tmp_path, monkeypatch
    ):
        """"Absent" and "failed" are different, and only one is silent.

        `clear_wiring`'s bool is False for both, so a purge that trusts it
        prints "Purge complete." over a config that still carries the wiring —
        and with LESS recourse than before, since the record, cert dir and
        daemon state a later `cswap pin --clear` could have keyed off are gone
        by then. Hand-editing is the only cure and nothing says so.

        The failure is REAL, not injected: `proper_lockfile` mkdirs its lock
        beside the config, so a read-only config dir locks the unwire out on
        the same path a contended lock does.
        """
        cfg = _cfg(tmp_path, "cfgdir", _dead_port())
        assert "_cswapPinWiredKeys" in json.loads(cfg.read_text()), (
            "fixture is not wired — a before/after check whose BEFORE is empty "
            "reports success for the absence of what it measures"
        )
        cfg.parent.chmod(0o500)
        try:
            out = self._purge_with(tmp_path, monkeypatch, cfg)
        finally:
            cfg.parent.chmod(0o700)  # or tmp_path cleanup cannot remove it

        assert "_cswapPinWiredKeys" in json.loads(cfg.read_text()), (
            "fixture did not reach the stranded shape: the unwire succeeded"
        )
        assert "Could not remove the cloud pin wiring" in out, (
            "the purge reported success over a wiring it failed to remove"
        )
        # AND THE OTHER HALF OF THE SAME REPORT. The warning above and the
        # "Removed:" list are printed by one run and read together; a purge
        # that warns the wiring survived while listing it as removed
        # contradicts itself in the same breath, and the list is the half a
        # user skims. Mutation-checked: dropping `and not survivors` from the
        # append left every test here green.
        assert "Cloud pin wiring" not in out.split("Removed:")[-1], (
            "the purge listed the wiring as removed in the same output that "
            f"tells the user to delete it by hand: {out!r}"
        )
        # The path the code resolves, not where this test happened to write:
        # a session-scoped isolated_home fixture also sets HOME.
        assert str(cfg) in out, "the message does not name the file to edit"
        # THE ENV KEYS, not the marker. This asserted `"_cswapPinWiredKeys"`,
        # which a sidecar-era config never carries — so the advice was
        # unfollowable for exactly the wiring the current pin writes, and the
        # sidecar that lists the real keys is rmtree'd moments later.
        assert "HTTPS_PROXY" in out and "CSWAP_PIN_PORT" in out, (
            f"it does not name what to delete: {out!r}"
        )

    @pytest.mark.skipif(
        sys.platform == "win32" or os.geteuid() == 0,
        reason="needs POSIX permission semantics (non-root): chmod is a no-op "
        "on win32 and root writes into a 0o500 dir regardless",
    )
    def test_the_warning_names_the_config_that_actually_survived(
        self, tmp_path, monkeypatch
    ):
        """It named ONE file; the check that produced it reads TWO.

        `_wiring_present` answers about EITHER config, so the survivor can be
        the default global config while the message points at the session one
        — sending the user to a file that is already clean, and leaving the
        wiring that strands them in a file they were never told about. After
        a purge the record, cert dir and daemon state are gone, so hand-editing
        is the ONLY cure and naming the wrong file is the whole failure.

        Every existing test here points both getters at one path, which is the
        common deployment shape — and is why a message naming one of them read
        as correct for as long as it did.
        """
        session_cfg = _cfg(tmp_path, "sessiondir", marker=False)  # clean
        default_cfg = _cfg(tmp_path, "defaultdir", _dead_port())  # the survivor
        assert "_cswapPinWiredKeys" in json.loads(default_cfg.read_text())
        assert "_cswapPinWiredKeys" not in json.loads(session_cfg.read_text()), (
            "fixture invalid: BOTH are wired, so naming either would pass"
        )

        # Lock the unwire out of the survivor the way the machine does.
        default_cfg.parent.chmod(0o500)
        try:
            out = self._purge_with(
                tmp_path, monkeypatch, session_cfg, default_cfg=default_cfg
            )
        finally:
            default_cfg.parent.chmod(0o700)

        assert "_cswapPinWiredKeys" in json.loads(default_cfg.read_text()), (
            "fixture did not reach the stranded shape: the unwire succeeded"
        )
        assert "Could not remove the cloud pin wiring" in out, out
        assert str(default_cfg) in out, (
            f"the warning does not name the file that ACTUALLY still carries "
            f"the wiring ({default_cfg}) — the user edits the wrong file and "
            f"the stranding survives: {out!r}"
        )

    @pytest.mark.skipif(
        sys.platform == "win32" or os.geteuid() == 0,
        reason="needs POSIX permission semantics (non-root) to fail the unwire",
    )
    def test_the_advice_names_what_is_ACTUALLY_in_the_file(
        self, tmp_path, monkeypatch
    ):
        """SIDECAR-ERA WIRING, and the advice was written for the old shape.

        `_wire_mark_of` reads the receipt from the config OR the sidecar, so a
        wiring the CURRENT pin wrote — receipt in
        `<backup>/pin-wiring/<sha>.json`, nothing but env vars in the config —
        counts as wired. When the unwire then fails, purge tells the user to
        delete `"_cswapPinWiredKeys"` from that config: a key which is not in
        it, and never was.

        Naming a key the user cannot find is worse than naming none, because
        the rmtree below is about to delete the sidecar — the only record of
        WHICH env vars were cswap's and what they displaced. After that the
        advice is unfollowable and no tool can reconstruct it. Name the env
        keys themselves, while they can still be read.
        """
        from claude_swap.pin import _ledger_path

        port = _dead_port()
        cfgdir = tmp_path / "cfgdir"
        cfgdir.mkdir()
        cfg = cfgdir / ".claude.json"
        cfg.write_text(json.dumps({"env": {
            "HTTPS_PROXY": f"http://127.0.0.1:{port}",
            "CSWAP_PIN_PORT": str(port),
        }}))  # no receipt here — the current pin does not write one
        monkeypatch.setenv("HOME", str(tmp_path))
        side = _ledger_path(cfg)
        side.parent.mkdir(parents=True, exist_ok=True)
        side.write_text(json.dumps({
            "_cswapPinWiredKeys": ["HTTPS_PROXY", "CSWAP_PIN_PORT"],
            "_cswapPinWiredKeysSaved": {"HTTPS_PROXY": "http://127.0.0.1:9901"},
        }))
        assert "_cswapPinWiredKeys" not in cfg.read_text(), (
            "fixture put the receipt in the config — that is the OLD shape, "
            "and the advice under test happens to be correct for it"
        )

        cfg.parent.chmod(0o500)
        try:
            out = self._purge_with(tmp_path, monkeypatch, cfg)
        finally:
            cfg.parent.chmod(0o700)

        # The dir is unwritable, so the wiring CANNOT go — that is the premise,
        # not the defect. The defect is what purge then says about it.
        left = json.loads(cfg.read_text()).get("env", {})
        assert "CSWAP_PIN_PORT" in left, (
            f"fixture did not reach the stranded shape — the unwire "
            f"succeeded: {left!r}"
        )
        assert "Removed:\n" not in out or "Cloud pin wiring" not in out.split(
            "Removed:"
        )[-1], (
            "purge claimed the wiring was removed while it is still in the "
            f"file. Clearing the SIDECAR made the config read as unwired, so "
            f"the survivor check — which asked for the marker it had just "
            f"deleted — saw nothing to warn about:\n\n{out}"
        )
        assert "Could not remove the cloud pin wiring" in out, (
            f"stranded silently: no warning at all.\n\n{out}"
        )
        assert "HTTPS_PROXY" in out and "CSWAP_PIN_PORT" in out, (
            f"the advice does not name the env keys the user must delete, and "
            f"the receipt that listed them is about to be rmtree'd: {out!r}"
        )

    def test_an_unwired_config_says_nothing(self, tmp_path, monkeypatch):
        """...and the silent case must stay silent: nothing wired, nothing to
        report. A warning here cries wolf on every ordinary purge."""
        cfg = _cfg(tmp_path, "cfgdir", marker=False)
        out = self._purge_with(tmp_path, monkeypatch, cfg)
        assert "cloud pin wiring" not in out.lower(), out

    def test_purge_unwires_before_it_deletes(self, tmp_path, monkeypatch):
        cfg = _cfg(tmp_path, "cfgdir", _dead_port())
        self._purge_with(tmp_path, monkeypatch, cfg)

        raw = json.loads(cfg.read_text())
        assert "_cswapPinWiredKeys" not in raw, (
            "purge left the pin wiring behind: every hand-launched claude "
            "now dials a port nothing serves"
        )
        assert "HTTPS_PROXY" not in raw.get("env", {})


class TestClearRunsWithTheExtraGone:
    """`cswap pin --clear` is priority 1 and the whole reason clear_wiring
    lives in cswap rather than the optional package.

    An AST scan proves nothing about it: a review defeated the import-time
    guards by adding a runtime `from cswap_pin.proxy import ...` inside
    pin.run(), and every test stayed green while `--clear` died with a
    ModuleNotFoundError. Drive the real command with cswap_pin blocked at
    sys.meta_path — the one form that also stops importlib.import_module.
    """

    def test_the_marker_still_matches_the_package_that_writes_it(self, tmp_path):
        """cswap READS a key cswap-pin WRITES, and the two version
        independently. Agreeing on a magic string by convention is this seam's
        one silent-drift risk: rename it there and `--clear` stops finding
        wirings while still reporting 'No cloud account pinned'.

        The port setting is the same shape of agreement in the other
        direction: cswap WRITES `settings.json` from `--set_port` and
        `proxy.configured_port` is what carries it to `bind()`. Both live here
        because both are "two installed packages agreeing", and neither can be
        checked at all without the extra.
        """
        import types

        # A HARD IMPORT, and the skip it replaces is the finding. Under
        # `importorskip` this test reported "skipped" on every job on every
        # platform — `uv sync --locked` installs base + dev, and the extra was
        # only under `[project.optional-dependencies]` — and, measured, in the
        # maintainer's checkout too. It had never run once, so the one check
        # standing between a rename in cswap-pin and `--clear` silently
        # finding nothing was decorative. `claude-swap[pin]` is in the dev
        # group now; if it ever leaves, this must fail rather than go quiet
        # again, which is the whole reason the skip is gone.
        from cswap_pin import proxy

        from claude_swap import pin
        from claude_swap.pin import _WIRE_MARK

        assert proxy._WIRE_MARK == _WIRE_MARK

        backup = tmp_path / "backup"
        (backup / "pin-proxy").mkdir(parents=True)
        sw = types.SimpleNamespace(
            backup_dir=backup,
            _write_json=lambda p, d: p.write_text(json.dumps(d), encoding="utf-8"),
        )
        assert pin.run(sw, None, set_port=44444) == 0
        assert proxy.configured_port(backup / "pin-proxy") == 44444, (
            "the package could not read the port cswap just set — the two "
            "sides of --set_port have drifted apart"
        )

    def test_clear_removes_the_wiring_with_cswap_pin_blocked(self, tmp_path):
        import subprocess
        import textwrap
        from pathlib import Path

        src = str(Path(__file__).resolve().parent.parent / "src")
        cfg = tmp_path / ".claude.json"
        cfg.write_text(
            json.dumps(
                {
                    "env": {"HTTPS_PROXY": f"http://127.0.0.1:{_dead_port()}", "K": "v"},
                    "_cswapPinWiredKeys": ["HTTPS_PROXY"],
                }
            )
        )
        code = textwrap.dedent(
            f"""
            import sys
            sys.path.insert(0, {src!r})
            class Block:
                def find_spec(self, name, path=None, target=None):
                    if name.split(".")[0] == "cswap_pin":
                        raise ImportError("blocked", name=name)
                    return None
            sys.meta_path.insert(0, Block())
            from pathlib import Path
            import claude_swap.paths as paths
            cfg = Path({str(cfg)!r})
            paths.get_global_config_path = lambda: cfg
            paths.get_default_global_config_path = lambda: cfg
            from claude_swap import pin
            from claude_swap.switcher import ClaudeAccountSwitcher
            sys.exit(pin.run(ClaudeAccountSwitcher(), None, clear=True))
            """
        )
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[-900:]
        raw = json.loads(cfg.read_text())
        assert "_cswapPinWiredKeys" not in raw
        assert raw["env"] == {"K": "v"}, "the wiring outlived the uninstall"


class TestAFailedClearIsNotReportedAsSuccess:
    """`--clear` must not say "Unpinned" while the pin survives.

    The failure is silent by construction: the success message was gated on
    clear_wiring(), which reports on the .claude.json wiring and never on the
    pin itself, so every way of failing printed "Unpinned" over a live pin.

    The pin record is cswap's OWN file, so these drive it through that file
    rather than through a stubbed package. That is the point of the fix: an
    earlier version asked the package "is it still pinned", which cannot answer
    when the package is the thing that is broken.
    """

    def _run(self, tmp_path, impl_src):
        import subprocess
        import textwrap
        from pathlib import Path

        src = str(Path(__file__).resolve().parent.parent / "src")
        cfg = tmp_path / ".claude.json"
        cfg.write_text(json.dumps({"env": {}}))
        backup = tmp_path / "backup"
        backup.mkdir()
        # A real pin record, written the way cswap writes one.
        (backup / "settings.json").write_text(
            json.dumps({"remoteControl": {"pinnedEmail": "cloud@example.com"}}, indent=2)
        )
        code = (
            textwrap.dedent(
                f"""
                import sys
                sys.path.insert(0, {src!r})
                from pathlib import Path
                import claude_swap.paths as paths
                cfg = Path({str(cfg)!r})
                paths.get_global_config_path = lambda: cfg
                paths.get_default_global_config_path = lambda: cfg
                from claude_swap import pin
                """
            )
            + impl_src
            + textwrap.dedent(
                f"""
                pin._impl = _impl_factory
                from claude_swap.switcher import ClaudeAccountSwitcher
                sw = ClaudeAccountSwitcher()
                sw.backup_dir = Path({str(backup)!r})
                sys.exit(pin.run(sw, None, clear=True))
                """
            )
        )
        return subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True
        )

    def test_an_unusable_package_still_clears_the_record(self, tmp_path):
        """`--clear` must CONVERGE when the package cannot help.

        The record is cswap's own file (settings.json -> remoteControl), so an
        unusable package is no reason to leave it. Leaving it made --clear fail,
        tell the user to REINSTALL the package they had just removed, never
        converge on a re-run, and re-pin the old account live the moment
        anything reinstalled it.
        """
        impl = (
            "def _impl_factory():\n"
            "    raise ImportError('cryptography')\n"
        )
        r = self._run(tmp_path, impl)
        assert "Unpinned" in r.stdout, r.stdout + r.stderr[-400:]
        assert r.returncode == 0, "a clear that converged must not exit 1"

    def test_a_clear_that_leaves_the_record_is_a_failure(self, tmp_path):
        """The control: when the record genuinely survives, say so."""
        impl = (
            "class _I:\n"
            "    def apply_pin(self, *a): raise OSError('disk full')\n"
            "def _impl_factory(): return _I()\n"
            # the record cannot be cleared either
            "import claude_swap.pin as _p\n"
            "_p._clear_pin_record = lambda *a: None\n"
        )
        r = self._run(tmp_path, impl)
        assert "Unpinned" not in r.stdout
        assert "Could not remove" in r.stdout, r.stdout + r.stderr[-400:]
        assert r.returncode == 1

    def test_a_real_clear_still_reports_success(self, tmp_path):
        """The control: the message must be right exactly when it worked."""
        impl = (
            "import json as _j\n"
            "class _I:\n"
            "    def apply_pin(self, sw, *a):\n"
            "        (sw.backup_dir / 'settings.json').write_text(_j.dumps({}))\n"
            "def _impl_factory(): return _I()\n"
        )
        r = self._run(tmp_path, impl)
        assert "Unpinned" in r.stdout, r.stdout + r.stderr[-400:]
        assert r.returncode == 0


class TestTheSetPathIsAsHonestAsTheClearPath:
    """`cswap pin NUM` must not report a pin that is not in effect.

    apply_pin writes the record BEFORE it starts the proxy, so both failures
    here leave a pin that `cswap pin` and the TUI badge report as live while
    nothing serves it.
    """

    def _run(self, tmp_path, impl_src):
        import subprocess
        import textwrap
        from pathlib import Path

        src = str(Path(__file__).resolve().parent.parent / "src")
        backup = tmp_path / "backup"
        backup.mkdir()
        code = (
            textwrap.dedent(
                f"""
                import sys
                sys.path.insert(0, {src!r})
                from pathlib import Path
                from claude_swap import pin
                """
            )
            + impl_src
            + textwrap.dedent(
                f"""
                pin._impl = _impl_factory
                class _SW:
                    backup_dir = Path({str(backup)!r})
                    def resolve_account(self, a):
                        return (2, "user2@example.com", "org-uuid")
                    def _account_kind(self, n):
                        return "oauth"
                sys.exit(pin.run(_SW(), "2"))
                """
            )
        )
        return subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True
        )

    def test_a_raising_apply_pin_is_not_a_traceback(self, tmp_path):
        # Real trigger, no injection: <backup>/pin-proxy as a plain FILE makes
        # ensure_proxy's certdir.mkdir raise FileExistsError, which is not a
        # ClaudeSwitchError and so reached the user as a traceback.
        impl = (
            "class _I:\n"
            "    def apply_pin(self, *a): raise FileExistsError('pin-proxy')\n"
            "def _impl_factory(): return _I()\n"
        )
        r = self._run(tmp_path, impl)
        assert "Traceback" not in r.stderr, r.stderr[-400:]
        assert "Pinned" not in r.stdout, "reported a pin that did not happen"
        assert "Could not pin" in r.stdout, r.stdout + r.stderr[-300:]
        assert r.returncode == 1

    def test_no_proxy_serving_is_not_unqualified_success(self, tmp_path):
        # apply_pin returning False means no proxy is serving. Suppressing the
        # follow-up note was the only signal; the word "Pinned" still went out.
        impl = (
            "class _I:\n"
            "    def apply_pin(self, *a): return False\n"
            "def _impl_factory(): return _I()\n"
        )
        r = self._run(tmp_path, impl)
        assert "nothing is pinned yet" in r.stdout, r.stdout + r.stderr[-300:]
        assert r.returncode == 1, "a pin nothing serves must not exit 0"

    def test_no_proxy_serving_ROLLS_BACK_the_record(self, tmp_path):
        """`started == False` must undo the record, like the raise path does.

        apply_pin writes ``remoteControl`` BEFORE it starts the proxy, so
        leaving it made the two commands contradict each other: `cswap pin 2`
        said "nothing is pinned yet" and exited 1, then `cswap pin` printed
        the address and exited 0 with the ○ cloud badge lit.

        The stub writes the record for real — a stub that only returns False
        cannot show the bug at all.
        """
        impl = (
            "import json\n"
            "from pathlib import Path as _P\n"
            "class _I:\n"
            "    def apply_pin(self, sw, email, org):\n"
            "        p = _P(sw.backup_dir) / 'settings.json'\n"
            "        raw = json.loads(p.read_text()) if p.exists() else {}\n"
            "        if email:\n"
            "            raw['remoteControl'] = {'pinnedEmail': email,\n"
            "                                    'pinnedOrganizationUuid': org or ''}\n"
            "        else:\n"
            "            raw.pop('remoteControl', None)\n"
            "        p.parent.mkdir(parents=True, exist_ok=True)\n"
            "        p.write_text(json.dumps(raw))\n"
            "        return False\n"
            "    def load_pin(self, *a): return None\n"
            "def _impl_factory(): return _I()\n"
        )
        r = self._run(tmp_path, impl)
        assert r.returncode == 1, r.stdout + r.stderr[-300:]

        import json as _json

        settings = tmp_path / "backup" / "settings.json"
        raw = _json.loads(settings.read_text()) if settings.exists() else {}
        assert "remoteControl" not in raw, (
            "the failed pin left a record the badge and `cswap pin` both read "
            f"as live: {raw.get('remoteControl')!r}"
        )


class TestTheExtraIsGatedByOneFloorOnly:
    """The extra's version floor lives in pyproject, and NOWHERE else.

    ONE floor, at INSTALL time. A hardcoded `_MIN_PIN_VERSION` tuple in
    `pin.py` would refuse an older cswap-pin at import time, and that is the
    gate this class asserts stays gone: it re-litigates on every call, and it
    refuses a package the installer has just chosen while blaming the user's
    version. The install-time requirement is the right place, and it is where
    the floor now lives — see
    `test_the_extra_floor_matches_what_the_lockfile_resolved`.

    This is exactly how the sibling extra behaves: `menubar = ["rumps>=0.4.0"]`
    in pyproject, and `menubar.py` asks only whether the import works. The
    difference between the two gates is WHEN they run, not whether a floor is
    allowed to exist.

    WINDOWS REFUSES BEFORE ANYTHING ELSE. `_impl` raises on win32 first (POSIX
    locks and FIFOs), and the failure that taught this was the quiet kind: an
    older test's bare `"REFUSED" in out` passed there for the PLATFORM, not for
    the reason it named. So Windows is asserted separately rather than skipped.
    """

    WIN = sys.platform == "win32"

    def _probe(self, version_literal):
        """Run `_impl()` against a synthetic cswap_pin carrying any version."""
        import subprocess
        import textwrap
        from pathlib import Path

        src = str(Path(__file__).resolve().parent.parent / "src")
        code = textwrap.dedent(
            f"""
            import sys, types
            sys.path.insert(0, {src!r})
            pkg = types.ModuleType("cswap_pin")
            pkg.__path__ = []
            {version_literal}
            proxy = types.ModuleType("cswap_pin.proxy")
            sys.modules["cswap_pin"] = pkg
            sys.modules["cswap_pin.proxy"] = proxy
            import importlib.util
            real = importlib.util.find_spec
            importlib.util.find_spec = lambda n, *a, **k: (
                object() if n.startswith("cswap_pin") else real(n, *a, **k))
            import importlib
            importlib.import_module = lambda n, *a, **k: sys.modules[n]
            from claude_swap import pin
            try:
                pin._impl()
                print("ACCEPTED")
            except Exception as e:
                print("REFUSED:", e)
            """
        )
        return subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True
        ).stdout

    def test_the_extra_floor_matches_what_the_lockfile_resolved(self):
        """The floor exists, and it agrees with `uv.lock`.

        THE FLOOR CAME BACK, deliberately. This test used to assert the extra
        named no version at all, on the argument that a constant somebody has
        to remember to raise goes stale — measured: `cswap-pin>=0.1.3` sat
        unchanged through THIRTY-SIX releases, so `uv tool install
        'claude-swap[pin]'` resolved a pin from before the port handdown, the
        chain walk and the launch hook while every install looked fine.

        That argument reasoned as though cswap-pin were somebody else's
        package. It is ours. The 0.1.3 failure happened because nobody raised
        it, not because floors are wrong, and dropping the floor did not fix
        the underlying problem — it moved it into the lockfile, where
        `uv.lock` quietly held cswap-pin 0.1.37, thirty-one releases behind,
        with a requirement that looked satisfied.

        SO THE INVARIANT IS AGREEMENT, not a magic number. The floor must
        equal the version the lock actually resolved. That catches the two
        ways this goes wrong offline and without a network call:

          - raising the requirement without relocking. Measured today: six
            red jobs on this PR, every one `uv sync --locked` refusing before
            a test ran.
          - a lock that drifts below the floor, which is the 0.1.37 shape.

        Staleness against the newest RELEASE is not checkable here and is not
        pretended to be — that is the release checklist's job, and pyproject
        says so beside the pin.
        """
        import re
        import tomllib
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        raw = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        specs = raw["project"]["optional-dependencies"]["pin"]
        assert len(specs) == 1, f"expected one pin requirement, got {specs!r}"

        m = re.fullmatch(r"cswap-pin>=(\d+(?:\.\d+)*)", specs[0])
        assert m, (
            f"the pin extra must name a `>=` floor on cswap-pin, got "
            f"{specs[0]!r}. The floor is what forces the relock that moves "
            f"the pinned version; without it the lock silently keeps an old "
            f"one (measured: 0.1.37, thirty-one releases behind)."
        )
        floor = m.group(1)

        lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
        locked = next(
            (p.get("version") for p in lock.get("package", [])
             if p.get("name") == "cswap-pin"),
            None,
        )
        assert locked is not None, "cswap-pin is not in uv.lock at all"
        assert locked == floor, (
            f"the pin floor is {floor} but uv.lock resolved {locked}. Raise "
            f"one without the other and CI fails at `uv sync --locked` before "
            f"any test runs — run `uv lock` after changing the requirement."
        )

    def test_no_version_is_refused_at_runtime(self):
        """Any installed version imports. Refusing one here would need a
        constant this project cannot keep current."""
        for literal in (
            'pkg.__version__ = "0.1.0"',
            'pkg.__version__ = "0.0.1"',
            "pass",  # a dev checkout with no __version__ at all
        ):
            out = self._probe(literal)
            expected = "not available on Windows" if self.WIN else "ACCEPTED"
            assert expected in out, f"{literal!r} -> {out}"
            assert "too old" not in out, f"a runtime floor came back: {out}"

    def test_the_seam_holds_no_version_constant(self):
        """Asserts the ABSENCE, because the constant is easy to reintroduce and
        the cost lands on a future release rather than on the commit."""
        from pathlib import Path

        src = (
            Path(__file__).resolve().parent.parent
            / "src"
            / "claude_swap"
            / "pin.py"
        ).read_text(encoding="utf-8")
        assert "_MIN_PIN_VERSION" not in src, (
            "a runtime version floor is back; raising it needs an upstream PR "
            "per cswap-pin release"
        )

    def test_windows_is_still_refused_for_the_platform(self):
        """Unreachable elsewhere, and the OS where this seam is most likely to
        drift — so it is asserted rather than skipped."""
        if not self.WIN:
            pytest.skip("asserted on Windows only; POSIX path covered above")
        out = self._probe("pass")
        assert "not available on Windows" in out, out


class TestAnActionReportedDoneMustReReadWhatItChanged:
    """An action reported as done while the state it claims to have changed is
    unchanged. A return value cannot carry that — "nothing to do" and "could
    not do it" collapse into the same False — so each of these re-reads the
    thing it just claimed. Each drives the seam, not a stub.
    """

    def _cli(self, tmp_path, impl_src, argv_account=None, clear=False, wired=True):
        import subprocess
        import textwrap
        from pathlib import Path

        src = str(Path(__file__).resolve().parent.parent / "src")
        cfg = tmp_path / ".claude.json"
        cfg.write_text(
            json.dumps(
                {
                    "env": {"HTTPS_PROXY": f"http://127.0.0.1:{_dead_port()}"},
                    "_cswapPinWiredKeys": ["HTTPS_PROXY"],
                }
                if wired
                else {"env": {}}
            )
        )
        backup = tmp_path / "backup"
        backup.mkdir()
        (backup / "settings.json").write_text(
            json.dumps({"remoteControl": {"pinnedEmail": "cloud@example.com"}}, indent=2)
        )
        code = (
            textwrap.dedent(
                f"""
                import sys
                sys.path.insert(0, {src!r})
                from pathlib import Path
                import claude_swap.paths as paths
                cfg = Path({str(cfg)!r})
                paths.get_global_config_path = lambda: cfg
                paths.get_default_global_config_path = lambda: cfg
                from claude_swap import pin
                """
            )
            + impl_src
            + textwrap.dedent(
                f"""
                pin._impl = _impl_factory
                class _SW:
                    backup_dir = Path({str(backup)!r})
                    def resolve_account(self, a):
                        return (2, "user2@example.com", "org-uuid")
                    def _account_kind(self, n):
                        return "oauth"
                sys.exit(pin.run(_SW(), {argv_account!r}, clear={clear!r}))
                """
            )
        )
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        return r, cfg, backup

    def test_a_clear_that_cannot_remove_the_wiring_is_a_failure(self, tmp_path):
        # apply_pin succeeds (the pin goes), but the wiring cannot be removed.
        # Reported as success, the daemon idles out and every hand-launched
        # claude dials a dead port — the stranding clear_wiring exists to stop.
        impl = (
            "import json as _j\n"
            "class _I:\n"
            "    def apply_pin(self, sw, *a):\n"
            "        (sw.backup_dir / 'settings.json').write_text(_j.dumps({}))\n"
            "def _impl_factory(): return _I()\n"
        )
        # Make clear_wiring a no-op so the wiring survives, as a held lock does.
        impl += "pin.clear_wiring = lambda *a, **k: False\n"
        r, cfg, _ = self._cli(tmp_path, impl, clear=True)
        assert "Unpinned" not in r.stdout, r.stdout
        assert "Could not remove" in r.stdout, r.stdout + r.stderr[-300:]
        assert r.returncode == 1
        assert "_cswapPinWiredKeys" in cfg.read_text(), "fixture no longer valid"

    def test_a_failed_set_rolls_the_record_back(self, tmp_path):
        # apply_pin writes the record before starting the proxy, so reporting
        # the failure is not enough: `cswap pin` reads it back and calls it
        # live, and the TUI badge agrees.
        impl = (
            "import json as _j\n"
            "class _I:\n"
            "    calls = []\n"
            "    def apply_pin(self, sw, email, org):\n"
            "        _I.calls.append(email)\n"
            "        if email is not None and len(_I.calls) == 1:\n"
            "            (sw.backup_dir / 'settings.json').write_text(\n"
            "                _j.dumps({'remoteControl': {'pinnedEmail': email}}))\n"
            "            raise FileExistsError('pin-proxy')\n"
            "        (sw.backup_dir / 'settings.json').write_text(_j.dumps({}))\n"
            "def _impl_factory(): return _I()\n"
        )
        r, _, backup = self._cli(tmp_path, impl, argv_account="2")
        assert "Could not pin" in r.stdout, r.stdout + r.stderr[-300:]
        assert r.returncode == 1
        raw = json.loads((backup / "settings.json").read_text())
        assert not raw.get("remoteControl", {}).get("pinnedEmail"), (
            "the failed pin stayed in the record; `cswap pin` would call it live"
        )

    def test_an_api_key_account_is_refused(self, tmp_path):
        impl = (
            "class _I:\n"
            "    def apply_pin(self, *a): raise AssertionError('must not be reached')\n"
            "def _impl_factory(): return _I()\n"
        )
        import subprocess
        import textwrap
        from pathlib import Path

        src = str(Path(__file__).resolve().parent.parent / "src")
        backup = tmp_path / "b"
        backup.mkdir()
        (backup / "settings.json").write_text("{}")
        code = (
            textwrap.dedent(
                f"""
                import sys
                sys.path.insert(0, {src!r})
                from pathlib import Path
                from claude_swap import pin
                from claude_swap.exceptions import ClaudeSwitchError
                """
            )
            + impl
            + textwrap.dedent(
                f"""
                pin._impl = _impl_factory
                class _SW:
                    backup_dir = Path({str(backup)!r})
                    def resolve_account(self, a):
                        return (3, "key@example.com", "org")
                    def _account_kind(self, n):
                        return "api_key"
                rc = pin.run(_SW(), "3")
                print("ACCEPTED" if rc == 0 else "REFUSED")
                """
            )
        )
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True
        ).stdout
        assert "REFUSED" in out, f"an API-key account was pinned: {out}"
        assert "API-key account" in out, out

    def test_one_place_decides_the_install_command(self):
        """A second hardcoded hint diverged from the derived one on pipx."""
        from pathlib import Path

        import ast

        src = Path(
            str(Path(__file__).resolve().parent.parent / "src")
        ).joinpath("claude_swap/pin.py")
        tree = ast.parse(src.read_text(encoding="utf-8"))

        # STRING CONSTANTS ONLY. The prose in docstrings names these commands
        # while explaining why there is one decider, so counting raw text
        # would forbid documenting the rule. What must not repeat is a literal
        # the code can PRINT.
        allowed = set()
        decider = "_install_hint"
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == decider:
                allowed = {id(n) for n in ast.walk(node)}
        assert allowed, (
            f"{decider}() is gone — this guard keys on it by name, so a rename "
            f"must move the name here rather than leave the check vacuous"
        )
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in allowed or ast.get_docstring(tree) == node.value:
                continue
            if any(
                f in node.value
                for f in ("uv tool install", "pipx install", "pip install")
            ):
                # A docstring is prose, not something the code emits.
                offenders.append(node.lineno)
        # Drop the lines that ARE docstrings.
        docs = {
            n.body[0].lineno
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module))
            and n.body
            and isinstance(n.body[0], ast.Expr)
            and isinstance(n.body[0].value, ast.Constant)
            and isinstance(n.body[0].value.value, str)
        }
        offenders = [ln for ln in offenders if ln not in docs]
        assert not offenders, (
            f"install command literal outside {decider}() at line(s) {offenders} "
            "— two places decide it, and they diverged on pipx once already"
        )


class TestTheVerdictIsSharedNotDuplicated:
    """clear_pin/set_pin are the one place the outcome is decided.

    One decision implemented twice diverges: a fix lands on the CLI and the
    TUI's sibling call site kept the old behaviour. These assert the shared
    functions themselves, so a future divergence needs someone to write a
    second copy rather than to forget a line.
    """

    def _sw(self, tmp_path, pinned="cloud@example.com", wired=True):
        import types

        backup = tmp_path / "b"
        backup.mkdir()
        (backup / "settings.json").write_text(
            json.dumps({"remoteControl": {"pinnedEmail": pinned}} if pinned else {})
        )
        cfg = tmp_path / ".claude.json"
        cfg.write_text(
            json.dumps(
                {"env": {"HTTPS_PROXY": "x"}, "_cswapPinWiredKeys": ["HTTPS_PROXY"]}
                if wired
                else {"env": {}}
            )
        )
        # resolve_account/_account_kind are what the REAL switcher offers, and
        # set_pin now checks the account kind before it touches the pin. A
        # stub without them made set_pin bail at the first line, so the
        # rollback tests below passed with apply_pin never called — green with
        # nothing behind them (confirmed: they survived deleting _restore_pin).
        return types.SimpleNamespace(
            backup_dir=backup,
            resolve_account=lambda a: ("2", "user2@example.com", "org"),
            _account_kind=lambda n: "oauth",
        ), cfg

    def test_clear_pin_fails_when_the_wiring_survives(self, tmp_path, monkeypatch):
        import claude_swap.paths as paths

        from claude_swap import pin

        sw, cfg = self._sw(tmp_path)
        monkeypatch.setattr(paths, "get_global_config_path", lambda: cfg)
        monkeypatch.setattr(paths, "get_default_global_config_path", lambda: cfg)

        class _I:
            def apply_pin(self, s, *a):
                (s.backup_dir / "settings.json").write_text("{}")

        monkeypatch.setattr(pin, "_impl", lambda: _I())
        # The lock is contended, so clear_wiring skips the path and returns
        # False — indistinguishable from "nothing to remove" by its return.
        monkeypatch.setattr(pin, "clear_wiring", lambda *a, **k: False)
        ok, msg = pin.clear_pin(sw)
        assert not ok, msg
        assert "wiring" in msg, msg

    def test_clear_pin_converges_against_a_peer_that_silently_does_nothing(
        self, tmp_path, monkeypatch
    ):
        """`--clear` MUST NOT TELL THE USER TO RE-RUN A COMMAND THAT CANNOT WORK.

        The record is cleared here only in the `except` branch, on the
        reasoning — spelled out in that branch's own comment — that advice
        which "never converges (run 2 is identical)" is worse than useless.
        A peer whose `apply_pin` RETURNS WITHOUT RAISING and clears nothing
        reaches the same dead end without going through the except at all.

        Measured against this code before the fix:
            run 1: False 'Could not remove the pin — re-run once it frees up'
                   record: a@b.c
            run 2: False 'Could not remove the pin — re-run once it frees up'
                   record: a@b.c

        Identical forever, and the record is cswap's OWN file the whole time.
        A released peer that no-ops on `apply_pin(sw, None, None)` — an older
        one, or one whose pin backend is disabled — leaves the user unable to
        unpin by any documented means.

        The failure is what the peer DID, not whether it raised, so the
        re-read that already runs is what should drive the fallback.

        THE CONTROL is a peer that clears properly, which must NOT have its
        record touched by the fallback path — otherwise "converges" would pass
        for a `--clear` that ignores the peer entirely.
        """
        import types

        from claude_swap import pin

        state = {"pinned": "a@b.c", "record_cleared": 0}

        def _run(peer_clears):
            state["pinned"] = "a@b.c"
            monkeypatch.setattr(
                pin, "_impl",
                lambda: types.SimpleNamespace(
                    apply_pin=lambda *a, **k: (
                        state.update(pinned=None) if peer_clears else None
                    )
                ),
            )
            monkeypatch.setattr(pin, "clear_wiring", lambda *a, **k: False)
            monkeypatch.setattr(pin, "_wiring_present", lambda *a, **k: False)
            monkeypatch.setattr(
                pin, "_pinned_email_now", lambda sw: state["pinned"]
            )
            monkeypatch.setattr(
                pin, "_clear_pin_record",
                lambda sw: state.update(
                    pinned=None, record_cleared=state["record_cleared"] + 1
                ),
            )
            return pin.clear_pin(object())

        # CONTROL: a peer that really clears must succeed on its own.
        before = state["record_cleared"]
        ok, msg = _run(peer_clears=True)
        assert ok, f"CONTROL FAILED: a working peer could not unpin: {msg}"
        assert state["record_cleared"] == before, (
            "the fallback ran even though the peer had already cleared the "
            "record — `--clear` is ignoring the peer"
        )

        ok, msg = _run(peer_clears=False)
        assert ok, (
            f"a peer that silently did nothing left the pin in place and told "
            f"the user to re-run: {msg!r} — run 2 is identical, forever"
        )
        assert state["pinned"] is None, "the record survived a successful clear"

    def test_clear_pin_succeeds_when_both_are_gone(self, tmp_path, monkeypatch):
        import claude_swap.paths as paths

        from claude_swap import pin

        sw, cfg = self._sw(tmp_path, wired=False)
        monkeypatch.setattr(paths, "get_global_config_path", lambda: cfg)
        monkeypatch.setattr(paths, "get_default_global_config_path", lambda: cfg)

        class _I:
            def apply_pin(self, s, *a):
                (s.backup_dir / "settings.json").write_text("{}")

        monkeypatch.setattr(pin, "_impl", lambda: _I())
        ok, msg = pin.clear_pin(sw)
        assert ok and "Unpinned" in msg, msg

    def test_set_pin_rolls_back_on_failure(self, tmp_path, monkeypatch):
        from claude_swap import pin

        sw, _ = self._sw(tmp_path, pinned=None)

        class _I:
            n = 0

            def apply_pin(self, s, email, org):
                _I.n += 1
                if _I.n == 1:
                    (s.backup_dir / "settings.json").write_text(
                        json.dumps({"remoteControl": {"pinnedEmail": email}})
                    )
                    raise FileExistsError("pin-proxy")
                (s.backup_dir / "settings.json").write_text("{}")

        monkeypatch.setattr(pin, "_impl", lambda: _I())
        ok, msg = pin.set_pin(sw, "user2@example.com", "org")
        assert not ok, msg
        assert pin._pinned_email_now(sw) is None, (
            "the failed pin stayed recorded; every read-back would call it live"
        )


class TestTheCliRendersABrokenPackageHonestly:
    """A broken package must reach the user as advice, not a traceback, and
    the advice must not promise more than the code does. A guard nothing
    asserts is a guard someone deletes."""

    def test_the_cli_renders_a_broken_package_instead_of_a_traceback(self, tmp_path):
        import subprocess
        import textwrap
        from pathlib import Path

        src = str(Path(__file__).resolve().parent.parent / "src")
        code = textwrap.dedent(
            f"""
            import sys
            sys.path.insert(0, {src!r})
            from claude_swap import pin
            # A broken package ROOT: _impl re-raises the underlying ImportError
            # on purpose, and nothing between there and the shell rendered it.
            pin.run = lambda *a, **k: (_ for _ in ()).throw(
                ImportError("No module named 'cryptography'", name="cryptography"))
            from claude_swap import cli
            sys.argv = ["cswap", "pin", "2"]
            try:
                cli._pin_command(["2"])
            except SystemExit as e:
                print("EXIT", e.code)
            """
        )
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        combined = r.stdout + r.stderr
        assert "Traceback" not in combined, combined[-500:]
        assert "not usable" in combined, combined[-500:]
        assert "EXIT 1" in combined, combined[-300:]

    def test_the_launch_unwire_is_bounded_by_the_budget(self, monkeypatch):
        """The unwire took the package's own 5s lock, unbounded by the budget
        the no-package branch gets. Assert the PROBE gates it."""
        import types

        from claude_swap import pin

        calls = []
        impl = types.SimpleNamespace(
            ensure_proxy=lambda sw: None,
            unwire_if_dead=lambda p: calls.append("unwired"),
        )
        monkeypatch.setattr(pin, "_impl", lambda: impl)
        monkeypatch.setattr(pin, "_config_lock_is_free", lambda b: False)
        sw = types.SimpleNamespace(backup_dir=__import__("pathlib").Path("/tmp"))
        assert pin.wire_launch_env(sw, {"A": "1"}) == {"A": "1"}
        assert calls == [], "the unwire ran while the lock was held"

        monkeypatch.setattr(pin, "_config_lock_is_free", lambda b: True)
        pin.wire_launch_env(sw, {"A": "1"})
        assert calls == ["unwired"], "the unwire never runs, even when free"

    def test_the_broken_package_advice_does_not_promise_an_unconditional_clear(
        self, tmp_path
    ):
        """Finding 3 makes a contended config lock a reachable reason
        `clear_wiring` skips a config it never got to try. The catch-all
        advice printed alongside a broken-package traceback must not tell the
        user `--clear` unconditionally 'still works and removes the wiring' —
        that promises an outcome the code cannot guarantee under a held lock.
        """
        import subprocess
        import textwrap
        from pathlib import Path

        src = str(Path(__file__).resolve().parent.parent / "src")
        code = textwrap.dedent(
            f"""
            import sys
            sys.path.insert(0, {src!r})
            from claude_swap import pin
            pin.run = lambda *a, **k: (_ for _ in ()).throw(
                ImportError("No module named 'cryptography'", name="cryptography"))
            from claude_swap import cli
            sys.argv = ["cswap", "pin", "2"]
            try:
                cli._pin_command(["2"])
            except SystemExit as e:
                print("EXIT", e.code)
            """
        )
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        combined = r.stdout + r.stderr
        assert "--clear" in combined, combined[-500:]
        assert "still works and removes the wiring." not in combined, (
            "the advice still promises an unconditional outcome: "
            f"{combined[-500:]}"
        )


class TestTheLockProbeActuallyProbes:
    """`_config_lock_is_free`'s only two references in the suite
    (`test_the_launch_unwire_is_bounded_by_the_budget` above) both
    monkeypatch the function itself — its real body, the `proper_lockfile`
    call, never runs under test. Inverting both returns (True<->False) left
    the suite at 150 passed.

    Drives the REAL function against a real lock directory in `tmp_path`, no
    monkeypatch of `_config_lock_is_free` itself. `get_global_config_path` is
    pointed at a tmp path the same way `TestHealNeverTearsDownAServingPin`
    and others already do in this file.

    THE CONTROL: asserting "returned in under N seconds" for the held case
    passes trivially if the lock was never actually taken by anything — a
    broken fixture and a working one both return fast. So the held case's
    False is paired with a free probe on the SAME path returning True; only
    together do they prove the probe reached the real lock.
    """

    def test_a_held_lock_answers_false_within_the_budget(self, tmp_path, monkeypatch):
        import time as _time

        import claude_swap.paths as paths
        from claude_swap import pin
        from claude_swap.claude_locks import proper_lockfile

        cfg = tmp_path / ".claude.json"
        cfg.write_text("{}")
        monkeypatch.setattr(paths, "get_global_config_path", lambda: cfg)

        lock_dir = cfg.parent / (cfg.name + ".lock")
        with proper_lockfile(lock_dir, timeout=5):
            start = _time.monotonic()
            held_result = pin._config_lock_is_free(0.3)
            elapsed = _time.monotonic() - start

        assert held_result is False, "answered True while the lock was held"
        # roughly `budget`, not instant and not the process default (9s) —
        # confirms the probe actually waited on the held lock rather than
        # short-circuiting some other way.
        assert 0.2 <= elapsed <= 2.0, (
            f"took {elapsed:.2f}s against a 0.3s budget — not bounded by it"
        )

        # THE CONTROL: same path, lock released, must now answer True. If a
        # broken fixture made the held probe return False for some reason
        # unrelated to the real lock, this would also fail — it is what rules
        # that out.
        assert pin._config_lock_is_free(1.0) is True, (
            "a free probe on the same path after release did not answer True "
            "— the held-case False above is not trustworthy without this"
        )


class TestTheVerdictHasExactlyOneImplementation:
    """The invariant an earlier commit CLAIMED and did not have.

    `clear_pin`/`set_pin` were added so a fix could not land on one front end
    and miss the other — but `run()` kept its own inline copy, so the API-key
    refusal lived in the CLI and not in the shared pair, and the TUI pinned an
    API-key account through a stale submenu row. Asserting the structure is
    what makes the claim true.
    """

    def _pin_src(self):
        from pathlib import Path

        return (
            Path(__file__).resolve().parent.parent / "src" / "claude_swap" / "pin.py"
        ).read_text(encoding="utf-8")

    def test_run_delegates_to_the_shared_pair(self):
        import ast

        tree = ast.parse(self._pin_src())
        run = next(
            n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run"
        )
        called = {
            n.func.id
            for n in ast.walk(run)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        assert {"clear_pin", "set_pin"} <= called, (
            "run() does not go through the shared verdict — it is the second "
            f"copy the pair exists to eliminate (calls: {sorted(called)})"
        )
        # And it must not re-derive the outcome: apply_pin belongs to the pair.
        attrs = {
            n.func.attr
            for n in ast.walk(run)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }
        assert "apply_pin" not in attrs, (
            "run() calls apply_pin directly again — the verdict is back in two places"
        )

    def test_set_pin_refuses_an_api_key_account(self, tmp_path):
        """The refusal must be IN set_pin, not only at a call site.

        The TUI's row filter is a courtesy: refresh_root_menu returns early
        below depth 1, so an open submenu is never rebuilt while the snapshot
        keeps updating — a row that was OAuth when drawn pins an API-key
        account when selected.
        """
        import types

        from claude_swap import pin

        backup = tmp_path / "b"
        backup.mkdir()
        (backup / "settings.json").write_text("{}")
        sw = types.SimpleNamespace(
            backup_dir=backup,
            resolve_account=lambda a: (3, "key@example.com", "org"),
            _account_kind=lambda n: "api_key",
        )
        ok, msg = pin.set_pin(sw, "key@example.com", "org")
        assert not ok, msg
        assert "API-key account" in msg, msg

    def test_a_duplicate_email_cannot_bypass_the_api_key_refusal(self, tmp_path):
        """The slot is PASSED, not re-derived from the email.

        cswap's own documented personal+org pattern gives one address two
        slots, so `resolve_account(email)` raises ConfigError — and swallowing
        that skipped `_account_kind` entirely, accepting the exact account the
        refusal exists to reject. Reproduced from the plain CLI: ok=True with
        apply_pin called.
        """
        import types

        from claude_swap import pin
        from claude_swap.exceptions import ConfigError

        backup = tmp_path / "b"
        backup.mkdir()
        (backup / "settings.json").write_text("{}")
        applied = []
        real_impl = pin._impl

        def _resolve(a):
            if "@" in str(a):  # ambiguous BY EMAIL, fine by number
                raise ConfigError("multiple accounts match dup@example.com")
            return (a, "dup@example.com", "org")

        sw = types.SimpleNamespace(
            backup_dir=backup,
            resolve_account=_resolve,
            _account_kind=lambda n: "api_key",
        )
        pin._impl = lambda: types.SimpleNamespace(
            apply_pin=lambda *a: applied.append(a[1:]) or True
        )
        try:
            ok, msg = pin.set_pin(sw, "dup@example.com", "org", num="2")
            assert not ok, "a duplicate email got past the API-key refusal"
            # THE DISTINGUISHING TEXT. "API-key account" alone appears in BOTH
            # this refusal ("... is an API-key account, which the cloud pin
            # cannot use ...") and the resolve-FAILURE message ("... so the
            # cloud pin cannot check it is not an API-key account") — a bug
            # that swallows the ConfigError and falls into the resolve-failure
            # branch instead of ever reaching `_account_kind` would match the
            # substring just as well as the real refusal does.
            assert "which the cloud pin cannot use" in msg, msg
            assert applied == [], "apply_pin ran for an API-key account"
        finally:
            pin._impl = real_impl

    def test_an_unreadable_kind_refuses_rather_than_proceeding(self, tmp_path):
        """A kind we cannot READ is not permission to pin.

        Swallowing the lookup turned an unreadable sequence.json into a silent
        skip of the refusal — indistinguishable, in effect, from having no
        refusal at all, and invisible.
        """
        import types

        from claude_swap import pin

        backup = tmp_path / "b"
        backup.mkdir()
        (backup / "settings.json").write_text("{}")
        applied = []
        real_impl = pin._impl

        def _boom(n):
            raise OSError("sequence.json is unreadable")

        sw = types.SimpleNamespace(
            backup_dir=backup,
            resolve_account=lambda a: ("2", "who@example.com", "org"),
            _account_kind=_boom,
        )
        pin._impl = lambda: types.SimpleNamespace(
            apply_pin=lambda *a: applied.append(a[1:]) or True
        )
        try:
            ok, msg = pin.set_pin(sw, "who@example.com", "org", num="2")
            assert not ok, "pinned an account whose kind could not be read"
            assert "will not guess" in msg, msg
            assert applied == [], "apply_pin ran without knowing the kind"
        finally:
            pin._impl = real_impl


class TestHealADeadPin:
    """A dead pin must not take the session with it.

    OUTAGE (2026-08-02, lmd42): the pin daemon died, and its wiring
    stayed in ``.claude.json``. Claude Code applies that env block at BOOT, so
    every session — including new ones — dialled the dead port and showed
    ``Unable to connect to API (ConnectionRefused) · attempt 6/300`` for hours,
    while the proxies behind the pin were healthy the whole time. Nothing
    recovered on its own because the recovery command did not exist: the status
    line called ``cswap pin --heal`` every few seconds and argparse rejected it,
    silently, every time.

    The requirement these pin down: turning the pin off — or having it die —
    must leave Claude working exactly as it did before the pin existed.
    """

    def _sw(self, tmp_path, wired=True, pinned="cloud@example.com"):
        import types

        backup = tmp_path / "b"
        backup.mkdir()
        (backup / "settings.json").write_text(
            json.dumps({"remoteControl": {"pinnedEmail": pinned}} if pinned else {})
        )
        cfg = tmp_path / ".claude.json"
        dead = _dead_port()
        cfg.write_text(
            json.dumps(
                {
                    "env": {
                        "HTTPS_PROXY": f"http://127.0.0.1:{dead}",
                        "CSWAP_PIN_PORT": str(dead),
                    },
                    "_cswapPinWiredKeys": ["HTTPS_PROXY", "CSWAP_PIN_PORT"],
                }
                if wired
                else {"env": {}}
            )
        )
        # _write_json is what the REAL switcher writes the config through; a
        # stub without it makes _clear_wiring_locked raise AttributeError,
        # which clear_wiring swallows into a bare False — so the unwire never
        # happens and the test reads it as "nothing was wired".
        return (
            types.SimpleNamespace(
                backup_dir=backup,
                _write_json=lambda path, data: path.write_text(
                    json.dumps(data, indent=2), encoding="utf-8"
                ),
            ),
            cfg,
        )

    def _paths(self, monkeypatch, cfg):
        import claude_swap.paths as paths

        monkeypatch.setattr(paths, "get_global_config_path", lambda: cfg)
        monkeypatch.setattr(paths, "get_default_global_config_path", lambda: cfg)

    def test_heal_restarts_the_proxy_when_it_can(self, tmp_path, monkeypatch):
        """Preferred outcome: the daemon comes back on the SAME port, so live
        sessions — whose env is fixed at exec — reattach with no restart."""
        import socket
        import threading

        from claude_swap import pin

        sw, cfg = self._sw(tmp_path)
        self._paths(monkeypatch, cfg)
        called = []
        revived = {}

        class _I:
            def heal(self, backup_dir):
                # A REAL heal binds the port the wiring names. Returning True
                # while binding nothing is a state no working package can
                # produce, and `heal` now re-reads rather than believing it —
                # so a fixture that only returns True would be asserting the
                # seam trusts a claim it must not trust.
                called.append(backup_dir)
                port = int(json.loads(cfg.read_text())["env"]["CSWAP_PIN_PORT"])
                srv = socket.socket()
                srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                srv.bind(("127.0.0.1", port))
                srv.listen(8)
                revived["srv"] = srv
                threading.Thread(
                    target=lambda: [srv.accept()[0].close() for _ in iter(int, 1)],
                    daemon=True,
                ).start()
                return True

        monkeypatch.setattr(pin, "_live_impl", lambda: _I())
        try:
            changed, msg = pin.heal(sw)
        finally:
            if "srv" in revived:
                revived["srv"].close()
        assert changed, msg
        # "Restored", not "Restarted": the same call also re-wires a daemon
        # that is serving while the config names nothing, so a message naming
        # only the restart would be wrong half the time it fires.
        assert "Restored" in msg, msg
        assert called == [sw.backup_dir]
        # The wiring is CORRECT now — healing must not have torn it down.
        assert "_cswapPinWiredKeys" in cfg.read_text()

    def test_heal_unwires_when_the_proxy_cannot_be_restarted(
        self, tmp_path, monkeypatch
    ):
        """THE OUTAGE. The daemon is gone and cannot come back. Leaving the
        wiring wired to a dead port is what took every session down, so the
        wiring must go — unpinned is a working session, wired-to-nothing is
        not."""
        from claude_swap import pin

        sw, cfg = self._sw(tmp_path)
        self._paths(monkeypatch, cfg)

        class _I:
            def heal(self, backup_dir):
                return False  # could not restart

        monkeypatch.setattr(pin, "_live_impl", lambda: _I())
        changed, msg = pin.heal(sw)
        assert changed, msg
        assert "fall back" in msg, msg
        # Re-READ the file: the verdict must describe the state, not the call.
        raw = json.loads(cfg.read_text())
        assert "_cswapPinWiredKeys" not in raw
        assert not (raw.get("env") or {}).get("HTTPS_PROXY")

    def test_heal_unwires_with_no_package_at_all(self, tmp_path, monkeypatch):
        """The half that matters MOST when the extra is missing or broken.
        A user whose `cswap-pin` install went bad cannot restart anything —
        but they can still be stranded by its leftover wiring, and that is
        precisely when they can least afford it."""
        from claude_swap import pin

        sw, cfg = self._sw(tmp_path)
        self._paths(monkeypatch, cfg)
        monkeypatch.setattr(pin, "_live_impl", lambda: None)  # no usable extra
        changed, msg = pin.heal(sw)
        assert changed, msg
        assert "_cswapPinWiredKeys" not in json.loads(cfg.read_text())

    def test_heal_does_not_claim_success_when_the_unwire_failed(
        self, tmp_path, monkeypatch
    ):
        """The verdict must come from the unwire's own result, not from having
        reached the call. A contended `.claude.json` lock makes clear_wiring
        return False with the wiring INTACT — reporting "sessions fall back"
        there tells the user the outage is over while every session is still
        dialling the dead port, which is the failure this whole path exists to
        end. (This is the mutation the file-content assertions do not catch:
        `clear_wiring(...) or True` leaves them all green.)"""
        from claude_swap import pin

        sw, cfg = self._sw(tmp_path)
        self._paths(monkeypatch, cfg)
        monkeypatch.setattr(pin, "_live_impl", lambda: None)
        monkeypatch.setattr(pin, "clear_wiring", lambda *a, **k: False)
        changed, msg = pin.heal(sw)
        assert not changed, msg
        assert "fall back" not in msg, msg
        # And the state agrees with the verdict: still wired.
        assert "_cswapPinWiredKeys" in json.loads(cfg.read_text())

    def test_heal_is_a_no_op_when_nothing_is_wired(self, tmp_path, monkeypatch):
        """Called from the status line every few seconds. The healthy case must
        cost nothing and must not claim to have done something."""
        from claude_swap import pin

        sw, cfg = self._sw(tmp_path, wired=False)
        self._paths(monkeypatch, cfg)
        monkeypatch.setattr(pin, "_live_impl", lambda: None)
        changed, msg = pin.heal(sw)
        assert not changed
        assert msg == "Nothing to heal"

    def test_heal_never_raises(self, tmp_path, monkeypatch):
        """The status line calls this on a timer; an exception there breaks the
        prompt itself, which is worse than the fault it is reporting.

        This docstring must not claim it exercises
        `_wired_port_is_serving` "OUTSIDE any try", asserting on a message
        that call supposedly produced. Instrumenting `Path.home()`
        and captured the frames: that call's own `_wired_ports()` IS guarded
        (fixed the round before this one) and its raise is swallowed there,
        contributing nothing to the outcome. The message this test would
        assert on came from a SECOND raise, inside `_wiring_present`, which
        happens to sit inside the bottom `try` in `heal` — so it passed for
        a reason the docstring never named.

        THIS ROUND (Task 4) removed that second raise too: `_wiring_present`
        and `clear_wiring` now guard their own path getters exactly as
        `_wired_ports` already did, so an unresolvable
        `get_default_global_config_path()` is "no opinion" everywhere, not a
        caught exception anywhere. With this fixture (own config
        resolvable via `CLAUDE_CONFIG_DIR` and wired to a genuinely dead
        port; `Path.home()` raising for the OTHER, unresolvable config):
        `heal` no longer falls back to "Could not heal" at all — it
        genuinely clears the resolvable config's stale wiring, exactly as it
        would with a resolvable default config that simply names nothing.
        The regression this test still catches: reverting EITHER guard (this
        round's on `_wiring_present`, or last round's on `_wired_ports`)
        reintroduces a raise on this exact fixture, either propagating out of
        `heal` (if `_wired_ports`' guard goes) or changing the verdict back
        to "Could not heal" (if `_wiring_present`'s guard goes) — either way
        these assertions catch it.
        """
        import pathlib

        from claude_swap import pin

        sw, cfg = self._sw(tmp_path)  # own config wired to a genuinely dead port
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        monkeypatch.delenv("HOME", raising=False)
        monkeypatch.setattr(
            pathlib.Path,
            "home",
            staticmethod(lambda: (_ for _ in ()).throw(RuntimeError("no HOME"))),
        )
        monkeypatch.setattr(pin, "_live_impl", lambda: None)  # package absent

        changed, msg = pin.heal(sw)  # must not raise
        assert changed, msg
        assert "fall back" in msg, msg
        # The resolvable config's own dead wiring is genuinely gone, not
        # merely reported gone.
        assert "_cswapPinWiredKeys" not in json.loads(cfg.read_text(encoding="utf-8"))

    def test_cli_accepts_heal(self, monkeypatch):
        """The whole outage was unrecoverable because argparse REJECTED the
        flag the status line was already shipping. Assert the flag parses and
        reaches run(), not merely that a function named heal exists."""
        import claude_swap.cli as cli

        seen = {}

        def _run(switcher, account, clear=False, heal_only=False, get_port=False,
                 set_port=None, ensure=False):
            seen.update(
                account=account, clear=clear, heal_only=heal_only,
                get_port=get_port, set_port=set_port, ensure=ensure,
            )
            return 0

        monkeypatch.setattr("claude_swap.pin.run", _run)
        monkeypatch.setattr(cli, "ClaudeAccountSwitcher", lambda **k: object())
        monkeypatch.setattr(cli, "_guard_root", lambda s: None)

        # A TRIPWIRE ON THE REAL `run`, because its failure is SILENT here.
        # `_pin_command` binds `pin_run` with a function-scoped
        # `from claude_swap.pin import run`. If that ever resolves to the
        # unpatched function, the real `run(heal_only=True)` calls `heal`,
        # which never raises and returns 0 — so `sys.exit(0)` fires, the exit
        # assertion below PASSES, and the only symptom is `seen == {}`: a bare
        # empty dict with nothing saying why.
        #
        # Observed once on CI (Linux, `-n 8`) and once locally, never
        # reproducibly; `-n0` over the whole suite is green, so whatever it
        # is lives in the concurrency, not in this file's ordering. This does
        # not fix that — it makes the next occurrence name itself instead of
        # arriving as an unexplained `{} != {...}`.
        def _real_run_reached(*_a, **_k):
            raise AssertionError(
                "the REAL claude_swap.pin.run executed: the monkeypatch did "
                "not reach _pin_command's function-scoped import of it"
            )

        monkeypatch.setattr("claude_swap.pin.heal", _real_run_reached)

        with pytest.raises(SystemExit) as e:
            cli._pin_command(["--heal"])
        assert e.value.code == 0
        assert seen, (
            "pin.run was never called at all, and `--heal` still exited 0 — "
            "the flag parsed and was dropped, which is exactly the outage "
            "this test exists to prevent"
        )
        assert seen == {
            "account": None, "clear": False, "heal_only": True,
            "get_port": False, "set_port": None, "ensure": False,
        }

    def test_get_port_answers_only_a_serving_pin(self, tmp_path, monkeypatch):
        """`--get_port` exists so consumers stop reading our files.

        Measured in the owner's dotfiles: `cc-update` opens
        `pin-proxy/proxy.json` at TWO hardcoded paths and parses our schema,
        because a pinned session's HTTPS_PROXY names the pin's own dynamic
        port and without that number every pinned session is reported as
        bypassing the cache proxy. Nothing could ASK, so our layout and schema
        became a compatibility surface we cannot change.

        Two properties, and the second is the one that makes it safe:
        stdout is bare digits (it is read by `$(...)`, so a prefix or a "no
        pin" sentence lands inside the caller's variable), and the port is
        PROBED — `proxy.json` outlives a dead daemon, and answering with that
        number reports a session as chained when it is not.
        """
        import socket
        import types

        from claude_swap import pin

        backup = tmp_path / "backup"
        (backup / "pin-proxy").mkdir(parents=True)
        sw = types.SimpleNamespace(backup_dir=backup)
        record = backup / "pin-proxy" / "proxy.json"

        # A dead record — the shape an unclean exit or a failed handover leaves.
        dead = socket.socket()
        dead.bind(("127.0.0.1", 0))
        dead_port = dead.getsockname()[1]
        dead.close()
        record.write_text(json.dumps({"port": dead_port, "pid": 999}))
        assert pin.run(sw, None, get_port=True) != 0, (
            "a dead recorded port was reported as serving"
        )

        # ...and a live one, with the package unavailable: the caller most
        # likely to ask is diagnosing a failure.
        lsn = socket.socket()
        lsn.bind(("127.0.0.1", 0))
        lsn.listen(4)
        port = lsn.getsockname()[1]
        record.write_text(json.dumps({"port": port, "pid": 999}))
        monkeypatch.setattr(pin, "_impl", lambda: (_ for _ in ()).throw(
            ClaudeSwitchError("The cloud pin requires 'cswap-pin'")))
        try:
            assert pin.run(sw, None, get_port=True) == 0
        finally:
            lsn.close()

    def test_set_port_persists_where_the_package_looks_for_it(
        self, tmp_path, monkeypatch
    ):
        """`--set_port N` writes the pin's own settings file, not .claude.json.

        THE SHAPE, not our own reader. This asserted `pin.configured_port`,
        a second copy of the parse that lived here with no caller — proving
        only that we are self-consistent, which two components that both
        drifted also are. The reader that matters is
        `cswap_pin.proxy.configured_port`, since that one reaches `bind()`,
        and it is asserted against this writer in
        `TestClearRunsWithTheExtraGone` where the extra is installed.

        Stated in raw JSON here so the check still runs on CI, which does not
        install the extra (see ci.yml).
        """
        import types

        from claude_swap import pin

        backup = tmp_path / "backup"
        (backup / "pin-proxy").mkdir(parents=True)
        sw = types.SimpleNamespace(
            backup_dir=backup,
            _write_json=lambda p, d: p.write_text(json.dumps(d), encoding="utf-8"),
        )

        monkeypatch.delenv("CSWAP_PIN_PORT", raising=False)
        assert pin.run(sw, None, set_port=44444) == 0
        # NOT in .claude.json — that file is for what Claude Code reads.
        assert "settings.json" in os.listdir(backup / "pin-proxy")
        # THE SHAPE THE PACKAGE PARSES: `proxy._settings_port` reads the
        # top-level "port" key out of this file and nothing else.
        raw = json.loads((backup / "pin-proxy" / "settings.json").read_text())
        assert raw.get("port") == 44444, raw

    def test_set_port_keeps_the_rest_of_the_settings_file(self, tmp_path):
        """It is a SETTINGS file, so `--set_port` must not truncate it.

        The next setting to land beside `port` would otherwise be erased by
        the next `--set_port` — the kind of loss nobody notices until the
        setting they set has quietly gone.

        Untested here until the package's own dead writer was removed: the
        read-modify-write was asserted against THAT one, so the writer that
        actually runs had the property and nothing checking it.
        """
        import types

        from claude_swap import pin

        backup = tmp_path / "backup"
        (backup / "pin-proxy").mkdir(parents=True)
        path = backup / "pin-proxy" / "settings.json"
        path.write_text(json.dumps({"somethingElse": "keep me"}))
        sw = types.SimpleNamespace(
            backup_dir=backup,
            _write_json=lambda p, d: p.write_text(json.dumps(d), encoding="utf-8"),
        )

        assert pin.run(sw, None, set_port=43333) == 0
        raw = json.loads(path.read_text())
        assert raw.get("somethingElse") == "keep me", (
            f"--set_port clobbered the rest of the settings file: {raw}"
        )
        assert raw.get("port") == 43333, raw

        # ...and clearing removes ONLY the port.
        assert pin.run(sw, None, set_port=0) == 0
        raw = json.loads(path.read_text())
        assert "port" not in raw, raw
        assert raw.get("somethingElse") == "keep me", raw

    def test_set_port_refuses_a_number_that_is_not_a_port(self, tmp_path):
        """0 is the interesting one and it is not merely invalid.

        `bind()` reads 0 as "choose one for me", so persisting it would do the
        OPPOSITE of what a user who typed it meant, while looking like it
        worked. 0 clears the setting instead — the one reading that cannot be
        mistaken for a request.
        """
        import types

        from claude_swap import pin

        backup = tmp_path / "backup"
        (backup / "pin-proxy").mkdir(parents=True)
        sw = types.SimpleNamespace(
            backup_dir=backup,
            _write_json=lambda p, d: p.write_text(json.dumps(d), encoding="utf-8"),
        )

        assert pin.run(sw, None, set_port=44444) == 0
        assert pin.run(sw, None, set_port=0) == 0, "clearing must be allowed"
        raw = json.loads((backup / "pin-proxy" / "settings.json").read_text())
        assert "port" not in raw, f"0 was persisted as a port: {raw}"

        for bad in (70000, -1):
            assert pin.run(sw, None, set_port=bad) != 0, (
                f"{bad} was accepted as a port"
            )

    def test_the_cli_forwards_the_port_flags_and_ensure(self):
        """Both flags must be WIRED, not merely declared.

        A flag that parses and is then dropped prints the pin STATUS and exits
        0 — a caller's `$(...)` captures prose and the script behaves as
        though a pin were serving. A correct mechanism with no caller is the
        defect this repo keeps finding, so assert on the parse tree.
        """
        import ast
        import inspect
        import textwrap

        from claude_swap import cli

        tree = ast.parse(textwrap.dedent(inspect.getsource(cli._pin_command)))
        for flag in ("--get_port", "--set_port", "--ensure"):
            assert [
                n for n in ast.walk(tree)
                if isinstance(n, ast.Call)
                and getattr(n.func, "attr", None) == "add_argument"
                and any(
                    isinstance(a, ast.Constant) and a.value == flag for a in n.args
                )
            ], f"cswap pin does not declare {flag}"
        forwarded = {
            kw.arg
            for n in ast.walk(tree) if isinstance(n, ast.Call)
            for kw in n.keywords
        }
        assert {"get_port", "set_port", "ensure"} <= forwarded, (
            f"parsed but never forwarded to pin.run: "
            f"{ {'get_port', 'set_port', 'ensure'} - forwarded }"
        )

    def test_a_peer_that_mutates_the_env_cannot_half_wire_a_launch(
        self, tmp_path, monkeypatch
    ):
        """"Degrades to an UNPINNED launch" must hold for a peer that WRITES.

        `wire_launch_env` validates `wire_env`'s RETURN and falls back to an
        unpinned launch when it is not a str->str mapping — but the fallback
        returns the caller's own `env` object, so the guard only covers a peer
        that returns wrongly, not one that MUTATES. `session.py` hands in its
        own dict and the result reaches `os.execvpe`, which sits outside the
        launch's `try`: a half-wired env there is not a caught exception, it
        is the launch.

        Not reachable through 0.1.68 — its `wire_env` opens with
        `out = dict(env)` (verified in the package source). This is the peer
        threat model the module states in its own words: "the package is a
        PEER on an independent release schedule", and `heal` already refuses
        to trust its return value. Trusting it not to write, while validating
        what it returns, was the half that was missing.
        """
        import types

        from claude_swap import pin

        class _Mutating:
            @staticmethod
            def ensure_proxy(_sw):
                return (41234, tmp_path / "ca.pem")

            @staticmethod
            def wire_env(env, port, ca_path, *a, **k):
                env["HTTPS_PROXY"] = f"http://127.0.0.1:{port}"
                env["NODE_EXTRA_CA_CERTS"] = str(ca_path)
                return None  # the shape the existing guard already rejects

        monkeypatch.setattr(pin, "_impl", lambda: _Mutating)
        monkeypatch.setattr(pin, "_pinned_email_now", lambda _sw: ("c@e.com", None))
        monkeypatch.setattr(pin, "_dead_wired_configs", lambda *a, **k: [])

        caller_env = {"PATH": "/usr/bin"}
        sw = types.SimpleNamespace(backup_dir=tmp_path)
        out = pin.wire_launch_env(sw, caller_env)

        assert "HTTPS_PROXY" not in out, (
            f"a rejected wire still reached the launch env: {sorted(out)} — "
            f"the value execvpe receives is half-wired, pointing at a proxy "
            f"the guard just refused to trust"
        )
        assert caller_env == {"PATH": "/usr/bin"}, (
            f"the caller's own dict was mutated: {caller_env}. session.py "
            f"passes the dict it goes on to use, so the damage outlives this "
            f"call even when the return value is discarded"
        )

    def test_heal_runs_before_the_package_is_required(self, tmp_path, monkeypatch):
        """`run(--heal)` must not go through _impl(): the missing-package error
        would abort exactly the users who most need the wiring removed."""
        from claude_swap import pin

        sw, _cfg = self._sw(tmp_path)
        monkeypatch.setattr(
            pin, "_impl", lambda: (_ for _ in ()).throw(ClaudeSwitchError("no extra"))
        )
        monkeypatch.setattr(pin, "heal", lambda s, **_k: (True, "Removed a stale wiring"))
        assert pin.run(sw, None, heal_only=True) == 0

    def test_heal_does_not_claim_success_over_a_PARTIAL_unwire(
        self, tmp_path, monkeypatch
    ):
        """`clear_wiring` returns True when ANY of the two configs changed —
        `heal` trusted that bool instead of re-reading, so with the session
        config's lock held (contended) and the default config free, the
        default cleared, `clear_wiring` returned True for that one change,
        and `heal` reported success while the SESSION config still named a
        dead port. `clear_pin` gets this right by re-reading
        `_wiring_present`; `heal` must do the same.

        TWO DISTINCT CONFIG FILES, not `_sw`'s single-file fixture: every
        existing heal test points both `get_global_config_path` and
        `get_default_global_config_path` at the SAME file, so a clear that
        only reaches one of two never arises there.
        """
        from claude_swap import pin
        from claude_swap.claude_locks import proper_lockfile

        session_dir = tmp_path / "session"
        default_dir = tmp_path / "default"
        session_dir.mkdir()
        default_dir.mkdir()
        session_cfg = session_dir / ".claude.json"
        default_cfg = default_dir / ".claude.json"
        for cfg in (session_cfg, default_cfg):
            dead = _dead_port()
            cfg.write_text(
                json.dumps(
                    {
                        "env": {
                            "HTTPS_PROXY": f"http://127.0.0.1:{dead}",
                            "CSWAP_PIN_PORT": str(dead),
                        },
                        "_cswapPinWiredKeys": ["HTTPS_PROXY", "CSWAP_PIN_PORT"],
                    }
                )
            )

        import claude_swap.paths as paths

        monkeypatch.setattr(paths, "get_global_config_path", lambda: session_cfg)
        monkeypatch.setattr(paths, "get_default_global_config_path", lambda: default_cfg)
        monkeypatch.setattr(pin, "_live_impl", lambda: None)  # nothing can restart

        sw, _cfg = self._sw(tmp_path)  # only used for backup_dir/_write_json

        # Hold the SESSION lock for real, exactly as measurement showed —
        # a stub would not exercise clear_wiring's per-path skip logic.
        with proper_lockfile(session_cfg.parent / (session_cfg.name + ".lock"), timeout=5):
            changed, msg = pin.heal(sw)

        assert "_cswapPinWiredKeys" in json.loads(session_cfg.read_text()), (
            "fixture invalid: the session config was not actually contended"
        )
        # THE STATE, not the call's own return value — exactly what clear_pin
        # already does and heal did not.
        assert pin._wiring_present(sw) is True, (
            "fixture invalid: no wiring survives to disagree with the verdict"
        )
        assert not changed, (
            f"heal reported success ({msg!r}) while a wiring survives on disk "
            "— every new session from that terminal still boots against a "
            "dead port"
        )


class TestHealNeverTearsDownAServingPin:
    """`heal` must ask the WIRING, not the restart's return value.

    REGRESSION: `impl.heal()` returns False for BOTH "could not
    restart" and "already serving, nothing to do". Reading the second as the
    first unwired a HEALTHY pin — run against a live daemon (pid alive, port
    answering), it stripped the env block and unpinned a working session. That
    is the same damage as the outage heal exists to fix, in the other
    direction, and it is this codebase's signature defect: a verdict inferred
    from a call's return instead of re-read from the state.
    """

    @staticmethod
    def _serving(port=0):
        """A listener that ACCEPTS, in a thread, until it is closed.

        A bare ``listen(n)`` that never accepts is not "a serving port" — it is
        a port with n free backlog slots, and each probe consumes one for the
        life of the test. On Linux with ``listen(1)``: connect #1 OK,
        #2 OK, #3 times out. Windows CI is stricter and refused the SECOND
        connect, which is what made
        ``test_the_serving_check_needs_no_package`` red there while `test` and
        `macos-keychain` passed.

        Raising the backlog would only move the ceiling, and silently: the next
        probe someone adds to `heal` puts it back, on one platform, in CI. So
        drain the queue instead — then "serving" means what the name says, for
        any number of probes, on any platform.
        """
        import socket
        import threading

        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen(8)

        def _drain():
            while True:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return  # closed by the test: the only exit
                conn.close()

        t = threading.Thread(target=_drain, daemon=True)
        t.start()
        return srv, srv.getsockname()[1]

    def _wired_to(self, tmp_path, port):
        import types

        backup = tmp_path / "b"
        backup.mkdir(exist_ok=True)
        (backup / "settings.json").write_text(
            json.dumps({"remoteControl": {"pinnedEmail": "c@e.com"}})
        )
        cfg = tmp_path / ".claude.json"
        cfg.write_text(
            json.dumps(
                {
                    "env": {
                        "HTTPS_PROXY": f"http://127.0.0.1:{port}",
                        "CSWAP_PIN_PORT": str(port),
                    },
                    "_cswapPinWiredKeys": ["HTTPS_PROXY", "CSWAP_PIN_PORT"],
                }
            )
        )
        return (
            types.SimpleNamespace(
                backup_dir=backup,
                _write_json=lambda p, d: p.write_text(
                    json.dumps(d, indent=2), encoding="utf-8"
                ),
            ),
            cfg,
        )

    def _paths(self, monkeypatch, cfg):
        import claude_swap.paths as paths

        monkeypatch.setattr(paths, "get_global_config_path", lambda: cfg)
        monkeypatch.setattr(paths, "get_default_global_config_path", lambda: cfg)

    def test_a_live_wired_port_is_never_unwired(self, tmp_path, monkeypatch):
        """A REAL listening socket, not a mock: 'is the pin serving' is a
        question about the network, and a mocked answer would pass while the
        real one tore the user's session down."""
        import socket

        from claude_swap import pin

        srv, port = self._serving()
        try:
            sw, cfg = self._wired_to(tmp_path, port)
            self._paths(monkeypatch, cfg)

            class _AlreadyServing:
                # False here means "nothing to do", NOT "I failed".
                def heal(self, backup_dir):
                    return False

            monkeypatch.setattr(pin, "_live_impl", lambda: _AlreadyServing())
            # clear_wiring must never even be REACHED. Asserting only on the
            # file lets the guard be deleted while a failing unwire keeps the
            # test green — the wiring survives for the wrong reason.
            monkeypatch.setattr(
                pin,
                "clear_wiring",
                lambda *a, **k: pytest.fail(
                    "clear_wiring was called against a SERVING pin"
                ),
            )
            changed, msg = pin.heal(sw)
            assert not changed, msg
            assert msg == "Nothing to heal", msg
            # Re-READ: the wiring must still be there.
            assert "_cswapPinWiredKeys" in json.loads(cfg.read_text())
        finally:
            srv.close()

    def test_a_dead_config_does_not_take_the_live_one_down_with_it(
        self, tmp_path, monkeypatch
    ):
        """THE MACHINE-WIDE VERDICT MUST NOT BECOME A MACHINE-WIDE ACT.

        `_wired_port_is_serving` is AND over every wired config, deliberately —
        a live session config must not mask a dead default config. That is the
        right VERDICT. But `clear_wiring` is unconditional over every wired
        config, so the two compose into: one dead config unwires the other
        config's LIVE, correctly-routed pin.

        The asymmetry is the shipped shape, not a corner case — the same one
        `_wired_port_is_serving`'s own comment describes:

            session cfg -> live   default cfg -> dead

        With the package present it self-recovers on the next tick (`impl.heal`
        re-wires a daemon that serves while the config names nothing), so the
        cost is churn plus a window where new sessions launch unpinned. With
        the package ABSENT nothing re-wires and the live pin is gone for good.

        Either way it contradicts this file's own rule, stated in capitals at
        the top of `heal`: a serving pin is never torn down. The per-config
        answer `_port_of_config` already computes is what decides which config
        may go.
        """
        from claude_swap import pin

        srv, live = self._serving()
        try:
            sw, session_cfg = self._wired_to(tmp_path, live)
            dead_dir = tmp_path / "default"
            dead_dir.mkdir()
            default_cfg = dead_dir / ".claude.json"
            dead = _dead_port()
            default_cfg.write_text(
                json.dumps(
                    {
                        "env": {
                            "HTTPS_PROXY": f"http://127.0.0.1:{dead}",
                            "CSWAP_PIN_PORT": str(dead),
                        },
                        "_cswapPinWiredKeys": ["HTTPS_PROXY", "CSWAP_PIN_PORT"],
                    }
                )
            )
            import claude_swap.paths as paths

            monkeypatch.setattr(paths, "get_global_config_path", lambda: session_cfg)
            monkeypatch.setattr(
                paths, "get_default_global_config_path", lambda: default_cfg
            )
            # No package: the case with no re-wire to hide the damage.
            monkeypatch.setattr(pin, "_live_impl", lambda: None)

            changed, msg = pin.heal(sw)

            assert "_cswapPinWiredKeys" not in json.loads(default_cfg.read_text()), (
                f"the DEAD config kept its wiring, which is the stranding heal "
                f"exists to clear: {msg}"
            )
            assert "_cswapPinWiredKeys" in json.loads(session_cfg.read_text()), (
                f"heal unwired a config whose own port ({live}) is SERVING, "
                f"because the other config named a dead one: ({changed}, {msg})"
            )
        finally:
            srv.close()

    def test_the_LAUNCH_path_does_not_take_the_live_config_down_either(
        self, tmp_path, monkeypatch
    ):
        """THE SIBLING CALL SITE, and it is the worse of the two.

        `heal` and `run(ensure=True)` both ask a MACHINE-WIDE verdict — is
        any wired config's own port dead — and both used to answer it with
        `clear_wiring`,
        a machine-wide ACT. Fixing only the one a review named would leave the
        identical defect on the path that runs from an rc hook before EVERY
        hand-launched `claude`, where it is worse in two ways: nothing calls
        `impl.heal` afterwards to re-wire what it stripped, and every launch
        pays it again.

        Kept beside its sibling on purpose. The two guards drifting apart is
        exactly how this was missed the first time — the grep for
        `clear_wiring(` finds both, a reading of one diff finds one.

        THE DEAD CONFIG'S LOCK IS HELD, and that is what makes this test reach
        the branch rather than describe it. Without contention `heal` runs
        FIRST inside the same `--ensure` call, clears the dead config itself,
        and the verdict is empty by the time the branch below it is asked —
        so a version of this test with no lock passes against the unfixed code
        and
        proves nothing. With the lock held, `heal` cannot clear it, the
        verdict is still stale, and the machine-wide clear below it reaches
        the config whose lock is FREE: the live one. That is not a contrived
        state — the file documents Claude Code holding `.claude.json.lock`
        through a routine credential refresh as the common case.
        """
        from claude_swap import pin

        srv, live = self._serving()
        try:
            sw, session_cfg = self._wired_to(tmp_path, live)
            dead_dir = tmp_path / "default"
            dead_dir.mkdir()
            default_cfg = dead_dir / ".claude.json"
            dead = _dead_port()
            default_cfg.write_text(
                json.dumps(
                    {
                        "env": {
                            "HTTPS_PROXY": f"http://127.0.0.1:{dead}",
                            "CSWAP_PIN_PORT": str(dead),
                        },
                        "_cswapPinWiredKeys": ["HTTPS_PROXY", "CSWAP_PIN_PORT"],
                    }
                )
            )
            import claude_swap.paths as paths

            monkeypatch.setattr(paths, "get_global_config_path", lambda: session_cfg)
            monkeypatch.setattr(
                paths, "get_default_global_config_path", lambda: default_cfg
            )
            monkeypatch.setattr(pin, "_live_impl", lambda: None)
            # Fresh mtime by construction, so `proper_lockfile` refuses rather
            # than taking it over (0.5s launch budget against a 10s staleness
            # window — see TestTheLockFailureThatStrandsTheWiringIsNamed).
            os.mkdir(default_cfg.parent / (default_cfg.name + ".lock"))

            assert pin.run(sw, None, ensure=True) == 0, "the launch hook failed"

            assert "_cswapPinWiredKeys" in json.loads(default_cfg.read_text()), (
                "the fixture did not reach the shape it names: the dead "
                "config was cleared, so the machine-wide branch under test "
                "was never asked"
            )
            assert "_cswapPinWiredKeys" in json.loads(session_cfg.read_text()), (
                f"the launch hook unwired a config whose own port ({live}) is "
                f"SERVING, because the other config named a dead one"
            )
        finally:
            srv.close()

    def test_every_clear_wiring_call_site_narrows_to_the_dead_set(self):
        """THE GUARD FOR THE MISS ITSELF, not for a fourth call site.

        This defect was found once and fixed once, and the fix reached one of
        THREE call sites — `heal` — because that is the one a review named. A
        `grep` for `clear_wiring(` then found `run(ensure=True)`; a `grep` for
        `_wiring_is_stale(` found a third, `wire_launch_env`, which the first
        grep had missed because it does not spell the call the same way. Two
        greps, two more sites, both with the identical defect on the LAUNCH
        path.

        Writing a third near-identical behavioural test would guard the site
        that exists today. This guards the RULE, so a fourth site cannot be
        added without either passing `only=` or deliberately deleting this.

        `clear_pin` is the one exemption and it is the whole point of the
        default: the user asked to be unpinned, so every wired config must go.
        Narrowing THERE would restore the stranding `clear_wiring` was moved
        into this repo to prevent.
        """
        import ast
        import inspect

        from claude_swap import pin

        tree = ast.parse(inspect.getsource(pin))
        offenders = []
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Call)
                    and getattr(node.func, "id", None) == "clear_wiring"
                    and not any(kw.arg == "only" for kw in node.keywords)
                ):
                    offenders.append((fn.name, node.lineno))
        assert [name for name, _ in offenders] == ["clear_pin"], (
            f"clear_wiring is called machine-wide from {offenders} — every "
            f"caller but `clear_pin` must pass `only=` (the dead set), or a "
            f"config whose own port is SERVING gets unwired because another "
            f"config names a dead one"
        )

    def test_a_restart_that_worked_is_not_then_unwired(self, tmp_path, monkeypatch):
        """The SECOND guard. `impl.heal()` uses False for 'already serving' as
        well as for 'failed', so a restart that genuinely brought the daemon
        back still returns False — and without a re-read after it, the very
        next line tears down the pin it just revived."""
        import socket

        from claude_swap import pin

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()  # dead at entry, so the serving guard lets us through

        sw, cfg = self._wired_to(tmp_path, port)
        self._paths(monkeypatch, cfg)
        revived = {}
        outer = self

        class _Reviver:
            def heal(self, backup_dir):
                # Bind the SAME port: this is what a real revival looks like,
                # and it is why the outcome must be re-read rather than taken
                # from the return value. Accepting, not merely listening — see
                # _serving.
                srv, _ = outer._serving(port)
                revived["srv"] = srv
                return False  # "nothing to report" — NOT failure

        monkeypatch.setattr(pin, "_live_impl", lambda: _Reviver())
        monkeypatch.setattr(
            pin,
            "clear_wiring",
            lambda *a, **k: pytest.fail("unwired a pin that had just come back"),
        )
        try:
            changed, msg = pin.heal(sw)
            assert not changed, msg
            assert "_cswapPinWiredKeys" in json.loads(cfg.read_text())
        finally:
            if "srv" in revived:
                revived["srv"].close()

    def test_a_dead_wired_port_still_gets_unwired(self, tmp_path, monkeypatch):
        """The guard above must not disable healing — bind then close, so the
        port is genuinely refusing."""
        import socket

        from claude_swap import pin

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
        s.close()

        sw, cfg = self._wired_to(tmp_path, dead)
        self._paths(monkeypatch, cfg)
        monkeypatch.setattr(pin, "_live_impl", lambda: None)
        changed, msg = pin.heal(sw)
        assert changed, msg
        assert "_cswapPinWiredKeys" not in json.loads(cfg.read_text())

    def test_the_serving_check_needs_no_package(self, tmp_path, monkeypatch):
        """It is a loopback connect, not an import. The uninstalled case is
        exactly when a wrong answer costs the most, in either direction."""
        import socket

        from claude_swap import pin

        srv, port = self._serving()
        try:
            sw, cfg = self._wired_to(tmp_path, port)
            self._paths(monkeypatch, cfg)
            monkeypatch.setattr(pin, "_live_impl", lambda: None)  # no extra
            # TWO probes: the explicit one here, and heal's own. That is what
            # made this the test Windows CI failed — see _serving.
            assert pin._wired_port_is_serving(sw) is True
            changed, _ = pin.heal(sw)
            assert not changed
            assert "_cswapPinWiredKeys" in json.loads(cfg.read_text())
        finally:
            srv.close()

    def test_the_launch_path_never_unwires_a_serving_pin(self, tmp_path, monkeypatch):
        """The guard `heal` had and `wire_launch_env` did not.

        `_impl()` raising says nothing about the daemon — a broken
        `cryptography` after an unrelated upgrade, a half-finished reinstall,
        an import error in a new release all land on that branch while the
        proxy on the port keeps answering every session already wired to it.
        Without the fix: ONE `cswap run` in that state stripped the env
        block from a pin whose port was serving, and every session on the box
        lost it. Same damage as the outage `heal` exists to end, in the other
        direction, at the other call site.
        """
        from claude_swap import pin

        srv, port = self._serving()
        try:
            sw, cfg = self._wired_to(tmp_path, port)
            self._paths(monkeypatch, cfg)

            def _broken():
                raise RuntimeError("cryptography is broken after an upgrade")

            monkeypatch.setattr(pin, "_impl", _broken)
            # Asserting on the file alone would let the guard be deleted while
            # a *failing* unwire kept the test green — the wiring surviving for
            # the wrong reason. Make the call itself the failure.
            monkeypatch.setattr(
                pin,
                "clear_wiring",
                lambda *a, **k: pytest.fail(
                    "the launch path unwired a SERVING pin"
                ),
            )
            out = pin.wire_launch_env(sw, {})
            assert out == {}  # unpinned this launch, but nothing torn down
            assert "_cswapPinWiredKeys" in json.loads(cfg.read_text())
        finally:
            srv.close()

    def test_the_launch_path_still_unwires_a_dead_one(self, tmp_path, monkeypatch):
        """The guard above must not disable the removal it guards.

        A wiring whose proxy is gone MUST still go: `.claude.json`'s env block
        is applied at boot, so leaving it sends every new session at a dead
        port — the outage this whole path exists to prevent.
        """
        import socket

        from claude_swap import pin

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
        s.close()

        sw, cfg = self._wired_to(tmp_path, dead)
        self._paths(monkeypatch, cfg)

        def _broken():
            raise RuntimeError("not installed")

        monkeypatch.setattr(pin, "_impl", _broken)
        pin.wire_launch_env(sw, {})
        assert "_cswapPinWiredKeys" not in json.loads(cfg.read_text())

    def test_heal_does_not_report_health_over_an_unwire_it_could_not_do(
        self, tmp_path, monkeypatch
    ):
        """`present and clear_wiring(...)` collapsed two outcomes into one.

        When the wiring is present, the port is dead, and the unwire fails
        because the config lock is contended, control falls to the
        healthy verdict — over an outage in progress. That path is routine, not
        exotic: the budget is 0.5s and Claude Code holds this lock during a
        credential refresh. And the status line calls `heal` on a timer, so the
        user's only signal during the exact failure it reports said everything
        was fine.

        The lock is held FOR REAL rather than mocked: "can this be taken" is a
        question about the filesystem, and a stubbed clear_wiring would pass
        while the real one still lied.
        """
        import socket

        from claude_swap import pin
        from claude_swap.claude_locks import proper_lockfile

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
        s.close()

        sw, cfg = self._wired_to(tmp_path, dead)
        self._paths(monkeypatch, cfg)
        monkeypatch.setattr(pin, "_live_impl", lambda: None)

        with proper_lockfile(cfg.parent / (cfg.name + ".lock"), timeout=5):
            changed, msg = pin.heal(sw)

        assert not changed  # nothing was removed, and it must not claim so
        assert msg != "Nothing to heal", "reported health over a live outage"
        # THE FULL SENTENCE, not the fragment. This is the only thing telling
        # a user what to do when the pin's proxy died AND the config lock is
        # held; nothing in the suite asserted it verbatim before this
        # (`grep -c` for the sentence across both test files was 0), so a
        # mutation to the string body survived unnoticed.
        assert msg == (
            "A cloud pin wiring points at a proxy that is gone, and it "
            "could not be removed — re-run `cswap pin --heal`, or "
            "`cswap pin --heal --debug` for the reason (a held config "
            "lock and a config directory you cannot write both land here)"
        ), msg
        # The wiring really did survive — the message is describing reality.
        assert "_cswapPinWiredKeys" in json.loads(cfg.read_text())

    def test_an_exception_inside_the_heal_arm_names_its_own_cause(
        self, tmp_path, monkeypatch
    ):
        """`pin.py:1319`'s catch-all: anything raising inside the block that
        checks staleness and clears the wiring lands here, with `_safe(exc)`
        naming the cause. Nothing in the suite asserted this message before
        (`grep -c "Could not heal the cloud pin" across both test files was
        0`); `tests/test_pin.py:2579,2586` mention "Could not heal" only in
        prose explaining a DIFFERENT fixture where this arm is NOT reached.

        `_dead_wired_configs` — the first call inside that block, and the one
        that reads the configs — is made to raise directly, the narrowest way
        to land in `heal`'s bottom `except`. The port passed to the fixture is
        never dialled: the mock raises before it would ever be read.
        """
        from claude_swap import pin

        sw, cfg = self._wired_to(tmp_path, 0)
        self._paths(monkeypatch, cfg)
        monkeypatch.setattr(pin, "_live_impl", lambda: None)
        monkeypatch.setattr(
            pin,
            "_dead_wired_configs",
            lambda switcher, **_k: (_ for _ in ()).throw(RuntimeError("disk gone")),
        )

        changed, msg = pin.heal(sw)

        assert not changed
        assert msg == "Could not heal the cloud pin (disk gone)", msg

    def test_a_serving_but_OBSOLETE_daemon_still_reaches_the_recycle(
        self, tmp_path, monkeypatch
    ):
        """The guard protects against TEARDOWN, not against repair.

        `heal` returning on `serving` before `impl.heal()` runs at all
        which made a whole class of repair unreachable: a daemon SERVING its
        wired port while running code we no longer ship is exactly the state an
        upgrade leaves behind, and every status-line tick declined to touch it.

        across three machines after installing a new release: two had
        daemons serving their own wired port, 24h old, running the previous
        version, and `cswap pin --heal` answered "Nothing to heal" forever. The
        third recycled only because its wiring named a DEAD port — the right
        outcome for the wrong reason.

        The package's `heal` is safe to call in the serving case by
        construction: it returns False for "serving, wired, and current" and
        recycles only when the fingerprint says the daemon predates the
        installed code, rebinding the SAME port.
        """
        from claude_swap import pin

        srv, port = self._serving()
        try:
            sw, cfg = self._wired_to(tmp_path, port)
            self._paths(monkeypatch, cfg)
            calls = []

            class _Obsolete:
                def heal(self, backup_dir):
                    calls.append(backup_dir)
                    return True  # the package recycled it onto the same port

            monkeypatch.setattr(pin, "_live_impl", lambda: _Obsolete())
            changed, msg = pin.heal(sw)
            assert calls, (
                "the package's recycle was never reached — a serving-but-stale "
                "daemon can never be upgraded"
            )
            assert changed and msg == "Restored the cloud pin", msg
            # And the wiring survives: a recycle rebinds, it does not unpin.
            assert "_cswapPinWiredKeys" in json.loads(cfg.read_text())
        finally:
            srv.close()

    def test_a_serving_pin_survives_with_no_package_at_all(self, tmp_path, monkeypatch):
        """Moving the restart ahead of the guard must not lose the guard.

        With the extra absent nothing can restart OR recycle, and removing the
        wiring would unpin a healthy session. That is the case where a user can
        least afford a wrong answer, so it gets its own test rather than
        riding on the branch above.
        """
        from claude_swap import pin

        srv, port = self._serving()
        try:
            sw, cfg = self._wired_to(tmp_path, port)
            self._paths(monkeypatch, cfg)
            monkeypatch.setattr(pin, "_live_impl", lambda: None)
            monkeypatch.setattr(
                pin,
                "clear_wiring",
                lambda *a, **k: pytest.fail("unwired a SERVING pin"),
            )
            changed, msg = pin.heal(sw)
            assert not changed
            assert msg == "Nothing to heal", msg
            assert "_cswapPinWiredKeys" in json.loads(cfg.read_text())
        finally:
            srv.close()

    def test_heal_does_not_take_the_packages_TRUE_on_trust(self, tmp_path, monkeypatch):
        """The False path re-reads; the True path must not be believed.

        Same mistake, opposite direction. This function's whole thesis is that
        a verdict comes from the state rather than from a call — and it matters
        because `cswap-pin` is a PEER on its own release schedule, so the seam
        cannot promise what a future version returns.

        Without the re-read, an impl returning True while binding
        nothing: heal() -> (True, "Restored the cloud pin") while the wired
        port served nothing. The status line calls this on a timer, so the
        user's only signal said the outage was over.
        """
        import socket

        from claude_swap import pin

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
        s.close()

        sw, cfg = self._wired_to(tmp_path, dead)
        self._paths(monkeypatch, cfg)

        class _Liar:
            def heal(self, backup_dir):
                return True  # claims success, binds nothing

        monkeypatch.setattr(pin, "_live_impl", lambda: _Liar())
        changed, msg = pin.heal(sw)
        assert msg != "Restored the cloud pin", (
            "claimed a restore while the wired port serves nothing"
        )
        # It must still do something useful: the wiring named a dead port, so
        # removing it is the honest outcome.
        assert changed, msg
        assert "_cswapPinWiredKeys" not in json.loads(cfg.read_text())

    def test_a_live_config_does_not_mask_a_dead_one(self, tmp_path, monkeypatch):
        """`_wired_port_is_serving` ORed across the two config paths.

        The writer is asymmetric — `cswap_pin.wire_global_config` writes only
        the session config while the seam reads both — so a live session config
        masked a DEAD default config. A user launching plain `claude` from a
        terminal booted against the dead one, and `--heal` answered "Nothing to
        heal" every tick.

        An unwired config is not a counter-example: it sends nobody anywhere.
        Only a config that NAMES a port has an opinion, and every such opinion
        has to be right.
        """
        import socket

        from claude_swap import pin
        import claude_swap.paths as paths

        srv, live = self._serving()
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
        s.close()
        try:
            sw, cfg_live = self._wired_to(tmp_path, live)
            cfg_dead = tmp_path / "default.json"
            cfg_dead.write_text(json.dumps({
                "env": {"CSWAP_PIN_PORT": str(dead)},
                "_cswapPinWiredKeys": ["CSWAP_PIN_PORT"],
            }))
            monkeypatch.setattr(paths, "get_global_config_path", lambda: cfg_live)
            monkeypatch.setattr(
                paths, "get_default_global_config_path", lambda: cfg_dead
            )
            assert pin._wired_port_is_serving(sw) is False, (
                "a live session config masked a dead default config"
            )
            assert bool(pin._dead_wired_configs(sw)) is True
        finally:
            srv.close()

    def test_an_unwired_second_config_is_not_a_counter_example(
        self, tmp_path, monkeypatch
    ):
        """The guard above must not turn every healthy pin into a broken one.

        Only ONE config is normally written, so if an absent wiring counted
        against serving, `_wired_port_is_serving` would be False on every
        healthy machine — and `wire_launch_env` would unwire a live pin on the
        next launch.
        """
        from claude_swap import pin
        import claude_swap.paths as paths

        srv, live = self._serving()
        try:
            sw, cfg_live = self._wired_to(tmp_path, live)
            cfg_bare = tmp_path / "default.json"
            cfg_bare.write_text(json.dumps({}))  # nothing wired here
            monkeypatch.setattr(paths, "get_global_config_path", lambda: cfg_live)
            monkeypatch.setattr(
                paths, "get_default_global_config_path", lambda: cfg_bare
            )
            assert pin._wired_port_is_serving(sw) is True
            assert bool(pin._dead_wired_configs(sw)) is False
        finally:
            srv.close()


class TestANoteMustNotFailTheAction:
    """`run()` had one unguarded call into the optional package, after the pin
    had already been applied and "Pinned…" already printed.

    A raise there — from a peer on its own release schedule — turned a
    SUCCEEDED pin into `Error: the cloud pin is installed but not usable`, exit
    1, plus advice to run `--clear`, which would have destroyed it. The TUI's
    sibling call already guarded the same thing, so the two front ends
    disagreed about one outcome.
    """

    def _sw(self, tmp_path):
        import types

        backup = tmp_path / "b"
        backup.mkdir()
        (backup / "settings.json").write_text("{}")
        return types.SimpleNamespace(
            backup_dir=backup,
            resolve_account=lambda a: ("2", "user2@example.com", None),
            _account_kind=lambda n: "oauth",
            _write_json=lambda p, d: p.write_text(json.dumps(d), encoding="utf-8"),
        )

    def _impl(self, backup, *, rc_raises=False, load_raises=False):
        class _I:
            def load_pin(self, b):
                if load_raises:
                    raise ValueError("settings.json is not valid JSON")
                raw = json.loads((b / "settings.json").read_text() or "{}")
                rc = raw.get("remoteControl") or {}
                return (rc["pinnedEmail"], rc.get("pinnedOrganizationUuid") or "") \
                    if rc.get("pinnedEmail") else None

            def apply_pin(self, switcher, email=None, org=None, *a, **k):
                # The REAL signature: (switcher, email, org). A stub taking a
                # path described a call production never makes.
                (switcher.backup_dir / "settings.json").write_text(json.dumps(
                    {"remoteControl": {"pinnedEmail": email,
                                       "pinnedOrganizationUuid": org}} if email else {}))
                return True

            def live_remote_control_sessions(self):
                if rc_raises:
                    raise RuntimeError(
                        "GET http://svc:s3cr3t@127.0.0.1:9901/sessions failed")
                return []

        return _I()

    def test_a_note_that_raises_does_not_fail_a_pin_that_worked(
        self, tmp_path, monkeypatch, capsys
    ):
        from claude_swap import pin

        sw = self._sw(tmp_path)
        monkeypatch.setattr(
            pin, "_impl", lambda: self._impl(sw.backup_dir, rc_raises=True)
        )
        rc = pin.run(sw, "2")
        out = capsys.readouterr().out
        assert rc == 0, f"a pin that succeeded returned a failure code: {out}"
        assert "Pinned" in out, out
        recorded = json.loads((sw.backup_dir / "settings.json").read_text())
        assert (recorded.get("remoteControl") or {}).get("pinnedEmail") == \
            "user2@example.com", "the pin is on disk — reporting failure invites --clear"

    def test_an_unreadable_pin_file_is_no_pin_not_a_broken_package(
        self, tmp_path, monkeypatch, capsys
    ):
        """The read-only path. The TUI badge answers None in this exact state."""
        from claude_swap import pin

        sw = self._sw(tmp_path)
        monkeypatch.setattr(
            pin, "_impl", lambda: self._impl(sw.backup_dir, load_raises=True)
        )
        rc = pin.run(sw, None)
        out = capsys.readouterr().out
        assert rc == 0, "a malformed pin file made a read-only command fail"
        assert "No cloud account pinned" in out, out

    def test_the_cli_catch_all_scrubs_credentials(self, monkeypatch, capsys):
        """`_safe` exists for exactly this renderer, and it was the one
        renderer not using it.

        DRIVEN, NOT GREPPED. This asserted `"_safe(e)" in
        inspect.getsource(cli._pin_command)`, which is wrong in both
        directions: renaming the caught exception to `exc` breaks it against a
        change that is entirely correct, and the literal appearing ANYWHERE in
        the function — a comment, an unrelated branch — satisfies it while the
        catch-all prints the credential raw. A test keyed on source text
        asserts how the code is spelled, not what it does.
        """
        import claude_swap.cli as cli
        from claude_swap.pin import _safe

        leaky = "GET http://svc:s3cr3t@127.0.0.1:9901/sessions failed"
        assert "s3cr3t" not in _safe(ValueError(leaky)), _safe(ValueError(leaky))

        def _boom(*_a, **_k):
            raise ValueError(leaky)

        monkeypatch.setattr("claude_swap.pin.run", _boom)
        monkeypatch.setattr(cli, "ClaudeAccountSwitcher", lambda **k: object())
        monkeypatch.setattr(cli, "_guard_root", lambda s: None)
        with pytest.raises(SystemExit) as exc:
            cli._pin_command([])
        assert exc.value.code == 1, exc.value.code

        err = capsys.readouterr().err
        assert "installed but not usable" in err, (
            f"the catch-all did not render at all, so this proves nothing "
            f"about scrubbing: {err!r}"
        )
        assert "s3cr3t" not in err, (
            f"the catch-all printed the proxy credential verbatim: {err!r}"
        )


class TestAWiringWeCannotReadIsNotAWiringThatIsDead:
    """`_wiring_present` keys on the MARKER; `_wired_port_is_serving` reads only
    `CSWAP_PIN_PORT`. A config carrying the marker and no port is therefore
    "wired" and "not serving" at the same time, so `_wiring_is_stale` is True
    and the wiring goes — against a live proxy.

    Today's writer always emits `CSWAP_PIN_PORT`, so this is not reachable
    through it. But the seam's own stated threat model is that the package is a
    PEER on an independent release schedule (`_impl`'s comment says exactly
    that), and the seam refuses to trust its RETURN VALUE while trusting its
    FILE FORMAT with the destructive operation. "I cannot tell" must not read
    as "it is dead".
    """

    def _wired_without_port(self, tmp_path):
        import types

        backup = tmp_path / "b"
        backup.mkdir()
        (backup / "settings.json").write_text(
            json.dumps({"remoteControl": {"pinnedEmail": "c@e.com"}})
        )
        cfg = tmp_path / ".claude.json"
        cfg.write_text(json.dumps({
            # The marker is present. The port is not — a shape the seam must
            # not read as "nothing is serving".
            "env": {"HTTPS_PROXY": "http://127.0.0.1:0"},
            "_cswapPinWiredKeys": ["HTTPS_PROXY"],
        }))
        return types.SimpleNamespace(
            backup_dir=backup,
            _write_json=lambda p, d: p.write_text(json.dumps(d), encoding="utf-8"),
        ), cfg

    def test_a_marker_with_no_port_is_not_reported_as_stale(
        self, tmp_path, monkeypatch
    ):
        from claude_swap import pin

        sw, cfg = self._wired_without_port(tmp_path)
        import claude_swap.paths as paths

        monkeypatch.setattr(paths, "get_global_config_path", lambda: cfg)
        monkeypatch.setattr(paths, "get_default_global_config_path", lambda: cfg)

        assert pin._wiring_present(sw) is True, "fixture is not wired"
        assert bool(pin._dead_wired_configs(sw)) is False, (
            "a wiring whose port cannot be read was called stale — the next "
            "launch would tear down a pin that may be perfectly live"
        )

    @pytest.mark.parametrize("with_package", [False, True])
    def test_a_LIVE_sibling_does_not_hide_the_config_that_cannot_be_read(
        self, tmp_path, monkeypatch, with_package
    ):
        """PER-CONFIG CONDITION, MACHINE-WIDE ANSWER — this file's own defect,
        reintroduced by the message that reports it.

        The branch keys on `_wiring_present(...) and not _wired_ports()`, and
        both are "does ANY config". So with the default config wired to a LIVE
        port and the session config wired to a hand-edited one,
        `_wired_ports()` is non-empty because of the default, the branch never
        fires, and `--heal` prints "Nothing to heal" without ever mentioning
        the config it cannot check. Exactly the masking
        `_wired_port_is_serving`'s comment describes and that
        `_dead_wired_configs` fixes per-config everywhere else.
        """
        import types

        from claude_swap import pin
        import claude_swap.paths as paths

        backup = tmp_path / "b"
        backup.mkdir()
        # The listener helper lives on the class that owns the serving-pin
        # tests; a second copy is how two "a port that answers" fixtures drift.
        srv, live = TestHealNeverTearsDownAServingPin._serving()
        try:
            good = tmp_path / "default.json"
            good.write_text(json.dumps({
                "env": {"HTTPS_PROXY": f"http://127.0.0.1:{live}",
                        "CSWAP_PIN_PORT": str(live)},
                "_cswapPinWiredKeys": ["HTTPS_PROXY", "CSWAP_PIN_PORT"],
            }))
            # Marker present, port unreadable — the range check at the read
            # turns a hand-edit into "no opinion", which is the whole point.
            bad = tmp_path / "session.json"
            bad.write_text(json.dumps({
                "env": {"HTTPS_PROXY": "http://127.0.0.1:99999",
                        "CSWAP_PIN_PORT": "99999"},
                "_cswapPinWiredKeys": ["HTTPS_PROXY", "CSWAP_PIN_PORT"],
            }))
            sw = types.SimpleNamespace(
                backup_dir=backup,
                _write_json=lambda p, d: p.write_text(json.dumps(d), encoding="utf-8"),
            )
            monkeypatch.setattr(paths, "get_global_config_path", lambda: bad)
            monkeypatch.setattr(paths, "get_default_global_config_path", lambda: good)
            # BOTH ARMS, because `heal` has a serving exit on each and they are
            # the sibling call sites this branch keeps being bitten by: fixing
            # the one a test happens to reach leaves the other saying "Nothing
            # to heal". Measured — with only the no-package arm, reverting the
            # package-present exit killed nothing.
            if with_package:
                monkeypatch.setattr(
                    pin, "_live_impl",
                    lambda: types.SimpleNamespace(
                        # False is also what the package returns for "already
                        # serving", which is the state this fixture is in.
                        heal=lambda _d: False,
                    ),
                )
            else:
                monkeypatch.setattr(pin, "_live_impl", lambda: None)

            # The premise: without this the assertion below could pass because
            # the fixture never built the masking shape.
            assert pin._wired_ports(), "no port anywhere — nothing to mask with"
            assert pin._port_of_config(bad) is None, "the bad port reads fine"

            changed, message = pin.heal(sw)

            assert not changed, message
            assert str(bad) in message, (
                f"heal did not name the config it cannot check, so the live "
                f"sibling hid it: {message!r}"
            )
        finally:
            srv.close()

    def test_heal_does_not_tear_down_a_marker_with_no_port(
        self, tmp_path, monkeypatch
    ):
        """`_wiring_is_stale` refuses to call this shape stale (the test
        above), but `heal` never asks it: it calls `_wired_port_is_serving`
        directly and unwires on `_wiring_present` alone. So the exact config
        the guard declares must not be touched is the one `heal` tears down —
        and `heal` is the worse of the two to leave unguarded, because the
        status line calls it on a timer, unattended, while the launch path
        runs once.

        Asserts on the STATE ON DISK (is the marker still there), not on
        which branch ran — the fix must survive a refactor of heal's control
        flow, not just today's shape of it.
        """
        from claude_swap import pin

        sw, cfg = self._wired_without_port(tmp_path)
        import claude_swap.paths as paths

        monkeypatch.setattr(paths, "get_global_config_path", lambda: cfg)
        monkeypatch.setattr(paths, "get_default_global_config_path", lambda: cfg)
        monkeypatch.setattr(pin, "_live_impl", lambda: None)  # package removed

        changed, message = pin.heal(sw)

        assert pin._wiring_present(sw) is True, (
            "heal tore down a wiring whose port could not be read — the "
            "same inference _wiring_is_stale exists to forbid"
        )
        # NOT ACTING IS NOT A REASON TO REPORT THE ALL-CLEAR. Refusing to
        # condemn this shape is right — "I cannot tell" is not "it is dead" —
        # but `cswap pin --heal` is the command this module's own messages
        # send a stranded user to, and it printed "Nothing to heal" over a
        # wiring whose port it could not read. That is this file's signature
        # defect (see the capitals in `heal`) reached through a different
        # door: the only signal the user gets says nothing is wrong.
        assert not changed, message
        assert message != "Nothing to heal", (
            "heal reported the all-clear over a wiring it could not check — "
            "the user's one signal during the fault says there is no fault"
        )
        assert "CSWAP_PIN_PORT" in message, (
            f"the message does not name what to fix: {message!r}"
        )


class TestTheDeadPortCanBeInTheOtherConfig:
    """`_wiring_is_stale` short-circuited False whenever THIS process's OWN
    config (what the per-config read, `_wired_port_of`, used — since deleted
    as dead code, see Task 3) named no port — even when a DIFFERENT, genuinely
    wired config named a port nothing serves. `_wiring_is_stale` gates a
    WHOLE-MACHINE action (`clear_wiring` clears every config carrying the
    marker), so bailing out on "my own config has no opinion" made a dead
    wiring in the OTHER config permanently unreachable. The Background note
    makes this the COMMON case, not a corner one: the process that heals is
    normally the one whose own config is unwired.

    (real path getters, no monkeypatching of pin internals, package
    uninstalled) — session config (this process's own) unwired, default
    config (~/.claude.json) wired to a dead port:

        _wired_ports          [dead-port]
        _wired_port_of        None      (the per-config read, since deleted)
        _wiring_is_stale      False    <- "do not touch"
        heal()                (False, 'Nothing to heal')
        ~/.claude.json after  DEAD PORT SURVIVES

    The parent commit (0cff56c), before that per-config read was narrowed to
    one file, True / (True, 'Removed a cloud pin wiring…') / {} for
    the same scenario.
    """

    def _sw(self, tmp_path):
        import types

        backup = tmp_path / "b"
        backup.mkdir()
        (backup / "settings.json").write_text(
            json.dumps({"remoteControl": {"pinnedEmail": "c@e.com"}})
        )
        return types.SimpleNamespace(
            backup_dir=backup,
            _write_json=lambda p, d: p.write_text(json.dumps(d), encoding="utf-8"),
        )

    def test_a_dead_port_in_the_other_config_is_reachable(
        self, tmp_path, monkeypatch
    ):
        """THE REGRESSION. The process's own config names no port (nothing
        to say), the OTHER config names a genuinely dead one — the guard
        must still see it, not bail out on "I cannot tell about MY config"."""
        from claude_swap import pin
        import claude_swap.paths as paths

        cfg_own = _cfg(tmp_path, "session", marker=False)
        dead = _dead_port()
        cfg_other = _cfg(tmp_path, "default", dead)

        monkeypatch.setattr(paths, "get_global_config_path", lambda: cfg_own)
        monkeypatch.setattr(paths, "get_default_global_config_path", lambda: cfg_other)

        assert bool(pin._dead_wired_configs(None)) is True, (
            "a dead port in the OTHER config is unreachable when this "
            "process's own config has no opinion — it must still be healed"
        )

    def test_heal_clears_a_dead_wiring_that_lives_only_in_the_other_config(
        self, tmp_path, monkeypatch
    ):
        from claude_swap import pin
        import claude_swap.paths as paths

        sw = self._sw(tmp_path)
        cfg_own = _cfg(tmp_path, "session", marker=False)
        dead = _dead_port()
        cfg_other = _cfg(tmp_path, "default", dead)

        monkeypatch.setattr(paths, "get_global_config_path", lambda: cfg_own)
        monkeypatch.setattr(paths, "get_default_global_config_path", lambda: cfg_other)
        monkeypatch.setattr(pin, "_live_impl", lambda: None)  # package removed

        changed, msg = pin.heal(sw)
        assert changed, msg
        assert "_cswapPinWiredKeys" not in json.loads(cfg_other.read_text(encoding="utf-8"))

    def test_the_guard_does_not_fire_when_the_other_configs_port_is_live(
        self, tmp_path, monkeypatch
    ):
        """The other direction: a guard that ALWAYS fires whenever this
        process's own config has no port reintroduces the masked-dead-port defect. When
        the only port anywhere on the machine is actually serving, nothing is
        stale."""
        import socket

        from claude_swap import pin
        import claude_swap.paths as paths

        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            cfg_own = _cfg(tmp_path, "session", marker=False)
            cfg_other = _cfg(tmp_path, "default", port)  # actually live

            monkeypatch.setattr(paths, "get_global_config_path", lambda: cfg_own)
            monkeypatch.setattr(
                paths, "get_default_global_config_path", lambda: cfg_other
            )
            assert bool(pin._dead_wired_configs(None)) is False, (
                "a live port in the other config was read as stale"
            )
        finally:
            srv.close()


class TestTheGuardIsPerConfigNotPerMachine:
    """Originally a unit-test class for `_wired_port_of`'s own per-config
    contract (it once was `next(iter(_wired_ports()), None)`, which returned
    the first port found on the MACHINE rather than the port of the config
    being judged). `_wired_port_of` had zero production callers — its only
    consumers were three assertions in this file — and mutating it to
    `return None` unconditionally survived the whole suite; it was deleted
    as dead code (Task 3). The remaining test below exercises
    `_wiring_is_stale` directly instead, which is what actually matters in
    production and does not need the deleted helper to pin its behaviour.

    `_wiring_is_stale`'s own short-circuit was a LATER round (Task 1 of the
    same brief) deliberately made per-MACHINE: `_wiring_is_stale` gates a
    whole-machine `clear_wiring`, and bailing out on "MY config has no port"
    left a dead port sitting in the OTHER config permanently unreachable —
    see `TestTheDeadPortCanBeInTheOtherConfig`. The test below must not assert
    the opposite (that a dead default must NOT make a portless session's
    wiring look stale); that assertion is what Task 1's fix legitimately
    overturns, so it is re-aimed rather than kept red.
    """

    def _wired_no_port(self, tmp_path, name):
        """A config carrying the marker with no CSWAP_PIN_PORT — the shape
        Task 1's fixture uses, now on ONE of two configs rather than the
        only one.

        The marker list must be NON-EMPTY: `_wire_mark_of` returns None for
        an empty list BY DESIGN, so `"_cswapPinWiredKeys": []` is not wired
        at all — this config would not even satisfy `_wiring_present`, which
        makes it a config the caller would never look at twice, not the
        "carries the marker, no readable port" shape this fixture claims to
        build. (Fixed here — an earlier version wrote `[]` and every test
        built on it was asserting on a config that was silently unwired.)

        No HTTPS_PROXY value: neither predicate under test reads it, so a
        port in there would describe a proxy nothing asks about."""
        d = tmp_path / name
        d.mkdir()
        cfg = d / ".claude.json"
        cfg.write_text(json.dumps({
            "env": {},
            "_cswapPinWiredKeys": ["HTTPS_PROXY"],
        }))
        return cfg

    def _wired_with_port(self, tmp_path, name, port):
        d = tmp_path / name
        d.mkdir()
        cfg = d / ".claude.json"
        cfg.write_text(json.dumps({
            "env": {"CSWAP_PIN_PORT": str(port)},
            "_cswapPinWiredKeys": ["CSWAP_PIN_PORT"],
        }))
        return cfg

    def test_a_dead_default_correctly_condemns_a_portless_session_too(
        self, tmp_path, monkeypatch
    ):
        """RE-AIMED (see class docstring). Session config in the new shape
        (marker, no port — "I cannot tell") + default config carrying an old
        wiring naming a port that is CONFIRMED DEAD:

            _wired_ports       : [dead-port]
            _wiring_is_stale   : True    <- machine-wide: something IS dead
            session still wired: False   <- cleared along with the default

        Asserting `is False` here assumes the session's
        portless wiring must never be judged by a different config's port.
        Task 1 overturns that: `_wiring_is_stale` gates `clear_wiring`, which
        already clears BOTH configs as one operation (its own docstring:
        "BOTH configs... Clearing only the resolved path leaves the other
        wired"), so there is no per-config selective clear to preserve here.
        Once ANY config on the machine names a port that is confirmed dead,
        "nothing is known to be dead" is no longer true of the machine, and
        `_wired_port_is_serving`'s own rule — EVERY wired config must serve,
        not merely one — already treats the portless session's silence as no
        opinion, never as a counter-vote. The alternative (bailing out
        because the session itself has "no opinion") is exactly the Task 1
        regression: a dead port stuck behind a portless config becomes
        permanently unreachable.
        """
        from claude_swap import pin
        import claude_swap.paths as paths

        cfg_session = self._wired_no_port(tmp_path, "session")
        cfg_default = self._wired_with_port(tmp_path, "default", _dead_port())

        monkeypatch.setattr(paths, "get_global_config_path", lambda: cfg_session)
        monkeypatch.setattr(paths, "get_default_global_config_path", lambda: cfg_default)

        assert bool(pin._dead_wired_configs(None)) is True, (
            "a confirmed-dead port in the default config must make the "
            "guard fire, even though the session's own config has no "
            "opinion — otherwise that dead port is unreachable (Task 1)"
        )


class TestTheMarkerGuardIsNotJustTheStalenessVerdict:
    """`_wiring_is_stale`'s old short-circuit, `if _wired_port_of(_switcher)
    is None: return False`, was doing TWO jobs at once. Job one — the
    per-config staleness verdict — is what `TestTheDeadPortCanBeInTheOtherConfig`
    correctly overturned into `if not _wired_ports(): return False`. Job two
    went unremarked: `_wired_port_of` reads only the session config, so a
    port sitting in the OTHER config with NO `_cswapPinWiredKeys` marker at
    all (not cswap's wiring — a foreign `CSWAP_PIN_PORT`, or a future
    `cswap-pin` that stops writing the marker) returns None and blocks
    right there. `_wired_ports()` reads both configs' ports and does not
    check the marker, so that block is gone: only `_wiring_present`
    (checked first, still gating the whole function) now stands between
    such a config and `clear_wiring`.

    Deleting the `if not _wiring_present(_switcher): return False` guard
    SURVIVES the full suite (107 passed when this was
    found). This class exists to make that mutation fail.
    """

    def _sw(self, tmp_path):
        import types

        backup = tmp_path / "b"
        backup.mkdir()
        (backup / "settings.json").write_text(json.dumps({}))
        return types.SimpleNamespace(
            backup_dir=backup,
            _write_json=lambda p, d: p.write_text(json.dumps(d), encoding="utf-8"),
        )

    def _unmarked_dead_port_config(self, tmp_path, name):
        """A config naming a dead `CSWAP_PIN_PORT` with NO
        `_cswapPinWiredKeys` marker — not a wiring cswap ever recorded."""
        d = tmp_path / name
        d.mkdir()
        cfg = d / ".claude.json"
        dead = _dead_port()
        cfg.write_text(
            json.dumps({"env": {"CSWAP_PIN_PORT": str(dead)}}), encoding="utf-8"
        )
        return cfg

    def test_a_dead_port_with_no_marker_is_not_stale(self, tmp_path, monkeypatch):
        """`_wiring_present` is False (no marker anywhere) — there is nothing
        of cswap's to condemn, so `_wiring_is_stale` must say so too, even
        though `_wired_ports()` sees a dead port sitting in that file."""
        from claude_swap import pin
        import claude_swap.paths as paths

        cfg_own = _cfg(tmp_path, "session", marker=False)
        cfg_other = self._unmarked_dead_port_config(tmp_path, "default")

        monkeypatch.setattr(paths, "get_global_config_path", lambda: cfg_own)
        monkeypatch.setattr(paths, "get_default_global_config_path", lambda: cfg_other)

        assert pin._wiring_present(None) is False, (
            "fixture invalid: a marker exists somewhere"
        )
        # THE PORT IS FILTERED AT THE SOURCE NOW, not one scope up.
        # `_port_of_config` asks `_wire_mark_of` before it reads
        # CSWAP_PIN_PORT, so an unmarked config has no port to offer any
        # consumer — which is what stopped a FOREIGN dead port from making
        # the verdict True while cswap's own marked wiring was serving.
        # Before, this read `!= []` and the whole guard lived in
        # `_wiring_is_stale`.
        assert pin._wired_ports() == [], (
            "an unmarked config still offers its port to every consumer — "
            "the marker guard has to hold at the read, not only in the one "
            "caller that remembered to ask"
        )
        assert bool(pin._dead_wired_configs(None)) is False, (
            "a config with no _cswapPinWiredKeys marker was condemned as "
            "stale wiring — nothing here is cswap's to remove"
        )

    def test_heal_does_not_touch_a_config_with_no_marker(self, tmp_path, monkeypatch):
        """End to end: `heal` must not report a removal, and the config
        (which cswap never wired) must be byte-for-byte unchanged."""
        from claude_swap import pin
        import claude_swap.paths as paths

        sw = self._sw(tmp_path)
        cfg_own = _cfg(tmp_path, "session", marker=False)
        cfg_other = self._unmarked_dead_port_config(tmp_path, "default")
        before = cfg_other.read_text(encoding="utf-8")

        monkeypatch.setattr(paths, "get_global_config_path", lambda: cfg_own)
        monkeypatch.setattr(paths, "get_default_global_config_path", lambda: cfg_other)
        monkeypatch.setattr(pin, "_live_impl", lambda: None)  # package removed

        changed, msg = pin.heal(sw)

        assert not changed, (
            f"heal reported a removal ({msg!r}) over a config it never "
            "wired — the marker guard that should have blocked this is gone"
        )
        assert cfg_other.read_text(encoding="utf-8") == before, (
            "heal rewrote a config with no _cswapPinWiredKeys marker"
        )


class TestOneConfigIsOneOpinion:
    """The two config getters resolve to the SAME file outside a session
    terminal, and `_wired_ports` de-dups on that. Nothing tested the de-dup:
    deleting it left the whole suite green (as a mutation), because
    every fixture that points both getters at one file also asserts on
    outcomes a doubled reading happens not to change.

    It matters to the caller that COUNTS. `_wired_port_is_serving` returns
    `bool(ports)` and probes each entry, so a doubled list makes one config
    look like two agreeing opinions and doubles the connect attempts against
    a port that may be refusing — the slow path, on the launch path.
    """

    def test_one_file_seen_through_both_getters_is_counted_once(
        self, tmp_path, monkeypatch
    ):
        from claude_swap import pin

        port = _dead_port()
        cfg = tmp_path / ".claude.json"
        cfg.write_text(
            json.dumps(
                {
                    "env": {
                        "HTTPS_PROXY": f"http://127.0.0.1:{port}",
                        "CSWAP_PIN_PORT": str(port),
                    },
                    "_cswapPinWiredKeys": ["HTTPS_PROXY", "CSWAP_PIN_PORT"],
                }
            )
        )
        import claude_swap.paths as paths

        # THE COMMON CASE, not a contrived one: outside a session terminal
        # both getters resolve to ~/.claude.json.
        monkeypatch.setattr(paths, "get_global_config_path", lambda: cfg)
        monkeypatch.setattr(paths, "get_default_global_config_path", lambda: cfg)

        assert pin._wired_ports() == [port], (
            "one config read through both getters produced more than one "
            "opinion — the serving probe would count a single file twice"
        )

    def test_two_distinct_configs_are_two_opinions(self, tmp_path, monkeypatch):
        """The de-dup must key on the PATH, not collapse everything to one:
        two genuinely different configs each get a say (that asymmetry is why
        `_wired_port_is_serving` requires every named port to answer).
        """
        from claude_swap import pin

        ports = []
        paths_ = []
        for name in ("session", "default"):
            d = tmp_path / name
            d.mkdir()
            p = _dead_port()
            cfg = d / ".claude.json"
            cfg.write_text(
                json.dumps(
                    {
                        "env": {
                            "HTTPS_PROXY": f"http://127.0.0.1:{p}",
                            "CSWAP_PIN_PORT": str(p),
                        },
                        "_cswapPinWiredKeys": ["HTTPS_PROXY", "CSWAP_PIN_PORT"],
                    }
                )
            )
            ports.append(p)
            paths_.append(cfg)
        import claude_swap.paths as paths

        monkeypatch.setattr(paths, "get_global_config_path", lambda: paths_[0])
        monkeypatch.setattr(paths, "get_default_global_config_path", lambda: paths_[1])

        assert pin._wired_ports() == ports, (
            "two distinct configs did not produce two opinions — a dead "
            "default config would hide behind a live session one"
        )

    def test_a_box_with_no_wiring_is_not_reported_as_serving(
        self, tmp_path, monkeypatch
    ):
        """No config names a port, so there is nothing to serve.

        PRE-EXISTING GAP, not one this refactor introduced: mutating the final
        `return bool(ports)` (`return any_wired` before the shared walk) to
        `return True` left the suite green at HEAD too — 89 passed.

        `_wiring_is_stale` is `wired and not serving`, so a serving probe that
        answers True unconditionally makes every stale wiring look healthy and
        `heal` reports "Nothing to heal" forever — the same silent-failure
        shape as the OR-over-configs bug this function's own comment records,
        just reached from the other end.
        """
        from claude_swap import pin

        cfg = tmp_path / ".claude.json"
        cfg.write_text(json.dumps({"env": {"UNRELATED": "keep me"}}))
        import claude_swap.paths as paths

        monkeypatch.setattr(paths, "get_global_config_path", lambda: cfg)
        monkeypatch.setattr(paths, "get_default_global_config_path", lambda: cfg)

        assert pin._wired_ports() == [], "fixture names a port"
        assert pin._wired_port_is_serving(None) is False, (
            "a box with no pin wiring at all was reported as SERVING — "
            "_wiring_is_stale then reads every dead wiring as healthy and "
            "heal answers 'Nothing to heal' forever"
        )


class TestNoFixtureNamesARealDaemonPort:
    """A fixture that hardcodes a port a real daemon uses describes the
    opposite of what it claims, on exactly the machines that run the pin.

    This was found once and fixed in the heal tests; the module-level
    `_dead_port` helper carries the reasoning: "these tests once used 36301 …
    so on a machine where the pin was actually running they described a LIVE
    wiring while claiming to describe a dead one, and every assertion about
    healing was inverted."

    The same literal survived in other fixtures. It is a lint, not a scenario:
    the number cannot be right, because the test cannot know what the machine
    is running.
    """

    def test_no_test_fixture_hardcodes_the_pin_daemon_port(self):
        import pathlib

        here = pathlib.Path(__file__)
        offenders = _port_literal_offenders(here.parent, here, self.__class__.__name__)
        assert not offenders, (
            "a fixture hardcodes 36301, the port a real pin daemon uses — on a "
            "machine running the pin these describe a LIVE wiring while "
            "claiming a dead one:\n  " + "\n  ".join(offenders)
        )

    def test_a_same_named_class_in_another_file_is_not_exempted(self, tmp_path):
        """The self-exemption must not key on the class name ALONE
        (``n.name == self.__class__.__name__``), with no check that the
        matching class lives in the lint's OWN file. A routine rename of
        this class — or a copy of it landing in another test file, same
        name — would silently exempt whatever a class of that name
        contains anywhere in the tree, including a genuine 36301 hardcode
        that is exactly what this lint exists to catch.

        The exemption must be scoped to the lint's own file, not to any
        file that happens to define a same-named class.
        """
        decoy = tmp_path / "test_decoy.py"
        decoy.write_text(
            f"class {self.__class__.__name__}:\n"
            "    def test_x(self):\n"
            "        port = 36301\n"
            "        assert port\n",
            encoding="utf-8",
        )
        offenders = _port_literal_offenders(
            tmp_path, tmp_path / "test_lint_home.py", self.__class__.__name__
        )
        assert offenders, (
            "a class in ANOTHER file sharing the lint's own class name "
            "silently exempted a real 36301 hardcode"
        )

    def test_conftest_py_is_scanned_too(self, tmp_path):
        """``glob('test_*.py')`` does not match ``conftest.py`` — exactly
        the file a shared fixture would most naturally live in, and exactly
        the file this lint's own docstring never mentions as excluded.
        """
        (tmp_path / "conftest.py").write_text("port = 36301\n", encoding="utf-8")
        offenders = _port_literal_offenders(
            tmp_path, tmp_path / "nonexistent.py", self.__class__.__name__
        )
        assert offenders, (
            "conftest.py was not scanned — a hardcoded 36301 placed there "
            "would slip past this lint entirely"
        )

    def test_an_unrelated_def_in_own_file_is_not_exempted(self, tmp_path):
        """The own-file exemption must not be
        ``is_own_file and isinstance(n, (ast.ClassDef, ast.FunctionDef))``
        with no check on WHICH def — that exempts every class/function in
        own_file, not just the lint's own code (``own_names``). A genuine
        36301 hardcode inside some unrelated function in the SAME file as
        the lint must still be caught; only defs named in ``own_names`` may
        be skipped.
        """
        own_file = tmp_path / "test_lint_home.py"
        own_file.write_text(
            f"class {self.__class__.__name__}:\n"
            "    def test_x(self):\n"
            "        pass\n"
            "\n"
            "def unrelated_helper():\n"
            "    port = 36301\n"
            "    return port\n",
            encoding="utf-8",
        )
        offenders = _port_literal_offenders(tmp_path, own_file, self.__class__.__name__)
        assert offenders, (
            "a real 36301 hardcode inside a function that is NOT the "
            "lint's own code was exempted merely for living in own_file"
        )

    def test_a_comment_mentioning_the_literal_is_not_an_offender(self, tmp_path):
        """A comment such as ``# never hardcode 36301`` MENTIONS the literal
        without hardcoding it as a value — only lines that are not comments
        count.
        """
        (tmp_path / "test_comment.py").write_text(
            "# reasoning: never hardcode 36301, call _dead_port() instead\n",
            encoding="utf-8",
        )
        offenders = _port_literal_offenders(
            tmp_path, tmp_path / "nonexistent.py", self.__class__.__name__
        )
        assert not offenders, (
            "a comment merely mentioning 36301 was flagged as a hardcode: "
            + "\n  ".join(offenders)
        )


class TestHealSurvivesAMalformedPinPort:
    """`_port_of_config` does `int(env.get("CSWAP_PIN_PORT") or 0)` and
    range-checks nothing. `socket.connect` raises `OverflowError`, not
    `OSError`, for a port outside 0-65535 — and `_wired_port_is_serving`
    catches only `OSError`. Both its call sites inside `heal` sit OUTSIDE the
    bottom `try`, so a config naming a malformed port takes `cswap pin
    --heal` down with a traceback instead of the exit-0-and-a-message `heal`
    documents.

    PRE-EXISTING, not introduced by the last round. Reproduced at the
    `pin.run(..., heal_only=True)` level — the call the status line actually
    makes on its timer, unattended — not at the predicate level.
    """

    def _sw_with_port(self, tmp_path, port_value: str):
        import types

        backup = tmp_path / "b"
        backup.mkdir()
        (backup / "settings.json").write_text(json.dumps({}))
        cfg = tmp_path / ".claude.json"
        cfg.write_text(
            json.dumps(
                {
                    "env": {"CSWAP_PIN_PORT": port_value},
                    "_cswapPinWiredKeys": ["CSWAP_PIN_PORT"],
                }
            )
        )
        sw = types.SimpleNamespace(
            backup_dir=backup,
            _write_json=lambda p, d: p.write_text(json.dumps(d), encoding="utf-8"),
        )
        return sw, cfg

    @pytest.mark.parametrize("port_value", ["99999", "70000", "-1", "4294967296"])
    @pytest.mark.parametrize(
        "impl_present", [True, False], ids=["package-present", "package-absent"]
    )
    def test_heal_does_not_raise_on_a_malformed_port(
        self, tmp_path, monkeypatch, port_value, impl_present
    ):
        from claude_swap import pin
        import claude_swap.paths as paths

        sw, cfg = self._sw_with_port(tmp_path, port_value)
        monkeypatch.setattr(paths, "get_global_config_path", lambda: cfg)
        monkeypatch.setattr(paths, "get_default_global_config_path", lambda: cfg)

        if impl_present:

            class _I:
                def heal(self, backup_dir):
                    return False  # could not restart

            monkeypatch.setattr(pin, "_live_impl", lambda: _I())
        else:
            monkeypatch.setattr(pin, "_live_impl", lambda: None)

        # Must not raise, and must exit 0 — this is exactly what the status
        # line calls on its timer.
        assert pin.run(sw, None, heal_only=True) == 0


class TestTheSiblingGettersGuardTheirPathGettersToo:
    """`_wired_ports` learned (Task 1 of the last round) that a path getter
    can itself raise: `get_default_global_config_path` calls `Path.home()`,
    which raises `RuntimeError` when HOME is unset and the uid has no
    `/etc/passwd` entry (the standard rootless-container shape). It wrapped
    its own `get()` calls in a `try`. `_wiring_present` and `clear_wiring`
    resolve the SAME two getters and were never given the same guard.

    (no HOME, `Path.home` raising) —

        _wired_ports     guards its path getters: True   (returns [])
        _wiring_present  guards its path getters: False  (raises)
        clear_wiring     guards its path getters: False  (raises)

    `heal` survives today only because `_wiring_present`'s raise happens
    INSIDE the bottom `try` in `heal`. One refactor moving that call above
    the `try` — exactly the kind of change `_wired_ports`' own call sites
    already went through once — reintroduces the traceback `heal` documents
    as never happening.
    """

    def _no_home(self, monkeypatch):
        import pathlib

        monkeypatch.delenv("HOME", raising=False)
        monkeypatch.setattr(
            pathlib.Path,
            "home",
            staticmethod(lambda: (_ for _ in ()).throw(RuntimeError("no HOME"))),
        )

    def test_wiring_present_does_not_raise_with_no_home(self, monkeypatch):
        from claude_swap import pin

        self._no_home(monkeypatch)
        # Must not raise: an unresolvable default-config path is "no opinion",
        # exactly as `_wired_ports` already treats it.
        assert pin._wiring_present(None) is False

    def test_clear_wiring_does_not_raise_with_no_home(self, tmp_path, monkeypatch):
        from claude_swap import pin
        import claude_swap.paths as paths

        # The SESSION config still resolves fine (CLAUDE_CONFIG_DIR is set) —
        # only the DEFAULT getter's Path.home() call is broken. This is the
        # realistic shape: a session terminal's own config is always
        # resolvable via CLAUDE_CONFIG_DIR; it is `~/.claude.json` that needs
        # a resolvable home.
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        self._no_home(monkeypatch)

        import types

        sw = types.SimpleNamespace(
            backup_dir=tmp_path,
            _write_json=lambda p, d: p.write_text(json.dumps(d), encoding="utf-8"),
        )
        # Must not raise: a config this call cannot even locate is "nothing
        # to remove there", not a crash.
        assert pin.clear_wiring(sw, timeout=0.1) is False

    def test_an_unresolvable_path_is_logged_not_silently_dropped(
        self, tmp_path, monkeypatch, caplog
    ):
        """A getter that raises is "no opinion" about ONE config — correct —
        but `clear_wiring`'s collector swallowed that exception with a bare
        ``except Exception: continue`` and no record of it anywhere. That
        makes "could not even be LOCATED" and "located, nothing wired" the
        same silence from outside — and `clear_wiring`'s bool is a claim
        about every path it REACHED, not that every path was reachable.
        Skipping is correct; the missing record is the bug.
        """
        import logging

        from claude_swap import pin

        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        self._no_home(monkeypatch)

        import types

        sw = types.SimpleNamespace(
            backup_dir=tmp_path,
            _write_json=lambda p, d: p.write_text(json.dumps(d), encoding="utf-8"),
        )
        with caplog.at_level(logging.DEBUG, logger="claude-swap"):
            pin.clear_wiring(sw, timeout=0.1)

        assert any(
            "get_default_global_config_path" in r.getMessage()
            for r in caplog.records
        ), (
            "an unresolvable path getter was swallowed with no record — "
            "indistinguishable from a config that resolved and had nothing "
            f"wired. Records: {[r.getMessage() for r in caplog.records]}"
        )


class TestEveryCallSiteRecordsAnUnresolvableGetter:
    """All three call sites resolve the same two getters and all three would
    swallow a raise with no record — `clear_wiring` logged, `_wiring_present`
    and `_wired_ports` said nothing.

    THE LEVEL IS PER CALL SITE, and there is no cap. A once-per-PROCESS cap
    suppresses nothing when every statusline tick is a fresh `cswap pin
    --heal`, and a WARNING on the two getters `heal` calls unconditionally
    turns "1 line ever" into "1 line per tick" — through the real
    CLI, 0 -> 6 lines over 6 ticks. So those two record at DEBUG.

    `clear_wiring` is the exception and warns: `heal` reaches it only through
    `_wiring_is_stale`, so it is gated rather than per-tick. Dropping it to
    DEBUG with the other two is the round-13 overcorrection
    `TestTheSelfLimitingCallSiteStillWarns` exists to catch.

    IT IS NOT THE PLACE THAT NAMES WHY A WIRING COULD NOT BE REMOVED, though
    four rounds of this docstring said so. That record is the lock WARNING at
    the BOTTOM of `clear_wiring` (`TestTheLockFailureThatStrandsTheWiringIsNamed`),
    added for exactly the shape this one cannot reach. This getter record's
    job is the narrower one: a config that could not be LOCATED is missing
    from `paths`, and `clear_wiring`'s bool only ever claimed to cover the
    paths it REACHED.
    """

    def _no_home(self, monkeypatch):
        monkeypatch.delenv("HOME", raising=False)
        monkeypatch.setattr(
            pathlib.Path,
            "home",
            staticmethod(lambda: (_ for _ in ()).throw(RuntimeError("no HOME"))),
        )

    @pytest.mark.parametrize(
        "name, call",
        [
            ("clear_wiring", lambda pin, sw: pin.clear_wiring(sw, timeout=0.1) is False),
            ("_wiring_present", lambda pin, sw: pin._wiring_present(None) is False),
            ("_wired_ports", lambda pin, sw: pin._wired_ports() == []),
        ],
    )
    def test_the_call_site_leaves_a_debug_record(
        self, name, call, tmp_path, monkeypatch, caplog
    ):
        import logging
        import types

        from claude_swap import pin

        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        self._no_home(monkeypatch)
        sw = types.SimpleNamespace(
            backup_dir=tmp_path,
            _write_json=lambda p, d: p.write_text(json.dumps(d), encoding="utf-8"),
        )
        with caplog.at_level(logging.DEBUG, logger="claude-swap"):
            assert call(pin, sw)

        assert any(
            "get_default_global_config_path" in r.getMessage()
            for r in caplog.records
        ), f"{name} swallowed an unresolvable getter with no record at all"

    # The two getters `heal` calls UNCONDITIONALLY, on every tick. Their level
    # is what the churn measurement is about. `clear_wiring` is deliberately
    # absent: `heal` reaches it only through `_wiring_is_stale`.
    #
    # BOTH TESTS BELOW KEY ON `record.funcName`, NOT ON LEVEL, and not on
    # `lineno`/`pathname` either. `pathname` is useless — every record comes
    # from `pin.py`. `lineno` is NOT useless, and the claim that it was is
    # wrong: `stacklevel=2` moves it to the caller exactly as it moves
    # `funcName`, so the unremovable tick yields THREE distinct linenos.
    # NOT one per `_log_unresolvable` site, as this said for two rounds:
    # there are THREE such sites and this shape reaches only TWO of them,
    # `_wired_ports` and `clear_wiring`'s getter site. The third lineno is
    # `clear_wiring`'s lock WARNING — a bare `_logger.warning`, not a
    # `_log_unresolvable` site at all. So the three are two getter sites plus
    # the lock WARNING, and the lock WARNING is what separates the two
    # records that share `funcName == "clear_wiring"`. Why this shape misses
    # `_wiring_present` is the last paragraph below, and it is not the reason
    # this comment gave for two rounds.
    #
    # `funcName` is chosen because a lineno key is BRITTLE, not because it
    # cannot discriminate. NO REVISION OF THIS PARAGRAPH has ever moved a
    # site in `pin.py` — the brittleness is that OTHER comment-only edits
    # keep moving the sites it names anyway. Across the whole branch all
    # four are moving targets: the getter below (447 -> 468 at `d343bfb`,
    # +17 COMMENT lines and +4 docstring lines above it — "not code" is
    # right, "a docstring" undersells it), the lock WARNING
    # (588 -> 633 -> 648), and both `_PER_TICK_SITES` getters,
    # `_wiring_present` (755 -> 800 -> 815) and `_wired_ports`
    # (1067 -> 1112 -> 1127 -> 1128, the last at `e6df933`). "Exactly N
    # moved" is a claim about a WINDOW, not a fixed count: those numbers
    # show three sites moving twice across `d343bfb` -> `f361237` ->
    # `6e03af7` while this getter sat still — `funcName` is immune to all
    # of it, for the `stacklevel=2` reason already given above. The
    # `two of them` in the two copies that point back here is that SAME
    # window narrowed to one set: the three linenos the UNREMOVABLE shape
    # reaches, of which the getter is the one that held.
    #
    # `_wiring_present`'s ABSENCE from the UNREMOVABLE funcName set is not
    # because it "fires solely on the REMOVABLE shape" — false, it
    # also fires on NOTHING-WIRED. It runs twice on a wired tick: once
    # inside `_wiring_is_stale` to gate the tick, once inside `heal` to
    # confirm `clear_wiring` worked. On UNREMOVABLE the marker survives the
    # failed removal, so the second call's `_wire_mark_of(raw) is not None:
    # return True` answers from the FIRST config before the raising getter
    # ever runs — an early return, not a shape restriction. REMOVABLE and
    # NOTHING-WIRED both reach it (marker gone / never present), and
    # `wired=False` in `test_the_per_tick_getters_stay_below_INFO` already
    # depends on that reach.
    _PER_TICK_SITES = ("_wiring_present", "_wired_ports")

    def _heal(self, tmp_path, monkeypatch, caplog, *, wired, removable=True):
        """One `heal` tick with an unresolvable `Path.home()`, wired or not.

        ``removable=False`` makes the config's DIRECTORY read-only, so
        `proper_lockfile`'s `os.mkdir` cannot create the lock and the wiring
        is stuck — the shape the bottom WARNING was added for. The config
        itself stays readable, so the tick gets all the way to the lock
        rather than failing earlier.
        """
        import logging
        import types

        from claude_swap import pin

        # `_cfg`, not a hand-rolled dict: it is the one place that decides the
        # marker list, and a fixture writing `[]` describes a config the code
        # never looks at twice (see its docstring).
        cfg_dir = _cfg(tmp_path, "session", _dead_port()).parent if wired else tmp_path
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg_dir))
        self._no_home(monkeypatch)
        monkeypatch.setattr(pin, "_live_impl", lambda: None)
        sw = types.SimpleNamespace(
            backup_dir=tmp_path,
            _write_json=lambda p, d: p.write_text(json.dumps(d), encoding="utf-8"),
        )
        if not removable:
            cfg_dir.chmod(0o500)
        try:
            with caplog.at_level(logging.DEBUG, logger="claude-swap"):
                pin.heal(sw)
        finally:
            if not removable:
                cfg_dir.chmod(0o700)  # or tmp_path cleanup cannot remove it
        return caplog.records

    def test_the_per_tick_getters_stay_below_INFO(self, tmp_path, monkeypatch, caplog):
        """The level is the whole point: WARNING here is per-tick log churn."""
        import logging

        records = self._heal(tmp_path, monkeypatch, caplog, wired=False)

        per_tick = [r for r in records if r.funcName in self._PER_TICK_SITES]
        # NOT VACUOUS. An origin filter that matches nothing passes for free,
        # which is precisely how the level-only version of this test passed:
        # with nothing wired, `_wiring_is_stale` is False and `heal` never
        # calls `clear_wiring` at all, so the assertion below was never once
        # applied to the call site whose level it was written to pin.
        # THE TWO WAYS THIS FILTER GOES EMPTY ARE DIFFERENT BUGS and it used
        # to name only one. "never reached them" is the vacuous-fixture case
        # this assertion exists for. But add ONE frame between a call site and
        # `_log_unresolvable`'s `log` call and `stacklevel=2` names the
        # wrapper instead of the caller: every record still fires, the filter
        # still empties, and the old message sent the reader looking for a
        # fixture fault that is not there. With that frame added:
        # `Origins seen: ['_log_unresolvable']` — the emitter itself, which
        # `stacklevel` exists to keep OUT of `funcName`.
        emitted = [r for r in records if "could not be resolved" in r.getMessage()]
        assert per_tick, (
            "no unresolvable-getter record came from "
            f"{self._PER_TICK_SITES}, so the level assertion below proves "
            + (
                "nothing — the getters DID log, but the records are "
                "attributed elsewhere: `_log_unresolvable`'s `stacklevel=2` "
                "no longer names its caller (a frame added in between?). "
                if emitted
                else "nothing — this tick never reached them. "
            )
            + f"Origins seen: {sorted({r.funcName for r in records})}"
        )
        # BELOW INFO, not merely below WARNING. The logger sits at INFO by
        # default (`logging_config.setup_logging`), so an INFO record reaches
        # the rotating file on every tick exactly as the WARNING did —
        # `debug` -> `info` passes all 128 tests while the real CLI
        # writes 12 lines over 6 ticks. A guard keyed on WARNING lets the
        # regression land again with CI green.
        loud = [r for r in per_tick if r.levelno >= logging.INFO]
        assert not loud, (
            "a getter `heal` calls unconditionally logged at or above INFO — "
            "the default logger level, so this reaches the file every tick: "
            f"{[(r.funcName, r.levelname, r.getMessage()) for r in loud]}"
        )

    @pytest.mark.parametrize(
        "removable",
        [
            True,
            # `removable=False` makes the config's DIRECTORY read-only, which
            # is a POSIX mechanism: on win32 a read-only directory does not
            # stop a write, and root ignores the mode bit, so the lock never
            # fails and the unwire WARNING this case exists to observe never
            # fires. On the Windows runner (test-windows red at
            # d343bfb, green on Linux and macOS):
            #
            #   Expected: [('clear_wiring','resolve',30),
            #              ('clear_wiring','unwire',30)]
            #   Got:      [('clear_wiring','resolve',30)]
            #
            # Same skip condition as
            # `TestTheLockFailureThatStrandsTheWiringIsNamed`, which pins the
            # same mechanism and already carried it. The REMOVABLE half runs
            # everywhere, so the platform-independent claim keeps its coverage.
            pytest.param(
                False,
                marks=pytest.mark.skipif(
                    sys.platform == "win32" or os.geteuid() == 0,
                    reason="needs POSIX permission semantics (non-root): a "
                    "read-only dir does not block a write on win32, and root "
                    "writes into a 0o500 dir regardless, so the lock never "
                    "fails and the second WARNING never fires",
                ),
            ),
        ],
    )
    def test_a_tick_with_work_warns_only_from_clear_wirings_own_two_sites(
        self, removable, tmp_path, monkeypatch, caplog
    ):
        """The other direction, on the ordinary shape when `heal` has work.

        The unwired tick above cannot see this: it never reaches
        `clear_wiring`. Harden its fixture with a wiring — a natural
        improvement — and the level-only assertion went red on this branch's own
        intended WARNING, which reads as "the WARNING is wrong" and invites
        the all-DEBUG regression back.

        BOTH REMOVABILITIES, because the count is not the same on the two and
        the version of this test that ran only the removable one asserted
        something FALSE about the other. `clear_wiring` has two WARNING sites
        now — the getter at the top and the lock at the bottom — and on an
        unremovable wiring they BOTH fire on the same tick. One
        tick, no HOME:

            REMOVABLE     [('clear_wiring', 'WARNING')]
            UNREMOVABLE   [('clear_wiring', 'WARNING'), ('clear_wiring', 'WARNING')]

        so the old `== [("clear_wiring", WARNING)]` equality was False on the
        second — passing only because the fixture made the removal succeed,
        which is the shape where the stranding cannot happen.
        """
        import logging

        records = self._heal(
            tmp_path, monkeypatch, caplog, wired=True, removable=removable
        )

        # EVERY record at INFO+, not just the pin's own call sites: churn is
        # origin-agnostic, and any INFO+ line on `heal`'s path is paid 43200
        # times a day. So one assertion covers both halves — `clear_wiring`
        # warns exactly once, and nothing else on the tick does.
        #
        # KEYED ON ORIGIN so it cannot blame the wrong code for the extra.
        # `claude_locks` logs to this same logger and `proper_lockfile`'s
        # release WARNING lands in this same window (`clear_wiring` takes that
        # lock). With the release `rmdir` forced to fail: the
        # level-only assertion reported it as "an unresolvable getter logged
        # at or above INFO", while this one reports
        # `[('clear_wiring', 30), ('proper_lockfile', 30)]` and names it.
        #
        # THE BREADTH IS THE POINT, AND IT CANNOT BE NARROWED BY NAME. This
        # reads `caplog.records`, which is root-scoped, so ANY logger's INFO+
        # record inside the window breaks the equality — deliberately: a tick
        # costs 43200 lines a day whoever wrote them. Filtering to
        # `r.name == "claude-swap"` would not even remove the one reachable
        # intruder anyone worries about: `claude_locks` calls
        # `logging.getLogger("claude-swap")` too, so
        # `proper_lockfile`'s release WARNING carries that same record name
        # and survives the filter. Origin is the only axis that separates
        # them, which is why this asserts on `funcName`.
        #
        # AND `funcName` ALONE IS NO LONGER ENOUGH. Both of `clear_wiring`'s
        # WARNINGs are lexically inside it, so both report
        # `funcName == "clear_wiring"` and an equality on that axis cannot
        # tell the getter site from the lock site — it can only count them.
        # The MESSAGE is what separates them, and it is the same text the user
        # reads in the log, so a format change that made the two
        # indistinguishable there fails here too. `lineno` WOULD also separate
        # them — and it separates
        # the getter sites too: `stacklevel=2` moves `lineno` to the caller
        # just as it moves `funcName`, giving three distinct values. It is
        # rejected for being BRITTLE, not blind — see the class-level comment
        # above, where editing `pin.py`'s COMMENTS alone renumbered two of
        # them. The message axis has an independent merit besides: it is the
        # text the user actually reads, so it catches a site-swap that leaves
        # the counting key identical.
        #
        # The ORDER the equality pins is deterministic: the getter record is
        # emitted from the `paths` loop, the lock record from the loop below
        # it, so on the unremovable shape it is always resolve-then-unwire.
        loud = [
            (
                r.funcName,
                "resolve"
                if "could not be resolved" in r.getMessage()
                else "unwire"
                if "could not be unwired" in r.getMessage()
                else "?",
                r.levelno,
            )
            for r in records
            if r.levelno >= logging.INFO
        ]
        # The getter WARNING fires on both shapes (this fixture's `Path.home()`
        # raises either way). The lock WARNING is what the UNREMOVABLE shape
        # adds — and it is the one that names why the wiring is still there.
        expected = [("clear_wiring", "resolve", logging.WARNING)]
        if not removable:
            expected.append(("clear_wiring", "unwire", logging.WARNING))
        assert loud == expected, (
            "a tick with work to do must warn only from `clear_wiring`'s own "
            "two sites, gated behind `_wiring_is_stale` — the getter one for "
            "a config that could not be LOCATED, and (when the removal fails) "
            "the lock one, which is what names WHY the wiring could not be "
            f"removed. Expected: {expected}. Got: {loud}"
        )


class TestTheLoggerNameStaysPinnedToClaudeSwap:
    """`getLogger("claude-swap")` -> `getLogger(__name__)` (i.e.
    `getLogger("claude_swap.pin")`) survives the whole suite silently: every
    test asserting on log records filters with
    ``caplog.at_level(..., logger="claude-swap")``, so a module-named logger
    that falls through to `logging.lastResort` (stderr) instead of the
    configured "claude-swap" logger (`logging_config.setup_logging`) produces
    NO test failure — the record simply never reaches the handler the
    configured logger owns, and every caplog-based assertion silently sees
    nothing from this module rather than failing loudly.

    Pin the logger's OWN name, not just its behavior through some other
    test's assertions.
    """

    def test_logger_name_is_the_shared_claude_swap_name(self):
        from claude_swap import pin

        assert pin._logger.name == "claude-swap"

    def test_a_warning_from_this_module_carries_that_record_name(self, caplog):
        """Not just the logger object's `.name` attribute — the actual
        LogRecord `.name` a handler receives, which is what a real filter
        or handler routing decision keys on."""
        import logging

        from claude_swap import pin

        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            pin._logger.warning("probe")

        assert any(r.name == "claude-swap" for r in caplog.records), (
            f"no record named 'claude-swap': {[r.name for r in caplog.records]}"
        )


class TestClearWiringDoesNotLockTheSameFileTwice:
    """With `CLAUDE_CONFIG_DIR` unset, `get_global_config_path` and
    `get_default_global_config_path` resolve to the SAME file (both fall
    back to `Path.home() / ".claude.json"`). Dropping `if path not in
    paths:` (keeping the bare `paths.append(path)`) survives the whole
    suite: `clear_wiring` then locks and clears that one file TWICE in the
    same call, and — because the fair-share arithmetic divides the budget by
    `len(paths)` — the launch path silently halves its own sub-second budget
    against a single file for no reason, exactly the "locked and cleared
    twice".
    """

    def test_one_config_is_locked_only_once(self, tmp_path, monkeypatch):
        from contextlib import contextmanager

        import claude_swap.paths as paths
        from claude_swap import claude_locks, pin
        from claude_swap.switcher import ClaudeAccountSwitcher

        cfg = tmp_path / ".claude.json"
        cfg.write_text(
            json.dumps(
                {
                    "env": {"CSWAP_PIN_PORT": "1"},
                    "_cswapPinWiredKeys": ["CSWAP_PIN_PORT"],
                }
            )
        )
        # BOTH getters resolve to the SAME path — the no-CLAUDE_CONFIG_DIR
        # shape the dedup exists for.
        monkeypatch.setattr(paths, "get_global_config_path", lambda: cfg)
        monkeypatch.setattr(paths, "get_default_global_config_path", lambda: cfg)

        real_proper_lockfile = claude_locks.proper_lockfile
        attempted = []

        @contextmanager
        def _recording_lockfile(lock_dir, **kwargs):
            attempted.append(lock_dir)
            with real_proper_lockfile(lock_dir, **kwargs):
                yield

        monkeypatch.setattr(claude_locks, "proper_lockfile", _recording_lockfile)

        changed = pin.clear_wiring(ClaudeAccountSwitcher(), timeout=2.0)

        assert changed is True
        lock_dir = cfg.parent / (cfg.name + ".lock")
        assert attempted.count(lock_dir) == 1, (
            f"the SAME config file was locked {attempted.count(lock_dir)} "
            "times in one call — the two getters resolving to one path must "
            "collapse to one attempt, not one per getter"
        )


class TestTheWarningDoesNotFireOnEveryTick:
    """A once-per-PROCESS cap cannot help when every tick is a new process.

    `cswap pin --heal` is spawned fresh by the statusline hook (`pin-ensure`
    runs `timeout 10 cswap pin --heal` on a ~2s cadence). `pin.heal` has one
    caller — `pin.run(..., heal_only=True)` from `cli.py` — and no long-lived
    daemon. So the module-level cap's lifetime IS one tick, and it can never
    suppress a line across ticks.

    through the real CLI with an unreadable `~/.claude` (a reachable
    `PermissionError` from `get_default_global_config_path`), counting lines
    in the actual rotating log:

        tick   before this fix   after adding it to all three call sites
        1      0                 1
        2      0                 2
        6      0                 6

    The direction is inverted from what the fix claimed. Before, the warning
    was reachable only from `clear_wiring`, which `heal` calls only when
    `_wiring_is_stale` — false once the wiring is gone, so it logged once and
    never again. Adding it to `_wiring_present` and `_wired_ports`, which run
    on EVERY tick unconditionally, turned "1 line ever" into "1 line per
    tick": ~4.2MB/day at 98 bytes, overwriting the whole 4MB rotating history
    every ~22.7h. That is the damage the comment claimed to prevent, newly
    created by the code that claimed it.

    Observability on those two getters is still worth having — they were
    swallowing the raise silently. It belongs at DEBUG, which the rotating
    handler does not record by default, so the record exists for anyone who
    turns it up and costs nothing per tick.
    """

    def test_repeated_heal_ticks_do_not_each_write_a_warning(
        self, tmp_path, monkeypatch, caplog
    ):
        import logging
        import types

        from claude_swap import pin

        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        monkeypatch.delenv("HOME", raising=False)
        monkeypatch.setattr(
            pathlib.Path,
            "home",
            staticmethod(lambda: (_ for _ in ()).throw(RuntimeError("no HOME"))),
        )
        monkeypatch.setattr(pin, "_live_impl", lambda: None)
        sw = types.SimpleNamespace(
            backup_dir=tmp_path,
            _write_json=lambda p, d: p.write_text(json.dumps(d), encoding="utf-8"),
        )
        # Three ticks. With no per-process state left, repeating the call
        # IS the fresh-process simulation — which is the point: nothing can
        # accumulate that a new process would reset.
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            for _ in range(3):
                pin.heal(sw)

        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert not warnings, (
            "a per-tick WARNING survives across processes — at a ~2s cadence "
            "this overwrites the whole rotating log history daily: "
            f"{[r.getMessage() for r in warnings]}"
        )

    def test_the_record_still_exists_at_debug(self, tmp_path, monkeypatch, caplog):
        """Silence was the original defect. The getter's failure must still be
        recoverable by anyone who turns the level up."""
        import logging
        import types

        from claude_swap import pin

        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        monkeypatch.delenv("HOME", raising=False)
        monkeypatch.setattr(
            pathlib.Path,
            "home",
            staticmethod(lambda: (_ for _ in ()).throw(RuntimeError("no HOME"))),
        )
        monkeypatch.setattr(pin, "_live_impl", lambda: None)
        sw = types.SimpleNamespace(
            backup_dir=tmp_path,
            _write_json=lambda p, d: p.write_text(json.dumps(d), encoding="utf-8"),
        )
        with caplog.at_level(logging.DEBUG, logger="claude-swap"):
            pin.heal(sw)

        assert any(
            "get_default_global_config_path" in r.getMessage()
            for r in caplog.records
        ), "the unresolvable getter left no record at any level"


class TestTheSelfLimitingCallSiteStillWarns:
    """`clear_wiring` is the ONE call site where WARNING was always correct.

    `heal` reaches it only through `_wiring_is_stale`, which goes false ONCE
    THE REMOVAL SUCCEEDS — so it logs once and goes quiet. The two getters
    `heal` calls UNCONDITIONALLY (`_wiring_present`, `_wired_ports`) are what
    produced per-tick churn when I put WARNING on them: 12 lines over 6 ticks
    through the real CLI.

    Dropping everything to DEBUG fixed the churn and threw this away with it.
    A user whose `~/.claude` becomes unreadable then gets "could not be
    removed — re-run `cswap pin --heal`" every tick forever, with NOTHING in
    the log naming the cause. Before the regression, one WARNING named the
    getter and the errno.

    THE SELF-LIMITATION IS CONDITIONAL, and it is the condition the WARNING
    exists to explain. Through the real CLI, 10 ticks each: nothing
    wired 0 lines; stale and removed on tick 1, 1 line; stale and UNREMOVABLE
    (read-only config dir) 10 lines and still wired — 6.06 MiB/day at 147 B a
    line, the `PermissionError` shape, since a read-only dir with no lock dir
    fails at `mkdir` (`pin.py` carries both rows and the derivation). Kept at
    WARNING anyway: that is a genuinely broken machine, and telling the user
    nothing is the worse failure. `clear_wiring`'s own call site carries the
    arithmetic.
    """

    def test_clear_wiring_warns_when_it_cannot_resolve_a_config(
        self, tmp_path, monkeypatch, caplog
    ):
        import logging
        import types

        from claude_swap import pin

        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        monkeypatch.delenv("HOME", raising=False)
        monkeypatch.setattr(
            pathlib.Path,
            "home",
            staticmethod(lambda: (_ for _ in ()).throw(RuntimeError("no HOME"))),
        )
        sw = types.SimpleNamespace(
            backup_dir=tmp_path,
            _write_json=lambda p, d: p.write_text(json.dumps(d), encoding="utf-8"),
        )
        with caplog.at_level(logging.DEBUG, logger="claude-swap"):
            pin.clear_wiring(sw, timeout=0.1)

        warned = [
            r
            for r in caplog.records
            if r.levelno >= logging.WARNING
            and "get_default_global_config_path" in r.getMessage()
        ]
        assert warned, (
            "clear_wiring logged below WARNING — this call site is gated "
            "behind _wiring_is_stale so it cannot churn, and it is the only "
            "record that a config was never even LOCATED (the lock WARNING "
            "below it covers the different case of one that was located and "
            "could not be unwired)"
        )


class TestTheLockFailureThatStrandsTheWiringIsNamed:
    """`heal` says "could not be removed" and nothing anywhere says WHICH
    config or WHY.

    It used to say "(the config is locked)", which was worse than vague: on
    the `0o500` shape below the cause is `PermissionError`, and the advice
    that followed could never come true. That claim is gone; the message now
    names the condition and points at `--debug`. What this class guards is
    the other half — that the WARNING carrying the real exception exists at
    all, since the console handler only appears under that flag.

    The unresolvable-getter WARNING above is NOT this record. That one needs
    `Path.home()` to raise, and on the shape it fires it names
    `get_default_global_config_path` — a DIFFERENT config from the stuck one,
    which resolves fine through `get_global_config_path`. On the
    flagship shape the comments cite (read-only config dir, HOME perfectly
    resolvable): five `heal` ticks, the "could not be removed" message every
    time, ZERO log records at any level, and the swallowed cause was
    `PermissionError: [Errno 13] ... '<session>/.claude.json.lock'`.

    So the case the WARNING was kept for was the one case the WARNING could
    not reach. `clear_wiring`'s `except Exception: continue` around the lock
    is where that fact dies.

    KEYED ON ORIGIN, as the round-16 guards are — but NOT because `funcName`
    is the only axis that can tell the call sites apart, which is what this
    said for four rounds. `stacklevel=2` moves `lineno` to the caller exactly
    as it moves `funcName`, so the sites differ on that axis too (
    the records come out at three distinct linenos, not one). `funcName` is
    chosen because a lineno key is BRITTLE — see the comment at
    `_PER_TICK_SITES`, where editing `pin.py`'s COMMENTS alone renumbered two
    of them. A new record on `heal`'s path that is not attributable the same
    way cannot be distinguished from a per-tick regression.

    THE FIRST TEST BELOW NEEDS NO PERMISSION BITS, and that is why it is
    first. The other two in this class, and the `removable=False` case in
    `TestEveryCallSiteRecordsAnUnresolvableGetter`, all reach the lock failure
    through a `0o500` directory — a POSIX non-root mechanism, so all three
    carry the same skipif and all three vanish TOGETHER. With the
    three conditions forced true (the root-container shape `lambda-docker`
    runs, and the win32 runner):

        control:           130 passed, 5 skipped
        lock WARNING gone: 130 passed, 5 skipped     <-- SURVIVED

    Deleting the record this guard exists for stays green. CI's `ubuntu-latest` is
    non-root so CI does cover it, but a green suite that says nothing about
    the flagship guard is exactly the "passes for the wrong reason" this branch
    has spent seven rounds on, and the skip made it silent rather than loud.
    """

    def test_a_lock_already_held_names_the_config_without_any_permission_bits(
        self, tmp_path, monkeypatch, caplog
    ):
        """The lock WARNING, reached with no `chmod` anywhere.

        A lock directory that ALREADY EXISTS with a FRESH mtime is refused by
        `os.mkdir` with `FileExistsError` for every uid including root and on
        win32, because directory-creation atomicity is the mutex — not a
        permission bit. Its mtime is inside `CONFIG_STALENESS_S`, so
        `proper_lockfile`'s stale-takeover branch (its `> staleness` test)
        never runs and cannot `rmdir` it; the budget expires and it raises
        `ClaudeCodeLockTimeout`. That is a real production shape, not a
        contrivance: it is a live Claude Code holding its own config lock
        through a credential refresh.

        NOT MONKEYPATCHED. `proper_lockfile` could be made to raise directly,
        but a test that patches the thing it claims to observe would pass over
        a `clear_wiring` that no longer calls it at all. This drives the real
        lock through the real `heal`.

        As a real euid 0 (via `unshare -r`), both mechanisms on the
        same tree:

            0o500 parent (the other tests):  heal -> "Removed a cloud pin
                                             wiring…", WARNING never fires
            lock dir exists, fresh mtime:    heal -> "could not be removed…",
                                             WARNING fires

        so this one survives precisely the runner the others skip on.
        """
        import logging
        import types

        from claude_swap import pin

        cfg = _cfg(tmp_path, "session", _dead_port())
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg.parent))
        monkeypatch.setattr(pin, "_live_impl", lambda: None)
        # No `_write_json`: the lock fails before anything is written, so the
        # sibling tests' copy of it is dead on this path (never
        # called). A stub that is never invoked describes a write this shape
        # does not do.
        sw = types.SimpleNamespace(backup_dir=tmp_path)

        # Fresh mtime by construction (just created), so the takeover path is
        # NOT the one exercised — the mtime is the only thing keeping this
        # from being the orphan case the test below covers.
        lock = cfg.parent / (cfg.name + ".lock")
        os.mkdir(lock)

        # BUDGET PINNED, because the default sits 1.0s from a cliff.
        #
        # `clear_wiring` gives each path `left / (paths remaining)`, so how
        # long THIS lock is waited on depends on how many configs are being
        # cleared: two -> 9.0/2 = 4.5s, one -> the whole 9.0s. `proper_lockfile`
        # takes a lock over once its mtime is older than CONFIG_STALENESS_S
        # (10.0s) and RE-CHECKS that on every loop, so the wait itself ages the
        # dir toward the threshold. Measured here:
        #
        #     budget  lock age at start   outcome
        #       4.5s        0.0s          refused after 4.5s
        #       4.5s        1.5s          refused after 4.5s
        #       9.0s        0.0s          refused after 9.0s
        #       9.0s        1.5s          TAKEN OVER after 8.8s   <- gate lost
        #       1.0s        0.0s          refused after 1.0s
        #
        # The takeover is correct behaviour — a lock nothing has touched for
        # 10s IS stale — but it is not the shape this test names, and with the
        # default budget the margin is however long setup took. It reached
        # zero on Windows CI the moment `heal` began clearing one config
        # instead of two. `lock_timeout` is the documented per-caller budget
        # (the launch path already passes it), not a patch of the mechanism
        # under test: the real lock, the real `proper_lockfile` and the real
        # `heal` are all still in the loop.
        with caplog.at_level(logging.DEBUG, logger="claude-swap"):
            changed, message = pin.heal(sw, lock_timeout=1.0)

        # THE LOCK DIR SURVIVING IS THE GATE, not the message. `heal` returns
        # this same `(False, "…could not be removed…")` for ANY raise inside
        # `clear_wiring`'s per-path `try`, so the message alone does not say
        # the lock was the thing that failed. Deleting the
        # `os.mkdir` above: `1 failed, 133 passed` — the message assertion
        # still passed, because `sw` has no `_write_json` and
        # `_clear_wiring_locked` raised `AttributeError` INSIDE the lock and
        # landed in `clear_wiring`'s per-path `except Exception`, with
        # `proper_lockfile`
        # never once refused. `proper_lockfile` `rmdir`s in a `finally`
        # (its `os.rmdir(lock_dir)`), so the dir is gone whenever it acquired —
        # it is still here only when it never could, which is this shape.
        assert lock.is_dir() and not changed and "could not be removed" in message, (
            "fixture did not reach the stranded shape: "
            f"{(lock.is_dir(), changed, message)}"
        )

        named = [
            r
            for r in caplog.records
            if r.funcName == "clear_wiring" and r.levelno >= logging.WARNING
        ]
        # Same two facts the POSIX tests pin — WHICH config and WHY — and
        # "Could not acquire" is written at exactly one place in the codebase
        # (`claude_locks.proper_lockfile`'s raise), so it proves the shape
        # arrived as `ClaudeCodeLockTimeout` rather than as something else.
        assert any(
            str(cfg) in r.getMessage() and "Could not acquire" in r.getMessage()
            for r in named
        ), (
            "heal told the user the wiring could not be removed and nothing "
            "logged WHICH config or WHY — and this is the ONE test of that "
            "record that a root or win32 runner still runs. Records: "
            f"{[(r.funcName, r.levelname, r.getMessage()) for r in caplog.records]}"
        )

    @pytest.mark.skipif(
        sys.platform == "win32" or os.geteuid() == 0,
        reason="needs POSIX permission semantics (non-root): root writes into "
        "a 0o500 dir regardless, so the lock never fails",
    )
    def test_a_lock_that_cannot_be_taken_names_the_config_and_the_errno(
        self, tmp_path, monkeypatch, caplog
    ):
        import logging
        import types

        from claude_swap import pin

        # A read-only DIRECTORY, not a read-only file: `proper_lockfile` takes
        # the lock with `os.mkdir`, and the config itself must stay readable
        # so the run gets all the way to the lock rather than failing earlier.
        #
        # HOME points somewhere harmless rather than being made to raise —
        # that is the whole point of this shape — but it must still be
        # redirected: `get_default_global_config_path` resolves through it,
        # and the real `~/.claude.json` is not this test's to lock.
        cfg = _cfg(tmp_path, "session", _dead_port())
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg.parent))
        monkeypatch.setattr(pin, "_live_impl", lambda: None)
        sw = types.SimpleNamespace(
            backup_dir=tmp_path,
            _write_json=lambda p, d: p.write_text(json.dumps(d), encoding="utf-8"),
        )
        cfg.parent.chmod(0o500)
        try:
            with caplog.at_level(logging.DEBUG, logger="claude-swap"):
                changed, message = pin.heal(sw)
        finally:
            cfg.parent.chmod(0o700)  # or tmp_path cleanup cannot remove it

        # The user-visible half of the defect, so the test fails loudly if the
        # fixture ever stops reaching the stranded state it is about.
        assert not changed and "could not be removed" in message, (
            f"fixture did not reach the stranded shape: {(changed, message)}"
        )
        # AND IT MUST NOT NAME A CAUSE IT CANNOT KNOW. `clear_wiring` catches
        # every exception around the lock, so this one message covered a held
        # lock AND an unwritable config directory — and said "the config is
        # locked" for both. Here the cause is `PermissionError: [Errno 13]`,
        # asserted below, and the advice that follows ("re-run `cswap pin
        # --heal`") can never come true: re-running does not chmod anything,
        # so the user waits on a lock that was never held.
        #
        # The true cause reaches only the log FILE — `logging_config` adds a
        # console handler under `--debug` alone — so this message is the whole
        # of what an ordinary run shows. Naming one cause for a condition with
        # two is the same defect as naming none.
        assert "is locked" not in message, (
            f"heal blamed a lock for a permission failure: {message!r}"
        )
        assert "--debug" in message, (
            f"the message does not say where the real cause is: {message!r}"
        )

        named = [
            r
            for r in caplog.records
            if r.funcName == "clear_wiring" and r.levelno >= logging.WARNING
        ]
        # THE CONFIG MUST BE NAMED INDEPENDENTLY OF THE ERRNO. `PermissionError`
        # renders the LOCK path, which contains the config path as a prefix, so
        # the naive `str(cfg) in message` passes on the exception text alone:
        # dropping `path` from the log call entirely still satisfied
        # it. Removing the lock path first makes the two facts separable, and
        # does it without pinning which order the message puts them in.
        assert any(
            str(cfg) in r.getMessage().replace(f"{cfg}.lock", "")
            and "Permission denied" in r.getMessage()
            for r in named
        ), (
            "heal told the user the wiring could not be removed and nothing "
            "logged WHICH config or WHY — the exact silence the "
            "unresolvable-getter WARNING is kept for, on the one shape that "
            f"WARNING cannot reach. Records: "
            f"{[(r.funcName, r.levelname, r.getMessage()) for r in caplog.records]}"
        )

    @pytest.mark.skipif(
        sys.platform == "win32" or os.geteuid() == 0,
        reason="needs POSIX permission semantics (non-root): root removes the "
        "stale lock dir regardless, so the takeover succeeds",
    )
    def test_a_permanently_orphaned_lock_is_named_at_warning_too(
        self, tmp_path, monkeypatch, caplog
    ):
        """The OTHER exception this `except` catches, and the worse one.

        Routing `ClaudeCodeLockTimeout` to DEBUG is an appealing split —
        transient contention (two `cswap run`s racing) is a Tuesday and logs
        at the same level as a permanently unwritable config. It is wrong,
        and this test is what makes it fail rather than merely look tidy: the
        two are the SAME TYPE here. A live competitor raises
        `ClaudeCodeLockTimeout`, and so does the shape below — an ORPHANED
        lock dir (a holder killed -9) inside a config dir this process cannot
        write. `proper_lockfile`'s stale-takeover path `rmdir`s the dead
        holder's dir, and that `rmdir` needs write permission on the parent it
        will never get, so this machine is stuck forever. 10 ticks:
        10 lines, still wired, every tick identical.

        So the split silences precisely the machine the WARNING exists for,
        and keeps only `PermissionError` — the kind that least needs it, since
        it names its own errno.

        A DISCRIMINATOR OTHER THAN THE TYPE DOES EXIST — the lock dir's mtime
        age, which `proper_lockfile` already stats (`os.stat(lock_dir)`) —
        so the refusal rests on COST, not on "nothing can tell them apart".
        Counted at BOTH cadences, because one figure cannot serve both (a
        tight loop of 10 `heal()` calls priced the transient case while the
        permanent one was priced at the real statusline cadence):

            tight loop (no sleep)   11 unwire lines, cleared on tick 12
            real ~2s statusline      2 unwire lines, cleared on tick 3

        Two lines once, against 43200/day forever. `clear_wiring`'s own call
        site carries the full arithmetic.
        """
        import logging
        import time
        import types

        from claude_swap import pin

        cfg = _cfg(tmp_path, "session", _dead_port())
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg.parent))
        monkeypatch.setattr(pin, "_live_impl", lambda: None)
        sw = types.SimpleNamespace(
            backup_dir=tmp_path,
            _write_json=lambda p, d: p.write_text(json.dumps(d), encoding="utf-8"),
        )

        # The orphan: a lock dir with nobody touching it, aged past
        # `CONFIG_STALENESS_S` so the takeover path is the one exercised —
        # and then made un-removable by the read-only parent.
        lock = cfg.parent / (cfg.name + ".lock")
        os.mkdir(lock)
        os.utime(lock, (time.time() - 3600, time.time() - 3600))
        cfg.parent.chmod(0o500)
        try:
            with caplog.at_level(logging.DEBUG, logger="claude-swap"):
                changed, message = pin.heal(sw)
        finally:
            cfg.parent.chmod(0o700)  # or tmp_path cleanup cannot remove it

        assert not changed and "could not be removed" in message, (
            f"fixture did not reach the stranded shape: {(changed, message)}"
        )

        named = [
            r
            for r in caplog.records
            if r.funcName == "clear_wiring" and r.levelno >= logging.WARNING
        ]
        # "Could not acquire" IS THE TYPE ASSERTION. That sentence is written
        # at exactly one place in the codebase — the `raise
        # ClaudeCodeLockTimeout` in `claude_locks.proper_lockfile` — so
        # matching it proves this shape reaches the `except` as that type and
        # not as the `PermissionError` the test above covers. Without it this
        # test would silently duplicate that one, and the split it rules out
        # would be reachable again with CI green.
        assert any(
            str(cfg) in r.getMessage() and "Could not acquire" in r.getMessage()
            for r in named
        ), (
            "a permanently stuck wiring logged below WARNING — this is the "
            "machine the record exists for, and `ClaudeCodeLockTimeout` is "
            "the type it raises. Routing that type to DEBUG silences the "
            "stuck case and keeps only the one that names its own errno. "
            f"Records: "
            f"{[(r.funcName, r.levelname, r.getMessage()) for r in caplog.records]}"
        )
