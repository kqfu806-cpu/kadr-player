from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_error_logging(
    path: Path, max_bytes: int = 2_000_000, backup_count: int = 5
) -> RotatingFileHandler:
    """Write backend warnings and errors to a bounded rotating log."""
    resolved_path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    root_logger = logging.getLogger()
    for handler in root_logger.handlers:
        if (
            isinstance(handler, RotatingFileHandler)
            and Path(handler.baseFilename) == resolved_path
        ):
            return handler

    handler = RotatingFileHandler(
        resolved_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setLevel(logging.WARNING)
    handler.addFilter(
        lambda record: record.name.startswith("kadr.")
        and record.levelno >= logging.WARNING
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root_logger.addHandler(handler)
    return handler
