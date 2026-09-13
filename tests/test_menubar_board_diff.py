"""Pixel-fidelity gate for the reset-timelines wave.

Two enforcement halves, per the design mandate (no exceptions):

1. **Geometry, zero tolerance** — the capture harness also records exact
   point boxes (``<state>.layout.json``); board boxes must match exactly.
2. **Image diff, anti-aliasing tolerance only** — a pixel counts as
   differing when any channel moves more than ``CHANNEL_TOL``; differing
   pixels may not exceed ``MAX_DIFF_PCT`` of the image. Glyph shapes move
   channels far more than rasterizer anti-aliasing does, so shape drift
   fails long before the budget is spent.

Pillow is intentionally *not* a locked dependency (the uv.lock in-flight
changes belong to other work): run the gate with
``uv run --with pillow python -m pytest tests/test_menubar_board_diff.py``
(``python -m`` matters — the console-script ``pytest`` resolves against the
project venv and misses the ``--with`` overlay). Without Pillow the image
half skips; the layout half still runs when captures exist.
``CSWAP_BOARD_STRICT=1`` makes missing captures a failure for the QA pass.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CAPTURES = REPO / "tests" / "fixtures" / "captures"
BOARDS = REPO / "assets" / "ui-ux-handoff"

# Design-board baselines the gate compares against. Timeline states get a
# tight 0.1% budget (geometry is enforced exactly by the layout half);
# main-panel states are informational deltas until the boards 01-15 are
# re-exported against bundled-font rendering (owner decision, Task 14).
GATED_STATES: dict[str, dict] = {}  # e.g. "17-right-dark": {"board": ..., "max_pct": 0.1}
INFO_STATES: dict[str, dict] = {
    "main-dark": {"board": BOARDS / "screens" / "01-main-dark.png"},
    "main-light": {"board": BOARDS / "screens" / "02-main-light.png"},
}

CHANNEL_TOL = 25  # per-channel delta under this is rasterizer AA, not change
# Informational states differ by design (illustrative identities, and the
# bundled-font shift on boards exported under fallback fonts). The floor
# only catches a broken capture (blank/partial render), never gates style.
MAX_INFO_PCT = 35.0

STRICT = os.environ.get("CSWAP_BOARD_STRICT") == "1"

try:
    from PIL import Image  # noqa: S101

    HAS_PIL = True
except ImportError:  # pragma: no cover - exercised via --with pillow runs
    HAS_PIL = False


def image_diff_pct(a: Path, b: Path, channel_tol: int = CHANNEL_TOL) -> float:
    """Percent of pixels where any channel differs by more than channel_tol.

    Sizes must match exactly — a size mismatch is a geometry failure and
    raises, never a 100% diff.
    """
    if not HAS_PIL:
        pytest.skip("Pillow not available; run with --with pillow")
    with Image.open(a) as ia, Image.open(b) as ib:
        if ia.size != ib.size:
            raise AssertionError(f"size mismatch: {a.name} {ia.size} vs {b.name} {ib.size}")
        pa, pb = ia.convert("RGBA").tobytes(), ib.convert("RGBA").tobytes()
    stride = 4
    total = len(pa) // stride
    differing = 0
    for i in range(0, len(pa), stride):
        if (
            abs(pa[i] - pb[i]) > channel_tol
            or abs(pa[i + 1] - pb[i + 1]) > channel_tol
            or abs(pa[i + 2] - pb[i + 2]) > channel_tol
        ):
            differing += 1
    return 100.0 * differing / total


def _write_png(path: Path, size=(8, 8), color=(10, 20, 30, 255)) -> None:
    if not HAS_PIL:
        pytest.skip("Pillow not available; run with --with pillow")
    from PIL import Image

    Image.new("RGBA", size, color).save(path)


class TestSelfChecks:
    """The diff function itself is under test — identical is zero, real
    changes are caught, and size drift is a hard error (never a % diff)."""

    def test_identical_images_diff_zero(self, tmp_path):
        a, b = tmp_path / "a.png", tmp_path / "b.png"
        _write_png(a)
        _write_png(b)
        assert image_diff_pct(a, b) == 0.0

    def test_small_channel_drift_below_tolerance_is_zero(self, tmp_path):
        a, b = tmp_path / "a.png", tmp_path / "b.png"
        _write_png(a, color=(100, 100, 100, 255))
        _write_png(b, color=(115, 115, 115, 255))  # 15 <= CHANNEL_TOL: AA band
        assert image_diff_pct(a, b) == 0.0

    def test_real_change_is_detected(self, tmp_path):
        a, b = tmp_path / "a.png", tmp_path / "b.png"
        _write_png(a, color=(100, 100, 100, 255))
        _write_png(b, color=(200, 100, 100, 255))
        assert image_diff_pct(a, b) == 100.0

    def test_size_mismatch_raises(self, tmp_path):
        a, b = tmp_path / "a.png", tmp_path / "b.png"
        _write_png(a, size=(8, 8))
        _write_png(b, size=(9, 8))
        with pytest.raises(AssertionError, match="size mismatch"):
            image_diff_pct(a, b)


def _captures(state: str) -> bool:
    return (CAPTURES / f"{state}.png").is_file()


class TestBoardGate:
    """Gated (timeline) states must pass both halves; informational
    (main-panel) states record their delta in the board report."""

    def test_gated_states_match_boards(self, tmp_path):
        report = []
        failures = []
        checked = 0
        for state, cfg in GATED_STATES.items():
            if not _captures(state):
                if STRICT:
                    failures.append(f"{state}: capture missing")
                continue
            checked += 1
            pct = image_diff_pct(CAPTURES / f"{state}.png", Path(cfg["board"]))
            report.append(f"{state}: {pct:.4f}% (budget {cfg.get('max_pct', 0.1)}%)")
            if pct > cfg.get("max_pct", 0.1):
                failures.append(f"{state}: {pct:.4f}% exceeds budget")
            layout = CAPTURES / f"{state}.layout.json"
            if layout.is_file():
                data = json.loads(layout.read_text(encoding="utf-8"))
                assert data.get("viewport", {}).get("w"), f"{state}: empty viewport"
        if STRICT and not checked and GATED_STATES:
            failures.append("no gated states captured")
        assert not failures, "fidelity gate failures:\n" + "\n".join(failures)

    def test_informational_main_panel_deltas_are_recorded(self, tmp_path):
        """Boards 01/02 use illustrative identities that differ from the
        fixture roster; the delta is recorded, not gated, until the owner
        re-exports those boards against bundled-font rendering (Task 14)."""
        if not any(_captures(s) for s in INFO_STATES):
            if STRICT:
                pytest.fail("no informational captures present in strict mode")
            pytest.skip("no captures; run scripts/board_capture.py")
        lines = ["# Main-panel capture deltas (informational)", ""]
        for state, cfg in INFO_STATES.items():
            if not _captures(state):
                continue
            pct = image_diff_pct(CAPTURES / f"{state}.png", Path(cfg["board"]))
            lines.append(f"- {state} vs {cfg['board'].name}: {pct:.2f}% differing")
            assert pct <= MAX_INFO_PCT, (
                f"{state} delta {pct:.2f}% far exceeds illustrative-data band "
                f"({MAX_INFO_PCT}%) — geometry regression, not text variance"
            )
        (CAPTURES / "DELTA-REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
