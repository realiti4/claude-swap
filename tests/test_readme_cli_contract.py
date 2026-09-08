"""The README must not document a `cswap` verb the CLI does not accept.

A documented verb the parser rejects is a promise the package does not keep:
the reader copies the line, runs it, and gets ``unrecognized arguments``.

The probe appends an unknown flag so the parser always exits during argument
parsing — no command ever runs — and then reads which tokens argparse named as
unrecognized. A verb the CLI knows is consumed before that list is built; an
unknown verb survives into it.
"""

import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import cli

README = Path(__file__).resolve().parents[1] / "README.md"
PROBE = "--zzz-not-a-real-flag"


def _readme_verbs() -> list[str]:
    """Every ``cswap <verb>`` shown in a fenced code block in the README."""
    blocks = re.findall(
        r"```(?:bash|console|sh)?\n(.*?)```",
        README.read_text(encoding="utf-8"),
        re.S,
    )
    return sorted(
        {
            match.group(1)
            for block in blocks
            for line in block.splitlines()
            if (match := re.match(r"cswap\s+([a-z][a-z0-9-]*)", line.strip()))
        }
    )


VERBS = _readme_verbs()


def _unrecognized(capsys, verb: str) -> set[str]:
    """The tokens argparse reports as unrecognized for ``cswap <verb> PROBE``."""
    with patch.object(sys, "argv", ["cswap", verb, PROBE]):
        with pytest.raises(SystemExit):
            cli.main()
    err = capsys.readouterr().err
    # Only the token list matters. Substring matching on the whole message
    # would call `import` unknown, because its arity error names `--import`.
    match = re.search(r"unrecognized arguments: (.*)", err)
    return set(match.group(1).split()) if match else set()


def test_the_readme_documents_commands_at_all():
    """Without this, an extraction that returned nothing would pass vacuously."""
    assert {"list", "status", "switch"} <= set(VERBS)


def test_the_probe_can_fail(capsys):
    """The other polarity: an unknown verb must reach the unrecognized list."""
    assert "definitelynotacommand" in _unrecognized(capsys, "definitelynotacommand")


@pytest.mark.parametrize("verb", VERBS)
def test_readme_verb_is_a_real_command(verb, capsys):
    assert verb not in _unrecognized(capsys, verb), (
        f"README documents `cswap {verb}`, which this build's CLI rejects"
    )
