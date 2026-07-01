"""Tests for Phase 8: Feature flags, audit logging, background tasks."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest


# -----------------------------------------------------------------------
# 8.1 Feature flags
# -----------------------------------------------------------------------


class TestFeatureFlags:
    def test_defaults_only(self):
        from fastware.features import FeatureFlags

        ff = FeatureFlags({"terminal": False, "monitoring": True})
        assert ff.enabled("terminal") is False
        assert ff.enabled("monitoring") is True

    def test_unknown_flag_defaults_false(self):
        from fastware.features import FeatureFlags

        ff = FeatureFlags({"a": True})
        assert ff.enabled("nonexistent") is False

    def test_all_flags(self):
        from fastware.features import FeatureFlags

        ff = FeatureFlags({"a": True, "b": False})
        assert ff.all_flags() == {"a": True, "b": False}

    def test_override_from_file(self, tmp_path: Path):
        overrides_file = tmp_path / "overrides.json"
        overrides_file.write_text(json.dumps({"terminal": True}))

        from fastware.features import FeatureFlags

        ff = FeatureFlags(
            {"terminal": False, "monitoring": True},
            overrides_path=overrides_file,
        )
        assert ff.enabled("terminal") is True
        assert ff.enabled("monitoring") is True

    def test_set_override_persists(self, tmp_path: Path):
        overrides_file = tmp_path / "overrides.json"

        from fastware.features import FeatureFlags

        ff = FeatureFlags({"x": False}, overrides_path=overrides_file)
        ff.set_override("x", True)

        assert ff.enabled("x") is True
        data = json.loads(overrides_file.read_text())
        assert data["x"] is True

    def test_set_override_without_path_raises(self):
        from fastware.features import FeatureFlags

        ff = FeatureFlags({"x": False})
        with pytest.raises(RuntimeError, match="no overrides_path"):
            ff.set_override("x", True)

    def test_reload(self, tmp_path: Path):
        overrides_file = tmp_path / "overrides.json"
        overrides_file.write_text(json.dumps({"y": True}))

        from fastware.features import FeatureFlags

        ff = FeatureFlags({"y": False}, overrides_path=overrides_file)
        assert ff.enabled("y") is True

        # Delete the file and reload
        overrides_file.unlink()
        ff.reload()
        assert ff.enabled("y") is False

    def test_malformed_file_uses_defaults(self, tmp_path: Path):
        overrides_file = tmp_path / "overrides.json"
        overrides_file.write_text("not valid json!!!")

        from fastware.features import FeatureFlags

        ff = FeatureFlags({"z": True}, overrides_path=overrides_file)
        assert ff.enabled("z") is True

    def test_all_flags_with_override(self, tmp_path: Path):
        overrides_file = tmp_path / "overrides.json"
        overrides_file.write_text(json.dumps({"a": False}))

        from fastware.features import FeatureFlags

        ff = FeatureFlags({"a": True, "b": True}, overrides_path=overrides_file)
        assert ff.all_flags() == {"a": False, "b": True}


# -----------------------------------------------------------------------
# 8.2 Audit logging
# -----------------------------------------------------------------------


class TestAuditLog:
    def test_log_creates_file(self, tmp_path: Path):
        from fastware.audit import AuditLog

        log_file = tmp_path / "audit.jsonl"
        audit = AuditLog(log_file)
        audit.log("test_event", {"key": "value"})

        assert log_file.exists()

    def test_log_appends_entries(self, tmp_path: Path):
        from fastware.audit import AuditLog

        log_file = tmp_path / "audit.jsonl"
        audit = AuditLog(log_file)
        audit.log("event_a", {"n": 1})
        audit.log("event_b", {"n": 2})
        audit.log("event_c")

        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 3

        entries = [json.loads(line) for line in lines]
        assert entries[0]["event_type"] == "event_a"
        assert entries[0]["payload"] == {"n": 1}
        assert entries[1]["event_type"] == "event_b"
        assert entries[2]["event_type"] == "event_c"
        assert "payload" not in entries[2]

    def test_entries_have_iso_timestamp(self, tmp_path: Path):
        from fastware.audit import AuditLog

        log_file = tmp_path / "audit.jsonl"
        audit = AuditLog(log_file)
        audit.log("ts_test")

        entry = json.loads(log_file.read_text().strip())
        # ISO 8601 format check
        assert "T" in entry["timestamp"]
        assert entry["timestamp"].endswith("+00:00")

    def test_thread_safety(self, tmp_path: Path):
        from fastware.audit import AuditLog

        log_file = tmp_path / "audit.jsonl"
        audit = AuditLog(log_file)
        n_threads = 10
        n_events = 50

        def writer(tid: int) -> None:
            for i in range(n_events):
                audit.log("thread_event", {"tid": tid, "i": i})

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == n_threads * n_events

    def test_nested_directory_created(self, tmp_path: Path):
        from fastware.audit import AuditLog

        log_file = tmp_path / "deep" / "nested" / "audit.jsonl"
        audit = AuditLog(log_file)
        audit.log("deep_event")

        assert log_file.exists()


# -----------------------------------------------------------------------
# 8.3 Background task registry
# -----------------------------------------------------------------------


class _DummyTask:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


class TestTaskRegistry:
    def test_register_and_start_all(self):
        from fastware.tasks import TaskRegistry

        registry = TaskRegistry()
        instances: list[_DummyTask] = []

        def factory() -> _DummyTask:
            t = _DummyTask()
            instances.append(t)
            return t

        registry.register("task_a", factory)
        registry.register("task_b", factory)
        registry.start_all()

        assert len(instances) == 2
        assert all(t.started for t in instances)

    def test_stop_all(self):
        from fastware.tasks import TaskRegistry

        registry = TaskRegistry()
        instances: list[_DummyTask] = []

        def factory() -> _DummyTask:
            t = _DummyTask()
            instances.append(t)
            return t

        registry.register("task_a", factory)
        registry.start_all()
        registry.stop_all()

        assert instances[0].stopped is True

    def test_duplicate_name_raises(self):
        from fastware.tasks import TaskRegistry

        registry = TaskRegistry()
        registry.register("dup", _DummyTask)
        with pytest.raises(ValueError, match="already registered"):
            registry.register("dup", _DummyTask)

    def test_feature_gated_task_skipped(self):
        from fastware.features import FeatureFlags
        from fastware.tasks import TaskRegistry

        registry = TaskRegistry()
        instances: list[_DummyTask] = []

        def factory() -> _DummyTask:
            t = _DummyTask()
            instances.append(t)
            return t

        registry.register("gated", factory, feature="terminal")
        ff = FeatureFlags({"terminal": False})
        registry.start_all(features=ff)

        assert len(instances) == 0

    def test_feature_gated_task_started_when_enabled(self):
        from fastware.features import FeatureFlags
        from fastware.tasks import TaskRegistry

        registry = TaskRegistry()
        instances: list[_DummyTask] = []

        def factory() -> _DummyTask:
            t = _DummyTask()
            instances.append(t)
            return t

        registry.register("gated", factory, feature="terminal")
        ff = FeatureFlags({"terminal": True})
        registry.start_all(features=ff)

        assert len(instances) == 1
        assert instances[0].started is True

    def test_list_tasks(self):
        from fastware.tasks import TaskRegistry

        registry = TaskRegistry()
        registry.register("a", _DummyTask, feature="x")
        registry.register("b", _DummyTask)

        tasks = registry.list_tasks()
        assert len(tasks) == 2
        assert tasks[0]["name"] == "a"
        assert tasks[0]["feature"] == "x"
        assert tasks[0]["running"] is False

    def test_get_task(self):
        from fastware.tasks import TaskRegistry

        registry = TaskRegistry()
        registry.register("my_task", _DummyTask)
        registry.start_all()

        task = registry.get_task("my_task")
        assert task is not None
        assert task.started is True
        assert registry.get_task("nonexistent") is None

    def test_feature_gated_skipped_when_no_features_provided(self):
        from fastware.tasks import TaskRegistry

        registry = TaskRegistry()
        instances: list[_DummyTask] = []

        def factory() -> _DummyTask:
            t = _DummyTask()
            instances.append(t)
            return t

        registry.register("gated", factory, feature="some_feature")
        # No FeatureFlags passed at all
        registry.start_all()

        assert len(instances) == 0

    def test_conforms_to_protocol(self):
        from fastware.tasks import BackgroundTask

        assert isinstance(_DummyTask(), BackgroundTask)
