"""Shared atomic write helper for mailbox cursor.json and the assignment ledger."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write `text` to `path` without ever leaving a truncated file behind.

    Writes to a same-directory temp file, fsyncs it, then os.replace()s it
    over `path` — a crash mid-write leaves either the old file or the new
    one, never a torn one. The temp name carries our pid, thread id, and a
    nanosecond timestamp so two concurrent writers — even two threads in the
    same process — never collide on it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(
        path.name + f".tmp.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}"
    )
    try:
        tmp.write_text(text, encoding=encoding)
        fd = os.open(tmp, os.O_WRONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def atomic_write_json(path: Path, obj, *, indent: int = 2) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=indent))
