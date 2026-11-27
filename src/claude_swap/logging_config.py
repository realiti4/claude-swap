"""Logging configuration for Claude Swap."""

import logging
from functools import lru_cache
from logging.handlers import RotatingFileHandler
from pathlib import Path


class _LazyDirRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler that creates its parent dir on first emit.

    Keeps the backup root from being materialized just because the switcher
    was instantiated. Necessary so a no-op run (e.g. ``cswap --status`` with
    no managed accounts) doesn't lay down ``cache/`` or log files inside the
    XDG path, which would later trip the legacy → XDG migration collision
    check if a legacy directory appeared between runs.
    """

    def _open(self):  # type: ignore[override]
        Path(self.baseFilename).parent.mkdir(parents=True, exist_ok=True)
        return super()._open()


@lru_cache(maxsize=None)
def decision_logger(log_dir: Path) -> logging.Logger:
    """The auto-switch tick-reasoning logger for one backup dir.

    Memoized because `logging.Handler.__init__` registers itself in the
    module's shutdown list, so a per-engine handler is never collected: every
    TUI dry-run/LIVE toggle built a new engine and leaked one open file, and
    two `RotatingFileHandler`s on one path rotate it independently.
    """
    # Constructed, not ``getLogger``: with no parent it cannot spill per-tick
    # records into the rotating claude-swap.log. No formatter: the default is
    # ``%(message)s`` and the caller supplies the UTC ``event.ts``
    # (``%(asctime)s`` is naive LOCAL, which every other record of the same
    # decision is not).
    logger = logging.Logger("claude-swap.decisions", logging.INFO)
    logger.addHandler(_LazyDirRotatingFileHandler(
        log_dir / "autoswitch-decisions.log",
        maxBytes=1024 * 1024, backupCount=2, delay=True,
    ))
    return logger


def setup_logging(log_dir: Path, debug: bool = False) -> logging.Logger:
    """Setup logging with file and optional console output.

    The log directory is *not* created eagerly; it materializes on the first
    log record actually written, via ``_LazyDirRotatingFileHandler``.

    Args:
        log_dir: Directory to store log files.
        debug: Enable debug logging to console.

    Returns:
        Configured logger instance.
    """
    logger = logging.getLogger("claude-swap")
    logger.setLevel(logging.DEBUG if debug else logging.INFO)

    # Clear any existing handlers
    logger.handlers.clear()

    # File handler - opens lazily so the dir is only created when something
    # is actually logged.
    log_file = log_dir / "claude-swap.log"
    file_handler = _LazyDirRotatingFileHandler(
        log_file,
        maxBytes=1024 * 1024,  # 1MB
        backupCount=3,
        delay=True,
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    )
    logger.addHandler(file_handler)

    # Console handler for debug mode
    if debug:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.DEBUG)
        console_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        logger.addHandler(console_handler)

    return logger
