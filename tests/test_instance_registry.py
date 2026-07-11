"""Tests for the instance registry: register/enumerate/deregister and stale pruning.

The base design is one JSON file per instance under a per-directory registry
dir; readers prune descriptors whose PID is dead. Unit tests exercise the
write/enumerate/cleanup functions directly; one integration test drives a real
``serve_background`` child and confirms it registers itself.
"""

from __future__ import annotations

import os
import urllib.request
from pathlib import Path

import msgspec

from fastware.server import (
    RegistryEntry,
    _registry_dir,
    _registry_entry_path,
    deregister_instance,
    list_instances,
    list_markers,
    register_instance,
    remove_marker,
    serve_background,
    stop,
    write_marker,
)


def test_register_then_list_round_trip(tmp_path: Path) -> None:
    pid_path = tmp_path / "app.pid"
    register_instance(pid_path, port=8123, name="myapp")

    entries = list_instances(pid_path)
    assert len(entries) == 1
    entry = entries[0]
    assert isinstance(entry, RegistryEntry)
    assert entry.pid == os.getpid()
    assert entry.port == 8123
    assert entry.name == "myapp"


def test_deregister_removes_entry(tmp_path: Path) -> None:
    pid_path = tmp_path / "app.pid"
    register_instance(pid_path, port=8123, name="myapp")
    assert list_instances(pid_path)

    deregister_instance(pid_path)
    assert list_instances(pid_path) == []


def test_list_empty_when_no_registry(tmp_path: Path) -> None:
    assert list_instances(tmp_path / "nothing.pid") == []


def test_registry_dir_layout(tmp_path: Path) -> None:
    pid_path = tmp_path / "sub" / "app.pid"
    assert _registry_dir(pid_path) == tmp_path / "sub" / ".fastware-registry"
    assert _registry_entry_path(pid_path, 42) == (
        tmp_path / "sub" / ".fastware-registry" / "42.json"
    )


def test_stale_entry_pruned_on_read(tmp_path: Path) -> None:
    """A descriptor for a dead PID is deleted and excluded on list."""
    pid_path = tmp_path / "app.pid"
    reg_dir = _registry_dir(pid_path)
    reg_dir.mkdir(parents=True, exist_ok=True)

    # A PID that is essentially guaranteed not to be alive.
    dead_pid = 2**31 - 1
    stale_file = _registry_entry_path(pid_path, dead_pid)
    stale_file.write_bytes(
        msgspec.json.encode({"pid": dead_pid, "port": 9999, "name": "ghost"})
    )

    # Also register a live entry alongside the stale one.
    register_instance(pid_path, port=8123, name="live")

    entries = list_instances(pid_path)
    assert [e.pid for e in entries] == [os.getpid()]
    assert not stale_file.exists()  # pruned on read


def test_corrupt_entry_pruned_on_read(tmp_path: Path) -> None:
    pid_path = tmp_path / "app.pid"
    reg_dir = _registry_dir(pid_path)
    reg_dir.mkdir(parents=True, exist_ok=True)
    corrupt = reg_dir / "abc.json"
    corrupt.write_bytes(b"not json{{{")

    assert list_instances(pid_path) == []
    assert not corrupt.exists()


def test_serve_background_registers_instance(tmp_path: Path) -> None:
    """A real background child registers itself with its PID, port, and name."""
    from tests.target_app_module import app

    pid_path = tmp_path / "bg.pid"
    url = serve_background(
        app, host="127.0.0.1", port=0, pid_path=pid_path, name="bgapp"
    )
    try:
        # Confirm it is actually serving.
        with urllib.request.urlopen(f"{url}/", timeout=5) as resp:
            assert resp.status == 200

        entries = list_instances(pid_path)
        assert len(entries) == 1
        entry = entries[0]
        assert entry.name == "bgapp"
        # Port matches the one embedded in the returned URL.
        assert str(entry.port) == url.rsplit(":", 1)[1]
        # PID matches the running child (alive).
        os.kill(entry.pid, 0)
    finally:
        stop(pid_path)

    # After stop, the child's atexit deregisters; a stale entry (if any) is
    # pruned on the next read.
    assert list_instances(pid_path) == []


# ---------------------------------------------------------------------------
# Presence markers (generic per-file signals beside instance descriptors)
# ---------------------------------------------------------------------------


def test_write_then_list_marker_round_trip(tmp_path: Path) -> None:
    pid_path = tmp_path / "app.pid"
    write_marker(pid_path, "window", "w1", fields={"window_id": "w1"})

    markers = list_markers(pid_path, "window")
    assert len(markers) == 1
    m = markers[0]
    assert m["pid"] == os.getpid()
    assert m["marker_id"] == "w1"
    assert m["window_id"] == "w1"
    assert m["kind"] == "window"


def test_remove_marker_round_trip(tmp_path: Path) -> None:
    pid_path = tmp_path / "app.pid"
    write_marker(pid_path, "window", "w1")
    assert list_markers(pid_path, "window")

    remove_marker(pid_path, "window", "w1")
    assert list_markers(pid_path, "window") == []


def test_markers_isolated_by_kind_and_from_instances(tmp_path: Path) -> None:
    """Markers of one kind never leak into another kind or into list_instances."""
    pid_path = tmp_path / "app.pid"
    register_instance(pid_path, port=8123, name="live")
    write_marker(pid_path, "window", "w1")
    write_marker(pid_path, "focus-request", "req1")

    assert [m["marker_id"] for m in list_markers(pid_path, "window")] == ["w1"]
    assert [m["marker_id"] for m in list_markers(pid_path, "focus-request")] == ["req1"]
    # Instance enumeration ignores marker files entirely.
    assert [e.name for e in list_instances(pid_path)] == ["live"]


def test_stale_marker_pruned_on_read(tmp_path: Path) -> None:
    """A marker for a dead PID is deleted and excluded on list."""
    pid_path = tmp_path / "app.pid"
    dead_pid = 2**31 - 1
    stale = write_marker(pid_path, "window", "ghost", fields={"pid": dead_pid})
    # write_marker uses os.getpid(); rewrite payload with the dead pid + path.
    stale_dead = _registry_dir(pid_path) / f"window--{dead_pid}--ghost.marker"
    stale.rename(stale_dead)

    write_marker(pid_path, "window", "alive")
    markers = list_markers(pid_path, "window")
    assert [m["pid"] for m in markers] == [os.getpid()]
    assert not stale_dead.exists()


def test_list_markers_no_registry(tmp_path: Path) -> None:
    assert list_markers(tmp_path / "nothing.pid", "window") == []


def test_remove_marker_other_pid(tmp_path: Path) -> None:
    """A consumer can remove a marker owned by another (dead-or-alive) PID."""
    pid_path = tmp_path / "app.pid"
    other_pid = 2**31 - 1
    write_marker(pid_path, "focus-request", "req")
    reg_dir = _registry_dir(pid_path)
    # Simulate a marker written by a different process.
    (reg_dir / f"focus-request--{other_pid}--req2.marker").write_bytes(
        msgspec.json.encode(
            {"pid": other_pid, "marker_id": "req2", "kind": "focus-request"}
        )
    )
    remove_marker(pid_path, "focus-request", "req2", pid=other_pid)
    assert not (reg_dir / f"focus-request--{other_pid}--req2.marker").exists()


def test_list_markers_no_prune_keeps_dead(tmp_path: Path) -> None:
    """prune_dead=False retains markers for dead PIDs (caller consumes them)."""
    pid_path = tmp_path / "app.pid"
    reg_dir = _registry_dir(pid_path)
    reg_dir.mkdir(parents=True, exist_ok=True)
    dead_pid = 2**31 - 1
    (reg_dir / f"focus-request--{dead_pid}--r.marker").write_bytes(
        msgspec.json.encode({"pid": dead_pid, "marker_id": "r", "kind": "focus-request"})
    )
    kept = list_markers(pid_path, "focus-request", prune_dead=False)
    assert [m["pid"] for m in kept] == [dead_pid]
