"""Regression tests for app.py hardening fixes.

Covers:
- Static file path traversal via sibling-prefix directories (startswith flaw)
- SPA-root serving path reusing the same traversal guard
- Lifespan startup/shutdown failure messaging (ASGI lifespan.*.failed)
"""

from __future__ import annotations

from contextlib import asynccontextmanager

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


# ---------------------------------------------------------------------------
# Lifespan failure messaging
# ---------------------------------------------------------------------------


async def _run_lifespan(app, *, shutdown: bool = True):
    """Drive the ASGI lifespan protocol; returns messages sent by the app."""
    messages = [{"type": "lifespan.startup"}]
    if shutdown:
        messages.append({"type": "lifespan.shutdown"})

    async def receive():
        return messages.pop(0)

    sent: list[dict] = []

    async def send(msg):
        sent.append(msg)

    await app({"type": "lifespan"}, receive, send)
    return sent


class TestLifespanFailureMessaging:
    @pytest.mark.anyio
    async def test_startup_failure_sends_failed_message(self):
        @asynccontextmanager
        async def lifespan(app):
            raise RuntimeError("db unreachable")
            yield

        app = create_app(Router(), lifespan=lifespan, request_id=False, request_timing=False)
        sent = await _run_lifespan(app, shutdown=False)

        assert sent == [{"type": "lifespan.startup.failed", "message": "db unreachable"}]

    @pytest.mark.anyio
    async def test_shutdown_failure_sends_failed_message(self):
        @asynccontextmanager
        async def lifespan(app):
            yield
            raise RuntimeError("teardown exploded")

        app = create_app(Router(), lifespan=lifespan, request_id=False, request_timing=False)
        sent = await _run_lifespan(app)

        assert sent[0] == {"type": "lifespan.startup.complete"}
        assert sent[1]["type"] == "lifespan.shutdown.failed"
        assert "teardown exploded" in sent[1]["message"]

    @pytest.mark.anyio
    async def test_clean_lifespan_still_completes(self):
        state = {}

        @asynccontextmanager
        async def lifespan(app):
            state["up"] = True
            yield {"answer": 42}
            state["down"] = True

        app = create_app(Router(), lifespan=lifespan, request_id=False, request_timing=False)
        sent = await _run_lifespan(app)

        assert sent == [
            {"type": "lifespan.startup.complete"},
            {"type": "lifespan.shutdown.complete"},
        ]
        assert state == {"up": True, "down": True}


# ---------------------------------------------------------------------------
# Streaming: no double response after http.response.start
# ---------------------------------------------------------------------------


def _stream_app():
    from fastware import StreamResponse

    router = Router()

    @router.get("/sse")
    async def sse(req):
        async def gen():
            yield "one"
            yield "two"
            yield "three"

        return StreamResponse(gen(), content_type="text/event-stream")

    return create_app(router, request_id=False, request_timing=False)


class TestStreamBrokenClient:
    @pytest.mark.anyio
    async def test_client_disconnect_mid_stream_no_second_start(self):
        """When send() starts failing mid-stream (client gone), the app must
        neither raise nor attempt a second http.response.start (500)."""
        app = _stream_app()
        sent: list[dict] = []
        body_count = 0

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(msg):
            nonlocal body_count
            if msg["type"] == "http.response.body":
                body_count += 1
                if body_count >= 2:
                    raise ConnectionResetError("client disconnected")
            elif sent and body_count >= 2:
                raise ConnectionResetError("client disconnected")
            sent.append(msg)

        # Must not raise even though the client vanished mid-stream
        await app(_http_scope("/sse"), receive, send)

        starts = [m for m in sent if m["type"] == "http.response.start"]
        assert len(starts) == 1
        assert starts[0]["status"] == 200

    @pytest.mark.anyio
    async def test_generator_error_after_start_no_second_start(self):
        """A generator bug after the response started cannot be turned into
        a 500 -- there must never be a second http.response.start."""
        from fastware import StreamResponse

        router = Router()

        @router.get("/boom")
        async def boom(req):
            async def gen():
                yield "first"
                raise RuntimeError("generator bug")

            return StreamResponse(gen(), content_type="text/event-stream")

        app = create_app(router, request_id=False, request_timing=False)
        sent = await _run_http(app, _http_scope("/boom"))

        starts = [m for m in sent if m["type"] == "http.response.start"]
        assert len(starts) == 1
        assert starts[0]["status"] == 200
