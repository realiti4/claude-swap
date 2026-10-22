"""Golden byte identities measured on origin/main 9ea59b2c33c4.

Only clocks are fixed. These cover the persisted sequence, untouched Claude
settings, complete list JSON rows, and event stream before/after an OAuth switch.
"""

from datetime import datetime, timezone
import hashlib
import json
from unittest.mock import patch

import pytest

from claude_swap import autoswitch
from tests.test_autoswitch import EngineHarness, _entry_for, _usage


@pytest.mark.parametrize("strategy", ["best", "consume-first"])
def test_gateway_fleet_default_identity(temp_home, monkeypatch, strategy):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 19, tzinfo=timezone.utc)

    monkeypatch.setattr(autoswitch, "datetime", FixedDatetime)
    monkeypatch.setattr(
        "claude_swap.switcher.get_timestamp", lambda: "2026-10-19T00:00:00Z"
    )
    h = EngineHarness(temp_home, strategy=strategy)
    h.seed(1, "one@example.com")
    h.seed(2, "two@example.com")
    h.make_live("one@example.com", 1)
    monkeypatch.setattr(h.engine, "_freshen_target", lambda *args: "ok")
    settings = temp_home / ".claude" / "settings.json"
    settings.write_bytes(b'{ "permissions": {"allow": ["Read"]} }\n')
    artifacts = {"sequence": [], "settings": [], "rows": [], "events": []}
    for first, second in ((20, 30), (100, 10)):
        entries = {
            "1": _entry_for(_usage(first), h.clock()),
            "2": _entry_for(_usage(second), h.clock()),
        }
        h.tick_with_entries(entries)
        with patch.object(h.switcher, "_collect_usage_entries", return_value=entries):
            rows = h.switcher.list_accounts(json_output=True)["accounts"]
        artifacts["sequence"].append(h.switcher.sequence_file.read_bytes())
        artifacts["settings"].append(settings.read_bytes())
        artifacts["rows"].append(json.dumps(rows).encode())
    artifacts["events"].append(json.dumps([e.to_json() for e in h.events]).encode())
    digests = {
        name: hashlib.sha256(b"\n".join(parts)).hexdigest()
        for name, parts in artifacts.items()
    }
    assert digests == {
        "sequence": "bb213b97a4a82cb5d9d1712bce5019f578f59a18c0e28c24465c7113b25cacd3",
        "settings": "d81f5f8716d2fb50e236a9e89d1ff02e8ad826264ffcec7d607c96f2fc8ff4b8",
        "rows": "50e69c6a8c3b3ec8fa0ea040389276b2685c0c97d0920816a52b8def479ef640",
        "events": {
            "best": "daaa80f5ffb59cf18339e84429b90d8bf39503ed816d15941afccb3744b528b5",
            "consume-first": "d107008be989b192773cc5e67c98e26a9fc2d368f01ffd1be734f1dd3be4dbcc",
        }[strategy],
    }
