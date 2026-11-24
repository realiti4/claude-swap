"""Model control for the auto engine: which model each account should run.

Turned on with ``autoswitch.modelControl``. Off by default. It does two
related jobs.

1. Decide which per-model windows count
---------------------------------------
Some plans report a separate weekly window for one model (for example
"Fable") on top of the account-wide 7-day window. ``autoswitch.model`` tells
the engine to treat those windows as limits. Without model control, a spent
Fable window makes the whole account look spent, even when nobody on this
computer is using Fable and the account still has plenty of general weekly
room.

With model control, a per-model window only counts for an account while that
model is actually in use, meaning either:

* Claude Code on this computer used that model within the last
  ``autoswitch.modelGraceSeconds`` (read from the session logs by
  :mod:`claude_swap.tokens`), or
* the model cswap is about to set for that account is that model.

Otherwise the window is left out of that account's decision.

2. Set Claude Code's default model
----------------------------------
``autoswitch.modelMap`` (for example
``"1=claude-fable-5-2,2=claude-opus-5-5"``) names a default model per slot.
When a slot becomes active, that model is written to the ``model`` key of
Claude Code's ``settings.json``. If that model's own window is over its line,
``autoswitch.fallbackModel`` is written instead (as long as the fallback is
not over its line too), so the account stays usable on another model rather
than being abandoned.

Only the ``model`` key is ever changed. The value that was there before is
saved in ``model_state.json``, and it is put back once cswap no longer needs
to set anything, unless someone changed the key in the meantime (then their
choice stays).

A change to ``settings.json`` applies to Claude Code sessions started after
it. Sessions already open keep their model until ``/model`` is used in them.
When that matters, the engine emits a hint saying so.

Limitation: the ``all`` sentinel of ``autoswitch.model`` names no model, so
model control has nothing to match it against and leaves every window in.
Name the models (``Fable``, ``Fable,Opus``) to use it.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import NamedTuple

from claude_swap.lines import window_line
from claude_swap.paths import get_claude_config_home

# Remembers what cswap wrote and what was there before. Lives in cswap's own
# data directory, never in Claude Code's. Keys:
#   "saved"   - the ``model`` value before cswap first changed it (may be null)
#   "applied" - the value cswap last wrote
STATE_FILENAME = "model_state.json"


class ModelView(NamedTuple):
    """What model control decided for one account. See :func:`view_usage`."""

    usage: dict | str | None
    """The account's usage with per-model windows for unused models removed."""

    model_to_set: str | None
    """Model cswap should write while this account is active, or None to leave it."""

    using_fallback: bool
    """True when ``model_to_set`` is the fallback because the usual model is over its line."""

    ignored_windows: list[str]
    """Names of the per-model windows that were removed, for display."""


# -- settings parsing ---------------------------------------------------------


def parse_model_map(value: str | None) -> dict[str, str]:
    """Parse ``autoswitch.modelMap`` into ``{slot number: model id}``.

    ``"1=claude-fable-5-2, 2=claude-opus-5-5"`` ->
    ``{"1": "claude-fable-5-2", "2": "claude-opus-5-5"}``. Malformed parts
    (no ``=``, a non-numeric slot, an empty model) are skipped.
    """
    model_map: dict[str, str] = {}
    for part in (value or "").split(","):
        slot_text, separator, model_text = part.partition("=")
        slot, model = slot_text.strip(), model_text.strip()
        if separator and slot.isdigit() and model:
            model_map[slot] = model
    return model_map


def matches(window_name: str, model: str | None) -> bool:
    """Does the per-model window ``window_name`` govern ``model``?

    Window names are display names ("Fable"). Model ids contain them in
    lower case ("claude-fable-5-2"), so this is a case-insensitive substring
    test.
    """
    return bool(model) and window_name.lower() in model.lower()


# -- which models are in use --------------------------------------------------


def models_in_use(
    ledger, models: tuple[str, ...], grace_seconds: float, now: float
) -> dict[str, bool]:
    """Return ``{lower-case window name: used recently?}`` for each name in ``models``.

    "Recently" means Claude Code on this computer made a request with that
    model within ``grace_seconds`` of ``now``. ``ledger`` is a
    :class:`claude_swap.tokens.Ledger`. The engine and the watch screen both
    call this, so they always agree on what is in use.
    """
    in_use: dict[str, bool] = {}
    for name in models:
        last_seen = ledger.model_last_seen(name)
        in_use[name.lower()] = last_seen is not None and now - last_seen <= grace_seconds
    return in_use


# -- Claude Code's settings.json ----------------------------------------------


def settings_path() -> Path:
    """Path of Claude Code's user ``settings.json``."""
    return get_claude_config_home() / "settings.json"


def read_model() -> tuple[bool, str | None]:
    """Read the ``model`` key from Claude Code's ``settings.json``.

    Returns ``(readable, model)``:

    * no file at all: ``(True, None)``, which is a valid state meaning
      Claude Code's default;
    * a file that is not valid JSON or not an object: ``(False, None)``,
      so the caller knows not to write to it;
    * otherwise ``(True, value)``, with ``None`` when the key is absent
      or empty.
    """
    try:
        raw = json.loads(settings_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return True, None
    except (OSError, ValueError):
        return False, None
    if not isinstance(raw, dict):
        return False, None
    value = raw.get("model")
    if isinstance(value, str) and value:
        return True, value
    return True, None


def write_model(model: str | None) -> None:
    """Set the ``model`` key in Claude Code's ``settings.json``, or remove it if ``model`` is None.

    Every other key is kept as it was. The file is replaced atomically (write
    a temporary file, then rename), so Claude Code never reads a half-written
    file, and the original file's permission bits are kept. A missing file is
    created with mode 0600.

    Raises ``OSError`` if the file cannot be read or written, and
    ``ValueError`` if it is not a JSON object. It is never overwritten in
    that case, since it may hold settings someone wants to keep.
    """
    path = settings_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        raw, mode = {}, 0o600
    if not isinstance(raw, dict):
        raise ValueError(f"{path} is not a JSON object; not touching it")

    if model is None:
        raw.pop("model", None)
    else:
        raw["model"] = model

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.cswap.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


# -- cswap's own record of what it changed ------------------------------------


def load_state(backup_root: Path) -> dict:
    """Read ``model_state.json`` (see :data:`STATE_FILENAME`). Missing or damaged gives ``{}``."""
    try:
        raw = json.loads((backup_root / STATE_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def save_state(backup_root: Path, state: dict) -> None:
    """Write ``model_state.json`` atomically, readable by this user only."""
    path = backup_root / STATE_FILENAME
    tmp = path.with_name(f".{STATE_FILENAME}.{os.getpid()}.tmp")
    old_umask = os.umask(0o077)
    try:
        tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        os.umask(old_umask)


# -- the decision -------------------------------------------------------------


def _scoped_windows(usage: dict) -> list[dict]:
    """The per-model ("scoped") windows of a usage reading, only well-formed entries."""
    return [w for w in usage.get("scoped") or [] if isinstance(w, dict)]


def _is_over_line(window: dict, line: float, weekly: float | None) -> bool:
    """Is this per-model window at or above its line? A window with no figure is not."""
    pct = window.get("pct")
    if not isinstance(pct, (int, float)):
        return False
    return pct >= window_line(window.get("name") or "", line, weekly)


def view_usage(
    usage,
    num: str,
    *,
    models: tuple[str, ...],
    model_map: Mapping[str, str],
    configured: str | None,
    fallback: str | None,
    line: float,
    weekly: float | None,
    recent: Mapping[str, bool],
) -> ModelView:
    """Decide which model account ``num`` should run, and which windows bind it.

    Arguments:
        usage: the account's usage reading, as the engine holds it.
        num: the account's slot number.
        models: per-model window names from ``autoswitch.model``.
        model_map: parsed ``autoswitch.modelMap``.
        configured: the ``model`` currently in Claude Code's settings.json.
            It stands in for a slot that has no ``modelMap`` entry.
        fallback: ``autoswitch.fallbackModel``.
        line, weekly: the account's thresholds, for :func:`window_line`.
        recent: output of :func:`models_in_use`.

    The steps:

    1. *Intended model*: the slot's ``modelMap`` entry, or else whatever is
       configured now.
    2. *Fallback?* Use the fallback model if a per-model window that governs
       the intended model is over its line, a fallback is set, and the
       fallback's own window (if it has one) is not over its line too. When
       no model is intended at all, a window counts as governing if its
       model was used recently.
    3. *Filter windows*: leave out each per-model window that governs
       neither the model in effect (intended, or fallback) nor a model used
       recently. What remains is what the engine ranks on.

    Pure (no I/O). The engine and the watch screen both call it, so they
    cannot show different answers.
    """
    mapped_model = model_map.get(num)
    if not isinstance(usage, dict):
        # No reading to filter (an error string or nothing yet). Still report
        # the mapped model, so switching to this account sets it.
        return ModelView(usage, mapped_model, False, [])

    intended = mapped_model if mapped_model is not None else configured
    watched_names = {m.lower() for m in models}
    scoped = _scoped_windows(usage)

    def governs_intended(window_name: str) -> bool:
        if intended is not None:
            return matches(window_name, intended)
        return recent.get(window_name.lower(), False)

    # Step 2: is the intended model blocked by its own window?
    intended_is_over = any(
        (w.get("name") or "").lower() in watched_names
        and governs_intended(w.get("name") or "")
        and _is_over_line(w, line, weekly)
        for w in scoped
    )
    fallback_is_over = any(
        matches(w.get("name") or "", fallback) and _is_over_line(w, line, weekly)
        for w in scoped
    )
    using_fallback = bool(intended_is_over and fallback and not fallback_is_over)
    model_in_effect = fallback if using_fallback else intended

    # Step 3: drop the windows of models that are not in play.
    kept: list = []
    ignored: list[str] = []
    for window in usage.get("scoped") or []:
        name = (window.get("name") or "") if isinstance(window, dict) else ""
        not_in_play = (
            name.lower() in watched_names
            and not matches(name, model_in_effect)
            and not recent.get(name.lower(), False)
        )
        if not_in_play:
            ignored.append(name)
        else:
            kept.append(window)

    if ignored:
        filtered = dict(usage)
        filtered["scoped"] = kept
    else:
        filtered = usage  # unchanged: hand back the same object

    model_to_set = fallback if using_fallback else mapped_model
    return ModelView(filtered, model_to_set, using_fallback, ignored)
