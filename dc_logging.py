from __future__ import annotations

import logging
import os
from typing import Final

LOGGER_NAME: Final[str] = "dc"


def _coerce_level(raw_level: str | None) -> int:
    if not raw_level:
        return logging.INFO
    level_name = raw_level.strip().upper()
    level = logging.getLevelName(level_name)
    if isinstance(level, int):
        return level
    return logging.INFO


def configure_dc_logging(level: str | None = None) -> logging.Logger:
    """Configure the DC-Agent logger namespace without taking over AstrBot logs."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(_coerce_level(level or os.getenv("DC_LOG_LEVEL")))
    logger.propagate = True
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger
