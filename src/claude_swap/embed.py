"""Embed cswap into Claude Code for managed (load-balanced) sessions.

The load balancer only manages sessions launched via ``cswap launch``, each in
its own per-session profile under ``managed/``. "Embedding" means installing the
cswap statusline + effort QoL into that profile so the session reports its usage
back to the registry and renders the compact balancer line.

Crucially it is installed as a **real, merged** ``settings.json`` inside the
managed profile — the user-settings layer Claude Code definitely reads from a
``CLAUDE_CONFIG_DIR`` (``settings.local.json`` may be ignored there). The merge
keeps the user's MCP servers / hooks / plugins from ``~/.claude/settings.json``
but strips inherited auth-override env keys and lets our statusLine + effort win.
The user's real settings are never touched (we never write through the share
symlink), and plain ``claude`` / ``cswap run`` sessions stay completely vanilla.
A canonical template under ``managed/_template/settings.json`` records what we
install, so ``cswap --status`` can report embed health and the upgrade migration
can refresh it on a new version.

Stdlib only; reuses ``switcher._write_json`` for atomic, chmod-0600 writes.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

# Persistable approximation of the session-only "ultracode" effort: the highest
# effort level that can be set via settings/CLI. (True ultracode is set in-session
# via /effort and cannot be persisted.)
EFFORT_LEVEL = "xhigh"

TEMPLATE_DIRNAME = "_template"


def _cswap_command(subcommand: str) -> str:
    """Return the shell command Claude Code should run for ``cswap <subcommand>``.

    Prefers a resolvable ``cswap`` on PATH; otherwise falls back to invoking the
    package with the current interpreter (``python -m claude_swap <subcommand>``),
    which always works for the installed environment.
    """
    if shutil.which("cswap"):
        return f"cswap {subcommand}"
    return f'"{sys.executable}" -m claude_swap {subcommand}'


def cswap_statusline_command() -> str:
    """Return the shell command Claude Code should run as the statusLine."""
    return _cswap_command("statusline")


def cswap_statusfailure_command() -> str:
    """Return the command Claude Code should run for the ``StopFailure`` hook.

    Resolved like the statusLine command (PATH or interpreter fallback).
    """
    return _cswap_command("statusfailure")


def build_managed_settings() -> dict:
    """The cswap-owned settings merged into every managed profile.

    Carries the statusLine (the balancer's event-driven trigger + display), the
    effort QoL default, and a ``StopFailure`` hook: a next-turn safety net that
    fires when a turn fails after claude exhausts its API retries (e.g. a hard
    rate limit) and migrates the session to a fresh account so the NEXT turn
    recovers without the user switching manually.
    """
    return {
        "statusLine": {"type": "command", "command": cswap_statusline_command()},
        "effortLevel": EFFORT_LEVEL,
        "hooks": {
            "StopFailure": [
                {"hooks": [{"type": "command", "command": cswap_statusfailure_command()}]}
            ]
        },
    }


def _merge_hooks(base_hooks: object, managed_hooks: object) -> dict:
    """Merge the user's ``hooks`` block with cswap's managed hooks (per event).

    Claude Code's ``hooks`` is ``{event_name: [matcher_group, ...]}``. We keep
    every event the user defines and append our matcher groups for the events we
    own (currently ``StopFailure``), so a user with their own ``StopFailure``
    hook keeps it and ours runs alongside it. Tolerant of a non-dict base.
    """
    merged: dict = dict(base_hooks) if isinstance(base_hooks, dict) else {}
    if not isinstance(managed_hooks, dict):
        return merged
    for event, groups in managed_hooks.items():
        existing = merged.get(event)
        if isinstance(existing, list):
            merged[event] = existing + list(groups)
        else:
            merged[event] = list(groups)
    return merged


def _has_stopfailure_hook(settings: dict) -> bool:
    """Whether ``settings`` carries a ``StopFailure`` -> ``statusfailure`` hook."""
    hooks = settings.get("hooks")
    groups = hooks.get("StopFailure") if isinstance(hooks, dict) else None
    if not isinstance(groups, list):
        return False
    for group in groups:
        for hook in (group or {}).get("hooks", []) if isinstance(group, dict) else []:
            cmd = hook.get("command", "") if isinstance(hook, dict) else ""
            if "statusfailure" in cmd:
                return True
    return False


def managed_template_path(switcher) -> Path:
    return switcher.managed_dir / TEMPLATE_DIRNAME / "settings.json"


def write_managed_template(switcher) -> bool:
    """Write/refresh the canonical managed-profile template. Returns True if changed.

    Idempotent: a no-op when the on-disk template already matches.
    """
    path = managed_template_path(switcher)
    path.parent.mkdir(parents=True, exist_ok=True)
    desired = build_managed_settings()
    try:
        if path.exists() and json.loads(path.read_text(encoding="utf-8")) == desired:
            return False
    except (OSError, json.JSONDecodeError):
        pass
    switcher._write_json(path, desired)
    return True


def _drop_settings_from_share_manifest(profile_dir: Path) -> None:
    """Remove ``settings.json`` from the profile's share manifest.

    The sharing sync symlinks ``settings.json`` from ``~/.claude`` and records
    it as cswap-managed. Since we replace it with a real merged file, drop it
    from the manifest so a later sync treats our file as the profile's own copy
    and never re-symlinks over it. Tolerant of a missing/corrupt manifest.
    """
    from claude_swap.session import SHARE_MANIFEST

    manifest_path = profile_dir / SHARE_MANIFEST
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        items = data.get("items", [])
    except (OSError, json.JSONDecodeError, AttributeError):
        return
    if "settings.json" not in items:
        return
    data["items"] = [i for i in items if i != "settings.json"]
    manifest_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def install_into_profile(switcher, profile_dir: Path) -> None:
    """Install the statusline + effort QoL into a managed profile.

    Writes a real, merged ``settings.json`` (the user-settings layer Claude Code
    reads for a ``CLAUDE_CONFIG_DIR``): the user's ``~/.claude/settings.json`` as
    a base (so managed sessions keep their MCP servers / hooks / plugins), with
    inherited auth-override env keys stripped, and our statusLine + effort layered
    on top. MUST be called AFTER the profile's sharing sync: it replaces the
    shared ``settings.json`` symlink with a real file (never writing through the
    symlink into ``~/.claude``) and drops it from the share manifest so a later
    sync won't re-symlink over it.
    """
    from claude_swap.session import AUTH_OVERRIDE_ENV_VARS

    profile_dir = Path(profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)

    user_settings = Path.home() / ".claude" / "settings.json"
    try:
        base = json.loads(user_settings.read_text(encoding="utf-8"))
        if not isinstance(base, dict):
            base = {}
    except (OSError, json.JSONDecodeError):
        base = {}

    # A managed session must use its profile's subscription creds, never an
    # inherited API key carried in the user's settings env.
    env = base.get("env")
    if isinstance(env, dict):
        base["env"] = {k: v for k, v in env.items() if k not in AUTH_OVERRIDE_ENV_VARS}

    managed = build_managed_settings()
    merged = {**base, **managed}
    # Shallow-merging ``hooks`` would clobber the user's own hooks. Deep-merge
    # per event so the user keeps their hooks and our StopFailure safety net is
    # added (or, for StopFailure, appended) rather than replacing them.
    merged["hooks"] = _merge_hooks(base.get("hooks"), managed.get("hooks"))

    dest = profile_dir / "settings.json"
    # Never write through the share symlink (that would mutate ~/.claude).
    if dest.is_symlink() or dest.exists():
        dest.unlink()
    switcher._write_json(dest, merged)

    _drop_settings_from_share_manifest(profile_dir)


def embed_health(switcher) -> dict:
    """Report whether cswap is embedded so new managed sessions auto-balance.

    Returns ``{"ok", "issues", "template_ok", "cswap_on_path"}``. ``ok`` is True
    once the managed template is installed and current — the statusline command
    always resolves (PATH or the interpreter fallback), so ``cswap_on_path`` is
    informational only.
    """
    issues: list[str] = []
    tpath = managed_template_path(switcher)
    template_ok = False
    try:
        if tpath.exists():
            t = json.loads(tpath.read_text(encoding="utf-8"))
            sl = t.get("statusLine") if isinstance(t, dict) else None
            if (
                isinstance(sl, dict)
                and sl.get("command")
                and t.get("effortLevel") == EFFORT_LEVEL
                and _has_stopfailure_hook(t)
            ):
                template_ok = True
    except (OSError, json.JSONDecodeError):
        template_ok = False
    if not template_ok:
        issues.append("managed-session template not installed")

    cswap_on_path = shutil.which("cswap") is not None
    return {
        "ok": not issues,
        "issues": issues,
        "template_ok": template_ok,
        "cswap_on_path": cswap_on_path,
    }


def install(switcher) -> dict:
    """One-time embed setup (idempotent): write the managed template.

    Returns :func:`embed_health` after installing.
    """
    switcher._setup_directories()
    write_managed_template(switcher)
    return embed_health(switcher)
