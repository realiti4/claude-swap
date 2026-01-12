"""Logging configuration for Claude Swap."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(log_dir: Path, debug: bool = False) -> logging.Logger:
    """Setup logging with file and optional console output.

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

    # Ensure log directory exists
    log_dir.mkdir(parents=True, exist_ok=True)

    # File handler - always log to file
    log_file = log_dir / "claude-swap.log"
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=1024 * 1024,  # 1MB
        backupCount=3,
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
