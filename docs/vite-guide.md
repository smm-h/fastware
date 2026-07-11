---
title: Vite Dev Mode
description: "fastware Vite integration: the dev() launcher, ViteDevProxy backend-first routing, production SPA fallback, and the service worker cache-retirement route."
date: 2026-07-01
---

# Vite Dev Mode

When building a frontend (React, Vue, Svelte, or plain TypeScript) alongside a fastware backend, you need both servers running during development -- Vite for hot module replacement (HMR) and fastware for your API routes and SSE endpoints. fastware provides two tools for this: the `dev()` function and the `ViteDevProxy` middleware.

## The problem

During development, you have 2 processes that need to coexist on different ports, each serving a distinct role. The frontend dev server handles asset transformation and hot module replacement, while the backend handles API routes, SSE broadcasting, and WebSocket endpoints:

- **Vite dev server** (port 5173) -- serves frontend assets, handles HMR via WebSocket, transforms TypeScript/JSX on the fly
- **fastware server** (port 8000) -- handles API routes, SSE broadcasting, WebSocket endpoints

The browser loads the page from one origin but needs to reach both. You could configure Vite's proxy, but that means your API is behind Vite and you lose fastware's middleware, error handling, and SSE support. fastware takes the opposite approach: the **backend is the entry point**, and unmatched requests are proxied to Vite.

## The dev() function

The simplest way to run both the Vite frontend and fastware backend servers together in a single process. It handles subprocess management, port readiness polling (up to 15 seconds), and proxy configuration automatically:

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

Under the hood, `dev()` uses `ViteDevProxy`, a pure ASGI middleware class that intercepts HTTP and WebSocket requests. You can also use `ViteDevProxy` directly for more control over proxy behavior, backend prefix configuration, and middleware ordering in your ASGI stack, or to integrate with custom app factory patterns:

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

ViteDevProxy uses a **backend-first** strategy for HTTP requests, which eliminates the need for API prefix configuration in most cases and keeps all middleware, error handling, and SSE support intact. This approach is the opposite of Vite's built-in proxy (which puts Vite first), and avoids the common problem of losing backend middleware when requests flow through the frontend server first. The 3-step routing logic is:

1. Every HTTP request hits the fastware app first
2. If the app returns 404 (no route matched), the request is proxied to Vite
3. Vite serves the frontend asset (JS, CSS, HTML, images)

This means backend routes like `/health`, `/api/users`, or `/events` work without any prefix configuration -- they are handled by your fastware router directly. Only requests that don't match any route are forwarded to Vite.

For **WebSocket** connections, the proxy uses prefix-based routing because WebSocket upgrades cannot be retried:

- Paths matching the API prefix (`/api` by default) or backend prefixes go to the fastware app
- Everything else is proxied to Vite (for HMR WebSocket connections)

### Configuring backend prefixes

By default, ViteDevProxy routes WebSocket connections on `/events` to the backend (in addition to the API prefix). The `backend_prefixes` list is checked for every WebSocket upgrade request; paths matching any prefix are handled by fastware while all others are proxied to Vite for HMR. If your SSE or WebSocket endpoints use different paths, configure them explicitly:

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

In production, Vite is not running. Instead, you build your frontend to static files (`npm run build`) and serve them with fastware's built-in SPA fallback, which routes unmatched GET requests to `index.html` for client-side navigation:

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

## Service workers and cache retirement

When you serve a hashed production build, `create_app` requires an explicit
`sw_mode` (`AppConfig.sw_mode`) alongside `static_dir`/`spa_fallback`:

- `"cache"` -- a per-build caching worker at `/__fastware/sw.js` (cache-first for
  hashed assets, network-first for the shell).
- `"reset"` -- a self-destruct worker served at `/__fastware/sw.js` **and** at the
  legacy registration paths; it clears caches, unregisters, and reloads once.
- `"off"` -- no worker; `/__fastware/sw.js` returns 404.

### Retiring a cache worker: `cache` -> `reset` -> `off`

Never switch a cache-mode worker straight to `off`. In `off` mode the worker
script 404s, but **a 404 does not unregister an already-installed worker** --
clients that installed the cache worker stay controlled by it indefinitely,
serving stale assets. The mandatory migration route is:

1. **`cache` -> `reset`.** Deploy `sw_mode="reset"`. The self-destruct worker is
   served AT the same URL the old worker lives at, so installed clients pick it
   up, clear their caches, unregister, and end controller-free.
2. **Drain.** Keep `reset` deployed until known clients have loaded at least once
   and passed through the self-destruct logic.
3. **`reset` -> `off`.** Only then switch to `sw_mode="off"`.

The `fastware dev run` CLI hard-errors if the resolved app is configured with
`sw_mode="cache"`, because Vite dev assets use non-content hashes that a caching
worker would pin. Use `sw_mode="off"` for development.

This guard only covers **in-process (`backend.app`) backends**, whose `sw_mode`
can be read by importing the app. **cmd-form backends (`backend.cmd`) are not
introspectable for `sw_mode`** -- the command is an opaque subprocess, so
`fastware dev run` cannot verify its worker mode and prints a one-line stderr
notice that the guard was skipped. If you run a cmd-form backend, make sure its
command does not serve a caching service worker in development.

## Complete development setup

This example shows a complete development setup with 3 files: a fastware backend module serving API routes and SSE events via the Broadcaster, a dev entry point that runs both Vite and fastware together with hot reload, and a production entry point that serves pre-built static assets with SPA fallback routing:

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

See the `dev()` function reference for combined Vite and fastware server startup with subprocess management, and the `ViteDevProxy` class reference for fine-grained control over proxy routing, backend prefix configuration, and WebSocket connection handling:

:-: ref path="src.fastware.dev"

:-: ref path="src.fastware.middleware" target="ViteDevProxy"
