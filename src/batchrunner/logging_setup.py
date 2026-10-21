"""Logging configuration shared by all commands.

Every handler this package installs uses :class:`RedactingFormatter`, so even a
message that accidentally contains a full number or the token is cleaned
before it reaches the console or a file.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from batchrunner.masking import RedactingFormatter

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_HANDLER_TAG = "_batchrunner_console"


def quiet_noisy_loggers() -> None:
    """httpx/httpcore log every request URL - with the full number - at INFO/DEBUG."""
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def configure_console(level: int = logging.INFO, secrets: Iterable[str] = ()) -> None:
    """(Re)install the redacting console handler on the root logger.

    Calling it again replaces the previous console handler, so a command can
    start logging before the token is known and re-configure once it is.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_TAG, False):
            root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(RedactingFormatter(LOG_FORMAT, secrets=secrets))
    setattr(handler, _HANDLER_TAG, True)
    root.addHandler(handler)
    root.setLevel(level)
    quiet_noisy_loggers()


@contextmanager
def file_log(path: Path, secrets: Iterable[str] = (), level: int = logging.INFO) -> Iterator[None]:
    """Mirror log records into ``path`` for the duration of a run."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(RedactingFormatter(LOG_FORMAT, secrets=secrets))
    handler.setLevel(level)
    root = logging.getLogger()
    root.addHandler(handler)
    if root.level > level or root.level == logging.NOTSET:
        root.setLevel(level)
    try:
        yield
    finally:
        root.removeHandler(handler)
        handler.close()
