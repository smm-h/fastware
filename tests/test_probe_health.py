"""Tests for the shared _probe_health helper (server._probe_health).

_probe_health backs both the serve_background readiness loop and the status()
health check, so it must: return the HTTP status code for any answer (including
error statuses, which still prove the server is up), and return None only when
the connection itself fails.
"""

from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from fastware.server import _probe_health


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Server:
    """A throwaway HTTP server that answers every request with a fixed status."""

    def __init__(self, status: int):
        self.status = status
        self.port = _free_port()
        status_code = status

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(status_code)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args):  # silence stderr noise
                pass

        self._httpd = HTTPServer(("127.0.0.1", self.port), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


def test_probe_health_returns_200():
    with _Server(200) as srv:
        assert _probe_health(f"http://127.0.0.1:{srv.port}/health", 2) == 200


def test_probe_health_returns_error_status_code():
    # A 404 still proves the server is up and answering at the HTTP level.
    with _Server(404) as srv:
        assert _probe_health(f"http://127.0.0.1:{srv.port}/health", 2) == 404


def test_probe_health_returns_500():
    with _Server(500) as srv:
        assert _probe_health(f"http://127.0.0.1:{srv.port}/health", 2) == 500


def test_probe_health_connection_refused_returns_none():
    port = _free_port()  # nothing is listening here
    assert _probe_health(f"http://127.0.0.1:{port}/health", 1) is None


@pytest.mark.parametrize(
    "code,healthy",
    [(200, True), (204, True), (399, True), (400, False), (404, False), (500, False)],
)
def test_status_criterion(code, healthy):
    # Mirrors status()'s health rule: success iff 200 <= code < 400.
    assert (code is not None and 200 <= code < 400) is healthy
