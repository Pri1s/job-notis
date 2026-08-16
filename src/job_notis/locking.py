"""Single-process locking for scheduled checks."""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO


class AlreadyRunningError(RuntimeError):
    """Raised when another check already owns the application lock."""


@contextmanager
def single_process_lock(path: Path) -> Iterator[None]:
    """Hold an advisory, non-blocking lock for the duration of one run."""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle: TextIO = path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AlreadyRunningError("Another Job Notis check is already running.") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
