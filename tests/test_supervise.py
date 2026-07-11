"""Tests for dev-CLI process supervision and orchestration (Phase 13.1).

Process tests are hermetic: no real Vite/npm/granian -- components are stubbed
with short-lived ``python -c`` sleepers/exiters.
"""

from __future__ import annotations

import socket
import sys
import time
from pathlib import Path

import pytest

from fastware.devconfig import DevConfig, build_dev_config
from fastware.supervise import (
    ComponentDied,
    DevProcess,
    PortCollisionError,
    PreSpawnGateError,
    ReadinessError,
    SWModeConflictError,
    _start_ordered,
    _wait_vite_ready,
    check_sw_mode,
    dev_pid_path,
    list_dev_instances,
    resolve_backend_sw_mode,
    run_dev,
    run_pre_spawn_gates,
    stop_dev_instances,
    supervise_loop,
    teardown,
)

SLEEPER = [sys.executable, "-c", "import time; time.sleep(30)"]
EXITER = [sys.executable, "-c", "import sys; sys.exit(3)"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _cfg(tmp_path: Path, **overrides) -> DevConfig:
    table = {
        "topology": "backend-first",
        "backend": {"cmd": list(SLEEPER)},
        "vite": {"cmd": ["stub"], "port": _free_port()},
    }
    table.update(overrides)
    return build_dev_config(table, source_path=tmp_path / "pyproject.toml", app_name="probe")


# ---------------------------------------------------------------------------
# DevProcess + teardown
# ---------------------------------------------------------------------------


def test_devprocess_start_stop():
    p = DevProcess("s", SLEEPER)
    p.start()
    assert p.is_running()
    p.stop(grace_s=5)
    assert not p.is_running()


def test_teardown_reverse_order():
    procs = [DevProcess(f"c{i}", SLEEPER) for i in range(3)]
    for p in procs:
        p.start()
    order: list[str] = []
    for p in procs:
        real = p.stop

        def wrapped(grace_s=None, _real=real, _name=p.name):
            order.append(_name)
            return _real(grace_s)

        p.stop = wrapped  # type: ignore[method-assign]
    teardown(procs, grace_s=5)
    assert order == ["c2", "c1", "c0"]
    assert all(not p.is_running() for p in procs)


# ---------------------------------------------------------------------------
# supervise loop: death detection
# ---------------------------------------------------------------------------


def test_supervise_loop_detects_death():
    short = DevProcess("dying", [sys.executable, "-c", "import time; time.sleep(0.4)"])
    long = DevProcess("alive", SLEEPER)
    short.start()
    long.start()
    started = [short, long]
    try:
        dead = supervise_loop(started, {"flag": False}, poll_s=0.05)
        assert dead is short
    finally:
        teardown(started, grace_s=5)


def test_supervise_loop_stop_flag_returns_none():
    p = DevProcess("alive", SLEEPER)
    p.start()
    try:
        assert supervise_loop([p], {"flag": True}) is None
    finally:
        p.stop(5)


# ---------------------------------------------------------------------------
# Pre-spawn gates
# ---------------------------------------------------------------------------


def test_pre_spawn_file_gate_failure(tmp_path):
    cfg = _cfg(
        tmp_path,
        pre_spawn=[{"message": "frontend/ must exist", "file": "frontend"}],
    )
    with pytest.raises(PreSpawnGateError, match="frontend/ must exist"):
        run_pre_spawn_gates(cfg)


def test_pre_spawn_file_gate_pass(tmp_path):
    (tmp_path / "frontend").mkdir()
    cfg = _cfg(
        tmp_path,
        pre_spawn=[{"message": "frontend/ must exist", "file": "frontend"}],
    )
    run_pre_spawn_gates(cfg)  # no raise


def test_pre_spawn_cmd_gate_failure(tmp_path):
    cfg = _cfg(
        tmp_path,
        pre_spawn=[{"message": "must exit zero", "cmd": [sys.executable, "-c", "import sys; sys.exit(1)"]}],
    )
    with pytest.raises(PreSpawnGateError, match="must exit zero"):
        run_pre_spawn_gates(cfg)


# ---------------------------------------------------------------------------
# Readiness + port collision
# ---------------------------------------------------------------------------


def test_vite_readiness_timeout_is_loud(tmp_path):
    cfg = _cfg(tmp_path, vite={"cmd": ["stub"], "port": _free_port()})
    p = DevProcess("vite", SLEEPER)  # never binds the port
    p.start()
    try:
        with pytest.raises(ReadinessError, match="did not answer"):
            _wait_vite_ready(cfg, p, timeout=1.2)
    finally:
        p.stop(5)


def test_vite_readiness_early_exit_is_loud(tmp_path):
    cfg = _cfg(tmp_path, vite={"cmd": ["stub"], "port": _free_port()})
    p = DevProcess("vite", EXITER)
    p.start()
    with pytest.raises(ReadinessError, match="exited"):
        _wait_vite_ready(cfg, p, timeout=5)


def test_port_collision_is_loud(tmp_path):
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        s.listen()
        busy = s.getsockname()[1]
        cfg = _cfg(tmp_path, vite={"cmd": ["stub"], "port": busy})
        started: list[DevProcess] = []
        try:
            with pytest.raises(PortCollisionError, match=f"port {busy}"):
                _start_ordered(cfg, 5.0, started)
        finally:
            teardown(started, 5)


# ---------------------------------------------------------------------------
# Aux service health gate (backend-first happy path with stubbed components)
# ---------------------------------------------------------------------------


def test_aux_health_failure_tears_down(tmp_path):
    # An aux service whose health check never passes must fail loudly and tear
    # down the aux process that was started.
    cfg = _cfg(
        tmp_path,
        aux=[
            {
                "name": "neverhealthy",
                "cmd": list(SLEEPER),
                "timeout_s": 1,
                "health": {"cmd": [sys.executable, "-c", "import sys; sys.exit(1)"]},
            }
        ],
    )
    with pytest.raises(ReadinessError, match="neverhealthy"):
        run_dev(cfg, daemon=False, grace_s=5, pid_dir=tmp_path)
    # registry must be clean (nothing left registered)
    assert list_dev_instances(cfg, pid_dir=tmp_path) == []


# ---------------------------------------------------------------------------
# sw_mode = cache hard error
# ---------------------------------------------------------------------------


def test_sw_mode_cache_hard_error(tmp_path):
    cfg = build_dev_config(
        {
            "topology": "backend-first",
            "backend": {"app": "tests.dev_fixture_apps:cache_app", "port": _free_port()},
            "vite": {"cmd": ["stub"], "port": _free_port()},
        },
        source_path=tmp_path / "pyproject.toml",
        app_name="probe",
    )
    with pytest.raises(SWModeConflictError, match="sw_mode='cache'"):
        check_sw_mode(cfg)
    # run_dev must also hard-error up front (before spawning anything).
    with pytest.raises(SWModeConflictError):
        run_dev(cfg, daemon=False, pid_dir=tmp_path)


def test_sw_mode_off_and_cmd_backend_pass_check(tmp_path):
    off_cfg = build_dev_config(
        {
            "topology": "backend-first",
            "backend": {"app": "tests.dev_fixture_apps:off_app", "port": _free_port()},
            "vite": {"cmd": ["stub"], "port": _free_port()},
        },
        source_path=tmp_path / "pyproject.toml",
    )
    assert resolve_backend_sw_mode(off_cfg) == "off"
    check_sw_mode(off_cfg)  # no raise
    cmd_cfg = _cfg(tmp_path)
    assert resolve_backend_sw_mode(cmd_cfg) is None  # cmd-form is not introspected
    check_sw_mode(cmd_cfg)  # no raise


# ---------------------------------------------------------------------------
# Daemon register / status / stop round-trip (fake short-lived process)
# ---------------------------------------------------------------------------


def test_dev_status_stop_roundtrip(tmp_path):
    import subprocess

    cfg = _cfg(tmp_path)
    pid_path = dev_pid_path(cfg, pid_dir=tmp_path)
    name = f"{cfg.app_name}-dev"
    # A fake daemon that registers itself (like _run_foreground does) then idles.
    code = (
        "from fastware.server import register_instance; from pathlib import Path; "
        "import time; "
        f"register_instance(Path({str(pid_path)!r}), 9999, {name!r}); time.sleep(30)"
    )
    proc = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
    try:
        deadline = time.monotonic() + 10
        entries = []
        while time.monotonic() < deadline:
            entries = list_dev_instances(cfg, pid_dir=tmp_path)
            if entries:
                break
            time.sleep(0.1)
        assert len(entries) == 1
        assert entries[0].pid == proc.pid
        assert entries[0].port == 9999
        assert entries[0].name == name

        stopped = stop_dev_instances(cfg, pid_dir=tmp_path, grace_s=5)
        assert len(stopped) == 1 and stopped[0].pid == proc.pid
        proc.wait(timeout=10)
        assert proc.poll() is not None
        # registry pruned once the process is gone
        assert list_dev_instances(cfg, pid_dir=tmp_path) == []
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
