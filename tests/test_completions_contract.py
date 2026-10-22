"""Fish completion contract tests.

`completions/cswap.fish` completes account targets by reading `sequence.json`
directly instead of shelling out to `cswap list --json`. That keeps Tab off the
network — `list` refetches usage over HTTPS once an entry passes
`SERVE_TTL_S` — but it couples the completion to the on-disk shape of the file
rather than to the versioned `--json` payload. These tests are that coupling's
guard rail.

Two layers:

1. **Contract** (every platform): the fields the completion reads —
   `activeAccountNumber`, and each account's `email` and `alias` — are the
   fields the switcher actually writes, in a pretty-printed layout that a
   line-oriented parser can read. Renaming or inlining any of them fails here
   rather than silently emptying someone's completions.

2. **Parser** (POSIX only): the awk program is extracted from the shipped
   `.fish` file and run against a real `sequence.json`, so the test exercises
   the code that ships rather than a copy of it. Skipped on Windows, which has
   no awk and no fish.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from claude_swap.switcher import ClaudeAccountSwitcher

COMPLETION_FILE = Path(__file__).resolve().parent.parent / "completions" / "cswap.fish"

requires_awk = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("awk") is None,
    reason="needs a POSIX awk; the fish completion does not target Windows",
)


def _awk_program() -> str:
    """The awk source embedded in the completion, so tests track the real thing."""
    text = COMPLETION_FILE.read_text(encoding="utf-8")
    start = text.index("awk '") + len("awk '")
    end = text.index("' $file", start)
    return text[start:end]


def _write_sequence(data: dict) -> Path:
    """Persist ``data`` through the switcher's own writer and return the path."""
    switcher = ClaudeAccountSwitcher()
    switcher._setup_directories()
    switcher._write_json(switcher.sequence_file, data)
    return switcher.sequence_file


def _candidates(path: Path) -> dict[str, str]:
    """Run the shipped awk program, returning {completion: description}."""
    result = subprocess.run(
        ["awk", _awk_program(), str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    pairs = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        value, _, description = line.partition("\t")
        pairs[value] = description
    return pairs


class TestSequenceJsonContract:
    """Layer 1: what the completion reads is what the switcher writes."""

    def test_fields_the_completion_reads_are_present(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        sample_sequence_data["accounts"]["1"]["alias"] = "work"
        path = _write_sequence(sample_sequence_data)

        data = json.loads(path.read_text(encoding="utf-8"))

        assert "activeAccountNumber" in data
        assert data["accounts"]["1"]["email"] == "account1@example.com"
        assert data["accounts"]["1"]["alias"] == "work"

    def test_alias_survives_a_real_set_alias_call(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        """`cswap alias` must land in the key the completion looks for."""
        _write_sequence(sample_sequence_data)
        switcher = ClaudeAccountSwitcher()
        switcher.set_alias("2", "personal")

        data = json.loads(switcher.sequence_file.read_text(encoding="utf-8"))

        assert data["accounts"]["2"]["alias"] == "personal"

    def test_layout_stays_line_oriented(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        """The awk parser is line-based; compact JSON would silently break it."""
        path = _write_sequence(sample_sequence_data)
        text = path.read_text(encoding="utf-8")

        assert "\n" in text, "sequence.json must not be written on one line"
        # Each account opens its own block, which is what anchors the parser.
        assert '"1": {' in text


@requires_awk
class TestCompletionParser:
    """Layer 2: the shipped awk program against a real file."""

    def test_offers_number_email_and_alias(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        sample_sequence_data["accounts"]["1"]["alias"] = "work"
        candidates = _candidates(_write_sequence(sample_sequence_data))

        assert "1" in candidates
        assert "account1@example.com" in candidates
        assert "work" in candidates
        assert "2" in candidates
        assert "account2@example.com" in candidates

    def test_marks_only_the_active_account(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        sample_sequence_data["activeAccountNumber"] = 2
        candidates = _candidates(_write_sequence(sample_sequence_data))

        assert "(active)" in candidates["2"]
        assert "(active)" not in candidates["1"]

    def test_account_without_an_alias_still_completes(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        """No alias set is the default state, not an edge case."""
        candidates = _candidates(_write_sequence(sample_sequence_data))

        assert "1" in candidates
        assert candidates["1"].startswith("account1@example.com")

    def test_aliases_do_not_leak_between_accounts(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        """Account 1's alias must not be carried onto account 2 by the parser."""
        sample_sequence_data["accounts"]["1"]["alias"] = "work"
        candidates = _candidates(_write_sequence(sample_sequence_data))

        assert "work" not in candidates["2"]

    @pytest.mark.parametrize(
        "content",
        ["", "{", '{"accounts": {"1": {"email": "truncated', "\x00\x01\x02not json"],
        ids=["empty", "brace", "truncated", "binary"],
    )
    def test_unusable_file_yields_no_candidates(self, tmp_path: Path, content: str):
        """A corrupt file must empty the menu, never break the prompt."""
        path = tmp_path / "sequence.json"
        path.write_text(content, encoding="utf-8", errors="ignore")

        result = subprocess.run(
            ["awk", _awk_program(), str(path)],
            capture_output=True,
            text=True,
        )

        assert result.stdout.strip() == ""
