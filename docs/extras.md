---
title: Optional Extras
description: Guide to fastware's optional dependency groups for auth, logging, dev tools, testing, MCP, and Pydantic
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

:-: table-dep path="pyproject.toml"

## Extras groups

### `auth` -- JWT and password hashing

**Installs:** `pyjwt`, `bcrypt`

**Required by:** `fastware.auth` -- specifically `create_token`, `verify_token`, `hash_password`, `verify_password`, and everything that depends on them (`get_current_user`, `require_role`, `UserStore.create_user`).

**What happens without it:** Importing `fastware.auth` succeeds (the module uses late imports), but calling any function that needs `jwt` or `bcrypt` raises `ImportError` with a traceback pointing to the missing package. The error appears at call time, not at import time, so you can safely import `fastware.auth` in shared code without installing the extra.

```bash
pip install fastware[auth]
```

### `logging` -- structured logging

**Installs:** `structlog`

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

**Installs:** `httpx`, `watchfiles`, `websockets`

**Required by:**

- `watchfiles` -- `serve(reload=True)` for hot reload during development. Without it, `serve(reload=True)` raises `ImportError`.
- `httpx` -- `fastware.middleware.ViteDevProxy` for proxying requests to a Vite dev server. Without it, the proxy raises `ImportError` when it tries to forward a request.
- `websockets` -- `ViteDevProxy` WebSocket proxying (HMR). Without it, WebSocket proxy connections are silently closed.

```bash
pip install fastware[dev]
```

### `testing` -- test client

**Installs:** `httpx`

**Required by:** `fastware.testing` -- the `AsyncTestClient` and `TestClient` classes that wrap httpx with `ASGITransport` for testing without a real server.

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

**Installs:** `mcp`

**Required by:** `fastware.mcp` -- the `create_mcp_server` function for role-based agent tool provisioning.

**What happens without it:** Importing `fastware.mcp` raises `ImportError`.

```bash
pip install fastware[mcp]
```

### `pydantic` -- Pydantic model support

**Installs:** `pydantic`

**Required by:**

- `Request.json_as(model)` -- parses the request body as a Pydantic model, raising `HTTPError(422)` on validation failure. Without pydantic, calling `json_as` raises `ImportError`.
- `response_model` parameter on route decorators -- validates handler return values against a Pydantic model. Without pydantic, using `response_model` raises `ImportError` at request time.
- Automatic Pydantic model serialization -- returning a Pydantic `BaseModel` instance from a handler automatically calls `.model_dump(mode="json")`. Without pydantic, this detection is skipped (models lacking `.model_dump` are JSON-encoded directly by msgspec, which may fail).

```bash
pip install fastware[pydantic]
```

### `all` -- everything

**Installs:** All of the above extras.

```bash
pip install fastware[all]
```

This is the simplest option for getting started. For production deployments, install only the extras your application actually uses to minimize the dependency footprint.

:<: callout-note
:=:
::: fastware uses late imports for all optional dependencies. The core package (`from fastware import Router, create_app, serve`) never triggers an import of pyjwt, bcrypt, structlog, httpx, watchfiles, websockets, mcp, or pydantic. You only pay the import cost when you use the feature that requires the extra.
:>:
