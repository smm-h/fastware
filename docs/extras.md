---
title: Optional Extras
description: "Guide to fastware's 7 optional extras groups: auth (JWT, bcrypt), logging (structlog), dev (hot reload), testing, MCP, Pydantic, and all."
date: 2026-07-01
---

# Optional Extras

fastware's core depends only on `msgspec` and `granian`. Everything else is opt-in via extras groups. Install only what your application needs, or use `[all]` to get everything.

```bash
pip install fastware              # core only (msgspec + granian)
pip install fastware[auth]        # + JWT and password hashing
pip install fastware[logging]     # + structured logging
pip install fastware[dev]         # + hot reload, Vite proxy, WebSocket proxy
pip install fastware[testing]     # + async/sync test client
pip install fastware[mcp]         # + MCP server support
pip install fastware[pydantic]    # + Pydantic model support
pip install fastware[all]         # everything above
```

## All dependencies

The complete dependency table below shows every package fastware can install, organized by extras group. The core has only 2 runtime dependencies (msgspec for JSON serialization and granian for ASGI serving); the remaining 8 packages are optional and only installed when you explicitly request the corresponding extras group:

:-: table-dep path="pyproject.toml"

## Extras groups

### `auth` -- JWT and password hashing

**Installs:** `pyjwt` for JSON Web Token encoding and decoding with HS256 algorithm support, and `bcrypt` for password hashing with configurable work factor rounds. These 2 packages provide the cryptographic foundation for the entire auth module.

**Required by:** `fastware.auth` -- specifically `create_token`, `verify_token`, `hash_password`, `verify_password`, and everything that depends on them (`get_current_user`, `require_role`, `UserStore.create_user`).

**What happens without it:** Importing `fastware.auth` succeeds (the module uses late imports), but calling any function that needs `jwt` or `bcrypt` raises `ImportError` with a traceback pointing to the missing package. The error appears at call time, not at import time, so you can safely import `fastware.auth` in shared code without installing the extra.

```bash
pip install fastware[auth]
```

### `logging` -- structured logging

**Installs:** `structlog` for structured, context-bound logging with JSON output in production environments and colored console rendering in development. Structlog adds request ID binding via contextvars, so every log entry within a request automatically includes the X-Request-Id header value.

**Required by:** `fastware.middleware.RequestIDMiddleware` and `fastware.middleware.RequestTimingMiddleware` use structlog when available for context-bound structured logging (request ID in every log entry, request duration as structured data).

**What happens without it:** The middleware works normally but uses Python's standard `logging` module instead of structlog. No error is raised -- structlog support is detected at import time via a try/except.

```bash
pip install fastware[logging]
```

:<: callout-note
:=:
::: Unlike other extras, `logging` degrades gracefully rather than raising an error. The middleware detects structlog at import time and falls back to stdlib logging automatically.
:>:

### `dev` -- development tools

**Installs:** 3 packages for development workflows: `httpx` as the HTTP client for the Vite dev proxy, `watchfiles` as a Rust-based file watcher for sub-second hot reload, and `websockets` for proxying Vite HMR WebSocket connections to the frontend dev server.

**Required by:**

- `watchfiles` -- `serve(reload=True)` for hot reload during development. Without it, `serve(reload=True)` raises `ImportError`.
- `httpx` -- `fastware.middleware.ViteDevProxy` for proxying requests to a Vite dev server. Without it, the proxy raises `ImportError` when it tries to forward a request.
- `websockets` -- `ViteDevProxy` WebSocket proxying (HMR). Without it, WebSocket proxy connections are silently closed.

```bash
pip install fastware[dev]
```

### `testing` -- test client

**Installs:** `httpx`, an async-capable HTTP client that provides `ASGITransport` for sending requests directly to an ASGI application in-process, bypassing the network stack entirely for zero-overhead test execution. Both the `AsyncTestClient` (for async/await tests) and `TestClient` (for synchronous tests) depend on this single package.

**Required by:** `fastware.testing` -- the `AsyncTestClient` and `TestClient` classes that wrap httpx with `ASGITransport` for testing route handlers, middleware, and lifespan without starting a real network server.

**What happens without it:** Importing `fastware.testing` raises `ImportError` immediately (httpx is imported at module level in the testing module).

```bash
pip install fastware[testing]
```

Usage:

```python
from fastware.testing import TestClient

with TestClient(app) as client:
    resp = client.get("/health")
    assert resp.status_code == 200
```

### `mcp` -- MCP server support

**Installs:** `mcp`, the Model Context Protocol SDK for building agent-compatible tool servers with stdio transport. This package provides the server framework, tool registration, and protocol handling needed by fastware's MCP integration.

**Required by:** `fastware.mcp` -- the `create_mcp_server` function for role-based agent tool provisioning. The module exports `ROLES` (a dict of role name to tool permissions) and `DEFAULT_ROLE` (the fallback role, `"auditor"`) for customization.

**What happens without it:** Importing `fastware.mcp` raises `ImportError`.

```bash
pip install fastware[mcp]
```

### `pydantic` -- Pydantic model support

**Installs:** `pydantic`, a data validation and serialization library that uses Python type annotations to define schemas. Pydantic v2 uses a Rust-based core for validation performance, and integrates with fastware via `request.json_as()` and the `response_model` route parameter.

**Required by:**

- `Request.json_as(model)` -- parses the request body as a Pydantic model, raising `HTTPError(422)` on validation failure. Without pydantic, calling `json_as` raises `ImportError`.
- `response_model` parameter on route decorators -- validates handler return values against a Pydantic model. Without pydantic, using `response_model` raises `ImportError` at request time.
- Automatic Pydantic model serialization -- returning a Pydantic `BaseModel` instance from a handler automatically calls `.model_dump(mode="json")`. Without pydantic, this detection is skipped (models lacking `.model_dump` are JSON-encoded directly by msgspec, which may fail).

```bash
pip install fastware[pydantic]
```

### `all` -- everything

**Installs:** All 6 extras groups above, pulling in every optional dependency (8 packages total) for full-featured development and testing. This is the simplest way to get started when you want access to all fastware features without picking individual extras.

```bash
pip install fastware[all]
```

This is the simplest option for getting started. For production deployments, install only the extras your application actually uses to minimize the dependency footprint.

:<: callout-note
:=:
::: fastware uses late imports for all optional dependencies. The core package (`from fastware import Router, create_app, serve`) never triggers an import of pyjwt, bcrypt, structlog, httpx, watchfiles, websockets, mcp, or pydantic. You only pay the import cost when you use the feature that requires the extra.
:>:
