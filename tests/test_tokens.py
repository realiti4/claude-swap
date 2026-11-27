"""Per-prompt token counts read from Claude Code transcripts (tokens.py).

Transcripts are written to a temporary directory in the shape Claude Code
2.1.331 writes them. Nothing here reads a real ~/.claude.
"""

import json
import os
import time
from datetime import datetime, timezone

import pytest

from claude_swap import tokens


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _user(ts, text="hello", **extra):
    return {"type": "user", "timestamp": _iso(ts), "cwd": "/home/x/myproject",
            "message": {"role": "user", "content": text}, **extra}


def _reply(ts, msg_id, *, inp=10, cache_read=0, cache_write=0, out=5, model="claude-opus-5-5"):
    return {"type": "assistant", "timestamp": _iso(ts), "message": {
        "id": msg_id, "model": model, "content": [{"type": "text", "text": "secret reply"}],
        "usage": {"input_tokens": inp, "cache_read_input_tokens": cache_read,
                  "cache_creation_input_tokens": cache_write, "output_tokens": out}}}


@pytest.fixture
def env(tmp_path):
    backup = tmp_path / "cswap"
    backup.mkdir()
    config = tmp_path / "claude"
    project = config / "projects" / "-home-x-myproject"
    project.mkdir(parents=True)
    return backup, config, project


def _write(path, records, mode="w"):
    with open(path, mode, encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, separators=(",", ":")) + "\n")


def _ledger(backup, config):
    ledger = tokens.Ledger(backup, config_dir=config)
    ledger.refresh(force=True)
    return ledger


def test_groups_requests_under_their_prompt(env):
    backup, config, project = env
    now = time.time()
    _write(project / "sess1.jsonl", [
        _user(now - 100, "first"),
        _reply(now - 99, "m1", inp=100, cache_read=900, out=10),
        _reply(now - 98, "m2", inp=50, cache_read=1000, cache_write=50, out=20),
        _user(now - 50, "second"),
        _reply(now - 49, "m3", inp=5, out=1),
    ])
    prompts = _ledger(backup, config).prompts()
    assert [p.calls for p in prompts] == [2, 1]
    first = prompts[0]
    assert first.sent == 100 + 900 + 50 + 1000 + 50
    assert first.received == 30
    assert first.cache_read == 1900
    assert first.peak_context == 1100
    assert first.project == "myproject"
    assert first.models == {"claude-opus-5-5": 2}


def test_repeated_message_id_is_counted_once(env):
    # One response split over several content-block records repeats its usage.
    backup, config, project = env
    now = time.time()
    _write(project / "s.jsonl", [_user(now - 10), _reply(now - 9, "m1"), _reply(now - 9, "m1")])
    assert _ledger(backup, config).prompts()[0].calls == 1


def test_non_prompts_do_not_start_a_prompt(env):
    backup, config, project = env
    now = time.time()
    tool_result = {"type": "user", "timestamp": _iso(now - 8),
                   "message": {"content": [{"type": "tool_result", "content": "x"}]}}
    _write(project / "s.jsonl", [
        _user(now - 10),
        _reply(now - 9, "m1"),
        tool_result,
        _user(now - 7, isMeta=True),
        _reply(now - 6, "m2"),
    ])
    prompts = _ledger(backup, config).prompts()
    assert len(prompts) == 1 and prompts[0].calls == 2


def test_synthetic_records_are_skipped(env):
    backup, config, project = env
    now = time.time()
    _write(project / "s.jsonl", [_user(now - 10), _reply(now - 9, "m1", model="<synthetic>")])
    assert _ledger(backup, config).prompts() == []


def test_subagent_requests_join_the_parent_prompt(env):
    backup, config, project = env
    now = time.time()
    _write(project / "sess1.jsonl", [_user(now - 100), _reply(now - 99, "m1")])
    sub = project / "sess1" / "subagents"
    sub.mkdir(parents=True)
    _write(sub / "agent-a.jsonl", [_user(now - 98, "do a thing"), _reply(now - 97, "s1")])
    prompts = _ledger(backup, config).prompts()
    assert len(prompts) == 1
    assert prompts[0].calls == 2


def test_incremental_read_and_partial_line(env):
    backup, config, project = env
    now = time.time()
    path = project / "s.jsonl"
    _write(path, [_user(now - 10), _reply(now - 9, "m1")])
    ledger = _ledger(backup, config)
    # A half-written line is left for later, then completed.
    line = json.dumps(_reply(now - 8, "m2"), separators=(",", ":"))
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line[:20])
    ledger.refresh(force=True)
    assert ledger.prompts()[0].calls == 1
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line[20:] + "\n")
    ledger.refresh(force=True)
    assert ledger.prompts()[0].calls == 2


def test_cache_survives_a_restart(env):
    backup, config, project = env
    now = time.time()
    _write(project / "s.jsonl", [_user(now - 10), _reply(now - 9, "m1", inp=42)])
    _ledger(backup, config)
    reloaded = tokens.Ledger(backup, config_dir=config)  # no refresh: cache only
    assert reloaded.prompts()[0].sent == 42


def test_no_text_is_stored(env):
    backup, config, project = env
    now = time.time()
    _write(project / "s.jsonl", [_user(now - 10, "my private prompt"), _reply(now - 9, "m1")])
    _ledger(backup, config)
    cached = (backup / "cache" / tokens.CACHE_FILENAME).read_text()
    assert "private" not in cached and "secret" not in cached
    assert "/home/x" not in cached  # project folder name only


def test_requests_are_credited_to_the_account_active_at_the_time(env):
    backup, config, project = env
    base = time.time() - 1000
    switch_at = datetime.fromtimestamp(base + 500).strftime("%Y-%m-%d %H:%M:%S")
    (backup / "claude-swap.log").write_text(
        f"{switch_at},000 - INFO - Switched from account 1 to 2\n", encoding="utf-8"
    )
    _write(project / "s.jsonl", [
        _user(base + 100), _reply(base + 101, "m1"),
        _user(base + 700), _reply(base + 701, "m2"),
    ])
    ledger = _ledger(backup, config)
    assert [p.account for p in ledger.prompts()] == ["1", "2"]
    assert set(ledger.totals(0)) == {"1", "2"}



def test_missing_projects_dir_is_harmless(tmp_path):
    ledger = tokens.Ledger(tmp_path, config_dir=tmp_path / "nothing")
    ledger.refresh(force=True)
    assert ledger.prompts() == []


@pytest.mark.parametrize("n, text", [(812, "812"), (45_200, "45.2k"), (1_234_567, "1.2M")])
def test_human(n, text):
    assert tokens.human(n) == text


def test_short_model():
    assert tokens.short_model("claude-opus-5-5") == "opus 5.5"
    assert tokens.short_model(None) == "?"
