"""A failed park must not make a foreign `.swapping` leftover read as account A."""
from pathlib import Path

import pytest

from claude_swap.session import is_session_stale, mark_session_stale
from claude_swap.switcher import ClaudeAccountSwitcher

EA, EB = "aa@example.com", "bb@example.com"


def _prep(s):
    # `_swap_session_dirs` reads the session dirs and nothing else -- no roster,
    # no sequence file. Seeding either would advertise a dependency it lacks.
    s._setup_directories()
    a, b = s._session_dir("1", EA), s._session_dir("2", EB)
    for d, tag in ((a, "A-HISTORY"), (b, "B-HISTORY")):
        d.mkdir(parents=True, exist_ok=True)
        (d / "marker").write_text(tag)
    return a, b


def test_a_failed_park_leaves_As_flag_on_As_own_profile(temp_home: Path):
    s = ClaudeAccountSwitcher()
    dir_a, _ = _prep(s)
    mark_session_stale(dir_a)
    assert is_session_stale(dir_a), "premise: A starts stale"

    # A leftover from an interrupted earlier swap. Nothing in the codebase
    # removes one, so a real os.replace raises ENOTEMPTY on the park.
    leftover = dir_a.with_name(dir_a.name + ".swapping")
    leftover.mkdir(parents=True)
    (leftover / "leftover").write_text("FOREIGN-LEFTOVER")

    moved: list[Path] = []
    s._swap_session_dirs("1", EA, "2", EB, moved)

    assert dir_a.exists() and (dir_a / "marker").read_text() == "A-HISTORY", (
        "premise: the park must have FAILED with A still at dir_a"
    )
    assert moved == [], f"premise: nothing should have landed, got {moved}"
    assert is_session_stale(dir_a), (
        "DEFECT: A's stale flag was taken off its own profile and written beside "
        f"an unrelated leftover; leftover_stale={is_session_stale(leftover)}"
    )


def test_control_the_flag_tracks_A_when_the_park_succeeds(temp_home: Path):
    """CONTROL: the same check in a case where it MUST report presence."""
    s = ClaudeAccountSwitcher()
    dir_a, _ = _prep(s)
    mark_session_stale(dir_a)
    moved: list[Path] = []
    s._swap_session_dirs("1", EA, "2", EB, moved)
    assert is_session_stale(s._session_dir("2", EA)), (
        "CONTROL FAILED: the instrument cannot report True"
    )


def test_an_interrupt_after_the_park_lands_still_recovers_A(temp_home: Path):
    """The park's rename and its record must not be two statements.

    A signal between them leaves A under `<slot>-<slug>.swapping` with the
    strand recovery disarmed, and that leftover is exactly what makes the next
    swap's park fail the way the test above describes.
    """
    import os

    s = ClaudeAccountSwitcher()
    dir_a, _ = _prep(s)
    real_replace = os.replace

    def interrupt_once_the_park_has_landed(src, dst):
        out = real_replace(src, dst)
        if str(dst).endswith(".swapping"):
            raise KeyboardInterrupt
        return out

    moved: list[Path] = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "replace", interrupt_once_the_park_has_landed)
        with pytest.raises(KeyboardInterrupt):
            s._swap_session_dirs("1", EA, "2", EB, moved)

    stranded = dir_a.with_name(dir_a.name + ".swapping")
    assert not stranded.exists(), (
        f"DEFECT: A is stranded under the staging name; it holds "
        f"{sorted(p.name for p in stranded.iterdir())}"
    )
    assert dir_a.exists() and (dir_a / "marker").read_text() == "A-HISTORY", (
        "DEFECT: A did not come back to its own name"
    )


def test_a_park_that_raised_never_names_the_leftover_as_A(temp_home: Path):
    """A vanished `dir_a` is not evidence that the rename went through.

    The rename and its record must be one statement for an ASYNC exception,
    and separately must NOT consult the filesystem when the rename itself
    failed: there A is still A's, and reading `dir_a`'s absence as "the park
    landed" names the foreign leftover as A and promotes it into A's slot.
    """
    import errno
    import os
    import shutil

    s = ClaudeAccountSwitcher()
    dir_a, _ = _prep(s)
    mark_session_stale(dir_a)
    leftover = dir_a.with_name(dir_a.name + ".swapping")
    leftover.mkdir(parents=True)
    (leftover / "marker").write_text("FOREIGN-LEFTOVER")

    real_replace = os.replace

    def vanish_then_refuse(src, dst, *a, **k):
        if str(dst).endswith(".swapping"):
            # The window: something outside removes A between the rename and
            # any check of it, and the rename fails on the leftover anyway.
            shutil.rmtree(src)
            raise OSError(errno.ENOTEMPTY, "Directory not empty")
        return real_replace(src, dst, *a, **k)

    moved: list[Path] = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "replace", vanish_then_refuse)
        s._swap_session_dirs("1", EA, "2", EB, moved)

    assert leftover.exists() and (leftover / "marker").read_text() == (
        "FOREIGN-LEFTOVER"
    ), "the leftover was moved, so it was mistaken for A"
    assert not dir_a.exists() or (dir_a / "marker").read_text() != (
        "FOREIGN-LEFTOVER"
    ), "DEFECT: an unrelated leftover was promoted into A's own slot name"


def test_a_landed_park_is_recorded_even_if_As_old_name_reappears(
    temp_home: Path,
):
    """The park landed; whether the old name is occupied afterwards is a
    different question, and answering it with `dir_a` skips A's second leg.
    """
    import os

    s = ClaudeAccountSwitcher()
    dir_a, _ = _prep(s)
    real_replace = os.replace

    def land_then_reappear(src, dst, *a, **k):
        out = real_replace(src, dst, *a, **k)
        if str(dst).endswith(".swapping"):
            # Something outside recreates the name the park just freed.
            dir_a.mkdir(parents=True, exist_ok=True)
            (dir_a / "marker").write_text("RECREATED")
        return out

    moved: list[Path] = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "replace", land_then_reappear)
        s._swap_session_dirs("1", EA, "2", EB, moved)

    stranded = dir_a.with_name(dir_a.name + ".swapping")
    assert not stranded.exists(), (
        "DEFECT: the park landed and was not recorded, so A never took its "
        "second leg and is stranded under the staging name"
    )
    assert (s._session_dir("2", EA) / "marker").read_text() == "A-HISTORY"
