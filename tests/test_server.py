from __future__ import annotations

import http.server
import socket
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from granian.constants import Loops

from fastware import server as server_module
from fastware.server import (
    _STOP_GRACE_SECONDS,
    AlreadyRunningError,
    PortInUseError,
    _find_free_port,
    _make_server,
    _port_file_path,
    check_already_running,
    ensure_port_available,
    read_port_file,
    serve,
    status,
    stop,
)


def test_check_already_running_no_pid(tmp_path: Path) -> None:
    """No PID file -- returns None."""
    pid_path = tmp_path / "test.pid"
    assert check_already_running(pid_path) is None


def test_check_already_running_stale_pid(tmp_path: Path) -> None:
    """PID file exists but the process is dead -- returns None and cleans up."""
    pid_path = tmp_path / "test.pid"
    pid_path.write_text("999999999")  # almost certainly not running
    assert check_already_running(pid_path) is None
    assert not pid_path.exists(), "Stale PID file should be cleaned up"


def test_check_already_running_corrupt_pid(tmp_path: Path) -> None:
    """PID file has non-numeric content -- returns None and cleans up."""
    pid_path = tmp_path / "test.pid"
    pid_path.write_text("not-a-number")
    assert check_already_running(pid_path) is None
    assert not pid_path.exists()


def test_check_already_running_live_process(tmp_path: Path) -> None:
    """PID file points to a running process -- returns the PID."""
    import os
    pid_path = tmp_path / "test.pid"
    pid_path.write_text(str(os.getpid()))  # current process is alive
    result = check_already_running(pid_path)
    assert result == os.getpid()
    assert pid_path.exists(), "PID file should NOT be removed for a live process"


def test_ensure_port_available_free() -> None:
    """A free port should be returned as-is."""
    # Bind to port 0 to find a free port, then release it
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]
    result = ensure_port_available("127.0.0.1", free_port)
    assert result == free_port


def test_ensure_port_available_occupied_unknown_process() -> None:
    """An occupied port with no health endpoint raises PortInUseError."""
    # Hold the port open for the duration of the test
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        occupied_port = s.getsockname()[1]
        with pytest.raises(PortInUseError):
            ensure_port_available("127.0.0.1", occupied_port)


class _HealthOkHandler(http.server.BaseHTTPRequestHandler):
    """Handler that answers /health with {"status":"ok"} -- simulates a server."""

    def do_GET(self) -> None:
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args: object) -> None:
        pass  # suppress stderr output during tests


def _start_health_ok_server() -> http.server.HTTPServer:
    httpd = http.server.HTTPServer(("127.0.0.1", 0), _HealthOkHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def test_ensure_port_available_kills_owned_stale_server(tmp_path: Path) -> None:
    """When the PID file matches a PID holding the port, kill it and reclaim."""
    import os
    import signal as _signal

    httpd = _start_health_ok_server()
    port = httpd.server_address[1]
    holder_pid = os.getpid()  # our own process holds the port (via the thread)

    pid_path = tmp_path / "app.pid"
    pid_path.write_text(str(holder_pid))

    def _fake_kill(pid: int, sig: int) -> None:
        assert pid == holder_pid
        assert sig == _signal.SIGTERM
        httpd.shutdown()
        httpd.server_close()

    with (
        patch("fastware.server._find_port_holder_pids", return_value=[holder_pid]),
        patch("fastware.server.os.kill", side_effect=_fake_kill),
    ):
        result = ensure_port_available("127.0.0.1", port, name="test", pid_path=pid_path)

    assert result == port


def test_ensure_port_available_never_kills_unowned_process(tmp_path: Path) -> None:
    """A health-'ok' response alone must NOT get a process killed.

    The PID file names a different PID than the port holder -- ownership is
    not established, so ensure_port_available must raise instead of killing.
    """
    import os

    httpd = _start_health_ok_server()
    port = httpd.server_address[1]
    try:
        pid_path = tmp_path / "app.pid"
        pid_path.write_text("1")  # definitely not the holder

        killed: list[tuple] = []
        with (
            patch("fastware.server._find_port_holder_pids", return_value=[os.getpid()]),
            patch("fastware.server.os.kill", side_effect=lambda *a: killed.append(a)),
        ):
            with pytest.raises(PortInUseError):
                ensure_port_available("127.0.0.1", port, name="test", pid_path=pid_path)
        assert killed == []
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_ensure_port_available_no_pid_path_never_kills() -> None:
    """Without a PID file there is no ownership proof -- never kill, raise."""
    httpd = _start_health_ok_server()
    port = httpd.server_address[1]
    try:
        killed: list[tuple] = []
        with patch("fastware.server.os.kill", side_effect=lambda *a: killed.append(a)):
            with pytest.raises(PortInUseError):
                ensure_port_available("127.0.0.1", port, name="test")
        assert killed == []
    finally:
        httpd.shutdown()
        httpd.server_close()


@patch("fastware.server.Granian")
def test_make_server_creates_granian(mock_granian_cls: MagicMock) -> None:
    """Verify Granian is instantiated with the correct parameters."""
    _make_server("myapp:app", "0.0.0.0", 9000)
    mock_granian_cls.assert_called_once_with(
        target="myapp:app",
        address="0.0.0.0",
        port=9000,
        interface="asgi",
        loop=Loops.asyncio,
        workers=1,
    )


@patch("fastware.server._make_embed_server")
def test_serve_background_returns_url(mock_make_embed: MagicMock) -> None:
    """Background serve returns the correct URL and launches an embed server."""
    mock_embed = MagicMock()
    mock_embed.serve = AsyncMock()
    mock_make_embed.return_value = mock_embed

    # Use a free port so ensure_port_available passes
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]

    async def my_app(scope, receive, send):
        pass

    url = serve(my_app, foreground=False, host="127.0.0.1", port=free_port)
    assert url == f"http://127.0.0.1:{free_port}"
    mock_make_embed.assert_called_once_with(my_app, "127.0.0.1", free_port)


def test_serve_single_instance_true_exits_on_conflict(tmp_path: Path) -> None:
    """serve() with single_instance=True raises AlreadyRunningError when an instance is running."""
    import os
    pid_path = tmp_path / "test.pid"
    pid_path.write_text(str(os.getpid()))  # current process is alive

    with pytest.raises(AlreadyRunningError):
        serve(
            "myapp:app",
            foreground=False,
            host="127.0.0.1",
            port=9999,
            pid_path=pid_path,
            single_instance=True,
        )


@patch("fastware.server._make_embed_server")
def test_serve_single_instance_false_skips_pid_check(
    mock_make_embed: MagicMock,
    tmp_path: Path,
) -> None:
    """serve() with single_instance=False does not check for existing instances."""
    import os
    mock_embed = MagicMock()
    mock_embed.serve = AsyncMock()
    mock_make_embed.return_value = mock_embed

    pid_path = tmp_path / "test.pid"
    pid_path.write_text(str(os.getpid()))  # current process is alive

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]

    async def my_app(scope, receive, send):
        pass

    # Should NOT exit despite existing PID
    url = serve(
        my_app,
        foreground=False,
        host="127.0.0.1",
        port=free_port,
        pid_path=pid_path,
        single_instance=False,
    )
    assert url == f"http://127.0.0.1:{free_port}"


@patch("fastware.server._make_embed_server")
def test_serve_pid_create_race_raises(mock_make_embed: MagicMock, tmp_path: Path) -> None:
    """Two concurrent serve() calls cannot both create the PID file.

    Simulates the race window: the PID file appears after check_already_running
    passes but before the PID file is created. The loser must raise
    AlreadyRunningError instead of overwriting the winner's PID file.
    """
    mock_embed = MagicMock()
    mock_embed.serve = AsyncMock()
    mock_make_embed.return_value = mock_embed
    pid_path = tmp_path / "race.pid"
    pid_path.write_text("54321")  # the other racer already won

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]

    async def my_app(scope, receive, send):
        pass

    with patch("fastware.server.check_already_running", return_value=None):
        with pytest.raises(AlreadyRunningError):
            serve(
                my_app,
                foreground=False,
                host="127.0.0.1",
                port=free_port,
                pid_path=pid_path,
                single_instance=True,
            )

    # The winner's PID file is untouched.
    assert pid_path.read_text() == "54321"


def test_write_pid_leaves_signal_handlers_alone(tmp_path: Path) -> None:
    """_write_pid must not install signal handlers.

    Granian overwrites SIGTERM/SIGINT handlers during startup anyway, and
    process-group termination is stop()'s job -- handler-based forwarding
    would be dead code.
    """
    import signal as _signal

    from fastware.server import _write_pid

    before_term = _signal.getsignal(_signal.SIGTERM)
    before_int = _signal.getsignal(_signal.SIGINT)
    try:
        _write_pid(tmp_path / "handlers.pid")
        assert _signal.getsignal(_signal.SIGTERM) is before_term
        assert _signal.getsignal(_signal.SIGINT) is before_int
    finally:
        _signal.signal(_signal.SIGTERM, before_term)
        _signal.signal(_signal.SIGINT, before_int)


# --- Port file tests ---


def test_find_free_port_returns_nonzero() -> None:
    """_find_free_port returns a valid port number."""
    port = _find_free_port("127.0.0.1")
    assert 1 <= port <= 65535


def test_port_file_path_derives_from_pid_path(tmp_path: Path) -> None:
    """Port file path replaces .pid suffix with .port."""
    pid_path = tmp_path / "app.pid"
    assert _port_file_path(pid_path) == tmp_path / "app.port"


def test_read_port_file_returns_port(tmp_path: Path) -> None:
    """read_port_file reads the port from a companion .port file."""
    pid_path = tmp_path / "app.pid"
    port_path = pid_path.with_suffix(".port")
    port_path.write_text("9876")
    assert read_port_file(pid_path) == 9876


def test_read_port_file_returns_none_when_missing(tmp_path: Path) -> None:
    """read_port_file returns None when no port file exists."""
    pid_path = tmp_path / "app.pid"
    assert read_port_file(pid_path) is None


def test_read_port_file_returns_none_on_corrupt(tmp_path: Path) -> None:
    """read_port_file returns None when port file is not a valid integer."""
    pid_path = tmp_path / "app.pid"
    port_path = pid_path.with_suffix(".port")
    port_path.write_text("not-a-number")
    assert read_port_file(pid_path) is None


@patch("fastware.server._make_embed_server")
def test_serve_port_zero_picks_random_port(mock_make_embed: MagicMock) -> None:
    """serve() with port=0 assigns a random free port."""
    mock_embed = MagicMock()
    mock_embed.serve = AsyncMock()
    mock_make_embed.return_value = mock_embed

    async def my_app(scope, receive, send):
        pass

    url = serve(my_app, foreground=False, host="127.0.0.1", port=0)
    assert url is not None
    assert url.startswith("http://127.0.0.1:")
    # Port should not be 0 in the URL
    actual_port = int(url.rsplit(":", 1)[1])
    assert actual_port > 0


@patch("fastware.server._make_embed_server")
def test_serve_writes_port_file(mock_make_embed: MagicMock, tmp_path: Path) -> None:
    """serve() writes a port file alongside the PID file."""
    mock_embed = MagicMock()
    mock_embed.serve = AsyncMock()
    mock_make_embed.return_value = mock_embed

    pid_path = tmp_path / "test.pid"

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]

    async def my_app(scope, receive, send):
        pass

    serve(
        my_app,
        foreground=False,
        host="127.0.0.1",
        port=free_port,
        pid_path=pid_path,
    )

    port_path = pid_path.with_suffix(".port")
    assert port_path.exists()
    assert int(port_path.read_text().strip()) == free_port


# ---------------------------------------------------------------------------
# Liveness probes must not mistake a corpse for a running server
# ---------------------------------------------------------------------------
# `os.kill(pid, 0)` -- the probe behind every entry point here -- succeeds for a
# zombie, and a `serve_background` child is a child of whoever called it, so it
# stays a zombie until this process waits on it. Each probe reaps first; these
# tests pin that per probe with a real unreaped child (the `zombie_pid`
# fixture), with no dependence on granian or on signal-delivery timing.


def test_status_reports_a_dead_child_as_not_running(
    tmp_path: Path, zombie_pid: int
) -> None:
    """status() must reap its own exited child rather than believe kill-0."""
    import os

    os.kill(zombie_pid, 0)  # precondition: the corpse still answers the raw probe

    pid_path = tmp_path / "app.pid"
    pid_path.write_text(str(zombie_pid))

    result = status(pid_path)
    assert result.running is False
    assert result.pid == zombie_pid


def test_check_already_running_ignores_a_dead_child(
    tmp_path: Path, zombie_pid: int
) -> None:
    """check_already_running() must not block a restart on an exited child."""
    pid_path = tmp_path / "app.pid"
    pid_path.write_text(str(zombie_pid))

    assert check_already_running(pid_path) is None
    assert not pid_path.exists(), "the dead instance's PID file should be cleaned up"


def test_stop_returns_promptly_for_an_already_dead_child(
    tmp_path: Path, zombie_pid: int
) -> None:
    """stop() must reap its own exited child instead of waiting it out.

    Without the reap, ``os.kill(pid, 0)`` keeps succeeding for the zombie, so
    stop() signals a corpse, polls it for the entire grace window and then
    escalates to SIGKILL -- every single time, never gracefully. The zombie is
    built directly here, so this pins the reap and nothing else: no live server,
    no SIGTERM delivery, no dependence on machine load.
    """
    import os

    pid_path = tmp_path / "app.pid"
    pid_path.write_text(str(zombie_pid))

    started = time.monotonic()
    stop(pid_path)
    elapsed = time.monotonic() - started

    assert elapsed < _STOP_GRACE_SECONDS / 2, (
        f"stop() took {elapsed:.1f}s -- it burnt the grace window waiting on a "
        f"zombie child instead of reaping it"
    )
    assert not pid_path.exists()
    with pytest.raises(ProcessLookupError):
        os.kill(zombie_pid, 0)


def test_stop_confirms_death_before_returning_after_sigkill(
    tmp_path: Path, sigterm_deaf_pid: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """stop() must not return until the SIGKILL it sent has actually landed.

    SIGKILL is delivered asynchronously, and once it lands this process's own
    child lingers as a zombie until waited on -- so a stop() that returns right
    after ``os.kill`` leaves a PID that still answers every liveness probe.
    Callers that probe immediately (status, list_instances,
    check_already_running, or a bare kill-0) are then told a corpse is running.
    """
    import os

    monkeypatch.setattr(server_module, "_STOP_GRACE_SECONDS", 0.5)

    pid_path = tmp_path / "app.pid"
    pid_path.write_text(str(sigterm_deaf_pid))

    stop(pid_path)

    with pytest.raises(ProcessLookupError):
        os.kill(sigterm_deaf_pid, 0)
    assert not pid_path.exists()


def test_stop_still_escalates_to_sigkill_after_the_grace_window(
    tmp_path: Path, sigterm_deaf_pid: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The graceful-then-forceful sequence itself is unchanged.

    A SIGTERM-deaf child survives the whole grace window and only dies to the
    escalation, so stop() must spend at least that window before killing it.
    """
    import os

    monkeypatch.setattr(server_module, "_STOP_GRACE_SECONDS", 0.5)

    pid_path = tmp_path / "app.pid"
    pid_path.write_text(str(sigterm_deaf_pid))

    started = time.monotonic()
    stop(pid_path)
    elapsed = time.monotonic() - started

    assert elapsed >= 0.5, (
        f"stop() returned after {elapsed:.2f}s -- it skipped the grace window "
        f"instead of giving the child a chance to exit on SIGTERM"
    )
    with pytest.raises(ProcessLookupError):
        os.kill(sigterm_deaf_pid, 0)
