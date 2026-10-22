"""Gateway fallback/return decisions through the real EngineHarness switcher."""

from unittest.mock import Mock

import pytest

from claude_swap import gateway
from claude_swap.autoswitch import AutoSwitchEngine, ConfigWarningEvent, NoSwitchEvent, SwitchEvent, TickOutcome
from claude_swap.usage_store import FetchRecord, UsageEntry
from tests.test_autoswitch import EngineHarness, _entry_for, _iso_at, _usage
from tests.test_gateway_accounts import KEY


def fleet(temp_home, monkeypatch, *, strategy="best", enabled=True):
    monkeypatch.setattr(gateway, "executable", lambda: "/fake/bin/cswap")
    monkeypatch.setattr(gateway, "JITTER_FRAC", 0)
    h = EngineHarness(temp_home, include_gateway_accounts=enabled, strategy=strategy)
    h.seed(1, "one@example.com")
    h.seed(2, "two@example.com")
    h.make_live("one@example.com", 1)
    h.switcher.add_account_from_token(KEY, base_url="https://gateway.example", slot=3)
    monkeypatch.setattr(h.engine, "_freshen_target", lambda *a: "ok")
    return h


def readings(h, first=100, second=100, status="ok", **extra):
    return {
        "1": _entry_for(_usage(first), h.clock()),
        "2": _entry_for(_usage(second), h.clock()),
        "3": UsageEntry(
            sentinel="gateway",
            gateway=dict(probeStatus=status, probedAt=h.clock(), budget=None, **extra),
        ),
    }


class TestAutoswitchGateway:
    def test_gateway_return_waits_for_unreadable_healthy_stash_sibling(self, temp_home, monkeypatch):
        import json
        from claude_swap import oauth

        h = fleet(temp_home, monkeypatch)
        s = h.switcher
        s.switch_to("3", json_output=True)
        h.seed(2, "two@example.com", expires_at=1000)
        current = s._read_account_credentials("2", "two@example.com")
        context = {"configSlot": "2", "consumedFp": oauth.credential_fingerprint(current)}
        def creds(label):
            return json.dumps({"claudeAiOauth": {"accessToken": label + "-access", "refreshToken": label + "-refresh", "expiresAt": 9999999999999}})
        foreign = s._store._write_unclaimed_credential(creds("foreign"), dict(context, tokenAccount={"uuid": "foreign"}))
        healthy = s._store._write_unclaimed_credential(creds("healthy"), dict(context, tokenAccount={"uuid": "uuid-2"}))
        read = s._store._read_unclaimed_credential
        locked = [True]
        monkeypatch.setattr(s._store, "_read_unclaimed_credential", lambda i: ("", True) if i == healthy and locked[0] else read(i))
        post = Mock(side_effect=AssertionError("must not POST a spent predecessor"))
        monkeypatch.setattr(oauth, "try_refresh_oauth_credentials", post)
        monkeypatch.setattr(h.engine, "_freshen_target", AutoSwitchEngine._freshen_target.__get__(h.engine))
        entries = readings(h, second=10, status="exhausted")
        assert h.tick_with_entries(entries) != TickOutcome.SWITCHED
        assert "2" not in h.state().get("quarantine", {})
        locked[0] = False
        assert h.tick_with_entries(entries) == TickOutcome.SWITCHED
        assert h.active_number() == 2
        assert s._read_account_credentials("2", "two@example.com") == creds("healthy")
        assert read(foreign) == (creds("foreign"), False)
        post.assert_not_called()

    @pytest.mark.parametrize("uuid_before", ["", " uuid-2 "])
    @pytest.mark.parametrize("unreadable", [False, True])
    def test_gateway_refresh_rechecks_identity_backfill_and_normalizes_uuid(self, temp_home, monkeypatch, uuid_before, unreadable):
        import json
        from claude_swap import oauth

        h = fleet(temp_home, monkeypatch)
        s = h.switcher
        h.seed(2, "two@example.com", expires_at=1000)
        data = s._get_sequence_data()
        data["accounts"]["2"]["uuid"] = uuid_before
        s._write_json(s.sequence_file, data)
        current = s._read_account_credentials("2", "two@example.com")
        successor = json.dumps({"claudeAiOauth": {"accessToken": "successor-access", "refreshToken": "successor-refresh", "expiresAt": 9999999999999}})
        def refresh(*_):
            if not uuid_before:
                s.backfill_account_uuid("2", "uuid-2")
            if unreadable:
                monkeypatch.setattr(s, "_read_account_credentials_ex", lambda *_: ("", True))
            return oauth.RefreshOutcome(successor, None, {"uuid": "foreign" if not uuid_before else "uuid-2"})
        monkeypatch.setattr(oauth, "try_refresh_oauth_credentials", refresh)
        outcome = s.consume_backup_grant("2", "two@example.com", current)
        if not uuid_before:
            assert outcome.error == "identity-conflict"
            assert outcome.credentials is None
            assert outcome.stashed
            assert s._read_account_credentials("2", "two@example.com") == current
        else:
            assert outcome.error == ("transient" if unreadable else None)
            assert outcome.credentials == successor

    @pytest.mark.parametrize("identity", ["uuid-conflict", "org-conflict", "match", "missing"])
    def test_gateway_return_preserves_stashed_successor_identity(self, temp_home, monkeypatch, identity):
        import json
        from claude_swap import oauth
        from claude_swap.transfer import export_accounts, import_accounts

        h = fleet(temp_home, monkeypatch)
        s = h.switcher
        s.switch_to("3", json_output=True)
        h.seed(2, "two@example.com", expires_at=1000)
        data = s._get_sequence_data()
        data["accounts"]["2"]["organizationUuid"] = "slot-org"
        s._write_json(s.sequence_file, data)
        s._write_account_config("2", "two@example.com", json.dumps({"oauthAccount": {"emailAddress": "two@example.com", "accountUuid": "uuid-2", "organizationUuid": "slot-org"}}))
        path = temp_home / "identical-refresh-import.json"
        export_accounts(s, str(path), account="2")
        predecessor = s._read_account_credentials("2", "two@example.com")
        successor = json.dumps({"claudeAiOauth": {"accessToken": "successor-access", "refreshToken": "successor-refresh", "expiresAt": 9999999999999}})
        token_account = None if identity == "missing" else {
            "uuid": "foreign-uuid" if identity == "uuid-conflict" else "uuid-2",
            "organizationUuid": "foreign-org" if identity == "org-conflict" else "slot-org",
            "email": "two@example.com",
        }
        def refresh(*args, **kwargs):
            import_accounts(s, str(path), force=True)
            return oauth.RefreshOutcome(successor, None, token_account)
        post = Mock(side_effect=refresh)
        monkeypatch.setattr(oauth, "try_refresh_oauth_credentials", post)
        monkeypatch.setattr(h.engine, "_freshen_target", AutoSwitchEngine._freshen_target.__get__(h.engine))
        entries = readings(h, second=10, status="exhausted")
        assert h.tick_with_entries(entries) != TickOutcome.SWITCHED
        assert h.active_number() == 3
        assert any(s._store._read_unclaimed_credential(i) == (successor, False) for i in s.list_unclaimed_credentials())
        outcome = h.tick_with_entries(entries)
        if identity.endswith("conflict"):
            assert outcome != TickOutcome.SWITCHED
            assert h.active_number() == 3
            assert h.state()["quarantine"]["2"]["reason"] == "identity-conflict"
            assert h.tick_with_entries(entries) != TickOutcome.SWITCHED
            assert h.active_number() == 3
            assert s._read_account_credentials("2", "two@example.com") == predecessor
            manifest = s.list_unclaimed_credentials()
            assert any(row.get("tokenAccount") == token_account for row in manifest.values())
            assert any(s._store._read_unclaimed_credential(i) == (successor, False) for i in manifest)
        else:
            assert outcome == TickOutcome.SWITCHED
            assert h.active_number() == 2
        assert post.call_count == 1

    def test_gateway_return_keeps_identity_paired_with_refresh_cas_winner(self, temp_home, monkeypatch):
        import json
        from claude_swap import oauth

        h = fleet(temp_home, monkeypatch)
        s = h.switcher
        s.switch_to("3", json_output=True)
        email = s.account_email("2")
        old = json.dumps({"claudeAiOauth": {"accessToken": "old-access", "refreshToken": "old-refresh", "expiresAt": 1000}})
        healthy = json.dumps({"claudeAiOauth": {"accessToken": "healthy-access", "refreshToken": "healthy-refresh", "expiresAt": 9999999999999}})
        foreign = json.dumps({"claudeAiOauth": {"accessToken": "foreign-access", "refreshToken": "foreign-refresh", "expiresAt": 9999999999999}})
        s._write_account_credentials("2", email, old)
        generation = s._get_sequence_data()["accounts"]["2"].get("authGeneration")
        identity = s.account_identity("2")
        def refresh(*args, **kwargs):
            s._write_account_credentials("2", email, healthy)
            return oauth.RefreshOutcome(foreign, None, {"uuid": "foreign-uuid", "organizationUuid": "foreign-org"})
        monkeypatch.setattr(oauth, "try_refresh_oauth_credentials", refresh)
        monkeypatch.setattr(h.engine, "_freshen_target", AutoSwitchEngine._freshen_target.__get__(h.engine))
        assert h.tick_with_entries(readings(h, second=10, status="exhausted")) == TickOutcome.SWITCHED
        assert h.active_number() == 2
        assert "2" not in h.state().get("quarantine", {})
        assert s._get_sequence_data()["accounts"]["2"].get("authGeneration") == generation
        assert s.account_identity("2") == identity
        assert s._read_account_credentials("2", email) == healthy
        assert any(s._store._read_unclaimed_credential(i) == (foreign, False) for i in s.list_unclaimed_credentials())

    @pytest.mark.parametrize("failure", ["invalid_grant", "identity-conflict"])
    @pytest.mark.parametrize("phase", ["freshening", "state-lock"])
    def test_gateway_return_does_not_quarantine_a_racing_healthy_oauth_generation(self, temp_home, monkeypatch, failure, phase):
        import json
        from claude_swap import oauth

        h = fleet(temp_home, monkeypatch)
        s = h.switcher
        s.switch_to("3", json_output=True)
        email = s.account_email("2")
        old = json.dumps({"claudeAiOauth": {"accessToken": "old-access", "refreshToken": "old-refresh", "expiresAt": 1000}})
        healthy = json.dumps({"claudeAiOauth": {"accessToken": "healthy-access", "refreshToken": "healthy-refresh", "expiresAt": 9999999999999}})
        foreign = json.dumps({"claudeAiOauth": {"accessToken": "foreign-access", "refreshToken": "foreign-refresh", "expiresAt": 9999999999999}})
        s._write_account_credentials("2", email, old)
        generation = s._get_sequence_data()["accounts"]["2"].get("authGeneration")
        armed, replaced = [], []
        def replace_token():
            if not replaced:
                replaced.append(True)
                s._write_account_credentials("2", email, healthy)
        def refresh(*args, **kwargs):
            if failure == "invalid_grant":
                armed.append(True)
                if phase == "freshening":
                    replace_token()
                return oauth.RefreshOutcome(None, "invalid_grant")
            return oauth.RefreshOutcome(foreign, None, {"uuid": "foreign"})
        consume = s.consume_backup_grant
        def consume_then_race(*args):
            outcome = consume(*args)
            if failure == "identity-conflict":
                assert outcome.error == "identity-conflict"
                armed.append(True)
                if phase == "freshening":
                    replace_token()
            return outcome
        monkeypatch.setattr(s, "consume_backup_grant", consume_then_race)
        monkeypatch.setattr(oauth, "try_refresh_oauth_credentials", refresh)
        monkeypatch.setattr(h.engine, "_freshen_target", AutoSwitchEngine._freshen_target.__get__(h.engine))
        state_lock = h.engine._state_lock
        def race():
            if phase == "state-lock" and armed:
                replace_token()
            return state_lock()
        monkeypatch.setattr(h.engine, "_state_lock", race)
        entries = readings(h, second=10, status="exhausted")
        h.tick_with_entries(entries)
        assert s._get_sequence_data()["accounts"]["2"].get("authGeneration") == generation
        assert s._read_account_credentials("2", email) == healthy
        assert "2" not in h.state().get("quarantine", {})
        assert h.tick_with_entries(entries) == TickOutcome.SWITCHED
        assert h.active_number() == 2

    @pytest.mark.parametrize("origin", ["oauth", "gateway"])
    @pytest.mark.parametrize("phase", ["post", "state-lock"])
    def test_gateway_replacement_is_not_quarantined_by_failed_oauth_refresh(self, temp_home, monkeypatch, origin, phase):
        import json
        from claude_swap import oauth
        from claude_swap.transfer import export_accounts, import_accounts

        h = fleet(temp_home, monkeypatch)
        s = h.switcher
        if origin == "gateway":
            s.switch_to("3", json_output=True)
        email = s.account_email("2")
        old = json.dumps({"claudeAiOauth": {"accessToken": "old-access", "refreshToken": "old-refresh", "expiresAt": 1000}})
        s._write_account_credentials("2", email, old)
        path = temp_home / "failed-refresh-replacement.json"
        export_accounts(s, str(path), account="2")
        envelope = json.loads(path.read_text())
        envelope["accounts"][0].update(kind="gateway", credentials=KEY + "-replacement", baseUrl="https://replacement.example", auth="bearer", probeModel=gateway.DEFAULT_MODEL)
        path.write_text(json.dumps(envelope))
        armed, replaced = [], []
        def replace_target():
            if not replaced:
                replaced.append(True)
                import_accounts(s, str(path), force=True)
        def refresh(*args, **kwargs):
            armed.append(True)
            if phase == "post":
                replace_target()
            return oauth.RefreshOutcome(None, "invalid_grant")
        monkeypatch.setattr(oauth, "try_refresh_oauth_credentials", refresh)
        monkeypatch.setattr(h.engine, "_freshen_target", AutoSwitchEngine._freshen_target.__get__(h.engine))
        state_lock = h.engine._state_lock
        def race():
            if phase == "state-lock" and armed:
                replace_target()
            return state_lock()
        monkeypatch.setattr(h.engine, "_state_lock", race)
        entries = readings(h, second=10, status="exhausted")
        h.tick_with_entries(entries)
        assert replaced
        assert "2" not in h.state().get("quarantine", {})
        entries["2"] = UsageEntry(sentinel="gateway", gateway=dict(probeStatus="ok", probedAt=h.clock(), budget=None))
        assert h.tick_with_entries(entries) == TickOutcome.SWITCHED
        assert h.active_number() == 2
        assert s.gateway_credential("2") == KEY + "-replacement"

    @pytest.mark.parametrize("phase", ["freshening", "pre-lock"])
    @pytest.mark.parametrize("target_kind,origin", [("oauth", "oauth"), ("oauth", "gateway"), ("gateway", "oauth")])
    def test_gateway_selection_rejects_replaced_target(self, temp_home, monkeypatch, phase, target_kind, origin):
        import json
        from claude_swap.transfer import export_accounts, import_accounts

        h = fleet(temp_home, monkeypatch, enabled=target_kind == "gateway")
        s = h.switcher
        target = "2" if target_kind == "oauth" else "3"
        if origin == "gateway":
            s.switch_to("3", json_output=True)
        source = h.active_number()
        path = temp_home / "target-replacement.json"
        export_accounts(s, str(path), account=target)
        envelope = json.loads(path.read_text())
        envelope["accounts"][0].update(kind="gateway", credentials=KEY + "-replacement", baseUrl="https://replacement.example", auth="bearer", probeModel=gateway.DEFAULT_MODEL)
        path.write_text(json.dumps(envelope))
        replaced = []
        def replace_target():
            if not replaced:
                replaced.append(True)
                import_accounts(s, str(path), force=True)
        if phase == "freshening":
            prepare = h.engine._prepare_target
            def race(*args):
                replace_target()
                return prepare(*args)
            monkeypatch.setattr(h.engine, "_prepare_target", race)
        else:
            prefetch = s._prefetch_live_identity
            def race():
                replace_target()
                return prefetch()
            monkeypatch.setattr(s, "_prefetch_live_identity", race)
        entries = readings(h, second=10 if target_kind == "oauth" else 100,
                           status="exhausted" if origin == "gateway" else "ok")
        assert h.tick_with_entries(entries) == TickOutcome.NO_ACTION
        assert h.active_number() == source
        assert s._read_account_credentials(target, s.account_email(target)) == KEY + "-replacement"
        assert not any(isinstance(e, SwitchEvent) for e in h.events)
        assert "lastSwitchAt" not in h.state()

    @pytest.mark.parametrize("phase", ["freshening", "pre-lock"])
    @pytest.mark.parametrize("origin,replacement", [("gateway", "other"), ("oauth", "other"), ("oauth", "same")])
    def test_gateway_departure_discards_changed_active_source(self, temp_home, monkeypatch, phase, origin, replacement):
        import json
        from claude_swap.transfer import export_accounts, import_accounts

        h = fleet(temp_home, monkeypatch)
        s = h.switcher
        s.add_account_from_token(KEY + "-b", slot=4, base_url="https://b.example")
        if origin == "gateway":
            s.switch_to("3", json_output=True)
        activated = []
        new_source = "1" if replacement == "same" else "4"
        def activate_other():
            if not activated:
                activated.append(True)
                if replacement == "same":
                    path = temp_home / "source-replacement.json"
                    export_accounts(s, str(path), account="1")
                    envelope = json.loads(path.read_text())
                    envelope["accounts"][0].update(kind="gateway", credentials=KEY + "-b", baseUrl="https://b.example", auth="bearer", probeModel=gateway.DEFAULT_MODEL)
                    path.write_text(json.dumps(envelope))
                    import_accounts(s, str(path), force=True)
                s.switch_to(new_source, json_output=True)
                activated.append((s._gateway_settings_path().read_bytes(), s._read_credentials()))
        if phase == "freshening":
            def prepare(*args):
                activate_other()
                return "ok"
            monkeypatch.setattr(h.engine, "_prepare_target", prepare)
        else:
            prefetch = s._prefetch_live_identity
            def race():
                activate_other()
                return prefetch()
            monkeypatch.setattr(s, "_prefetch_live_identity", race)
        entries = readings(h, second=10, status="exhausted")
        entries["4"] = UsageEntry(sentinel="gateway", gateway=dict(probeStatus="ok", probedAt=h.clock(), budget=None))
        assert h.tick_with_entries(entries) == TickOutcome.NO_ACTION
        assert h.active_number() == int(new_source)
        assert s.gateway_credential(new_source) == KEY + "-b"
        assert (s._gateway_settings_path().read_bytes(), s._read_credentials()) == activated[1]
        assert not any(isinstance(e, SwitchEvent) for e in h.events)
        assert "lastSwitchAt" not in h.state()

    @pytest.mark.parametrize("failure", ["skip-live-session", "transient", "identity-conflict"])
    def test_oauth_freshening_failure_tries_api_key_before_gateway(self, temp_home, monkeypatch, failure):
        from dataclasses import replace

        h = fleet(temp_home, monkeypatch)
        h.engine.settings = replace(h.settings, include_api_key_accounts=True)
        h.switcher.add_account_from_token("sk-ant-api03-" + KEY, slot=4)
        attempted = []
        def freshen(num, email):
            attempted.append(num)
            return failure if num == "2" else "ok"
        monkeypatch.setattr(h.engine, "_freshen_target", freshen)
        entries = readings(h, second=10)
        entries["4"] = UsageEntry(sentinel="api key")
        assert h.tick_with_entries(entries) == TickOutcome.SWITCHED
        assert attempted == ["2", "4"]
        assert h.active_number() == 4

    def test_gateway_promotion_publishes_active_probe_deadline(self, temp_home, monkeypatch):
        from dataclasses import replace

        h = fleet(temp_home, monkeypatch)
        h.engine.settings = replace(h.settings, interval_seconds=360)
        monkeypatch.setattr(h.switcher, "_run_usage_fetches",
                            lambda infos, pre: {str(i[0]): FetchRecord(usage=_usage(100)) for i in infos})
        probes = []
        def probe(record, key, **kwargs):
            probes.append(h.clock())
            return dict(probeStatus="ok", probedAt=h.clock(), retryAt=None, budget=None)
        monkeypatch.setattr(gateway, "probe", probe)
        h.switcher.usage_entries_by_account(fetch={"3"})
        h.clock.advance(1)
        h.switcher.switch_to("3", json_output=True)
        waits = []
        def wait(delay):
            waits.append(delay)
            h.clock.advance(delay)
            if len(waits) == 2:
                h.engine.stop()
        monkeypatch.setattr(h.engine._wake, "wait", wait)
        assert h.engine.run_loop() == 0
        assert waits[0] == pytest.approx(gateway.ACTIVE_INTERVAL_S - 1)
        assert probes[1] - probes[0] == pytest.approx(gateway.ACTIVE_INTERVAL_S)

    @pytest.mark.parametrize("mode", ["no-candidates", "reset", "opt-out", "disabled", "quarantined"])
    def test_blocked_loop_obeys_enabled_gateway_recovery_deadline(self, temp_home, monkeypatch, mode):
        from dataclasses import replace
        from tests.test_autoswitch import _iso_at

        h = fleet(temp_home, monkeypatch)
        if mode == "no-candidates":
            h.switcher.remove_account("2", assume_yes=True)
        monkeypatch.setattr(h.switcher, "_run_usage_fetches",
                            lambda infos, pre: {str(i[0]): FetchRecord(usage=_usage(100, _iso_at(h.clock() + 10000))) for i in infos})
        probes = []
        def probe(record, key, **kwargs):
            probes.append(h.clock())
            return dict(probeStatus="cooling" if len(probes) == 1 else "ok",
                        probedAt=h.clock(), retryAt=h.clock() + 60 if len(probes) == 1 else None,
                        coolingSince=h.clock(), budget=None)
        monkeypatch.setattr(gateway, "probe", probe)
        h.switcher.usage_entries_by_account(fetch={"3"})
        if mode == "opt-out":
            h.engine.settings = replace(h.settings, include_gateway_accounts=False)
        elif mode == "disabled":
            h.switcher.set_account_disabled("3", True)
        elif mode == "quarantined":
            h.engine._quarantine("3", h.switcher.account_email("3"), "invalid_grant")
        waits = []
        def wait(delay):
            waits.append(delay)
            h.clock.advance(delay)
            if mode in ("opt-out", "disabled", "quarantined") or h.active_number() == 3 or len(waits) > 3:
                h.engine.stop()
        monkeypatch.setattr(h.engine._wake, "wait", wait)
        assert h.engine.run_loop() == 0
        if mode in ("no-candidates", "reset"):
            assert waits[0] == pytest.approx(60)
            assert probes[1] - probes[0] == pytest.approx(60)
            assert h.active_number() == 3
        else:
            assert waits[0] >= 300
            assert len(probes) == 1
            assert h.active_number() == 1

    @pytest.mark.parametrize("status,extra,enabled,target_state,second,expected", [
        ("exhausted", {}, True, "usable", 100, 4),
        ("unauthorized", {}, True, "usable", 100, 4),
        ("cooling", {"coolingSince": 999000}, True, "usable", 100, 4),
        ("transient", {"transientSince": 999000}, True, "usable", 100, 4),
        ("ok", {}, True, "usable", 100, 3),
        ("cooling", {"coolingSince": 1000000}, True, "usable", 100, 3),
        ("exhausted", {}, False, "usable", 100, 3),
        ("exhausted", {}, True, "disabled", 100, 3),
        ("exhausted", {}, True, "quarantined", 100, 3),
        ("exhausted", {}, True, "usable", 10, 2),
    ])
    def test_failed_gateway_honors_api_key_fallback(
        self, temp_home, monkeypatch, status, extra, enabled, target_state, second, expected,
    ):
        from dataclasses import replace

        h = fleet(temp_home, monkeypatch)
        h.engine.settings = replace(h.settings, include_api_key_accounts=enabled)
        h.switcher.add_account_from_token("sk-ant-api03-" + KEY, slot=4)
        h.switcher.switch_to("3", json_output=True)
        if target_state == "disabled":
            h.switcher.set_account_disabled("4", True)
        elif target_state == "quarantined":
            h.engine._quarantine("4", h.switcher.account_email("4"), "invalid_grant")
        if expected != 2:
            monkeypatch.setattr(h.engine, "_freshen_target", AutoSwitchEngine._freshen_target.__get__(h.engine))
        entries = readings(h, second=second, status=status, **extra)
        entries["4"] = UsageEntry(sentinel="api key")
        outcome = h.tick_with_entries(entries)
        assert h.active_number() == expected
        if expected != 3:
            assert outcome == TickOutcome.SWITCHED
            assert gateway.active_slot(h.switcher._gateway_settings_path(), h.switcher._get_sequence_data()["accounts"]) is None
            assert "apiKeyHelper" not in gateway.read_settings(h.switcher._gateway_settings_path())

    def test_mixed_gateway_failures_share_departure_grace(self, temp_home, monkeypatch):
        from tests.test_gateway_probe import respond

        h = fleet(temp_home, monkeypatch)
        h.switcher.switch_to("3", json_output=True)
        record = gateway.account_fields("https://gateway.example")
        verdict = None
        for index in range(12):
            respond(monkeypatch, 500 if index % 2 == 0 else 503)
            verdict = gateway.probe(record, KEY, now=h.clock(), previous=verdict)
            entries = readings(h, second=75)
            entries["3"] = UsageEntry(sentinel="gateway", gateway=verdict)
            outcome = h.tick_with_entries(entries)
            if outcome == TickOutcome.SWITCHED:
                break
            h.clock.advance(120)
        assert outcome == TickOutcome.SWITCHED
        assert h.active_number() == 2

    def test_gateway_hold_inside_hysteresis_is_not_all_exhausted(self, temp_home, monkeypatch):
        h = fleet(temp_home, monkeypatch)
        h.switcher.switch_to("3", json_output=True)
        assert h.tick_with_entries(readings(h, second=85)) == TickOutcome.NO_ACTION
        assert [e.reason for e in h.events if isinstance(e, NoSwitchEvent)] == ["gateway-hold"]

    def test_persistent_transient_gateway_leaves_for_oauth_after_grace(self, temp_home, monkeypatch):
        h = fleet(temp_home, monkeypatch)
        h.switcher.switch_to("3", json_output=True)
        since = h.clock()
        assert h.tick_with_entries(readings(h, second=75, status="transient", transientSince=since)) == TickOutcome.NO_ACTION
        h.clock.advance(601)
        assert h.tick_with_entries(readings(h, second=75, status="transient", transientSince=since)) == TickOutcome.SWITCHED
        assert h.active_number() == 2
        assert [e.trigger for e in h.events if isinstance(e, SwitchEvent)] == ["gateway-transient"]

    @pytest.mark.parametrize("strategy", ["best", "consume-first"])
    def test_gateway_fallback_after_live_oauth_candidate_skipped(
        self, temp_home, monkeypatch, strategy
    ):
        h = fleet(temp_home, monkeypatch, strategy=strategy)
        monkeypatch.setattr(
            h.engine, "_freshen_target",
            AutoSwitchEngine._freshen_target.__get__(h.engine),
        )
        monkeypatch.setattr(h.switcher, "live_session_pids_for", lambda n, email: [123] if n == "2" else [])
        assert h.tick_with_entries(readings(h, second=10)) == TickOutcome.SWITCHED
        assert h.active_number() == 3

    @pytest.mark.parametrize("strategy", ["best", "consume-first"])
    @pytest.mark.parametrize("second", [10, 85])
    def test_gateway_only_after_qualifying_oauth(
        self, temp_home, monkeypatch, strategy, second
    ):
        h = fleet(temp_home, monkeypatch, strategy=strategy)
        assert h.tick_with_entries(readings(h, second=second)) == TickOutcome.SWITCHED
        assert h.active_number() == 2
        h.make_live("one@example.com", 1)
        assert h.tick_with_entries(readings(h)) == TickOutcome.SWITCHED
        assert h.active_number() == 3

    def test_opt_in_and_no_consume_first_nudge(self, temp_home, monkeypatch):
        h = fleet(temp_home, monkeypatch, enabled=False)
        h.tick_with_entries(readings(h))
        assert h.active_number() != 3
        h = fleet(temp_home, monkeypatch, strategy="consume-first")
        h.tick_with_entries(readings(h, first=20))
        assert h.active_number() != 3

    @pytest.mark.parametrize(
        "status,extra,reason",
        [
            ("exhausted", {}, "gateway-exhausted"),
            ("unauthorized", {}, "gateway-unauthorized"),
            ("cooling", {"coolingSince": 999000}, "gateway-cooling"),
        ],
    )
    def test_departure_reasons(self, temp_home, monkeypatch, status, extra, reason):
        h = fleet(temp_home, monkeypatch)
        h.switcher.switch_to("3", json_output=True)
        result = h.tick_with_entries(readings(h, second=50, status=status, **extra))
        assert result == TickOutcome.SWITCHED
        assert h.active_number() == 2
        assert [e.trigger for e in h.events if isinstance(e, SwitchEvent)] == [reason]

    @pytest.mark.parametrize("strategy", ["best", "consume-first"])
    @pytest.mark.parametrize("status,extra", [
        ("exhausted", {}),
        ("unauthorized", {}),
        ("cooling", {"coolingSince": 999000}),
        ("transient", {"transientSince": 999000}),
    ])
    def test_failed_gateway_can_escape_without_return_margin(
        self, temp_home, monkeypatch, strategy, status, extra
    ):
        h = fleet(temp_home, monkeypatch, strategy=strategy)
        h.switcher.switch_to("3", json_output=True)
        h.engine._mutate_state(lambda state: state.update(lastSwitchAt=h.clock()))
        assert (
            h.tick_with_entries(readings(h, second=85, status=status, **extra))
            == TickOutcome.SWITCHED
        )
        assert h.active_number() == 2
        assert [e.trigger for e in h.events if isinstance(e, SwitchEvent)] == ["gateway-" + status]

    @pytest.mark.parametrize("strategy", ["best", "consume-first"])
    @pytest.mark.parametrize("preferred_freshens", [True, False])
    def test_failed_gateway_prefers_margin_then_tries_under_line(
        self, temp_home, monkeypatch, strategy, preferred_freshens
    ):
        h = fleet(temp_home, monkeypatch, strategy=strategy)
        h.switcher.switch_to("3", json_output=True)
        entries = readings(h, first=85, second=75, status="exhausted")
        # The inside-band account resets sooner: margin preference must precede
        # consume-first's usual weekly-reset ranking, without excluding it.
        entries["1"].last_good["seven_day"]["resets_at"] = _iso_at(h.clock() + 3600)
        entries["2"].last_good["seven_day"]["resets_at"] = _iso_at(h.clock() + 7200)
        attempted = []

        def freshen(number, email):
            attempted.append(number)
            return "transient" if number == "2" and not preferred_freshens else "ok"

        monkeypatch.setattr(h.engine, "_freshen_target", freshen)
        assert h.tick_with_entries(entries) == TickOutcome.SWITCHED
        assert h.active_number() == (2 if preferred_freshens else 1)
        assert attempted == (["2"] if preferred_freshens else ["2", "1"])

    @pytest.mark.parametrize("second", [90, 95])
    def test_failed_gateway_holds_when_every_oauth_seat_reaches_its_line(
        self, temp_home, monkeypatch, second
    ):
        h = fleet(temp_home, monkeypatch)
        h.switcher.switch_to("3", json_output=True)
        assert h.tick_with_entries(readings(h, first=90, second=second, status="exhausted")) == TickOutcome.BLOCKED
        assert h.active_number() == 3
        holds = [e for e in h.events if isinstance(e, NoSwitchEvent)]
        assert [(e.reason, e.detail) for e in holds] == [
            ("gateway-exhausted", "no eligible fallback account; holding gateway")
        ]

    def test_gateway_does_not_hide_oauth_model_warning(self, temp_home, monkeypatch):
        h = fleet(temp_home, monkeypatch)
        h.engine._models = ("typo-model",)
        h.engine._check_model_names(
            set(), {"1": _usage(10), "2": _usage(20), "3": "gateway"}
        )
        assert any(isinstance(event, ConfigWarningEvent) for event in h.events)

    def test_gateway_hold_and_failed_return_hysteresis_and_all_exhausted(self, temp_home, monkeypatch):
        h = fleet(temp_home, monkeypatch)
        h.switcher.switch_to("3", json_output=True)
        assert h.tick_with_entries(readings(h, second=81)) == TickOutcome.NO_ACTION
        assert h.active_number() == 3
        h.engine._mutate_state(lambda state: state.update(lastSwitchAt=h.clock()))
        assert h.tick_with_entries(readings(h, second=50)) == TickOutcome.NO_ACTION
        h.clock.advance(301)
        assert (
            h.tick_with_entries(readings(h, status="exhausted")) == TickOutcome.BLOCKED
        )
        assert h.active_number() == 3
        assert h.tick_with_entries(readings(h, second=50, status="exhausted")) == TickOutcome.SWITCHED

    def test_real_collector_probes_once_per_tick_at_gateway_cadence(
        self, temp_home, monkeypatch
    ):
        h = fleet(temp_home, monkeypatch)
        monkeypatch.setattr(
            h.switcher,
            "_run_usage_fetches",
            lambda infos, pre: {
                str(i[0]): FetchRecord(usage=_usage(100)) for i in infos
            },
        )
        probe = Mock(
            side_effect=lambda *a, **kw: dict(
                probeStatus="ok", probedAt=kw["now"], retryAt=None, budget=None
            )
        )
        monkeypatch.setattr(gateway, "probe", probe)
        assert h.engine.tick() == TickOutcome.SWITCHED
        assert probe.call_count == 1
        h.clock.advance(60)
        h.engine.tick()
        assert probe.call_count == 1
        h.clock.advance(60)
        h.engine.tick()
        assert probe.call_count == 2

    def test_budget_uses_own_threshold(self, temp_home, monkeypatch):
        h = fleet(temp_home, monkeypatch)
        h.switcher.set_account_threshold("3", 60)
        data = readings(h)
        data["3"].gateway["budget"] = {
            "pct": 65,
            "spentUsd": 325,
            "limitUsd": 500,
            "resetsAt": None,
        }
        h.tick_with_entries(data)
        assert h.active_number() != 3


@pytest.mark.parametrize("strategy", ["best", "consume-first"])
def test_gateway_recovered_oauth_waits_until_gateway_says_no(temp_home, monkeypatch, strategy):
    h = fleet(temp_home, monkeypatch, strategy=strategy)
    h.switcher.switch_to("3", json_output=True)
    assert h.tick_with_entries(readings(h, second=10)) == TickOutcome.NO_ACTION
    assert h.active_number() == 3
    assert h.tick_with_entries(readings(h, second=10, status="exhausted")) == TickOutcome.SWITCHED
    assert h.active_number() == 2


@pytest.mark.parametrize("reason", ["identity-conflict", "invalid_grant"])
def test_gateway_return_quarantine_survives_second_tick(temp_home, monkeypatch, reason):
    import json
    from claude_swap import oauth

    h = fleet(temp_home, monkeypatch)
    h.switcher.switch_to("3", json_output=True)
    h.seed(2, "two@example.com", expires_at=0)
    monkeypatch.setattr(h.engine, "_freshen_target", AutoSwitchEngine._freshen_target.__get__(h.engine))
    fresh = json.dumps({"claudeAiOauth": {
        "accessToken": "foreign-access", "refreshToken": "foreign-refresh",
        "expiresAt": 99_999_999_999_000,
    }})
    result = (oauth.RefreshOutcome(fresh, None, {"uuid": "foreign-uuid", "email": "foreign@example.com", "organizationUuid": ""})
              if reason == "identity-conflict" else oauth.RefreshOutcome(None, "invalid_grant"))
    refresh = Mock(return_value=result)
    monkeypatch.setattr(oauth, "try_refresh_oauth_credentials", refresh)
    assert h.tick_with_entries(readings(h, second=10, status="exhausted")) == TickOutcome.BLOCKED
    assert h.tick_with_entries(readings(h, second=10, status="exhausted")) == TickOutcome.BLOCKED
    assert h.active_number() == 3
    assert h.state()["quarantine"]["2"]["reason"] == reason
    assert refresh.call_count == 1


def test_gateway_real_collector_resumes_after_oauth_idle_hold(temp_home, monkeypatch):
    from dataclasses import replace
    from claude_swap.autoswitch import IDLE_HOLD_MAX_S
    from claude_swap.json_output import USAGE_TOKEN_EXPIRED

    h = fleet(temp_home, monkeypatch)
    h.switcher.remove_account("2", assume_yes=True)
    h.engine.settings = replace(h.settings, unhealthy_ticks=1)
    recovered = False
    polled = []

    def fetch(infos, pre):
        polled.extend(str(i[0]) for i in infos)
        return {str(i[0]): FetchRecord(usage=_usage(10)) if recovered
                else FetchRecord(sentinel=USAGE_TOKEN_EXPIRED) for i in infos}

    monkeypatch.setattr(h.switcher, "_run_usage_fetches", fetch)
    status = "ok"
    monkeypatch.setattr(gateway, "probe", lambda *a, **kw: dict(
        probeStatus=status, probedAt=kw["now"], retryAt=None, budget=None))
    assert h.engine.tick() == TickOutcome.NO_ACTION
    assert h.engine._idle_hold_since is not None
    h.clock.advance(IDLE_HOLD_MAX_S + 1)
    assert h.engine.tick() == TickOutcome.SWITCHED
    assert h.active_number() == 3
    recovered = True
    status = "exhausted"
    polled.clear()
    h.clock.advance(601)
    assert h.engine.tick() == TickOutcome.SWITCHED
    assert polled == ["1"]
    assert h.active_number() == 1
    assert h.engine._idle_hold_since is None
    assert not h.engine._idle_hold_slow
    assert h.engine._unhealthy_ticks == 0


def test_gateway_entry_captures_live_rotation_before_candidate_collection(temp_home, monkeypatch):
    import json
    from claude_swap import oauth
    from claude_swap.paths import get_credentials_path

    h = fleet(temp_home, monkeypatch)
    h.seed(1, "one@example.com", expires_at=1000)
    fresh = json.dumps({"claudeAiOauth": {
        "accessToken": "rotated-access", "refreshToken": "rotated-refresh",
        "expiresAt": 99_999_999_999_000,
    }})
    get_credentials_path().write_text(fresh)
    monkeypatch.setattr(oauth, "fetch_oauth_profile", lambda *a: {
        "uuid": "uuid-1", "email": "one@example.com", "organizationUuid": ""})
    h.switcher.switch_to("3", json_output=True)
    observed = {}

    def fetch(infos, pre):
        observed.update({str(i[0]): i[5] for i in infos})
        return {str(i[0]): FetchRecord(usage=_usage(10)) if i[5] == fresh
                else FetchRecord(error="invalid_grant") for i in infos}

    monkeypatch.setattr(h.switcher, "_run_usage_fetches", fetch)
    monkeypatch.setattr(gateway, "probe", lambda *a, **kw: dict(
        probeStatus="ok", probedAt=kw["now"], retryAt=None, budget=None))
    assert h.engine.tick() == TickOutcome.NO_ACTION
    assert observed["1"] == fresh
    assert h.switcher.read_account_credentials("1", "one@example.com") == fresh
    assert not h.switcher._slot_token_dead("1", "one@example.com")
    assert not h.state().get("quarantine")


def test_gateway_unavailable_credential_has_bounded_departure(temp_home, monkeypatch):
    h = fleet(temp_home, monkeypatch)
    h.switcher.switch_to("3", json_output=True)
    read = h.switcher._read_account_credentials
    monkeypatch.setattr(h.switcher, "_read_account_credentials", lambda n, email: "" if n == "3" else read(n, email))
    monkeypatch.setattr(h.switcher, "_run_usage_fetches", lambda infos, pre: {str(i[0]): FetchRecord(usage=_usage(10)) for i in infos})
    probe = Mock()
    monkeypatch.setattr(gateway, "probe", probe)
    assert h.engine.tick() == TickOutcome.NO_ACTION
    verdict = h.switcher.usage_entries_by_account(fetch=set())["3"].gateway
    assert verdict and verdict["probeStatus"] == "transient"
    h.clock.advance(601)
    assert h.engine.tick() == TickOutcome.SWITCHED
    assert h.active_number() in (1, 2)
    assert not probe.called
