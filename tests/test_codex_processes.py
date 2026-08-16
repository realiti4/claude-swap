"""Detecting running codex processes, so a switch can warn about restarts."""

from __future__ import annotations

from claude_swap.codex import processes


def test_no_processes_when_the_lister_returns_nothing(monkeypatch):
    monkeypatch.setattr(processes, "_list_processes", lambda: [])
    assert processes.running_codex_pids() == []


def test_a_codex_process_is_detected(monkeypatch):
    monkeypatch.setattr(
        processes, "_list_processes", lambda: [(4242, "/Users/x/.local/bin/codex")]
    )
    assert processes.running_codex_pids() == [4242]


def test_an_unrelated_process_is_ignored(monkeypatch):
    monkeypatch.setattr(processes, "_list_processes", lambda: [(1, "/usr/bin/python")])
    assert processes.running_codex_pids() == []


def test_a_path_merely_containing_codex_does_not_match(monkeypatch):
    """`/Users/me/codex-notes/server` is not the codex CLI. Matching on the
    executable name is what keeps the restart warning from crying wolf."""
    monkeypatch.setattr(
        processes, "_list_processes", lambda: [(7, "/Users/me/codex-notes/server")]
    )
    assert processes.running_codex_pids() == []


def test_the_codext_fork_counts_as_codex(monkeypatch):
    """codext switches seamlessly, but it is still a running Codex session the
    user may want to know about."""
    monkeypatch.setattr(processes, "_list_processes", lambda: [(9, "/usr/local/bin/codext")])
    assert processes.running_codex_pids() == [9]


def test_lister_failure_degrades_to_no_processes(monkeypatch):
    def boom():
        raise OSError("ps missing")

    monkeypatch.setattr(processes, "_list_processes", boom)
    assert processes.running_codex_pids() == []


def test_the_posix_ps_parser_reads_real_output(monkeypatch):
    """The only coverage the POSIX branch gets — every other test here patches
    the lister away."""

    class R:
        stdout = "  501 codex\n  900 python3\n 1200 codext\n"

    monkeypatch.setattr(processes.sys, "platform", "darwin")
    monkeypatch.setattr(processes.subprocess, "run", lambda *a, **k: R())

    assert processes._list_processes() == [(501, "codex"), (900, "python3"), (1200, "codext")]
    assert processes.running_codex_pids() == [501, 1200]


def test_the_windows_tasklist_parser_reads_real_csv_output(monkeypatch):
    """Windows support was an explicit decision, and every other test in this
    module patches the lister away — so this is the only coverage the win32
    branch gets. The CSV is real `tasklist /FO CSV /NH` output."""

    class R:
        stdout = (
            '"codex.exe","4242","Console","1","52,000 K"\n'
            '"explorer.exe","900","Console","1","98,000 K"\n'
        )

    monkeypatch.setattr(processes.sys, "platform", "win32")
    monkeypatch.setattr(processes.subprocess, "run", lambda *a, **k: R())

    assert processes._list_processes() == [(4242, "codex.exe"), (900, "explorer.exe")]
    assert processes.running_codex_pids() == [4242]


def test_the_exe_suffix_is_stripped_when_matching():
    assert processes._executable_name("C:\\tools\\codex.exe") == "codex"
    assert processes._executable_name("C:\\tools\\CODEX.EXE") == "CODEX"
    assert processes._executable_name("/usr/bin/codex") == "codex"
