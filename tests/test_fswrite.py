"""Tests for the shared LockedFileWriter utility (fastware._fswrite).

Locks in the append/overwrite/parent-dir-creation/thread-safety behavior that
the audit log and feature-flag store now delegate to.
"""

from __future__ import annotations

import threading
from pathlib import Path

from fastware._fswrite import LockedFileWriter


def test_append_line_creates_parent_dirs(tmp_path: Path):
    target = tmp_path / "nested" / "deep" / "log.txt"
    LockedFileWriter(target).append_line("hello")
    assert target.exists()
    assert target.read_text() == "hello\n"


def test_append_line_accumulates(tmp_path: Path):
    w = LockedFileWriter(tmp_path / "log.txt")
    w.append_line("a")
    w.append_line("b")
    assert (tmp_path / "log.txt").read_text() == "a\nb\n"


def test_overwrite_replaces_and_creates_parent(tmp_path: Path):
    target = tmp_path / "sub" / "state.json"
    w = LockedFileWriter(target)
    w.overwrite("first")
    w.overwrite("second")
    assert target.read_text() == "second"


def test_append_line_thread_safe(tmp_path: Path):
    target = tmp_path / "log.txt"
    w = LockedFileWriter(target)
    n_threads, n_lines = 10, 50

    def writer(tid: int) -> None:
        for i in range(n_lines):
            w.append_line(f"{tid}-{i}")

    threads = [threading.Thread(target=writer, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = target.read_text().strip().split("\n")
    # No interleaving/loss: every line is intact and the total count is exact.
    assert len(lines) == n_threads * n_lines
    assert all("-" in line for line in lines)
