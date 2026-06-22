"""macOS menu bar app for claude-swap (``cswap --menubar``).

A thin GUI shell over ``ClaudeAccountSwitcher`` — it never re-implements
account logic. Built on ``rumps`` (an optional extra, macOS only). The pure
helpers below (settings, formatting, plist rendering) are import-safe without
rumps so they can be unit-tested in CI; ``rumps`` is imported lazily inside
the app glue.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path

ICON = "⇄"
REFRESH_CHOICES: tuple[int, ...] = (30, 60, 300)


@dataclass
class MenuBarSettings:
    """User-configurable menu bar behavior, persisted as JSON."""

    show_account_name: bool = True
    show_quota_pct: bool = True
    refresh_interval: int = 60
    launch_at_login: bool = False

    @classmethod
    def load(cls, path: Path) -> "MenuBarSettings":
        """Load settings, falling back to defaults on any problem.

        Unknown keys are ignored; a value whose type doesn't match the field
        default is dropped (that field keeps its default). A missing or
        unparseable file yields all-defaults.
        """
        defaults = cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return defaults
        if not isinstance(raw, dict):
            return defaults
        kwargs = {}
        for f in fields(cls):
            if f.name in raw and isinstance(raw[f.name], type(getattr(defaults, f.name))):
                kwargs[f.name] = raw[f.name]
        return cls(**kwargs)

    def save(self, path: Path) -> None:
        """Write settings as pretty JSON, creating parent directories."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
