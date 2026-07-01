---
title: fastware vs Starlette
description: Comparison of fastware and Starlette -- batteries-included framework vs modular ASGI toolkit
date: 2026-07-01
---

# fastware vs Starlette

Starlette is a lightweight ASGI toolkit that provides the building blocks for web applications. FastAPI, for example, is built on top of Starlette. fastware is a batteries-included framework that provides the assembled product -- routing, server management, SSE, auth, and middleware all ship together.

## Toolkit vs framework

The core difference is scope:

**Starlette provides building blocks.** You get a router, request/response classes, middleware protocol, WebSocket support, and a test client. You assemble them into an application, choose a server (Uvicorn, Hypercorn, Daphne), configure middleware, pick a JSON library, add auth, and wire up SSE yourself.

**fastware provides the assembled product.** You get the same building blocks, but also a managed Granian server, SSE broadcaster, auth module, dependency injection, feature flags, error logging, background tasks, dev mode with Vite proxy, and a CLI -- all designed to work together.

## Feature comparison

| | fastware | Starlette |
|---|---|---|
| Routing | `Router` with `{param}`, `{param:int}`, `{param:path}` | `Router` with `{param}`, `{param:int}`, `{param:path}`, `{param:float}` |
| Request/Response | `Request`, `JSONResponse`, `HTMLResponse`, `StreamResponse`, `FileResponse`, etc. | `Request`, `JSONResponse`, `HTMLResponse`, `StreamingResponse`, `FileResponse`, etc. |
| JSON engine | msgspec (10-75x faster than stdlib json) | stdlib json |
| ASGI server | Granian (managed lifecycle, PID files, stop/status) | None (BYO: Uvicorn, Hypercorn, etc.) |
| Middleware | 5 built-in (CORS, RequestID, RequestTiming, TrustedHost, ViteDevProxy) | 5 built-in (CORS, TrustedHost, HTTPSRedirect, GZip, Sessions) |
| SSE | Built-in Broadcaster with typed events, heartbeat, auto-pruning | Not included (use StreamingResponse manually) |
| WebSocket | Supported with DI | Supported |
| Dependency injection | DependencyResolver (sync/async factories, generator cleanup) | Not included |
| Auth | JWT, passwords, CSRF, rate limiting, user storage | Not included |
| Background tasks | TaskRegistry with feature gating | BackgroundTask (per-response, no registry) |
| Test client | Built-in TestClient and AsyncTestClient | TestClient (based on httpx) |
| Static files | Built-in with SPA fallback | StaticFiles (no SPA fallback) |
| Dev mode | `dev()` with Vite proxy and HMR | Not included |
| Feature flags | Built-in FeatureFlags | Not included |
| Error log | SQLite-backed ErrorLog | Not included |
| Exception handlers | Per-type with MRO-based priority | Per-type |
| Mount sub-apps | `router.mount(prefix, app)` | `Mount(path, app)` |
| Lifespan | Async context manager | Async context manager |

## What Starlette does differently

Starlette is intentionally minimal. This is a strength, not a weakness:

- **Lighter dependency footprint**: Starlette has no required dependencies beyond `anyio` and `typing_extensions`. fastware requires msgspec and granian.
- **More server choices**: Because Starlette does not manage a server, you can run it on Uvicorn, Hypercorn, Daphne, or any ASGI server. fastware is opinionated about Granian.
- **Foundation for other frameworks**: Starlette is designed to be built upon. FastAPI, Starlite (now Litestar), and others extend it. fastware is a standalone framework, not a toolkit for building frameworks.
- **HTTPSRedirect and GZip middleware**: Starlette includes these out of the box. fastware does not have them built in.
- **Sessions middleware**: Starlette has cookie-based sessions. fastware has JWT-based auth with session cookies, which is a different approach.
- **Mature and battle-tested**: Starlette has been in production at scale for years. fastware is new.

## When to choose fastware

- You want a framework that includes everything from auth to server management, rather than assembling pieces yourself.
- You want msgspec performance for JSON serialization without manually wiring it in.
- You need SSE broadcasting as a first-class feature.
- You want `serve()` / `stop()` / `status()` with PID files and process management instead of configuring a server separately.
- You are building a full-stack app with Vite and want the dev proxy built in.

## When to choose Starlette

- You want a minimal toolkit and prefer to choose each component yourself.
- You need to run on a server other than Granian (Uvicorn, Hypercorn, Daphne).
- You are building a framework on top of ASGI foundations rather than an application.
- You want the lightest possible dependency footprint.
- You need the maturity and community support of a well-established project.
