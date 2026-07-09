"""Tests for follow-up fixes.

Covers:
- WebSocket.receive_raw() returns raw ASGI message dict
- Broadcaster heartbeat_interval yields SSE heartbeat comments
- Recursive Pydantic model serialization in handler responses
- AppConfig and create_app configuration
- serve(reload=True) requires foreground=True and invokes watchfiles
- MCP module -- role definitions and server creation
- query_list coercion failure
- Cache Request.state
"""

from __future__ import annotations

import asyncio
import json
import socket
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel

from fastware import (
    AppConfig,
    JSONResponse,
    Router,
    WebSocket,
    create_app,
)
from fastware.server import serve
from fastware.sse import Broadcaster


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
# WebSocket.receive_raw()
# ---------------------------------------------------------------------------


class TestWebSocketReceiveRaw:
    @pytest.mark.anyio
    async def test_receive_raw_returns_raw_dict(self):
        """receive_raw() returns the raw ASGI message dict with type/bytes/text keys."""
        router = Router()
        captured_msg = {}

        @router.ws("/ws/raw")
        async def handler(ws):
            await ws.accept()
            msg = await ws.receive_raw()
            captured_msg.update(msg)

        app = create_app(router)
        scope, receive, send, recv_q, sent = await _ws_connect(app, "/ws/raw")
        await recv_q.put({
            "type": "websocket.receive",
            "bytes": b"\x00\x01\x02",
        })
        await app(scope, receive, send)

        assert captured_msg["type"] == "websocket.receive"
        assert captured_msg["bytes"] == b"\x00\x01\x02"

    @pytest.mark.anyio
    async def test_receive_raw_text_frame(self):
        """receive_raw() returns text frames as-is."""
        router = Router()
        captured_msg = {}

        @router.ws("/ws/raw-text")
        async def handler(ws):
            await ws.accept()
            msg = await ws.receive_raw()
            captured_msg.update(msg)

        app = create_app(router)
        scope, receive, send, recv_q, sent = await _ws_connect(app, "/ws/raw-text")
        await recv_q.put({
            "type": "websocket.receive",
            "text": '{"type": "resize", "rows": 24, "cols": 80}',
        })
        await app(scope, receive, send)

        assert captured_msg["type"] == "websocket.receive"
        assert "text" in captured_msg
        parsed = json.loads(captured_msg["text"])
        assert parsed["type"] == "resize"

    @pytest.mark.anyio
    async def test_receive_raw_disconnect(self):
        """receive_raw() returns disconnect messages."""
        router = Router()
        captured_msg = {}

        @router.ws("/ws/raw-dc")
        async def handler(ws):
            await ws.accept()
            msg = await ws.receive_raw()
            captured_msg.update(msg)

        app = create_app(router)
        scope, receive, send, recv_q, sent = await _ws_connect(app, "/ws/raw-dc")
        await recv_q.put({"type": "websocket.disconnect"})
        await app(scope, receive, send)

        assert captured_msg["type"] == "websocket.disconnect"


# ---------------------------------------------------------------------------
# Broadcaster heartbeat_interval
# ---------------------------------------------------------------------------


class TestBroadcasterHeartbeat:
    @pytest.mark.anyio
    async def test_heartbeat_emitted_on_timeout(self):
        """With heartbeat_interval set, a heartbeat comment is yielded on timeout."""
        b = Broadcaster(strict=False, heartbeat_interval=0.05)
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=256)
        b._clients.append(queue)

        gen = b._event_generator(queue)
        # No messages in the queue, so it should timeout and yield heartbeat
        msg = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        assert msg == ": heartbeat\n\n"

        # Clean up
        await gen.aclose()

    @pytest.mark.anyio
    async def test_real_message_before_heartbeat(self):
        """With heartbeat_interval, real messages are yielded before timeout."""
        b = Broadcaster(strict=False, heartbeat_interval=5.0)
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=256)
        b._clients.append(queue)

        # Put a real message
        queue.put_nowait("event: ping\ndata: {}\n\n")

        gen = b._event_generator(queue)
        msg = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        assert msg == "event: ping\ndata: {}\n\n"

        await gen.aclose()

    @pytest.mark.anyio
    async def test_no_heartbeat_without_interval(self):
        """Without heartbeat_interval, generator blocks and yields nothing."""
        b = Broadcaster(strict=False)
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=256)
        b._clients.append(queue)

        gen = b._event_generator(queue)
        yielded = []

        async def collect():
            async for msg in gen:
                yielded.append(msg)

        task = asyncio.create_task(collect())
        await asyncio.sleep(0.15)
        # After 150ms with no messages, nothing should have been yielded
        assert yielded == []
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    @pytest.mark.anyio
    async def test_multiple_heartbeats(self):
        """Multiple heartbeats are emitted while idle."""
        b = Broadcaster(strict=False, heartbeat_interval=0.03)
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=256)
        b._clients.append(queue)

        gen = b._event_generator(queue)
        msg1 = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        msg2 = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        assert msg1 == ": heartbeat\n\n"
        assert msg2 == ": heartbeat\n\n"

        await gen.aclose()


# ---------------------------------------------------------------------------
# Recursive Pydantic serialization
# ---------------------------------------------------------------------------


class UserModel(BaseModel):
    name: str
    age: int


class AddressModel(BaseModel):
    city: str
    zip: str


class TestRecursivePydanticSerialization:
    @pytest.mark.anyio
    async def test_dict_with_nested_pydantic_model(self, client_for):
        """Handler returns dict containing a Pydantic model -> fully serialized."""
        router = Router()

        @router.get("/api/nested")
        async def nested(req):
            return {"user": UserModel(name="Alice", age=30), "extra": "data"}

        app = create_app(router)
        async with client_for(app) as client:
            resp = await client.get("/api/nested")
        assert resp.status_code == 200
        data = resp.json()
        assert data["user"] == {"name": "Alice", "age": 30}
        assert data["extra"] == "data"

    @pytest.mark.anyio
    async def test_list_of_pydantic_models(self, client_for):
        """Handler returns list of Pydantic models -> all serialized."""
        router = Router()

        @router.get("/api/users")
        async def users(req):
            return [
                UserModel(name="Alice", age=30),
                UserModel(name="Bob", age=25),
            ]

        app = create_app(router)
        async with client_for(app) as client:
            resp = await client.get("/api/users")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0] == {"name": "Alice", "age": 30}
        assert data[1] == {"name": "Bob", "age": 25}

    @pytest.mark.anyio
    async def test_deeply_nested_pydantic(self, client_for):
        """Dict with nested dict containing Pydantic model -> recursively serialized."""
        router = Router()

        @router.get("/api/deep")
        async def deep(req):
            return {
                "result": {
                    "user": UserModel(name="Carol", age=40),
                    "address": AddressModel(city="Berlin", zip="10115"),
                },
            }

        app = create_app(router)
        async with client_for(app) as client:
            resp = await client.get("/api/deep")
        assert resp.status_code == 200
        data = resp.json()
        assert data["result"]["user"] == {"name": "Carol", "age": 40}
        assert data["result"]["address"] == {"city": "Berlin", "zip": "10115"}

    @pytest.mark.anyio
    async def test_list_in_dict_with_pydantic(self, client_for):
        """Dict containing a list of Pydantic models -> serialized."""
        router = Router()

        @router.get("/api/team")
        async def team(req):
            return {
                "team": [
                    UserModel(name="Alice", age=30),
                    UserModel(name="Bob", age=25),
                ],
                "count": 2,
            }

        app = create_app(router)
        async with client_for(app) as client:
            resp = await client.get("/api/team")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2
        assert data["team"][0]["name"] == "Alice"
        assert data["team"][1]["name"] == "Bob"

    @pytest.mark.anyio
    async def test_plain_dict_unchanged(self, client_for):
        """Dict without Pydantic models passes through unchanged."""
        router = Router()

        @router.get("/api/plain")
        async def plain(req):
            return {"key": "value", "num": 42}

        app = create_app(router)
        async with client_for(app) as client:
            resp = await client.get("/api/plain")
        assert resp.status_code == 200
        assert resp.json() == {"key": "value", "num": 42}


# ---------------------------------------------------------------------------
# AppConfig and create_app configuration
# ---------------------------------------------------------------------------


class TestAppConfig:
    @pytest.mark.anyio
    async def test_create_app_no_config(self, client_for):
        """create_app(router) still works without config or kwargs."""
        router = Router()

        @router.get("/api/ping")
        async def ping(req):
            return {"ok": True}

        app = create_app(router)
        async with client_for(app) as client:
            resp = await client.get("/api/ping")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    @pytest.mark.anyio
    async def test_create_app_with_config(self, client_for):
        """create_app(router, AppConfig(cors_origins=...)) works."""
        router = Router()

        @router.get("/api/ping")
        async def ping(req):
            return {"ok": True}

        cfg = AppConfig(cors_origins=["http://localhost"])
        app = create_app(router, config=cfg)
        async with client_for(app) as client:
            resp = await client.get(
                "/api/ping",
                headers={"origin": "http://localhost"},
            )
        assert resp.status_code == 200
        # CORS header is present when Origin matches an allowed origin
        assert resp.headers.get("access-control-allow-origin") == "http://localhost"

    @pytest.mark.anyio
    async def test_create_app_kwargs_still_work(self, client_for):
        """create_app(router, cors_origins=...) still works (backward compat)."""
        router = Router()

        @router.get("/api/ping")
        async def ping(req):
            return {"ok": True}

        app = create_app(router, cors_origins=["http://localhost"])
        async with client_for(app) as client:
            resp = await client.get(
                "/api/ping",
                headers={"origin": "http://localhost"},
            )
        assert resp.status_code == 200
        assert resp.headers.get("access-control-allow-origin") == "http://localhost"

    @pytest.mark.anyio
    async def test_kwargs_override_config(self, client_for):
        """Keyword arguments override matching config fields."""
        router = Router()

        @router.get("/api/ping")
        async def ping(req):
            return {"ok": True}

        # Config says no request_id, kwarg overrides to True
        cfg = AppConfig(request_id=False)
        app = create_app(router, config=cfg, request_id=True)
        async with client_for(app) as client:
            resp = await client.get("/api/ping")
        assert resp.status_code == 200
        # RequestIDMiddleware adds x-request-id to the response header
        assert resp.headers.get("x-request-id") is not None

    @pytest.mark.anyio
    async def test_config_request_id_false_no_header(self, client_for):
        """AppConfig(request_id=False) suppresses the x-request-id header."""
        router = Router()

        @router.get("/api/ping")
        async def ping(req):
            return {"ok": True}

        cfg = AppConfig(request_id=False, request_timing=False)
        app = create_app(router, config=cfg)
        async with client_for(app) as client:
            resp = await client.get("/api/ping")
        assert resp.status_code == 200
        assert resp.headers.get("x-request-id") is None

    def test_unknown_kwarg_raises_type_error(self):
        """Passing an unknown keyword argument raises TypeError."""
        router = Router()
        with pytest.raises(TypeError, match="unexpected keyword argument"):
            create_app(router, bogus_param=True)

    def test_appconfig_importable_from_fastware(self):
        """AppConfig is importable from the top-level fastware package."""
        from fastware import AppConfig as AC
        assert AC is AppConfig


# ---------------------------------------------------------------------------
# serve(reload=True) -- auto-restart on .py file changes
# ---------------------------------------------------------------------------


class TestServeReload:
    def test_reload_requires_foreground(self):
        """reload=True with foreground=False raises ValueError."""
        with pytest.raises(ValueError, match="reload requires foreground=True"):
            serve(
                "myapp:app",
                foreground=False,
                reload=True,
                host="127.0.0.1",
                port=9999,
            )

    @patch("fastware.server.ensure_port_available")
    def test_reload_calls_run_process(self, mock_port):
        """reload=True invokes watchfiles.run_process with correct args."""
        mock_port.return_value = 9999
        mock_run = MagicMock(return_value=0)

        # Mock watchfiles in sys.modules so the lazy import inside serve()
        # picks it up without needing importlib.reload (which poisons later
        # tests by replacing class objects on the shared module).
        mock_wf = MagicMock(run_process=mock_run, PythonFilter=MagicMock)
        with patch.dict("sys.modules", {"watchfiles": mock_wf}):
            serve(
                "myapp:app",
                foreground=True,
                reload=True,
                host="127.0.0.1",
                port=9999,
            )

        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args
        # First positional arg is the watch path
        assert call_kwargs[0][0] == "."
        # target is _run_server
        assert call_kwargs[1]["target"].__name__ == "_run_server"
        # args pass the target, host, port, and (for string targets) no shim dir
        assert call_kwargs[1]["args"] == (
            "myapp:app", "127.0.0.1", 9999, None, "asyncio", 1,
        )

    @patch("fastware.server._make_embed_server")
    def test_reload_false_does_not_import_watchfiles(self, mock_make_embed):
        """reload=False (default) does not touch watchfiles."""
        mock_embed = MagicMock()
        mock_embed.serve = AsyncMock()
        mock_make_embed.return_value = mock_embed

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            free_port = s.getsockname()[1]

        async def my_app(scope, receive, send):
            pass

        # Should work fine without watchfiles installed
        url = serve(my_app, foreground=False, host="127.0.0.1", port=free_port)
        assert url == f"http://127.0.0.1:{free_port}"
        mock_make_embed.assert_called_once()


# ---------------------------------------------------------------------------
# MCP module -- role-agnostic tool registration and server creation
# ---------------------------------------------------------------------------

# The framework ships no built-in role model: tests define their own.
_TEST_ROLES = {
    "reader": {"level": "read-only", "tools": ["read_file"]},
    "writer": {"level": "read-write", "tools": ["read_file", "write_file"]},
}


def _fake_tools_module():
    from types import ModuleType

    mod = ModuleType("fake_tools")
    mod.TOOLS = {
        "read_file": lambda: "read",
        "write_file": lambda: "write",
        "git_commit": lambda: "commit",
    }
    return mod


class _FakeServer:
    def __init__(self):
        self.registered = {}

    def add_tool(self, fn, *, name):
        self.registered[name] = fn


class TestMCPModule:
    def test_no_builtin_role_model(self):
        """fastware.mcp ships no domain role model (moved to consumers)."""
        import fastware.mcp as mcp_mod

        assert not hasattr(mcp_mod, "ROLES")
        assert not hasattr(mcp_mod, "DEFAULT_ROLE")
        assert "ROLES" not in mcp_mod.__all__
        assert "DEFAULT_ROLE" not in mcp_mod.__all__

    def test_import_does_not_load_mcp_package(self):
        """importing fastware.mcp must not import the heavy optional mcp package."""
        import subprocess
        import sys

        code = (
            "import fastware.mcp, sys; "
            "assert 'mcp' not in sys.modules, 'mcp package was imported eagerly'"
        )
        subprocess.run([sys.executable, "-c", code], check=True)

    def test_mcp_available_flag_reflects_installation(self):
        """_MCP_AVAILABLE is True here (mcp installed in the test env)."""
        import fastware.mcp as mcp_mod

        assert mcp_mod._MCP_AVAILABLE is True

    def test_create_mcp_server_without_mcp_package(self, monkeypatch):
        """create_mcp_server raises RuntimeError when mcp is not installed."""
        import fastware.mcp as mcp_mod

        monkeypatch.setattr(mcp_mod, "_MCP_AVAILABLE", False)
        with pytest.raises(RuntimeError, match="mcp.*package.*required"):
            mcp_mod.create_mcp_server()

    def test_register_tools_for_role_filters_correctly(self):
        """register_tools_for_role only registers tools allowed by the role."""
        from fastware.mcp import register_tools_for_role

        mod = _fake_tools_module()
        server = _FakeServer()

        result = register_tools_for_role(
            server, "reader", [mod], roles=_TEST_ROLES
        )
        assert "read_file" in result
        assert "write_file" not in result
        assert "git_commit" not in result
        assert "read_file" in server.registered
        assert "write_file" not in server.registered

    def test_register_tools_for_role_requires_roles(self):
        """roles is a required keyword argument -- no built-in default."""
        from fastware.mcp import register_tools_for_role

        with pytest.raises(TypeError):
            register_tools_for_role(_FakeServer(), "reader", [_fake_tools_module()])

    def test_register_tools_unknown_role_with_default_falls_back(self):
        """Unknown role falls back to the caller-supplied default_role."""
        from fastware.mcp import register_tools_for_role

        result = register_tools_for_role(
            _FakeServer(),
            "nonexistent_role",
            [_fake_tools_module()],
            roles=_TEST_ROLES,
            default_role="reader",
        )
        assert set(result) == {"read_file"}

    def test_register_tools_unknown_role_without_default_is_error(self):
        """Unknown role with no default_role is a hard error, not a fallback."""
        from fastware.mcp import register_tools_for_role

        with pytest.raises(KeyError, match="nonexistent_role"):
            register_tools_for_role(
                _FakeServer(),
                "nonexistent_role",
                [_fake_tools_module()],
                roles=_TEST_ROLES,
            )

    def test_create_mcp_server_requires_roles_with_tool_modules(self):
        """tool_modules without a roles mapping is a hard error."""
        from fastware.mcp import create_mcp_server

        with pytest.raises(ValueError, match="roles"):
            create_mcp_server(tool_modules=[_fake_tools_module()])

    def test_create_mcp_server_requires_role_or_default(self):
        """tool_modules with roles but neither role nor default_role is a hard error."""
        from fastware.mcp import create_mcp_server

        with pytest.raises(ValueError, match="role"):
            create_mcp_server(
                tool_modules=[_fake_tools_module()], roles=_TEST_ROLES
            )

    def test_create_mcp_server_registers_role_tools(self):
        """create_mcp_server wires role-filtered tools onto a real FastMCP."""
        from fastware.mcp import create_mcp_server

        server = create_mcp_server(
            "test-server",
            role="reader",
            tool_modules=[_fake_tools_module()],
            roles=_TEST_ROLES,
        )
        # FastMCP exposes registered tools via its tool manager.
        tool_names = {t.name for t in server._tool_manager.list_tools()}
        assert tool_names == {"read_file"}

    def test_mcp_exports_from_top_level(self):
        """Only the factory functions are exported from fastware.mcp."""
        import fastware.mcp as mcp_mod

        assert sorted(mcp_mod.__all__) == [
            "create_mcp_server",
            "register_tools_for_role",
        ]
        assert callable(mcp_mod.create_mcp_server)
        assert callable(mcp_mod.register_tools_for_role)


# ---------------------------------------------------------------------------
# query_list coercion failure
# ---------------------------------------------------------------------------


class TestQueryListCoercion:
    @pytest.mark.anyio
    async def test_query_list_invalid_type_returns_422(self, client_for):
        """query_list with unconvertible values raises HTTPError(422)."""
        router = Router()

        @router.get("/api/nums")
        async def nums(req):
            values = req.query_list("n", type_=int)
            return {"values": values}

        app = create_app(router)
        async with client_for(app) as client:
            resp = await client.get("/api/nums?n=abc&n=2")
        assert resp.status_code == 422
        assert "cannot convert" in resp.json()["detail"]

    @pytest.mark.anyio
    async def test_query_list_valid_type_works(self, client_for):
        """query_list with valid convertible values returns the list."""
        router = Router()

        @router.get("/api/nums")
        async def nums(req):
            values = req.query_list("n", type_=int)
            return {"values": values}

        app = create_app(router)
        async with client_for(app) as client:
            resp = await client.get("/api/nums?n=1&n=2&n=3")
        assert resp.status_code == 200
        assert resp.json()["values"] == [1, 2, 3]

    @pytest.mark.anyio
    async def test_query_list_absent_key_returns_empty(self, client_for):
        """query_list for an absent key returns an empty list."""
        router = Router()

        @router.get("/api/nums")
        async def nums(req):
            values = req.query_list("n", type_=int)
            return {"values": values}

        app = create_app(router)
        async with client_for(app) as client:
            resp = await client.get("/api/nums")
        assert resp.status_code == 200
        assert resp.json()["values"] == []


# ---------------------------------------------------------------------------
# Cache Request.state
# ---------------------------------------------------------------------------


class TestRequestStateCaching:
    def test_state_identity(self):
        """req.state is req.state -- the same object on repeated access."""
        from fastware import Request

        scope = {"state": {"key": "value"}}
        req = Request(scope, {}, None)
        assert req.state is req.state

    def test_state_preserves_mutations(self):
        """Mutations on req.state persist across accesses."""
        from fastware import Request

        scope = {"state": {}}
        req = Request(scope, {}, None)
        req.state.foo = "bar"
        assert req.state.foo == "bar"
