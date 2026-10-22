"""Settings switch transactions preserve both user settings and saved OAuth."""

import json
import sys

import pytest

from claude_swap import gateway
from claude_swap import cli
from claude_swap.exceptions import ConfigError, SessionError, SwitchError, ValidationError
from claude_swap.paths import get_credentials_path, get_global_config_path
from tests.test_gateway_accounts import KEY
from tests.test_gateway_accounts import gateway_switcher as gateway_switcher


def seed_oauth(s):
    s.add_account_from_token("sk-ant-oat01-dummy", slot=1)
    s.switch_to("1", json_output=True)
    s.add_account_from_token(KEY, base_url="https://gateway.example", slot=2)


class TestGatewayActivation:
    def test_gateway_json_status_keeps_kind_with_captured_metadata(self, gateway_switcher, temp_home, monkeypatch):
        from claude_swap.transfer import export_accounts, import_accounts
        from claude_swap.usage_store import UsageEntry

        s = gateway_switcher
        seed_oauth(s)
        path = temp_home / "status-kind-replacement.json"
        export_accounts(s, str(path), account="1")
        envelope = json.loads(path.read_text())
        envelope["accounts"][0].update(kind="gateway", credentials=KEY, baseUrl="https://replacement.example", auth="bearer", probeModel=gateway.DEFAULT_MODEL)
        path.write_text(json.dumps(envelope))
        def race(*args, **kwargs):
            import_accounts(s, str(path), force=True)
            return UsageEntry()
        monkeypatch.setattr(s, "_active_account_usage", race)
        result = s.status(json_output=True)
        assert s.account_kind_for("1") == "gateway"
        assert result["active"]["email"] == s.account_email("1")
        assert "gateway" not in result["active"]
        assert result["active"].get("kind") != "gateway"

    @pytest.mark.parametrize("json_output", [False, True])
    def test_gateway_status_rechecks_active_slot_before_live_capture(self, gateway_switcher, monkeypatch, capsys, json_output):
        from claude_swap import oauth

        s = gateway_switcher
        seed_oauth(s)
        s.add_account_from_token("sk-ant-oat01-second", slot=3)
        for num in ("1", "3"):
            value = json.dumps({"claudeAiOauth": {"accessToken": "access-" + num, "refreshToken": "refresh-" + num, "expiresAt": 9999999999999}})
            s._write_account_credentials(num, s.account_email(num), value)
        get_credentials_path().write_text(s._read_account_credentials("1", s.account_email("1")))
        requests = []
        monkeypatch.setattr(oauth, "try_fetch_usage_for_account", lambda *args, **kwargs: requests.append(True) or oauth.UsageOutcome({"five_hour": {"pct": 10}}))
        usage = s._active_account_usage
        def race(*args, **kwargs):
            s.switch_to("3", json_output=True)
            return usage(*args, **kwargs)
        monkeypatch.setattr(s, "_active_account_usage", race)
        s.status(json_output=json_output)
        capsys.readouterr()
        assert s.current_account_number() == "3"
        assert requests == []
        assert "lastGood" not in s._usage_store._read_rows().get("1", {})
        assert json.loads(get_credentials_path().read_text())["claudeAiOauth"]["refreshToken"] == "refresh-3"

    @pytest.mark.parametrize("json_output", [False, True])
    @pytest.mark.parametrize("origin", ["oauth", "gateway"])
    def test_gateway_status_pins_captured_authentication(self, gateway_switcher, temp_home, monkeypatch, capsys, json_output, origin):
        from claude_swap import oauth
        from claude_swap.transfer import export_accounts, import_accounts

        s = gateway_switcher
        seed_oauth(s)
        num = "2" if origin == "gateway" else "1"
        email = s.account_email(num)
        captured = json.dumps({"claudeAiOauth": {"accessToken": "captured-access", "refreshToken": "captured-refresh", "expiresAt": 9999999999999}})
        s._write_account_credentials(num, email, captured)
        if origin == "gateway":
            s.switch_to(num, json_output=True)
        else:
            get_credentials_path().write_text(captured)
        path = temp_home / "status-replacement.json"
        export_accounts(s, str(path), account=num)
        envelope = json.loads(path.read_text())
        envelope["accounts"][0].update(kind="oauth", credentials={"claudeAiOauth": {"accessToken": "replacement-access", "refreshToken": "replacement-refresh", "expiresAt": 9999999999999}})
        path.write_text(json.dumps(envelope))
        requests, replaced = [], []
        monkeypatch.setattr(oauth, "try_fetch_usage_for_account", lambda *args, **kwargs: requests.append(True) or oauth.UsageOutcome({"five_hour": {"pct": 10}}))
        monkeypatch.setattr(oauth, "fetch_oauth_profile", lambda *args: {"uuid": "self-uuid", "email": email, "organizationUuid": ""})
        collect = s._collect_usage_entries
        def race(*args, **kwargs):
            if not replaced:
                replaced.append(True)
                if origin == "gateway":
                    s.switch_to("1", json_output=True)
                import_accounts(s, str(path), force=True)
            return collect(*args, **kwargs)
        monkeypatch.setattr(s, "_collect_usage_entries", race)
        s.status(json_output=json_output)
        capsys.readouterr()
        assert replaced
        assert requests == []
        assert "lastGood" not in s._usage_store._read_rows().get(num, {})
        assert json.loads(s._read_account_credentials(num, email))["claudeAiOauth"]["refreshToken"] == "replacement-refresh"

    def test_gateway_usage_routing_keeps_captured_active_generation(self, gateway_switcher, temp_home, monkeypatch):
        from claude_swap import oauth
        from claude_swap.transfer import export_accounts, import_accounts

        s = gateway_switcher
        seed_oauth(s)
        email = s.account_email("1")
        old = json.dumps({"claudeAiOauth": {"accessToken": "live-access", "refreshToken": "live-refresh", "expiresAt": 9999999999999}})
        s._write_account_credentials("1", email, old)
        get_credentials_path().write_text(old)
        info = next(i for i in s._build_accounts_info() if i[0] == 1)
        path = temp_home / "active-routing-replacement.json"
        export_accounts(s, str(path), account="1")
        envelope = json.loads(path.read_text())
        envelope["accounts"][0]["credentials"] = {"claudeAiOauth": {"accessToken": "replacement-access", "refreshToken": "replacement-refresh", "expiresAt": 1000}}
        path.write_text(json.dumps(envelope))
        requests = []
        monkeypatch.setattr(oauth, "try_fetch_usage_for_account", lambda *args, **kwargs: requests.append(True) or oauth.UsageOutcome({"five_hour": {"pct": 10}}))
        monkeypatch.setattr(oauth, "fetch_oauth_profile", lambda *args: {"uuid": "self-uuid", "email": email, "organizationUuid": ""})
        fetch = s._fetch_active_usage
        def race(*args, **kwargs):
            import_accounts(s, str(path), force=True)
            return fetch(*args, **kwargs)
        monkeypatch.setattr(s, "_fetch_active_usage", race)
        s._fetch_account_usage(info)
        assert requests == []
        assert json.loads(s._read_account_credentials("1", email))["claudeAiOauth"]["refreshToken"] == "replacement-refresh"

    @pytest.mark.parametrize("operation", ["adopt", "consume"])
    def test_gateway_rejects_stale_oauth_persistence_entry(self, gateway_switcher, monkeypatch, operation):
        from claude_swap import oauth

        s = gateway_switcher
        key = json.dumps({"claudeAiOauth": {"accessToken": "opaque-access", "refreshToken": "opaque-refresh", "expiresAt": 1000}})
        s.add_account_from_token(key, slot=1, base_url="https://gateway.example")
        email = s.account_email("1")
        def unexpected(*args, **kwargs):
            pytest.fail("Gateway credential reached an OAuth persistence path")
        monkeypatch.setattr(oauth, "try_refresh_oauth_credentials", unexpected)
        monkeypatch.setattr(s, "_session_profile_ahead", unexpected)
        if operation == "adopt":
            assert s._adopt_session_credential("1", email, "") is None
        else:
            assert s.consume_backup_grant("1", email, key).error == "transient"
        assert s._read_account_credentials("1", email) == key

    @pytest.mark.parametrize("phase", ["usage", "profile", "pre-lock"])
    @pytest.mark.parametrize("replacement_kind", ["gateway", "oauth"])
    def test_gateway_replacement_survives_inflight_oauth_collection(self, gateway_switcher, temp_home, monkeypatch, phase, replacement_kind):
        from claude_swap import oauth
        from claude_swap.transfer import export_accounts, import_accounts

        s = gateway_switcher
        seed_oauth(s)
        email = s.account_email("1")
        old = json.dumps({"claudeAiOauth": {"accessToken": "old-access", "refreshToken": "old-refresh", "expiresAt": 1000}})
        live = json.dumps({"claudeAiOauth": {"accessToken": "live-access", "refreshToken": "live-refresh", "expiresAt": 9999999999999}})
        s._write_account_credentials("1", email, old)
        get_credentials_path().write_text(old if phase == "pre-lock" else live)
        path = temp_home / "concurrent.json"
        export_accounts(s, str(path), account="1")
        envelope = json.loads(path.read_text())
        row = envelope["accounts"][0]
        if replacement_kind == "gateway":
            row.update(kind="gateway", credentials=KEY, baseUrl="https://replacement.example", auth="bearer", probeModel=gateway.DEFAULT_MODEL)
        else:
            row["credentials"] = {"claudeAiOauth": {"accessToken": "imported-access", "refreshToken": "imported-refresh", "expiresAt": 1000}}
        path.write_text(json.dumps(envelope))
        replacement = []
        def replace_slot():
            if not replacement:
                replacement.append(True)
                import_accounts(s, str(path), force=True)
                replacement.append(s._read_account_credentials("1", email))
        def usage(*args, **kwargs):
            if phase == "usage":
                replace_slot()
            return oauth.UsageOutcome({"five_hour": {"pct": 10}})
        def profile(*args):
            if phase == "profile":
                replace_slot()
            return {"uuid": "self-uuid", "email": email, "organizationUuid": ""}
        monkeypatch.setattr(oauth, "try_fetch_usage_for_account", usage)
        monkeypatch.setattr(oauth, "fetch_oauth_profile", profile)
        refreshes = []
        def refresh(*args, **kwargs):
            refreshes.append(True)
            return oauth.RefreshOutcome(live, None)
        monkeypatch.setattr(oauth, "try_refresh_oauth_credentials", refresh)
        if phase == "pre-lock":
            read = s._read_account_credentials
            def read_then_replace(num, address):
                value = read(num, address)
                if num == "1":
                    replace_slot()
                return value
            monkeypatch.setattr(s, "_read_account_credentials", read_then_replace)
        s._fetch_active_usage("1", email, old if phase == "pre-lock" else live)
        assert replacement
        assert s._read_account_credentials("1", email) == replacement[1]
        assert not refreshes

    @pytest.mark.parametrize("key_shape", ["raw", "oauth-shaped"])
    def test_gateway_replacement_during_refresh_stashes_successor(self, gateway_switcher, temp_home, monkeypatch, key_shape):
        from claude_swap import oauth
        from claude_swap.transfer import export_accounts, import_accounts

        s = gateway_switcher
        seed_oauth(s)
        email = s.account_email("1")
        old = json.dumps({"claudeAiOauth": {"accessToken": "old-access", "refreshToken": "old-refresh", "expiresAt": 1000}})
        successor = json.dumps({"claudeAiOauth": {"accessToken": "successor-access", "refreshToken": "successor-refresh", "expiresAt": 9999999999999}})
        s._write_account_credentials("1", email, old)
        path = temp_home / "during-refresh.json"
        export_accounts(s, str(path), account="1")
        envelope = json.loads(path.read_text())
        key = KEY if key_shape == "raw" else old
        envelope["accounts"][0].update(kind="gateway", credentials=key, baseUrl="https://replacement.example", auth="bearer", probeModel=gateway.DEFAULT_MODEL)
        path.write_text(json.dumps(envelope))
        def refresh(*args, **kwargs):
            import_accounts(s, str(path), force=True)
            return oauth.RefreshOutcome(successor, None)
        monkeypatch.setattr(oauth, "try_refresh_oauth_credentials", refresh)
        result = s.consume_backup_grant("1", email, old)
        assert s._read_account_credentials("1", email) == key
        assert result.error == "transient" and result.stashed
        assert any(s._store._read_unclaimed_credential(i) == (successor, False) for i in s.list_unclaimed_credentials())

    @pytest.mark.parametrize("operation", ["import", "add-token"])
    def test_gateway_replacement_refuses_unreadable_retained_live_before_writes(self, gateway_switcher, temp_home, monkeypatch, operation):
        from claude_swap.exceptions import CredentialReadError
        from claude_swap.transfer import export_accounts, import_accounts

        s = gateway_switcher
        seed_oauth(s)
        s.switch_to("2", json_output=True)
        email = s.account_email("1")
        path = temp_home / "replacement.json"
        export_accounts(s, str(path), account="1")
        envelope = json.loads(path.read_text())
        envelope["accounts"][0]["credentials"]["claudeAiOauth"]["accessToken"] = "replacement-access"
        path.write_text(json.dumps(envelope))
        before = s.sequence_file.read_bytes(), s._read_account_credentials("1", email)
        monkeypatch.setattr(s, "_read_credentials", lambda: None)
        with pytest.raises(CredentialReadError):
            if operation == "import":
                import_accounts(s, str(path), force=True)
            else:
                s.add_account_from_token("sk-ant-oat01-replacement", email=email)
        assert (s.sequence_file.read_bytes(), s._read_account_credentials("1", email)) == before
        assert not s._account_write_pending("1")

    @pytest.mark.parametrize("expiry,mode", [(1000, "direct"), (2000, "direct"), (None, "direct"),
        (1000, "via-gateway"), (None, "refresh"), (2000, "move"), (None, "swap"),
        (None, "stash-failure"), (None, "publication-failure")])
    def test_gateway_return_preserves_explicit_oauth_replacement(self, gateway_switcher, temp_home, monkeypatch, expiry, mode):
        from claude_swap import oauth
        from claude_swap.exceptions import ClaudeSwitchError
        from claude_swap.models import Platform
        from claude_swap.switcher import ClaudeAccountSwitcher
        from claude_swap.transfer import export_accounts, import_accounts

        s = gateway_switcher
        seed_oauth(s)
        s.add_account_from_token(KEY + "-b", slot=3, base_url="https://b.example")
        s.add_account_from_token("sk-ant-oat01-other", slot=4)
        email = s.account_email("1")
        old = json.dumps({"claudeAiOauth": {"accessToken": "old-access", "refreshToken": "old-refresh", "expiresAt": 2000}})
        s._write_account_credentials("1", email, old)
        s.switch_to("1", force=True, json_output=True)
        s.switch_to("2", json_output=True)
        monkeypatch.setattr(oauth, "fetch_oauth_profile", lambda *args: None)
        path = temp_home / "replacement.json"
        export_accounts(s, str(path), account="1")
        envelope = json.loads(path.read_text())
        token = {"accessToken": "replacement-access", "refreshToken": "replacement-refresh"}
        if expiry is not None:
            token["expiresAt"] = expiry
        envelope["accounts"][0]["credentials"] = {"claudeAiOauth": token}
        path.write_text(json.dumps(envelope))
        if mode == "publication-failure":
            write = s._write_json
            def fail_receipt(target, data):
                if target == s.sequence_file and any(r.get("gatewayExplicitReplacement") for r in data.get("accounts", {}).values()):
                    raise OSError("injected receipt failure")
                return write(target, data)
            with monkeypatch.context() as patch:
                patch.setattr(s, "_write_json", fail_receipt)
                with pytest.raises(ClaudeSwitchError):
                    import_accounts(s, str(path), force=True)
            assert s._read_account_credentials("1", email) == ""
            assert s.gateway_credential("2") == KEY
        import_accounts(s, str(path), force=True)
        target = "1"
        if mode == "via-gateway":
            s.switch_to("3", json_output=True)
        elif mode == "refresh":
            token.update(accessToken="descendant-access", refreshToken="descendant-refresh")
            s._write_account_credentials("1", email, json.dumps({"claudeAiOauth": token}))
        elif mode == "move":
            s.move_account("1", "5")
            target = "5"
        elif mode == "swap":
            s.swap_accounts("1", "4")
            target = "4"
        fresh = ClaudeAccountSwitcher()
        fresh.platform = Platform.LINUX
        replacement = fresh._read_account_credentials(target, email)
        if mode == "stash-failure":
            settings = fresh._gateway_settings_path().read_bytes()
            def fail_stash(*args):
                raise OSError("injected stash failure")
            with monkeypatch.context() as patch:
                patch.setattr(fresh, "_stash_live_credential", fail_stash)
                with pytest.raises(SwitchError):
                    fresh.switch_to(target, json_output=True)
            assert fresh._gateway_settings_path().read_bytes() == settings
            assert fresh._read_account_credentials(target, email) == replacement
        fresh.switch_to(target, json_output=True)
        assert json.loads(fresh._read_credentials())["claudeAiOauth"] == token
        assert fresh._read_account_credentials(target, email) == replacement
        assert any(fresh._store._read_unclaimed_credential(i) == (old, False) for i in fresh.list_unclaimed_credentials())
        assert not any(r.get("gatewayExplicitReplacement") for r in fresh._get_sequence_data()["accounts"].values())

    @pytest.mark.parametrize("swap", [False, True])
    @pytest.mark.parametrize("reimport", [False, True])
    def test_gateway_return_keeps_unreplaced_live_rotation(self, gateway_switcher, temp_home, monkeypatch, swap, reimport):
        from claude_swap import oauth
        from claude_swap.transfer import export_accounts, import_accounts

        s = gateway_switcher
        seed_oauth(s)
        s.add_account_from_token("sk-ant-oat01-other", slot=4)
        email = s.account_email("1")
        old = json.dumps({"claudeAiOauth": {"accessToken": "old-access", "refreshToken": "old-refresh", "expiresAt": 2000}})
        s._write_account_credentials("1", email, old)
        s.switch_to("1", force=True, json_output=True)
        s.switch_to("2", json_output=True)
        if reimport:
            path = temp_home / "unchanged.json"
            export_accounts(s, str(path), account="1")
            import_accounts(s, str(path), force=True)
        rotated = json.dumps({"claudeAiOauth": {"accessToken": "rotated-access", "refreshToken": "rotated-refresh", "expiresAt": 9000}})
        get_credentials_path().write_text(rotated)
        monkeypatch.setattr(oauth, "fetch_oauth_profile", lambda *args: None)
        if swap:
            s.swap_accounts("1", "4")
        target = "4" if swap else "1"
        s.switch_to(target, json_output=True)
        assert s._read_account_credentials(target, email) == rotated

    @pytest.mark.parametrize("token", ["sk-ant-api03-dummy", "sk-ant-oat01-dummy"])
    def test_gateway_return_keeps_replaced_nonrefreshable_credential(self, gateway_switcher, token):
        s = gateway_switcher
        email = "same@example.com"
        s.add_account_from_token(token, email=email, slot=1)
        s.switch_to("1", json_output=True)
        old_live = s._read_credentials()
        s.add_account_from_token(KEY, slot=2, base_url="https://gateway.example")
        s.switch_to("2", json_output=True)
        s.add_account_from_token(token + "-replacement", email=email)
        replacement = s._read_account_credentials("1", email)
        s.switch_to("1", json_output=True)
        assert s._read_account_credentials("1", email) == replacement
        assert s._read_credentials() == replacement
        assert old_live != replacement
        stash = s.list_unclaimed_credentials()
        assert any(s._store._read_unclaimed_credential(entry_id) == (old_live, False)
                   for entry_id in stash)

    @pytest.mark.parametrize("force", [False, True])
    def test_gateway_identity_match_does_not_replace_settings_activation(self, gateway_switcher, force):
        s = gateway_switcher
        seed_oauth(s)
        email = s.account_email("1")
        live = s._read_credentials()
        s.remove_account("1", assume_yes=True)
        s.add_account_from_token(KEY, base_url="https://gateway.example", slot=3, email=email)
        assert s.current_account_number() is None
        assert s.status(json_output=True)["active"] == {"email": email, "managed": False}
        assert not any(info[4] for info in s._build_accounts_info())
        assert s.switch(json_output=True)["reason"] == "unmanaged-account"
        result = s.switch_to("3", json_output=True, force=force)
        assert result["switched"] and result["from"]["number"] is None
        assert s.current_account_number() == "3"
        assert s.gateway_credential("3") == KEY
        assert s._read_credentials() == live
        assert s.switch_to("3", json_output=True)["reason"] == "already-active"

    def test_live_add_refuses_oauth_refresh_of_inactive_gateway(self, gateway_switcher, monkeypatch):
        s = gateway_switcher
        seed_oauth(s)
        email = s.account_email("1")
        s.remove_account("1", assume_yes=True)
        s.add_account_from_token(KEY, base_url="https://gateway.example", slot=3, email=email)
        monkeypatch.setattr(s, "_reject_foreign_credential_capture", lambda creds, *args: creds)
        monkeypatch.setattr(s, "_reject_credential_drift_since_verify", lambda *args: None)
        monkeypatch.setattr(s, "_reject_identity_drift_since_verify", lambda *args: None)
        sequence = s.sequence_file.read_bytes()
        with pytest.raises(ValidationError, match="gateway"):
            s.add_account()
        assert s.sequence_file.read_bytes() == sequence
        assert s._read_account_credentials("3", email) == KEY

    def test_live_add_refuses_replacing_active_gateway(self, gateway_switcher):
        s = gateway_switcher
        seed_oauth(s)
        s.switch_to("2", json_output=True)
        with pytest.raises(ConfigError, match="Switch away from the active gateway"):
            s.add_account(slot=2, assume_yes=True)
        assert s.gateway_credential("2") == KEY

    @pytest.mark.parametrize("operation", ["purge", "add-token", "add"])
    def test_gateway_activation_during_confirmation_refuses_deletion(
        self, gateway_switcher, monkeypatch, operation
    ):
        s = gateway_switcher
        seed_oauth(s)

        def confirm(*args):
            s.switch_to("2", json_output=True)
            return "y"

        monkeypatch.setattr("builtins.input", confirm)
        with pytest.raises(ConfigError, match="Switch away from the active gateway"):
            if operation == "purge":
                s.purge()
            elif operation == "add":
                s.add_account(slot=2)
            else:
                s.add_account_from_token(KEY, base_url="https://other.example", slot=2, email="other@example.com")
        assert s.current_account_number() == "2"
        assert s.gateway_credential("2") == KEY

    @pytest.mark.parametrize("contents", ["{broken", '{"env": []}'])
    def test_malformed_gateway_settings_raises_switch_error(self, gateway_switcher, contents):
        s = gateway_switcher
        seed_oauth(s)
        s._gateway_settings_path().write_text(contents)
        with pytest.raises(SwitchError, match="Cannot read Claude settings.json"):
            s.switch_to("2", json_output=True)
        assert s._gateway_settings_path().read_text() == contents

    @pytest.mark.parametrize("name", ["ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR", "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR"])
    def test_gateway_activation_refuses_settings_auth_overrides(self, gateway_switcher, name):
        s = gateway_switcher
        seed_oauth(s)
        path = s._gateway_settings_path()
        path.write_text(json.dumps({"env": {name: KEY}}))
        settings = path.read_bytes()
        sequence = s.sequence_file.read_bytes()
        with pytest.raises(SwitchError, match=str(path)) as error:
            s.switch_to("2", json_output=True)
        assert KEY not in str(error.value)
        assert path.read_bytes() == settings
        assert s.sequence_file.read_bytes() == sequence

    @pytest.mark.parametrize("slot,email", [(2, None), (None, "gateway-2@token.local"), (4, "gateway-2@token.local"), (2, "other@example.com")])
    def test_active_gateway_add_token_overwrite_or_migration_refused(self, gateway_switcher, slot, email):
        s = gateway_switcher
        seed_oauth(s)
        s.switch_to("2", json_output=True)
        sequence = s.sequence_file.read_bytes()
        settings = s._gateway_settings_path().read_bytes()
        with pytest.raises(ConfigError, match="Switch away from the active gateway"):
            s.add_account_from_token(KEY, base_url="https://other.example", slot=slot, email=email, assume_yes=True)
        assert s.sequence_file.read_bytes() == sequence
        assert s._gateway_settings_path().read_bytes() == settings
        assert s.gateway_credential("2") == KEY

    def test_remove_gateway_live_session_refusal_keeps_settings(self, gateway_switcher, monkeypatch):
        s = gateway_switcher
        seed_oauth(s)
        s.switch_to("2", json_output=True)
        s.switch_to("1", json_output=True)
        settings = s._gateway_settings_path().read_bytes()
        sequence = s.sequence_file.read_bytes()
        calls = []

        def refuse_at_delete(*args):
            calls.append(args)
            if len(calls) > 1:
                raise SessionError("live session appeared")

        monkeypatch.setattr(s, "_ensure_no_live_session", refuse_at_delete)
        with pytest.raises(SessionError, match="live session appeared"):
            s.remove_account("2", assume_yes=True)
        assert s._gateway_settings_path().read_bytes() == settings
        assert s.sequence_file.read_bytes() == sequence

    def test_purge_refuses_active_gateway(self, gateway_switcher, monkeypatch):
        s = gateway_switcher
        seed_oauth(s)
        s.switch_to("2", json_output=True)
        settings = s._gateway_settings_path().read_bytes()
        sequence = s.sequence_file.read_bytes()
        monkeypatch.setattr("builtins.input", lambda *a: "y")
        with pytest.raises(ConfigError, match="Switch away from the active gateway before purging"):
            s.purge()
        assert s.sequence_file.read_bytes() == sequence
        assert s._gateway_settings_path().read_bytes() == settings
        assert s.gateway_credential("2") == KEY

    def test_gateway_helper_refuses_session_profile(self, gateway_switcher, monkeypatch, capsys):
        s = gateway_switcher
        seed_oauth(s)
        s.switch_to("2", json_output=True)
        email = s._get_sequence_data()["accounts"]["1"]["email"]
        profile = s._session_dir("1", email)
        profile.mkdir(parents=True)
        # Simulate an older session profile with inherited gateway settings.
        (profile / "settings.json").write_bytes(s._gateway_settings_path().read_bytes())
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(profile))
        monkeypatch.setattr(cli, "ClaudeAccountSwitcher", lambda: s)
        monkeypatch.setattr(sys, "argv", ["cswap", "gateway-credential", "2"])
        capsys.readouterr()
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 1
        output = capsys.readouterr()
        assert output.out == ""
        assert "session profile" in output.err
        assert KEY not in output.err

    @pytest.mark.parametrize("first,second", [("claude-swap", "cswap"), ("cswap", "claude-swap")])
    def test_gateway_return_across_cli_aliases(self, gateway_switcher, monkeypatch, first, second):
        s = gateway_switcher
        seed_oauth(s)
        monkeypatch.setattr(gateway, "executable", lambda: f"/fake/bin/{first}")
        s.switch_to("2", json_output=True)
        monkeypatch.setattr(gateway, "executable", lambda: f"/fake/bin/{second}")
        result = s.switch_to("1", json_output=True)
        assert result["switched"]
        assert result["from"]["number"] == 2
        settings = json.loads(s._gateway_settings_path().read_text())
        assert "apiKeyHelper" not in settings
        assert settings["env"]["ANTHROPIC_BASE_URL"] == gateway.FIRST_PARTY_URL

    @pytest.mark.parametrize("operation", ["move", "swap", "move-occupied"])
    def test_active_gateway_renumber_refused(self, gateway_switcher, operation):
        s = gateway_switcher
        seed_oauth(s)
        s.add_account_from_token(KEY, base_url="https://gateway.example", slot=3)
        s.switch_to("2", json_output=True)
        sequence = s.sequence_file.read_bytes()
        settings = s._gateway_settings_path().read_bytes()
        with pytest.raises(ValidationError, match="active gateway.*switch away"):
            if operation == "move":
                s.move_account("2", "4")
            elif operation == "swap":
                s.swap_accounts("3", "2")
            else:
                s.move_account("3", "2")
        assert s.sequence_file.read_bytes() == sequence
        assert s._gateway_settings_path().read_bytes() == settings
        assert s.gateway_credential("2") == KEY

    def test_return_keeps_oauth_backup_rotated_while_gateway_active(
        self, gateway_switcher
    ):
        s = gateway_switcher
        seed_oauth(s)
        email = s._get_sequence_data()["accounts"]["1"]["email"]
        original = json.loads(s._read_account_credentials("1", email))
        original["claudeAiOauth"].update(
            accessToken="old-access", refreshToken="old-refresh", expiresAt=1000
        )
        s._write_account_credentials("1", email, json.dumps(original))
        s.switch_to("1", force=True, json_output=True)
        s.switch_to("2", json_output=True)
        refreshed = json.loads(json.dumps(original))
        refreshed["claudeAiOauth"].update(
            accessToken="new-access", refreshToken="new-refresh", expiresAt=2000
        )
        s._write_account_credentials("1", email, json.dumps(refreshed))

        s.switch_to("1", json_output=True)

        assert json.loads(s._read_account_credentials("1", email)) == refreshed
        assert json.loads(get_credentials_path().read_text()) == refreshed
        assert s.current_account_number() == "1"

    def test_activation_and_return_preserve_saved_login(self, gateway_switcher):
        s = gateway_switcher
        seed_oauth(s)
        credentials = get_credentials_path().read_bytes()
        login = get_global_config_path().read_bytes()
        settings = s._gateway_settings_path()
        settings.write_text('{"permissions":{"allow":["Read"]},"env":{"KEEP":"yes"}}')
        s.switch_to("2", json_output=True)
        assert get_credentials_path().read_bytes() == credentials
        assert get_global_config_path().read_bytes() == login
        assert s.current_account_number() == "2"
        assert s.gateway_credential("2") == KEY
        data = json.loads(settings.read_text())
        assert KEY not in settings.read_text()
        assert data["apiKeyHelper"] == "/fake/bin/cswap gateway-credential 2"
        assert data["env"]["KEEP"] == "yes"
        if sys.platform != "win32":
            assert settings.stat().st_mode & 0o777 == 0o600
        result = s.switch_to("1", json_output=True)
        assert result["from"]["number"] == 2 and result["to"]["number"] == 1
        data = json.loads(settings.read_text())
        assert "apiKeyHelper" not in data
        assert data["env"] == {
            "KEEP": "yes",
            "ANTHROPIC_BASE_URL": gateway.FIRST_PARTY_URL,
        }
        assert data["permissions"] == {"allow": ["Read"]}
        assert s.gateway_credential("2") is None

    @pytest.mark.parametrize("exists", [False, True])
    def test_failed_sequence_write_restores_exact_settings(
        self, gateway_switcher, monkeypatch, exists
    ):
        s = gateway_switcher
        seed_oauth(s)
        path = s._gateway_settings_path()
        before = b'{ "env" : { "KEEP" : "yes" } }\n'
        if exists:
            path.write_bytes(before)
        seq_before = s.sequence_file.read_bytes()
        original = s._write_json

        def fail(path, data):
            if path == s.sequence_file and data.get("activeAccountNumber") == 2:
                raise OSError("injected sequence write failure")
            return original(path, data)

        monkeypatch.setattr(s, "_write_json", fail)
        with pytest.raises(SwitchError, match="previous settings restored"):
            s.switch_to("2", json_output=True)
        assert path.read_bytes() == before if exists else not path.exists()
        assert s.sequence_file.read_bytes() == seq_before
        assert s.current_account_number() == "1"

    def test_incomplete_rollback_is_reported_without_exception_content(
        self, gateway_switcher, monkeypatch, caplog
    ):
        s = gateway_switcher
        seed_oauth(s)
        seq_before = s.sequence_file.read_bytes()

        def fail(*args, **kwargs):
            raise OSError(KEY)

        monkeypatch.setattr(s, "_write_json", fail)
        monkeypatch.setattr(gateway, "restore_settings", fail)
        with pytest.raises(SwitchError, match="rollback was incomplete") as error:
            s.switch_to("2", json_output=True)
        assert KEY not in str(error.value)
        assert KEY not in caplog.text
        assert s.sequence_file.read_bytes() == seq_before

    def test_failed_return_restores_gateway_settings(
        self, gateway_switcher, monkeypatch
    ):
        s = gateway_switcher
        seed_oauth(s)
        s.switch_to("2", json_output=True)
        before = s._gateway_settings_path().read_bytes()
        sequence = s.sequence_file.read_bytes()

        def fail(*args, **kwargs):
            raise OSError("injected OAuth write failure")

        monkeypatch.setattr(s, "_write_credentials", fail)
        with pytest.raises(SwitchError, match="previous settings restored"):
            s.switch_to("1", json_output=True)
        assert s._gateway_settings_path().read_bytes() == before
        assert s.sequence_file.read_bytes() == sequence
        assert s.gateway_credential("2") == KEY

    def test_foreign_helper_refused(self, gateway_switcher):
        s = gateway_switcher
        seed_oauth(s)
        path = s._gateway_settings_path()
        before = b'{"apiKeyHelper":"some-other-provider"}'
        path.write_bytes(before)
        with pytest.raises(SwitchError, match="foreign apiKeyHelper"):
            s.switch_to("2", json_output=True)
        assert path.read_bytes() == before
        assert s.current_account_number() == "1"

    def test_last_gateway_removal_cleans_only_owned_keys(self, gateway_switcher):
        s = gateway_switcher
        seed_oauth(s)
        s.switch_to("2", json_output=True)
        s.switch_to("1", json_output=True)
        s.remove_account("2", assume_yes=True)
        assert "ANTHROPIC_BASE_URL" not in json.loads(
            s._gateway_settings_path().read_text()
        ).get("env", {})

    @pytest.mark.skipif(sys.platform == "win32", reason="symlink mode is POSIX-only")
    def test_symlink_settings_write_through(self, gateway_switcher, temp_home):
        s = gateway_switcher
        seed_oauth(s)
        target = temp_home / "shared-settings.json"
        target.write_text('{"custom":"preserved"}')
        path = s._gateway_settings_path()
        path.symlink_to(target)
        s.switch_to("2", json_output=True)
        assert path.is_symlink()
        assert json.loads(target.read_text())["custom"] == "preserved"
        assert (
            json.loads(target.read_text())["env"]["ANTHROPIC_BASE_URL"]
            == "https://gateway.example"
        )


@pytest.mark.parametrize("ownership", ["known-foreign", "unresolved"])
def test_gateway_return_stashes_earlier_expiry_foreign_live_credentials(
    gateway_switcher, monkeypatch, ownership
):
    from claude_swap import oauth

    s = gateway_switcher
    seed_oauth(s)
    email = s.account_email("1")
    original = json.loads(s.read_account_credentials("1", email))
    original["claudeAiOauth"].update(accessToken="own-access", refreshToken="own-refresh", expiresAt=2000)
    backup = json.dumps(original)
    s._write_account_credentials("1", email, backup)
    get_credentials_path().write_text(backup)
    s.switch_to("2", json_output=True)
    foreign = json.dumps({"claudeAiOauth": {
        "accessToken": "foreign-access", "refreshToken": "foreign-refresh", "expiresAt": 1000,
    }})
    get_credentials_path().write_text(foreign)
    s._probe_verdicts.clear()
    profile_calls = []
    monkeypatch.setattr(oauth, "fetch_oauth_profile", lambda token: profile_calls.append(True))
    if ownership == "known-foreign":
        s._probe_verdicts[s._lineage_key("1", email, oauth.credential_fingerprint(foreign))] = False
    s.switch_to("1", json_output=True)
    assert profile_calls
    assert s.read_account_credentials("1", email) == backup
    assert json.loads(get_credentials_path().read_text()) == original
    stash = s.list_unclaimed_credentials()
    assert len(stash) == 1
    entry_id, metadata = next(iter(stash.items()))
    assert metadata["reason"] == (
        "known-foreign" if ownership == "known-foreign" else "gateway-retained-live"
    )
    assert s._store._read_unclaimed_credential(entry_id) == (foreign, False)


def _oauth_blob(access, refresh, expires_at):
    return json.dumps({"claudeAiOauth": {
        "accessToken": access, "refreshToken": refresh, "expiresAt": expires_at,
    }})


@pytest.mark.parametrize("entry", [
    "own-bytes", "own-family", "own-rotated", "unresolved", "forced", "alien",
])
def test_gateway_return_stashes_only_live_login_changed_since_entry(
    gateway_switcher, monkeypatch, caplog, entry
):
    from claude_swap import oauth

    s = gateway_switcher
    seed_oauth(s)
    email = s.account_email("1")
    row = s._get_sequence_data()["accounts"]["1"]
    s._write_account_credentials("1", email, _oauth_blob("own-access", "own-refresh", 1000))
    live = {
        "own-bytes": _oauth_blob("own-access", "own-refresh", 1000),
        "own-family": _oauth_blob("live-access", "own-refresh", 1500),
        "forced": _oauth_blob("own-access", "own-refresh", 1000),
    }.get(entry, _oauth_blob("rotated-access", "rotated-refresh", 1500))
    get_credentials_path().write_text(live)
    profile = {
        "own-rotated": {
            "email": row["email"], "organizationUuid": row.get("organizationUuid") or "",
            "uuid": "uuid-1",
        },
        "alien": {
            "email": "alien@example.com", "organizationUuid": "org-alien", "uuid": "uuid-alien",
        },
    }.get(entry)
    monkeypatch.setattr(oauth, "fetch_oauth_profile", lambda token: profile)
    if entry == "forced":
        # A fingerprint left by an episode that ended without an OAuth
        # activation must not vouch for a forced entry that ran no backup.
        data = s._get_sequence_data()
        data["gatewayRetainedLiveFp"] = oauth.credential_fingerprint(live)
        s._write_json(s.sequence_file, data)
    s.switch_to("2", force=entry == "forced", json_output=True)
    recorded = entry not in ("forced", "alien")
    assert (
        s._get_sequence_data().get("gatewayRetainedLiveFp")
        == (oauth.credential_fingerprint(live) if recorded else None)
    )
    # The episode: polling rotates the slot, and the retained access token
    # expires, so the profile endpoint cannot attribute it at the return.
    polled = _oauth_blob("polled-access", "polled-refresh", 9000)
    s._write_account_credentials("1", email, polled)
    monkeypatch.setattr(oauth, "fetch_oauth_profile", lambda token: None)
    caplog.clear()
    s.switch_to("1", json_output=True)
    assert s.read_account_credentials("1", email) == polled
    assert get_credentials_path().read_text() == polled
    reasons = sorted(m["reason"] for m in s.list_unclaimed_credentials().values())
    if recorded:
        # Unchanged since the entry backup put its lineage in the slot: the
        # slot's own predecessor, nothing to preserve or warn about.
        assert reasons == []
        assert not [r for r in caplog.records if r.levelname == "WARNING"]
    else:
        assert reasons == (["alien"] if entry == "alien" else []) + ["gateway-retained-live"]
        assert "outside cswap" not in caplog.text
    data = s._get_sequence_data()
    assert "gatewayRetainedLiveFp" not in data
    assert not any("gatewayRetainedLiveFp" in record for record in data["accounts"].values())


@pytest.mark.parametrize("missing", [False, True])
def test_gateway_entry_can_recover_without_live_oauth_credentials(gateway_switcher, missing):
    s = gateway_switcher
    seed_oauth(s)
    backup = s.read_account_credentials("1", s.account_email("1"))
    if missing:
        get_credentials_path().unlink()
    else:
        get_credentials_path().write_text("")
    s.switch_to("2", json_output=True)
    assert s.current_account_number() == "2"
    assert s.gateway_credential("2") == KEY
    assert s.read_account_credentials("1", s.account_email("1")) == backup


@pytest.mark.parametrize("live_expiry", [1000, 3000])
def test_gateway_return_resolves_foreign_live_owner_without_cached_verdict(gateway_switcher, monkeypatch, live_expiry):
    from claude_swap import oauth
    s = gateway_switcher
    seed_oauth(s)
    email = s.account_email("1")
    backup = json.dumps({"claudeAiOauth": {"accessToken": "own-access", "refreshToken": "own-refresh", "expiresAt": 2000}})
    s._write_account_credentials("1", email, backup)
    get_credentials_path().write_text(backup)
    s.switch_to("2", json_output=True)
    foreign = json.dumps({"claudeAiOauth": {"accessToken": "foreign-access", "refreshToken": "foreign-refresh", "expiresAt": live_expiry}})
    get_credentials_path().write_text(foreign)
    s._probe_verdicts.clear()
    monkeypatch.setattr(oauth, "fetch_oauth_profile", lambda *a: {"uuid": "foreign-uuid", "email": "foreign@example.com", "organizationUuid": "foreign-org"})
    s.switch_to("1", json_output=True)
    assert s.read_account_credentials("1", email) == backup
    assert any(s._store._read_unclaimed_credential(n)[0] == foreign for n in s.list_unclaimed_credentials())


@pytest.mark.parametrize("missing", [False, True])
def test_gateway_return_restores_backup_after_live_login_missing(gateway_switcher, missing):
    s = gateway_switcher
    seed_oauth(s)
    backup = s.read_account_credentials("1", s.account_email("1"))
    if missing:
        get_credentials_path().unlink()
    else:
        get_credentials_path().write_text("")
    s.switch_to("2", json_output=True)
    s.switch_to("1", json_output=True)
    assert s.current_account_number() == "1"
    assert s.read_account_credentials("1", s.account_email("1")) == backup
    assert json.loads(get_credentials_path().read_text()) == json.loads(backup)


def test_gateway_remove_rechecks_replacement_activated_during_confirmation(gateway_switcher, monkeypatch):
    s = gateway_switcher
    seed_oauth(s)
    def confirm(*args):
        s.add_account_from_token(KEY, base_url="https://replacement.example", slot=2, assume_yes=True)
        s.switch_to("2", json_output=True)
        return "y"
    monkeypatch.setattr("builtins.input", confirm)
    with pytest.raises(ConfigError, match="Switch away from the active gateway"):
        s.remove_account("2")
    assert s.current_account_number() == "2"
    assert s.gateway_credential("2") == KEY


def test_gateway_remove_holds_roster_lock_through_sequence_commit(gateway_switcher, monkeypatch):
    from claude_swap.locking import FileLock
    s = gateway_switcher
    seed_oauth(s)
    write = s._write_json
    held = []
    def checked_write(path, data):
        if path == s.sequence_file and "2" not in data.get("accounts", {}):
            lock = FileLock(s.lock_file)
            acquired = lock.acquire(timeout=0)
            held.append(not acquired)
            if acquired:
                lock.release()
        return write(path, data)
    monkeypatch.setattr(s, "_write_json", checked_write)
    s.remove_account("2", assume_yes=True)
    assert held == [True]
