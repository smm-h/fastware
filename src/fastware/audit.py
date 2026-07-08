"""Append-only JSONL audit log writer for recording timestamped application events with structured payloads, using thread-safe file writes.

Each entry is a single JSON line with an ISO timestamp, event type, and
optional payload dict. Thread-safe via ``threading.Lock``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from fastware._fswrite import LockedFileWriter

__all__ = [
    "AuditLog",
]


class AuditLog:
    """Append-only JSONL audit log.

    Parameters
    ----------
    path:
        Path to the JSONL file. Created on first write if it does not exist.
    """

    def __init__(self, path: str | Path) -> None:
        self._writer = LockedFileWriter(path)

    def log(self, event_type: str, payload: dict | None = None) -> None:
        """Append a single audit entry as a JSON line.

        Each line has the shape::

            {"timestamp": "...", "event_type": "...", "payload": {...}}
        """
        entry: dict = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
        }
        if payload is not None:
            entry["payload"] = payload
        line = json.dumps(entry, separators=(",", ":"))
        self._writer.append_line(line)
