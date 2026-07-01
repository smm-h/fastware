---
title: fastware vs Litestar
description: Comparison of fastware and Litestar -- both use msgspec, different philosophies
date: 2026-07-01
---

# fastware vs Litestar

Litestar and fastware share a conviction: msgspec is the right JSON engine for Python web frameworks. Both use it for serialization and deserialization. But they come from different places and make different trade-offs.

## Origin stories

**Litestar** started as Starlite, a Starlette-based framework. It later dropped the Starlette dependency and became its own standalone framework. It grew into a full-featured framework with OpenAPI support, dependency injection, middleware, and an ORM integration layer. Litestar is a community-driven project with multiple maintainers.

**fastware** was extracted from wesktop, a Python framework for building web-based desktop applications. wesktop needed an ASGI router, SSE broadcaster, and managed server lifecycle for native desktop windows. These components were pulled out into fastware as a standalone web framework, bringing along the server management, auth module, and dev tooling that wesktop needed.

## Feature comparison

| | fastware | Litestar |
|---|---|---|
| JSON engine | msgspec | msgspec (also supports Pydantic, attrs) |
| ASGI server | Granian (managed lifecycle) | None (BYO: Uvicorn, Hypercorn, Granian) |
| OpenAPI | Not yet | Automatic from type annotations |
| DI | DependencyResolver (flat dict, sync/async factories) | Full DI with sub-dependencies and injection |
| SSE | Built-in Broadcaster with typed events | StreamingResponse (manual SSE) |
| Server lifecycle | serve/stop/status with PID files | Not included |
| Auth | JWT, passwords, CSRF, rate limiting, user storage | Session-based auth with multiple backends |
| Middleware | 5 built-in (CORS, RequestID, RequestTiming, TrustedHost, ViteDevProxy) | Multiple built-in (CORS, CSRF, rate limiting, compression, sessions) |
| Background tasks | TaskRegistry with feature gating | Not built in (use anyio task groups) |
| Test client | Built-in TestClient and AsyncTestClient | Built-in test client |
| Static files | Built-in with SPA fallback | Built-in |
| Dev mode | `dev()` with Vite proxy | Not included |
| Feature flags | Built-in FeatureFlags | Not included |
| Error log | SQLite-backed ErrorLog | Not included |
| ORM integration | Not included | SQLAlchemy integration (Advanced Alchemy) |
| CLI | strictcli-based diagnose and config | Litestar CLI |
| Type-driven routes | Not yet | Full type-driven parameter injection |
| WebSocket | Supported with DI | Supported with DI |
| Data validation | msgspec Structs (Pydantic optional) | msgspec, Pydantic, attrs |

## Shared ground: msgspec

Both frameworks use msgspec as their primary serialization engine, which gives them a shared performance advantage over Pydantic-based frameworks. If you are choosing between them and msgspec matters to you, both are solid choices.

However, Litestar also supports Pydantic and attrs as alternative data backends. You can mix and match within a single application. fastware supports Pydantic as an optional extra (`request.json_as()`, `response_model`) but msgspec is the only primary path -- there is no attrs support.

## What Litestar does better

- **OpenAPI**: Litestar generates OpenAPI schemas automatically from your route signatures and data models. fastware has no OpenAPI support yet. This is significant for API documentation and client code generation.
- **Type-driven route signatures**: Litestar inspects your handler function parameters and automatically extracts, validates, and injects request data. fastware requires manual `request.json`, `request.query()`, and `request.path_params` access.
- **Deeper DI**: Litestar's dependency injection supports sub-dependencies (dependencies that have their own dependencies), which enables complex DI graphs. fastware's DependencyResolver is a flat name-to-factory mapping.
- **Multiple data backends**: Supporting msgspec, Pydantic, and attrs gives teams flexibility when adopting Litestar in existing codebases.
- **ORM integration**: Litestar has first-party SQLAlchemy integration via Advanced Alchemy, including repository patterns and DTO generation. fastware has no ORM integration.
- **Larger community**: Litestar has more contributors, more production deployments, and more ecosystem support.

## What fastware does differently

- **Managed server lifecycle**: fastware includes `serve()`, `stop()`, `status()`, `serve_background()` with PID files, port checks, and process group management. Litestar does not manage a server.
- **Built-in SSE Broadcaster**: Typed server-sent events with per-client queues, automatic pruning, and heartbeat support. Litestar requires manual SSE via streaming responses.
- **Dev mode with Vite**: `dev()` starts both Vite and the ASGI server in a single command with HMR proxy. Litestar has no Vite integration.
- **Auth primitives**: JWT, bcrypt passwords, CSRF double-submit, rate limiting, and user storage are built in. Litestar has session auth with pluggable backends, but not JWT or password hashing.
- **Feature flags**: Built-in `FeatureFlags` with file-based overrides.
- **Error log**: SQLite-backed 5xx error log integrated with request timing middleware.
- **Background task registry**: `TaskRegistry` with feature-gated lifecycle, vs. no built-in task management in Litestar.

## When to choose fastware

- You want server lifecycle management built into the framework.
- You need SSE broadcasting as a first-class feature.
- You want auth primitives (JWT, passwords, CSRF) without third-party packages.
- You are building a full-stack app with Vite and want integrated dev mode.
- You prefer a leaner dependency tree (msgspec + granian, nothing else required).

## When to choose Litestar

- You need automatic OpenAPI documentation.
- You want type-driven route signatures with automatic parameter injection.
- You need SQLAlchemy/ORM integration.
- You want the flexibility to use Pydantic, attrs, or msgspec for data modeling.
- You want a larger community and more mature ecosystem.
- You need deep sub-dependency injection.
