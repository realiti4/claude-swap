"""Shared messages name the provider they run for.

Paths shared by both providers word their hints through the switcher
(``display_name``, ``short_name``, ``cli_prefix``, ``relogin_hint``), so a
Codex run names Codex and ``cswap codex …`` while Claude's text stays
byte-for-byte what it was. A Codex re-login hint is always ``cswap codex add
--login``: a ``codex login`` in the live home would revoke the stored account
it replaces.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_swap.autoswitch import AutoSwitchEngine, NoSwitchEvent, QuarantineEvent
from claude_swap.codex_switcher import CodexAccountSwitcher
from claude_swap.json_output import USAGE_TOKEN_EXPIRED
from claude_swap.models import Platform
from claude_swap.settings import AutoSwitchSettings
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.usage_store import UsageEntry


def _codex() -> CodexAccountSwitcher:
    s = CodexAccountSwitcher()
    s.platform = Platform.LINUX
    return s


def _engine(switcher, events: list) -> AutoSwitchEngine:
    return AutoSwitchEngine(switcher, AutoSwitchSettings(), events.append)


def _detail(events: list, reason: str) -> str:
    return next(e.detail for e in events if isinstance(e, NoSwitchEvent) and e.reason == reason)


class TestReloginHint:
    def test_claude_text_comes_back_verbatim(self, temp_home: Path):
        text = "log in as it and run: cswap add --slot 2"
        assert ClaudeAccountSwitcher().relogin_hint(text, 2, "run: {cmd}") == text

    def test_codex_always_signs_in_through_cswap(self, codex_home: Path):
        s = _codex()
        assert s.relogin_hint("cswap --add-account --slot 3", 3) == (
            "cswap codex add --login --slot 3"
        )
        assert s.relogin_hint("log in and run: cswap add", via_login="run: {cmd}") == (
            "run: cswap codex add --login"
        )


class TestAutoSwitchStrings:
    def test_quarantine_points_codex_at_add_login(self, codex_home: Path):
        events: list = []
        _engine(_codex(), events)._quarantine("2", "x@example.com", "invalid_grant")
        (event,) = [e for e in events if isinstance(e, QuarantineEvent)]
        assert event.human() == (
            "Account-2 (x@example.com) quarantined: invalid_grant. "
            "Run 'cswap codex add --login --slot 2' to recover."
        )
        assert "recovery" not in event.to_json()

    def test_claude_quarantine_text_is_unchanged(self, temp_home: Path):
        events: list = []
        _engine(ClaudeAccountSwitcher(), events)._quarantine("2", "x@example.com", "invalid_grant")
        (event,) = [e for e in events if isinstance(e, QuarantineEvent)]
        assert event.human() == (
            "Account-2 (x@example.com) quarantined: invalid_grant. "
            "Log in with it and run 'cswap --add-account --slot 2' to recover."
        )

    def test_unmanaged_and_missing_logins_name_cswap_codex(self, codex_home: Path):
        events: list = []
        engine = _engine(_codex(), events)
        engine.tick()  # the live login is not managed yet
        assert _detail(events, "unmanaged-active-account") == (
            "run 'cswap codex --add-account' to include it in rotation"
        )
        (codex_home / "auth.json").unlink()
        engine.tick()
        assert _detail(events, "no-active-account") == (
            "log in and run 'cswap codex --add-account' first"
        )

    def test_idle_hold_names_codex(self, codex_home: Path, monkeypatch):
        s = _codex()
        s.add_account()
        monkeypatch.setattr(
            s, "usage_entries_by_account",
            lambda **_: {"1": UsageEntry(sentinel=USAGE_TOKEN_EXPIRED)},
        )
        events: list = []
        _engine(s, events).tick()
        assert _detail(events, "active-idle") == (
            "token expired while Codex is idle; resumes on next use"
        )

    @pytest.mark.parametrize("make,prefix", [
        (lambda: ClaudeAccountSwitcher(), "cswap"),
        (_codex, "cswap codex"),
    ])
    def test_stash_unreadable_names_the_providers_unclaimed(
        self, codex_home: Path, monkeypatch, make, prefix
    ):
        from claude_swap import autoswitch

        msg = autoswitch._systemic_message("stash-unreadable", make())
        assert f"`{prefix} unclaimed` inspects it" in msg


PERSONAL = dict(email="user@example.com", account_id="acct-personal", user_id="user-1", plan="plus")
TEAM = dict(email="user@example.com", account_id="acct-team", user_id="user-1", plan="team")
OTHER = dict(email="other@example.com", account_id="acct-other", user_id="user-2", plan="pro")


def _add(s: CodexAccountSwitcher, codex_home: Path, **kw) -> str:
    from tests import codex_fixtures as cf

    text = cf.auth_json(**kw)
    (codex_home / "auth.json").write_text(text, encoding="utf-8")
    s.add_account()
    return text


class TestSwitcherMessages:
    def test_missing_backup_points_at_the_providers_relogin(
        self, codex_home: Path, temp_home: Path
    ):
        from claude_swap.exceptions import SwitchError

        with pytest.raises(SwitchError) as codex:
            _codex()._read_target_credentials("7", "nobody@example.com")
        assert str(codex.value).endswith(
            "Re-add with: cswap codex add --login --slot 7"
        )
        with pytest.raises(SwitchError) as claude:
            ClaudeAccountSwitcher()._read_target_credentials("7", "nobody@example.com")
        assert str(claude.value).endswith("Re-add with: cswap --add-account --slot 7")

    def test_session_drift_warning_names_codex(self, codex_home: Path, monkeypatch):
        from types import SimpleNamespace

        s = _codex()
        _add(s, codex_home, **OTHER)
        _add(s, codex_home, **PERSONAL)  # slot 2 is live
        monkeypatch.setattr(
            s, "_scan_live_sessions", lambda d: ([SimpleNamespace(pid=42)], 0)
        )
        monkeypatch.setattr(s, "_session_profile_ahead", lambda *a: False)
        result = s.switch_to("1", json_output=True)
        (msg,) = [w for w in result["warnings"] if "session-mode" in w]
        assert "a live session-mode Codex instance (PID 42)" in msg
        assert msg.endswith("re-run 'cswap codex run 1'.")

    @pytest.mark.parametrize("scan,needle", [
        (([42], 0), "Live session-mode Codex instance(s) found: 1-user (PID 42)."),
        (([], 1), "Whether a Codex instance is live cannot be determined"),
    ])
    def test_purge_refusals_name_codex(self, codex_home: Path, monkeypatch, scan, needle):
        from types import SimpleNamespace

        from claude_swap.exceptions import SessionError

        s = _codex()
        (s.backup_dir / "sessions" / "1-user").mkdir(parents=True)
        pids, unreadable = scan
        monkeypatch.setattr(
            s, "_scan_live_sessions",
            lambda d: ([SimpleNamespace(pid=p) for p in pids], unreadable),
        )
        with pytest.raises(SessionError, match=needle.replace("(", r"\(").replace(")", r"\)")):
            s.purge()

    def test_duplicate_credential_warning_points_at_add_login(self, codex_home: Path):
        creds = (codex_home / "auth.json").read_text()
        rows = [(1, "a@x.com", "", "o1", True, creds, ""), (2, "b@x.com", "", "o2", False, creds, "")]
        (msg,) = _codex()._duplicate_account_warnings(rows)
        assert msg.endswith("Re-add the missing account with: cswap codex add --login --slot N")
        assert "Log in with" not in msg

    def test_ambiguous_email_suggests_cswap_codex(self, codex_home: Path):
        from claude_swap.exceptions import ConfigError

        s = _codex()
        _add(s, codex_home, **PERSONAL)
        _add(s, codex_home, **TEAM)
        with pytest.raises(ConfigError, match=r"e\.g\., cswap codex --switch-to 1"):
            s.resolve_account("user@example.com")

    def test_rotation_empty_names_cswap_codex_enable(self, codex_home: Path, capsys):
        s = _codex()
        _add(s, codex_home, **PERSONAL)
        s.set_account_disabled("1", True)
        assert "Re-enable one with cswap codex enable <num|email>." in capsys.readouterr().out


    def test_a_login_vanishing_mid_add_is_worded_for_codex(
        self, codex_home: Path, monkeypatch
    ):
        from claude_swap.exceptions import ConfigError

        s = _codex()
        monkeypatch.setattr(s, "_snapshot_live_config", lambda: None)
        with pytest.raises(ConfigError, match="^No live Codex login found$"):
            s.add_account()


class TestTransferMessages:
    def test_import_names_the_codex_api_key_format(self, codex_home: Path, tmp_path: Path):
        import json

        from claude_swap.exceptions import TransferError
        from claude_swap.transfer import export_accounts, import_accounts

        s = _codex()
        s.add_account_from_token("sk-proj-abc", slot=1)
        path = tmp_path / "out.json"
        export_accounts(s, str(path))
        doc = json.loads(path.read_text())
        doc["accounts"][0]["credentials"] = "sk-ant-api03-not-codex"
        path.write_text(json.dumps(doc))
        with pytest.raises(TransferError, match="must be a Codex API-key auth.json string"):
            import_accounts(s, str(path), force=True)

    def test_export_skip_points_at_add_login(self, codex_home: Path, tmp_path: Path, capsys):
        from claude_swap.transfer import export_accounts

        s = _codex()
        _add(s, codex_home, **OTHER)
        _add(s, codex_home, **PERSONAL)  # slot 1 is no longer live
        s._delete_account_credentials("1", OTHER["email"])
        export_accounts(s, str(tmp_path / "out.json"))
        assert (
            "re-add with: cswap codex add --login --slot 1" in capsys.readouterr().err
        )
