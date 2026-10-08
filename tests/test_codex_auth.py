"""Tests for codex_auth (Codex credential/usage/refresh helpers) and the
shape dispatch that routes Codex blobs through oauth.py's helpers."""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import pytest

from claude_swap import __version__, codex_auth, oauth
from tests import codex_fixtures as cf
from tests import conftest

NOW = int(datetime.now(timezone.utc).timestamp())
_live_network = pytest.mark.no_codex_network_fake


class _Recorder:
    """``urlopen`` stand-in that records requests and replays ``responses``
    in order (an exception instance is raised instead of returned)."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append((req, timeout))
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.fixture
def urlopen(monkeypatch):
    def install(*responses):
        rec = _Recorder(*responses)
        monkeypatch.setattr("claude_swap.codex_auth.urllib.request.urlopen", rec)
        return rec

    return install


# --------------------------------------------------------------------- JWT


class TestDecodeJwtPayload:
    def test_round_trip(self):
        assert codex_auth.decode_jwt_payload(cf.fake_jwt({"a": 1, "b": "x"})) == {
            "a": 1, "b": "x",
        }

    @pytest.mark.parametrize("value", ["a", "ab", "abc", "abcd", "abcde"])
    def test_every_padding_length_decodes(self, value):
        # base64url segments arrive with their "=" padding stripped; each of
        # these lands on a different payload length mod 4.
        assert codex_auth.decode_jwt_payload(cf.fake_jwt({"v": value})) == {"v": value}

    @pytest.mark.parametrize(
        "token",
        [
            "",
            "onlyonepart",
            "two.parts",
            "head..sig",  # empty payload
            "head.!!!notbase64!!!.sig",
            "head." + "bm90IGpzb24" + ".sig",  # base64("not json")
            "head." + "WzEsMl0" + ".sig",  # base64("[1,2]") -> not an object
        ],
    )
    def test_malformed_is_none(self, token):
        assert codex_auth.decode_jwt_payload(token) is None

    def test_non_string_is_none(self):
        assert codex_auth.decode_jwt_payload(None) is None  # type: ignore[arg-type]


# ------------------------------------------------------------ blob shapes


class TestBlobShape:
    @pytest.mark.parametrize(
        "data",
        [
            {"tokens": {}},
            {"OPENAI_API_KEY": "sk"},
            {"OPENAI_API_KEY": None, "auth_mode": "chatgpt"},
            {"auth_mode": "apikey"},
        ],
    )
    def test_codex_shapes(self, data):
        assert codex_auth.is_codex_blob(data) is True

    @pytest.mark.parametrize(
        "data",
        [
            {"claudeAiOauth": {"accessToken": "a"}},
            {"claudeAiOauth": {}, "tokens": {}},  # Claude marker wins
            {},
            [],
            "string",
            None,
        ],
    )
    def test_non_codex_shapes(self, data):
        assert codex_auth.is_codex_blob(data) is False

    def test_parse_blob(self):
        assert codex_auth.parse_blob(cf.auth_json())["auth_mode"] == "chatgpt"
        assert codex_auth.parse_blob(json.dumps({"claudeAiOauth": {}})) is None
        assert codex_auth.parse_blob("not json") is None
        assert codex_auth.parse_blob("") is None


class TestApiKeyBlob:
    def test_make_api_key_blob_uses_codex_literal(self):
        blob = codex_auth.make_api_key_blob("sk-proj-abc")
        assert json.loads(blob) == {"auth_mode": "apikey", "OPENAI_API_KEY": "sk-proj-abc"}
        assert codex_auth.is_api_key_blob(blob)
        assert codex_auth.is_codex_blob(json.loads(blob))

    def test_codex_written_api_key_file(self):
        assert codex_auth.is_api_key_blob(cf.api_key_json())

    def test_chatgpt_blob_is_not_api_key(self):
        assert not codex_auth.is_api_key_blob(cf.auth_json())

    def test_chatgpt_mode_with_exchanged_key_is_not_api_key(self):
        # Browser login may store an exchanged OPENAI_API_KEY next to the
        # tokens; auth_mode decides, exactly as Codex resolves it.
        data = cf.auth_dict()
        data["OPENAI_API_KEY"] = "sk-exchanged"
        assert not codex_auth.is_api_key_blob(json.dumps(data))

    def test_mode_absent_falls_back_to_key_presence(self):
        assert codex_auth.is_api_key_blob(json.dumps({"OPENAI_API_KEY": "sk"}))
        assert not codex_auth.is_api_key_blob(
            json.dumps({"OPENAI_API_KEY": None, "tokens": cf.auth_dict()["tokens"]})
        )

    def test_garbage_is_not_api_key(self):
        assert not codex_auth.is_api_key_blob("nope")


# --------------------------------------------------------------- identity


class TestIdentity:
    def test_personal(self):
        ident = codex_auth.identity(cf.auth_json())
        assert ident == {
            "email": "user@example.com",
            "uuid": "user-1",
            "organizationUuid": "acct-personal",
            "planType": "plus",
        }

    def test_workspace_of_same_person_has_distinct_key(self):
        personal = codex_auth.identity(cf.auth_json())
        workspace = codex_auth.identity(cf.auth_json(account_id="acct-team", plan="team"))
        assert workspace["planType"] == "team"
        assert workspace["email"] == personal["email"]
        assert workspace["uuid"] == personal["uuid"]
        assert workspace["organizationUuid"] == "acct-team"
        assert workspace["organizationUuid"] != personal["organizationUuid"]

    def test_stored_account_id_wins_over_claim(self):
        data = cf.auth_dict(account_id="acct-claim")
        data["tokens"]["account_id"] = "acct-stored"
        assert codex_auth.identity(json.dumps(data))["organizationUuid"] == "acct-stored"

    def test_claim_is_the_fallback_account_id(self):
        blob = cf.auth_json(account_id="acct-claim", store_account_id=False)
        assert codex_auth.identity(blob)["organizationUuid"] == "acct-claim"

    def test_profile_claim_email_and_user_id_fallbacks(self):
        data = cf.auth_dict()
        data["tokens"]["id_token"] = cf.fake_jwt({
            cf.PROFILE_CLAIM: {"email": "p@example.com"},
            cf.AUTH_CLAIM: {"user_id": "legacy-user", "chatgpt_account_id": "x"},
        })
        ident = codex_auth.identity(json.dumps(data))
        assert ident["email"] == "p@example.com"
        assert ident["uuid"] == "legacy-user"
        assert ident["planType"] is None

    def test_api_key_is_none(self):
        assert codex_auth.identity(cf.api_key_json()) is None

    def test_malformed_id_token_is_none(self):
        data = cf.auth_dict()
        data["tokens"]["id_token"] = "garbage"
        assert codex_auth.identity(json.dumps(data)) is None

    @pytest.mark.parametrize(
        "creds", ["", "not json", json.dumps({"claudeAiOauth": {}}), '{"tokens": "x"}']
    )
    def test_non_codex_or_broken_is_none(self, creds):
        assert codex_auth.identity(creds) is None


class TestOauthView:
    def test_view_fields(self):
        data = cf.auth_dict(access_exp=NOW + 600)
        view = codex_auth.oauth_view(json.dumps(data))
        assert view == {
            "accessToken": data["tokens"]["access_token"],
            "refreshToken": data["tokens"]["refresh_token"],
            "expiresAt": (NOW + 600) * 1000,
            "idToken": data["tokens"]["id_token"],
            "accountId": "acct-personal",
        }

    def test_undecodable_access_token_has_unknown_expiry(self):
        view = codex_auth.oauth_view(cf.auth_json(access="opaque-token"))
        assert view["accessToken"] == "opaque-token"
        assert view["expiresAt"] is None

    def test_account_id_falls_back_to_claim(self):
        view = codex_auth.oauth_view(cf.auth_json(account_id="acct-c", store_account_id=False))
        assert view["accountId"] == "acct-c"

    def test_api_key_and_garbage_are_none(self):
        assert codex_auth.oauth_view(cf.api_key_json()) is None
        assert codex_auth.oauth_view("nope") is None
        assert codex_auth.oauth_view(json.dumps({"claudeAiOauth": {}})) is None


def test_now_rfc3339_matches_codex_format():
    stamp = codex_auth.now_rfc3339()
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z", stamp)
    parsed = datetime.fromisoformat(stamp)
    assert abs(parsed.timestamp() - datetime.now(timezone.utc).timestamp()) < 5


# ---------------------------------------------------------------- refresh


@_live_network
class TestTryRefresh:
    def _success(self, urlopen, creds, **resp):
        body = {
            "id_token": cf.id_token(email="user@example.com", account_id="acct-personal"),
            "access_token": "new-access",
            "refresh_token": "new-refresh",
        }
        body.update(resp)
        body = {k: v for k, v in body.items() if v is not None}
        rec = urlopen(cf.FakeResponse(body))
        return rec, codex_auth.try_refresh(creds, timeout_s=3.0)

    def test_success_replaces_tokens_and_stamps_last_refresh(self, urlopen):
        creds = cf.auth_json()
        rec, outcome = self._success(urlopen, creds)

        assert outcome.error is None
        new = json.loads(outcome.credentials)
        assert new["tokens"]["access_token"] == "new-access"
        assert new["tokens"]["refresh_token"] == "new-refresh"
        assert new["tokens"]["account_id"] == "acct-personal"
        assert new["last_refresh"] != cf.LAST_REFRESH
        assert re.fullmatch(r".+\.\d{6}Z", new["last_refresh"])
        assert new["auth_mode"] == "chatgpt"
        assert outcome.token_account == {
            "uuid": "user-1",
            "email": "user@example.com",
            "organizationUuid": "acct-personal",
        }

        req, timeout = rec.requests[0]
        assert timeout == 3.0
        assert req.full_url == "https://auth.openai.com/oauth/token"
        assert req.get_method() == "POST"
        assert req.get_header("Content-type") == "application/json"
        assert json.loads(req.data) == {
            "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
            "grant_type": "refresh_token",
            "refresh_token": "rt-user-1-acct-personal",
        }

    def test_absent_response_fields_keep_stored_values(self, urlopen):
        creds = cf.auth_json()
        old = json.loads(creds)["tokens"]
        _, outcome = self._success(urlopen, creds, id_token=None, refresh_token=None)
        new = json.loads(outcome.credentials)["tokens"]
        assert new["access_token"] == "new-access"
        assert new["refresh_token"] == old["refresh_token"]
        assert new["id_token"] == old["id_token"]

    @pytest.mark.parametrize("pretty", [True, False])
    def test_serialization_style_round_trips(self, urlopen, pretty):
        creds = cf.auth_json(pretty=pretty)
        _, outcome = self._success(urlopen, creds)
        assert ("\n" in outcome.credentials) is pretty
        expected = json.loads(creds)
        new = json.loads(outcome.credentials)
        assert list(new) == list(expected)  # key order preserved
        assert list(new["tokens"]) == list(expected["tokens"])

    def test_rotated_refresh_token_alone_is_kept(self, urlopen):
        # The old refresh token is spent server-side the moment the grant
        # succeeds; dropping a reply that carries only its successor would
        # leave the slot holding a token the next refresh gets
        # refresh_token_reused for.
        creds = cf.auth_json()
        old = json.loads(creds)["tokens"]
        _, outcome = self._success(urlopen, creds, id_token=None, access_token=None)
        assert outcome.error is None
        new = json.loads(outcome.credentials)
        assert new["tokens"]["refresh_token"] == "new-refresh"
        assert new["tokens"]["access_token"] == old["access_token"]
        assert new["last_refresh"] != cf.LAST_REFRESH

    @pytest.mark.parametrize("body", [{}, {"token_type": "Bearer"}, [1], "str"])
    def test_reply_without_any_token_is_transient(self, urlopen, body):
        urlopen(cf.FakeResponse(json.dumps(body).encode()))
        outcome = codex_auth.try_refresh(cf.auth_json())
        assert outcome == oauth.RefreshOutcome(None, "transient")

    @pytest.mark.parametrize("rotate_id_token", [True, False])
    def test_missing_account_id_is_backfilled_from_id_token(self, urlopen, rotate_id_token):
        # Every ChatGPT-mode auth.json cswap writes must carry
        # tokens.account_id: Codex's guarded refresh fails without it.
        creds = cf.auth_json(account_id="acct-old", store_account_id=False)
        new_id = cf.id_token(account_id="acct-new") if rotate_id_token else None
        _, outcome = self._success(urlopen, creds, id_token=new_id)
        expected = "acct-new" if rotate_id_token else "acct-old"
        assert json.loads(outcome.credentials)["tokens"]["account_id"] == expected

    def test_stored_account_id_is_never_rewritten(self, urlopen):
        _, outcome = self._success(
            urlopen, cf.auth_json(account_id="acct-a"), id_token=cf.id_token(account_id="acct-b")
        )
        assert json.loads(outcome.credentials)["tokens"]["account_id"] == "acct-a"

    @pytest.mark.parametrize(
        "status,body,expected",
        [
            (400, {"error": "invalid_grant"}, "invalid_grant"),
            (400, {"error": "refresh_token_expired"}, "invalid_grant"),
            (400, {"error": {"code": "refresh_token_reused"}}, "invalid_grant"),
            (400, {"code": "refresh_token_invalidated"}, "invalid_grant"),
            (400, {"error": "REFRESH_TOKEN_EXPIRED"}, "invalid_grant"),
            (401, b"", "invalid_grant"),
            (401, {"error": "something_else"}, "invalid_grant"),
            (400, {"error": "invalid_client"}, "invalid_client"),
            (401, {"error": "invalid_client"}, "invalid_client"),
            (400, {"error": "temporarily_unavailable"}, "transient"),
            (400, b"<html>bad gateway</html>", "transient"),
            # 403 classifies by code exactly as Claude's refresh does.
            (403, {"error": "invalid_grant"}, "invalid_grant"),
            (403, {"error": "refresh_token_reused"}, "invalid_grant"),
            (403, {"error": "invalid_client"}, "invalid_client"),
            (403, {"error": "forbidden"}, "transient"),
            (403, b"", "transient"),
            (500, {"error": "invalid_grant"}, "transient"),
        ],
    )
    def test_http_error_mapping(self, urlopen, status, body, expected):
        urlopen(cf.http_error(status, body))
        outcome = codex_auth.try_refresh(cf.auth_json())
        assert outcome.credentials is None
        assert outcome.error == expected

    def test_network_error_is_transient(self, urlopen):
        urlopen(urllib.error.URLError("dns"))
        assert codex_auth.try_refresh(cf.auth_json()).error == "transient"

    def test_missing_refresh_token(self, urlopen):
        data = cf.auth_dict()
        del data["tokens"]["refresh_token"]
        rec = urlopen()
        assert codex_auth.try_refresh(json.dumps(data)).error == "no_refresh_token"
        assert codex_auth.try_refresh(cf.api_key_json()).error == "no_refresh_token"
        assert rec.requests == []

    def test_unparseable_is_transient(self, urlopen):
        rec = urlopen()  # the class opts out of the network stub
        assert codex_auth.try_refresh("not json").error == "transient"
        assert codex_auth.try_refresh("[1]").error == "transient"
        assert rec.requests == []


# ------------------------------------------------------------------ usage


@_live_network
class TestRequestUsage:
    def test_default_endpoint_and_headers(self, urlopen):
        rec = urlopen(cf.FakeResponse({"ok": 1}))
        data = cf.auth_dict()
        assert codex_auth.request_usage(json.dumps(data), timeout_s=4.0) == {"ok": 1}

        req, timeout = rec.requests[0]
        assert timeout == 4.0
        assert req.full_url == "https://chatgpt.com/backend-api/wham/usage"
        assert req.get_method() == "GET"
        assert req.get_header("Authorization") == f"Bearer {data['tokens']['access_token']}"
        assert req.get_header("Chatgpt-account-id") == "acct-personal"
        assert req.get_header("User-agent") == f"claude-swap/{__version__}"
        assert req.get_header("Accept") == "application/json"

    @pytest.mark.parametrize(
        "base,url",
        [
            ("https://chatgpt.com/backend-api/", "https://chatgpt.com/backend-api/wham/usage"),
            ("https://corp.example/backend-api", "https://corp.example/backend-api/wham/usage"),
            ("https://proxy.example.com/", "https://proxy.example.com/api/codex/usage"),
            ("https://chatgpt.com", "https://chatgpt.com/backend-api/wham/usage"),
            ("https://chat.openai.com/", "https://chat.openai.com/backend-api/wham/usage"),
        ],
    )
    def test_base_url_path_rule(self, urlopen, base, url):
        rec = urlopen(cf.FakeResponse({}))
        codex_auth.request_usage(cf.auth_json(), base_url=base)
        assert rec.requests[0][0].full_url == url

    def test_http_error_raises_and_classifies_like_claude(self, urlopen):
        urlopen(cf.http_error(429, b"", headers={"Retry-After": "30"}))
        with pytest.raises(urllib.error.HTTPError) as exc:
            codex_auth.request_usage(cf.auth_json())
        assert oauth._classify_usage_error(exc.value) == ("http-429", 30.0)

    def test_no_access_token_raises(self, urlopen):
        rec = urlopen()
        with pytest.raises(ValueError):
            codex_auth.request_usage(cf.api_key_json())
        assert rec.requests == []


class TestBuildUsageResult:
    @staticmethod
    def _entry(pct, reset_at):
        iso = datetime.fromtimestamp(reset_at, tz=timezone.utc).isoformat()
        countdown, clock = oauth.format_reset(iso)
        return {"pct": float(pct), "resets_at": iso, "countdown": countdown, "clock": clock}

    def test_primary_five_hour_secondary_weekly(self):
        data = cf.wham_usage((12, 18000, NOW + 3600), (40, 604800, NOW + 86400 * 3))
        assert codex_auth.build_usage_result(data) == {
            "five_hour": self._entry(12, NOW + 3600),
            "seven_day": self._entry(40, NOW + 86400 * 3),
        }

    def test_buckets_follow_duration_not_position(self):
        data = cf.wham_usage((40, 604800, NOW + 86400 * 3), (12, 18000, NOW + 3600))
        result = codex_auth.build_usage_result(data)
        assert result["five_hour"]["pct"] == 12.0
        assert result["seven_day"]["pct"] == 40.0

    def test_only_weekly(self):
        result = codex_auth.build_usage_result(cf.wham_usage((7, 604800, NOW + 1000)))
        assert set(result) == {"seven_day"}
        assert result["seven_day"]["pct"] == 7.0

    def test_both_short_keeps_higher(self):
        data = cf.wham_usage((30, 18000, NOW + 100), (55, 86400, NOW + 200))
        result = codex_auth.build_usage_result(data)
        assert set(result) == {"five_hour"}
        assert result["five_hour"] == self._entry(55, NOW + 200)

    def test_missing_duration_falls_back_to_position(self):
        data = cf.wham_usage((20, 18000, NOW + 60), (70, 604800, NOW + 600))
        for key in ("primary_window", "secondary_window"):
            del data["rate_limit"][key]["limit_window_seconds"]
        result = codex_auth.build_usage_result(data)
        assert result["five_hour"]["pct"] == 20.0
        assert result["seven_day"]["pct"] == 70.0

    def test_non_numeric_duration_falls_back_to_position(self):
        data = cf.wham_usage(None, (70, 604800, NOW + 600))
        data["rate_limit"]["secondary_window"]["limit_window_seconds"] = "604800"
        assert set(codex_auth.build_usage_result(data)) == {"seven_day"}

    def test_window_without_reset_at_has_pct_only(self):
        data = cf.wham_usage((33, 18000, NOW + 60))
        del data["rate_limit"]["primary_window"]["reset_at"]
        assert codex_auth.build_usage_result(data) == {"five_hour": {"pct": 33.0}}
        # Downstream still sees the window, with an unknown reset.
        assert oauth.relevant_windows({"five_hour": {"pct": 33.0}}) == [("5h", 33.0, None)]

    def test_resets_at_is_readable_by_downstream_parsers(self):
        result = codex_auth.build_usage_result(cf.wham_usage((1, 18000, NOW + 60)))
        assert datetime.fromisoformat(result["five_hour"]["resets_at"]).timestamp() == NOW + 60
        assert oauth.relevant_windows(result) == [
            ("5h", 1.0, result["five_hour"]["resets_at"])
        ]

    @pytest.mark.parametrize(
        "data",
        [
            {},
            {"plan_type": "plus"},
            {"rate_limit": None},
            cf.wham_usage(None, None),
        ],
    )
    def test_no_window_data_is_none(self, data):
        assert codex_auth.build_usage_result(data) is None


@_live_network
class TestFetchWorkspaceName:
    def _accounts(self, *accounts):
        return cf.FakeResponse({"accounts": list(accounts), "default_account_id": None})

    def test_matching_account_name(self, urlopen):
        rec = urlopen(self._accounts(
            {"id": "acct-other", "name": "Other"},
            {"id": "acct-team", "name": "Acme Team", "plan_type": "team"},
        ))
        creds = cf.auth_json(account_id="acct-team", plan="team")
        assert codex_auth.fetch_workspace_name(creds) == "Acme Team"
        req = rec.requests[0][0]
        assert req.full_url == "https://chatgpt.com/backend-api/wham/accounts/check"
        assert req.get_header("Chatgpt-account-id") == "acct-team"

    def test_codex_api_path_style(self, urlopen):
        rec = urlopen(self._accounts())
        codex_auth.fetch_workspace_name(cf.auth_json(), base_url="https://proxy.example")
        assert rec.requests[0][0].full_url == "https://proxy.example/api/codex/accounts/check"

    @pytest.mark.parametrize(
        "response",
        [
            cf.FakeResponse({"accounts": [{"id": "acct-x", "name": "X"}]}),
            cf.FakeResponse({"accounts": [{"id": "acct-personal", "name": ""}]}),
            cf.FakeResponse({"accounts": "nope"}),
            cf.FakeResponse(b"not json"),
            cf.http_error(500),
            urllib.error.URLError("dns"),
        ],
    )
    def test_failures_are_none(self, urlopen, response):
        urlopen(response)
        assert codex_auth.fetch_workspace_name(cf.auth_json()) is None

    def test_api_key_is_none_without_request(self, urlopen):
        rec = urlopen()
        assert codex_auth.fetch_workspace_name(cf.api_key_json()) is None
        assert rec.requests == []


# ------------------------------------------------------- oauth.py dispatch


class TestOauthShapeDispatch:
    def test_extract_helpers(self):
        creds = cf.auth_json(access_exp=NOW + 600)
        view = codex_auth.oauth_view(creds)
        assert oauth.extract_oauth_data(creds) == view
        assert oauth.extract_access_token(creds) == view["accessToken"]

    def test_fingerprints_match_claude_format(self):
        creds = cf.auth_json(refresh_token="rt-x")
        assert oauth.credential_fingerprint(creds) == (
            "sha256:" + hashlib.sha256(b"rt-x").hexdigest()
        )
        at = json.loads(creds)["tokens"]["access_token"]
        assert oauth.access_token_fingerprint(creds) == (
            "sha256-at:" + hashlib.sha256(at.encode()).hexdigest()
        )

    def test_fingerprint_is_stable_across_access_token_rotation(self):
        a = cf.auth_json(refresh_token="rt-1", access=cf.access_token(nonce="1"))
        b = cf.auth_json(refresh_token="rt-1", access=cf.access_token(nonce="2"))
        assert oauth.credential_fingerprint(a) == oauth.credential_fingerprint(b)
        assert oauth.access_token_fingerprint(a) != oauth.access_token_fingerprint(b)

    def test_api_key_blob_behaves_like_claude_api_key(self):
        creds = cf.api_key_json()
        assert oauth.extract_oauth_data(creds) is None
        assert oauth.extract_access_token(creds) is None
        assert oauth.access_token_fingerprint(creds) is None
        assert oauth.credential_fingerprint(creds) == (
            "sha256-full:" + hashlib.sha256(creds.encode()).hexdigest()
        )
        assert oauth.build_token_status(creds) is None
        outcome = oauth.try_fetch_usage_for_account("1", "e", creds, is_active=False)
        assert outcome.error == "no-access-token"

    def test_expiry_helpers(self):
        fresh = cf.auth_json(access_exp=NOW + 3600)
        stale = cf.auth_json(access_exp=NOW - 10)
        assert not oauth.is_oauth_token_expired(oauth.extract_oauth_data(fresh)["expiresAt"])
        assert oauth.is_oauth_token_expired(oauth.extract_oauth_data(stale)["expiresAt"])
        assert oauth.login_expires_at_iso(fresh) is None
        assert oauth.build_token_status(fresh).startswith("oauth: fresh, refresh token yes")
        assert oauth.build_token_status(stale).startswith("oauth: expired, refresh token yes")

    def test_refresh_delegates_to_codex(self, monkeypatch):
        calls = []
        sentinel = oauth.RefreshOutcome("new", None)
        monkeypatch.setattr(
            codex_auth, "try_refresh", lambda c, t=10.0: calls.append((c, t)) or sentinel
        )
        creds = cf.auth_json()
        assert oauth.try_refresh_oauth_credentials(creds, timeout_s=6) is sentinel
        assert calls == [(creds, 6)]

    def test_active_usage_uses_codex_endpoint(self, monkeypatch):
        calls = []
        body = cf.wham_usage((25, 18000, NOW + 600), (60, 604800, NOW + 9000))
        monkeypatch.setattr(
            codex_auth, "request_usage", lambda c, *a, **k: calls.append(c) or body
        )
        creds = cf.auth_json()
        outcome = oauth.try_fetch_usage_for_account("1", "e", creds, is_active=True)
        assert outcome.error is None
        assert outcome.usage["five_hour"]["pct"] == 25.0
        assert outcome.usage["seven_day"]["pct"] == 60.0
        assert calls == [creds]

    def test_inactive_expired_refreshes_then_fetches_with_new_creds(self, monkeypatch):
        old = cf.auth_json(access_exp=NOW - 10, refresh_token="rt-old")
        new = cf.auth_json(access_exp=NOW + 3600, refresh_token="rt-new")
        used = []
        monkeypatch.setattr(
            codex_auth, "request_usage",
            lambda c, *a, **k: used.append(c) or cf.wham_usage((5, 18000, NOW + 60)),
        )
        via = []

        def refresh_via(num, email, snapshot):
            via.append(snapshot)
            return oauth.RefreshOutcome(new, None)

        outcome = oauth.try_fetch_usage_for_account(
            "2", "e", old, is_active=False, refresh_via=refresh_via
        )
        assert via == [old]
        assert used == [new]
        assert outcome.usage["five_hour"]["pct"] == 5.0

    def test_inactive_401_refreshes_and_retries(self, monkeypatch):
        old = cf.auth_json(refresh_token="rt-old")
        new = cf.auth_json(refresh_token="rt-new")
        responses = [cf.http_error(401), cf.wham_usage((9, 18000, NOW + 60))]
        used = []

        def fake_request(c, *a, **k):
            used.append(c)
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        monkeypatch.setattr(codex_auth, "request_usage", fake_request)
        monkeypatch.setattr(
            codex_auth, "try_refresh", lambda c, t=10.0: oauth.RefreshOutcome(new, None)
        )
        persisted = []
        outcome = oauth.try_fetch_usage_for_account(
            "3", "e", old, is_active=False,
            persist_credentials=lambda n, e, c: persisted.append(c),
        )
        assert used == [old, new]
        assert persisted == [new]
        assert outcome.usage["five_hour"]["pct"] == 9.0

    def test_dead_refresh_lineage_strikes_codex_fingerprint(self, monkeypatch):
        old = cf.auth_json(access_exp=NOW - 10, refresh_token="rt-dead")
        monkeypatch.setattr(
            codex_auth, "try_refresh",
            lambda c, t=10.0: oauth.RefreshOutcome(None, "invalid_grant"),
        )
        outcome = oauth.try_fetch_usage_for_account("4", "e", old, is_active=False)
        assert outcome.error == "invalid_grant"
        assert outcome.struck_fp == "sha256:" + hashlib.sha256(b"rt-dead").hexdigest()

    def test_http_errors_keep_claude_classification(self, monkeypatch):
        def fake_request(c, *a, **k):
            raise cf.http_error(429, b"", headers={"Retry-After": "12"})

        monkeypatch.setattr(codex_auth, "request_usage", fake_request)
        outcome = oauth.try_fetch_usage_for_account("1", "e", cf.auth_json(), is_active=True)
        assert (outcome.error, outcome.retry_after_s) == ("http-429", 12.0)


# --------------------------------------------------------- test guards


class TestCodexTestGuards:
    def test_network_functions_are_stubbed_by_default(self):
        assert codex_auth.fetch_workspace_name(cf.auth_json()) is None
        with pytest.raises(AssertionError, match="real Codex network call in test"):
            codex_auth.request_usage(cf.auth_json())
        with pytest.raises(AssertionError, match="real Codex network call in test"):
            codex_auth.try_refresh(cf.auth_json())

    def test_stubbed_usage_surfaces_as_an_error_outcome(self):
        # try_fetch_usage_for_account's catch-all absorbs the stub's
        # AssertionError: a test that strays into Codex usage sees this
        # error kind, not a crash.
        outcome = oauth.try_fetch_usage_for_account("1", "e", cf.auth_json(), is_active=True)
        assert (outcome.usage, outcome.error) == (None, "AssertionError")

    def test_relative_and_symlinked_codex_home_are_absolute(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        real = tmp_path / "real-codex"
        real.mkdir()
        (tmp_path / "link-codex").symlink_to(real)
        monkeypatch.setattr("pathlib.Path.home", lambda: home)
        monkeypatch.chdir(tmp_path)

        monkeypatch.setenv("CODEX_HOME", "rel-codex")
        specs = dict(conftest._freeze_real_store_specs())
        assert specs.get(tmp_path / "rel-codex") is True
        assert Path("rel-codex") not in specs

        # As spelled (what cswap joins paths onto) and resolved (what Codex
        # canonicalizes it to): the hook compares spellings, not inodes.
        monkeypatch.setenv("CODEX_HOME", "link-codex")
        specs = dict(conftest._freeze_real_store_specs())
        assert specs.get(tmp_path / "link-codex") is True
        assert specs.get(real) is True

    def test_real_codex_homes_are_frozen_as_protected(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        custom = tmp_path / "custom-codex-home"
        monkeypatch.setattr("pathlib.Path.home", lambda: home)
        monkeypatch.setenv("CODEX_HOME", str(custom))

        specs = dict(conftest._freeze_real_store_specs())

        assert specs.get(home / ".codex") is True
        assert specs.get(custom) is True

    def test_frozen_specs_refuse_a_nested_codex_write(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        nested = home / ".codex" / "sessions"
        nested.mkdir(parents=True)
        monkeypatch.setattr("pathlib.Path.home", lambda: home)
        specs = conftest._freeze_real_store_specs()
        monkeypatch.setattr(conftest, "_REAL_STORE_SPECS", specs)
        monkeypatch.setattr(
            conftest, "_REAL_STORE_HINTS", conftest._derive_real_store_hints(specs, home)
        )
        with pytest.raises(conftest.RealStoreWriteBlocked):
            (nested / "rollout.jsonl").write_text("x")
        with pytest.raises(conftest.RealStoreWriteBlocked):
            (home / ".codex" / "auth.json").write_text("{}")

    def test_a_checkout_inside_codex_home_keeps_only_direct_children(
        self, monkeypatch, tmp_path
    ):
        # Codex's own worktrees live under $CODEX_HOME/worktrees; a suite run
        # from one must still be able to write its caches beside itself.
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        monkeypatch.setattr("pathlib.Path.home", lambda: home)
        monkeypatch.setattr(
            conftest, "_CONFTEST_PATH",
            home / ".codex" / "worktrees" / "w1" / "tests" / "conftest.py",
        )
        specs = dict(conftest._freeze_real_store_specs())
        assert specs.get(home / ".codex") is False


@_live_network
def test_a_refresh_of_a_hybrid_login_reports_the_tokens_owner(urlopen):
    """codex-spec gotcha 5: B's stored account_id over A's tokens. The
    refreshed tokens are A's, so ``token_account`` must say A."""
    data = cf.auth_dict()
    data["tokens"]["account_id"] = "acct-team"
    urlopen(cf.FakeResponse({"id_token": cf.id_token(), "refresh_token": "rt-new"}))

    outcome = codex_auth.try_refresh(json.dumps(data, indent=2))

    assert outcome.token_account["organizationUuid"] == "acct-personal"


def test_a_real_codex_binary_is_never_looked_up_or_spawned():
    """conftest guard: a test that is not faking codex fails rather than
    running the real binary (a real `codex login` revokes; the TUI hangs)."""
    import shutil
    import subprocess

    with pytest.raises(AssertionError, match="real codex"):
        shutil.which("codex")
    with pytest.raises(AssertionError, match="real codex"):
        subprocess.run(["/usr/local/bin/codex", "login"])
    with pytest.raises(AssertionError, match="real codex"):
        subprocess.run("codex logout", shell=True)
    assert shutil.which("python3") or shutil.which("python")  # others untouched


def test_http_errors_from_the_fixtures_are_closed_after_the_test():
    from tests import codex_fixtures

    err = cf.http_error(401, {"error": "invalid_grant"})
    assert err in codex_fixtures.OPEN_HTTP_ERRORS  # closed by conftest at teardown
