"""Logging to stderr and a rotating log file in the app data directory."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def configure_logging(log_file: Path | None, *, verbose: bool = False) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.handlers.clear()

    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.WARNING)
    console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    root.addHandler(console)

    if log_file is not None:
        file_handler = RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter(FORMAT))
        root.addHandler(file_handler)
