"""SQLite-backed error log for recording and querying 5xx server responses with request context, tracebacks, and timestamps for post-mortem analysis.

Provides a simple append-only store that the request timing middleware
can write to on server errors.  Each entry captures enough context for
dashboard display and post-mortem investigation.

Usage::

    from fastware.error_log import ErrorLog

    error_log = ErrorLog("errors.db")
    error_log.append(
        method="POST",
        path="/api/deploy",
        status_code=500,
        detail="Docker timeout",
        request_id="abc-123",
    )
"""

from __future__ import annotations

import queue
import sqlite3
import threading
import time
from pathlib import Path

__all__ = [
    "ErrorLog",
]


_CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS errors (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp  REAL    NOT NULL,
    method     TEXT    NOT NULL,
    path       TEXT    NOT NULL,
    status_code INTEGER NOT NULL,
    detail     TEXT    NOT NULL DEFAULT '',
    request_id TEXT    NOT NULL DEFAULT '',
    user       TEXT    NOT NULL DEFAULT '',
    traceback  TEXT    NOT NULL DEFAULT ''
)
"""

_INSERT = (
    "INSERT INTO errors "
    "(timestamp, method, path, status_code, detail, request_id, user, traceback) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)

# Sentinel enqueued by ``close()`` to stop the background writer cleanly.
_SHUTDOWN = object()


class ErrorLog:
    """Append-only SQLite error log with non-blocking writes.

    ``append()`` never touches SQLite on the calling thread.  Instead it
    enqueues the entry and a single dedicated background worker thread
    performs the ``connect``/``INSERT``/``commit`` off the event loop.  This
    keeps the ASGI event loop responsive even during 5xx bursts, when the
    request timing middleware calls ``append()`` on every failing request.

    Ordering and durability:

    - A single FIFO queue and a single writer thread preserve insertion
      order.  The timestamp is captured at ``append()`` time so it reflects
      the true event order regardless of write latency.
    - ``recent()`` (and the explicit ``flush()``) drain the queue before
      reading, so reads always observe every preceding ``append()``.
    - Errors raised by the writer (e.g. a bad path) are surfaced -- not
      swallowed -- the next time ``flush()``/``recent()`` is called.

    Args:
        path: Filesystem path for the SQLite database.  Created on
              first write if it does not exist.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._queue: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._worker_lock = threading.Lock()
        self._worker_error: BaseException | None = None

    def _ensure_worker(self) -> None:
        with self._worker_lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker_error = None
            self._worker = threading.Thread(
                target=self._run_worker,
                name="fastware-errorlog",
                daemon=True,
            )
            self._worker.start()

    def _run_worker(self) -> None:
        """Drain the queue, writing each entry with one long-lived connection."""
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(self._path)
            conn.execute(_CREATE_TABLE)
            conn.commit()
        except BaseException as exc:  # noqa: BLE001 - surfaced via flush()/recent()
            self._worker_error = exc
        try:
            while True:
                item = self._queue.get()
                try:
                    if item is _SHUTDOWN:
                        break
                    if conn is not None and self._worker_error is None:
                        conn.execute(_INSERT, item)
                        conn.commit()
                except BaseException as exc:  # noqa: BLE001 - surfaced on flush()
                    self._worker_error = exc
                finally:
                    # Always mark done so flush()'s join() can never hang.
                    self._queue.task_done()
        finally:
            if conn is not None:
                conn.close()

    def append(
        self,
        *,
        method: str,
        path: str,
        status_code: int,
        detail: str = "",
        request_id: str = "",
        user: str = "",
        traceback: str = "",
    ) -> None:
        """Enqueue an error entry for non-blocking, off-thread persistence.

        Returns immediately; the actual SQLite write happens on the background
        worker thread.  The timestamp is captured here so ordering reflects the
        true event order regardless of write latency.
        """
        self._ensure_worker()
        self._queue.put(
            (time.time(), method, path, status_code, detail, request_id, user, traceback)
        )

    def flush(self) -> None:
        """Block until all queued writes have been committed.

        Raises any error the background writer encountered while persisting
        entries, so failures are surfaced rather than silently dropped.
        """
        if self._worker is not None:
            self._queue.join()
        if self._worker_error is not None:
            raise self._worker_error

    def recent(self, limit: int = 50) -> list[dict]:
        """Return the most recent *limit* error entries, newest first.

        Pending queued writes are flushed first, so the result reflects every
        preceding ``append()``.
        """
        self.flush()
        conn = sqlite3.connect(self._path)
        try:
            conn.execute(_CREATE_TABLE)
            conn.commit()
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM errors ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def close(self) -> None:
        """Gracefully stop the background writer after draining pending writes."""
        with self._worker_lock:
            worker = self._worker
            self._worker = None
        if worker is None:
            return
        self._queue.put(_SHUTDOWN)
        worker.join(timeout=5)
