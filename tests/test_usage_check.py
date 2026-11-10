"""Manual usage check for setup-token accounts: Watch's `u`.

One header probe per setup-token account on request, whatever
usage.headerProbe says and however fresh the stored reading; the server's
backoff still holds, cswap's own "probe off" backoff does not.
"""
from __future__ import annotations

import contextlib
import dataclasses
import inspect
import io
import json
import time
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_swap import oauth
from claude_swap.switcher import ClaudeAccountSwitcher, UsageCheckResult
from claude_swap.usage_store import AUTH_DEAD_STRIKES, _row_eligible

TOKEN_1 = "sk-ant-oat01-" + "1" * 30
TOKEN_2 = "sk-ant-oat01-" + "2" * 30


# ---------------------------------------------------------------------------
# store eligibility
# ---------------------------------------------------------------------------


class TestForcedEligibility:
    NOW = 10_000.0

    def row(self, **fields) -> dict:
        return {"fetchedAt": self.NOW - 5, "nextPollAt": self.NOW + 600, **fields}

    def test_fresh_and_not_due_is_eligible_only_when_forced(self):
        assert not _row_eligible(self.row(), self.NOW, respect_plans=True)
        assert not _row_eligible(self.row(), self.NOW, respect_plans=False, repair_overslept=True)
        assert _row_eligible(self.row(), self.NOW, respect_plans=False, force=True)

    def test_probe_off_backoff_does_not_hold_a_forced_read(self):
        row = self.row(backoffUntil=self.NOW + 300, lastError="setup-token")
        assert not _row_eligible(row, self.NOW, respect_plans=True)
        assert _row_eligible(row, self.NOW, respect_plans=False, force=True)

    @pytest.mark.parametrize("fields", [
        {"backoffUntil": NOW + 300, "lastError": "http-429"},
        {"backoffUntil": NOW + 300, "lastError": None},  # a quota refusal
        {"heldUntil": NOW + 300},
        {"claimUntil": NOW + 30, "lastAttemptAt": NOW - 1},
        {"authDeadStrikes": AUTH_DEAD_STRIKES},
    ])
    def test_server_and_coordination_gates_still_hold(self, fields):
        assert not _row_eligible(self.row(**fields), self.NOW, respect_plans=False, force=True)


# ---------------------------------------------------------------------------
# the switcher's check
# ---------------------------------------------------------------------------


def probe_response(five: float = 0.25, seven: float = 0.5):
    resp = MagicMock()
    resp.headers = {
        "anthropic-ratelimit-unified-5h-utilization": str(five),
        "anthropic-ratelimit-unified-5h-reset": str(int(time.time()) + 3600),
        "anthropic-ratelimit-unified-7d-utilization": str(seven),
        "anthropic-ratelimit-unified-7d-reset": str(int(time.time()) + 86400),
    }
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


@pytest.fixture
def switcher(temp_home: Path) -> ClaudeAccountSwitcher:
    s = ClaudeAccountSwitcher()
    s._setup_directories()
    with contextlib.redirect_stdout(io.StringIO()):
        s.add_account_from_token(TOKEN_1, email="one@example.com")
        s.add_account_from_token(TOKEN_2, email="two@example.com")
    return s


#: One manual read of one account: the Haiku probe and, always for `u`, Fable's.
ONE_READ = [oauth.USAGE_PROBE_MODEL, oauth.USAGE_PROBE_FABLE_MODEL]


def run_check(switcher, answer):
    """check_setup_token_usage with every probe answered by ``answer``;
    returns the results and the model each request asked for, sorted (the
    accounts are read in parallel)."""
    urls: list[str] = []

    def fake_urlopen(req, timeout=None):
        assert req.full_url == oauth.USAGE_PROBE_URL
        urls.append(json.loads(req.data)["model"])
        result = answer(req) if inspect.isfunction(answer) else answer
        if isinstance(result, Exception):
            raise result
        return result

    with patch.object(switcher, "_live_session_pids", return_value=[]), \
         patch("claude_swap.oauth.urllib.request.urlopen", side_effect=fake_urlopen):
        return switcher.check_setup_token_usage(), sorted(urls)


def entries(switcher) -> dict:
    data = switcher._get_sequence_data()
    identities = {n: (a["email"], a.get("organizationUuid") or "") for n, a in data["accounts"].items()}
    return switcher._usage_store.entries(identities)


class TestCheckSetupTokenUsage:
    def test_reads_every_setup_token_account_with_the_probe_off(self, switcher):
        results, urls = run_check(switcher, probe_response())
        assert urls == sorted(ONE_READ * 2)
        assert [(r.number, r.outcome) for r in results] == [("1", "updated"), ("2", "updated")]
        stored = entries(switcher)
        assert stored["1"].last_good["five_hour"]["pct"] == 25.0
        assert stored["2"].last_good["seven_day"]["pct"] == 50.0

    def test_a_second_press_reads_again_however_fresh(self, switcher):
        run_check(switcher, probe_response(five=0.25))
        results, urls = run_check(switcher, probe_response(five=0.40))
        assert urls == sorted(ONE_READ * 2) and all(r.outcome == "updated" for r in results)
        assert entries(switcher)["1"].last_good["five_hour"]["pct"] == 40.0

    def test_the_probe_off_backoff_from_a_normal_pass_does_not_block_it(self, switcher):
        with patch.object(switcher, "_live_session_pids", return_value=[]), \
             patch("claude_swap.oauth.urllib.request.urlopen") as urlopen:
            switcher._collect_usage_entries(switcher._build_accounts_info())
        urlopen.assert_not_called()
        assert entries(switcher)["1"].last_error == "setup-token"
        assert entries(switcher)["1"].in_backoff(switcher._usage_store.clock())

        results, urls = run_check(switcher, probe_response())
        assert urls == sorted(ONE_READ * 2) and all(r.outcome == "updated" for r in results)

    def test_a_429_is_reported_and_then_respected(self, switcher):
        def answer(req):
            return urllib.error.HTTPError(
                oauth.USAGE_PROBE_URL, 429, "Too Many", hdrs={"Retry-After": "120"}, fp=None)

        results, _ = run_check(switcher, answer)
        assert all(r.outcome == "failed" and "http-429" in r.detail for r in results)
        assert all("retry in 2m" in r.detail for r in results)

        results, urls = run_check(switcher, probe_response())
        assert urls == []
        assert all(r.outcome == "skipped" and "server asked to wait" in r.detail for r in results)

    def test_an_account_at_its_limit_is_current_not_skipped(self, switcher):
        """A quota refusal holds the account until its deadline; the next
        check doesn't ask again, and reports it as at its limit, not as an
        account it failed to read."""
        def refused(req):
            return urllib.error.HTTPError(
                oauth.USAGE_PROBE_URL, 429, "Too Many", fp=None, hdrs={
                    "Retry-After": "7200",
                    "anthropic-ratelimit-unified-status": "rejected",
                    "anthropic-ratelimit-unified-5h-utilization": "1.0",
                    "anthropic-ratelimit-unified-5h-reset": str(int(time.time()) + 7200),
                })

        results, _ = run_check(switcher, refused)
        assert all(r.outcome == "updated" and r.detail == "at its limit" for r in results)

        results, urls = run_check(switcher, probe_response())
        assert urls == []
        assert all(r.outcome == "at-limit" for r in results)
        assert all(r.detail.startswith("at its limit for another 1h") for r in results)

    def test_refreshing_logins_and_api_keys_are_not_probed(self, switcher):
        import json

        switcher._write_account_credentials("3", "me@example.com", json.dumps({"claudeAiOauth": {
            "accessToken": "sk-3", "refreshToken": "rt-3", "expiresAt": 99999999999000}}))
        switcher._write_account_config("3", "me@example.com", json.dumps({"oauthAccount": {
            "emailAddress": "me@example.com", "accountUuid": "uuid-3"}}))
        data = switcher._get_sequence_data()
        data["accounts"]["3"] = {"email": "me@example.com", "uuid": "uuid-3",
                                 "organizationUuid": "org", "organizationName": "Org"}
        data["sequence"].append(3)
        switcher._write_json(switcher.sequence_file, data)
        with contextlib.redirect_stdout(io.StringIO()):
            switcher.add_account_from_token("sk-ant-api03-" + "k" * 30, email="key@example.com")

        results, urls = run_check(switcher, probe_response())
        assert sorted(r.number for r in results) == ["1", "2"]
        assert urls == sorted(ONE_READ * 2)

    def test_no_setup_token_accounts_means_no_requests(self, temp_home):
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        results, urls = run_check(s, probe_response())
        assert results == [] and urls == []


# ---------------------------------------------------------------------------
# display
# ---------------------------------------------------------------------------


class TestDisplay:
    def test_setup_token_card_always_says_when_it_was_updated(self):
        from claude_swap.tui.widgets import account_card_text
        from claude_swap.usage_store import UsageEntry
        from tests.test_tui import make_account

        now = time.time()
        reading = {"five_hour": {"pct": 25.0}, "seven_day": {"pct": 50.0}}
        fresh = UsageEntry(last_good=reading, fetched_at=now - 2, age_s=2)
        older = UsageEntry(last_good=reading, fetched_at=now - 200, age_s=200)
        token = dataclasses.replace(make_account(1), setup_token=True, usage=fresh)
        assert "updated just now" in account_card_text(token, 100, now=now).plain
        token = dataclasses.replace(token, usage=older)
        assert "updated 3m ago" in account_card_text(token, 100, now=now).plain

        login = dataclasses.replace(make_account(2), usage=older)
        assert "updated" not in account_card_text(login, 100, now=now).plain

    def test_summary_line(self):
        from claude_swap.tui.app import usage_check_summary

        assert usage_check_summary([]) == "No setup-token accounts to check."
        ok = UsageCheckResult("1", "a@x.io", "updated")
        assert usage_check_summary([ok]) == "Usage up to date for 1 setup-token account"
        waited = UsageCheckResult("2", "b@x.io", "skipped", "the server asked to wait (http-429), 4m left")
        limit = UsageCheckResult("3", "c@x.io", "updated", "at its limit")
        held = UsageCheckResult("4", "d@x.io", "at-limit", "at its limit for another 2h 5m")
        assert usage_check_summary([ok, waited, limit, held]) == (
            "Usage up to date for 3 of 4 setup-token accounts · "
            "2: the server asked to wait (http-429), 4m left · 3: at its limit · "
            "4: at its limit for another 2h 5m"
        )
        assert usage_check_summary([ok, held]).startswith("Usage up to date for 2 setup-token accounts")


# ---------------------------------------------------------------------------
# Watch's u
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestWatchKey:
    async def test_u_checks_and_notifies(self, tmp_path):
        from tests.test_tui import FakeSwitcher, make_account, make_app, settle

        fake = FakeSwitcher(
            [make_account(1, active=True), dataclasses.replace(make_account(2), setup_token=True)],
            tmp_path,
        )
        fake.check_setup_token_usage = MagicMock(
            return_value=[UsageCheckResult("2", "b@example.com", "updated")])
        app = make_app(fake)
        notes: list[str] = []
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            app.notify = lambda message, **kw: notes.append(str(message))
            await pilot.press("w")
            await settle(pilot)
            await pilot.press("u")
            await settle(pilot)
        fake.check_setup_token_usage.assert_called_once()
        assert "Usage up to date for 1 setup-token account" in notes

    async def test_u_without_setup_token_accounts_sends_nothing(self, tmp_path):
        from tests.test_tui import FakeSwitcher, make_account, make_app, settle

        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        fake.check_setup_token_usage = MagicMock(return_value=[])
        app = make_app(fake)
        notes: list[str] = []
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            app.notify = lambda message, **kw: notes.append(str(message))
            await pilot.press("w")
            await settle(pilot)
            await pilot.press("u")
            await settle(pilot)
        fake.check_setup_token_usage.assert_not_called()
        assert notes == ["No setup-token accounts to check."]
