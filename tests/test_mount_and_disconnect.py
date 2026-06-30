"""Tests for ASGI sub-app mounting and WebSocket disconnect detection.

Covers:
- Router.mount() registers a prefix and forwards to sub-apps
- Path rewriting (strip prefix, extend root_path)
- Exact-prefix requests get path="/"
- Non-mounted paths fall through to normal routing
- Mounted sub-apps work with websocket scope type
- WebSocketDisconnect raised on receive_json/receive_text/receive_bytes
"""

from __future__ import annotations

import asyncio
import json

import pytest

from fastware import (
    JSONResponse,
    Router,
    WebSocket,
    WebSocketDisconnect,
    create_app,
)
from httpx import ASGITransport, AsyncClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _ws_connect(app, path, headers=None, query_string=b""):
    """Simulate a WebSocket connection via raw ASGI scope/receive/send."""
    scope = {
        "type": "websocket",
        "path": path,
        "headers": headers or [],
        "query_string": query_string,
    }
    receive_queue: asyncio.Queue = asyncio.Queue()
    sent_messages: list[dict] = []

    await receive_queue.put({"type": "websocket.connect"})

    async def receive():
        return await receive_queue.get()

    async def send(msg):
        sent_messages.append(msg)

    return scope, receive, send, receive_queue, sent_messages


# ---------------------------------------------------------------------------
# Sub-app mounting (HTTP)
# ---------------------------------------------------------------------------


class TestMountHTTP:
    @pytest.mark.anyio
    async def test_mount_forwards_to_sub_app(self):
        """Requests to /sub/hello are forwarded to the sub-app with path=/hello."""
        captured_scopes: list[dict] = []

        async def sub_app(scope, receive, send):
            captured_scopes.append(dict(scope))
            await send({"type": "http.response.start", "status": 200, "headers": [[b"content-type", b"text/plain"]]})
            await send({"type": "http.response.body", "body": b"sub ok"})

        router = Router()
        router.mount("/sub", sub_app)
        app = create_app(router)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/sub/hello")
        assert resp.status_code == 200
        assert resp.text == "sub ok"
        assert captured_scopes[0]["path"] == "/hello"
        assert captured_scopes[0]["root_path"].endswith("/sub")

    @pytest.mark.anyio
    async def test_mount_exact_prefix_gets_slash(self):
        """A request to exactly /sub (the prefix) forwards with path='/'."""
        captured_scopes: list[dict] = []

        async def sub_app(scope, receive, send):
            captured_scopes.append(dict(scope))
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"root"})

        router = Router()
        router.mount("/sub", sub_app)
        app = create_app(router)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/sub")
        assert resp.status_code == 200
        assert captured_scopes[0]["path"] == "/"

    @pytest.mark.anyio
    async def test_non_mounted_path_uses_normal_routing(self):
        """Requests to /other go through normal routing, not the mounted sub-app."""
        router = Router()

        @router.get("/other")
        async def handler(req):
            return JSONResponse({"source": "main"})

        async def sub_app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"sub"})

        router.mount("/sub", sub_app)
        app = create_app(router)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/other")
        assert resp.status_code == 200
        assert resp.json() == {"source": "main"}

    @pytest.mark.anyio
    async def test_mount_prefix_normalized_trailing_slash(self):
        """Trailing slash on prefix is stripped: mount('/sub/') works like mount('/sub')."""
        captured_scopes: list[dict] = []

        async def sub_app(scope, receive, send):
            captured_scopes.append(dict(scope))
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        router = Router()
        router.mount("/sub/", sub_app)
        app = create_app(router)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/sub/hello")
        assert resp.status_code == 200
        assert captured_scopes[0]["path"] == "/hello"

    @pytest.mark.anyio
    async def test_mount_deep_path(self):
        """Mounting at /api/v2 correctly strips the full prefix."""
        captured_scopes: list[dict] = []

        async def sub_app(scope, receive, send):
            captured_scopes.append(dict(scope))
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"deep"})

        router = Router()
        router.mount("/api/v2", sub_app)
        app = create_app(router)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/api/v2/users/42")
        assert resp.status_code == 200
        assert captured_scopes[0]["path"] == "/users/42"
        assert captured_scopes[0]["root_path"].endswith("/api/v2")

    @pytest.mark.anyio
    async def test_mount_does_not_match_partial_prefix(self):
        """/subpath should not match a mount at /sub."""
        router = Router()

        @router.get("/subpath")
        async def handler(req):
            return JSONResponse({"matched": "main"})

        async def sub_app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"sub"})

        router.mount("/sub", sub_app)
        app = create_app(router)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/subpath")
        assert resp.status_code == 200
        assert resp.json() == {"matched": "main"}


# ---------------------------------------------------------------------------
# Sub-app mounting (WebSocket)
# ---------------------------------------------------------------------------


class TestMountWebSocket:
    @pytest.mark.anyio
    async def test_mount_with_websocket_scope(self):
        """Mounted sub-apps receive forwarded websocket scope with rewritten path."""
        captured_scopes: list[dict] = []

        async def sub_app(scope, receive, send):
            captured_scopes.append(dict(scope))
            # Minimal websocket handshake: accept then close
            msg = await receive()  # websocket.connect
            await send({"type": "websocket.accept"})
            await send({"type": "websocket.close", "code": 1000})

        router = Router()
        router.mount("/ws-sub", sub_app)
        app = create_app(router)

        scope, receive, send, recv_q, sent = await _ws_connect(app, "/ws-sub/chat")
        await app(scope, receive, send)

        assert captured_scopes[0]["type"] == "websocket"
        assert captured_scopes[0]["path"] == "/chat"
        assert captured_scopes[0]["root_path"].endswith("/ws-sub")


# ---------------------------------------------------------------------------
# WebSocket disconnect detection
# ---------------------------------------------------------------------------


class TestWebSocketDisconnect:
    @pytest.mark.anyio
    async def test_receive_json_raises_on_disconnect(self):
        """receive_json() raises WebSocketDisconnect when client disconnects."""
        router = Router()
        exc_raised = {}

        @router.ws("/ws/json-dc")
        async def handler(ws):
            await ws.accept()
            try:
                await ws.receive_json()
            except WebSocketDisconnect as e:
                exc_raised["code"] = e.code

        app = create_app(router)
        scope, receive, send, recv_q, sent = await _ws_connect(app, "/ws/json-dc")
        await recv_q.put({"type": "websocket.disconnect", "code": 1001})
        await app(scope, receive, send)

        assert exc_raised["code"] == 1001

    @pytest.mark.anyio
    async def test_receive_text_raises_on_disconnect(self):
        """receive_text() raises WebSocketDisconnect when client disconnects."""
        router = Router()
        exc_raised = {}

        @router.ws("/ws/text-dc")
        async def handler(ws):
            await ws.accept()
            try:
                await ws.receive_text()
            except WebSocketDisconnect as e:
                exc_raised["code"] = e.code

        app = create_app(router)
        scope, receive, send, recv_q, sent = await _ws_connect(app, "/ws/text-dc")
        await recv_q.put({"type": "websocket.disconnect", "code": 1006})
        await app(scope, receive, send)

        assert exc_raised["code"] == 1006

    @pytest.mark.anyio
    async def test_receive_bytes_raises_on_disconnect(self):
        """receive_bytes() raises WebSocketDisconnect when client disconnects."""
        router = Router()
        exc_raised = {}

        @router.ws("/ws/bytes-dc")
        async def handler(ws):
            await ws.accept()
            try:
                await ws.receive_bytes()
            except WebSocketDisconnect as e:
                exc_raised["code"] = e.code

        app = create_app(router)
        scope, receive, send, recv_q, sent = await _ws_connect(app, "/ws/bytes-dc")
        await recv_q.put({"type": "websocket.disconnect"})
        await app(scope, receive, send)

        # Default code is 1000 when not specified
        assert exc_raised["code"] == 1000

    @pytest.mark.anyio
    async def test_receive_raw_does_not_raise_on_disconnect(self):
        """receive_raw() returns the disconnect message without raising."""
        router = Router()
        captured_msg = {}

        @router.ws("/ws/raw-no-raise")
        async def handler(ws):
            await ws.accept()
            msg = await ws.receive_raw()
            captured_msg.update(msg)

        app = create_app(router)
        scope, receive, send, recv_q, sent = await _ws_connect(app, "/ws/raw-no-raise")
        await recv_q.put({"type": "websocket.disconnect", "code": 1000})
        await app(scope, receive, send)

        assert captured_msg["type"] == "websocket.disconnect"

    @pytest.mark.anyio
    async def test_receive_json_works_normally(self):
        """receive_json() still works for normal data messages."""
        router = Router()
        captured_data = {}

        @router.ws("/ws/json-ok")
        async def handler(ws):
            await ws.accept()
            data = await ws.receive_json()
            captured_data.update(data)

        app = create_app(router)
        scope, receive, send, recv_q, sent = await _ws_connect(app, "/ws/json-ok")
        await recv_q.put({"type": "websocket.receive", "text": json.dumps({"hello": "world"})})
        await app(scope, receive, send)

        assert captured_data == {"hello": "world"}

    def test_websocket_disconnect_importable_from_fastware(self):
        """WebSocketDisconnect is importable from the top-level package."""
        from fastware import WebSocketDisconnect as WSD
        assert WSD is WebSocketDisconnect

    def test_websocket_disconnect_exception_attributes(self):
        """WebSocketDisconnect has the expected code attribute and message."""
        exc = WebSocketDisconnect(code=1001)
        assert exc.code == 1001
        assert "1001" in str(exc)

        exc_default = WebSocketDisconnect()
        assert exc_default.code == 1000
