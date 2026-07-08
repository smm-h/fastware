"""End-to-end HTTP method-dispatch tests for the ASGI app.

Covers the two defects fixed in app.py:
- POST (or any non-matching method) to a path that only has other methods
  returns 405 Method Not Allowed with a correct ``Allow`` header, instead of
  falling through to static/SPA/404.
- HEAD to a GET route runs the GET handler but sends an empty body while
  preserving the GET response headers (including content-type).

Also guards against regressions for genuinely-unknown paths (still 404) and
existing OPTIONS behaviour.
"""

from __future__ import annotations

import pytest

from fastware import JSONResponse, Router, create_app


def _make_app():
    router = Router()

    @router.get("/api/thing")
    async def get_thing(req):
        return JSONResponse({"thing": True})

    @router.delete("/api/thing")
    async def delete_thing(req):
        return JSONResponse({"deleted": True})

    @router.post("/api/only-post")
    async def only_post(req):
        return JSONResponse({"posted": True})

    return create_app(router, request_id=False, request_timing=False)


@pytest.mark.anyio
async def test_post_to_get_only_route_returns_405(client_for):
    """POST to a path that only has GET/DELETE returns 405 with Allow header."""
    app = _make_app()
    async with client_for(app) as c:
        resp = await c.post("/api/thing", json={})
    assert resp.status_code == 405
    allow = resp.headers.get("allow")
    assert allow is not None
    methods = {m.strip() for m in allow.split(",")}
    assert methods == {"GET", "HEAD", "DELETE"}


@pytest.mark.anyio
async def test_get_to_post_only_route_returns_405(client_for):
    """GET to a POST-only route returns 405 listing POST (no GET/HEAD)."""
    app = _make_app()
    async with client_for(app) as c:
        resp = await c.get("/api/only-post")
    assert resp.status_code == 405
    methods = {m.strip() for m in resp.headers["allow"].split(",")}
    assert methods == {"POST"}


@pytest.mark.anyio
async def test_head_to_get_route_returns_empty_body(client_for):
    """HEAD to a GET route returns 200, empty body, GET content-type."""
    app = _make_app()
    async with client_for(app) as c:
        resp = await c.head("/api/thing")
    assert resp.status_code == 200
    assert resp.content == b""
    assert resp.headers["content-type"] == "application/json"


async def _drive(app, method: str, path: str):
    """Drive an ASGI app directly and capture the raw messages it sends.

    httpx's client discards HEAD response bodies, so an ASGI-level driver is
    needed to prove the app itself emits no entity body for HEAD requests.
    """
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [],
        "query_string": b"",
    }
    sent: list[dict] = []
    request_done = False

    async def receive():
        nonlocal request_done
        if not request_done:
            request_done = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    return sent


@pytest.mark.anyio
async def test_head_emits_no_entity_body_at_asgi_level(client_for):
    """The app must not transmit a non-empty body for a HEAD request.

    Proven at the ASGI layer: every http.response.body message carries an
    empty body, while the start message keeps the GET content-type header.
    """
    app = _make_app()
    sent = await _drive(app, "HEAD", "/api/thing")

    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 200
    header_map = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    assert header_map["content-type"] == "application/json"

    bodies = [m for m in sent if m["type"] == "http.response.body"]
    assert bodies, "expected at least one body message"
    assert all(m.get("body", b"") == b"" for m in bodies)


@pytest.mark.anyio
async def test_head_to_post_only_route_returns_405(client_for):
    """HEAD to a path with no GET route is a method mismatch -> 405."""
    app = _make_app()
    async with client_for(app) as c:
        resp = await c.head("/api/only-post")
    assert resp.status_code == 405
    methods = {m.strip() for m in resp.headers["allow"].split(",")}
    assert methods == {"POST"}


@pytest.mark.anyio
async def test_head_to_unknown_path_returns_404(client_for):
    """HEAD to a truly unknown path still 404s (no matching route)."""
    app = _make_app()
    async with client_for(app) as c:
        resp = await c.head("/api/nope")
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_unknown_path_still_404(client_for):
    """A genuinely-unmatched path still returns 404, not 405."""
    app = _make_app()
    async with client_for(app) as c:
        resp = await c.post("/api/nope", json={})
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_options_route_still_dispatches(client_for):
    """An explicitly-registered OPTIONS handler is still reached (unregressed)."""
    router = Router()

    @router.get("/api/thing")
    async def get_thing(req):
        return JSONResponse({"thing": True})

    async def opts(req):
        return JSONResponse({"options": True})

    router.add_route("OPTIONS", "/api/thing", opts)
    app = create_app(router, request_id=False, request_timing=False)
    async with client_for(app) as c:
        resp = await c.options("/api/thing")
    assert resp.status_code == 200
    assert resp.json() == {"options": True}


@pytest.mark.anyio
async def test_matched_get_unregressed(client_for):
    """A normal GET still succeeds after the 405/HEAD changes."""
    app = _make_app()
    async with client_for(app) as c:
        resp = await c.get("/api/thing")
    assert resp.status_code == 200
    assert resp.json() == {"thing": True}
