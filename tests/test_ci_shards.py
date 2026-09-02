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

import pytest

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
        # A TRAILING COMMENT IS NOT A PATH, and neither is another key. This
        # took every non-`- name:`/`paths:` line as more paths, so
        # `paths: tests/x.py  # the cheap one` made the shard "name" three
        # files that do not exist and the workflow was REFUSED for a comment
        # -- a false refusal on a correct edit, which is the direction this
        # file keeps having to close.
        line = _uncomment(line).strip()
        if not line:
            continue
        if line.startswith("- name:"):
            current = {"name": line.split(":", 1)[1].strip(), "paths": ""}
            shards.append(current)
        elif line.startswith("paths:"):
            rest = line.split(":", 1)[1].strip()
            current["paths"] = "" if rest in (">-", ">", "|", "|-", ">+", "|+") else rest
        elif re.match(r"-?\s*[A-Za-z_][\w-]*:(\s|$)", line):
            continue  # another key of this shard, not a path
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



def _workflow_jobs(workflow: Path) -> dict[str, str]:
    """Every job in the workflow, by name -- ENUMERATED, not listed.

    A hardcoded pair is right until someone adds a job, and the job this file
    forgot was the ubuntu one that runs the WHOLE suite: made
    `continue-on-error`, stripped of its pytest call, or pointed at a file
    that does not exist, the suite stayed green through all three.
    """
    text = workflow.read_text(encoding="utf-8")
    body = text.split("\njobs:\n", 1)[1] if "\njobs:\n" in text else text
    out, name, start = {}, None, 0
    lines = body.splitlines(keepends=True)
    for i, ln in enumerate(lines):
        m = re.match(r"  ([A-Za-z0-9_-]+):\s*$", ln)
        if not m:
            continue
        if name is not None:
            out[name] = "".join(lines[start:i])
        name, start = m.group(1), i + 1
    if name is not None:
        out[name] = "".join(lines[start:])
    # A DROPPED KEY IS SILENT, and a refused FIRST key discards its whole
    # body -- everything before the next match is attributed to nobody. The
    # strict pattern above refuses legal spellings (a quoted key, a trailing
    # comment), so a permissive count is what makes the drop loud instead.
    loose = re.findall(r"(?m)^  (?![ #])\S.*?:", body)
    assert len(out) == len(loose), (
        f"the job parse dropped a key: parsed {sorted(out)} against "
        f"{len(loose)} key(s) on the page"
    )
    return out


def test_no_job_in_the_workflow_can_swallow_its_own_failure():
    """Every job, not the two this file happens to name.

    The ubuntu `test` job runs the entire suite and nothing here referenced
    it, so one line made the primary gate green regardless of failures.
    """
    jobs = _workflow_jobs(_WORKFLOW)
    # THE DENOMINATOR. An empty parse reports a clean sweep over nothing.
    assert len(jobs) >= 3, (
        f"the workflow parse found {len(jobs)} job(s), so this would pass "
        f"over almost nothing: {sorted(jobs)}"
    )
    for name, body in sorted(jobs.items()):
        _assert_no_step_can_swallow_failure(body, name)


@pytest.mark.parametrize("spelling", [
    "true", "True", "TRUE",
    "true  # flaky on the shared runner",
    "${{ true }}",
    "'true'",
])
def test_a_job_that_cannot_fail_is_refused_for_every_job(tmp_path, spelling):
    """The guard had no witness at all -- both call sites could be deleted.

    Run per JOB, so a job added without the guard fails here rather than
    going quiet. The three spellings are one assert apart and YAML resolves
    all of them to boolean true.
    """
    text = _WORKFLOW.read_text(encoding="utf-8")
    names = sorted(_workflow_jobs(_WORKFLOW))
    assert len(names) >= 3, f"nothing to sabotage: {names}"
    for name in names:
        anchor = f"\n  {name}:\n"
        assert text.count(anchor) == 1, f"{name}: anchor is not unique"
        broken = text.replace(
            anchor, f"{anchor}    continue-on-error: {spelling}\n", 1)
        assert broken != text, f"{name}: nothing was mutated"
        f = tmp_path / "ci.yml"
        f.write_text(broken, encoding="utf-8")
        with pytest.raises(AssertionError, match="continue-on-error"):
            for jn, body in sorted(_workflow_jobs(f).items()):
                _assert_no_step_can_swallow_failure(body, jn)


def _assert_no_step_can_swallow_failure(job: str, which: str) -> None:
    """A STEP THAT CANNOT FAIL RUNS NOTHING, as far as CI is concerned.

    One line makes every shard of a job green regardless of failures, which
    is the loudest form of "this file silently stopped running in CI". It
    was guarded on one job and not on the other, so it lives here.
    """
    # ANYTHING THAT IS NOT LITERALLY `false`. Matching spellings of TRUE
    # loses every one nobody enumerated: a trailing comment defeats an
    # end-anchor (`true  # flaky on the shared runner` is how a person
    # actually writes it), and `${{ }}` is GitHub's own documented form.
    assert not re.search(
        r"^\s*continue-on-error\s*:\s*(?!(?:'|\"|\$\{\{\s*)?false\b)\S",
        job, re.M | re.I), (
        f"the {which} job's step is continue-on-error, so a failure there "
        "is green"
    )


def _assert_windows_job_consumes_the_matrix(workflow: Path) -> None:
    """The shards are data ONLY if the job actually consumes them.

    Everything above reads the matrix and nothing read its consumer, so three
    edits that collapse sharding entirely were invisible: dropping
    `${{ matrix.paths }}` makes the matrix dead data and every `--ignore`
    inert; dropping `-o testpaths=` lets pyproject's own `testpaths` override
    the positional paths so each shard silently runs the whole suite; deleting
    the run line stops the job testing anything. All three left the suite green.
    """
    text = workflow.read_text(encoding="utf-8")
    block = re.search(r"\n  test-windows:\n(.*?)(?=\n  \S|\Z)", text, re.S)
    assert block, "the test-windows job is gone or was renamed"
    runs = _pytest_run_lines(block.group(1))
    assert runs, "the Windows job no longer invokes pytest — the shards run nowhere"
    # ONE INVOCATION, NOT THEIR CONCATENATION. Joined, a second pytest step
    # anywhere in the job satisfies both markers below for the real shard
    # command -- the exact collapse this case exists to forbid. It also pins
    # the count `_mutate_windows_run` already assumes.
    assert len(runs) == 1, (
        f"the Windows job invokes pytest {len(runs)} times; the shard markers "
        f"below would be satisfied across them rather than by one line: {runs!r}"
    )
    line = runs[0]
    assert "${{ matrix.paths }}" in line, (
        "the Windows pytest command does not consume `matrix.paths`, so the "
        f"shard matrix is dead data and every shard runs the same thing: {line!r}"
    )
    # KEPT, BUT NOT FOR THE REASON THIS ONCE GAVE. Measured: the `rest` shard
    # collects the same number with the flag and without, and every other
    # shard matches too -- positional paths already beat `testpaths`, so the
    # consequence the old message named ("each shard runs it all") does not
    # occur. The control that the flag is honoured at all is `-o
    # testpaths=docs`, which collects nothing. What it protects is a shard
    # that ever needs to ignore the testpaths root itself.
    assert "-o testpaths=" in line, (
        "the Windows pytest command does not clear `testpaths`, so a shard "
        f"cannot ignore the testpaths root itself: {line!r}"
    )


def test_a_backslash_continuation_keeps_the_tail_it_continues_into():
    """COUNTING CANNOT SEE THIS EITHER. Only the FIRST body line carries the
    word `pytest`, so joined-into-one and split-into-two both return exactly
    one `run:` line -- the join had no witness. What is lost when it goes is
    the TAIL, which is where the markers the caller asserts live.
    """
    job = ("    - name: t\n      run: |\n"
           "        uv run pytest -n 4 \\\n"
           "          -o testpaths= ${{ matrix.paths }}")
    runs = _pytest_run_lines(job)
    assert len(runs) == 1, runs
    assert "-o testpaths=" in runs[0], (
        f"the continuation was dropped, so the markers went with it: {runs!r}"
    )
    assert "${{ matrix.paths }}" in runs[0], runs[0]


def test_a_comment_naming_a_deleted_test_file_is_not_a_missing_path(tmp_path):
    """Only what the job RUNS can name a file it needs.

    `_assert_macos_job_is_intact` scans the job text for test paths, so
    without the strip an ordinary comment mentioning a since-deleted file
    reddens the suite AND masks the case that checks a real one. Measured
    when the duplicate strip was removed and the removal was reported as a
    no-op; this is the witness that was missing then.
    """
    wf = tmp_path / "ci.yml"
    wf.write_text(
        _WORKFLOW.read_text(encoding="utf-8").replace(
            "    runs-on: macos-latest",
            "    runs-on: macos-latest\n"
            "    # tests/test_gone_forever.py was folded into another file",
            1),
        encoding="utf-8")
    _assert_macos_job_is_intact(wf)


@pytest.mark.parametrize("command", [
    'uv run pytest -k "smoke # fast" -n 4 -o testpaths= ${{ matrix.paths }}',
    "uv run pytest -k 'a # b' -n 4 -o testpaths= ${{ matrix.paths }}",
    "uv run pytest -n 4 -o testpaths= ${{ matrix.paths }}  # shard it",
])
def test_a_hash_the_shell_does_not_treat_as_a_comment_is_not_cut(command):
    """COUNTING LINES CANNOT SEE THIS. A truncated command still contains the
    word `pytest`, so the reader still returns one `run:` line and every
    count-based case passes -- what is lost is the TAIL, which is where every
    marker the caller asserts lives. So the content is what has to be read.

    `-k "smoke # fast"` carries a `#` that starts no comment, and cutting
    there refuses a correct workflow: the opposite failure to the one the
    strip exists for. A real trailing comment is still removed, and the
    command in front of it survives whole.
    """
    job = "    - name: t\n      run: |\n        " + command
    runs = _pytest_run_lines(job)
    assert len(runs) == 1, runs
    assert "${{ matrix.paths }}" in runs[0], (
        f"the command was truncated at a `#` the shell does not honour: {runs[0]!r}"
    )
    assert "-o testpaths=" in runs[0], runs[0]
    assert "# shard it" not in runs[0], (
        f"a real trailing comment survived the strip: {runs[0]!r}"
    )


@pytest.mark.parametrize("scalar", ["plain", "|", ">"])
@pytest.mark.parametrize("sep", [";", "&&", "||", "|", "&"])
def test_a_second_command_on_one_line_is_not_read_as_one(scalar, sep):
    """The collapse this file exists to forbid, spelled on ONE line.

    Chained, the shard command loses `-o testpaths=` while a harmless second
    `pytest` carries it -- and every marker below is then satisfied across
    the two. The splitter shipped knowing `;`, `&&` and `||`; `|` and `&`
    are the remaining POSIX list operators and run a second command exactly
    the same way, so both read CLEAN on the real workflow.

    Every emit path, because the split is applied at three call sites.
    """
    cmd = ("uv run pytest -n 4 ${{ matrix.paths }} " + sep
           + " uv run pytest -o testpaths= --version")
    if scalar == "plain":
        job = "    - name: t\n      run: " + cmd
    else:
        job = f"    - name: t\n      run: {scalar}\n        " + cmd
    runs = _pytest_run_lines(job)
    assert len(runs) == 2, (
        f"`{sep}` runs a second command and the reader saw one: {runs!r}"
    )


@pytest.mark.parametrize("expr,commands", [
    # GitHub's own expression syntax uses `&&` and `||`; cutting inside one
    # halves a CORRECT command -- the false refusal.
    ("uv run pytest ${{ a && b }} -o testpaths=", 1),
    ("uv run pytest ${{ a || b }} -o testpaths=", 1),
    # A redirect is not a separator.
    ("uv run pytest -o testpaths= 2>&1", 1),
    # An UNTERMINATED `${{` must not swallow the rest of the line, or every
    # separator after it is invisible.
    ("uv run pytest ${{ a } ; uv run pytest --version", 2),
])
def test_the_splitter_cuts_shell_operators_and_nothing_else(expr, commands):
    assert len(_shell_commands(expr)) == commands, _shell_commands(expr)


@pytest.mark.parametrize("scalar,body,expected", [
    (
        ">",
        ["# keep the shard list on one line",
         "uv run pytest -n 4 -o testpaths= ${{ matrix.paths }}"],
        0,
    ),
    (
        "|",
        ["# keep the shard list on one line",
         "uv run pytest -n 4 -o testpaths= ${{ matrix.paths }}"],
        1,
    ),
    (
        "|",
        ["uv run pytest -n 4 ${{ matrix.paths }}",
         "uv run pytest -o testpaths= --version"],
        2,
    ),
    (
        "|",
        ["uv run pytest -n 4 \\",
         "  -o testpaths= ${{ matrix.paths }}"],
        1,
    ),
])
def test_the_reader_folds_a_block_the_way_its_scalar_says(
    scalar, body, expected
):
    """`|` and `>` are different commands, and reading them alike is a hole.

    `>` FOLDS its lines into one shell line, so a `#` anywhere before the
    command comments out everything after it -- stripping per source line
    first drops the `#` line and reassembles a live-looking invocation, which
    is the false CLEAN the shared stripping was added to close, rebuilt one
    spelling over. `paths: >-` is already used in this workflow, so `run: >`
    is an ordinary next edit.

    `|` keeps its lines SEPARATE, so joining them hides a second invocation
    inside one block -- and the real collapse is a shard command that loses
    `-o testpaths=` while a harmless second `pytest` carries it. The caller's
    `len(runs) == 1` cannot see that through a join. A trailing backslash is
    the one case that really is one command, so it is joined first.

    THE STRIP IS QUOTE-AWARE for the same reason the shell is: `-k "smoke #
    fast"` carries a `#` that starts no comment, and cutting there truncates a
    LIVE command to something with no markers -- a false refusal on a correct
    workflow, the opposite failure to the one the strip exists for. A real
    trailing comment is still removed and the command still read.
    """
    job = "    - name: t\n      run: " + scalar + "\n" + "\n".join(
        "        " + ln for ln in body)
    assert len(_pytest_run_lines(job)) == expected


def test_a_commented_out_macos_command_is_refused(tmp_path):
    """The comment stripping's own witness, which nothing had.

    Removing the strip left the file green, and the case deleted for claiming
    to witness it fired only when the run anchor went too. Folded, the shape
    is the one that matters: a `run: |` whose command is commented out joins
    into a value carrying `pytest`, and only the stripping tells that from a
    job that still invokes it.
    """
    text = _WORKFLOW.read_text(encoding="utf-8")
    block = re.search(r"\n  macos-keychain:\n(.*?)(?=\n  \S|\Z)", text, re.S)
    assert block, "the macos-keychain job is gone or was renamed"
    real = re.search(r"\n( +)run: ([^\n]*(?<![\w-])pytest(?![\w-])[^\n]*)",
                     block.group(1))
    assert real, "premise: no inline pytest run line to comment out"
    indent, cmd = real.group(1), real.group(2)
    disabled = text.replace(
        f"\n{indent}run: {cmd}",
        f"\n{indent}run: |\n{indent}  # {cmd}\n{indent}  echo \"disabled\"",
        1,
    )
    assert disabled != text, "nothing was mutated — this check lost its subject"
    fake = tmp_path / "ci.yml"
    fake.write_text(disabled, encoding="utf-8")
    with pytest.raises(AssertionError, match="no longer invokes pytest"):
        _assert_macos_job_is_intact(fake)


def test_a_commented_out_windows_command_is_refused(tmp_path):
    """The Windows reader is handed the RAW job; the macOS one is stripped.

    Folding a block scalar joins its lines, so a `#` that comments the real
    command out ends up INSIDE the folded value carrying `pytest`,
    `${{ matrix.paths }}` and `-o testpaths=` — every marker the guard looks
    for, on a job that runs `echo`. The asymmetry is what makes it a false
    CLEAN on one job and a correct refusal on the other.
    """
    text = _WORKFLOW.read_text(encoding="utf-8")
    block = re.search(r"\n  test-windows:\n(.*?)(?=\n  \S|\Z)", text, re.S)
    assert block, "the test-windows job is gone or was renamed"
    real = re.search(r"\n( +)run: ([^\n]*(?<![\w-])pytest(?![\w-])[^\n]*)",
                     block.group(1))
    assert real, "premise: no inline pytest run line to comment out"
    indent, cmd = real.group(1), real.group(2)
    disabled = text.replace(
        f"\n{indent}run: {cmd}",
        f"\n{indent}run: |\n{indent}  # {cmd}\n{indent}  echo \"disabled\"",
        1,
    )
    assert disabled != text, "nothing was mutated — this check lost its subject"
    fake = tmp_path / "ci.yml"
    fake.write_text(disabled, encoding="utf-8")
    with pytest.raises(AssertionError, match="no longer invokes pytest"):
        _assert_windows_job_consumes_the_matrix(fake)


def test_the_windows_job_consumes_the_shard_matrix():
    _assert_windows_job_consumes_the_matrix(_WORKFLOW)


def test_a_step_named_for_pytest_that_runs_nothing_is_refused(tmp_path):
    """The scoping fix had no witness: every case passed with it reverted.

    Keying anywhere in the job rather than on the `run:` line accepts a step
    whose NAME says pytest while its command does something else -- and
    "Run pytest on macOS" is the most ordinary step name there is. Measured:
    with the reader loosened back to a bare search over the block, this is the
    only case that fails.
    """
    text = _WORKFLOW.read_text(encoding="utf-8")
    renamed = _mutate_macos_run(text, "\n        name: Run pytest on macOS")
    assert renamed != text, "nothing was mutated — this check lost its subject"
    fake = tmp_path / "ci.yml"
    fake.write_text(renamed, encoding="utf-8")
    with pytest.raises(AssertionError, match="no longer invokes pytest"):
        _assert_macos_job_is_intact(fake)


def _mutate_windows_run(text: str, repl: str) -> str:
    block = re.search(r"\n  test-windows:\n(.*?)(?=\n  \S|\Z)", text, re.S)
    assert block, "the test-windows job is gone or was renamed"
    body, n = re.subn(
        r"(\n +run: [^\n]*(?<![\w-])pytest(?![\w-])[^\n]*)", repl,
        block.group(1), count=1,
    )
    assert n == 1, "the Windows job has no pytest invocation to mutate"
    return text[: block.start(1)] + body + text[block.end(1) :]


@pytest.mark.parametrize(
    "repl, expected",
    [
        ("", "no longer invokes pytest"),
        (lambda m: m.group(1).replace(" ${{ matrix.paths }}", ""), "dead data"),
        (lambda m: m.group(1).replace(" -o testpaths=", ""),
         "cannot ignore the testpaths root itself"),
    ],
    ids=["invocation-deleted", "matrix-not-consumed", "testpaths-not-cleared"],
)
def test_each_way_the_windows_shards_collapse_is_refused(tmp_path, repl, expected):
    """The three edits that were measured green before this existed."""
    text = _WORKFLOW.read_text(encoding="utf-8")
    broken = _mutate_windows_run(text, repl)
    assert broken != text, "nothing was mutated — this check lost its subject"
    fake = tmp_path / "ci.yml"
    fake.write_text(broken, encoding="utf-8")
    with pytest.raises(AssertionError, match=expected):
        _assert_windows_job_consumes_the_matrix(fake)


def _uncomment(text: str) -> str:
    """`text` up to the `#` that starts a comment, by the SHELL's rule.

    A `#` inside a quoted string starts no comment -- `pytest -k "smoke #
    fast"` carries one -- and cutting there truncates a LIVE command to
    something with no markers, which is a false refusal, the opposite failure
    to the one this exists for. A backslash escapes the next character inside
    a double quote, so it does not close it.

    AN UNBALANCED QUOTE IS NOT A LICENCE TO KEEP EVERYTHING. Returning the
    whole line there lets a real comment survive and answer the invocation
    question -- a job running `echo` reads as running the shard command, which
    is the original false CLEAN. Unparseable falls back to the unquoted rule,
    which is what the regex this replaced always did.
    """
    quote, i, n = None, 0, len(text)
    while i < n:
        ch = text[i]
        if quote:
            if ch == "\\" and quote == '"':
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or text[i - 1] in " \t"):
            return text[:i]
        i += 1
    if quote:
        return re.sub(r"(?<!\S)#.*", "", text)
    return text


def _shell_commands(line: str) -> list[str]:
    """One shell line split into the commands it actually runs.

    A `;`, `&&`, `||`, `|` or `&` chain is several commands on one line, and
    reading them as one hides a second invocation exactly as joining a block
    scalar's lines did -- the shard command loses `-o testpaths=` while a
    harmless second `pytest` on the same line carries it. The two-character
    operators are consumed first, so a bare `|`/`&` is what falls through;
    a redirect (`&>`, `>&`, `2>&1`) is not a separator.

    QUOTES AND `${{ }}` ARE SKIPPED. GitHub's own expression syntax uses `&&`
    and `||`, so splitting inside `${{ ... }}` would cut a CORRECT command in
    half -- the false refusal, again.
    """
    out, buf, quote, i, n = [], "", None, 0, len(line)
    while i < n:
        two = line[i:i + 2]
        if quote:
            if line[i] == "\\" and quote == '"':
                buf += line[i:i + 2]
                i += 2
                continue
            if line[i] == quote:
                quote = None
        elif line[i] in "'\"":
            quote = line[i]
        elif line.startswith("${{", i):
            end = line.find("}}", i)
            if end < 0:
                # UNTERMINATED IS NOT "TO END OF LINE". Copying the rest
                # verbatim hides every separator after it, so a chain reads
                # as one command. Treat the marker as ordinary text and keep
                # scanning; Actions rejects the file anyway, but only AFTER
                # this suite has said CLEAN.
                buf += "${{"
                i += 3
                continue
            buf += line[i:end + 2]
            i = end + 2
            continue
        elif two in ("&&", "||"):
            out.append(buf)
            buf, i = "", i + 2
            continue
        elif line[i] == ";":
            out.append(buf)
            buf, i = "", i + 1
            continue
        elif line[i] in "|&" and not (
                two in ("&>", ">&") or (buf and buf[-1] == ">")):
            out.append(buf)
            buf, i = "", i + 1
            continue
        buf += line[i]
        i += 1
    out.append(buf)
    return [c.strip() for c in out if c.strip()]


def _pytest_run_lines(job: str) -> list[str]:
    """The job's `run:` lines that invoke pytest.

    ON THE `run:` LINE, not anywhere in the block. Keyed loosely, the word in a
    step's `name:` answers yes while the command does nothing -- and `name: Run
    pytest on macOS` is the most ordinary step name there is, so the loose form
    is both a false clean and a false alarm.

    Block scalars are folded first, because `run: |` is a legal spelling of the
    same command and the header alone carries no `pytest`: read literally it
    reports a job that tests everything as testing nothing.

    WHAT THIS STILL CANNOT DO: the two mutation helpers below select their
    target with a line regex and do NOT fold. On a workflow written with block
    scalars they raise "no pytest invocation to mutate" -- and they raise it
    whether that workflow is CORRECT or runs nothing, so that refusal has no
    discriminating power and calling it "the safe direction" overstates it.
    What actually separates the two is the comment stripping above, which is
    why it lives in this reader and not in one caller.
    """
    lines, folded, i = job.splitlines(), [], 0
    while i < len(lines):
        # `|2` / `>2-` carry an explicit indentation indicator and are legal.
        # A TRAILING COMMENT ON THE HEADER IS LEGAL YAML. Unmatched, the
        # block never folds and a CORRECT job reads as invoking nothing.
        head = re.match(
            r"(\s*)run: *([|>])[-+]?\d*[-+]?\s*(?:#.*)?$", lines[i])
        if not head:
            # A PLAIN `run:` LINE IS A SHELL LINE TOO, so a chain on it
            # hides a second invocation the same way.
            head_run = re.match(r"(\s*)run: +(\S.*)$", lines[i])
            if head_run:
                folded += [head_run.group(1) + "run: " + c
                           for c in _shell_commands(
                               _uncomment(head_run.group(2)))]
            else:
                folded.append(_uncomment(lines[i]))
            i += 1
            continue
        # A BLOCK SCALAR IS THE SAME COMMAND. `run: |` puts the invocation on
        # the following, deeper-indented lines; read literally the header has
        # no `pytest` in it and the job reads as running nothing.
        pad, scalar = head.group(1), head.group(2)
        indent = len(pad)
        i, raw_body = i + 1, []
        while i < len(lines) and (
            not lines[i].strip()
            or len(lines[i]) - len(lines[i].lstrip()) > indent
        ):
            raw_body.append(lines[i])
            i += 1
        # THE BLOCK'S OWN INDENT, from its first non-empty line: anything
        # deeper is a YAML newline, not a continuation of the same line.
        body_indent = next(
            (len(r) - len(r.lstrip()) for r in raw_body if r.strip()),
            indent + 1)
        body = [x.strip() for x in raw_body if x.strip()]
        if scalar == ">":
            # FOLDED ONLY BETWEEN SAME-INDENT NON-EMPTY LINES. A blank line
            # and a MORE-indented line both stay newlines in YAML, so they
            # separate commands exactly as `|` does -- joining across them
            # hides a second invocation, which is the collapse the `|` arm
            # was just fixed for. Within a group the fold is real, and a `#`
            # anywhere before the command comments out the rest of it.
            group, groups = [], []
            for raw in raw_body:
                if not raw.strip() or (
                        len(raw) - len(raw.lstrip()) > body_indent):
                    if group:
                        groups.append(group)
                    group = [] if not raw.strip() else [raw.strip()]
                    if group:
                        groups.append(group)
                        group = []
                    continue
                group.append(raw.strip())
            if group:
                groups.append(group)
            folded += [pad + "run: " + c
                       for g in groups if g
                       for c in _shell_commands(_uncomment(" ".join(g)))]
            continue
        # `|` KEEPS ITS LINES SEPARATE, so each is its own command and joining
        # them hides a second invocation inside one block. A trailing
        # backslash is the one case that really is one command.
        logical, buf = [], ""
        for ln in body:
            buf += ln[:-1] + " " if ln.endswith("\\") else ln
            if not ln.endswith("\\"):
                logical.append(buf)
                buf = ""
        if buf:
            logical.append(buf)
        folded += [pad + "run: " + c for x in logical
                   for c in _shell_commands(_uncomment(x))]
    return [
        ln for ln in folded
        if re.match(r"\s*run: ", ln) and re.search(r"(?<![\w-])pytest(?![\w-])", ln)
    ]


def _assert_macos_job_is_intact(workflow: Path) -> None:
    # EVERY JOB, not just macos-keychain: the ubuntu `test` job runs the
    # whole suite and had neither of these two checks, so `run: echo
    # skipping` and a `run:` naming a deleted test file both stayed green.
    for name, job in sorted(_workflow_jobs(workflow).items()):
        assert _pytest_run_lines(job), (
            f"the {name} job no longer invokes pytest — it would stay green testing nothing"
        )
        # STRIPPED FOR THIS SCAN TOO. `_macos_job` used to strip comments
        # before returning, and removing that duplicate stripping was
        # reported as a no-op -- it is not: the path scan reads this string,
        # so an ordinary comment naming a since-deleted test file reddens the
        # suite here AND masks `test_a_job_naming_a_deleted_file_is_refused`.
        # Only what the job RUNS can name a file it needs.
        live = "\n".join(_uncomment(ln) for ln in job.splitlines())
        for path in re.findall(r"tests/test_\w+\.py", live):
            assert (_ROOT / path).exists(), f"the {name} job names {path}, which does not exist"
    # A STEP THAT CANNOT FAIL RUNS NOTHING, as far as CI is concerned. LAST,
    # because it is the broadest: raised ahead of the path check it masked
    # `test_a_job_naming_a_deleted_file_is_refused`, which then failed with
    # "regex did not match" about a workflow whose real defect was elsewhere.
    # NOT HERE. `test_no_job_in_the_workflow_can_swallow_its_own_failure`
    # asks this of EVERY job, so a copy inside a per-job helper adds nothing
    # and carries the masking hazard its own comment used to describe: it is
    # the broadest assert in the function, and raised first it reports four
    # unrelated cases as "regex did not match".


def test_the_macos_job_runs_pytest_on_paths_that_exist():
    """Ways the macOS job goes green testing nothing that THIS can see: the
    invocation deleted, the word present only in a step name, the step made
    continue-on-error, or a rename leaving it naming a file that is gone.

    NOT AN EXHAUSTIVE LIST, and deliberately not. `echo "uv run pytest ..."`,
    `|| true`, `--collect-only` and `-k nothing` all leave a real pytest
    invocation on a real `run:` line while selecting or running nothing.
    Judging that needs pytest's own parser; the reader that tried it was 250
    lines and got 16 of 31 shapes wrong, which is why it was deleted.
    """
    _assert_macos_job_is_intact(_WORKFLOW)


def _mutate_macos_run(text: str, repl: str) -> str:
    """`text` with the macOS job's pytest `run:` line rewritten by `repl`.

    SCOPED TO THAT JOB, and keyed on the invocation rather than on any path it
    names: a sibling PR replaces the two named files with a bare `pytest`, so a
    substitution keyed on a filename matches nothing once merged and the case
    that used it silently stops testing.
    """
    block = re.search(r"\n  macos-keychain:\n(.*?)(?=\n  \S|\Z)", text, re.S)
    assert block, "the macos-keychain job is gone or was renamed"
    body, n = re.subn(
        r"(\n +run: [^\n]*(?<![\w-])pytest(?![\w-])[^\n]*)", repl,
        block.group(1), count=1,
    )
    assert n == 1, "the macOS job has no pytest invocation to mutate"
    return text[: block.start(1)] + body + text[block.end(1) :]


def test_a_job_that_stopped_running_pytest_is_refused(tmp_path):
    """The deletion above, on a real copy of the real workflow."""
    text = _WORKFLOW.read_text(encoding="utf-8")
    gutted = _mutate_macos_run(text, "")
    assert gutted != text, "nothing was mutated — this check lost its subject"
    fake = tmp_path / "ci.yml"
    fake.write_text(gutted, encoding="utf-8")
    with pytest.raises(AssertionError, match="no longer invokes pytest"):
        _assert_macos_job_is_intact(fake)


def test_a_job_naming_a_deleted_file_is_refused(tmp_path):
    """The other half: the invocation survives, a path it names does not.

    The decoy is APPENDED, so this holds whether the job names two files or
    none -- which is exactly what a sibling PR changes about this step.
    """
    text = _WORKFLOW.read_text(encoding="utf-8")
    assert not (_ROOT / "tests/test_gone.py").exists(), "the decoy path exists"
    stale = _mutate_macos_run(text, r"\1 tests/test_gone.py")
    assert stale != text, "nothing was mutated — this check lost its subject"
    fake = tmp_path / "ci.yml"
    fake.write_text(stale, encoding="utf-8")
    with pytest.raises(AssertionError, match="names tests/test_gone.py"):
        _assert_macos_job_is_intact(fake)
