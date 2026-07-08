"""Tests for serve_background: readiness detection and callable targets."""

from __future__ import annotations

import time
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fastware.server import serve, serve_background, stop


# ---------------------------------------------------------------------------
# Readiness poll: any HTTP response proves the server is up
# ---------------------------------------------------------------------------


class TestReadinessPoll:
    """The readiness poll must not require a /health route."""

    def test_http_404_counts_as_ready(self, tmp_path: Path) -> None:
        """A 404 from /health proves the server is up -- must not time out."""
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 12345

        def raise_404(*args, **kwargs):
            raise urllib.error.HTTPError(
                "http://127.0.0.1:1/health", 404, "Not Found", None, None
            )

        with (
            patch("subprocess.Popen", return_value=mock_proc),
            patch("urllib.request.urlopen", side_effect=raise_404),
        ):
            start = time.monotonic()
            url = serve_background(
                "myapp:app",
                host="127.0.0.1",
                port=12399,
                pid_path=tmp_path / "app.pid",
            )
            elapsed = time.monotonic() - start

        assert url == "http://127.0.0.1:12399"
        # Must return promptly, not exhaust the 10s startup deadline.
        assert elapsed < 5
        mock_proc.terminate.assert_not_called()

    def test_connection_refused_then_dead_child_raises(self, tmp_path: Path) -> None:
        """A child that dies before ever answering still raises RuntimeError."""
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 1
        mock_proc.returncode = 1
        mock_proc.pid = 12345

        with (
            patch("subprocess.Popen", return_value=mock_proc),
            patch("urllib.request.urlopen", side_effect=OSError("refused")),
        ):
            with pytest.raises(RuntimeError, match="exited"):
                serve_background(
                    "myapp:app",
                    host="127.0.0.1",
                    port=12399,
                    pid_path=tmp_path / "app.pid",
                )


# ---------------------------------------------------------------------------
# Callable targets across process boundaries
# ---------------------------------------------------------------------------


class TestCallableTargetBackground:
    """serve_background with a callable target must work in the spawned child."""

    def test_module_level_callable_serves(self, tmp_path: Path) -> None:
        """A module-level callable target starts and answers HTTP in the child."""
        from tests.target_app_module import app

        pid_path = tmp_path / "bg.pid"
        url = serve_background(app, host="127.0.0.1", port=0, pid_path=pid_path)
        try:
            with urllib.request.urlopen(f"{url}/", timeout=5) as resp:
                assert resp.status == 200
                assert b"hello from target_app_module" in resp.read()
        finally:
            stop(pid_path)

    def test_local_callable_raises_clear_error(self, tmp_path: Path) -> None:
        """A non-importable callable (local function) raises immediately."""

        async def local_app(scope, receive, send):
            pass

        with pytest.raises(ValueError, match="importable"):
            serve_background(
                local_app,
                host="127.0.0.1",
                port=0,
                pid_path=tmp_path / "bg.pid",
            )


class TestCallableTargetReload:
    """serve(reload=True) with a callable target must work in the reload child."""

    def test_local_callable_raises_clear_error(self) -> None:
        """A non-importable callable raises before run_process starts."""

        async def local_app(scope, receive, send):
            pass

        with patch("watchfiles.run_process") as mock_run:
            with pytest.raises(ValueError, match="importable"):
                serve(local_app, foreground=True, host="127.0.0.1", port=0, reload=True)
        mock_run.assert_not_called()

    def test_module_level_callable_uses_importable_shim(self) -> None:
        """Reload passes an importable shim module + sys.path dir to the child."""
        from tests.target_app_module import app

        seen: dict[str, object] = {}

        def fake_run_process(*args, **kwargs):
            target_str, host, port, extra_sys_path = kwargs["args"]
            seen["target_str"] = target_str
            seen["extra_sys_path"] = extra_sys_path
            # The shim module file must exist while the child would import it.
            assert extra_sys_path is not None
            mod_name = target_str.split(":")[0]
            shim_file = Path(extra_sys_path) / f"{mod_name}.py"
            seen["shim_existed"] = shim_file.exists()
            seen["shim_file"] = shim_file

        with patch("watchfiles.run_process", side_effect=fake_run_process):
            serve(app, foreground=True, host="127.0.0.1", port=0, reload=True)

        assert seen["shim_existed"] is True
        assert isinstance(seen["target_str"], str)
        assert str(seen["target_str"]).endswith(":app")
        # Cleaned up after run_process returns.
        assert not Path(str(seen["extra_sys_path"])).exists()
