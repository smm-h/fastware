---
title: Vite Dev Mode
description: How to use fastware's ViteDevProxy and dev() function for hot-reload frontend development
date: 2026-07-01
---

# Vite Dev Mode

When building a frontend (React, Vue, Svelte, or plain TypeScript) alongside a fastware backend, you need both servers running during development -- Vite for hot module replacement (HMR) and fastware for your API routes and SSE endpoints. fastware provides two tools for this: the `dev()` function and the `ViteDevProxy` middleware.

## The problem

During development, you have two processes:

- **Vite dev server** (port 5173) -- serves frontend assets, handles HMR via WebSocket, transforms TypeScript/JSX on the fly
- **fastware server** (port 8000) -- handles API routes, SSE broadcasting, WebSocket endpoints

The browser loads the page from one origin but needs to reach both. You could configure Vite's proxy, but that means your API is behind Vite and you lose fastware's middleware, error handling, and SSE support. fastware takes the opposite approach: the **backend is the entry point**, and unmatched requests are proxied to Vite.

## The dev() function

The simplest way to run both servers together:

```python
from fastware.dev import dev

dev(
    "myapp:app",
    vite_command="npm run dev",
    vite_port=5173,
    host="127.0.0.1",
    port=8000,
)
```

`dev()` does three things in order:

1. Spawns Vite as a subprocess (`npm run dev` by default)
2. Waits for Vite to be ready (polls the port for up to 15 seconds)
3. Wraps your app with `ViteDevProxy` and starts the fastware server

When the fastware server shuts down (Ctrl+C), the Vite subprocess is terminated automatically.

### Parameters

| Parameter | Default | Description |
|---|---|---|
| `target` | (required) | ASGI app -- either an import path string (`"myapp:app"`) or a callable |
| `vite_command` | `"npm run dev"` | Shell command to start the Vite dev server |
| `vite_port` | `5173` | Port the Vite dev server listens on |
| `host` | `None` | Bind address (falls back to env var) |
| `port` | `None` | Bind port (falls back to env var) |
| `pid_path` | `None` | Optional PID file path for process management |
| `name` | `"FASTWARE"` | Application name for env var prefix and logging |
| `pre_serve` | `None` | Optional callable invoked before server starts |

## ViteDevProxy middleware

Under the hood, `dev()` uses `ViteDevProxy`, which you can also use directly for more control:

```python
from fastware import Router, create_app, AppConfig

router = Router()

# ... register routes ...

app = create_app(router, config=AppConfig(
    vite_dev_port=5173,
))
```

Setting `vite_dev_port` in `AppConfig` enables ViteDevProxy as a built-in middleware layer.

### How routing works

ViteDevProxy uses a **backend-first** strategy for HTTP requests:

1. Every HTTP request hits the fastware app first
2. If the app returns 404 (no route matched), the request is proxied to Vite
3. Vite serves the frontend asset (JS, CSS, HTML, images)

This means backend routes like `/health`, `/api/users`, or `/events` work without any prefix configuration -- they are handled by your fastware router directly. Only requests that don't match any route are forwarded to Vite.

For **WebSocket** connections, the proxy uses prefix-based routing because WebSocket upgrades cannot be retried:

- Paths matching the API prefix (`/api` by default) or backend prefixes go to the fastware app
- Everything else is proxied to Vite (for HMR WebSocket connections)

### Configuring backend prefixes

By default, ViteDevProxy routes WebSocket connections on `/events` to the backend (in addition to the API prefix). If your SSE or WebSocket endpoints use different paths, configure them explicitly:

```python
from fastware.middleware import ViteDevProxy

proxy = ViteDevProxy(
    app,
    vite_port=5173,
    api_prefix="/api",
    backend_prefixes=["/events", "/ws", "/sse"],
)
```

The `backend_prefixes` parameter is a list of path prefixes that should be routed to the fastware app for WebSocket connections. HTTP requests to these paths go to the backend first regardless (because of the backend-first strategy).

## Production: SPA fallback

In production, Vite is not running. Instead, you build your frontend to static files and serve them with fastware's SPA fallback:

```bash
npm run build   # outputs to dist/
```

```python
from pathlib import Path
from fastware import Router, AppConfig, create_app, serve

router = Router()

# ... register API routes ...

app = create_app(router, config=AppConfig(
    static_dir=Path("dist/assets"),
    static_path="/assets",
    spa_fallback=Path("dist/index.html"),
    api_prefix="/api",
))

if __name__ == "__main__":
    serve(app, foreground=True, host="0.0.0.0", port=8000)
```

With SPA fallback:

- Requests to `/assets/*` serve files from `dist/assets/`
- API routes (`/api/*`) are handled by the router (404 if no match)
- All other GET requests serve `dist/index.html`, letting the frontend router handle client-side navigation

## Complete development setup

**backend (myapp.py):**

```python
from fastware import Router, Broadcaster, sse_route, create_app

broadcaster = Broadcaster(heartbeat_interval=30)
broadcaster.register_event("update")

router = Router()
router.add_route("GET", "/events", sse_route(broadcaster))

@router.get("/api/health")
async def health(request):
    return {"status": "ok"}

@router.post("/api/items")
async def create_item(request):
    item = request.json
    broadcaster.broadcast("update", {"action": "created", "item": item})
    return {"ok": True}

app = create_app(router)
```

**dev entry point (dev.py):**

```python
from fastware.dev import dev

if __name__ == "__main__":
    dev("myapp:app", host="127.0.0.1", port=8000)
```

**production entry point (main.py):**

```python
from pathlib import Path
from fastware import AppConfig, create_app, serve
from myapp import router

app = create_app(router, config=AppConfig(
    static_dir=Path("dist/assets"),
    static_path="/assets",
    spa_fallback=Path("dist/index.html"),
    api_prefix="/api",
))

if __name__ == "__main__":
    serve(app, foreground=True, host="0.0.0.0", port=8000)
```

Run `python dev.py` during development, `python main.py` in production.

## API reference

:-: ref path="src.fastware.dev"

:-: ref path="src.fastware.middleware" target="ViteDevProxy"
