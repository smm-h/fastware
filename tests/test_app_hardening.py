"""Regression tests for app.py hardening fixes.

Covers:
- Static file path traversal via sibling-prefix directories (startswith flaw)
- SPA-root serving path reusing the same traversal guard
"""

from __future__ import annotations

import pytest

from fastware import Router, create_app
from fastware.app import _serve_static


# ---------------------------------------------------------------------------
# Raw ASGI helpers
# ---------------------------------------------------------------------------


def _http_scope(path: str, method: str = "GET") -> dict:
    return {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [],
        "query_string": b"",
    }


async def _run_http(app, scope, body_messages=None):
    """Drive an app with a raw HTTP scope, returning sent messages."""
    messages = list(body_messages or [{"type": "http.request", "body": b"", "more_body": False}])

    async def receive():
        if messages:
            return messages.pop(0)
        return {"type": "http.disconnect"}

    sent: list[dict] = []

    async def send(msg):
        sent.append(msg)

    await app(scope, receive, send)
    return sent


def _response_body(sent: list[dict]) -> bytes:
    return b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")


def _response_status(sent: list[dict]) -> int:
    for m in sent:
        if m["type"] == "http.response.start":
            return m["status"]
    raise AssertionError("no http.response.start sent")


# ---------------------------------------------------------------------------
# Path traversal: sibling-prefix escape
# ---------------------------------------------------------------------------


class TestStaticPathTraversal:
    @pytest.mark.anyio
    async def test_sibling_prefix_dir_not_served(self, tmp_path):
        """A sibling dir sharing the static dir's name as a string prefix
        (e.g. static-private next to static) must not be reachable."""
        static = tmp_path / "static"
        static.mkdir()
        (static / "ok.txt").write_text("public")
        private = tmp_path / "static-private"
        private.mkdir()
        (private / "secret.txt").write_text("secret")

        sent: list[dict] = []

        async def send(msg):
            sent.append(msg)

        served = await _serve_static(send, static, "../static-private/secret.txt")
        assert served is False
        assert sent == []

    @pytest.mark.anyio
    async def test_dotdot_traversal_not_served(self, tmp_path):
        static = tmp_path / "static"
        static.mkdir()
        (tmp_path / "outside.txt").write_text("outside")

        sent: list[dict] = []

        async def send(msg):
            sent.append(msg)

        served = await _serve_static(send, static, "../outside.txt")
        assert served is False
        assert sent == []

    @pytest.mark.anyio
    async def test_legit_file_still_served(self, tmp_path):
        static = tmp_path / "static"
        static.mkdir()
        (static / "app.js").write_text("console.log(1)")

        sent: list[dict] = []

        async def send(msg):
            sent.append(msg)

        served = await _serve_static(send, static, "app.js")
        assert served is True
        assert _response_body(sent) == b"console.log(1)"

    @pytest.mark.anyio
    async def test_spa_root_sibling_prefix_not_served(self, tmp_path):
        """The SPA-root serving path (spa_fallback.parent) must apply the
        same traversal guard: a sibling-prefix dir must not be reachable."""
        static = tmp_path / "static"
        static.mkdir()
        (static / "index.html").write_text("<html>index</html>")
        private = tmp_path / "static-private"
        private.mkdir()
        (private / "secret.txt").write_text("secret")

        router = Router()
        app = create_app(
            router,
            spa_fallback=static / "index.html",
            request_id=False,
            request_timing=False,
        )

        # Raw ASGI scope bypasses httpx URL normalization
        sent = await _run_http(app, _http_scope("/../static-private/secret.txt"))
        body = _response_body(sent)
        assert b"secret" not in body
        assert body == b"<html>index</html>"
