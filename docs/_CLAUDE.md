---
title: CLAUDE.md
description: Developer guide for AI agents working on the fastware codebase
date: 2026-07-01
---

# fastware

:-: var key="project.description"

## Architecture

fastware is a modular ASGI framework extracted from a monolithic `asgi.py` into purpose-specific modules. Each module has a single responsibility and minimal internal dependencies.

- **types.py** -- ASGI type aliases (Scope, Receive, Send). Zero-dependency leaf module used by the rest of the framework.
- **responses.py** -- 6 response classes (JSONResponse, TextResponse, HTMLResponse, BytesResponse, StreamResponse, FileResponse), cookie helpers (set_cookie, delete_cookie), HTTPError exception, and the low-level `send_error` function for middleware.
- **request.py** -- Request wrapper with lazy JSON body parsing, typed query parameter extraction with constraints (ge/le/min_length/max_length), cookie access, header lookup, State object with dual attribute/dict access, and `json_as()` for Pydantic model parsing.
- **routing.py** -- Path-based Router with `{param}`, `{param:int}`, and `{param:path}` placeholder syntax. Method decorators (get/post/put/patch/delete), WebSocket route registration, sub-router inclusion with prefix and deps, and sub-app mounting.
- **websocket.py** -- WebSocket helper wrapping the raw ASGI triple. JSON send/receive via msgspec. WebSocketDisconnect exception with close code.
- **app.py** -- AppConfig dataclass and `create_app` factory. The hub module that wires routing, DI resolution, response dispatch, static file serving, SPA fallback, lifespan, exception handlers, response_model validation, and built-in middleware (CORS, RequestID, RequestTiming, TrustedHost, ViteDevProxy).
- **sse.py** -- SSE Broadcaster with typed event registration (strict mode by default), per-client async queues, automatic disconnected-client pruning, and optional heartbeat interval.
- **middleware.py** -- Pure ASGI middleware classes: CORSMiddleware, RequestIDMiddleware (with structlog contextvars binding), RequestTimingMiddleware (with ring buffer and ErrorLog integration), TrustedHostMiddleware, and ViteDevProxy (backend-first HTTP routing, prefix-based WebSocket routing).
- **server.py** -- Granian lifecycle management: PID file write/cleanup, port availability checks (with stale instance recovery), process group leadership for clean subprocess termination, `serve()` (foreground/background/reload modes), `serve_background()` (detached subprocess), `stop()`, and `status()`.
- **di.py** -- DependencyResolver for per-request dependency injection. Supports sync/async factories, sync/async generator factories (yield pattern with cleanup), per-request caching, and dependency overrides for testing.
- **auth.py** -- JWT token creation/verification (HS256 via PyJWT), bcrypt password hashing, UserStore interface with JSONFileUserStore implementation, `get_current_user` DI factory (Bearer header / session cookie / query param), `require_role` factory, CSRFMiddleware (double-submit cookie pattern with Bearer exemption), session cookie helpers, and token-bucket `rate_limit` decorator.
- **testing.py** -- AsyncTestClient and TestClient (sync wrapper with background event loop) using httpx ASGITransport. No real server needed for tests.
- **logging.py** -- structlog configuration with auto-detected JSON/console output, `get_logger` with component binding, and `init_sentry` with 4xx HTTPError filtering.
- **tasks.py** -- BackgroundTask protocol and TaskRegistry with feature-gated lifecycle (start_all/stop_all).
- **features.py** -- FeatureFlags with boolean defaults and optional JSON file overrides. Thread-safe with reload support.
- **error_log.py** -- SQLite-backed append-only error log for 5xx responses. Thread-safe writes with recent() query.
- **audit.py** -- JSONL append-only audit log with ISO timestamps and event types. Thread-safe.
- **config.py** -- TOML config loader with optional Pydantic schema validation.
- **mcp.py** -- MCP server factory with configurable role-based tool provisioning. Roles dict and default_role are parameters, not hardcoded constants.
- **dev.py** -- Development mode orchestrator: spawns Vite as a subprocess, waits for readiness, wraps the ASGI app with ViteDevProxy, and runs `serve()` in foreground. Clean Vite shutdown on exit.

## Module Layout

:-: list-modules path="src/fastware/"

## Dependency Graph

Internal import structure, from leaves to hub:

- **Leaf modules** (zero internal imports): types, di, config, features, error_log, audit, testing, mcp, routing, websocket
- **One-dep modules**:
    - request depends on responses (for HTTPError)
    - middleware depends on types and responses (for send_error)
    - auth depends on responses (for HTTPError, send_error, set_cookie, delete_cookie)
    - logging depends on responses (lazy import for HTTPError in Sentry filter)
    - tasks depends on features (TYPE_CHECKING import only)
    - sse depends on request and responses
- **Hub module**: app imports from types, responses, request, routing, websocket at module level, and lazily imports di and middleware inside `create_app()`
- **dev** imports middleware (ViteDevProxy) and server lazily inside the `dev()` function

## Dependencies

:-: table-dep path="pyproject.toml"

## Optional Extras

| Extra | Packages | Required by | Enables |
|-------|----------|-------------|---------|
| `[auth]` | pyjwt, bcrypt | `fastware.auth` | `create_token`, `verify_token`, `hash_password`, `verify_password` |
| `[logging]` | structlog | `fastware.logging` | `configure_logging`, `get_logger`; also enables contextvars binding in RequestIDMiddleware and structured logging in RequestTimingMiddleware |
| `[dev]` | httpx, watchfiles, websockets | `fastware.dev`, `fastware.middleware` (ViteDevProxy), `fastware.server` (reload mode) | ViteDevProxy HTTP proxying and WebSocket proxy (HMR), `serve(reload=True)` |
| `[testing]` | httpx | `fastware.testing` | AsyncTestClient, TestClient |
| `[mcp]` | mcp | `fastware.mcp` | `create_mcp_server` |
| `[pydantic]` | pydantic | `fastware.request` (`json_as`), `fastware.app` (response_model), `fastware.config` (schema validation) | Request body parsing as Pydantic models, response_model validation, typed config loading |
| `[all]` | all of the above | -- | Everything |

## Public API

The export system has two tiers:

**Tier 1 -- `from fastware import X` (core symbols, eagerly imported):**

- Router -- routing
- Request, State -- request handling
- WebSocket, WebSocketDisconnect -- WebSocket connections
- JSONResponse, TextResponse, HTMLResponse, BytesResponse, StreamResponse, FileResponse -- response types
- HTTPError, send_error -- error handling
- set_cookie, delete_cookie -- cookie helpers
- create_app, AppConfig -- app factory and configuration
- DependencyResolver -- dependency injection
- Broadcaster, sse_route -- SSE support
- Scope, Receive, Send -- ASGI type aliases

**Tier 1 -- lazy (via `__getattr__`, loaded on first access to avoid granian import overhead):**

- serve, serve_background, stop, status -- server lifecycle
- ServerStatus, PortInUseError, AlreadyRunningError -- server types
- check_already_running, ensure_port_available, read_port_file -- server utilities

**Tier 2 -- sub-module imports only (`from fastware.X import Y`):**

- `fastware.auth` -- JWT, passwords, CSRF, rate limiting, sessions
- `fastware.middleware` -- CORS, RequestID, RequestTiming, TrustedHost, ViteDevProxy
- `fastware.logging` -- structlog config, Sentry integration
- `fastware.testing` -- AsyncTestClient, TestClient
- `fastware.features` -- FeatureFlags
- `fastware.audit` -- AuditLog
- `fastware.error_log` -- ErrorLog
- `fastware.tasks` -- BackgroundTask, TaskRegistry
- `fastware.config` -- load_config
- `fastware.mcp` -- create_mcp_server, register_tools_for_role, ROLES
- `fastware.dev` -- dev()

## Development

```bash
uv sync --all-extras        # Install all dependencies including optional extras
uv run pytest               # Run test suite
selfdoc build               # Build documentation site
selfdoc check               # Lint docs for SEO and staleness
```

## Conventions

- **msgspec for all JSON serialization.** JSON encoding/decoding throughout the ASGI layer uses msgspec, never `json` from stdlib. This applies to JSONResponse dispatch, Request.json parsing, WebSocket.send_json/receive_json, and send_error.
- **Lazy imports for optional dependencies.** Modules that depend on optional packages (pydantic, structlog, pyjwt, bcrypt, mcp, sentry-sdk, httpx, watchfiles, websockets) import them inside functions, not at module level. This ensures `import fastware` works with only the core dependencies (msgspec, granian).
- **Server raises exceptions, never sys.exit().** PortInUseError and AlreadyRunningError are raised for programmatic handling. Callers decide how to present errors.
- **ViteDevProxy backend_prefixes is configurable.** Default is `["/events"]`. Consumers pass their own list if they have different SSE/backend paths.
- **MCP ROLES is configurable via parameter.** Both `create_mcp_server` and `register_tools_for_role` accept `roles` and `default_role` parameters, falling back to module-level constants only when not provided.
- **DI factories are introspected.** The resolver checks whether a factory accepts a `request` parameter. Zero-arg factories (useful for test overrides) are called without arguments.
- **Response dispatch auto-wraps.** Plain dicts/lists returned from handlers are auto-wrapped as JSONResponse. Pydantic BaseModel instances are serialized via `model_dump(mode="json")`. Nested Pydantic models inside dicts/lists are recursively converted.
- **SSE strict mode by default.** Broadcaster requires event types to be registered via `register_event()` before `broadcast()`. Pass `strict=False` to disable.
- **TestClient class naming.** The sync test client is internally named `_SyncTestClient` (prefixed with underscore) to prevent pytest from treating it as a test class. It is exported as `TestClient`.

## Relationship to wesktop

fastware was extracted from wesktop's `asgi.py` monolith. wesktop re-exports all fastware symbols from its `__init__.py`, so consumers using wesktop see a unified API. wesktop adds four modules on top of fastware:

- **desktop.py** -- pywebview integration for native OS windows (late-imports pywebview)
- **entries.py** -- cross-platform desktop shortcut creation and removal
- **cli.py** -- strictcli-based CLI (diagnose, config subcommands)
- **sdui.py** -- server-driven UI primitives

wesktop's ASGI layer, SSE, server, middleware, DI, auth, logging, testing, features, tasks, error_log, audit, config, mcp, and dev modules are all provided by fastware.
