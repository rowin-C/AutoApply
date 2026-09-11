"""Logging setup: console + rotating file under data/logs/."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s %(message)s"
_NOISY = ("urllib3", "playwright", "asyncio", "websockets")


def setup_logging(log_dir: Path, level: str = "INFO", console: bool = True) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level.upper())
    fmt = logging.Formatter(_FORMAT)

    _INSTALLED = getattr(root, "_aa_handlers", None)

    if console and _INSTALLED != "both":
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(fmt)
        handler.setLevel(level.upper())
        root.addHandler(handler)

    if _INSTALLED != "both":
        file_handler = RotatingFileHandler(
            log_dir / "autoapply.log",
            maxBytes=5_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        file_handler.setLevel(logging.DEBUG)
        root.addHandler(file_handler)
        root._aa_handlers = "both" if console else "file"

    for noisy in _NOISY:
        logging.getLogger(noisy).setLevel(logging.WARNING)
