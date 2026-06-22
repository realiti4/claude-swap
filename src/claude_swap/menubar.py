"""macOS menu bar app for claude-swap (``cswap --menubar``).

A thin GUI shell over ``ClaudeAccountSwitcher`` — it never re-implements
account logic. Built on ``rumps`` (an optional extra, macOS only). The pure
helpers below (settings, formatting, plist rendering) are import-safe without
rumps so they can be unit-tested in CI; ``rumps`` is imported lazily inside
the app glue.
"""

from __future__ import annotations

import json
import plistlib
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


def tightest_pct(usage: dict | str | None) -> float | None:
    """Highest 5h/7d utilization percentage, or None if unknown.

    Mirrors ``oauth.account_headroom`` (which returns ``100 - max(pct)``) but
    surfaces the utilization itself for display. Spend is excluded — it isn't
    a rate-limit window.
    """
    if not isinstance(usage, dict):
        return None
    pcts = [
        window["pct"]
        for window in (usage.get("five_hour"), usage.get("seven_day"))
        if isinstance(window, dict) and isinstance(window.get("pct"), (int, float))
    ]
    return max(pcts) if pcts else None


def usage_summary(usage: dict | str | None) -> str:
    """One-line usage summary for an account row."""
    if isinstance(usage, str):
        return usage
    if usage is None:
        return "usage unavailable"
    parts: list[str] = []
    h5 = usage.get("five_hour")
    if isinstance(h5, dict) and isinstance(h5.get("pct"), (int, float)):
        parts.append(f"5h {h5['pct']:.0f}%")
    d7 = usage.get("seven_day")
    if isinstance(d7, dict) and isinstance(d7.get("pct"), (int, float)):
        parts.append(f"7d {d7['pct']:.0f}%")
    spend = usage.get("spend")
    if isinstance(spend, dict) and isinstance(spend.get("pct"), (int, float)):
        parts.append(f"$ {spend['pct']:.0f}%")
    return " · ".join(parts) if parts else "usage unavailable"


def format_account_label(num: int, email: str, usage: dict | str | None) -> str:
    """Build one account row's menu label."""
    return f"{num}  {email}  {usage_summary(usage)}"


def _local_part(email: str, limit: int = 12) -> str:
    """Email text before '@', truncated with a trailing '*' marker."""
    local = email.split("@", 1)[0]
    if len(local) > limit:
        return local[: limit - 1] + "*"
    return local


def format_title(
    active_email: str | None,
    active_usage: dict | str | None,
    settings: MenuBarSettings,
) -> str:
    """Build the menu-bar title from the active account and settings."""
    if active_email is None:
        return ICON
    segments: list[str] = []
    if settings.show_account_name:
        segments.append(_local_part(active_email))
    if settings.show_quota_pct:
        pct = tightest_pct(active_usage)
        if pct is not None:
            segments.append(f"{pct:.0f}%")
    if not segments:
        return ICON
    return f"{ICON} " + " · ".join(segments)


LAUNCH_AGENT_LABEL = "com.claude-swap.menubar"


def launch_agent_path() -> Path:
    """Path to the menu bar LaunchAgent plist."""
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"


def render_launch_agent(program_args: list[str]) -> bytes:
    """Render the LaunchAgent plist that starts the menu bar at login."""
    return plistlib.dumps(
        {
            "Label": LAUNCH_AGENT_LABEL,
            "ProgramArguments": list(program_args),
            "RunAtLoad": True,
        }
    )


def set_launch_at_login(enabled: bool, program_args: list[str]) -> None:
    """Install or remove the login LaunchAgent. Removal is idempotent."""
    path = launch_agent_path()
    if enabled:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(render_launch_agent(program_args))
    else:
        path.unlink(missing_ok=True)
