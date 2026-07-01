---
title: Migrating from FastAPI
description: "Step-by-step guide to migrating FastAPI apps to fastware: import mappings, route handler patterns, DI translation, and SSE migration."
date: 2026-07-01
---

# Migrating from FastAPI

This guide walks through converting a FastAPI application to fastware, covering import changes, pattern translations, and trade-offs.

## Why migrate?

- **Performance** -- fastware uses msgspec for JSON serialization (10-75x faster than Pydantic's default JSON encoding) and bundles Granian, a Rust-based ASGI server, eliminating the need for a separately configured uvicorn.
- **Batteries included** -- SSE broadcasting, dependency injection, middleware (CORS, request tracing, trusted host), test client, background tasks, and structured logging are all built in. No need for sse-starlette, python-multipart, or other third-party packages.
- **Server management** -- PID files, port availability checks, signal handling, and process group leadership are handled by the framework. Start, stop, and status-check your server programmatically.

## Import mapping

The table below maps 9 common FastAPI imports to their fastware equivalents:

| FastAPI | fastware |
|---|---|
| `from fastapi import FastAPI` | `from fastware import Router, create_app` |
| `from fastapi import Request` | `from fastware import Request` |
| `from fastapi.responses import JSONResponse` | `from fastware import JSONResponse` |
| `from fastapi.responses import HTMLResponse` | `from fastware import HTMLResponse` |
| `from fastapi.responses import StreamingResponse` | `from fastware import StreamResponse` |
| `from fastapi.responses import FileResponse` | `from fastware import FileResponse` |
| `from fastapi import Depends` | `from fastware import DependencyResolver` |
| `from starlette.middleware.cors import CORSMiddleware` | `from fastware.middleware import CORSMiddleware` |
| `from starlette.testclient import TestClient` | `from fastware.testing import TestClient` |

## Pattern translation

### App creation

In FastAPI, you create the application object directly and register routes on it. In fastware, routing and app construction are separate concerns, which makes the router testable independently and allows multiple routers to be composed before building the final ASGI app:

**FastAPI:**

```python
from fastapi import FastAPI

app = FastAPI(title="My App")
```

**fastware:**

```python
from fastware import Router, create_app, serve

router = Router()

# Register routes on router (see below)

app = create_app(router)

# Serve it
if __name__ == "__main__":
    serve(app, foreground=True, host="127.0.0.1", port=8000)
```

fastware separates routing (Router) from app construction (create_app). The Router collects route definitions; create_app wraps the router with middleware, static file serving, SPA fallback, and lifespan management.

### Route handlers

The biggest API difference between FastAPI and fastware is how route handlers access request data. FastAPI inspects handler signatures and injects parameters automatically; fastware passes an explicit `Request` object with typed accessor methods:

**FastAPI** -- signature-injected parameters:

```python
@app.get("/users/{user_id}")
async def get_user(user_id: int, q: str = None):
    return {"user_id": user_id, "q": q}
```

**fastware** -- explicit Request object:

```python
@router.get("/users/{user_id:int}")
async def get_user(request):
    user_id = request.path_params["user_id"]  # already an int
    q = request.query("q")                     # returns None if absent
    return {"user_id": user_id, "q": q}
```

Key differences:
- Path parameter types are declared in the route pattern: `{user_id:int}`, `{name:str}`, `{path:path}`.
- Query parameters are accessed via `request.query(name, default=..., type_=int)` with optional constraints (`ge`, `le`, `min_length`, `max_length`).
- The handler receives a single `Request` object. Body is accessed via `request.json` (parsed dict/list) or `request.body` (raw bytes).
- Return a plain dict or list and it gets auto-wrapped as a JSON response. Or return an explicit `JSONResponse`, `HTMLResponse`, etc.

### Middleware

FastAPI uses `add_middleware` calls after app creation, which means middleware ordering depends on call order. In fastware, all 5 built-in middleware classes (CORS, RequestID, RequestTiming, TrustedHost, ViteDevProxy) are configured declaratively via `AppConfig` fields, eliminating the imperative step and ensuring middleware ordering is deterministic regardless of configuration order:

**FastAPI:**

```python
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
)
```

**fastware:**

```python
from fastware import Router, AppConfig, create_app

router = Router()
app = create_app(router, config=AppConfig(
    cors_origins=["*"],
    request_id=True,       # X-Request-Id header (on by default)
    request_timing=True,   # request logging with ring buffer (on by default)
    trusted_hosts=["localhost", "myapp.example.com"],  # optional
))
```

Built-in middleware is configured declaratively via AppConfig. Custom middleware can be passed as a list of ASGI middleware classes via `AppConfig(middleware=[...])`.

### Dependency injection

FastAPI uses a `Depends()` wrapper function that reads the handler signature. fastware uses an explicit `deps` dict on route decorators, making dependencies visible in the decorator call without inspecting the handler body:

**FastAPI:**

```python
from fastapi import Depends

async def get_db():
    db = await connect()
    try:
        yield db
    finally:
        await db.close()

@app.get("/items")
async def list_items(db=Depends(get_db)):
    return await db.fetch_all("SELECT * FROM items")
```

**fastware:**

```python
from fastware import Router, DependencyResolver, create_app

async def get_db(request):
    db = await connect()
    try:
        yield db
    finally:
        await db.close()

router = Router()

@router.get("/items", deps={"db": get_db})
async def list_items(request, db):
    return await db.fetch_all("SELECT * FROM items")

app = create_app(router)
```

Dependencies are declared as a dict mapping names to factory callables, passed via the `deps` keyword on route decorators or `router.include_router(other, deps={...})` for router-wide dependencies. Factory functions receive the Request object and support the yield pattern for cleanup.

### Testing

The test client pattern is nearly identical between the two frameworks. Both wrap httpx with ASGI transport to send requests directly to the ASGI application in-process, so your existing test assertions (status code checks, JSON body comparisons, header inspection) mostly transfer unchanged -- only the import path changes:

**FastAPI:**

```python
from fastapi.testclient import TestClient

with TestClient(app) as client:
    resp = client.get("/health")
    assert resp.status_code == 200
```

**fastware:**

```python
from fastware.testing import TestClient

with TestClient(app) as client:
    resp = client.get("/health")
    assert resp.status_code == 200
```

The pattern is the same. fastware's TestClient wraps httpx with ASGITransport. An async variant is also available:

```python
from fastware.testing import AsyncTestClient

async with AsyncTestClient(app) as client:
    resp = await client.get("/health")
    assert resp.status_code == 200
```

### SSE (Server-Sent Events)

FastAPI requires the `sse-starlette` third-party package and a manual generator function pattern. fastware includes a built-in `Broadcaster` with typed event registration, per-client async queues, automatic disconnect pruning, and configurable heartbeat intervals:

**FastAPI** -- requires `sse-starlette` third-party package:

```python
from sse_starlette.sse import EventSourceResponse

@app.get("/events")
async def events():
    async def generate():
        while True:
            yield {"data": "ping"}
            await asyncio.sleep(1)
    return EventSourceResponse(generate())
```

**fastware** -- built-in Broadcaster:

```python
from fastware import Router, Broadcaster, sse_route, create_app

broadcaster = Broadcaster(heartbeat_interval=30)
broadcaster.register_event("update")

router = Router()
router.add_route("GET", "/events", sse_route(broadcaster))

@router.post("/notify")
async def notify(request):
    broadcaster.broadcast("update", {"msg": "hello"})
    return {"ok": True}

app = create_app(router)
```

The Broadcaster manages per-client queues, prunes disconnected clients, and optionally sends heartbeat comments to keep connections alive. See the [SSE Broadcasting guide](sse-guide.md) for full details.

## What's different (trade-offs)

- **No automatic OpenAPI generation.** FastAPI generates an OpenAPI schema from your route signatures automatically. fastware does not currently generate OpenAPI docs.
- **No signature-based parameter injection.** FastAPI inspects handler signatures to inject path params, query params, headers, and body. fastware uses an explicit Request object -- you call `request.path_params`, `request.query()`, `request.json`, `request.header()`.
- **No Pydantic integration at the routing level.** FastAPI validates request bodies and query params through Pydantic models declared in handler signatures. fastware supports `request.json_as(MyModel)` for Pydantic body validation and `response_model` on routes for response validation, but does not auto-parse from signatures.

## What's better

- **msgspec (10-75x faster JSON)** -- all JSON serialization and deserialization uses msgspec. No Pydantic overhead on the hot path.
- **Granian (managed server lifecycle)** -- `serve()` handles PID files, port checking, signal forwarding, and process group management. No separate `uvicorn.run()` configuration needed.
- **Built-in SSE** -- the Broadcaster provides typed events, per-client queues, auto-pruning, and heartbeats with zero third-party dependencies.
- **Built-in middleware suite** -- CORS, request ID propagation, request timing with ring buffer, trusted host checking, and Vite dev proxy are all included.
- **Built-in test client** -- both sync and async test clients ship with fastware. No need to install Starlette or httpx separately (httpx is a dev dependency).
- **Optional deps are truly optional** -- pywebview, structlog, pydantic, and websockets are late-imported only when needed. The core framework loads with only msgspec and granian.

## API reference

See the `Router` class for all 6 route registration methods (get, post, put, patch, delete, ws) plus `add_route` and `include_router` for programmatic registration, and the `AppConfig` dataclass for all 12 declarative configuration options including middleware, static files, and SPA fallback:

:-: ref path="src.fastware.routing" target="Router"

:-: ref path="src.fastware.app" target="AppConfig"
