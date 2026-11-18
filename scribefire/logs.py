"""Logging setup and the failure guard used around risky subsystems.

The hotkey listener, the audio callback and the transcription path all run
outside the main flow.  A single unhandled exception in any of them must not
kill the whole app silently, so every one of them is wrapped in
:func:`guarded`.
"""

from __future__ import annotations

import functools
import logging
import logging.handlers
import os
import platform
import sys
import traceback
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar

from .config import log_dir

F = TypeVar("F", bound=Callable[..., Any])

_LOGGER_NAME = "scribefire"
_configured = False


def log_path() -> Path:
    return log_dir() / "scribefire.log"


def setup_logging(level: str = "INFO", to_console: bool = True) -> logging.Logger:
    """Configure and return the root Scribefire logger.

    Logs go to the per-OS user log directory with rotation so the file cannot
    grow without bound.
    """
    global _configured
    logger = logging.getLogger(_LOGGER_NAME)
    if _configured:
        return logger

    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    logger.propagate = False

    try:
        directory = log_dir()
        directory.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            log_path(), maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        )
        logger.addHandler(handler)
    except Exception:  # pragma: no cover - unwritable log dir
        pass

    if to_console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
        logger.addHandler(stream)

    _configured = True
    return logger


def get_logger(name: Optional[str] = None) -> logging.Logger:
    if not _configured:
        setup_logging()
    if name:
        return logging.getLogger(f"{_LOGGER_NAME}.{name}")
    return logging.getLogger(_LOGGER_NAME)


def guarded(name: str):
    """Decorator that swallows and logs exceptions raised by a callable.

    Used for hotkey callbacks, the audio callback and the transcription
    worker so one failure does not take the application down.
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            log = get_logger(name)
            try:
                return func(*args, **kwargs)
            except Exception:
                log.error("unhandled error in %s.%s\n%s", name, func.__name__, traceback.format_exc())
                return None

        return wrapper  # type: ignore[return-value]

    return decorator


def log_exception(message: str) -> None:
    """Log an exception with a full traceback without re-raising."""
    get_logger("error").error("%s\n%s", message, traceback.format_exc())
