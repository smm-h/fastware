"""Streaming-disconnect correctness (Phase 10).

Covers the disconnect watcher that owns the ASGI receive channel for a
request's post-body lifetime, the child-task restructure of ``_send_stream``,
guaranteed ``generator.aclose()`` in every teardown path, DI-resource cleanup
ordering across a streaming body, SSE client pruning on disconnect, and
non-interaction with the WebSocket path.
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from fastware import Router, StreamResponse, create_app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _AcloseSpy:
    """Transparent async-generator proxy that counts ``aclose()`` calls.

    ``_send_stream`` iterates via ``__aiter__``/``__anext__`` and finalises via
    ``aclose()``; this proxy forwards both to the wrapped generator while
    recording how many times ``aclose`` runs, so tests can assert the
    close-in-all-paths guarantee.
    """

    def __init__(self, agen):
        self._agen = agen
        self.aclose_calls = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self._agen.__anext__()

    async def aclose(self):
        self.aclose_calls += 1
        return await self._agen.aclose()

    async def athrow(self, *args):
        return await self._agen.athrow(*args)


def _http_scope(path: str) -> dict:
    return {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": [],
        "query_string": b"",
    }


async def _drive_until_disconnect(app, path, started_event, timeout: float = 5.0):
    """Run *app* for a streaming GET, deliver ``http.disconnect`` once the
    stream has signalled *started_event*, and return the sent ASGI messages.

    A single receive queue models the ASGI channel: the body message is
    delivered first (consumed by the body-read loop), then the disconnect is
    delivered to the watcher after the stream is mid-flight.
    """
    recv_q: asyncio.Queue = asyncio.Queue()
    await recv_q.put({"type": "http.request", "body": b"", "more_body": False})
    sent: list[dict] = []

    async def receive():
        return await recv_q.get()

    async def send(msg):
        sent.append(msg)

    task = asyncio.create_task(app(_http_scope(path), receive, send))
    await asyncio.wait_for(started_event.wait(), timeout=timeout)
    await recv_q.put({"type": "http.disconnect"})
    await asyncio.wait_for(task, timeout=timeout)
    return sent


def _bodies(sent: list[dict]) -> bytes:
    return b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")


def _starts(sent: list[dict]) -> list[dict]:
    return [m for m in sent if m["type"] == "http.response.start"]


# ---------------------------------------------------------------------------
# Watcher-driven cancellation runs generator teardown
# ---------------------------------------------------------------------------


class TestDisconnectRunsTeardown:
    @pytest.mark.anyio
    async def test_disconnect_runs_generator_finally(self):
        """A generator blocked between yields is unwound on disconnect: the
        watcher cancels the stream task, the generator's finally runs, and the
        app returns cleanly with only one http.response.start."""
        router = Router()
        state = {"finally_ran": False}
        started = asyncio.Event()

        @router.get("/stream")
        async def stream(req):
            async def gen():
                try:
                    yield "chunk-1"
                    started.set()
                    while True:
                        await asyncio.sleep(3600)
                        yield "never"
                finally:
                    state["finally_ran"] = True

            return StreamResponse(gen(), content_type="text/event-stream")

        app = create_app(router, request_id=False, request_timing=False)
        sent = await _drive_until_disconnect(app, "/stream", started)

        assert state["finally_ran"] is True
        assert len(_starts(sent)) == 1
        assert b"chunk-1" in _bodies(sent)

    @pytest.mark.anyio
    async def test_cancelled_error_swallowing_generator_tears_down(self):
        """A generator that CATCHES CancelledError and returns normally (a real
        consumer pattern) must still tear down cleanly and have aclose run."""
        router = Router()
        state = {"finally_ran": False, "cancel_caught": False}
        started = asyncio.Event()
        holder: dict = {}

        @router.get("/stream")
        async def stream(req):
            async def gen():
                try:
                    yield "chunk-1"
                    started.set()
                    while True:
                        await asyncio.sleep(3600)
                        yield "never"
                except asyncio.CancelledError:
                    state["cancel_caught"] = True
                    return
                finally:
                    state["finally_ran"] = True

            spy = _AcloseSpy(gen())
            holder["spy"] = spy
            return StreamResponse(spy, content_type="text/event-stream")

        app = create_app(router, request_id=False, request_timing=False)
        sent = await _drive_until_disconnect(app, "/stream", started)

        assert state["cancel_caught"] is True
        assert state["finally_ran"] is True
        assert holder["spy"].aclose_calls == 1
        assert len(_starts(sent)) == 1

    @pytest.mark.anyio
    async def test_aclose_runs_on_disconnect_when_generator_propagates(self):
        """When the generator does NOT catch CancelledError, aclose is still
        guaranteed (previously aclose only ran on a send() failure)."""
        router = Router()
        started = asyncio.Event()
        holder: dict = {}

        @router.get("/stream")
        async def stream(req):
            async def gen():
                yield "one"
                started.set()
                while True:
                    await asyncio.sleep(3600)
                    yield "x"

            spy = _AcloseSpy(gen())
            holder["spy"] = spy
            return StreamResponse(spy, content_type="text/event-stream")

        app = create_app(router, request_id=False, request_timing=False)
        await _drive_until_disconnect(app, "/stream", started)

        assert holder["spy"].aclose_calls == 1


# ---------------------------------------------------------------------------
# Normal completion path stays clean
# ---------------------------------------------------------------------------


class TestNormalCompletion:
    @pytest.mark.anyio
    async def test_normal_completion_closes_generator_once(self):
        """On normal stream completion the generator is closed exactly once and
        the full body is delivered."""
        holder: dict = {}
        router = Router()

        @router.get("/stream")
        async def stream(req):
            async def gen():
                yield "one\n"
                yield "two\n"

            spy = _AcloseSpy(gen())
            holder["spy"] = spy
            return StreamResponse(spy, content_type="text/event-stream")

        app = create_app(router, request_id=False, request_timing=False)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/stream")

        assert resp.status_code == 200
        assert "one" in resp.text and "two" in resp.text
        assert holder["spy"].aclose_calls == 1

    @pytest.mark.anyio
    async def test_normal_completion_leaves_no_watcher_task(self):
        """After a completed streaming request no disconnect-watcher or stream
        task is left running (the request-level finally cancels the watcher)."""
        router = Router()

        @router.get("/stream")
        async def stream(req):
            async def gen():
                yield "a"
                yield "b"

            return StreamResponse(gen(), content_type="text/event-stream")

        app = create_app(router, request_id=False, request_timing=False)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/stream")
        assert resp.status_code == 200

        # Let any cancelled tasks settle, then verify nothing lingers.
        await asyncio.sleep(0.02)
        lingering = [
            t
            for t in asyncio.all_tasks()
            if t is not asyncio.current_task() and not t.done()
        ]
        names = " ".join(repr(t.get_coro()) for t in lingering)
        assert "_disconnect_watcher" not in names
        assert "_send_stream" not in names
        assert "_drive" not in names


# ---------------------------------------------------------------------------
# DI-provided resource lifetime across the stream
# ---------------------------------------------------------------------------


class TestStreamDependencyCleanup:
    @pytest.mark.anyio
    async def test_di_resource_alive_during_stream_closed_once_after_disconnect(self):
        """A generator DI resource stays alive for the whole streaming body and
        is closed exactly once, only after a disconnect-driven cancellation."""
        router = Router()
        events: list[str] = []
        started = asyncio.Event()

        def get_resource(req):
            events.append("open")
            try:
                yield "RESOURCE"
            finally:
                events.append("close")

        @router.get("/stream", deps={"res": get_resource})
        async def stream(req, res=None):
            async def gen():
                yield f"using-{res}"
                events.append("used")
                started.set()
                while True:
                    await asyncio.sleep(3600)
                    yield "tick"

            return StreamResponse(gen(), content_type="text/event-stream")

        app = create_app(router, request_id=False, request_timing=False)
        sent = await _drive_until_disconnect(app, "/stream", started)

        assert events.count("open") == 1
        assert events.count("close") == 1
        # Resource was used inside the stream body before it was torn down.
        assert events.index("used") < events.index("close")
        assert b"using-RESOURCE" in _bodies(sent)

    @pytest.mark.anyio
    async def test_di_resource_closed_once_after_normal_completion(self):
        """A generator DI resource is closed exactly once, after the stream
        body finishes on normal completion."""
        router = Router()
        events: list[str] = []

        def get_resource(req):
            events.append("open")
            try:
                yield "RES"
            finally:
                events.append("close")

        @router.get("/stream", deps={"res": get_resource})
        async def stream(req, res=None):
            async def gen():
                yield f"a-{res}\n"
                events.append("used")
                yield "b\n"

            return StreamResponse(gen(), content_type="text/event-stream")

        app = create_app(router, request_id=False, request_timing=False)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/stream")

        assert resp.status_code == 200
        assert "a-RES" in resp.text
        assert events.count("open") == 1
        assert events.count("close") == 1
        assert events.index("used") < events.index("close")


# ---------------------------------------------------------------------------
# SSE broadcaster prunes its client queue on mid-stream disconnect
# ---------------------------------------------------------------------------


class TestSSEBroadcasterDisconnect:
    @pytest.mark.anyio
    async def test_broadcaster_client_removed_on_mid_stream_disconnect(self):
        """A client's queue is unregistered when the stream is cancelled by a
        disconnect (the SSE generator's finally runs)."""
        from fastware.sse import Broadcaster

        broadcaster = Broadcaster()
        broadcaster.register_event("ping")

        router = Router()

        @router.get("/events")
        async def events(req):
            return await broadcaster.stream(req)

        app = create_app(router, request_id=False, request_timing=False)

        recv_q: asyncio.Queue = asyncio.Queue()
        await recv_q.put({"type": "http.request", "body": b"", "more_body": False})
        sent: list[dict] = []

        async def receive():
            return await recv_q.get()

        async def send(msg):
            sent.append(msg)

        task = asyncio.create_task(app(_http_scope("/events"), receive, send))

        # Wait until the SSE generator has registered its client queue.
        for _ in range(1000):
            if broadcaster.client_count == 1:
                break
            await asyncio.sleep(0.001)
        assert broadcaster.client_count == 1

        broadcaster.broadcast("ping", {"n": 1})
        await asyncio.sleep(0.01)

        # Client disconnects mid-stream.
        await recv_q.put({"type": "http.disconnect"})
        await asyncio.wait_for(task, timeout=5.0)

        assert broadcaster.client_count == 0
        assert b"event: ping" in _bodies(sent)


# ---------------------------------------------------------------------------
# WebSocket path is untouched by the HTTP disconnect watcher
# ---------------------------------------------------------------------------


class TestWebSocketUnaffected:
    @pytest.mark.anyio
    async def test_websocket_echo_still_works(self):
        """The WebSocket scope path creates no HTTP disconnect watcher; a normal
        echo handshake completes unchanged."""
        import json

        router = Router()
        got: dict = {}

        @router.ws("/ws/echo")
        async def echo(ws):
            await ws.accept()
            msg = await ws.receive_json()
            got["msg"] = msg
            await ws.send_json({"echo": msg})
            await ws.close()

        app = create_app(router, request_id=False, request_timing=False)

        step = 0
        sent: list[dict] = []

        async def receive():
            nonlocal step
            step += 1
            if step == 1:
                return {"type": "websocket.connect"}
            if step == 2:
                return {"type": "websocket.receive", "text": json.dumps({"hi": 1})}
            await asyncio.sleep(3600)

        async def send(msg):
            sent.append(msg)

        scope = {
            "type": "websocket",
            "path": "/ws/echo",
            "headers": [],
            "query_string": b"",
        }
        await asyncio.wait_for(app(scope, receive, send), timeout=5.0)

        assert got["msg"] == {"hi": 1}
        assert sent[0]["type"] == "websocket.accept"
        assert sent[-1]["type"] == "websocket.close"


# ---------------------------------------------------------------------------
# Generator error after start (no immediate disconnect) still cannot 500
# ---------------------------------------------------------------------------


class TestGeneratorErrorAfterStart:
    @pytest.mark.anyio
    async def test_generator_error_after_start_no_second_start(self):
        """A generator that raises after the response started (while the client
        is still connected) must not produce a second http.response.start."""
        router = Router()

        @router.get("/boom")
        async def boom(req):
            async def gen():
                yield "first"
                raise RuntimeError("generator bug")

            return StreamResponse(gen(), content_type="text/event-stream")

        app = create_app(router, request_id=False, request_timing=False)

        step = 0
        sent: list[dict] = []

        async def receive():
            nonlocal step
            step += 1
            if step == 1:
                return {"type": "http.request", "body": b"", "more_body": False}
            # Never disconnect: the watcher blocks here for the request lifetime.
            await asyncio.sleep(3600)

        async def send(msg):
            sent.append(msg)

        await asyncio.wait_for(app(_http_scope("/boom"), receive, send), timeout=5.0)

        assert len(_starts(sent)) == 1
        assert _starts(sent)[0]["status"] == 200
        assert b"first" in _bodies(sent)
