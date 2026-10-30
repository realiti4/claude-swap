import json
import sys

import pytest

from claude_swap import desktop_follow as df
from claude_swap.exceptions import ClaudeSwitchError


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "backup"
    config = tmp_path / ".claude.json"
    monkeypatch.setattr(df.paths, "get_backup_root", lambda: root)
    monkeypatch.setattr(df.paths, "get_global_config_path", lambda: config)
    monkeypatch.setattr(df, "claude_config_lock", lambda **_: _NoLock())
    return root, config


class _NoLock:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_apply_env_adds_ours_and_keeps_the_rest():
    config, wrote = df.apply_env({"theme": "dark", "env": {"A": "1"}}, {"HTTPS_PROXY": "p"})
    assert config == {"theme": "dark", "env": {"A": "1", "HTTPS_PROXY": "p"}}
    assert wrote == {"HTTPS_PROXY": "p"}


def test_apply_env_never_overwrites_a_users_own_value():
    config, wrote = df.apply_env({"env": {"HTTPS_PROXY": "corp"}}, {"HTTPS_PROXY": "p", "X": "y"})
    assert config["env"] == {"HTTPS_PROXY": "corp", "X": "y"}
    assert wrote == {"X": "y"}


def test_remove_env_takes_out_only_what_we_wrote():
    config = {"env": {"HTTPS_PROXY": "p", "NODE_EXTRA_CA_CERTS": "changed", "A": "1"}}
    out = df.remove_env(config, {"HTTPS_PROXY": "p", "NODE_EXTRA_CA_CERTS": "ca"})
    assert out["env"] == {"NODE_EXTRA_CA_CERTS": "changed", "A": "1"}


def test_remove_env_drops_an_empty_block():
    assert df.remove_env({"env": {"HTTPS_PROXY": "p"}, "x": 1}, {"HTTPS_PROXY": "p"}) == {"x": 1}


def test_ensure_does_nothing_when_off(home):
    assert df.ensure() == "off"


def test_ensure_leaves_a_live_proxy_alone(home, monkeypatch):
    df.save_state({"enabled": True, "port": 9})
    monkeypatch.setattr(df, "proxy_alive", lambda port: True)
    assert df.ensure() == "ok"


def test_ensure_restarts_a_dead_proxy(home, monkeypatch):
    df.save_state({"enabled": True, "port": 9})
    started = []
    monkeypatch.setattr(df, "proxy_alive", lambda port: False)
    monkeypatch.setattr(df, "start_proxy", lambda port: started.append(port))
    assert df.ensure() == "restarted"
    assert started == [9]


def test_ensure_gives_the_wiring_up_when_the_proxy_will_not_start(home, monkeypatch):
    root, config = home
    wrote = df.wanted_env(9)
    config.write_text(json.dumps({"env": {**wrote, "A": "1"}}))
    df.marker_path().parent.mkdir(parents=True)
    df.marker_path().write_text("claude-desktop\n")
    df.save_state({"enabled": True, "port": 9, "wrote": wrote})

    def fail(port):
        raise ClaudeSwitchError("no mitmdump")

    monkeypatch.setattr(df, "proxy_alive", lambda port: False)
    monkeypatch.setattr(df, "start_proxy", fail)
    assert df.ensure() == "healed"
    assert json.loads(config.read_text()) == {"env": {"A": "1"}}
    assert not df.marker_path().exists()
    assert df.load_state()["enabled"] is False


def test_state_word(home, monkeypatch):
    root, config = home
    assert df.state_word()[0] == "OFF"
    wrote = df.wanted_env(9)
    df.save_state({"enabled": True, "port": 9, "wrote": wrote})
    monkeypatch.setattr(df, "proxy_alive", lambda port: False)
    assert df.state_word()[0] == "NOT-OK"
    monkeypatch.setattr(df, "proxy_alive", lambda port: True)
    config.write_text(json.dumps({"env": {}}))
    word, detail = df.state_word()
    assert word == "NOT-OK" and "HTTPS_PROXY" in detail
    config.write_text(json.dumps({"env": wrote}))
    assert df.state_word()[0] == "OK"


def test_proxy_listens_only_on_loopback():
    cmd = df.proxy_command("mitmdump", 8899)
    assert cmd[cmd.index("--listen-host") + 1] == "127.0.0.1"
    assert cmd[cmd.index("--listen-port") + 1] == "8899"


def test_task_runs_windowless_and_every_minute(monkeypatch):
    calls = []
    monkeypatch.setattr(df.sys, "platform", "win32")
    monkeypatch.setattr(df.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or type("R", (), {"returncode": 0})())
    df.install_task()
    cmd = calls[0]
    assert cmd[cmd.index("/SC") + 1] == "MINUTE" and cmd[cmd.index("/MO") + 1] == "1"
    assert "desktop-follow --ensure" in cmd[cmd.index("/TR") + 1]


@pytest.mark.skipif(sys.platform == "win32", reason="the refusal is for other platforms")
def test_other_platforms_are_pointed_at_pin(capsys):
    assert df.run(["--state"]) == 1
    assert "cswap pin" in capsys.readouterr().err
