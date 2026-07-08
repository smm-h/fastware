"""Tests for serve_background: readiness detection and callable targets."""

from __future__ import annotations

import time
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fastware.server import serve_background


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
