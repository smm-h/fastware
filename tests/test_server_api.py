"""Tests for Phase 2: Server lifecycle improvements (2.1--2.5)."""

from __future__ import annotations

import os
import signal
import socket
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from granian.constants import Loops

from fastware.server import (
    AlreadyRunningError,
    ServerStatus,
    _materialize_target,
    _resolve_host_port,
    _resolve_target,
    serve,
    status,
    stop,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Find an ephemeral port that is currently free."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# 2.1 serve(foreground) with no default
# ---------------------------------------------------------------------------


class TestServeAPI:
    """Tests for the merged serve() function."""

    def test_foreground_is_required(self) -> None:
        """serve() raises TypeError when foreground is not provided."""
        with pytest.raises(TypeError, match="foreground"):
            serve("myapp:app", host="127.0.0.1", port=8000)  # type: ignore[call-arg]

    @patch("fastware.server._make_embed_server")
    def test_serve_foreground_false_returns_url(self, mock_make_embed: MagicMock) -> None:
        """foreground=False uses embed server and returns URL."""
        mock_embed = MagicMock()
        mock_embed.serve = AsyncMock()
        mock_make_embed.return_value = mock_embed
        port = _free_port()

        async def my_app(scope, receive, send):
            pass

        url = serve(my_app, foreground=False, host="127.0.0.1", port=port)

        assert url == f"http://127.0.0.1:{port}"
        mock_make_embed.assert_called_once_with(my_app, "127.0.0.1", port)

    @patch("fastware.server.Granian")
    def test_serve_foreground_true_returns_none(self, mock_granian_cls: MagicMock) -> None:
        """foreground=True blocks and returns None."""
        mock_instance = MagicMock()
        mock_granian_cls.return_value = mock_instance
        port = _free_port()

        result = serve("myapp:app", foreground=True, host="127.0.0.1", port=port)

        assert result is None
        mock_instance.serve.assert_called_once()

    @patch("fastware.server._make_embed_server")
    def test_serve_with_callable_target(self, mock_make_embed: MagicMock) -> None:
        """serve() accepts a callable and passes it directly to the embed server."""
        mock_embed = MagicMock()
        mock_embed.serve = AsyncMock()
        mock_make_embed.return_value = mock_embed
        port = _free_port()

        async def my_app(scope, receive, send):
            pass

        url = serve(my_app, foreground=False, host="127.0.0.1", port=port)

        assert url is not None
        # The embed server should receive the callable directly
        mock_make_embed.assert_called_once_with(my_app, "127.0.0.1", port)

    @patch("fastware.server.Granian")
    def test_serve_with_string_target_foreground(self, mock_granian_cls: MagicMock) -> None:
        """serve(foreground=True) passes string targets through to Granian unchanged."""
        mock_instance = MagicMock()
        mock_granian_cls.return_value = mock_instance
        port = _free_port()

        serve("myapp:app", foreground=True, host="127.0.0.1", port=port)

        mock_granian_cls.assert_called_once_with(
            target="myapp:app",
            address="127.0.0.1",
            port=port,
            interface="asgi",
            loop=Loops.asyncio,
            workers=1,
        )

    @patch("fastware.server._make_embed_server")
    def test_serve_with_pid_path(self, mock_make_embed: MagicMock, tmp_path: Path) -> None:
        """serve() writes a PID file when pid_path is given."""
        mock_embed = MagicMock()
        mock_embed.serve = AsyncMock()
        mock_make_embed.return_value = mock_embed
        port = _free_port()
        pid_path = tmp_path / "test.pid"

        async def my_app(scope, receive, send):
            pass

        serve(my_app, foreground=False, host="127.0.0.1", port=port, pid_path=pid_path)

        assert pid_path.exists()
        assert int(pid_path.read_text().strip()) == os.getpid()


# ---------------------------------------------------------------------------
# 2.1 _resolve_target
# ---------------------------------------------------------------------------


class TestResolveTarget:
    """Tests for callable-to-string target resolution."""

    def test_string_passthrough(self) -> None:
        """String targets pass through unchanged."""
        assert _resolve_target("myapp:app") == "myapp:app"

    def test_callable_gets_registered(self) -> None:
        """Callable targets are registered on a synthetic module."""
        import sys

        async def my_app(scope, receive, send):
            pass

        result = _resolve_target(my_app)

        assert isinstance(result, str)
        assert ":app" in result
        mod_name = result.split(":")[0]
        assert mod_name in sys.modules
        assert sys.modules[mod_name].app is my_app

    def test_unique_module_names(self) -> None:
        """Each callable gets a unique module name."""

        async def app1(scope, receive, send):
            pass

        async def app2(scope, receive, send):
            pass

        name1 = _resolve_target(app1)
        name2 = _resolve_target(app2)
        assert name1 != name2


class TestMaterializeTarget:
    """Tests for cross-process target materialization."""

    def test_string_passthrough(self) -> None:
        """String targets pass through with no shim directory."""
        target_str, shim_dir = _materialize_target("myapp:app")
        assert target_str == "myapp:app"
        assert shim_dir is None

    def test_module_level_callable_writes_importable_shim(self) -> None:
        """A module-level callable produces a shim file that re-imports it."""
        import sys

        from tests.target_app_module import app

        target_str, shim_dir = _materialize_target(app)
        try:
            assert shim_dir is not None and shim_dir.is_dir()
            mod_name, _, attr = target_str.partition(":")
            assert attr == "app"
            shim_file = shim_dir / f"{mod_name}.py"
            assert shim_file.exists()
            # Import the shim the way a child process would.
            sys.path.insert(0, str(shim_dir))
            try:
                import importlib

                shim = importlib.import_module(mod_name)
                assert shim.app is app
            finally:
                sys.path.remove(str(shim_dir))
                sys.modules.pop(mod_name, None)
        finally:
            import shutil

            shutil.rmtree(shim_dir, ignore_errors=True)

    def test_local_callable_rejected(self) -> None:
        """Local functions are rejected with a clear error."""

        async def local_app(scope, receive, send):
            pass

        with pytest.raises(ValueError, match="importable"):
            _materialize_target(local_app)

    def test_lambda_rejected(self) -> None:
        """Lambdas are rejected with a clear error."""
        with pytest.raises(ValueError, match="importable"):
            _materialize_target(lambda scope, receive, send: None)


# ---------------------------------------------------------------------------
# 2.2 stop(pid_path)
# ---------------------------------------------------------------------------


class TestStop:
    """Tests for the stop() function."""

    def test_stop_missing_pid_file(self, tmp_path: Path) -> None:
        """Raises FileNotFoundError when PID file does not exist."""
        pid_path = tmp_path / "nonexistent.pid"
        with pytest.raises(FileNotFoundError):
            stop(pid_path)

    def test_stop_stale_pid_file(self, tmp_path: Path) -> None:
        """Returns normally and removes stale PID file when process is dead."""
        pid_path = tmp_path / "stale.pid"
        pid_path.write_text("999999999")  # almost certainly not running
        stop(pid_path)  # should not raise
        # PID file should be cleaned up
        assert not pid_path.exists()

    def test_stop_corrupt_pid_file(self, tmp_path: Path) -> None:
        """Raises FileNotFoundError when PID file has non-numeric content."""
        pid_path = tmp_path / "corrupt.pid"
        pid_path.write_text("not-a-number")
        with pytest.raises(FileNotFoundError, match="Corrupt"):
            stop(pid_path)
        assert not pid_path.exists()

    @patch("fastware.server.os.kill")
    def test_stop_sends_sigterm(self, mock_kill: MagicMock, tmp_path: Path) -> None:
        """stop() sends SIGTERM to the PID."""
        pid_path = tmp_path / "test.pid"
        pid_path.write_text("12345")

        # First os.kill(pid, 0) check -- process alive
        # Then os.kill(pid, SIGTERM)
        # Then os.kill(pid, 0) in poll loop -- process dead
        call_count = 0

        def kill_side_effect(pid, sig):
            nonlocal call_count
            call_count += 1
            if sig == 0 and call_count == 1:
                return  # alive on first check
            if sig == signal.SIGTERM:
                return  # SIGTERM sent
            if sig == 0:
                raise ProcessLookupError  # dead after SIGTERM

        mock_kill.side_effect = kill_side_effect

        stop(pid_path)

        # Verify SIGTERM was sent
        sigterm_calls = [c for c in mock_kill.call_args_list if c[0][1] == signal.SIGTERM]
        assert len(sigterm_calls) == 1
        assert sigterm_calls[0][0][0] == 12345
        assert not pid_path.exists()

    @patch("fastware.server.os.killpg")
    @patch("fastware.server.os.getpgid")
    @patch("fastware.server.os.kill")
    def test_stop_signals_process_group_of_group_leader(
        self,
        mock_kill: MagicMock,
        mock_getpgid: MagicMock,
        mock_killpg: MagicMock,
        tmp_path: Path,
    ) -> None:
        """When the PID is its own group leader, stop() signals the whole group."""
        pid_path = tmp_path / "test.pid"
        pid_path.write_text("4242")
        mock_getpgid.return_value = 4242  # pid == pgid -> group leader

        probes = {"count": 0}

        def kill_side_effect(pid, sig):
            # Only liveness probes (sig 0) may go through os.kill; signalling
            # must use os.killpg for a group leader.
            assert sig == 0
            probes["count"] += 1
            if probes["count"] == 1:
                return  # alive before SIGTERM
            raise ProcessLookupError  # dead after SIGTERM

        mock_kill.side_effect = kill_side_effect

        stop(pid_path)

        mock_killpg.assert_called_once_with(4242, signal.SIGTERM)
        assert not pid_path.exists()

    @patch("fastware.server.os.killpg")
    @patch("fastware.server.os.getpgid")
    @patch("fastware.server.os.kill")
    def test_stop_does_not_killpg_non_group_leader(
        self,
        mock_kill: MagicMock,
        mock_getpgid: MagicMock,
        mock_killpg: MagicMock,
        tmp_path: Path,
    ) -> None:
        """When the PID leads no group, only that PID is signalled -- its group
        may contain unrelated processes (e.g. the launching shell)."""
        pid_path = tmp_path / "test.pid"
        pid_path.write_text("4242")
        mock_getpgid.return_value = 1  # pid != pgid -> not a group leader

        probes = {"count": 0}

        def kill_side_effect(pid, sig):
            if sig == signal.SIGTERM:
                assert pid == 4242
                return
            assert sig == 0
            probes["count"] += 1
            if probes["count"] == 1:
                return
            raise ProcessLookupError

        mock_kill.side_effect = kill_side_effect

        stop(pid_path)

        mock_killpg.assert_not_called()
        sigterm_calls = [c for c in mock_kill.call_args_list if c[0][1] == signal.SIGTERM]
        assert len(sigterm_calls) == 1
        assert not pid_path.exists()


# ---------------------------------------------------------------------------
# Embed server background mode
# ---------------------------------------------------------------------------


class TestEmbedServerBackground:
    """serve(foreground=False) uses the embed server, not the multiprocess Granian."""

    @patch("fastware.server._make_embed_server")
    def test_no_signal_patching_needed(self, mock_make_embed: MagicMock) -> None:
        """serve(foreground=False) does not touch granian's signal handlers.

        The embed server runs async workers in the caller's event loop, so no
        signal.signal calls are made and no patching is needed."""
        import signal as _signal

        from granian.server import common as granian_common

        mock_embed = MagicMock()
        mock_embed.serve = AsyncMock()
        mock_make_embed.return_value = mock_embed

        before_term = _signal.getsignal(_signal.SIGTERM)
        before_int = _signal.getsignal(_signal.SIGINT)

        async def my_app(scope, receive, send):
            pass

        serve(my_app, foreground=False, host="127.0.0.1", port=_free_port())

        # Signal handlers should be completely untouched
        assert _signal.getsignal(_signal.SIGTERM) is before_term
        assert _signal.getsignal(_signal.SIGINT) is before_int

    @patch("fastware.server._make_embed_server")
    def test_embed_server_stored_for_stop(self, mock_make_embed: MagicMock) -> None:
        """serve(foreground=False) stores the embed server for stop_background()."""
        from fastware.server import _background_servers

        mock_embed = MagicMock()
        mock_embed.serve = AsyncMock()
        mock_make_embed.return_value = mock_embed
        port = _free_port()

        async def my_app(scope, receive, send):
            pass

        url = serve(my_app, foreground=False, host="127.0.0.1", port=port)

        assert url in _background_servers
        assert _background_servers[url] is mock_embed
        # Clean up
        _background_servers.pop(url, None)

    @patch("fastware.server._make_embed_server")
    def test_stop_background_calls_stop(self, mock_make_embed: MagicMock) -> None:
        """stop_background() calls stop() on the stored embed server."""
        from fastware.server import stop_background

        mock_embed = MagicMock()
        mock_embed.serve = AsyncMock()
        mock_make_embed.return_value = mock_embed
        port = _free_port()

        async def my_app(scope, receive, send):
            pass

        url = serve(my_app, foreground=False, host="127.0.0.1", port=port)
        stop_background(url)

        mock_embed.stop.assert_called_once()

    def test_stop_background_unknown_url_raises(self) -> None:
        """stop_background() raises KeyError for an unknown URL."""
        from fastware.server import stop_background

        with pytest.raises(KeyError, match="No background server tracked"):
            stop_background("http://127.0.0.1:99999")


# ---------------------------------------------------------------------------
# 2.3 status(pid_path, health_url)
# ---------------------------------------------------------------------------


class TestStatus:
    """Tests for the status() function."""

    def test_status_no_pid_file(self, tmp_path: Path) -> None:
        """Returns running=False when PID file does not exist."""
        result = status(tmp_path / "nonexistent.pid")
        assert result == ServerStatus(running=False, pid=None, healthy=None)

    def test_status_stale_pid(self, tmp_path: Path) -> None:
        """Returns running=False when PID file references dead process."""
        pid_path = tmp_path / "stale.pid"
        pid_path.write_text("999999999")
        result = status(pid_path)
        assert result.running is False
        assert result.pid == 999999999
        assert result.healthy is None

    def test_status_running_process(self, tmp_path: Path) -> None:
        """Returns running=True for the current process (which is alive)."""
        pid_path = tmp_path / "current.pid"
        pid_path.write_text(str(os.getpid()))
        result = status(pid_path)
        assert result.running is True
        assert result.pid == os.getpid()
        assert result.healthy is None  # no health_url given

    def test_status_corrupt_pid_file(self, tmp_path: Path) -> None:
        """Returns running=False for corrupt PID files."""
        pid_path = tmp_path / "corrupt.pid"
        pid_path.write_text("garbage")
        result = status(pid_path)
        assert result == ServerStatus(running=False, pid=None, healthy=None)

    def test_status_healthy(self, tmp_path: Path) -> None:
        """Returns healthy=True when health URL returns 200."""
        pid_path = tmp_path / "running.pid"
        pid_path.write_text(str(os.getpid()))

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = lambda self: self
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = status(pid_path, health_url="http://127.0.0.1:8000/health")

        assert result.running is True
        assert result.healthy is True

    def test_status_unhealthy(self, tmp_path: Path) -> None:
        """Returns healthy=False when health URL fails."""
        pid_path = tmp_path / "running.pid"
        pid_path.write_text(str(os.getpid()))

        with patch("urllib.request.urlopen", side_effect=Exception("Connection refused")):
            result = status(pid_path, health_url="http://127.0.0.1:8000/health")

        assert result.running is True
        assert result.healthy is False


# ---------------------------------------------------------------------------
# 2.4 Pre-serve callback
# ---------------------------------------------------------------------------


class TestPreServe:
    """Tests for the pre_serve callback parameter."""

    @patch("fastware.server._make_embed_server")
    def test_pre_serve_runs_before_server(self, mock_make_embed: MagicMock, tmp_path: Path) -> None:
        """pre_serve is called before the embed server starts."""
        mock_embed = MagicMock()
        mock_embed.serve = AsyncMock()
        mock_make_embed.return_value = mock_embed
        port = _free_port()

        flag_file = tmp_path / "pre_serve_ran"

        async def my_app(scope, receive, send):
            pass

        def my_pre_serve():
            flag_file.write_text("yes")

        serve(my_app, foreground=False, host="127.0.0.1", port=port, pre_serve=my_pre_serve)

        assert flag_file.exists()
        assert flag_file.read_text() == "yes"

    @patch("fastware.server.Granian")
    def test_pre_serve_runs_before_granian_serve(self, mock_granian_cls: MagicMock, tmp_path: Path) -> None:
        """pre_serve runs before Granian.serve() is called."""
        mock_instance = MagicMock()
        mock_granian_cls.return_value = mock_instance
        port = _free_port()

        call_order: list[str] = []

        def my_pre_serve():
            call_order.append("pre_serve")

        # Granian.serve() records itself too
        mock_instance.serve.side_effect = lambda: call_order.append("granian_serve")

        serve("myapp:app", foreground=True, host="127.0.0.1", port=port, pre_serve=my_pre_serve)

        assert call_order == ["pre_serve", "granian_serve"]

    @patch("fastware.server._make_embed_server")
    def test_no_pre_serve(self, mock_make_embed: MagicMock) -> None:
        """Server starts normally when no pre_serve is given."""
        mock_embed = MagicMock()
        mock_embed.serve = AsyncMock()
        mock_make_embed.return_value = mock_embed
        port = _free_port()

        async def my_app(scope, receive, send):
            pass

        serve(my_app, foreground=False, host="127.0.0.1", port=port)

        mock_make_embed.assert_called_once()


# ---------------------------------------------------------------------------
# 2.5 Env var settings
# ---------------------------------------------------------------------------


class TestEnvVarSettings:
    """Tests for env var host/port resolution."""

    def test_resolve_host_port_explicit(self) -> None:
        """Explicit args are returned as-is."""
        host, port = _resolve_host_port("0.0.0.0", 9000, "FASTWARE")
        assert host == "0.0.0.0"
        assert port == 9000

    def test_resolve_host_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Host falls back to env var when not provided explicitly."""
        monkeypatch.setenv("FASTWARE_HOST", "10.0.0.1")
        host, port = _resolve_host_port(None, 8080, "FASTWARE")
        assert host == "10.0.0.1"
        assert port == 8080

    def test_resolve_port_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Port falls back to env var when not provided explicitly."""
        monkeypatch.setenv("FASTWARE_PORT", "9999")
        host, port = _resolve_host_port("127.0.0.1", None, "FASTWARE")
        assert host == "127.0.0.1"
        assert port == 9999

    def test_resolve_both_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Both host and port fall back to env vars."""
        monkeypatch.setenv("MYAPP_HOST", "192.168.1.1")
        monkeypatch.setenv("MYAPP_PORT", "3000")
        host, port = _resolve_host_port(None, None, "MYAPP")
        assert host == "192.168.1.1"
        assert port == 3000

    def test_explicit_overrides_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Explicit args override env vars."""
        monkeypatch.setenv("FASTWARE_HOST", "10.0.0.1")
        monkeypatch.setenv("FASTWARE_PORT", "9999")
        host, port = _resolve_host_port("127.0.0.1", 8080, "FASTWARE")
        assert host == "127.0.0.1"
        assert port == 8080

    def test_missing_host_raises(self) -> None:
        """ValueError when host is neither provided nor in env."""
        with pytest.raises(ValueError, match="host must be provided"):
            _resolve_host_port(None, 8080, "FASTWARE")

    def test_missing_port_raises(self) -> None:
        """ValueError when port is neither provided nor in env."""
        with pytest.raises(ValueError, match="port must be provided"):
            _resolve_host_port("127.0.0.1", None, "FASTWARE")

    def test_missing_both_raises(self) -> None:
        """ValueError when neither host nor port is provided and no env vars."""
        with pytest.raises(ValueError, match="host must be provided"):
            _resolve_host_port(None, None, "FASTWARE")

    def test_invalid_port_env_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ValueError when env var port is not a valid integer."""
        monkeypatch.setenv("FASTWARE_PORT", "not-a-number")
        with pytest.raises(ValueError, match="must be an integer"):
            _resolve_host_port("127.0.0.1", None, "FASTWARE")

    def test_custom_name_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Custom name parameter changes env var prefix."""
        monkeypatch.setenv("CODEHOME_HOST", "10.0.0.2")
        monkeypatch.setenv("CODEHOME_PORT", "5000")
        host, port = _resolve_host_port(None, None, "CODEHOME")
        assert host == "10.0.0.2"
        assert port == 5000

    def test_name_uppercased(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Name parameter is uppercased for env var lookup."""
        monkeypatch.setenv("MYAPP_HOST", "10.0.0.3")
        monkeypatch.setenv("MYAPP_PORT", "4000")
        host, port = _resolve_host_port(None, None, "myapp")
        assert host == "10.0.0.3"
        assert port == 4000

    @patch("fastware.server._make_embed_server")
    def test_serve_reads_env_vars(self, mock_make_embed: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
        """serve() uses env vars when host/port not provided explicitly."""
        mock_embed = MagicMock()
        mock_embed.serve = AsyncMock()
        mock_make_embed.return_value = mock_embed

        port = _free_port()
        monkeypatch.setenv("FASTWARE_HOST", "127.0.0.1")
        monkeypatch.setenv("FASTWARE_PORT", str(port))

        async def my_app(scope, receive, send):
            pass

        url = serve(my_app, foreground=False)

        assert url == f"http://127.0.0.1:{port}"
        mock_make_embed.assert_called_once_with(my_app, "127.0.0.1", port)

    def test_serve_raises_without_host_or_port(self) -> None:
        """serve() raises ValueError when neither explicit args nor env vars exist."""
        with pytest.raises(ValueError, match="host must be provided"):
            serve("myapp:app", foreground=False)


# ---------------------------------------------------------------------------
# Integration: serve + stop + status
# ---------------------------------------------------------------------------


class TestServeStopStatusIntegration:
    """Integration tests for serve, stop, and status working together."""

    @patch("fastware.server._make_embed_server")
    def test_status_after_serve(self, mock_make_embed: MagicMock, tmp_path: Path) -> None:
        """status() reports running=True after serve(foreground=False)."""
        mock_embed = MagicMock()
        mock_embed.serve = AsyncMock()
        mock_make_embed.return_value = mock_embed
        port = _free_port()
        pid_path = tmp_path / "test.pid"

        async def my_app(scope, receive, send):
            pass

        serve(my_app, foreground=False, host="127.0.0.1", port=port, pid_path=pid_path)

        result = status(pid_path)
        assert result.running is True
        assert result.pid == os.getpid()


# ---------------------------------------------------------------------------
# Export verification
# ---------------------------------------------------------------------------


class TestExports:
    """Verify new functions are exported from the fastware package."""

    def test_serve_exported(self) -> None:
        import fastware
        assert hasattr(fastware, "serve")
        assert callable(fastware.serve)

    def test_stop_exported(self) -> None:
        import fastware
        assert hasattr(fastware, "stop")
        assert callable(fastware.stop)

    def test_status_exported(self) -> None:
        import fastware
        assert hasattr(fastware, "status")
        assert callable(fastware.status)

    def test_server_status_exported(self) -> None:
        import fastware
        assert hasattr(fastware, "ServerStatus")

    def test_stop_background_exported(self) -> None:
        import fastware
        assert hasattr(fastware, "stop_background")
        assert callable(fastware.stop_background)

    def test_all_in_dunder_all(self) -> None:
        import fastware
        for name in ("serve", "stop", "stop_background", "status", "ServerStatus"):
            assert name in fastware.__all__, f"{name} not in fastware.__all__"
