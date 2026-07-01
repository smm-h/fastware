---
title: fastware vs FastAPI
description: Detailed comparison of fastware and FastAPI covering performance, features, architecture, and developer experience
date: 2026-07-01
---

# fastware vs FastAPI

FastAPI is the most popular Python ASGI framework, known for automatic OpenAPI documentation and type-driven route signatures. fastware takes a different approach: batteries-included infrastructure with the fastest possible JSON serialization.

## At a glance

| | fastware | FastAPI |
|---|---|---|
| JSON engine | msgspec (Struct-based, zero-copy decode) | Pydantic v2 (Rust core, schema compilation) |
| ASGI server | Granian (managed lifecycle, PID files, signal handling) | Uvicorn (BYO, no lifecycle management) |
| Framework deps | msgspec + granian | starlette + pydantic + anyio |
| SSE | Built-in Broadcaster with typed events | Third-party (sse-starlette) |
| Dependency injection | DependencyResolver (sync/async factories, generator cleanup) | Depends() (signature injection, sub-dependencies) |
| Server lifecycle | serve/stop/status with PID files, background mode, reload | Not included -- bring your own process manager |
| Test client | Built-in TestClient and AsyncTestClient | Starlette's TestClient |
| Middleware | 5 built-in (CORS, RequestID, RequestTiming, TrustedHost, ViteDevProxy) | 2 from Starlette (CORS, TrustedHost) |
| SPA fallback | Built-in with static file serving | Not included |
| Auth module | JWT, passwords, CSRF, rate limiting, user storage | Not included -- third-party or DIY |
| Background tasks | TaskRegistry with feature gating | BackgroundTasks (per-request, no registry) |
| Feature flags | Built-in FeatureFlags with file overrides | Not included |
| Error log | SQLite-backed ErrorLog for 5xx responses | Not included |
| OpenAPI | Not yet | Automatic from type annotations |
| Type-driven routes | Not yet | Signature injection with automatic validation |
| WebSocket | Supported (with DI) | Supported (with DI) |

## Performance

### JSON serialization

fastware uses msgspec for all JSON encoding and decoding. The msgspec documentation reports 10-75x faster serialization compared to Pydantic v1, and significant speedups over Pydantic v2 as well. msgspec Structs have no schema compilation step and no startup penalty -- they are defined as plain Python classes with type annotations and are ready to use immediately.

FastAPI uses Pydantic v2, which has a Rust-based core that improved performance substantially over v1. However, Pydantic still performs schema compilation at import time, and its validation model is fundamentally heavier than msgspec's zero-copy decoding approach.

In practice, the difference matters most for high-throughput JSON APIs where serialization is a significant fraction of request time. For typical CRUD apps with database-bound latency, the serialization difference is less impactful.

### ASGI server

fastware manages Granian, a Rust-based ASGI server. Granian handles HTTP parsing in Rust and has faster cold start than Uvicorn. fastware's `serve()` function provides PID file management, port availability checks, process group leadership (workers die with the parent), and signal handling out of the box.

FastAPI is server-agnostic and typically runs on Uvicorn. This means you configure and manage the server separately. There is no built-in PID file management, health checking, or stop/status commands.

## Batteries

Things fastware includes that FastAPI does not:

- **SSE Broadcaster**: Typed server-sent events with per-client async queues, automatic disconnection pruning, and optional heartbeat. FastAPI requires the third-party `sse-starlette` package.
- **Server lifecycle**: `serve()`, `stop()`, `status()`, `serve_background()` with PID files, port checks, and process group management. FastAPI has no server management -- you use Uvicorn or Gunicorn externally.
- **ViteDevProxy middleware**: Proxies unmatched requests to a Vite dev server with WebSocket HMR support. The `dev()` function starts both Vite and fastware in a single command.
- **Auth module**: JWT creation/verification, bcrypt password hashing, user storage (abstract + JSON file), CSRF double-submit middleware, rate limiting decorator, session cookie helpers. FastAPI has `Depends()` for auth patterns but no built-in auth primitives.
- **Feature flags**: `FeatureFlags` with defaults and per-machine JSON overrides.
- **Error log**: SQLite-backed `ErrorLog` for 5xx responses, integrated with request timing middleware.
- **Background task registry**: `TaskRegistry` with feature-gated lifecycle management. FastAPI's `BackgroundTasks` are per-request and have no registry or lifecycle.
- **Structured logging**: Optional structlog integration with request ID binding via contextvars.

## What FastAPI does better

Being honest about where FastAPI has clear advantages:

- **Automatic OpenAPI**: FastAPI generates OpenAPI schemas, Swagger UI, and ReDoc from your route signatures and Pydantic models. This is a major productivity feature for API documentation and client generation. fastware has no OpenAPI support yet.
- **Type-driven route signatures**: FastAPI's signature injection means the framework reads your function parameters, validates the request against them, and passes typed values. This eliminates manual parsing. fastware requires explicit `request.json`, `request.query()`, and `request.path_params` access.
- **Massive ecosystem**: FastAPI has thousands of extensions, tutorials, Stack Overflow answers, and integrations. fastware is new.
- **Extensive documentation**: FastAPI has one of the best documentation sites in the Python ecosystem, with interactive tutorials and deployment guides.
- **Huge community**: FastAPI has millions of downloads per month and a large contributor base. Bug reports get addressed quickly, edge cases are well-covered, and hiring for FastAPI skills is straightforward.
- **Sub-dependency resolution**: FastAPI's `Depends()` supports arbitrarily nested dependency trees with caching. fastware's `DependencyResolver` is simpler -- a flat dict of name-to-factory mappings resolved per request.
- **Pydantic integration depth**: If you are already heavily invested in Pydantic (validators, serializers, field-level constraints, computed fields), FastAPI gives you first-class integration. fastware supports Pydantic as an optional extra (`request.json_as()`, `response_model`), but msgspec Structs are the primary path.

## When to choose fastware

- You want batteries-included: SSE, auth, feature flags, error logging, server lifecycle, and dev mode without installing a dozen separate packages.
- You need SSE broadcasting as a first-class feature, not a third-party add-on.
- You want managed server lifecycle (start, stop, status, PID files) without configuring systemd or supervisord.
- You care about serialization performance and want msgspec's speed without giving up framework features.
- You are building a full-stack app with a frontend (SPA fallback, Vite dev proxy, static file serving are built in).

## When to choose FastAPI

- You need automatic OpenAPI documentation for your API.
- You want the largest community and ecosystem for finding help, extensions, and developers.
- You are already using Pydantic heavily and want deep framework integration with your models.
- You want type-driven route signatures where the framework infers request parsing from your function parameters.
- You are building a pure API service where OpenAPI code generation for clients matters.
