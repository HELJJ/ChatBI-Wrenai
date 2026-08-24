"""Uvicorn entrypoint: ``uvicorn wren_chat_api.main:app``."""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from wren_chat_api.app import create_app


def _configure_logging() -> None:
    """Timestamped console logging, plus an optional rotating file sink.

    Set ``WREN_CHAT_LOG_FILE=/path/to/log`` to persist service diagnostics
    (extraction run ids, anomaly lists, upstream failures) for post-hoc
    tracing; the file rotates at 20 MiB and keeps 5 backups.
    """
    root = logging.getLogger()
    if root.handlers:  # already configured (e.g. by a supervisor)
        return
    root.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)
    file_path = os.environ.get("WREN_CHAT_LOG_FILE")
    if file_path:
        file_handler = RotatingFileHandler(
            file_path,
            maxBytes=20 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)


_configure_logging()
app = create_app()
