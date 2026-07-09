---
title: fastware
description: "fastware home: a fast, batteries-included ASGI framework with msgspec JSON, the bundled Granian server, install steps, and a minimal Router example."
date: 2026-07-01
---

# fastware

:-: var key="project.description"

**Current version:** :-: var key="project.version"

## Install

```bash
pip install fastware
```

## Minimal example

```python
from fastware import Router, create_app, serve

router = Router()

@router.get("/hello/{name}")
async def hello(request):
    return {"message": f"Hello, {request.path_params['name']}!"}

app = create_app(router)
serve(app, foreground=True, host="127.0.0.1", port=8000)
```

Run it and visit `http://127.0.0.1:8000/hello/world` to see `{"message": "Hello, world!"}`.

## Features

| Feature | Description | Docs |
|---|---|---|
| Routing and Requests | Path parameters with type coercion, query validation, request body parsing, WebSocket routes | [Core API](api.md) |
| SSE Broadcasting | Typed server-sent events with per-client queues and automatic disconnect pruning | [SSE Guide](sse-guide.md) |
| Middleware Suite | CORS, request tracing, trusted host, request ID, Vite dev proxy -- all pure ASGI | [Middleware API](api-middleware.md) |
| Auth and Security | JWT tokens, bcrypt password hashing, CSRF double-submit, role-based access, rate limiting | [Auth API](api-auth.md) |
| Server Lifecycle | Granian integration with PID files, port checks, background mode, status, and hot reload | [Server API](api-server.md) |
| Dependency Injection | Per-request DI with sync/async factories, generator cleanup, caching, and test overrides | [DI Guide](di-guide.md) |
| Testing | Sync and async test clients wrapping httpx with ASGITransport -- no real server needed | [Testing API](api-testing.md) |
| Migration from FastAPI | Side-by-side comparison and step-by-step guide for porting FastAPI applications | [Migration Guide](migration-fastapi.md) |

## Why fastware?

- **msgspec for JSON** -- 10-75x faster serialization than Pydantic. No schema compilation step, no startup penalty.
- **Granian server included** -- Rust-based ASGI server with managed lifecycle, PID files, and signal handling. No need to install or configure a separate server.
- **Batteries included** -- SSE broadcasting, dependency injection, authentication, middleware suite, test client, background tasks, and structured logging are all built in.
- **Minimal core dependencies** -- The framework core depends only on msgspec and granian. Everything else is opt-in via [extras](extras.md).

## Dependencies

The table below lists all runtime and optional dependencies that fastware can install. The core has only 2 required packages (msgspec for JSON serialization and granian for ASGI serving); all others are opt-in via 6 extras groups (auth, logging, dev, testing, mcp, pydantic) that add specific capabilities without bloating the default install:

:-: table-dep path="pyproject.toml"

## Project structure

The source tree consists of 20 modules organized by feature area (routing, responses, middleware, SSE, server, auth, DI, testing, and more). Each module is independently importable with no circular dependencies, and optional features use late imports to avoid loading unused packages:

:-: list-tree path="src/fastware/" depth="1"
