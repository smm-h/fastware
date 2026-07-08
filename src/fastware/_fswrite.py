"""A small thread-safe file writer shared by the append/overwrite call sites.

Several modules persist small files with the same three-part shape: guard with
a lock, create the parent directory tree on demand, then append or overwrite.
:class:`LockedFileWriter` is that pattern extracted once so the audit log
(append) and the feature-flag store (overwrite) do not each reimplement it.
"""

from __future__ import annotations

import threading
from pathlib import Path


class LockedFileWriter:
    """Serialize writes to a single file, creating parent dirs on demand.

    A single :class:`threading.Lock` serializes every write so concurrent
    callers never interleave bytes. The parent directory tree is created
    lazily on the first write, so callers need not pre-create it.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()

    def append_line(self, line: str) -> None:
        """Append *line* followed by a newline, under the lock."""
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a") as f:
                f.write(line + "\n")

    def overwrite(self, text: str) -> None:
        """Replace the file's entire contents with *text*, under the lock."""
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(text)
