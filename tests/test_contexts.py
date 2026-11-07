"""Work / personal contexts: rules, resolution, the Claude Code hook, the
auto-switch barrier and the `cswap context` / `cswap run` CLI surface.

Synthetic fixtures only; the real store is never touched (conftest guards).
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import cli, hook, paths
from claude_swap.autoswitch import NoSwitchEvent, SwitchEvent, TickOutcome
from claude_swap.contexts import (
    MODE_ASYMMETRIC,
    MODE_OPEN,
    MODE_STRICT,
    PERSONAL,
    SHARED,
    WORK,
    ContextStore,
    check,
)
from claude_swap.mappings import normalize_path
from claude_swap.process_detection import ClaudeSession

from tests.test_account_policy_store import PolicyStoreBase
from tests.test_autoswitch import EngineHarness, _usage


# -- the rules ---------------------------------------------------------------


@pytest.mark.parametrize(
    "mode, directory, account, allowed",
    [
        # open: labels only
        (MODE_OPEN, WORK, PERSONAL, True),
        (MODE_OPEN, PERSONAL, WORK, True),
        # asymmetric: only work directories are fenced
        (MODE_ASYMMETRIC, WORK, WORK, True),
        (MODE_ASYMMETRIC, WORK, PERSONAL, False),
        (MODE_ASYMMETRIC, WORK, None, False),  # untagged counts as personal
        (MODE_ASYMMETRIC, PERSONAL, WORK, True),
        (MODE_ASYMMETRIC, PERSONAL, PERSONAL, True),
        (MODE_ASYMMETRIC, SHARED, PERSONAL, True),
        # strict: both directions fenced, shared accepts all
        (MODE_STRICT, WORK, PERSONAL, False),
        (MODE_STRICT, PERSONAL, WORK, False),
        (MODE_STRICT, PERSONAL, None, True),
        (MODE_STRICT, SHARED, WORK, True),
        (MODE_STRICT, SHARED, PERSONAL, True),
    ],
)
def test_check_matrix(mode, directory, account, allowed):
    verdict = check(directory, account, mode)
    assert verdict.allowed is allowed
    assert bool(verdict.reason) is (not allowed)


# -- resolution --------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> ContextStore:
    return ContextStore(tmp_path / "backup")


def _git_repo(root: Path, url: str) -> Path:
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "config").write_text(
        f'[remote "origin"]\n\turl = {url}\n\tfetch = +refs/heads/*\n'
    )
    return root


def test_default_when_nothing_matches(store, tmp_path):
    r = store.resolve(tmp_path)
    assert (r.context, r.source) == (PERSONAL, "default")
    store.set_default_context(SHARED)
    assert store.resolve(tmp_path).context == SHARED


def test_path_rule_covers_subdirectories(store, tmp_path):
    work = tmp_path / "work"
    (work / "vpn" / "src").mkdir(parents=True)
    store.set_path(work, WORK)
    r = store.resolve(work / "vpn" / "src")
    assert (r.context, r.source, r.detail) == (WORK, "path", normalize_path(work))


def test_sibling_prefix_does_not_match(store, tmp_path):
    (tmp_path / "work").mkdir()
    (tmp_path / "workshop").mkdir()
    store.set_path(tmp_path / "work", WORK)
    assert store.resolve(tmp_path / "workshop").source == "default"


def test_nearest_rule_wins_marker_inside_path_rule(store, tmp_path):
    work = tmp_path / "work"
    side = work / "side-project"
    side.mkdir(parents=True)
    store.set_path(work, WORK)
    (side / ".claude-context").write_text("# mine\nshared\n")
    r = store.resolve(side)
    assert (r.context, r.source) == (SHARED, "marker")


def test_nearest_rule_wins_path_rule_inside_marker(store, tmp_path):
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    (outer / ".claude-context").write_text("personal")
    store.set_path(inner, WORK)
    assert store.resolve(inner).context == WORK


def test_invalid_marker_is_ignored(store, tmp_path):
    (tmp_path / ".claude-context").write_text("boss\n")
    assert store.resolve(tmp_path).source == "default"


def test_remote_hint(store, tmp_path):
    repo = _git_repo(tmp_path / "vpn", "git@gitlab.Acme.com:infra/vpn.git")
    (repo / "sub").mkdir()
    store.set_remote("gitlab.acme.com", WORK)
    r = store.resolve(repo / "sub")
    assert (r.context, r.source, r.detail) == (WORK, "remote", "gitlab.acme.com")


@pytest.mark.parametrize("url", [
    "git@github.com:Acme-Corp/vpn.git",
    "ssh://git@github.com/Acme-Corp/vpn.git",
    "https://github.com/Acme-Corp/vpn",
    "https://token@github.com/acme-corp/vpn.git",
    "git@github-work:Acme-Corp/vpn.git",  # ~/.ssh/config host alias
])
def test_remote_hint_matches_ssh_and_https_spellings(store, tmp_path, url):
    repo = _git_repo(tmp_path / "vpn", url)
    store.set_remote("https://github.com/Acme-Corp", WORK)
    store.set_remote("github-work", WORK)
    assert store.resolve(repo).context == WORK


def test_remote_hint_does_not_match_other_org(store, tmp_path):
    repo = _git_repo(tmp_path / "r", "git@github.com:acme-corporation-fan/x.git")
    store.set_remote("github.com/acme-corp/", WORK)
    assert store.resolve(repo).source == "default"


def test_longest_remote_pattern_wins(store, tmp_path):
    repo = _git_repo(tmp_path / "r", "https://gitlab.acme.com/oss/tool.git")
    store.set_remote("gitlab.acme.com", WORK)
    store.set_remote("gitlab.acme.com/oss", SHARED)
    assert store.resolve(repo).context == SHARED


def test_path_rule_beats_remote_hint(store, tmp_path):
    repo = _git_repo(tmp_path / "r", "https://gitlab.acme.com/x.git")
    store.set_remote("gitlab.acme.com", WORK)
    store.set_path(repo, PERSONAL)
    assert store.resolve(repo).source == "path"


def test_remote_hint_through_worktree_gitdir_file(store, tmp_path):
    main = _git_repo(tmp_path / "main", "git@gitlab.acme.com:a/b.git")
    wt_meta = main / ".git" / "worktrees" / "feat"
    wt_meta.mkdir(parents=True)
    (wt_meta / "commondir").write_text("../..\n")
    worktree = tmp_path / "feat"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {wt_meta}\n")
    store.set_remote("gitlab.acme.com", WORK)
    assert store.resolve(worktree).context == WORK


def test_persistence_and_validation(store, tmp_path):
    store.set_mode(MODE_STRICT)
    store.set_account_context("a@x.test", "org-1", WORK)
    assert ContextStore(store.path.parent).mode() == MODE_STRICT
    assert store.account_context("a@x.test", "org-1") == WORK
    assert store.account_context("a@x.test", "") is None  # org is part of identity
    store.set_account_context("a@x.test", "org-1", None)
    assert store.account_context("a@x.test", "org-1") is None
    with pytest.raises(ValueError):
        store.set_mode("lax")
    with pytest.raises(ValueError):
        store.set_account_context("a@x.test", "", SHARED)  # accounts are work/personal
    with pytest.raises(ValueError):
        store.set_path(tmp_path, "boss")
    assert json.loads(store.path.read_text())["schemaVersion"] == 1


def test_corrupt_file_reads_as_defaults(store):
    store.path.parent.mkdir(parents=True)
    store.path.write_text("{not json")
    data = store.load()
    assert (data["mode"], data["default"], data["paths"]) == (
        MODE_ASYMMETRIC, PERSONAL, {},
    )


def test_blocked_directories(store, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    store.set_path(work, WORK)
    cwds = [str(work), str(tmp_path), ""]
    assert store.blocked_directories(cwds, "p@x.test", "") == [str(work)]
    store.set_account_context("p@x.test", "", WORK)
    assert store.blocked_directories(cwds, "p@x.test", "") == []
    store.set_mode(MODE_OPEN)
    store.set_account_context("p@x.test", "", None)
    assert store.blocked_directories(cwds, "p@x.test", "") == []


# -- the Claude Code hook ----------------------------------------------------


@pytest.fixture
def hook_env(tmp_path, monkeypatch):
    """A fake Claude config (live login) and backup root for the hook."""
    config_dir = tmp_path / "claude-profile"
    config_dir.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    # Ask paths where the hook will look: XDG on Linux, the legacy
    # ~/.claude-swap-backup (under the isolated home) on macOS and Windows.
    store = ContextStore(paths.get_backup_root())
    work = tmp_path / "work"
    work.mkdir()
    store.set_path(work, WORK)

    def login(email: str, org: str = "org-1") -> None:
        (config_dir / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": email, "organizationUuid": org},
        }))

    return store, work, login


def _run_hook(event: str, cwd: Path) -> dict | None:
    out = io.StringIO()
    stdin = io.StringIO(json.dumps({"hook_event_name": event, "cwd": str(cwd)}))
    assert hook.run(stdin, out) == 0
    return json.loads(out.getvalue()) if out.getvalue() else None


def test_hook_blocks_personal_account_in_work_dir(hook_env):
    store, work, login = hook_env
    login("me@perso.test")
    reply = _run_hook(hook.USER_PROMPT_SUBMIT, work)
    assert reply["decision"] == "block"
    assert "work directory" in reply["reason"]

    start = _run_hook(hook.SESSION_START, work)
    assert "WORK context" in start["hookSpecificOutput"]["additionalContext"]
    assert "Prompts will be blocked" in start["systemMessage"]


def test_hook_allows_tagged_work_account(hook_env):
    store, work, login = hook_env
    login("me@acme.test")
    store.set_account_context("me@acme.test", "org-1", WORK)
    assert _run_hook(hook.USER_PROMPT_SUBMIT, work) is None
    start = _run_hook(hook.SESSION_START, work)
    assert "systemMessage" not in start
    assert "me@acme.test (work)" in start["hookSpecificOutput"]["additionalContext"]


def test_hook_personal_dir_is_open_in_asymmetric_mode(hook_env, tmp_path):
    store, _, login = hook_env
    login("me@acme.test")
    store.set_account_context("me@acme.test", "org-1", WORK)
    assert _run_hook(hook.USER_PROMPT_SUBMIT, tmp_path) is None


def test_hook_without_login_says_nothing(hook_env):
    _, work, _ = hook_env
    assert _run_hook(hook.USER_PROMPT_SUBMIT, work) is None


@pytest.mark.parametrize("stdin", ["", "not json", "[]", '{"hook_event_name": "Stop"}'])
def test_hook_fails_open(stdin):
    out = io.StringIO()
    assert hook.run(io.StringIO(stdin), out) == 0
    assert out.getvalue() == ""


def test_main_dispatches_hook_before_anything_else(monkeypatch):
    monkeypatch.setattr("sys.argv", ["cswap", "hook"])
    with patch.object(hook, "run", return_value=0) as run:
        with pytest.raises(SystemExit) as exc:
            cli.main()
    assert exc.value.code == 0
    run.assert_called_once()


# -- installing the hook into settings.json ----------------------------------


def test_install_merges_and_is_idempotent(tmp_path):
    settings = tmp_path / "settings.json"
    other = {"type": "command", "command": "notify-send hi"}
    settings.write_text(json.dumps({
        "theme": "dark",
        "hooks": {"SessionStart": [{"hooks": [other]}]},
    }))
    assert hook.install(settings, "/bin/cswap hook") is True
    assert hook.install(settings, "/bin/cswap hook") is False
    data = json.loads(settings.read_text())
    assert data["theme"] == "dark"
    starts = data["hooks"]["SessionStart"]
    assert starts[0] == {"hooks": [other]}
    assert starts[1]["hooks"][0]["command"] == "/bin/cswap hook"
    assert len(data["hooks"]["UserPromptSubmit"]) == 1

    assert hook.install(settings, "/bin/cswap hook", uninstall=True) is True
    data = json.loads(settings.read_text())
    assert data["hooks"] == {"SessionStart": [{"hooks": [other]}]}


@pytest.mark.skipif(os.name == "nt", reason="symlinks")
def test_install_writes_through_symlink_and_keeps_mode(tmp_path):
    real = tmp_path / "dotfiles" / "settings.json"
    real.parent.mkdir()
    real.write_text("{}")
    real.chmod(0o600)
    link = tmp_path / "settings.json"
    link.symlink_to(real)
    hook.install(link, "cswap hook")
    assert link.is_symlink()
    assert "UserPromptSubmit" in json.loads(real.read_text())["hooks"]
    assert (real.stat().st_mode & 0o777) == 0o600


def test_install_refuses_non_object(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text("[]")
    with pytest.raises(ValueError):
        hook.install(settings, "cswap hook")


# -- the auto-switch barrier --------------------------------------------------


@pytest.fixture
def fleet(temp_home: Path):
    """Active 1 (work) over its limit; 2 personal with the most headroom; 3 work."""
    h = EngineHarness(temp_home)
    h.seed(1, "a@acme.test")
    h.seed(2, "b@perso.test")
    h.seed(3, "c@acme.test")
    h.make_live("a@acme.test", 1)
    store = ContextStore(h.switcher.backup_dir)
    store.set_account_context("a@acme.test", "", WORK)
    store.set_account_context("c@acme.test", "", WORK)
    work = temp_home / "work"
    work.mkdir()
    store.set_path(work, WORK)
    return h, store, work


def _live_sessions(*cwds: Path):
    sessions = [
        ClaudeSession(pid=100 + i, session_id=f"s{i}", cwd=str(c), started_at=0,
                      kind="interactive", entrypoint="cli")
        for i, c in enumerate(cwds)
    ]
    return patch(
        "claude_swap.process_detection.scan_sessions", return_value=(sessions, 0)
    )


USAGE = {"1": _usage(95), "2": _usage(5), "3": _usage(40)}


def test_autoswitch_keeps_work_session_on_work_account(fleet):
    h, _, work = fleet
    with _live_sessions(work):
        assert h.tick_with_usage(USAGE) is TickOutcome.SWITCHED
    assert h.active_number() == 3  # not 2, despite 2 having more headroom


def test_autoswitch_blocks_when_no_allowed_candidate(fleet):
    h, store, work = fleet
    store.set_account_context("c@acme.test", "", PERSONAL)
    with _live_sessions(work):
        assert h.tick_with_usage(USAGE) is TickOutcome.BLOCKED
    assert h.active_number() == 1
    reasons = [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]
    assert reasons == ["context-barrier"]
    assert str(work) in next(
        e.detail for e in h.events if isinstance(e, NoSwitchEvent)
    )


def test_autoswitch_free_when_sessions_are_personal(fleet, temp_home):
    h, _, _ = fleet
    with _live_sessions(temp_home):
        assert h.tick_with_usage(USAGE) is TickOutcome.SWITCHED
    assert h.active_number() == 2


def test_autoswitch_ignores_barrier_in_open_mode(fleet):
    h, store, work = fleet
    store.set_mode(MODE_OPEN)
    with _live_sessions(work):
        h.tick_with_usage(USAGE)
    assert h.active_number() == 2
    assert any(isinstance(e, SwitchEvent) for e in h.events)


# -- CLI ----------------------------------------------------------------------


class TestContextCli(PolicyStoreBase):
    def test_tag_account_set_dir_and_show(self, temp_home, capsys, monkeypatch):
        s = self._fleet(temp_home, count=2)
        work = temp_home / "work"
        work.mkdir()
        cli._context_command(["account", "2", "work"])
        cli._context_command(["set", "work", str(work)])
        cli._context_command(["mode", "strict"])
        capsys.readouterr()

        cli._context_command(["show", str(work), "--json"])
        shown = json.loads(capsys.readouterr().out)
        assert shown["mode"] == "strict"
        assert shown["directory"]["context"] == "work"
        assert [a["context"] for a in shown["accounts"]] == [None, "work"]

        store = ContextStore(s.backup_dir)
        assert store.account_context("account-2@example.test", "") == WORK

    def test_bare_context_shows_current_directory(self, temp_home, capsys, monkeypatch):
        self._fleet(temp_home, count=1)
        monkeypatch.chdir(temp_home)
        cli._context_command([])
        out = capsys.readouterr().out
        assert "Mode:" in out and "personal" in out and "account-1@example.test" in out

    def test_account_needs_exactly_one_of_context_or_unset(self, temp_home):
        self._fleet(temp_home, count=1)
        with pytest.raises(SystemExit):
            cli._context_command(["account", "1"])
        with pytest.raises(SystemExit):
            cli._context_command(["account", "1", "work", "--unset"])

    def test_unknown_account_is_an_error(self, temp_home, capsys):
        self._fleet(temp_home, count=1)
        with pytest.raises(SystemExit) as exc:
            cli._context_command(["account", "9", "work"])
        assert exc.value.code == 1

    def test_run_refuses_barred_account(self, temp_home, monkeypatch, capsys):
        s = self._fleet(temp_home, count=2)
        work = temp_home / "work"
        work.mkdir()
        ContextStore(s.backup_dir).set_path(work, WORK)
        monkeypatch.chdir(work)
        with patch("claude_swap.session.SessionManager.run") as run:
            with pytest.raises(SystemExit) as exc:
                cli._run_command(["1"])
        assert exc.value.code == 1
        run.assert_not_called()
        assert "not allowed here" in capsys.readouterr().err

    def test_run_allows_tagged_account(self, temp_home, monkeypatch):
        s = self._fleet(temp_home, count=2)
        work = temp_home / "work"
        work.mkdir()
        store = ContextStore(s.backup_dir)
        store.set_path(work, WORK)
        store.set_account_context("account-1@example.test", "", WORK)
        monkeypatch.chdir(work)
        with patch("claude_swap.session.SessionManager.run") as run:
            cli._run_command(["1"])
        run.assert_called_once()

    def test_install_hooks_targets_default_profile(self, temp_home, capsys):
        self._fleet(temp_home, count=1)
        with patch.object(cli, "_hook_command_string", return_value="/x/cswap hook"):
            cli._context_command(["install-hooks"])
        settings = json.loads((temp_home / ".claude" / "settings.json").read_text())
        assert settings["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"] == (
            "/x/cswap hook"
        )
