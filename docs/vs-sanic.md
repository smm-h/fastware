---
title: fastware vs Sanic
description: "Comparison of fastware and Sanic: both bundle their own server, but differ in JSON engine (msgspec vs stdlib), auth, SSE, and worker management."
date: 2026-07-01
---

# fastware vs Sanic

Sanic and fastware share an unusual trait among Python web frameworks: both bundle their own server instead of deferring to a separate ASGI server like Uvicorn. This makes them both "run it and it works" frameworks, but they make different choices about what that server looks like and what else the framework includes.

## Server philosophies

**Sanic** has its own server written in Python (built on top of `uvloop` and `httptools`). It handles process management internally, including multi-worker forking, graceful reloading, and signal handling. The server is tightly integrated with the framework -- `app.run()` starts everything.

**fastware** manages Granian, a Rust-based ASGI server. Instead of writing a server from scratch, fastware wraps Granian with lifecycle management: PID files, port availability checks, process group leadership, health probing, and stop/status commands. The `serve()` function provides foreground and background modes, and `serve_background()` spawns a fully detached subprocess.

The practical difference: Sanic's server is Python-native and deeply integrated. fastware's server is Rust-native and managed externally. Granian's Rust HTTP parsing gives it a performance edge on raw throughput, while Sanic's Python server gives it more control over worker management and custom protocols.

## Feature comparison

| | fastware | Sanic |
|---|---|---|
| Server | Granian (Rust, managed externally) | Built-in (Python, uvloop + httptools) |
| JSON engine | msgspec | stdlib json (Pydantic optional) |
| SSE | Built-in Broadcaster with typed events | Not built in (use streaming response) |
| DI | DependencyResolver (sync/async factories, generator cleanup) | Injection via `ext.injection` |
| Server lifecycle | serve/stop/status/serve_background with PID files | `app.run()` with multi-worker, auto-reload |
| Middleware | 5 built-in (CORS, RequestID, RequestTiming, TrustedHost, ViteDevProxy) | Middleware via decorators, listeners |
| Auth | JWT, passwords, CSRF, rate limiting, user storage | Not built in (Sanic-Ext or third-party) |
| Background tasks | TaskRegistry with feature gating | `app.add_task()` |
| Test client | Built-in TestClient and AsyncTestClient | Built-in `app.test_client` (ASGI) |
| Static files | Built-in with SPA fallback | `app.static()` |
| Dev mode | `dev()` with Vite proxy and HMR | Auto-reload with `debug=True` |
| Feature flags | Built-in FeatureFlags | Not included |
| Error log | SQLite-backed ErrorLog | Not included |
| OpenAPI | Not yet | Sanic-Ext (optional) |
| WebSocket | Supported with DI | Supported |
| Blueprints / sub-routers | `include_router()` with prefix and deps | Blueprints with prefix and middleware |
| Multi-worker | Via Granian workers | Built-in multi-worker forking |
| Signals / events | Lifespan context manager | Signal system (server events, request events) |

## Performance

Both frameworks prioritize performance, but optimize in different layers of the stack. The 3 key performance dimensions are JSON serialization speed, HTTP protocol parsing latency, and event loop throughput. fastware gains its edge primarily from msgspec (10-75x faster JSON than stdlib), while Sanic optimizes at the server level with uvloop and httptools:

- **JSON serialization**: fastware uses msgspec, which is significantly faster than stdlib json. Sanic uses stdlib json by default. This gives fastware an advantage on JSON-heavy endpoints. Sanic can be configured to use alternative JSON libraries, but it requires manual setup.
- **HTTP parsing**: Granian parses HTTP in Rust. Sanic uses httptools (a Python binding to the Node.js HTTP parser, also written in C). Both are fast at the protocol level.
- **Event loop**: Sanic uses uvloop (a C implementation of the asyncio event loop) by default. Granian has its own event loop management in Rust. Both are faster than the default asyncio loop.

## What Sanic does differently

- **Multi-worker forking**: Sanic can fork multiple worker processes from a single command (`app.run(workers=4)`). fastware relies on Granian's worker management, which is less exposed at the framework level.
- **Signal system**: Sanic has a rich event/signal system that lets you hook into server lifecycle events (`before_server_start`, `after_server_stop`), request lifecycle events, and custom signals. fastware uses lifespan context managers for startup/shutdown and middleware for request lifecycle.
- **Blueprints**: Sanic's blueprint system supports middleware scoped to blueprint routes, error handlers per blueprint, and blueprint groups. fastware has `include_router()` with prefix and dependency injection, but no blueprint-scoped middleware.
- **Version routing**: Sanic supports API versioning in routes (`/v1/users`, `/v2/users`) as a first-class feature. fastware does this via manual prefix configuration.
- **Mature ecosystem**: Sanic has been around since 2016 and has a larger community, more production deployments, and more third-party extensions (Sanic-Ext, Sanic-CORS, etc.).
- **Sanic-Ext**: Provides OpenAPI generation, input validation, CORS, and dependency injection as an official extension.

## What fastware includes that Sanic does not

- **SSE Broadcaster**: Typed events, per-client queues, automatic pruning, heartbeat. Sanic requires manual streaming response setup for SSE.
- **Auth primitives**: JWT, bcrypt passwords, CSRF double-submit, rate limiting, user storage -- all built in. Sanic requires third-party packages or manual implementation.
- **ViteDevProxy**: Built-in middleware for proxying to a Vite dev server with WebSocket HMR support. The `dev()` function starts both servers together.
- **Feature flags**: `FeatureFlags` with defaults and file-based overrides.
- **Error log**: SQLite-backed 5xx error log integrated with request timing.
- **PID file management**: `serve()`, `stop()`, `status()` with PID files for process lifecycle. Sanic manages workers internally but does not expose PID file management.
- **msgspec as the JSON engine**: fastware uses msgspec throughout for 10-75x faster JSON serialization compared to stdlib json.

## When to choose fastware

- You want msgspec performance for JSON without manual configuration.
- You need SSE broadcasting as a first-class feature.
- You want built-in auth primitives (JWT, passwords, CSRF, rate limiting).
- You want managed server lifecycle with PID files and stop/status commands.
- You are building a full-stack app with Vite and want integrated dev mode.
- You prefer Granian's Rust-based HTTP handling.

## When to choose Sanic

- You need multi-worker process management built into the framework.
- You want a rich signal/event system for hooking into server and request lifecycle.
- You need blueprint-scoped middleware and error handlers.
- You want a larger community with more production track record (since 2016).
- You need OpenAPI support via Sanic-Ext.
- You prefer a Python-native server with full control over worker management.
