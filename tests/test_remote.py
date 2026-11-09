"""Remote sync: setup-token accounts shared through an encrypted git repository.

Two simulated machines (separate cswap stores) share one local bare
repository over file://, so every test runs real git with no network.
"""
from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import stat
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import remote as rm
from claude_swap.exceptions import PassphraseError, RemoteError, ValidationError
from claude_swap.switcher import ClaudeAccountSwitcher

PASS = "correct horse battery"
TOKEN_A = "sk-ant-oat01-" + "A" * 30
TOKEN_A2 = "sk-ant-oat01-" + "a" * 30
TOKEN_B = "sk-ant-oat01-" + "B" * 30
TOKEN_C = "sk-ant-oat01-" + "C" * 30

pytestmark = pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="git not available",
)


@pytest.fixture(autouse=True)
def _fast_kdf(monkeypatch):
    # Real scrypt, small cost: the parameters are read back from the manifest.
    monkeypatch.setattr(rm, "KDF_DEFAULTS", {"name": "scrypt", "n": 2**10, "r": 8, "p": 1})


class Machine:
    """One cswap store plus its RemoteSync, as on a separate computer."""

    def __init__(self, root: Path, name: str):
        root.mkdir(parents=True, exist_ok=True)
        with patch.dict(os.environ, {"XDG_DATA_HOME": str(root)}):
            self.s = ClaudeAccountSwitcher()
        self.s._setup_directories()
        self.s._init_sequence_file()
        self.r = rm.RemoteSync(self.s, use_keychain=False, hostname=name)

    def token_account(self, token: str, email: str, alias: str | None = None) -> str:
        with contextlib.redirect_stdout(io.StringIO()):
            self.s.add_account_from_token(token, email=email)
            num = self.s._find_account_slot(self.s._get_sequence_data(), email, "")
            if alias:
                self.s.set_alias(num, alias)
        return num

    def login_account(self, num: int, email: str) -> str:
        """A refreshing login (has a refresh token): never shareable."""
        self.s._write_account_credentials(str(num), email, json.dumps({"claudeAiOauth": {
            "accessToken": f"sk-{num}", "refreshToken": f"rt-{num}"}}))
        self.s._write_account_config(str(num), email, json.dumps({"oauthAccount": {
            "emailAddress": email, "accountUuid": f"uuid-{num}"}}))
        data = self.s._get_sequence_data()
        data["accounts"][str(num)] = {"email": email, "uuid": f"uuid-{num}",
                                      "organizationUuid": "org-1", "organizationName": "Org"}
        data["sequence"].append(num)
        self.s._write_json(self.s.sequence_file, data)
        return str(num)

    def token_of(self, num: str) -> str:
        rec = self.s._get_sequence_data()["accounts"][num]
        creds = self.s._read_account_credentials(num, rec["email"])
        return json.loads(creds)["claudeAiOauth"]["accessToken"]

    def record(self, num: str) -> dict:
        return self.s._get_sequence_data()["accounts"][num]

    def rows(self, name: str = "shared") -> dict[str, rm.Row]:
        return {r.email: r for r in self.r.status(name, PASS).rows}


@pytest.fixture
def bare(tmp_path: Path) -> Path:
    path = tmp_path / "shared.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(path)], check=True)
    return path


@pytest.fixture
def a(tmp_path, temp_home) -> Machine:
    return Machine(tmp_path / "machine-a", "host-a")


@pytest.fixture
def b(tmp_path, temp_home) -> Machine:
    return Machine(tmp_path / "machine-b", "host-b")


@pytest.fixture
def joined(a, b, bare):
    a.r.add("shared", bare.as_uri(), passphrase=PASS)
    b.r.add("shared", bare.as_uri(), passphrase=PASS)
    return a, b


def repo_files(bare: Path) -> list[str]:
    out = subprocess.run(["git", "--git-dir", str(bare), "ls-tree", "-r", "--name-only", "main"],
                         capture_output=True, text=True, check=True).stdout
    return sorted(out.split())


def repo_text(bare: Path) -> str:
    """Everything committed, every revision: tokens must never appear in it."""
    return subprocess.run(["git", "--git-dir", str(bare), "log", "-p", "--all"],
                          capture_output=True, text=True, check=True).stdout


# ---------------------------------------------------------------------------
# crypto and format
# ---------------------------------------------------------------------------


class TestCrypto:
    def test_manifest_opens_with_the_passphrase_only(self):
        manifest, key = rm.new_manifest(PASS)
        assert rm.unlock_manifest(manifest, PASS) == key
        with pytest.raises(PassphraseError):
            rm.unlock_manifest(manifest, PASS + "x")

    def test_short_passphrase_refused(self):
        with pytest.raises(ValidationError):
            rm.new_manifest("short")

    def test_newer_format_is_refused_with_upgrade_hint(self):
        manifest, _ = rm.new_manifest(PASS)
        manifest["format"] = 2
        with pytest.raises(RemoteError, match="Upgrade cswap"):
            rm.unlock_manifest(manifest, PASS)

    @pytest.mark.parametrize("change", [
        {"n": 2**20}, {"n": 1000}, {"r": 64}, {"p": 50}, {"n": True}, {"name": "argon2"},
    ])
    def test_kdf_parameters_from_the_repository_are_bounded(self, change):
        manifest, _ = rm.new_manifest(PASS)
        manifest["kdf"].update(change)
        with pytest.raises(RemoteError):
            rm.unlock_manifest(manifest, PASS)

    def test_entry_is_bound_to_its_file_name(self):
        _, key = rm.new_manifest(PASS)
        eid, other = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
        text = rm.seal_entry(key, rm.Entry(id=eid, email="x@example.com", token=TOKEN_A))
        assert rm.open_entry(key, eid, text).token == TOKEN_A
        with pytest.raises(RemoteError, match="does not decrypt"):
            rm.open_entry(key, other, text)

    def test_tampered_entry_is_rejected(self):
        _, key = rm.new_manifest(PASS)
        eid = "11111111-1111-4111-8111-111111111111"
        blob = json.loads(rm.seal_entry(key, rm.Entry(id=eid, email="x@example.com", token=TOKEN_A)))
        ct = bytearray(base64.b64decode(blob["ciphertext"]))
        ct[0] ^= 1
        blob["ciphertext"] = base64.b64encode(bytes(ct)).decode()
        with pytest.raises(RemoteError):
            rm.open_entry(key, eid, json.dumps(blob))

    def test_entry_contents_are_validated_after_decryption(self):
        _, key = rm.new_manifest(PASS)
        eid = "11111111-1111-4111-8111-111111111111"
        for bad in (rm.Entry(id=eid, email="../../etc@x", token=TOKEN_A),
                    rm.Entry(id=eid, email="x@example.com", token="sk-ant-api03-nope")):
            with pytest.raises(RemoteError):
                rm.open_entry(key, eid, rm.seal_entry(key, bad))

    def test_only_setup_tokens_validate(self):
        assert rm.validate_setup_token(f"  {TOKEN_A}\n") == TOKEN_A
        for bad in ("sk-ant-api03-" + "x" * 30, "hello", ""):
            with pytest.raises(ValidationError):
                rm.validate_setup_token(bad)


# ---------------------------------------------------------------------------
# adding a remote
# ---------------------------------------------------------------------------


class TestAddRemote:
    def test_empty_repository_is_initialized(self, a, bare):
        cfg, created, count = a.r.add("shared", bare.as_uri(), passphrase=PASS)
        assert (created, count) == (True, 0)
        assert repo_files(bare) == ["accounts/.gitkeep", rm.MANIFEST_NAME]
        manifest = json.loads(subprocess.run(
            ["git", "--git-dir", str(bare), "show", f"main:{rm.MANIFEST_NAME}"],
            capture_output=True, text=True, check=True).stdout)
        assert set(manifest) == {"format", "kdf", "check"}
        assert [r.name for r in a.r.remotes()] == ["shared"]
        assert cfg.id in a.r.config_path.read_text()

    def test_a_new_remote_lists_nothing(self, a, bare):
        a.r.add("shared", bare.as_uri(), passphrase=PASS)
        assert a.r.status("shared", PASS).rows == []

    def test_passphrase_is_remembered_outside_the_config(self, a, bare):
        cfg, _, _ = a.r.add("shared", bare.as_uri(), passphrase=PASS)
        assert PASS not in a.r.config_path.read_text()
        assert a.r.stored_passphrase(cfg) == PASS
        secrets = a.r.root / cfg.id / "secrets.json"
        assert stat.S_IMODE(secrets.stat().st_mode) == 0o600

    def test_no_remember_stores_nothing(self, a, bare):
        cfg, _, _ = a.r.add("shared", bare.as_uri(), passphrase=PASS, remember=False)
        assert a.r.stored_passphrase(cfg) is None
        assert not (a.r.root / cfg.id / "secrets.json").exists()

    def test_second_machine_joins_an_existing_remote(self, a, b, bare):
        a.r.add("shared", bare.as_uri(), passphrase=PASS)
        _, created, _ = b.r.add("mine", bare.as_uri(), passphrase=PASS)
        assert created is False

    def test_wrong_passphrase_leaves_nothing_behind(self, a, b, bare):
        a.r.add("shared", bare.as_uri(), passphrase=PASS)
        with pytest.raises(PassphraseError):
            b.r.add("shared", bare.as_uri(), passphrase="wrong passphrase")
        assert b.r.remotes() == []
        assert not b.r.root.exists() or [p.name for p in b.r.root.iterdir()] in ([], [".no-hooks"])

    def test_repository_with_other_files_is_refused(self, a, tmp_path):
        bare = tmp_path / "busy.git"
        work = tmp_path / "busy"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
        subprocess.run(["git", "clone", "-q", str(bare), str(work)], check=True, capture_output=True)
        (work / "README.md").write_text("hi")
        (work / "src.py").write_text("print()")
        git = ["git", "-C", str(work), "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run([*git, "add", "-A"], check=True)
        subprocess.run([*git, "commit", "-qm", "x"], check=True)
        subprocess.run([*git, "push", "-q", "origin", "main"], check=True, capture_output=True)
        with pytest.raises(RemoteError, match="other files"):
            a.r.add("busy", bare.as_uri(), passphrase=PASS)

    def test_readme_only_repository_is_accepted(self, a, tmp_path):
        bare = tmp_path / "readme.git"
        work = tmp_path / "readme"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
        subprocess.run(["git", "clone", "-q", str(bare), str(work)], check=True, capture_output=True)
        (work / "README.md").write_text("hi")
        git = ["git", "-C", str(work), "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run([*git, "add", "-A"], check=True)
        subprocess.run([*git, "commit", "-qm", "x"], check=True)
        subprocess.run([*git, "push", "-q", "origin", "main"], check=True, capture_output=True)
        _, created, _ = a.r.add("readme", bare.as_uri(), passphrase=PASS)
        assert created
        assert "README.md" in repo_files(bare)

    @pytest.mark.parametrize("kwargs, match", [
        ({"name": "bad name"}, "Remote names"),
        ({"url": "-oProxyCommand=x"}, "URL"),
        ({"branch": "../x"}, "branch"),
        ({"auth": "https-token"}, "token is required"),
        ({"auth": "ssh-key", "ssh_key": "/nonexistent/key"}, "not found"),
    ])
    def test_invalid_settings_are_refused(self, a, bare, kwargs, match):
        args = {"name": "shared", "url": bare.as_uri(), "passphrase": PASS, **kwargs}
        with pytest.raises(ValidationError, match=match):
            a.r.add(args.pop("name"), args.pop("url"), **args)

    def test_duplicate_name_refused(self, a, bare):
        a.r.add("shared", bare.as_uri(), passphrase=PASS)
        with pytest.raises(ValidationError, match="already exists"):
            a.r.add("SHARED", bare.as_uri(), passphrase=PASS)

    def test_unreachable_repository_explains(self, a, tmp_path):
        with pytest.raises(RemoteError, match="git fetch failed"):
            a.r.add("gone", (tmp_path / "missing.git").as_uri(), passphrase=PASS)
        assert a.r.remotes() == []


# ---------------------------------------------------------------------------
# status, push, pull
# ---------------------------------------------------------------------------


class TestSync:
    def test_push_then_pull_on_another_machine(self, joined, bare):
        a, b = joined
        num_a = a.token_account(TOKEN_A, "team@example.com", alias="team")
        assert a.rows()["team@example.com"].state == rm.HERE_ONLY

        report = a.r.push("shared", PASS)
        assert report.changed == 1
        assert len([f for f in repo_files(bare) if f.endswith(".enc")]) == 1
        assert TOKEN_A not in repo_text(bare)

        row = b.rows()["team@example.com"]
        assert row.state == rm.REPO_ONLY and row.alias == "team"
        report = b.r.pull("shared", PASS)
        assert report.changed == 1
        num_b = b.s._find_account_slot(b.s._get_sequence_data(), "team@example.com", "")
        assert b.token_of(num_b) == TOKEN_A
        assert b.record(num_b)["alias"] == "team"

        cfg_a, cfg_b = a.r.get("shared"), b.r.get("shared")
        assert a.record(num_a)["remotes"][cfg_a.id] == b.record(num_b)["remotes"][cfg_b.id]
        assert a.rows()["team@example.com"].state == rm.IN_SYNC
        assert b.rows()["team@example.com"].state == rm.IN_SYNC

    def test_refreshing_logins_are_never_shared(self, joined, bare):
        a, _ = joined
        a.login_account(1, "me@example.com")
        assert a.rows()["me@example.com"].state == rm.NOT_SHAREABLE
        report = a.r.push("shared", PASS)
        assert report.changed == 0
        report = a.r.push("shared", PASS, ["1"])
        assert "not shared" in "\n".join(report.lines)
        assert not [f for f in repo_files(bare) if f.endswith(".enc")]

    def test_same_token_on_both_machines_links_on_first_contact(self, joined, bare):
        a, b = joined
        a.token_account(TOKEN_A, "team@example.com")
        a.r.push("shared", PASS)
        num_b = b.token_account(TOKEN_A, "other-label@example.com")

        row = b.rows()["other-label@example.com"]
        assert row.state == rm.IN_SYNC
        assert b.record(num_b)["remotes"][b.r.get("shared").id] == row.entry_id
        assert b.r.push("shared", PASS).changed == 0
        assert len([f for f in repo_files(bare) if f.endswith(".enc")]) == 1

    def test_renewed_token_differs_and_keeps_its_entry(self, joined):
        a, b = joined
        num_a = a.token_account(TOKEN_A, "team@example.com")
        a.r.push("shared", PASS)
        b.r.pull("shared", PASS)
        entry_id = a.rows()["team@example.com"].entry_id

        a.token_account(TOKEN_A2, "team@example.com")  # renewed on machine A
        row = a.rows()["team@example.com"]
        assert row.state == rm.DIFFERS and row.entry_id == entry_id

        asked = []
        report = a.r.push("shared", PASS, confirm=lambda r: asked.append(r) or False)
        assert report.changed == 0 and len(asked) == 1
        report = a.r.push("shared", PASS, confirm=lambda r: True)
        assert report.changed == 1
        assert a.rows()["team@example.com"].state == rm.IN_SYNC
        assert a.rows()["team@example.com"].entry_id == entry_id

        row_b = b.rows()["team@example.com"]
        assert row_b.state == rm.DIFFERS and "host-a" in row_b.note
        num_b = row_b.local_num
        b.r.pull("shared", PASS, confirm=lambda r: False)
        assert b.token_of(num_b) == TOKEN_A
        b.r.pull("shared", PASS, confirm=lambda r: True)
        assert b.token_of(num_b) == TOKEN_A2
        assert a.token_of(num_a) == TOKEN_A2

    def test_push_all_skips_a_label_the_repository_holds_with_another_token(self, joined, bare):
        a, b = joined
        a.token_account(TOKEN_A, "team@example.com")
        a.r.push("shared", PASS)
        num_b = b.token_account(TOKEN_B, "team@example.com")  # before ever syncing

        row = next(r for r in b.r.status("shared", PASS).rows if r.local_num == num_b)
        assert row.state == rm.HERE_ONLY and row.label_taken_by and not row.pushed_by_default
        report = b.r.push("shared", PASS)
        assert report.changed == 0 and "different token labelled" in "\n".join(report.lines)
        assert b.r.push("shared", PASS, [num_b]).changed == 1
        assert len([f for f in repo_files(bare) if f.endswith(".enc")]) == 2

    def test_pull_refuses_to_overwrite_a_same_label_account(self, joined):
        a, b = joined
        a.token_account(TOKEN_A, "team@example.com")
        a.r.push("shared", PASS)
        num_b = b.token_account(TOKEN_B, "team@example.com")

        row = b.rows()["team@example.com"]
        # B's own account is local; the repository's is remote, with a conflict.
        repo_row = next(r for r in b.r.status("shared", PASS).rows if r.state == rm.REPO_ONLY)
        assert repo_row.conflict_num == num_b
        assert row is not None
        report = b.r.pull("shared", PASS)
        assert report.changed == 0 and "already uses that email label" in "\n".join(report.lines)
        assert b.token_of(num_b) == TOKEN_B

        report = b.r.pull("shared", PASS, [repo_row.entry_id[:8]], replace_conflicts=True)
        assert report.changed == 1
        assert b.token_of(num_b) == TOKEN_A

    def test_a_same_email_refreshing_login_with_an_org_is_not_a_conflict(self, joined):
        a, b = joined
        a.token_account(TOKEN_A, "me@example.com")
        a.r.push("shared", PASS)
        login = b.login_account(1, "me@example.com")
        row = next(r for r in b.r.status("shared", PASS).rows if r.state == rm.REPO_ONLY)
        assert row.conflict_num is None
        b.r.pull("shared", PASS)
        assert b.token_of(login) == "sk-1"  # the login is untouched
        nums = [n for n, rec in b.s._get_sequence_data()["accounts"].items()
                if rec["email"] == "me@example.com"]
        assert len(nums) == 2

    def test_two_entries_with_one_label_add_only_one_account(self, joined):
        a, b = joined
        a.r.add_token("shared", PASS, TOKEN_A, email="dup@example.com")
        a.r.add_token("shared", PASS, TOKEN_B, email="dup@example.com")
        report = b.r.pull("shared", PASS)
        assert report.changed == 1
        assert "was just added" in "\n".join(report.lines)

    def test_skipped_entries_are_not_pulled_until_named(self, joined):
        a, b = joined
        a.token_account(TOKEN_A, "team@example.com")
        a.r.push("shared", PASS)
        b.r.set_skipped("shared", PASS, "team@example.com", True)
        assert b.rows()["team@example.com"].state == rm.SKIPPED
        assert b.r.pull("shared", PASS).changed == 0

        assert b.r.pull("shared", PASS, ["team@example.com"]).changed == 1
        assert b.r.get("shared").skipped == []
        assert b.rows()["team@example.com"].state == rm.IN_SYNC

    def test_add_token_goes_to_the_repository_only(self, joined, bare):
        a, b = joined
        entry = a.r.add_token("shared", PASS, TOKEN_C, email="spare@example.com", alias="spare")
        assert a.s._find_account_slot(a.s._get_sequence_data(), "spare@example.com", "") is None
        assert a.rows()["spare@example.com"].state == rm.REPO_ONLY
        assert b.rows()["spare@example.com"].entry_id == entry.id
        with pytest.raises(RemoteError, match="already in the repository"):
            a.r.add_token("shared", PASS, TOKEN_C)
        with pytest.raises(ValidationError):
            a.r.add_token("shared", PASS, "sk-ant-api03-" + "x" * 30)

    def test_entries_resolve_by_label_alias_id_prefix_and_local_part(self, joined):
        a, _ = joined
        e1 = a.r.add_token("shared", PASS, TOKEN_A, email="alpha@example.com", alias="first")
        e2 = a.r.add_token("shared", PASS, TOKEN_B, email="alpha@other.org")
        rows = a.r.status("shared", PASS).rows
        assert a.r._resolve_entry(rows, "first").entry_id == e1.id
        assert a.r._resolve_entry(rows, "ALPHA@other.org").entry_id == e2.id
        assert a.r._resolve_entry(rows, e2.id[:4]).entry_id == e2.id
        with pytest.raises(RemoteError, match="matches 2"):
            a.r._resolve_entry(rows, "alpha")
        with pytest.raises(RemoteError, match="Nothing"):
            a.r._resolve_entry(rows, "nobody")

    def test_add_token_without_a_label_gets_a_placeholder(self, joined):
        a, _ = joined
        entry = a.r.add_token("shared", PASS, TOKEN_C)
        assert entry.email.endswith("@token.local")

    def test_removed_entry_is_not_pushed_back_unless_named(self, joined, bare):
        a, b = joined
        num_a = a.token_account(TOKEN_A, "team@example.com")
        a.r.push("shared", PASS)
        b.r.pull("shared", PASS)

        b.r.remove_entry("shared", PASS, "team@example.com")
        assert not [f for f in repo_files(bare) if f.endswith(".enc")]
        assert b.r.get("shared").id not in b.record(b.rows()["team@example.com"].local_num).get("remotes", {})

        row = a.rows()["team@example.com"]
        assert row.state == rm.HERE_ONLY and row.note == rm._REMOVED_NOTE
        report = a.r.push("shared", PASS)
        assert report.changed == 0 and "removed from the repository" in "\n".join(report.lines)
        assert a.r.push("shared", PASS, [num_a]).changed == 1

    def test_unreadable_entries_are_reported_not_fatal(self, joined, bare, tmp_path):
        a, b = joined
        a.token_account(TOKEN_A, "team@example.com")
        a.r.push("shared", PASS)
        work = tmp_path / "vandal"
        subprocess.run(["git", "clone", "-q", str(bare), str(work)], check=True, capture_output=True)
        (work / "accounts" / "00000000-0000-4000-8000-000000000000.enc").write_text("{}")
        git = ["git", "-C", str(work), "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run([*git, "add", "-A"], check=True)
        subprocess.run([*git, "commit", "-qm", "x"], check=True)
        subprocess.run([*git, "push", "-q", "origin", "main"], check=True, capture_output=True)

        rows = b.r.status("shared", PASS).rows
        assert {r.state for r in rows} == {rm.REPO_ONLY, rm.UNREADABLE}
        assert b.r.pull("shared", PASS).changed == 1
        with pytest.raises(RemoteError, match="cannot be decrypted"):
            b.r.change_passphrase("shared", PASS, "another passphrase")

    def test_push_retries_when_another_machine_pushed_first(self, joined, bare):
        a, b = joined
        a.token_account(TOKEN_A, "a@example.com")
        b.token_account(TOKEN_B, "b@example.com")
        real_sync = rm._Git.sync
        state = {"raced": False}

        def racing_sync(self):
            real_sync(self)
            if self.workdir.is_relative_to(b.r.root) and not state["raced"]:
                state["raced"] = True
                a.r.push("shared", PASS)  # lands between B's fetch and B's push

        with patch.object(rm._Git, "sync", racing_sync):
            report = b.r.push("shared", PASS)
        assert state["raced"] and report.changed == 1
        assert len([f for f in repo_files(bare) if f.endswith(".enc")]) == 2

    def test_an_inherited_git_dir_cannot_redirect_the_reset(self, joined, tmp_path, monkeypatch):
        a, _ = joined
        victim = tmp_path / "victim"
        subprocess.run(["git", "init", "-q", str(victim)], check=True)
        (victim / "precious.txt").write_text("keep me")
        monkeypatch.setenv("GIT_DIR", str(victim / ".git"))
        monkeypatch.setenv("GIT_WORK_TREE", str(victim))
        a.token_account(TOKEN_A, "team@example.com")
        assert a.r.push("shared", PASS).changed == 1
        assert (victim / "precious.txt").read_text() == "keep me"

    def test_wrong_passphrase_on_an_operation(self, joined):
        a, _ = joined
        with pytest.raises(PassphraseError):
            a.r.status("shared", "not the passphrase")


class TestManagement:
    def test_change_passphrase_reencrypts_everything(self, joined, bare):
        a, b = joined
        a.token_account(TOKEN_A, "team@example.com")
        a.r.push("shared", PASS)
        before = repo_files(bare)
        assert a.r.change_passphrase("shared", PASS, "a new passphrase") == 1
        assert repo_files(bare) == before
        assert a.r.stored_passphrase(a.r.get("shared")) == "a new passphrase"
        with pytest.raises(PassphraseError):
            b.r.status("shared", PASS)
        rows = b.r.status("shared", "a new passphrase").rows
        assert rows[0].state == rm.REPO_ONLY and rows[0].entry.token == TOKEN_A

    def test_remove_forgets_locally_and_leaves_the_repository(self, joined, bare):
        a, _ = joined
        num = a.token_account(TOKEN_A, "team@example.com")
        a.r.push("shared", PASS)
        cfg = a.r.get("shared")
        files = repo_files(bare)
        a.r.remove("shared")
        assert a.r.remotes() == []
        assert not (a.r.root / cfg.id).exists()
        assert "remotes" not in a.record(num)
        assert repo_files(bare) == files

    def test_links_survive_a_token_refresh_in_place(self, joined):
        a, _ = joined
        num = a.token_account(TOKEN_A, "team@example.com")
        a.r.push("shared", PASS)
        link = a.record(num)["remotes"]
        a.token_account(TOKEN_A2, "team@example.com")
        assert a.record(num)["remotes"] == link

    def test_update_changes_the_url(self, joined, tmp_path, bare):
        a, _ = joined
        moved = tmp_path / "moved.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(bare), str(moved)],
                       check=True, capture_output=True)
        a.r.update("shared", url=moved.as_uri())
        a.token_account(TOKEN_A, "team@example.com")
        a.r.push("shared", PASS)
        assert [f for f in repo_files(moved) if f.endswith(".enc")]
        assert not [f for f in repo_files(bare) if f.endswith(".enc")]


class TestGitAuth:
    def _git(self, tmp_path, **cfg) -> rm._Git:
        config = rm.RemoteConfig(id="11111111-1111-4111-8111-111111111111",
                                 name="x", url="https://example.invalid/r.git", **cfg)
        return rm._Git(tmp_path, config, "s3cret-token", tmp_path / "hooks")

    def test_https_token_reaches_git_only_through_the_environment(self, tmp_path):
        g = self._git(tmp_path, auth="https-token", username="me")
        cmd, env = g._cmd(("fetch",)), g._env()
        assert not any("s3cret-token" in part for part in cmd)
        assert env["CSWAP_GIT_PASSWORD"] == "s3cret-token" and env["CSWAP_GIT_USERNAME"] == "me"
        assert cmd.count("-c") >= 2 and "credential.helper=" in cmd

    def test_token_is_masked_in_errors(self, tmp_path):
        g = self._git(tmp_path, auth="https-token")
        msg = g._explain("fetch", "fatal: https://me:s3cret-token@host: Authentication failed")
        assert "s3cret-token" not in msg and "refused" in msg

    def test_ssh_key_is_used_alone_and_never_prompts(self, tmp_path):
        g = self._git(tmp_path, auth="ssh-key", ssh_key="/home/u/.ssh/id deploy")
        ssh = g._env()["GIT_SSH_COMMAND"]
        assert "BatchMode=yes" in ssh and "IdentitiesOnly=yes" in ssh
        assert "'/home/u/.ssh/id deploy'" in ssh

    def test_repository_variables_from_the_environment_are_dropped(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GIT_DIR", "/home/u/project/.git")
        monkeypatch.setenv("GIT_WORK_TREE", "/home/u/project")
        monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -F /home/u/.ssh/alt")
        env = self._git(tmp_path)._env()
        assert "GIT_DIR" not in env and "GIT_WORK_TREE" not in env
        assert env["GIT_SSH_COMMAND"] == "ssh -F /home/u/.ssh/alt"

    def test_hooks_are_disabled(self, tmp_path):
        cmd = self._git(tmp_path)._cmd(("commit",))
        assert f"core.hooksPath={tmp_path / 'hooks'}" in cmd
