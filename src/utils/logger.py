"""Structured logging with rotation."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logger(
    name: str = "polymarket_bot",
    level: str = "INFO",
    log_dir: str = "logs",
) -> logging.Logger:
    """Configure and return the application logger.

    - Console handler (stdout) with concise format
    - Rotating file handler (max 50 MB, 2 backups)
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # already configured

    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)

    # File (rotating)
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        f"{log_dir}/{name}.log",
        maxBytes=50 * 1024 * 1024,  # 50 MB
        backupCount=2,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    return logger


# Module-level default logger
logger = setup_logger()
