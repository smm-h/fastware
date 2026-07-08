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


# ---------------------------------------------------------------------------
# Request body size cap
# ---------------------------------------------------------------------------


class TestMaxBodySize:
    def _echo_app(self, **kwargs):
        router = Router()

        @router.post("/echo")
        async def echo(req):
            return {"size": len(req.body or b"")}

        return create_app(router, request_id=False, request_timing=False, **kwargs)

    @pytest.mark.anyio
    async def test_body_over_limit_returns_413(self):
        app = self._echo_app(max_body_size=16)
        sent = await _run_http(
            app,
            _http_scope("/echo", method="POST"),
            body_messages=[{"type": "http.request", "body": b"x" * 64, "more_body": False}],
        )
        assert _response_status(sent) == 413

    @pytest.mark.anyio
    async def test_streamed_body_over_limit_returns_413(self):
        """The cap applies to the accumulated total across chunks, and the
        app stops buffering as soon as the limit is crossed."""
        app = self._echo_app(max_body_size=16)
        chunks = [
            {"type": "http.request", "body": b"x" * 10, "more_body": True},
            {"type": "http.request", "body": b"x" * 10, "more_body": True},
            {"type": "http.request", "body": b"x" * 10, "more_body": False},
        ]
        sent = await _run_http(app, _http_scope("/echo", method="POST"), body_messages=chunks)
        assert _response_status(sent) == 413

    @pytest.mark.anyio
    async def test_body_under_limit_passes_through(self):
        app = self._echo_app(max_body_size=1024)
        chunks = [
            {"type": "http.request", "body": b"a" * 10, "more_body": True},
            {"type": "http.request", "body": b"b" * 10, "more_body": False},
        ]
        sent = await _run_http(app, _http_scope("/echo", method="POST"), body_messages=chunks)
        assert _response_status(sent) == 200
        assert b'"size":20' in _response_body(sent)

    @pytest.mark.anyio
    async def test_default_limit_allows_normal_bodies(self):
        app = self._echo_app()
        sent = await _run_http(
            app,
            _http_scope("/echo", method="POST"),
            body_messages=[{"type": "http.request", "body": b"y" * 1000, "more_body": False}],
        )
        assert _response_status(sent) == 200


# ---------------------------------------------------------------------------
# WebSocket: dep failures, dep filtering, disconnect handling
# ---------------------------------------------------------------------------


async def _run_ws(app, path, incoming=None):
    """Drive a websocket scope; returns messages sent by the app."""
    messages = [{"type": "websocket.connect"}] + list(incoming or [])

    async def receive():
        if messages:
            return messages.pop(0)
        return {"type": "websocket.disconnect", "code": 1000}

    sent: list[dict] = []

    async def send(msg):
        sent.append(msg)

    scope = {"type": "websocket", "path": path, "headers": [], "query_string": b""}
    await app(scope, receive, send)
    return sent


class TestWebSocketDepAndDisconnect:
    @pytest.mark.anyio
    async def test_dep_http_error_closes_socket(self):
        """An HTTPError from a WebSocket dependency (e.g. auth) must close
        the socket instead of escaping as a server traceback."""
        from fastware import HTTPError

        router = Router()

        async def failing_dep(request):
            raise HTTPError(401, "no auth")

        @router.ws("/ws/secure", deps={"user": failing_dep})
        async def handler(ws, user):
            await ws.accept()

        app = create_app(router, request_id=False, request_timing=False)
        sent = await _run_ws(app, "/ws/secure")

        closes = [m for m in sent if m["type"] == "websocket.close"]
        assert len(closes) == 1
        assert closes[0]["code"] == 1008

    @pytest.mark.anyio
    async def test_dep_generic_error_closes_socket(self):
        router = Router()

        async def broken_dep(request):
            raise RuntimeError("db down")

        @router.ws("/ws/broken", deps={"db": broken_dep})
        async def handler(ws, db):
            await ws.accept()

        app = create_app(router, request_id=False, request_timing=False)
        sent = await _run_ws(app, "/ws/broken")

        closes = [m for m in sent if m["type"] == "websocket.close"]
        assert len(closes) == 1
        assert closes[0]["code"] == 1011

    @pytest.mark.anyio
    async def test_deps_filtered_to_handler_signature(self):
        """Router-level deps not named in the handler signature must be
        filtered out (the HTTP path already does this)."""
        router = Router()
        seen = {}

        async def extra_dep(request):
            return "extra-value"

        async def wanted_dep(request):
            return "wanted-value"

        @router.ws("/ws/filtered", deps={"extra": extra_dep, "wanted": wanted_dep})
        async def handler(ws, wanted):
            seen["wanted"] = wanted
            await ws.accept()
            await ws.close()

        app = create_app(router, request_id=False, request_timing=False)
        await _run_ws(app, "/ws/filtered")

        assert seen == {"wanted": "wanted-value"}

    @pytest.mark.anyio
    async def test_client_disconnect_does_not_raise(self):
        """A normal client disconnect (WebSocketDisconnect escaping the
        handler) must not produce a server traceback."""
        router = Router()

        @router.ws("/ws/dc")
        async def handler(ws):
            await ws.accept()
            await ws.receive_json()  # client disconnects here

        app = create_app(router, request_id=False, request_timing=False)
        # Must not raise
        sent = await _run_ws(app, "/ws/dc", incoming=[{"type": "websocket.disconnect", "code": 1001}])
        assert any(m["type"] == "websocket.accept" for m in sent)


# ---------------------------------------------------------------------------
# Unbound 'request' in exception-handler path
# ---------------------------------------------------------------------------


class TestUnboundRequestGuard:
    @pytest.mark.anyio
    async def test_receive_error_before_request_bound_returns_500(self, caplog):
        """If receive() raises before Request is constructed, the exception
        handler must not be invoked with an unbound 'request' (previously an
        UnboundLocalError/NameError fired inside the handler-dispatch block,
        logged misleadingly as an 'Exception handler error'). The app must
        skip handlers cleanly and respond 500."""
        import logging as _logging

        router = Router()

        @router.post("/upload")
        async def upload(req):
            return {"ok": True}

        handler_called = {}

        async def handle_runtime(request, exc):
            handler_called["yes"] = True
            return {"handled": True}

        app = create_app(
            router,
            exception_handlers={RuntimeError: handle_runtime},
            request_id=False,
            request_timing=False,
        )

        async def receive():
            raise RuntimeError("transport exploded")

        sent: list[dict] = []

        async def send(msg):
            sent.append(msg)

        with caplog.at_level(_logging.DEBUG, logger="fastware"):
            # Must not raise
            await app(_http_scope("/upload", method="POST"), receive, send)

        assert _response_status(sent) == 500
        assert handler_called == {}
        # No NameError/UnboundLocalError anywhere in the logged tracebacks
        for record in caplog.records:
            if record.exc_info:
                assert not isinstance(record.exc_info[1], NameError), (
                    "unbound 'request' leaked into exception-handler dispatch"
                )


# ---------------------------------------------------------------------------
# Accepted-param sets precomputed (no per-request inspect.signature)
# ---------------------------------------------------------------------------


class TestPrecomputedHandlerParams:
    @pytest.mark.anyio
    async def test_no_signature_introspection_per_request(self, monkeypatch):
        """The accepted-param set for dep filtering must be computed once
        (at app creation), not via inspect.signature on every request."""
        import inspect as inspect_mod

        router = Router()

        async def auth_dep(request):
            return {"sub": "u1"}

        @router.get("/items", deps={"user": auth_dep})
        async def items(req):  # does not accept 'user' -> filtering applies
            return {"items": []}

        app = create_app(router, request_id=False, request_timing=False)

        introspected = []
        orig_signature = inspect_mod.signature

        def counting_signature(obj, *args, **kwargs):
            introspected.append(obj)
            return orig_signature(obj, *args, **kwargs)

        monkeypatch.setattr(inspect_mod, "signature", counting_signature)

        for _ in range(2):
            sent = await _run_http(app, _http_scope("/items"))
            assert _response_status(sent) == 200

        # The route handler must never be introspected at request time
        assert items not in introspected
