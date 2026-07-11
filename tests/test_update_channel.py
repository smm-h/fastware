"""Tests for the framework-owned update channel (Phase 12.2).

/__fastware/events is an SSE stream that primes each connection with the
current build id as its first "build" event. The page-side client.js compares
that id to its own and reloads on DIFFERENT.

These drive the ASGI app directly with a controlled receive queue (delivering
http.disconnect once the first event is out) rather than httpx streaming, which
would block forever on the never-ending SSE generator.
"""

from __future__ import annotations

import asyncio

import msgspec
import pytest

from fastware import Broadcaster, Router, create_app
from fastware.request import Request


def _http_scope(path: str) -> dict:
    return {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": [],
        "query_string": b"",
    }


async def _collect_sse_until_first_event(app, path: str, timeout: float = 5.0) -> list[dict]:
    """Drive *app* for a streaming GET; once a non-empty body chunk is sent,
    deliver http.disconnect and let the app unwind. Return the sent messages."""
    recv_q: asyncio.Queue = asyncio.Queue()
    await recv_q.put({"type": "http.request", "body": b"", "more_body": False})
    sent: list[dict] = []

    async def receive():
        return await recv_q.get()

    async def send(msg):
        sent.append(msg)

    task = asyncio.create_task(app(_http_scope(path), receive, send))

    async def _wait_for_body():
        while not any(
            m["type"] == "http.response.body" and m.get("body") for m in sent
        ):
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_wait_for_body(), timeout=timeout)
    await recv_q.put({"type": "http.disconnect"})
    await asyncio.wait_for(task, timeout=timeout)
    return sent


def _starts(sent: list[dict]) -> list[dict]:
    return [m for m in sent if m["type"] == "http.response.start"]


def _body_text(sent: list[dict]) -> str:
    return b"".join(
        m.get("body", b"") for m in sent if m["type"] == "http.response.body"
    ).decode()


@pytest.mark.anyio
async def test_events_endpoint_primes_current_build_id():
    app = create_app(Router(), name="myapp")
    sent = await _collect_sse_until_first_event(app, "/__fastware/events")

    # SSE content type.
    start = _starts(sent)[0]
    headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    assert "text/event-stream" in headers["content-type"]

    text = _body_text(sent)
    assert "event: build" in text
    # Extract the JSON data payload.
    data_line = next(
        line[len("data:"):].strip()
        for line in text.splitlines()
        if line.startswith("data:")
    )
    payload = msgspec.json.decode(data_line.encode())
    assert "build_id" in payload


@pytest.mark.anyio
async def test_events_build_id_matches_version_endpoint():
    from fastware.app import _compute_static_build_id

    app = create_app(Router())
    sent = await _collect_sse_until_first_event(app, "/__fastware/events")
    text = _body_text(sent)
    data_line = next(
        line[len("data:"):].strip()
        for line in text.splitlines()
        if line.startswith("data:")
    )
    payload = msgspec.json.decode(data_line.encode())
    # No static dir -> empty build id constant.
    assert payload["build_id"] == _compute_static_build_id(None)


# ---------------------------------------------------------------------------
# Broadcaster initial-event support (unit level)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_broadcaster_stream_initial_events():
    b = Broadcaster()
    b.register_event("build")
    req = Request(_http_scope("/"), {}, None)
    resp = await b.stream(req, initial=[("build", {"build_id": "abc"})])
    gen = resp.generator
    try:
        first = await asyncio.wait_for(gen.__anext__(), timeout=2)
    finally:
        await gen.aclose()
    assert "event: build" in first
    assert "abc" in first
