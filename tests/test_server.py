from __future__ import annotations

import http.server
import socket
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fastware.server import (
    AlreadyRunningError,
    PortInUseError,
    _find_free_port,
    _make_server,
    _port_file_path,
    check_already_running,
    ensure_port_available,
    read_port_file,
    serve,
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
    )


@patch("fastware.server.Granian")
def test_serve_background_returns_url(mock_granian_cls: MagicMock) -> None:
    """Background serve returns the correct URL and launches a daemon thread."""
    mock_instance = MagicMock()
    mock_granian_cls.return_value = mock_instance

    # Use a free port so ensure_port_available passes
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]

    url = serve("myapp:app", foreground=False, host="127.0.0.1", port=free_port)
    assert url == f"http://127.0.0.1:{free_port}"
    mock_instance.serve.assert_called_once()


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


@patch("fastware.server.Granian")
def test_serve_single_instance_false_skips_pid_check(
    mock_granian_cls: MagicMock,
    tmp_path: Path,
) -> None:
    """serve() with single_instance=False does not check for existing instances."""
    import os
    mock_instance = MagicMock()
    mock_granian_cls.return_value = mock_instance

    pid_path = tmp_path / "test.pid"
    pid_path.write_text(str(os.getpid()))  # current process is alive

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]

    # Should NOT exit despite existing PID
    url = serve(
        "myapp:app",
        foreground=False,
        host="127.0.0.1",
        port=free_port,
        pid_path=pid_path,
        single_instance=False,
    )
    assert url == f"http://127.0.0.1:{free_port}"
    mock_instance.serve.assert_called_once()


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


@patch("fastware.server.Granian")
def test_serve_port_zero_picks_random_port(mock_granian_cls: MagicMock) -> None:
    """serve() with port=0 assigns a random free port."""
    mock_instance = MagicMock()
    mock_granian_cls.return_value = mock_instance

    url = serve("myapp:app", foreground=False, host="127.0.0.1", port=0)
    assert url is not None
    assert url.startswith("http://127.0.0.1:")
    # Port should not be 0 in the URL
    actual_port = int(url.rsplit(":", 1)[1])
    assert actual_port > 0


@patch("fastware.server.Granian")
def test_serve_writes_port_file(mock_granian_cls: MagicMock, tmp_path: Path) -> None:
    """serve() writes a port file alongside the PID file."""
    mock_instance = MagicMock()
    mock_granian_cls.return_value = mock_instance

    pid_path = tmp_path / "test.pid"

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]

    serve(
        "myapp:app",
        foreground=False,
        host="127.0.0.1",
        port=free_port,
        pid_path=pid_path,
    )

    port_path = pid_path.with_suffix(".port")
    assert port_path.exists()
    assert int(port_path.read_text().strip()) == free_port
