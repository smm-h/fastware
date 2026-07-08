"""Tests for Phase 5: Middleware and observability.

Covers:
- 5.1 structlog integration (configure_logging, get_logger)
- 5.2 Request ID middleware (generation, propagation, response header)
- 5.3 Request timing middleware (duration, ring buffer, error_log escalation)
- 5.4 CORS middleware (preflight OPTIONS, response headers, origin filtering)
- 5.5 TrustedHost middleware (allowed/rejected hosts)
- 5.6 Built-in middleware wiring (create_app parameters)
- 5.7 Error tracking (Sentry init_sentry with mock)
- 5.8 SQLite error log (ErrorLog append and recent)
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from fastware import (
    HTTPError,
    JSONResponse,
    Router,
    create_app,
    send_error,
)
from fastware.error_log import ErrorLog
from fastware.logging import configure_logging, get_logger, init_sentry
from fastware.middleware import (
    CORSMiddleware,
    RequestIDMiddleware,
    RequestTimingMiddleware,
    TrustedHostMiddleware,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_app(**kwargs):
    """Create a minimal app with GET /health for testing middleware."""
    router = Router()

    @router.get("/health")
    async def health(req):
        return JSONResponse({"status": "ok"})

    @router.get("/fail")
    async def fail(req):
        raise RuntimeError("boom")

    return create_app(router, request_id=False, request_timing=False, **kwargs)


def _client(app, base_url: str = "http://test") -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url=base_url,
    )


# ---------------------------------------------------------------------------
# 5.1 structlog integration
# ---------------------------------------------------------------------------

class TestStructlogIntegration:

    def test_configure_logging_json(self):
        """configure_logging(json_output=True) completes without error."""
        configure_logging(json_output=True)

    def test_configure_logging_console(self):
        """configure_logging(json_output=False) completes without error."""
        configure_logging(json_output=False)

    def test_configure_logging_auto(self):
        """configure_logging() auto-detects based on isatty."""
        configure_logging()

    def test_get_logger_returns_logger(self):
        """get_logger() returns a structlog bound logger."""
        configure_logging(json_output=True)
        logger = get_logger()
        assert logger is not None
        # Should have standard log methods.
        assert hasattr(logger, "info")
        assert hasattr(logger, "debug")
        assert hasattr(logger, "warning")
        assert hasattr(logger, "error")

    def test_get_logger_with_component(self):
        """get_logger(component=...) binds the component key."""
        configure_logging(json_output=True)
        logger = get_logger("auth")
        assert logger is not None

    def test_get_logger_with_kwargs(self):
        """get_logger() accepts arbitrary initial bindings."""
        configure_logging(json_output=True)
        logger = get_logger(component="db", host="localhost")
        assert logger is not None


# ---------------------------------------------------------------------------
# 5.2 Request ID middleware
# ---------------------------------------------------------------------------

class TestRequestIDMiddleware:

    @pytest.mark.anyio
    async def test_generates_request_id(self):
        """A new UUID is generated when no X-Request-Id header is sent."""
        app = _make_app()
        inner = RequestIDMiddleware(app)
        async with _client(inner) as client:
            resp = await client.get("/health")
            assert resp.status_code == 200
            rid = resp.headers.get("x-request-id")
            assert rid is not None
            assert len(rid) == 36  # UUID4 format

    @pytest.mark.anyio
    async def test_propagates_incoming_request_id(self):
        """An incoming X-Request-Id header is reused in the response."""
        app = _make_app()
        inner = RequestIDMiddleware(app)
        async with _client(inner) as client:
            resp = await client.get(
                "/health",
                headers={"x-request-id": "my-custom-id"},
            )
            assert resp.headers["x-request-id"] == "my-custom-id"

    @pytest.mark.anyio
    async def test_duplicate_header_first_value_wins(self):
        """With duplicate X-Request-Id headers, the first value is used."""
        app = _make_app()
        inner = RequestIDMiddleware(app)
        async with _client(inner) as client:
            resp = await client.get(
                "/health",
                headers=[("x-request-id", "first-id"), ("x-request-id", "second-id")],
            )
            assert resp.headers["x-request-id"] == "first-id"

    @pytest.mark.anyio
    async def test_stores_in_scope_state(self):
        """Request ID is stored in scope['state']['request_id']."""
        router = Router()
        captured = {}

        @router.get("/check")
        async def check(req):
            captured["request_id"] = req.state.get("request_id")
            return JSONResponse({"ok": True})

        app = create_app(router, request_id=False, request_timing=False)
        inner = RequestIDMiddleware(app)
        async with _client(inner) as client:
            resp = await client.get(
                "/check",
                headers={"x-request-id": "trace-123"},
            )
            assert resp.status_code == 200
            assert captured["request_id"] == "trace-123"

    @pytest.mark.anyio
    async def test_non_http_passthrough(self):
        """Non-http scope types pass through without modification."""
        called = False

        async def inner_app(scope, receive, send):
            nonlocal called
            called = True

        mw = RequestIDMiddleware(inner_app)
        await mw({"type": "lifespan"}, None, None)
        assert called


# ---------------------------------------------------------------------------
# 5.3 Request timing middleware
# ---------------------------------------------------------------------------

class TestRequestTimingMiddleware:

    @pytest.mark.anyio
    async def test_request_count_increments(self):
        """request_count increments on each request."""
        app = _make_app()
        mw = RequestTimingMiddleware(app)
        assert mw.request_count == 0
        async with _client(mw) as client:
            await client.get("/health")
        assert mw.request_count == 1

    @pytest.mark.anyio
    async def test_ring_buffer_captures_entry(self):
        """Ring buffer stores (timestamp, method, path, status, duration_ms)."""
        app = _make_app()
        mw = RequestTimingMiddleware(app)
        async with _client(mw) as client:
            await client.get("/health")
        assert len(mw.request_history) == 1
        entry = mw.request_history[0]
        assert entry[1] == "GET"
        assert entry[2] == "/health"
        assert entry[3] == 200
        assert entry[4] >= 0  # duration_ms

    @pytest.mark.anyio
    async def test_ring_buffer_maxlen(self):
        """Ring buffer respects maxlen."""
        app = _make_app()
        mw = RequestTimingMiddleware(app, maxlen=3)
        async with _client(mw) as client:
            for _ in range(5):
                await client.get("/health")
        assert len(mw.request_history) == 3

    @pytest.mark.anyio
    async def test_exclude_paths(self):
        """Excluded paths are not stored in the ring buffer."""
        app = _make_app()
        mw = RequestTimingMiddleware(app, exclude_paths=["/health"])
        async with _client(mw) as client:
            await client.get("/health")
        assert len(mw.request_history) == 0
        assert mw.request_count == 1  # Still counted

    @pytest.mark.anyio
    async def test_error_log_on_5xx(self, tmp_path):
        """On 5xx responses, the error_log.append() is called."""
        error_log = ErrorLog(tmp_path / "errors.db")
        app = _make_app()
        mw = RequestTimingMiddleware(app, error_log=error_log)
        async with _client(mw) as client:
            resp = await client.get("/fail")
            assert resp.status_code == 500
        entries = error_log.recent()
        assert len(entries) == 1
        assert entries[0]["method"] == "GET"
        assert entries[0]["path"] == "/fail"
        assert entries[0]["status_code"] == 500

    @pytest.mark.anyio
    async def test_non_http_passthrough(self):
        """Non-http scope types pass through without modification."""
        called = False

        async def inner_app(scope, receive, send):
            nonlocal called
            called = True

        mw = RequestTimingMiddleware(inner_app)
        await mw({"type": "lifespan"}, None, None)
        assert called


# ---------------------------------------------------------------------------
# 5.4 CORS middleware
# ---------------------------------------------------------------------------

class TestCORSMiddleware:

    @pytest.mark.anyio
    async def test_preflight_returns_204(self):
        """Preflight OPTIONS with matching origin returns 204 with CORS headers."""
        app = _make_app()
        mw = CORSMiddleware(app, allow_origins=["http://localhost:5173"])
        async with _client(mw) as client:
            resp = await client.options(
                "/health",
                headers={
                    "origin": "http://localhost:5173",
                    "access-control-request-method": "POST",
                },
            )
            assert resp.status_code == 204
            assert resp.headers["access-control-allow-origin"] == "http://localhost:5173"
            assert "POST" in resp.headers["access-control-allow-methods"]
            assert resp.headers.get("access-control-allow-credentials") == "true"

    @pytest.mark.anyio
    async def test_normal_request_gets_cors_headers(self):
        """A normal GET with matching origin gets CORS headers in the response."""
        app = _make_app()
        mw = CORSMiddleware(app, allow_origins=["http://localhost:5173"])
        async with _client(mw) as client:
            resp = await client.get(
                "/health",
                headers={"origin": "http://localhost:5173"},
            )
            assert resp.status_code == 200
            assert resp.headers["access-control-allow-origin"] == "http://localhost:5173"

    @pytest.mark.anyio
    async def test_unmatched_origin_passes_through(self):
        """A request from an unlisted origin gets no CORS headers."""
        app = _make_app()
        mw = CORSMiddleware(app, allow_origins=["http://allowed.example.com"])
        async with _client(mw) as client:
            resp = await client.get(
                "/health",
                headers={"origin": "http://evil.example.com"},
            )
            assert resp.status_code == 200
            assert "access-control-allow-origin" not in resp.headers

    @pytest.mark.anyio
    async def test_wildcard_origin(self):
        """allow_origins=["*"] (without credentials) matches any origin."""
        app = _make_app()
        mw = CORSMiddleware(app, allow_origins=["*"], allow_credentials=False)
        async with _client(mw) as client:
            resp = await client.get(
                "/health",
                headers={"origin": "http://anything.example.com"},
            )
            assert resp.headers["access-control-allow-origin"] == "http://anything.example.com"

    def test_wildcard_with_credentials_raises(self):
        """allow_origins=["*"] + allow_credentials=True is rejected at construction."""
        app = _make_app()
        with pytest.raises(ValueError, match="allow_credentials"):
            CORSMiddleware(app, allow_origins=["*"], allow_credentials=True)

    def test_wildcard_with_default_credentials_raises(self):
        """The default allow_credentials=True must also be rejected with '*'."""
        app = _make_app()
        with pytest.raises(ValueError, match="allow_credentials"):
            CORSMiddleware(app, allow_origins=["*"])

    @pytest.mark.anyio
    async def test_vary_origin_on_normal_response(self):
        """CORS responses carry Vary: Origin so caches key on the origin."""
        app = _make_app()
        mw = CORSMiddleware(app, allow_origins=["http://localhost:5173"])
        async with _client(mw) as client:
            resp = await client.get(
                "/health",
                headers={"origin": "http://localhost:5173"},
            )
            assert "origin" in resp.headers.get("vary", "").lower()

    @pytest.mark.anyio
    async def test_vary_origin_on_preflight(self):
        """Preflight responses carry Vary: Origin."""
        app = _make_app()
        mw = CORSMiddleware(app, allow_origins=["http://localhost:5173"])
        async with _client(mw) as client:
            resp = await client.options(
                "/health",
                headers={
                    "origin": "http://localhost:5173",
                    "access-control-request-method": "POST",
                },
            )
            assert resp.status_code == 204
            assert "origin" in resp.headers.get("vary", "").lower()

    @pytest.mark.anyio
    async def test_plain_options_reaches_app_route(self):
        """OPTIONS without Access-Control-Request-Method is NOT a preflight.

        App-defined OPTIONS routes must remain reachable; only real
        preflights (which carry Access-Control-Request-Method) are
        short-circuited by the middleware.
        """
        router = Router()

        async def custom_options(req):
            return JSONResponse({"custom": True})

        router.add_route("OPTIONS", "/thing", custom_options)
        app = create_app(router, request_id=False, request_timing=False)
        mw = CORSMiddleware(app, allow_origins=["http://test"])
        async with _client(mw) as client:
            resp = await client.options(
                "/thing",
                headers={"origin": "http://test"},
            )
            assert resp.status_code == 200
            assert resp.json() == {"custom": True}
            # CORS headers still injected on the pass-through response.
            assert resp.headers["access-control-allow-origin"] == "http://test"

    @pytest.mark.anyio
    async def test_no_origin_header_passes_through(self):
        """A request without an Origin header passes through without CORS headers."""
        app = _make_app()
        mw = CORSMiddleware(app, allow_origins=["http://localhost:5173"])
        async with _client(mw) as client:
            resp = await client.get("/health")
            assert resp.status_code == 200
            assert "access-control-allow-origin" not in resp.headers

    @pytest.mark.anyio
    async def test_custom_methods_and_headers(self):
        """Custom allow_methods and allow_headers are returned in preflight."""
        app = _make_app()
        mw = CORSMiddleware(
            app,
            allow_origins=["http://test"],
            allow_methods=["GET", "POST"],
            allow_headers=["x-custom"],
            allow_credentials=False,
        )
        async with _client(mw) as client:
            resp = await client.options(
                "/health",
                headers={
                    "origin": "http://test",
                    "access-control-request-method": "POST",
                },
            )
            assert resp.status_code == 204
            assert resp.headers["access-control-allow-methods"] == "GET, POST"
            assert resp.headers["access-control-allow-headers"] == "x-custom"
            assert "access-control-allow-credentials" not in resp.headers

    @pytest.mark.anyio
    async def test_non_http_passthrough(self):
        """Non-http scope types pass through without modification."""
        called = False

        async def inner_app(scope, receive, send):
            nonlocal called
            called = True

        mw = CORSMiddleware(inner_app, allow_origins=["*"], allow_credentials=False)
        await mw({"type": "websocket"}, None, None)
        assert called


# ---------------------------------------------------------------------------
# 5.5 TrustedHost middleware
# ---------------------------------------------------------------------------

class TestTrustedHostMiddleware:

    @pytest.mark.anyio
    async def test_allowed_host_passes(self):
        """A request with a trusted Host header passes through."""
        app = _make_app()
        mw = TrustedHostMiddleware(app, allowed_hosts=["test"])
        async with _client(mw) as client:
            resp = await client.get("/health")
            assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_disallowed_host_returns_400(self):
        """A request from an untrusted Host returns 400."""
        app = _make_app()
        mw = TrustedHostMiddleware(app, allowed_hosts=["trusted.local"])
        async with _client(mw, base_url="http://evil.local") as client:
            resp = await client.get("/health")
            assert resp.status_code == 400
            data = resp.json()
            assert "Invalid host" in data["detail"]

    @pytest.mark.anyio
    async def test_wildcard_allows_all(self):
        """allowed_hosts=["*"] allows any host."""
        app = _make_app()
        mw = TrustedHostMiddleware(app, allowed_hosts=["*"])
        async with _client(mw, base_url="http://anything.example.com") as client:
            resp = await client.get("/health")
            assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_non_http_passthrough(self):
        """Non-http/ws scope types pass through without modification."""
        called = False

        async def inner_app(scope, receive, send):
            nonlocal called
            called = True

        mw = TrustedHostMiddleware(inner_app, allowed_hosts=["localhost"])
        await mw({"type": "lifespan"}, None, None)
        assert called

    @pytest.mark.anyio
    async def test_websocket_disallowed_host_closed(self):
        """DNS-rebinding protection applies to ws:// too — bad Host is rejected."""
        called = False

        async def inner_app(scope, receive, send):
            nonlocal called
            called = True

        mw = TrustedHostMiddleware(inner_app, allowed_hosts=["trusted.local"])
        sent: list[dict] = []
        incoming = [{"type": "websocket.connect"}]

        async def receive():
            return incoming.pop(0)

        async def send(message):
            sent.append(message)

        scope = {
            "type": "websocket",
            "path": "/ws",
            "headers": [(b"host", b"evil.local")],
        }
        await mw(scope, receive, send)
        assert not called
        assert sent[-1]["type"] == "websocket.close"
        assert sent[-1]["code"] == 1008  # policy violation

    @pytest.mark.anyio
    async def test_websocket_allowed_host_passes(self):
        """A websocket with a trusted Host header reaches the app."""
        called = False

        async def inner_app(scope, receive, send):
            nonlocal called
            called = True

        mw = TrustedHostMiddleware(inner_app, allowed_hosts=["trusted.local"])
        sent: list[dict] = []

        async def receive():
            return {"type": "websocket.connect"}

        async def send(message):
            sent.append(message)

        scope = {
            "type": "websocket",
            "path": "/ws",
            "headers": [(b"host", b"trusted.local")],
        }
        await mw(scope, receive, send)
        assert called
        assert sent == []  # middleware did not respond on the app's behalf


# ---------------------------------------------------------------------------
# 5.5b ViteDevProxy HTTP path
# ---------------------------------------------------------------------------

def _http_scope(path: str, method: str = "GET") -> dict:
    return {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": b"",
        "headers": [],
    }


class TestViteDevProxyHTTP:

    @pytest.mark.anyio
    async def test_streams_response_chunks_without_buffering(self):
        """SSE-style responses stream through chunk by chunk.

        The inner app blocks after its first chunk until that chunk has
        been observed downstream.  A proxy that buffers the whole response
        never forwards the chunk, so this times out (red before the fix).
        """
        import asyncio

        from fastware.middleware import ViteDevProxy

        proceed = asyncio.Event()

        async def inner_app(scope, receive, send):
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
            })
            await send({
                "type": "http.response.body",
                "body": b"data: 1\n\n",
                "more_body": True,
            })
            await proceed.wait()
            await send({
                "type": "http.response.body",
                "body": b"data: 2\n\n",
                "more_body": False,
            })

        proxy = ViteDevProxy(inner_app, vite_port=1)  # port never contacted
        sent: list[dict] = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        task = asyncio.ensure_future(proxy(_http_scope("/stream"), receive, send))
        try:

            async def first_chunk_forwarded():
                while not any(m["type"] == "http.response.body" for m in sent):
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(first_chunk_forwarded(), timeout=2)
        finally:
            proceed.set()
        await asyncio.wait_for(task, timeout=2)

        assert sent[0]["type"] == "http.response.start"
        bodies = [m for m in sent if m["type"] == "http.response.body"]
        assert bodies[0]["body"] == b"data: 1\n\n"
        assert bodies[-1]["body"] == b"data: 2\n\n"

    @pytest.mark.anyio
    async def test_backend_prefix_404_not_proxied(self):
        """A 404 from a backend-prefixed path returns as-is, never hits Vite."""
        from fastware.middleware import ViteDevProxy

        async def inner_app(scope, receive, send):
            await send({"type": "http.response.start", "status": 404, "headers": []})
            await send({"type": "http.response.body", "body": b"nope"})

        proxy = ViteDevProxy(inner_app, vite_port=1, backend_prefixes=["/events"])
        sent: list[dict] = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        await proxy(_http_scope("/events/missing"), receive, send)
        assert sent[0]["type"] == "http.response.start"
        assert sent[0]["status"] == 404  # not a 502 from a Vite connect attempt
        assert sent[1]["body"] == b"nope"

    @pytest.mark.anyio
    async def test_unread_request_body_replayed_to_vite(self, monkeypatch):
        """A 404ing backend that never reads the body doesn't lose it for Vite."""
        from fastware.middleware import ViteDevProxy

        async def inner_app(scope, receive, send):
            # 404 without ever reading the request body.
            await send({"type": "http.response.start", "status": 404, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        proxy = ViteDevProxy(inner_app, vite_port=1)
        captured: dict = {}

        async def fake_proxy_http(scope, send, *, body=b""):
            captured["body"] = body

        monkeypatch.setattr(proxy, "_proxy_http", fake_proxy_http)

        chunks = [
            {"type": "http.request", "body": b"hello ", "more_body": True},
            {"type": "http.request", "body": b"world", "more_body": False},
        ]

        async def receive():
            return chunks.pop(0)

        async def send(message):
            pass

        await proxy(_http_scope("/app-page", method="POST"), receive, send)
        assert captured["body"] == b"hello world"

    @pytest.mark.anyio
    async def test_404_swallowed_then_proxied(self, monkeypatch):
        """On a non-backend 404, nothing from the backend reaches the client."""
        from fastware.middleware import ViteDevProxy

        async def inner_app(scope, receive, send):
            await send({"type": "http.response.start", "status": 404, "headers": []})
            await send({"type": "http.response.body", "body": b"backend 404 page"})

        proxy = ViteDevProxy(inner_app, vite_port=1)
        proxied = {}

        async def fake_proxy_http(scope, send, *, body=b""):
            proxied["called"] = True

        monkeypatch.setattr(proxy, "_proxy_http", fake_proxy_http)
        sent: list[dict] = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        await proxy(_http_scope("/index.html"), receive, send)
        assert proxied.get("called")
        assert sent == []  # backend's 404 messages were discarded


# ---------------------------------------------------------------------------
# 5.5c ViteDevProxy WebSocket path
# ---------------------------------------------------------------------------

class TestViteDevProxyWS:

    @staticmethod
    def _ws_fixtures():
        sent: list[dict] = []
        incoming = [{"type": "websocket.connect"}]

        async def receive():
            return incoming.pop(0)

        async def send(message):
            sent.append(message)

        scope = {
            "type": "websocket",
            "path": "/hmr",
            "query_string": b"",
            "headers": [],
        }
        return scope, receive, send, sent

    @pytest.mark.anyio
    async def test_missing_websockets_closes_with_error_code(self):
        """Without the websockets library, close with 1011 -- not a clean 1000."""
        from fastware.middleware import ViteDevProxy

        async def inner_app(scope, receive, send):
            raise AssertionError("inner app must not be called")

        proxy = ViteDevProxy(inner_app, vite_port=1)
        scope, receive, send, sent = self._ws_fixtures()

        with patch.dict(
            "sys.modules",
            {
                "websockets": None,
                "websockets.asyncio": None,
                "websockets.asyncio.client": None,
            },
        ):
            await proxy._proxy_ws(scope, receive, send)

        close = sent[-1]
        assert close["type"] == "websocket.close"
        assert close["code"] == 1011

    @pytest.mark.anyio
    async def test_vite_unreachable_closes_with_error_code(self):
        """A failed proxy connection closes with 1011, not a clean 1000."""
        pytest.importorskip("websockets")
        from fastware.middleware import ViteDevProxy

        async def inner_app(scope, receive, send):
            raise AssertionError("inner app must not be called")

        proxy = ViteDevProxy(inner_app, vite_port=1)  # nothing listens here
        scope, receive, send, sent = self._ws_fixtures()

        await proxy._proxy_ws(scope, receive, send)

        close = sent[-1]
        assert close["type"] == "websocket.close"
        assert close["code"] == 1011


# ---------------------------------------------------------------------------
# 5.6 Built-in middleware wiring
# ---------------------------------------------------------------------------

class TestBuiltinMiddlewareWiring:

    @pytest.mark.anyio
    async def test_create_app_with_request_id(self):
        """create_app(request_id=True) adds X-Request-Id header."""
        router = Router()

        @router.get("/health")
        async def health(req):
            return JSONResponse({"ok": True})

        app = create_app(router, request_timing=False)
        async with _client(app) as client:
            resp = await client.get("/health")
            assert resp.status_code == 200
            assert "x-request-id" in resp.headers

    @pytest.mark.anyio
    async def test_create_app_without_request_id(self):
        """create_app(request_id=False) does not add X-Request-Id."""
        router = Router()

        @router.get("/health")
        async def health(req):
            return JSONResponse({"ok": True})

        app = create_app(router, request_id=False, request_timing=False)
        async with _client(app) as client:
            resp = await client.get("/health")
            assert resp.status_code == 200
            assert "x-request-id" not in resp.headers

    @pytest.mark.anyio
    async def test_create_app_with_cors(self):
        """create_app(cors_origins=...) adds CORS headers."""
        router = Router()

        @router.get("/health")
        async def health(req):
            return JSONResponse({"ok": True})

        app = create_app(
            router,
            cors_origins=["http://test"],
            request_id=False,
            request_timing=False,
        )
        async with _client(app) as client:
            resp = await client.get(
                "/health",
                headers={"origin": "http://test"},
            )
            assert resp.headers["access-control-allow-origin"] == "http://test"

    @pytest.mark.anyio
    async def test_create_app_with_trusted_hosts(self):
        """create_app(trusted_hosts=...) rejects bad hosts."""
        router = Router()

        @router.get("/health")
        async def health(req):
            return JSONResponse({"ok": True})

        app = create_app(
            router,
            trusted_hosts=["test"],
            request_id=False,
            request_timing=False,
        )
        # Allowed host
        async with _client(app) as client:
            resp = await client.get("/health")
            assert resp.status_code == 200

        # Disallowed host
        async with _client(app, base_url="http://evil.com") as client:
            resp = await client.get("/health")
            assert resp.status_code == 400

    @pytest.mark.anyio
    async def test_all_middleware_together(self):
        """All built-in middleware work together in correct order."""
        router = Router()

        @router.get("/health")
        async def health(req):
            return JSONResponse({"ok": True})

        app = create_app(
            router,
            cors_origins=["http://test"],
            trusted_hosts=["test"],
            request_id=True,
            request_timing=True,
        )
        async with _client(app) as client:
            resp = await client.get(
                "/health",
                headers={"origin": "http://test"},
            )
            assert resp.status_code == 200
            assert "x-request-id" in resp.headers
            assert resp.headers["access-control-allow-origin"] == "http://test"


# ---------------------------------------------------------------------------
# 5.7 Error tracking (Sentry)
# ---------------------------------------------------------------------------

class TestSentryIntegration:

    def test_init_sentry_no_sdk(self):
        """init_sentry is a no-op when sentry_sdk is not installed."""
        with patch.dict("sys.modules", {"sentry_sdk": None}):
            # Should not raise.
            init_sentry("https://fake@sentry.io/123")

    def test_init_sentry_with_mock_sdk(self):
        """init_sentry calls sentry_sdk.init() with DSN and before_send."""
        mock_sdk = MagicMock()
        with patch.dict("sys.modules", {"sentry_sdk": mock_sdk}):
            init_sentry("https://fake@sentry.io/123", traces_sample_rate=0.5)
            mock_sdk.init.assert_called_once()
            call_kwargs = mock_sdk.init.call_args
            assert call_kwargs[1]["dsn"] == "https://fake@sentry.io/123"
            assert call_kwargs[1]["traces_sample_rate"] == 0.5
            assert callable(call_kwargs[1]["before_send"])

    def test_before_send_filters_4xx(self):
        """before_send returns None for HTTPError with 4xx status."""
        mock_sdk = MagicMock()
        with patch.dict("sys.modules", {"sentry_sdk": mock_sdk}):
            init_sentry("https://fake@sentry.io/123")
            before_send = mock_sdk.init.call_args[1]["before_send"]

            # 4xx should be filtered out
            exc = HTTPError(404, "not found")
            result = before_send(
                {"event": "test"},
                {"exc_info": (type(exc), exc, None)},
            )
            assert result is None

    def test_before_send_passes_5xx(self):
        """before_send passes through HTTPError with 5xx status."""
        mock_sdk = MagicMock()
        with patch.dict("sys.modules", {"sentry_sdk": mock_sdk}):
            init_sentry("https://fake@sentry.io/123")
            before_send = mock_sdk.init.call_args[1]["before_send"]

            exc = HTTPError(500, "internal")
            event = {"event": "test"}
            result = before_send(event, {"exc_info": (type(exc), exc, None)})
            assert result is event

    def test_before_send_passes_non_http_errors(self):
        """before_send passes through non-HTTPError exceptions."""
        mock_sdk = MagicMock()
        with patch.dict("sys.modules", {"sentry_sdk": mock_sdk}):
            init_sentry("https://fake@sentry.io/123")
            before_send = mock_sdk.init.call_args[1]["before_send"]

            exc = ValueError("bad value")
            event = {"event": "test"}
            result = before_send(event, {"exc_info": (type(exc), exc, None)})
            assert result is event

    def test_before_send_passes_no_exc_info(self):
        """before_send passes through events without exc_info."""
        mock_sdk = MagicMock()
        with patch.dict("sys.modules", {"sentry_sdk": mock_sdk}):
            init_sentry("https://fake@sentry.io/123")
            before_send = mock_sdk.init.call_args[1]["before_send"]

            event = {"event": "test"}
            result = before_send(event, {})
            assert result is event


# ---------------------------------------------------------------------------
# 5.8 SQLite error log
# ---------------------------------------------------------------------------

class TestErrorLog:

    def test_append_and_recent(self, tmp_path):
        """append() writes an entry and recent() retrieves it."""
        log = ErrorLog(tmp_path / "errors.db")
        log.append(
            method="POST",
            path="/api/deploy",
            status_code=500,
            detail="Docker timeout",
            request_id="abc-123",
            user="alice",
            traceback="Traceback ...",
        )
        entries = log.recent()
        assert len(entries) == 1
        e = entries[0]
        assert e["method"] == "POST"
        assert e["path"] == "/api/deploy"
        assert e["status_code"] == 500
        assert e["detail"] == "Docker timeout"
        assert e["request_id"] == "abc-123"
        assert e["user"] == "alice"
        assert e["traceback"] == "Traceback ..."
        assert e["timestamp"] > 0

    def test_recent_ordering(self, tmp_path):
        """recent() returns newest entries first."""
        log = ErrorLog(tmp_path / "errors.db")
        log.append(method="GET", path="/a", status_code=500, detail="first")
        log.append(method="GET", path="/b", status_code=500, detail="second")
        entries = log.recent()
        assert len(entries) == 2
        assert entries[0]["detail"] == "second"
        assert entries[1]["detail"] == "first"

    def test_recent_limit(self, tmp_path):
        """recent(limit=N) returns at most N entries."""
        log = ErrorLog(tmp_path / "errors.db")
        for i in range(10):
            log.append(method="GET", path=f"/{i}", status_code=500)
        entries = log.recent(limit=3)
        assert len(entries) == 3

    def test_auto_creates_db_and_table(self, tmp_path):
        """ErrorLog auto-creates the database file and table on first write."""
        db_path = tmp_path / "sub" / "errors.db"
        db_path.parent.mkdir(parents=True)
        log = ErrorLog(db_path)
        log.append(method="GET", path="/test", status_code=502)
        log.flush()  # writes are offloaded to a background thread; wait for them
        assert db_path.exists()
        # Verify table exists
        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute("SELECT count(*) FROM errors")
        assert cursor.fetchone()[0] == 1
        conn.close()

    def test_defaults_for_optional_fields(self, tmp_path):
        """Optional fields default to empty string."""
        log = ErrorLog(tmp_path / "errors.db")
        log.append(method="GET", path="/test", status_code=503)
        entries = log.recent()
        assert entries[0]["detail"] == ""
        assert entries[0]["request_id"] == ""
        assert entries[0]["user"] == ""
        assert entries[0]["traceback"] == ""

    def test_thread_safety(self, tmp_path):
        """Multiple threads can write to the same ErrorLog concurrently."""
        import concurrent.futures

        log = ErrorLog(tmp_path / "errors.db")

        def write(i: int):
            log.append(method="GET", path=f"/{i}", status_code=500, detail=f"error-{i}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(write, range(20)))

        entries = log.recent(limit=100)
        assert len(entries) == 20

    def test_append_offloads_write_and_does_not_block_caller(self, tmp_path):
        """append() offloads the SQLite write to a background worker thread.

        Regression: append() used to connect + INSERT + commit synchronously
        on the calling thread (the event loop), blocking every request during
        5xx bursts. The write must now happen off-thread, so append() returns
        immediately even when the underlying commit is slow or blocked.
        """
        log = ErrorLog(tmp_path / "errors.db")

        release = threading.Event()
        insert_threads: list = []
        real_connect = sqlite3.connect

        class _GatedConn(sqlite3.Connection):
            """Connection whose INSERT commit blocks until ``release`` is set."""

            _pending_insert = False

            def execute(self, sql, *args, **kwargs):
                if isinstance(sql, str) and sql.lstrip().upper().startswith("INSERT"):
                    self._pending_insert = True
                return super().execute(sql, *args, **kwargs)

            def commit(self):
                if self._pending_insert:
                    insert_threads.append(threading.current_thread())
                    release.wait(5)
                    self._pending_insert = False
                return super().commit()

        def gated_connect(*args, **kwargs):
            kwargs["factory"] = _GatedConn
            return real_connect(*args, **kwargs)

        append_thread: dict = {}
        done = threading.Event()

        def do_append():
            append_thread["t"] = threading.current_thread()
            log.append(method="GET", path="/boom", status_code=500)
            done.set()

        with patch("fastware.error_log.sqlite3.connect", gated_connect):
            t = threading.Thread(target=do_append, daemon=True)
            t.start()
            # append() must return promptly even though the worker's INSERT
            # commit is gated (blocked) below. If append blocks, this times out.
            assert done.wait(2), "append() blocked on the DB write (not offloaded)"
            release.set()
            t.join(5)

        entries = log.recent()
        assert len(entries) == 1
        assert entries[0]["path"] == "/boom"
        # The write executed on a background worker thread, not the caller.
        assert insert_threads
        assert all(th is not append_thread["t"] for th in insert_threads)
