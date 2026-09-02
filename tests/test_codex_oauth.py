"""Codex token refresh. No test here touches the network."""

from __future__ import annotations

import json
import time
import urllib.error
from io import BytesIO

from claude_swap.codex import oauth as coauth
from tests.conftest_codex import make_auth_json, make_jwt


class _Resp:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code: int, body: dict | str) -> urllib.error.HTTPError:
    raw = json.dumps(body).encode() if isinstance(body, dict) else body.encode()
    return urllib.error.HTTPError("u", code, "err", {}, BytesIO(raw))


def _raiser(exc):
    def boom(*a, **k):
        raise exc

    return boom


def test_refresh_updates_the_access_token(monkeypatch):
    payload = make_auth_json(refresh_token="rt-old")
    new_jwt = make_jwt({"exp": 1_900_000_000, "email": "a@example.com"})
    monkeypatch.setattr(
        coauth.urllib.request,
        "urlopen",
        lambda *a, **k: _Resp(
            {"access_token": new_jwt, "refresh_token": "rt-new", "id_token": new_jwt}
        ),
    )

    outcome = coauth.try_refresh(payload)

    assert outcome.kind is None
    assert outcome.payload["tokens"]["access_token"] == new_jwt
    assert outcome.payload["tokens"]["refresh_token"] == "rt-new"


def test_refresh_keeps_the_old_refresh_token_when_none_is_returned(monkeypatch):
    """RFC 6749 6: an absent refresh_token means keep the one you have.
    Overwriting it with None would destroy the account's only way back."""
    payload = make_auth_json(refresh_token="rt-old")
    monkeypatch.setattr(
        coauth.urllib.request, "urlopen", lambda *a, **k: _Resp({"access_token": "at"})
    )
    outcome = coauth.try_refresh(payload)
    assert outcome.payload["tokens"]["refresh_token"] == "rt-old"


def test_refresh_does_not_mutate_the_payload_it_was_given(monkeypatch):
    """The caller may still need the pre-refresh payload if the persist fails."""
    payload = make_auth_json(refresh_token="rt-old")
    monkeypatch.setattr(
        coauth.urllib.request,
        "urlopen",
        lambda *a, **k: _Resp({"access_token": "at", "refresh_token": "rt-new"}),
    )
    coauth.try_refresh(payload)
    assert payload["tokens"]["refresh_token"] == "rt-old"


def test_refresh_stamps_last_refresh(monkeypatch):
    payload = make_auth_json()
    monkeypatch.setattr(
        coauth.urllib.request, "urlopen", lambda *a, **k: _Resp({"access_token": "at"})
    )
    outcome = coauth.try_refresh(payload)
    assert outcome.payload["last_refresh"] != payload["last_refresh"]


def test_a_200_without_a_token_is_not_treated_as_success(monkeypatch):
    """Persisting it would store a stale access token alongside a possibly
    already-spent refresh token."""
    monkeypatch.setattr(coauth.urllib.request, "urlopen", lambda *a, **k: _Resp({}))
    assert coauth.try_refresh(make_auth_json()).kind == "transient"


def test_missing_refresh_token_is_a_permanent_verdict():
    payload = make_auth_json()
    payload["tokens"]["refresh_token"] = None
    assert coauth.try_refresh(payload).kind == "no_refresh_token"


def test_api_key_account_is_not_refreshable():
    assert coauth.try_refresh({"auth_mode": "apikey", "tokens": None}).kind == "not_applicable"


def test_the_endpoints_nested_error_shape_is_understood(monkeypatch):
    """Verified live: this endpoint answers 401 with a nested error object, not
    RFC 6749's flat {"error": "..."} string."""
    monkeypatch.setattr(
        coauth.urllib.request,
        "urlopen",
        _raiser(
            _http_error(
                401,
                {
                    "error": {
                        "message": "Could not validate your token.",
                        "type": "invalid_request_error",
                        "code": "token_expired",
                    }
                },
            )
        ),
    )
    assert coauth.try_refresh(make_auth_json()).kind == "token_expired"


def test_the_flat_rfc_error_shape_is_also_understood(monkeypatch):
    """The endpoint is undocumented; if it moves to the standard shape, the
    verdict must not silently downgrade to 'transient' and retry forever."""
    monkeypatch.setattr(
        coauth.urllib.request,
        "urlopen",
        _raiser(_http_error(400, {"error": "invalid_grant"})),
    )
    assert coauth.try_refresh(make_auth_json()).kind == "invalid_grant"


def test_a_rejected_client_is_permanent(monkeypatch):
    monkeypatch.setattr(
        coauth.urllib.request,
        "urlopen",
        _raiser(_http_error(401, {"error": {"code": "invalid_client"}})),
    )
    assert coauth.try_refresh(make_auth_json()).kind == "invalid_client"


def test_an_unrecognised_error_code_stays_transient(monkeypatch):
    """A misclassified transient costs one retry; a misclassified permanent
    quarantines a live account."""
    monkeypatch.setattr(
        coauth.urllib.request,
        "urlopen",
        _raiser(_http_error(400, {"error": {"code": "rate_limited"}})),
    )
    assert coauth.try_refresh(make_auth_json()).kind == "transient"


def test_an_unparseable_error_body_stays_transient(monkeypatch):
    monkeypatch.setattr(
        coauth.urllib.request, "urlopen", _raiser(_http_error(400, "not json at all"))
    )
    assert coauth.try_refresh(make_auth_json()).kind == "transient"


def test_a_server_error_is_transient(monkeypatch):
    monkeypatch.setattr(
        coauth.urllib.request,
        "urlopen",
        _raiser(_http_error(503, {"error": {"code": "invalid_grant"}})),
    )
    assert coauth.try_refresh(make_auth_json()).kind == "transient"


def test_a_network_failure_is_transient(monkeypatch):
    monkeypatch.setattr(
        coauth.urllib.request, "urlopen", _raiser(urllib.error.URLError("down"))
    )
    assert coauth.try_refresh(make_auth_json()).kind == "transient"


def test_the_request_uses_the_verified_client_id_and_json_body(monkeypatch):
    """Both were established against the live endpoint; a silent change to
    either turns every refresh into invalid_client."""
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data.decode())
        seen["headers"] = dict(req.headers)
        return _Resp({"access_token": "at"})

    monkeypatch.setattr(coauth.urllib.request, "urlopen", fake_urlopen)
    coauth.try_refresh(make_auth_json(refresh_token="rt-x"))

    assert seen["url"] == "https://auth.openai.com/oauth/token"
    assert seen["headers"]["Content-type"] == "application/json"
    assert seen["body"] == {
        "grant_type": "refresh_token",
        "refresh_token": "rt-x",
        "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
        "scope": "openid profile email offline_access",
    }


def test_needs_refresh_is_true_for_an_expired_token():
    assert coauth.needs_refresh(make_auth_json(exp=0)) is True


def test_needs_refresh_is_false_well_inside_the_window():
    assert coauth.needs_refresh(make_auth_json(exp=time.time() + 3600)) is False


def test_needs_refresh_is_true_inside_the_margin():
    assert coauth.needs_refresh(make_auth_json(exp=time.time() + 30)) is True


def test_needs_refresh_is_true_when_the_expiry_is_unreadable():
    assert coauth.needs_refresh({"tokens": {"access_token": "x"}}) is True


def test_no_token_value_ever_reaches_a_log(monkeypatch, caplog):
    """A refresh token in a log line is a credential leak with a long tail —
    these logs are what users paste into public issues."""
    monkeypatch.setattr(
        coauth.urllib.request,
        "urlopen",
        _raiser(_http_error(401, {"error": {"code": "token_expired"}})),
    )
    with caplog.at_level("DEBUG"):
        coauth.try_refresh(make_auth_json(refresh_token="SECRET-RT"))
    assert "SECRET-RT" not in caplog.text
