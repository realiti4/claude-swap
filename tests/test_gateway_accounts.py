"""Gateway records, opt-in settings, public output, and transfer boundaries."""

import io
import json
import sys

import pytest

from claude_swap import cli, gateway
from claude_swap.exceptions import ValidationError
from claude_swap.gateway import probe as real_gateway_probe
from claude_swap.models import Platform
from claude_swap.settings import load_settings, set_setting
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.transfer import export_accounts, import_accounts

KEY = "gateway-test-secret-DO-NOT-LEAK-9x7"


def test_gateway_auth_generation_follows_explicit_writes_and_relocations(gateway_switcher, temp_home):
    s = gateway_switcher
    for num in (1, 2):
        s.add_account_from_token("sk-ant-oat01-" + str(num), email=f"{num}@example.com", slot=num)
    before = {n: r["authGeneration"] for n, r in s._get_sequence_data()["accounts"].items()}
    path = temp_home / "batch.json"
    export_accounts(s, str(path))
    envelope = json.loads(path.read_text())
    assert all("authGeneration" not in r for r in envelope["accounts"])
    for row in envelope["accounts"]:
        row["authGeneration"] = "caller-supplied"
    path.write_text(json.dumps(envelope))
    import_accounts(s, str(path), force=True)
    after = {n: r["authGeneration"] for n, r in s._get_sequence_data()["accounts"].items()}
    assert all(after[n] not in (before[n], "caller-supplied") for n in before)
    s._write_account_credentials("1", s.account_email("1"), "automatic-descendant")
    assert s._get_sequence_data()["accounts"]["1"]["authGeneration"] == after["1"]
    s.swap_accounts("1", "2")
    s.move_account("2", "5")
    rows = s._get_sequence_data()["accounts"]
    assert rows["1"]["authGeneration"] == after["2"]
    assert rows["5"]["authGeneration"] == after["1"]


@pytest.fixture
def gateway_switcher(temp_home, monkeypatch):
    monkeypatch.setattr(gateway, "executable", lambda: "/fake/bin/cswap")
    monkeypatch.setattr(gateway, "JITTER_FRAC", 0)
    monkeypatch.setattr(
        gateway,
        "probe",
        lambda record, key, **kw: {
            "probeStatus": "ok",
            "probedAt": kw["now"],
            "retryAt": None,
            "budget": None,
        },
    )
    s = ClaudeAccountSwitcher()
    s.platform = Platform.LINUX
    s._setup_directories()
    s._init_sequence_file()
    return s


class TestGatewayAccounts:
    @pytest.mark.parametrize("json_output", [False, True])
    @pytest.mark.parametrize("status", [429, 401, 200])
    def test_next_available_preserves_gateway_probe_status(
        self, gateway_switcher, monkeypatch, capsys, status, json_output
    ):
        from claude_swap.usage_store import FetchRecord
        from tests.test_gateway_probe import respond

        s = gateway_switcher
        s.add_account_from_token("sk-ant-oat01-first", slot=1)
        s.switch_to("1", json_output=True)
        s.add_account_from_token(KEY, base_url="https://gateway.example", slot=2)
        s.add_account_from_token("sk-ant-oat01-third", slot=3)
        monkeypatch.setattr(gateway, "probe", real_gateway_probe)
        request = respond(monkeypatch, status)
        monkeypatch.setattr(s, "_run_usage_fetches", lambda infos, pre: {
            str(info[0]): FetchRecord(usage={"five_hour": {"pct": 10}})
            for info in infos
        })
        # Exercise collection and conversion too: a stubbed usage map misses
        # the sentinel conversion that used to discard the actual HTTP verdict.
        result = s.switch(strategy="next-available", json_output=json_output)
        assert request.open.call_count == 1
        assert s.current_account_number() == ("2" if status == 200 else "3")
        output = capsys.readouterr().out
        if status != 200:
            reason = "exhausted" if status == 429 else "unauthorized"
            message = f"Account-2 (gateway {reason})"
            assert message in (" ".join(result["warnings"]) if json_output else output)

    @pytest.mark.parametrize("json_output", [False, True])
    def test_next_available_names_failing_gateway_without_window_limits(
        self, gateway_switcher, monkeypatch, capsys, json_output
    ):
        from tests.test_gateway_probe import respond

        s = gateway_switcher
        s.add_account_from_token("sk-ant-oat01-first", slot=1)
        s.switch_to("1", json_output=True)
        s.add_account_from_token(KEY, base_url="https://gateway.example", slot=2)
        monkeypatch.setattr(gateway, "probe", real_gateway_probe)
        respond(monkeypatch, 401)
        monkeypatch.setattr(s, "_run_usage_fetches", lambda infos, pre: {})
        result = s.switch(strategy="next-available", json_output=json_output)
        assert s.current_account_number() == "1"
        message = "All other accounts are at their limits or unavailable — staying on Account-1."
        if json_output:
            assert result["reason"] == "candidates-exhausted"
            assert result["message"] == message
        else:
            output = capsys.readouterr()
            assert message in output.out + output.err
            assert "5h/7d" not in output.out + output.err

    @pytest.mark.parametrize("view", ["json-list", "dashboard"])
    def test_gateway_rendering_uses_one_metadata_snapshot(self, gateway_switcher, temp_home, monkeypatch, view):
        from claude_swap import oauth

        s = gateway_switcher
        s.add_account_from_token("sk-ant-oat01-dummy", slot=1)
        path = temp_home / "render-replacement.json"
        export_accounts(s, str(path), account="1")
        envelope = json.loads(path.read_text())
        envelope["accounts"][0].update(kind="gateway", credentials=KEY, baseUrl="https://gateway.example", auth="bearer", probeModel=gateway.DEFAULT_MODEL)
        path.write_text(json.dumps(envelope))
        monkeypatch.setattr(oauth, "try_fetch_usage_for_account", lambda *args, **kwargs: oauth.UsageOutcome({"five_hour": {"pct": 10}}))
        armed, replaced = [False], []
        collect, read = s._collect_usage_entries, s._get_sequence_data
        def arm(*args, **kwargs):
            result = collect(*args, **kwargs)
            armed[0] = True
            return result
        def race():
            data = read()
            if armed[0]:
                armed[0] = False
                import_accounts(s, str(path), force=True)
                replaced.append(True)
            return data
        monkeypatch.setattr(s, "_collect_usage_entries", arm)
        monkeypatch.setattr(s, "_get_sequence_data", race)
        if view == "json-list":
            payload = s.list_accounts(json_output=True)
            assert "gateway" not in payload["accounts"][0]
        else:
            snapshot = s.accounts_snapshot()
            assert snapshot.accounts[0].kind == "oauth"
            assert snapshot.accounts[0].gateway is None
        assert replaced
        assert s.account_kind_for("1") == "gateway"

    @pytest.mark.parametrize("active", [False, True])
    def test_gateway_import_discards_oauth_polling_deadline(self, gateway_switcher, temp_home, monkeypatch, active):
        from unittest.mock import Mock
        from claude_swap.usage_store import FetchRecord

        s = gateway_switcher
        s.add_account_from_token("sk-ant-oat01-dummy", slot=1)
        email = s.account_email("1")
        monkeypatch.setattr(s._usage_store, "clock", lambda: 1000.0)
        s._usage_store.record({"1": FetchRecord(usage={"five_hour": {"pct": 10}})}, {"1": (email, "")})
        rows = s._usage_store._read_rows()
        rows["1"].update(nextPollAt=2800.0, pollIntervalS=1800.0, heldUntil=2800.0)
        s._usage_store._write_rows(rows)
        path = temp_home / "gateway-over-oauth.json"
        export_accounts(s, str(path), account="1")
        envelope = json.loads(path.read_text())
        envelope["accounts"][0].update(kind="gateway", credentials=KEY, baseUrl="https://gateway.example", auth="bearer", probeModel=gateway.DEFAULT_MODEL)
        path.write_text(json.dumps(envelope))
        import_accounts(s, str(path), force=True)
        if active:
            s.switch_to("1", json_output=True)
        probe = Mock(return_value=dict(probeStatus="ok", probedAt=1000.0, retryAt=None, budget=None))
        monkeypatch.setattr(gateway, "probe", probe)
        entry = s._collect_usage_entries(s._build_accounts_info())["1"]
        assert probe.call_count == 1
        assert entry.gateway["probeStatus"] == "ok"
        assert entry.last_good is None
        assert entry.held_until is None
        assert entry.next_poll_at == 1000.0 + (gateway.ACTIVE_INTERVAL_S if active else gateway.CANDIDATE_INTERVAL_S)

    @pytest.mark.parametrize("status", ["unauthorized", "transient", "cooling"])
    def test_gateway_probe_publishes_failure_during_credential_read_outage(self, gateway_switcher, monkeypatch, status):
        s = gateway_switcher
        s.add_account_from_token(KEY, slot=1, base_url="https://gateway.example")
        now, unavailable, probes = [1000.0], [False], []
        monkeypatch.setattr(s._usage_store, "clock", lambda: now[0])
        read = s._read_account_credentials
        monkeypatch.setattr(s, "_read_account_credentials", lambda *args: "" if unavailable[0] else read(*args))
        def probe(*args, **kwargs):
            probes.append(True)
            unavailable[0] = True
            since = (kwargs.get("previous") or {}).get("failureSince", kwargs["now"])
            return dict(probeStatus=status, probedAt=kwargs["now"], failureSince=since, transientSince=since, retryAt=kwargs["now"] + 60 if status == "cooling" else None, budget=None)
        monkeypatch.setattr(gateway, "probe", probe)
        for _ in range(3):
            unavailable[0] = False
            entry = s._collect_usage_entries(s._build_accounts_info())["1"]
            assert entry.gateway is not None
            assert entry.gateway["probeStatus"] == status
            assert entry.gateway["failureSince"] == 1000
            now[0] += 601
        if status == "unauthorized":
            assert len(probes) == 1

    @pytest.mark.parametrize("mode", ["collector", "worker", "diagnostics", "after-static"])
    def test_gateway_captured_credentials_never_cross_oauth_routing(self, gateway_switcher, temp_home, monkeypatch, mode):
        from claude_swap import oauth
        from claude_swap.usage_store import UsageEntry

        s = gateway_switcher
        key = json.dumps({"claudeAiOauth": {"accessToken": "opaque-access", "refreshToken": "opaque-refresh", "expiresAt": 9999999999999, "refreshTokenExpiresAt": 9999999999999}})
        for num in (1, 2):
            s.add_account_from_token(key, slot=num, base_url="https://gateway.example")
        infos = s._build_accounts_info()
        path = temp_home / "routing-replacement.json"
        export_accounts(s, str(path))
        envelope = json.loads(path.read_text())
        for num, row in enumerate(envelope["accounts"]):
            row.update(kind="oauth", credentials={"claudeAiOauth": {"accessToken": "oauth-access-" + str(num), "refreshToken": "oauth-refresh-" + str(num), "expiresAt": 9999999999999}})
        path.write_text(json.dumps(envelope))
        import_accounts(s, str(path), force=True)
        requests = []
        def fetch(*args, **kwargs):
            requests.append(args[2])
            return oauth.UsageOutcome({"five_hour": {"pct": 10}})
        monkeypatch.setattr(oauth, "try_fetch_usage_for_account", fetch)
        def unexpected(*args, **kwargs):
            pytest.fail("Captured gateway credential reached an OAuth oracle")
        monkeypatch.setattr(oauth, "try_refresh_oauth_credentials", unexpected)
        monkeypatch.setattr(oauth, "fetch_oauth_profile", unexpected)
        if mode == "after-static":
            infos = s._build_accounts_info()
            sentinel = s._static_usage_sentinel
            replaced = []
            def race(info):
                result = sentinel(info)
                if not replaced:
                    replaced.append(True)
                    for row in envelope["accounts"]:
                        row.update(kind="gateway", credentials=key)
                    path.write_text(json.dumps(envelope))
                    import_accounts(s, str(path), force=True)
                return result
            monkeypatch.setattr(s, "_static_usage_sentinel", race)
            s._collect_usage_entries(infos)
        elif mode == "collector":
            s._collect_usage_entries(infos)
        elif mode == "worker":
            for info in infos:
                s._fetch_account_usage(info)
        else:
            assert all(s._token_status_lines(info) == [] for info in infos)
            assert s._duplicate_account_warnings(infos) == []
            payload = s._build_list_payload(infos, {"1": UsageEntry(), "2": UsageEntry()})
            assert all("loginExpiresAt" not in row for row in payload["accounts"])
        assert requests == []
        assert all("lastGood" not in row for row in s._usage_store._read_rows().values())

    @pytest.mark.parametrize("phase", ["before-claim", "during-probe"])
    def test_gateway_probe_cannot_republish_after_oauth_replacement(self, gateway_switcher, temp_home, monkeypatch, phase):
        from claude_swap.autoswitch import due_candidate
        from claude_swap.usage_store import FetchRecord

        s = gateway_switcher
        s.add_account_from_token(KEY, slot=1, email="same@example.com", base_url="https://gateway.example")
        s.add_account_from_token(KEY + "-b", slot=2, base_url="https://b.example")
        s.switch_to("2", json_output=True)
        path = temp_home / "replace.json"
        export_accounts(s, str(path), account="1")
        envelope = json.loads(path.read_text())
        envelope["accounts"][0].update(kind="oauth", credentials={"claudeAiOauth": {"accessToken": "replacement-access", "refreshToken": "replacement-refresh", "expiresAt": 9999999999999}})
        path.write_text(json.dumps(envelope))
        replaced = []
        def replace_target():
            if not replaced:
                replaced.append(True)
                import_accounts(s, str(path), force=True)
        snapshot = s.account_auth_snapshot
        if phase == "before-claim":
            def race(num, *args):
                result = snapshot(num, *args)
                if num == "1":
                    replace_target()
                return result
            monkeypatch.setattr(s, "account_auth_snapshot", race)
        else:
            probe = gateway.probe
            def race(record, key, **kwargs):
                if record["email"] == "same@example.com":
                    replace_target()
                return probe(record, key, **kwargs)
            monkeypatch.setattr(gateway, "probe", race)
        s.usage_entries_by_account(fetch={"1"})
        row = s._usage_store._read_rows().get("1", {})
        assert "gateway" not in row and "gatewayFingerprint" not in row
        entries = s.usage_entries_by_account(fetch=set())
        assert due_candidate(["1"], entries, s._usage_store.clock()) == "1"
        polled = []
        def fetch(infos, pre):
            polled.extend(str(i[0]) for i in infos)
            return {str(i[0]): FetchRecord(usage={"five_hour": {"pct": 10}}) for i in infos}
        monkeypatch.setattr(s, "_run_usage_fetches", fetch)
        s.usage_entries_by_account(fetch={"1"})
        assert polled == ["1"]

    @pytest.mark.parametrize("key", ["12345", "null", "true", "[]", '"opaque"',
        '{"claudeAiOauth":{"accessToken":"opaque","refreshToken":"opaque-refresh","expiresAt":9999999999999}}'])
    def test_gateway_json_shaped_keys_remain_opaque(self, gateway_switcher, temp_home, capsys, key):
        s = gateway_switcher
        for num in [1, 2]:
            s.add_account_from_token(key, slot=num, base_url="https://gateway.example")
        s.switch_to("1", json_output=True)
        payload = s.list_accounts(json_output=True)
        assert all("loginExpiresAt" not in row for row in payload["accounts"])
        assert not payload.get("duplicateAccountWarnings")
        s.list_accounts()
        s.status(json_output=True)
        assert "Traceback" not in capsys.readouterr().err
        path = temp_home / "opaque-export.json"
        export_accounts(s, str(path))
        sequence = s.sequence_file.read_bytes()
        import_accounts(s, str(path))
        assert s.sequence_file.read_bytes() == sequence
        assert s.gateway_credential("1") == key

    def test_gateway_replaced_by_oauth_clears_cache_and_polls(
        self, gateway_switcher, monkeypatch
    ):
        from claude_swap.usage_store import FetchRecord

        s = gateway_switcher
        email = "same@example.com"
        s.add_account_from_token(KEY, base_url="https://gateway.example", slot=1, email=email)
        record = s._get_sequence_data()["accounts"]["1"]
        store = s._usage_store
        store.collect_gateway("1", (email, ""), record, KEY, active=False, fetch=True)
        assert store.entries({"1": (email, "")})["1"].sentinel == "gateway"
        s.remove_account("1", assume_yes=True)
        assert "1" not in store._read_rows()
        s.add_account_from_token("sk-ant-oat01-dummy", slot=1, email=email)
        usage = {"five_hour": {"pct": 10.0}, "seven_day": {"pct": 20.0}}
        polled = []

        def fetch(infos, pre):
            polled.extend(str(info[0]) for info in infos)
            return {str(info[0]): FetchRecord(usage=usage) for info in infos}

        monkeypatch.setattr(s, "_run_usage_fetches", fetch)
        entry = s.usage_entries_by_account()["1"]
        assert polled == ["1"]
        assert entry.sentinel is None and entry.gateway is None
        assert entry.decision_value() == usage

    @pytest.mark.parametrize("key", [KEY, "sk-ant-oat01-dummy", "sk-ant-api03-dummy"])
    def test_raw_record_and_defaults(self, gateway_switcher, key):
        s = gateway_switcher
        s.add_account_from_token(
            key, base_url="https://gateway.example/v1/", alias="apm"
        )
        record = s._get_sequence_data()["accounts"]["1"]
        assert record["email"] == "gateway-1@token.local"
        assert s._account_kind("1") == "gateway"
        assert record["baseUrl"] == "https://gateway.example"
        assert record["auth"] == "bearer"
        assert record["probeModel"] == "claude-haiku-4-5"
        assert record["modelDiscovery"] is False
        assert record["alias"] == "apm"
        assert s._read_account_credentials("1", record["email"]) == key

    @pytest.mark.parametrize(
        "url",
        [
            "http://gateway.example",
            "https://user:secret@host",
            "https://host/?key=secret",
            "ftp://host",
            "https://",
        ],
    )
    def test_rejects_unsafe_urls_without_echo(self, gateway_switcher, url):
        with pytest.raises(ValidationError) as exc:
            gateway_switcher.add_account_from_token(KEY, base_url=url)
        assert "secret" not in str(exc.value)
        assert not gateway_switcher._get_sequence_data()["accounts"]

    @pytest.mark.parametrize(
        "first,second", [(None, "https://gw.example"), ("https://gw.example", None)]
    )
    def test_cross_kind_collision(self, gateway_switcher, first, second):
        s = gateway_switcher
        s.add_account_from_token(KEY, email="same@example.com", base_url=first)
        with pytest.raises(ValidationError, match="already exists"):
            s.add_account_from_token(KEY, email="same@example.com", base_url=second)

    def test_gateway_setting_roundtrip(self, gateway_switcher):
        root = gateway_switcher.backup_dir
        assert load_settings(root).include_gateway_accounts is False
        set_setting(root, "autoswitch.includeGatewayAccounts", "true")
        assert load_settings(root).include_gateway_accounts is True

    def test_cli_stdin(self, gateway_switcher, monkeypatch):
        monkeypatch.setattr(cli, "ClaudeAccountSwitcher", lambda **kw: gateway_switcher)
        monkeypatch.setattr(sys, "stdin", io.StringIO(KEY + "\n"))
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "cswap",
                "add-token",
                "-",
                "--base-url",
                "http://localhost:9000",
                "--auth",
                "x-api-key",
                "--alias",
                "apm",
                "--slot",
                "4",
                "--model-discovery",
            ],
        )
        cli.main()
        r = gateway_switcher._get_sequence_data()["accounts"]["4"]
        assert r["auth"] == "x-api-key" and r["alias"] == "apm" and r["modelDiscovery"]

    def test_json_and_display_no_key_leak(self, gateway_switcher, capsys, caplog):
        s = gateway_switcher
        s.add_account_from_token(KEY, base_url="https://gateway.example")
        s.switch_to("1", json_output=True)
        row = s.list_accounts(json_output=True)["accounts"][0]
        assert row["kind"] == row["usageStatus"] == "gateway"
        assert row["gateway"]["probeStatus"] == "ok"
        assert row["gateway"]["budget"] is None
        assert s.status(json_output=True)["active"]["gateway"] == row["gateway"]
        s.list_accounts()
        s.status()
        out = capsys.readouterr().out
        assert "gateway · gateway.example · bearer" in out
        assert "gateway ok" in out
        assert KEY not in out + caplog.text + json.dumps(row)

    def test_export_import_gateway_round_trip(self, gateway_switcher, temp_home):
        s = gateway_switcher
        s.add_account_from_token(
            KEY,
            base_url="https://gateway.example",
            auth="x-api-key",
            usage_url="https://gateway.example/v1/usage",
            model_discovery=True,
        )
        s.switch_to("1", json_output=True)
        path = temp_home / "gateway-export.json"
        export_accounts(s, str(path))
        original = s._get_sequence_data()["accounts"]["1"]
        data = s._get_sequence_data()
        data["accounts"] = {}
        data["sequence"] = []
        s._write_json(s.sequence_file, data)
        import_accounts(s, str(path))
        record = s._get_sequence_data()["accounts"]["1"]
        for field in ("kind", *gateway.RECORD_FIELDS):
            assert record.get(field) == original.get(field)
        assert s._read_account_credentials("1", record["email"]) == KEY

    def test_gateway_surfaces_render(self, gateway_switcher):
        from claude_swap.menubar import _account_display_usage
        from claude_swap.tui.widgets import account_card_text

        s = gateway_switcher
        s.add_account_from_token(KEY, base_url="https://gateway.example")
        row = s.accounts_snapshot().accounts[0]
        assert "gateway · gateway.example · bearer" in account_card_text(row, 100).plain
        assert "gateway ok" in account_card_text(row, 100).plain
        assert "gateway ok" in _account_display_usage(row.usage)

    def test_hidden_helper_only_active_slot(
        self, gateway_switcher, monkeypatch, capsys
    ):
        s = gateway_switcher
        s.add_account_from_token(KEY, base_url="https://gateway.example")
        monkeypatch.setattr(cli, "ClaudeAccountSwitcher", lambda: s)
        monkeypatch.setattr(sys, "argv", ["cswap", "gateway-credential", "1"])
        capsys.readouterr()
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 1
        assert capsys.readouterr() == ("", "")
        s.switch_to("1", json_output=True)
        cli.main()
        assert capsys.readouterr() == (KEY, "")


@pytest.mark.parametrize("replacement", ["url", "kind"])
def test_forced_import_refuses_active_gateway_replacement(gateway_switcher, temp_home, replacement):
    s = gateway_switcher
    s.add_account_from_token(KEY, base_url="https://gateway.example", slot=1)
    s.switch_to("1", json_output=True)
    path = temp_home / "gateway-export.json"
    export_accounts(s, str(path))
    envelope = json.loads(path.read_text())
    row = envelope["accounts"][0]
    if replacement == "url":
        row["baseUrl"] = "https://other.example"
    else:
        row["kind"] = "oauth"
        row["credentials"] = {"claudeAiOauth": {"accessToken": "import-access", "refreshToken": "import-refresh"}}
    path.write_text(json.dumps(envelope))
    sequence = s.sequence_file.read_bytes()
    settings = s._gateway_settings_path().read_bytes()
    credentials = s._read_account_credentials("1", row["email"])
    with pytest.raises(ValidationError, match="Switch away from the active gateway"):
        import_accounts(s, str(path), force=True)
    assert s.sequence_file.read_bytes() == sequence
    assert s._gateway_settings_path().read_bytes() == settings
    assert s._read_account_credentials("1", row["email"]) == credentials
    assert s.gateway_credential("1") == KEY


@pytest.mark.parametrize("replacement", ["url", "kind"])
def test_gateway_import_from_session_shell_refuses_default_replacement(gateway_switcher, temp_home, monkeypatch, replacement):
    from claude_swap.exceptions import SwitchError

    s = gateway_switcher
    s.add_account_from_token(KEY, slot=1, base_url="https://gateway.example")
    s.switch_to("1", json_output=True)
    path = temp_home / "replacement.json"
    export_accounts(s, str(path))
    envelope = json.loads(path.read_text())
    row = envelope["accounts"][0]
    if replacement == "url":
        row["baseUrl"] = "https://new.example"
    else:
        row["kind"] = "oauth"
        row["credentials"] = {"claudeAiOauth": {"accessToken": "sk-ant-oat01-dummy"}}
    path.write_text(json.dumps(envelope))
    settings_path = s._gateway_settings_path()
    settings, sequence = settings_path.read_bytes(), s.sequence_file.read_bytes()
    profile = s._session_dir("9", "session@example.com")
    profile.mkdir(parents=True)
    (profile / "settings.json").write_text("{}")
    with monkeypatch.context() as patch:
        patch.setenv("CLAUDE_CONFIG_DIR", str(profile))
        with pytest.raises(SwitchError, match="session profile"):
            import_accounts(s, str(path), force=True)
    assert settings_path.read_bytes() == settings
    assert s.sequence_file.read_bytes() == sequence
    assert s.gateway_credential("1") == KEY


@pytest.mark.parametrize("slot", [None, 1])
def test_gateway_add_token_rechecks_alias_under_write_lock(gateway_switcher, monkeypatch, slot):
    from claude_swap.exceptions import ConfigError

    s = gateway_switcher
    s.add_account_from_token(KEY, slot=1, email="a@example.com", base_url="https://a.example")
    s.add_account_from_token(KEY + "-b", slot=2, email="b@example.com", base_url="https://b.example")
    other = ClaudeAccountSwitcher()
    other.platform = Platform.LINUX
    check = s._alias_in_use
    assigned = []
    def race(*args, **kwargs):
        result = check(*args, **kwargs)
        if not assigned:
            assigned.append(True)
            other.set_alias("2", "claimed")
        return result
    monkeypatch.setattr(s, "_alias_in_use", race)
    with pytest.raises(ConfigError, match="already used"):
        s.add_account_from_token(KEY + "-new", slot=slot, email="a@example.com", base_url="https://new.example", alias="claimed")
    row, key = s.account_auth_snapshot("1", "a@example.com")
    assert (row["baseUrl"], key) == ("https://a.example", KEY)
    assert "alias" not in row
    assert s.resolve_account("claimed")[0] == "2"


def test_add_token_numeric_alias_is_validation_error(gateway_switcher, monkeypatch, capsys):
    with pytest.raises(ValidationError):
        gateway_switcher.add_account_from_token(KEY, base_url="https://gateway.example", alias="123")
    monkeypatch.setattr(cli, "ClaudeAccountSwitcher", lambda **kw: gateway_switcher)
    monkeypatch.setattr(sys, "stdin", io.StringIO(KEY + "\n"))
    monkeypatch.setattr(sys, "argv", ["cswap", "add-token", "-", "--base-url", "https://gateway.example", "--alias", "123"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    output = capsys.readouterr()
    assert "Error:" in output.err + output.out
    assert "Traceback" not in output.err + output.out


def test_forced_import_preflights_all_accounts_before_active_gateway_refusal(gateway_switcher, temp_home):
    from tests.test_gateway_activation import seed_oauth

    s = gateway_switcher
    seed_oauth(s)
    s.switch_to("2", json_output=True)
    path = temp_home / "mixed-export.json"
    export_accounts(s, str(path))
    envelope = json.loads(path.read_text())
    envelope["accounts"][0]["credentials"]["claudeAiOauth"]["refreshToken"] = "replacement-refresh"
    envelope["accounts"][1]["baseUrl"] = "https://other.example"
    path.write_text(json.dumps(envelope))
    credentials = {n: s.read_account_credentials(n, s.account_email(n)) for n in ("1", "2")}
    sequence = s.sequence_file.read_bytes()
    settings = s._gateway_settings_path().read_bytes()
    with pytest.raises(ValidationError, match="Switch away from the active gateway"):
        import_accounts(s, str(path), force=True)
    assert {n: s.read_account_credentials(n, s.account_email(n)) for n in ("1", "2")} == credentials
    assert s.sequence_file.read_bytes() == sequence
    assert s._gateway_settings_path().read_bytes() == settings


def test_gateway_collection_snapshots_endpoint_and_key_together(gateway_switcher, monkeypatch):
    s = gateway_switcher
    s.add_account_from_token(KEY, base_url="https://old.example", slot=1)
    infos = s._build_accounts_info()
    s.add_account_from_token(KEY + "-replacement", base_url="https://new.example", slot=1, assume_yes=True)
    pairs = []
    def probe(record, key, **kw):
        pairs.append((record["baseUrl"], key))
        return dict(probeStatus="ok", probedAt=kw["now"], budget=None)
    monkeypatch.setattr(gateway, "probe", probe)
    s._collect_usage_entries(infos)
    assert pairs == [("https://new.example", KEY + "-replacement")]


@pytest.mark.parametrize("initial_gateway", [True, False])
def test_gateway_export_snapshots_endpoint_and_key_together(gateway_switcher, temp_home, monkeypatch, initial_gateway):
    from claude_swap.locking import FileLock

    s = gateway_switcher
    original_key = KEY if initial_gateway else "sk-ant-api03-" + KEY
    replacement_key = "sk-ant-api03-" + KEY + "-replacement"
    s.add_account_from_token(original_key, base_url="https://old.example" if initial_gateway else None, slot=1)
    read = s._read_account_credentials
    attempted = []

    def racing_read(num, email):
        lock = FileLock(s.lock_file)
        acquired = lock.acquire(timeout=0)
        attempted.append(acquired)
        if acquired:
            try:
                data = s._get_sequence_data()
                data["accounts"][num].update(kind="gateway", baseUrl="https://new.example", auth="bearer", probeModel=gateway.DEFAULT_MODEL)
                s._write_json(s.sequence_file, data)
                s._write_account_credentials(num, email, replacement_key)
            finally:
                lock.release()
        return read(num, email)

    monkeypatch.setattr(s, "_read_account_credentials", racing_read)
    path = temp_home / "gateway-export.json"
    export_accounts(s, str(path))
    row = json.loads(path.read_text())["accounts"][0]
    assert attempted
    assert (row.get("baseUrl"), row["credentials"]) in [
        ("https://old.example" if initial_gateway else None, original_key),
        ("https://new.example", replacement_key),
    ]


@pytest.mark.parametrize("failure", ["malformed", "write"])
def test_last_gateway_remove_settings_failure_is_cli_error(gateway_switcher, monkeypatch, capsys, failure):
    s = gateway_switcher
    s.add_account_from_token(KEY, base_url="https://gateway.example", slot=1)
    path = s._gateway_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"env":' if failure == "malformed" else '{}')
    before = path.read_bytes()
    sequence = s.sequence_file.read_bytes()
    email = s.account_email("1")
    credentials = s._read_account_credentials("1", email)
    if failure == "write":
        def fail(*args, **kwargs):
            raise OSError(KEY)
        monkeypatch.setattr(gateway, "write_settings", fail)
    monkeypatch.setattr(cli, "ClaudeAccountSwitcher", lambda **kw: s)
    monkeypatch.setattr("builtins.input", lambda *args: "y")
    monkeypatch.setattr(sys, "argv", ["cswap", "--remove-account", "1"])
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    output = capsys.readouterr()
    assert "Error:" in output.err + output.out
    assert str(path) in output.err + output.out
    assert "Traceback" not in output.err + output.out
    assert KEY not in output.err + output.out
    assert s.sequence_file.read_bytes() == sequence
    assert path.read_bytes() == before
    assert s._read_account_credentials("1", email) == credentials


@pytest.mark.parametrize("operation", ["alias", "unalias", "threshold", "clear-threshold", "disable", "enable"])
@pytest.mark.parametrize("target", ["1", "2"])
def test_gateway_metadata_write_serializes_with_replacement(gateway_switcher, monkeypatch, operation, target):
    from claude_swap.locking import FileLock

    s = gateway_switcher
    s.add_account_from_token(KEY, base_url="https://old.example", slot=1)
    s.add_account_from_token("sk-ant-oat01-dummy", slot=2)
    data = s._get_sequence_data()
    data["accounts"][target].update(alias="original", threshold=50, disabled=operation == "enable")
    s._write_json(s.sequence_file, data)
    write = s._write_json
    attempted = []

    def racing_write(path, value):
        if path == s.sequence_file and not attempted:
            lock = FileLock(s.lock_file)
            acquired = lock.acquire(timeout=0)
            attempted.append(acquired)
            if acquired:
                try:
                    latest = s._get_sequence_data()
                    latest["accounts"]["1"]["baseUrl"] = "https://new.example"
                    write(path, latest)
                    s._write_account_credentials("1", latest["accounts"]["1"]["email"], KEY + "-replacement")
                finally:
                    lock.release()
        return write(path, value)

    monkeypatch.setattr(s, "_write_json", racing_write)
    if operation == "alias":
        s.set_alias(target, "changed")
    elif operation == "unalias":
        s.unset_alias(target)
    elif "threshold" in operation:
        s.set_account_threshold(target, None if operation == "clear-threshold" else 60)
    else:
        s.set_account_disabled(target, operation == "disable")
    row, key = s.account_auth_snapshot("1", s.account_email("1"))
    assert attempted
    assert (row["baseUrl"], key) in [("https://old.example", KEY), ("https://new.example", KEY + "-replacement")]


def test_gateway_migration_holds_roster_lock(gateway_switcher, monkeypatch):
    from claude_swap.locking import FileLock

    s = gateway_switcher
    s.add_account_from_token(KEY, base_url="https://old.example", slot=1)
    s.add_account_from_token("sk-ant-oat01-dummy", slot=2)
    data = s._get_sequence_data()
    del data["accounts"]["2"]["organizationUuid"]
    s._write_json(s.sequence_file, data)
    read = s._read_account_config
    attempted = []
    def racing_read(num, email):
        lock = FileLock(s.lock_file)
        acquired = lock.acquire(timeout=0)
        attempted.append(acquired)
        if acquired:
            try:
                latest = s._get_sequence_data()
                latest["accounts"]["1"]["baseUrl"] = "https://new.example"
                s._write_json(s.sequence_file, latest)
                s._write_account_credentials("1", latest["accounts"]["1"]["email"], KEY + "-replacement")
            finally:
                lock.release()
        return read(num, email)
    monkeypatch.setattr(s, "_read_account_config", racing_read)
    s._get_sequence_data_migrated()
    row, key = s.account_auth_snapshot("1", s.account_email("1"))
    assert attempted
    assert (row["baseUrl"], key) in [("https://old.example", KEY), ("https://new.example", KEY + "-replacement")]


def test_gateway_roster_initialization_holds_lock(gateway_switcher, monkeypatch):
    from claude_swap.locking import FileLock

    s = gateway_switcher
    s.add_account_from_token(KEY, base_url="https://gateway.example", slot=1)
    populated = s._get_sequence_data()
    s.sequence_file.unlink()
    write = s._write_json
    attempted = []
    def racing_write(path, data):
        lock = FileLock(s.lock_file)
        acquired = lock.acquire(timeout=0)
        attempted.append(acquired)
        if acquired:
            try:
                write(path, populated)
            finally:
                lock.release()
        return write(path, data)
    monkeypatch.setattr(s, "_write_json", racing_write)
    s._init_sequence_file()
    assert attempted == [False] or "1" in s._get_sequence_data()["accounts"]


def test_gateway_add_token_rechecks_identity_after_slot_swap(gateway_switcher, monkeypatch):
    from claude_swap.exceptions import ConfigError

    s = gateway_switcher
    s.add_account_from_token(KEY, base_url="https://a.example", slot=1, email="a@example.com")
    s.add_account_from_token(KEY + "-b", base_url="https://b.example", slot=2, email="b@example.com")
    check = s._refuse_active_gateway_replacement
    swapped = []
    def race(data, *slots):
        check(data, *slots)
        if not swapped:
            swapped.append(True)
            s.swap_accounts("1", "2")
    monkeypatch.setattr(s, "_refuse_active_gateway_replacement", race)
    with pytest.raises(ConfigError, match="changed|retry"):
        s.add_account_from_token(KEY + "-new", base_url="https://new.example", email="a@example.com")
    row, key = s.account_auth_snapshot("1", "b@example.com")
    assert (row["baseUrl"], key) == ("https://b.example", KEY + "-b")
    row, key = s.account_auth_snapshot("2", "a@example.com")
    assert (row["baseUrl"], key) == ("https://a.example", KEY)


@pytest.mark.parametrize("slot", [None, 1])
@pytest.mark.parametrize("token", ["sk-ant-oat01-dummy", "sk-ant-api03-dummy"])
def test_gateway_add_token_rechecks_kind_after_initial_validation(gateway_switcher, monkeypatch, slot, token):
    s = gateway_switcher
    email = "same@example.com"
    s.add_account_from_token(token, email=email, slot=1)
    other = ClaudeAccountSwitcher()
    other.platform = Platform.LINUX
    check = s._reject_cross_kind_collision
    replaced = []

    def race(*args, **kwargs):
        check(*args, **kwargs)
        if not replaced:
            replaced.append(True)
            other.remove_account("1", assume_yes=True)
            other.add_account_from_token(KEY, email=email, slot=1, base_url="https://gateway.example")

    monkeypatch.setattr(s, "_reject_cross_kind_collision", race)
    with pytest.raises(ValidationError, match="already exists"):
        s.add_account_from_token(token + "-new", email=email, slot=slot)
    row, key = s.account_auth_snapshot("1", email)
    assert (row["kind"], row["baseUrl"], key) == ("gateway", "https://gateway.example", KEY)
    assert not s._account_write_marker("1").exists()


@pytest.mark.parametrize("operation", ["refresh", "slot", "import"])
@pytest.mark.parametrize("failure", ["config", "cache", "sequence", "after-sequence"])
def test_gateway_failed_replacement_refuses_credentials_until_retry(gateway_switcher, temp_home, monkeypatch, operation, failure):
    from claude_swap.exceptions import ConfigError

    s = gateway_switcher
    s.add_account_from_token(KEY, base_url="https://old.example", slot=1, email="a@example.com")
    s.add_account_from_token(KEY + "-control", base_url="https://control.example", slot=2)
    path = temp_home / "replacement.json"
    export_accounts(s, str(path), account="1")
    envelope = json.loads(path.read_text())
    envelope["accounts"][0].update(baseUrl="https://new.example", credentials=KEY + "-replacement")
    path.write_text(json.dumps(envelope))
    def replace():
        if operation == "import":
            import_accounts(s, str(path), force=True)
        else:
            s.add_account_from_token(KEY + "-replacement", base_url="https://new.example", email="a@example.com", slot=1 if operation == "slot" else None)
    def fail(*args, **kwargs):
        raise OSError(KEY)
    with monkeypatch.context() as patch:
        if failure == "config":
            patch.setattr(s, "_write_account_config", fail)
        elif failure == "cache":
            patch.setattr(s._usage_store, "clear_dead_token", fail)
        else:
            write = s._write_json
            def fail_commit(file, data):
                if file == s.sequence_file and data.get("accounts", {}).get("1", {}).get("baseUrl") == "https://new.example":
                    if failure == "after-sequence":
                        write(file, data)
                    fail()
                return write(file, data)
            patch.setattr(s, "_write_json", fail_commit)
        with pytest.raises((OSError, ConfigError)):
            replace()
    fresh = ClaudeAccountSwitcher()
    fresh.platform = Platform.LINUX
    assert fresh._read_account_credentials("1", "a@example.com") == ""
    assert fresh._read_account_credentials("2", fresh.account_email("2")) == KEY + "-control"
    assert fresh.usage_entries_by_account()["1"].gateway["probeStatus"] == "transient"
    with pytest.raises(ConfigError, match="incomplete.*retry"):
        fresh.resolve_account("1")
    replace()
    row, key = fresh.account_auth_snapshot("1", "a@example.com")
    assert (row["baseUrl"], key) == ("https://new.example", KEY + "-replacement")


@pytest.mark.parametrize("failure", ["create", "clear", "read"])
def test_gateway_replacement_marker_errors_fail_closed(gateway_switcher, monkeypatch, failure):
    from pathlib import Path
    from claude_swap.exceptions import ConfigError
    from claude_swap import switcher as module

    s = gateway_switcher
    s.add_account_from_token(KEY, base_url="https://old.example", slot=1)
    email = s.account_email("1")
    marker = s._account_write_marker("1")
    with monkeypatch.context() as patch:
        if failure == "create":
            write = module.atomic_write_text
            def fail_write(path, text, *args, **kwargs):
                if path == marker:
                    raise OSError(KEY)
                return write(path, text, *args, **kwargs)
            patch.setattr(module, "atomic_write_text", fail_write)
        elif failure == "clear":
            unlink = Path.unlink
            def fail_unlink(path, *args, **kwargs):
                if path == marker:
                    raise OSError(KEY)
                return unlink(path, *args, **kwargs)
            patch.setattr(Path, "unlink", fail_unlink)
        else:
            stat = Path.stat
            def fail_stat(path, *args, **kwargs):
                if path == marker:
                    raise PermissionError(KEY)
                return stat(path, *args, **kwargs)
            patch.setattr(Path, "stat", fail_stat)
            assert s._read_account_credentials("1", email) == ""
            with pytest.raises(ConfigError, match="incomplete.*retry"):
                s.resolve_account("1")
            return
        with pytest.raises(ConfigError, match="incomplete.*retry") as error:
            s.add_account_from_token(KEY + "-new", base_url="https://new.example", slot=1)
        assert KEY not in str(error.value)
    assert s._read_account_credentials("1", email) == (KEY if failure == "create" else "")
    s.add_account_from_token(KEY + "-new", base_url="https://new.example", slot=1)
    assert not marker.exists()


@pytest.mark.parametrize("operation", ["swap", "move", "empty-move", "remove", "migration-retry"])
def test_gateway_pending_replacement_relocation_and_recovery(gateway_switcher, monkeypatch, operation):
    from claude_swap.exceptions import ConfigError

    s = gateway_switcher
    s.add_account_from_token(KEY, base_url="https://old.example", slot=1, email="a@example.com")
    s.add_account_from_token(KEY + "-b", base_url="https://b.example", slot=2)
    pending = "3" if operation == "empty-move" else "1"
    if operation == "migration-retry":
        with monkeypatch.context() as patch:
            def fail(*args):
                raise OSError(KEY)
            patch.setattr(s, "_write_account_config", fail)
            with pytest.raises(ConfigError):
                s.add_account_from_token(KEY + "-new", base_url="https://new.example", email="a@example.com", slot=3)
        assert s._account_write_pending("1") and s._account_write_pending("3")
        s.add_account_from_token(KEY + "-new", base_url="https://new.example", email="a@example.com", slot=3)
        assert not s._account_write_pending("1") and not s._account_write_pending("3")
        return
    s._account_write_marker(pending).write_text(json.dumps({"slots": [pending]}))
    before = s.sequence_file.read_bytes()
    if operation == "remove":
        s.remove_account("1", assume_yes=True)
        assert not s._account_write_pending("1")
        s.add_account_from_token(KEY + "-new", base_url="https://new.example", slot=1)
        assert s._read_account_credentials("1", s.account_email("1")) == KEY + "-new"
    else:
        with pytest.raises(ConfigError, match="incomplete.*retry"):
            s.swap_accounts("2", pending) if operation == "swap" else s.move_account("2", pending)
        assert s.sequence_file.read_bytes() == before


@pytest.mark.parametrize("failure", ["crash", "rollback", "success"])
def test_gateway_overlapping_swap_refuses_partial_credentials(gateway_switcher, monkeypatch, failure):
    import subprocess
    from claude_swap.exceptions import ConfigError

    s = gateway_switcher
    email = "same@example.com"
    s.add_account_from_token(KEY, email=email, slot=1, base_url="https://a.example")
    data = s._get_sequence_data()
    data["accounts"]["1"]["organizationUuid"] = "org-a"
    s._write_json(s.sequence_file, data)
    s.add_account_from_token(KEY + "-b", email=email, slot=2, base_url="https://b.example")
    s.add_account_from_token(KEY + "-c", slot=3, base_url="https://c.example")
    if failure == "crash":
        child = subprocess.run([sys.executable, "-c", """
import os
from claude_swap.models import Platform
from claude_swap.switcher import ClaudeAccountSwitcher
s = ClaudeAccountSwitcher()
s.platform = Platform.LINUX
write = s._write_account_credentials
def stop_after_write(*args):
    write(*args)
    os._exit(77)
s._write_account_credentials = stop_after_write
s.swap_accounts('1', '2')
"""], capture_output=True, timeout=20)
        assert child.returncode == 77
    elif failure == "rollback":
        write = s._write_account_credentials
        def fail_after_write(*args):
            write(*args)
            raise OSError("injected persistent write failure")
        with monkeypatch.context() as patch:
            patch.setattr(s, "_write_account_credentials", fail_after_write)
            with pytest.raises(ConfigError, match="incomplete.*retry"):
                s.swap_accounts("1", "2")
    else:
        s.swap_accounts("1", "2")
    fresh = ClaudeAccountSwitcher()
    fresh.platform = Platform.LINUX
    for num, endpoint, key in [("1", "https://b.example", KEY + "-b"), ("2", "https://a.example", KEY)]:
        record, actual = fresh.account_auth_snapshot(num, email)
        if failure == "success":
            assert (record["baseUrl"], actual) == (endpoint, key)
            assert not fresh._account_write_pending(num)
        else:
            assert actual == ""
            with pytest.raises(ConfigError, match="incomplete.*retry"):
                fresh.resolve_account(num)
    assert fresh._read_account_credentials("3", fresh.account_email("3")) == KEY + "-c"
