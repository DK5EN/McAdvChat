#!/usr/bin/env python3
"""
Centralized logging configuration for McApp.

Replaces scattered `if has_console: print(...)` patterns with proper logging.
Keeps emoji prefixes for visual scanning in logs.
"""

import logging
import sys
from typing import ClassVar

# Default format with emoji support
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s"
LOG_FORMAT_SIMPLE = "%(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class EmojiFormatter(logging.Formatter):
    """Custom formatter that keeps emoji prefixes and adds level-based prefixes."""

    LEVEL_EMOJIS: ClassVar[dict[int, str]] = {
        logging.DEBUG: "",  # No extra emoji for debug (message may have one)
        logging.INFO: "",  # No extra emoji for info
        logging.WARNING: "⚠️ ",
        logging.ERROR: "❌ ",
        logging.CRITICAL: "💥 ",
    }

    def format(self, record: logging.LogRecord) -> str:
        # Add emoji prefix for warnings/errors if not already present
        emoji = self.LEVEL_EMOJIS.get(record.levelno, "")
        if emoji and not record.getMessage().strip().startswith(tuple("⚠️❌💥🔧📡🔍🔄")):
            record.msg = f"{emoji}{record.msg}"
        return super().format(record)


def setup_logging(
    verbose: bool = False,
    console_output: bool = True,
    log_file: str | None = None,
    simple_format: bool = False,
) -> None:
    """
    Configure logging for McApp.

    Args:
        verbose: Enable DEBUG level logging (default: INFO)
        console_output: Output to stdout (default: True)
        log_file: Optional file path for log output
        simple_format: Use simplified format without timestamps (for console-like output)
    """
    level = logging.DEBUG if verbose else logging.INFO
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Clear existing handlers
    root_logger.handlers.clear()

    # Choose format
    fmt = LOG_FORMAT_SIMPLE if simple_format else LOG_FORMAT

    # Console handler
    if console_output:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(level)
        console_handler.setFormatter(EmojiFormatter(fmt, datefmt=DATE_FORMAT))
        root_logger.addHandler(console_handler)

    # File handler (optional)
    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(level)
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
        root_logger.addHandler(file_handler)


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger instance for a module.

    Usage:
        from logging_setup import get_logger
        logger = get_logger(__name__)
        logger.info("Server started on %s:%d", host, port)
        logger.debug("Message received: %s", msg_id)
    """
    return logging.getLogger(name)


def has_console() -> bool:
    """
    Check if running with a console (TTY).
    Useful for backward compatibility during migration.
    """
    return sys.stdout.isatty()


# Module-level logger for this module
logger = get_logger(__name__)
