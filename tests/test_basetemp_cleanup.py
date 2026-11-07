"""Unit tests for ``tests/conftest.py``'s ``pytest_sessionfinish`` basetemp
self-delete -- see that hook's comment for the measured reason."""
from __future__ import annotations

import subprocess
import types

import pytest

from tests import conftest


def _make_basetemp(tmp_path):
    """A throwaway directory standing in for a session's real basetemp,
    with one nested entry so a no-op deletion can't pass by accident."""
    basetemp = tmp_path / "pytest-7"
    (basetemp / "popen-gw0" / "isolated_home-3").mkdir(parents=True)
    return basetemp


def _make_session(basetemp, given_basetemp, is_worker):
    factory = types.SimpleNamespace(_basetemp=basetemp, _given_basetemp=given_basetemp)
    config = types.SimpleNamespace(_tmp_path_factory=factory)
    if is_worker:
        config.workerinput = {"workerid": "gw0"}
    return types.SimpleNamespace(config=config)


@pytest.fixture(autouse=True)
def _wait_for_the_detached_removal(monkeypatch):
    real = subprocess.Popen

    def popen(*args, **kwargs):
        child = real(*args, **kwargs)
        child.wait()
        return child

    monkeypatch.setattr(subprocess, "Popen", popen)


@pytest.mark.parametrize(
    "exitstatus, is_worker, user_given, expect_deleted",
    [
        (0, True, True, True),  # green worker: cleans its own share regardless
        (1, True, True, False),  # red: kept for debugging
        (0, False, True, False),  # green controller, user's own --basetemp: left alone
        (0, False, False, True),  # green controller, no user basetemp: cleans its own
    ],
    ids=["green-worker", "red-worker", "green-controller-user-given", "green-controller"],
)
def test_pytest_sessionfinish(tmp_path, exitstatus, is_worker, user_given, expect_deleted):
    basetemp = _make_basetemp(tmp_path)
    session = _make_session(basetemp, basetemp if user_given else None, is_worker)

    conftest.pytest_sessionfinish(session, exitstatus=exitstatus)

    assert basetemp.exists() != expect_deleted


def test_the_removal_is_detached_and_never_waited_on(tmp_path, monkeypatch):
    basetemp = _make_basetemp(tmp_path)
    started = []
    # a child with no wait/poll/communicate: any wait by the hook is an AttributeError
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: started.append(kw) or types.SimpleNamespace())

    conftest.pytest_sessionfinish(_make_session(basetemp, None, is_worker=False), exitstatus=0)

    assert basetemp.exists()  # the hook itself removed nothing
    (kw,) = started
    assert kw["start_new_session"] is True
    assert kw["stdin"] == kw["stdout"] == kw["stderr"] == subprocess.DEVNULL  # no inherited pipe to wait on


def test_a_failed_spawn_falls_back_to_removing_in_process(tmp_path, monkeypatch):
    basetemp = _make_basetemp(tmp_path)

    def refused(*a, **kw):
        raise OSError(11, "Resource temporarily unavailable")  # EAGAIN under a process cap

    monkeypatch.setattr(subprocess, "Popen", refused)

    conftest.pytest_sessionfinish(_make_session(basetemp, None, is_worker=False), exitstatus=0)  # must not raise

    assert not basetemp.exists()


def test_nothing_made_is_a_no_op(tmp_path):
    session = _make_session(None, None, is_worker=False)

    conftest.pytest_sessionfinish(session, exitstatus=0)  # must not raise
