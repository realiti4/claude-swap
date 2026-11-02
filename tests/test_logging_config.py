"""Tests for claude_swap.logging_config."""

from __future__ import annotations

import logging
from pathlib import Path

from claude_swap.logging_config import setup_logging


def test_setup_does_not_create_dir(tmp_path: Path):
    """Calling setup_logging must not materialize the log directory.

    The log dir lives under the cswap backup root; pre-creating it laid down
    cache/log artifacts that later tripped the legacy → XDG migration
    collision check (see paths.migrate_legacy_backup_dir).
    """
    log_dir = tmp_path / "should-not-exist"
    logger = setup_logging(log_dir)
    try:
        assert not log_dir.exists()
        # File handler is registered but stays unopened until first emit.
        assert logger.handlers
    finally:
        for handler in logger.handlers[:]:
            handler.close()
            logger.removeHandler(handler)


def test_dir_is_created_on_first_log(tmp_path: Path):
    log_dir = tmp_path / "lazy"
    logger = setup_logging(log_dir)
    try:
        assert not log_dir.exists()
        logger.warning("trigger")
        for handler in logger.handlers:
            handler.flush()
        assert log_dir.is_dir()
        assert (log_dir / "claude-swap.log").exists()
    finally:
        for handler in logger.handlers[:]:
            handler.close()
            logger.removeHandler(handler)


def test_debug_adds_console_handler(tmp_path: Path):
    log_dir = tmp_path / "dbg"
    logger = setup_logging(log_dir, debug=True)
    try:
        assert logger.level == logging.DEBUG
        assert any(
            isinstance(h, logging.StreamHandler)
            and not isinstance(h, logging.FileHandler)
            for h in logger.handlers
        )
    finally:
        for handler in logger.handlers[:]:
            handler.close()
            logger.removeHandler(handler)
