"""Unit tests for ``tests/conftest.py``'s ``pytest_sessionfinish`` basetemp
self-delete -- see that hook's comment for the measured reason."""
from __future__ import annotations

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


def test_nothing_made_is_a_no_op(tmp_path):
    session = _make_session(None, None, is_worker=False)

    conftest.pytest_sessionfinish(session, exitstatus=0)  # must not raise
