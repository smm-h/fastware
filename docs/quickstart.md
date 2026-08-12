---
title: Quickstart
description: "Build your first fastware ASGI application in 6 steps: routing, response types, app factory, server, SSE broadcasting, and middleware."
date: 2026-07-01
---

# Quickstart

This guide walks through building a complete fastware application from scratch. Each step adds a new capability, and every code block is runnable on its own.

## Install

```bash
pip install fastware
```

## Step 1: Create a Router and add routes

The `Router` is the core of every fastware app. It provides decorator-based route registration for all 7 HTTP methods (`get`, `post`, `put`, `patch`, `delete`, `options`, `head`) plus WebSocket routes via `ws`. Each handler receives a `Request` object with typed accessors for path parameters, query strings, headers, and body:

```python
from fastware import Router, Request

router = Router()

@router.get("/health")
async def health(request: Request):
    return {"status": "ok"}

@router.get("/greet/{name}")
async def greet(request: Request):
    name = request.path_params["name"]
    return {"greeting": f"Hello, {name}!"}
```

Path parameters use `{param}` syntax. For typed parameters, use `{id:int}` -- the value is automatically coerced, and a non-integer path segment will produce a 404 instead of a crash.

```python
@router.get("/users/{id:int}")
async def get_user(request: Request):
    user_id = request.path_params["id"]  # already an int
    return {"id": user_id, "name": "Alice"}
```

:<: callout-tip
:=:
::: Returning a plain `dict` or `list` from a handler automatically wraps it in a `JSONResponse`. You only need explicit response types when you want to set status codes, headers, or cookies.
:>:

## Step 2: Use response types

fastware provides 5 typed response classes for different content types. Each accepts optional `status`, `headers`, and `cookies` parameters for fine-grained control over the HTTP response. The `HTTPError` exception can be raised from anywhere to return a JSON error body:

```python
from fastware import (
    JSONResponse,
    HTMLResponse,
    TextResponse,
    BytesResponse,
    StreamResponse,
    HTTPError,
)

@router.get("/json")
async def json_example(request: Request):
    return JSONResponse(
        {"items": [1, 2, 3]},
        status=200,
        headers={"X-Custom": "value"},
    )

@router.get("/page")
async def html_example(request: Request):
    return HTMLResponse("<h1>Hello</h1>")

@router.get("/error")
async def error_example(request: Request):
    raise HTTPError(404, "Item not found")
```

`HTTPError` can be raised from anywhere in a handler to return a JSON error response with the given status code and detail message.

## Step 3: Create the ASGI app

The `create_app` factory assembles the router, middleware chain, static file serving, SPA fallback, and async lifespan hooks into a single ASGI callable that can be served by Granian or any ASGI-compatible server:

```python
from fastware import Router, create_app

router = Router()

@router.get("/health")
async def health(request):
    return {"status": "ok"}

app = create_app(router)
```

`create_app` accepts keyword arguments or an `AppConfig` dataclass for configuration. The defaults enable request ID and request timing middleware automatically.

```python
from pathlib import Path
from fastware import AppConfig, create_app

app = create_app(
    router,
    config=AppConfig(
        static_dir=Path("static"),
        static_path="/assets",
        spa_fallback=Path("static/index.html"),
        api_prefix="/api",
    ),
)
```

## Step 4: Serve it

Use `serve()` to start the Granian ASGI server with managed PID files, port availability checks, and signal handling. The `foreground` parameter controls whether the call blocks until shutdown or returns immediately with the server URL:

```python
from fastware import Router, create_app, serve

router = Router()

@router.get("/health")
async def health(request):
    return {"status": "ok"}

app = create_app(router)

if __name__ == "__main__":
    serve(app, foreground=True, host="127.0.0.1", port=8000)
```

:<: callout-tip
:=:
::: `host` and `port` are required -- fastware does not use implicit defaults for these values. You can also set them via `{NAME}_HOST` and `{NAME}_PORT` environment variables, where `NAME` defaults to `FASTWARE`.
:>:

For background mode (useful when embedding fastware in another application):

```python
url = serve(app, foreground=False, host="127.0.0.1", port=8000)
print(f"Server running at {url}")
```

## Step 5: Add SSE broadcasting

The `Broadcaster` manages typed server-sent events with per-client async queues, configurable heartbeat intervals, and automatic disconnect pruning. Events must be registered before broadcast (strict mode), catching typos and invalid event names at development time rather than silently dropping messages:

```python
from fastware import Router, Broadcaster, sse_route, create_app, serve

router = Router()
broadcaster = Broadcaster()

# Register event types (strict mode enforces this)
broadcaster.register_event("message")
broadcaster.register_event("notification")

# SSE endpoint -- clients connect here to receive events
router.add_route("GET", "/events", sse_route(broadcaster))

@router.post("/send")
async def send_message(request):
    data = request.json
    broadcaster.broadcast("message", data)
    return {"sent": True}

app = create_app(router)

if __name__ == "__main__":
    serve(app, foreground=True, host="127.0.0.1", port=8000)
```

Clients connect to `/events` and receive SSE messages. The broadcaster automatically prunes clients whose queues fill up (disconnected or stuck clients).

:<: callout-tip
:=:
::: In strict mode (the default), calling `broadcast()` with an unregistered event name raises `ValueError`. Pass `strict=False` to `Broadcaster()` if you want to skip event type validation.
:>:

## Step 6: Add middleware

fastware includes 5 built-in middleware classes (CORS, request ID, request timing, trusted host, Vite dev proxy) that can be enabled declaratively via `create_app` parameters or `AppConfig` fields, without writing any middleware wrapping code:

```python
from fastware import Router, create_app, serve

router = Router()

@router.get("/health")
async def health(request):
    return {"status": "ok"}

app = create_app(
    router,
    cors_origins=["http://localhost:5173"],  # enable CORS
    trusted_hosts=["localhost", "127.0.0.1:8000"],  # reject unknown hosts
    request_id=True,    # assign X-Request-Id (enabled by default)
    request_timing=True,  # log request duration (enabled by default)
)

if __name__ == "__main__":
    serve(app, foreground=True, host="127.0.0.1", port=8000)
```

You can also add custom middleware. Middleware is a callable that takes an ASGI app and returns a new ASGI app:

```python
from fastware.middleware import CORSMiddleware

async def my_middleware(scope, receive, send):
    # do something before
    await app(scope, receive, send)
    # do something after

app = create_app(router, middleware=[my_middleware])
```

## Next steps

- [Core API Reference](api.md) -- full documentation of routing, request, and response types
- [Auth and Security](api-auth.md) -- JWT, passwords, CSRF, rate limiting
- [Optional Extras](extras.md) -- install only what you need
- [Migration from FastAPI](migration-fastapi.md) -- porting guide with side-by-side comparisons
