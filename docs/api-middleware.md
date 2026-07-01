---
title: Middleware API Reference
description: CORS, request ID tracing, request timing, trusted host validation, and Vite dev proxy middleware
date: 2026-07-01
---

# Middleware API Reference

All middleware classes are pure ASGI -- no framework dependency beyond fastware's own `send_error` helper. This makes them streaming-safe (SSE, WebSocket) and avoids the response-buffering issues of BaseHTTPMiddleware-style wrappers.

Built-in middleware is automatically applied by `create_app` when the corresponding `AppConfig` fields are set. You can also use these classes directly for custom middleware stacks.

:-: ref path="src.fastware.middleware"

## CORS Configuration for a Typical SPA

When building a single-page application with a separate frontend dev server (e.g., Vite on port 5173), configure CORS to allow the frontend origin:

```python
from fastware import Router, create_app, AppConfig

router = Router()

# Option 1: Via AppConfig
app = create_app(
    router,
    config=AppConfig(
        cors_origins=["http://localhost:5173"],
        api_prefix="/api",
        spa_fallback=Path("dist/index.html"),
    ),
)

# Option 2: Using CORSMiddleware directly for full control
from fastware.middleware import CORSMiddleware

app = create_app(
    router,
    middleware=[
        lambda app: CORSMiddleware(
            app,
            allow_origins=["http://localhost:5173", "https://myapp.com"],
            allow_methods=["GET", "POST", "PUT", "DELETE"],
            allow_headers=["authorization", "content-type", "x-csrf-token"],
            allow_credentials=True,
        ),
    ],
)
```

When `cors_origins` is set on `AppConfig`, `create_app` applies `CORSMiddleware` automatically with default methods and headers. Use the direct middleware approach when you need to customize allowed methods or headers.

## ViteDevProxy Routing

The `ViteDevProxy` middleware uses a backend-first routing strategy for HTTP requests:

1. Every HTTP request hits the fastware backend first.
2. If the backend returns 404 (no route matched), the request is proxied to the Vite dev server.
3. This means backend routes like `/health` or `/metrics` work without needing an API prefix.

The `backend_prefixes` parameter controls which additional paths are always routed to the backend for WebSocket connections (since WebSocket upgrades cannot be retried). By default, this includes `["/events"]` for SSE endpoints.

```python
from fastware import Router, create_app, AppConfig

router = Router()

# Vite dev server on port 5173
# Backend routes: /api/*, /events, /ws/*
app = create_app(
    router,
    config=AppConfig(
        vite_dev_port=5173,
        api_prefix="/api",
    ),
)

# Or with custom backend prefixes for WebSocket routing:
from fastware.middleware import ViteDevProxy

app = create_app(
    router,
    middleware=[
        lambda app: ViteDevProxy(
            app,
            vite_port=5173,
            api_prefix="/api",
            backend_prefixes=["/events", "/ws", "/notifications"],
        ),
    ],
)
```

For WebSocket connections, prefix-based routing is used: paths matching `api_prefix` or any `backend_prefixes` entry go to the backend; everything else is proxied to Vite (for HMR). HTTP requests use the try-backend-first approach regardless of path.
