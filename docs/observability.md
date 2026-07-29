---
title: Observability
description: "fastware observability guide: structured logging with structlog, Sentry error tracking, SQLite-backed 5xx error log, and JSONL audit logging."
date: 2026-07-01
---

# Observability

fastware provides four complementary observability systems: structured logging via structlog, Sentry integration for error tracking, an SQLite-backed error log for 5xx responses, and a JSONL audit log for application events. Each system is optional and independent -- use one, some, or all depending on your needs.

| System | Purpose | Storage | Module |
|---|---|---|---|
| Structured logging | Runtime log output (JSON in production, colored console in dev) | stderr | `fastware.logging` |
| Sentry | External error tracking with alerting | Sentry cloud | `fastware.logging` |
| Error log | Persistent 5xx error records for dashboards and post-mortems | SQLite file | `fastware.error_log` |
| Audit log | Append-only event trail for compliance and debugging | JSONL file | `fastware.audit` |

## Structured logging with structlog

### Setup

Call `configure_logging()` once at startup, typically in a lifespan handler:

```python
from fastware.logging import configure_logging, get_logger

configure_logging()
```

Output format is auto-detected: JSON lines when stderr is not a TTY (production), colored console output when it is (development). Override with the `json_output` parameter:

```python
configure_logging(json_output=True)   # always JSON
configure_logging(json_output=False)  # always colored console
```

structlog is an optional dependency. Install with `pip install fastware[logging]`.

### Getting loggers

Use `get_logger()` to obtain a bound structlog logger. The optional `component` argument adds a `component` key to every log entry, making it easy to filter logs by subsystem:

```python
logger = get_logger("auth")
logger.info("login attempt", username="alice")
```

Additional key-value pairs passed to `get_logger()` are permanently bound to the logger:

```python
logger = get_logger("billing", tenant_id="acme")
logger.info("invoice created", amount=42.50)
# Every entry from this logger includes tenant_id="acme"
```

### What structlog adds to every entry

The configured processor chain adds several fields automatically:

| Field | Source | Example |
|---|---|---|
| `log_level` | `add_log_level` | `"info"` |
| `timestamp` | `TimeStamper(fmt="iso")` | `"2026-07-28T14:30:00.123456Z"` |
| `module` | `CallsiteParameterAdder` | `"auth"` |
| `func_name` | `CallsiteParameterAdder` | `"login"` |
| `lineno` | `CallsiteParameterAdder` | `47` |
| `request_id` | `merge_contextvars` (when `RequestIDMiddleware` is active) | `"a1b2c3d4-..."` |

The `merge_contextvars` processor pulls in any values bound via `structlog.contextvars.bind_contextvars()`. The `RequestIDMiddleware` binds the request ID automatically, so all log entries within a request include it without explicit passing.

### JSON output example

In production (non-TTY stderr), log entries are single JSON lines:

```json
{"log_level": "info", "timestamp": "2026-07-28T14:30:00.123456Z", "module": "auth", "func_name": "login", "lineno": 47, "request_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890", "component": "auth", "event": "login attempt", "username": "alice"}
```

### Integration with RequestIDMiddleware

When both `configure_logging()` and `RequestIDMiddleware` are active, every log entry within a request automatically includes the `request_id` field. The middleware clears contextvars at the start of each request to prevent context leaks from previous requests, then binds the request ID:

```python
from fastware import create_app, Router, AppConfig

router = Router()
app = create_app(router, config=AppConfig(
    request_id=True,  # enables RequestIDMiddleware
))
```

No additional configuration is needed -- the middleware detects structlog and binds the request ID to contextvars automatically.

### Integration with RequestTimingMiddleware

When structlog is available, the `RequestTimingMiddleware` emits a debug-level log entry for every HTTP request with method, path, status code, and duration:

```json
{"log_level": "debug", "component": "request", "event": "request", "method": "POST", "path": "/api/deploy", "status": 200, "duration_ms": 42.5}
```

## Sentry integration

### Setup

`init_sentry()` initializes the Sentry SDK with ASGI-compatible defaults. If `sentry-sdk` is not installed, the call is a no-op -- safe to call unconditionally:

```python
from fastware.logging import init_sentry

init_sentry(dsn="https://examplePublicKey@o0.ingest.sentry.io/0")
```

Additional keyword arguments are forwarded to `sentry_sdk.init()`:

```python
init_sentry(
    dsn="https://...",
    traces_sample_rate=0.1,
    environment="production",
)
```

### 4xx filtering

The `before_send` hook automatically drops Sentry events caused by `HTTPError` exceptions with 4xx status codes. These are expected client errors (bad request, unauthorized, not found) and are not actionable server issues. Only 5xx errors and unhandled exceptions reach Sentry.

## SQLite error log

The `ErrorLog` class provides a persistent, queryable record of 5xx server errors. It is designed for dashboard display and post-mortem investigation -- every 5xx response is captured with enough context to diagnose the failure without digging through log streams.

### Setup

Create an `ErrorLog` instance and pass it to `create_app` via `AppConfig`:

```python
from fastware.error_log import ErrorLog
from fastware import create_app, Router, AppConfig

error_log = ErrorLog("errors.db")

router = Router()
app = create_app(router, config=AppConfig(
    error_log=error_log,
))
```

The `RequestTimingMiddleware` (enabled by default) detects the error log and calls `error_log.append()` on every 5xx response. The SQLite database is created on first write if it does not exist.

### What it captures

Each error entry contains:

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER | Auto-incrementing primary key |
| `timestamp` | REAL | Unix timestamp (captured at `append()` time) |
| `method` | TEXT | HTTP method (GET, POST, etc.) |
| `path` | TEXT | Request path |
| `status_code` | INTEGER | HTTP status code (500, 502, etc.) |
| `detail` | TEXT | Error description |
| `request_id` | TEXT | Request ID from `RequestIDMiddleware` (if active) |
| `user` | TEXT | User identifier (if available) |
| `traceback` | TEXT | Python traceback (if available) |

### Non-blocking writes

`ErrorLog.append()` never touches SQLite on the calling thread. It enqueues the entry to a FIFO queue, and a single dedicated background thread performs `INSERT`/`commit` off the ASGI event loop. This keeps the event loop responsive even during 5xx bursts.

The timestamp is captured at `append()` time (not write time), so entry ordering reflects the true event order regardless of write latency.

### Querying recent errors

The `recent()` method returns the most recent errors as a list of dicts, newest first:

```python
errors = error_log.recent(limit=20)
for err in errors:
    print(f"{err['method']} {err['path']} -> {err['status_code']}")
```

`recent()` flushes the queue before reading, so the result always reflects every preceding `append()`. This makes it safe to use in admin dashboard endpoints:

```python
@router.get("/admin/errors")
async def list_errors(request):
    return error_log.recent(limit=50)
```

### Manual writes

You can append errors directly for cases not covered by the middleware (background tasks, custom error handlers):

```python
error_log.append(
    method="POST",
    path="/api/deploy",
    status_code=500,
    detail="Docker timeout after 30s",
    request_id="abc-123",
    user="admin@example.com",
    traceback=traceback.format_exc(),
)
```

### Lifecycle

Call `error_log.close()` during shutdown to gracefully stop the background writer after draining pending writes:

```python
async def lifespan(app):
    yield
    error_log.close()
```

If the background worker encounters an error (e.g., invalid database path), it is surfaced on the next call to `flush()` or `recent()` -- never silently swallowed.

## JSONL audit log

The `AuditLog` class provides an append-only event trail for recording application actions. Each entry is a single JSON line with an ISO timestamp, event type, and optional payload. Use it for compliance, debugging, and understanding what happened in production.

### Setup

```python
from fastware.audit import AuditLog

audit = AuditLog("audit.jsonl")
```

The JSONL file is created on first write. Parent directories are created automatically.

### Logging events

Call `audit.log()` with an event type and an optional payload dict:

```python
audit.log("user.login", {"user_id": "alice", "ip": "192.168.1.1"})
audit.log("deployment.started", {"version": "1.2.3", "target": "production"})
audit.log("config.changed", {"key": "rate_limit", "old": 100, "new": 200})
```

Events without a payload omit the `payload` field:

```python
audit.log("system.startup")
```

### Entry format

Each line in the JSONL file is a self-contained JSON object:

```json
{"timestamp":"2026-07-28T14:30:00.123456+00:00","event_type":"user.login","payload":{"user_id":"alice","ip":"192.168.1.1"}}
{"timestamp":"2026-07-28T14:30:01.456789+00:00","event_type":"system.startup"}
```

Fields:

| Field | Always present | Description |
|---|---|---|
| `timestamp` | Yes | ISO 8601 UTC timestamp |
| `event_type` | Yes | Dot-namespaced event identifier |
| `payload` | Only when provided | Arbitrary dict of event-specific data |

JSON is serialized with compact separators (no spaces) for minimal file size.

### Thread safety

Writes are serialized via `LockedFileWriter` -- a `threading.Lock` ensures concurrent callers never interleave bytes. The lock is per-`AuditLog` instance, so multiple `AuditLog` instances writing to different files do not block each other.

### Querying audit logs

JSONL files are line-oriented and can be processed with standard tools:

```bash
# All login events
grep '"event_type":"user.login"' audit.jsonl

# Events from the last hour (using jq)
jq -s '[.[] | select(.timestamp > "2026-07-28T13:30:00")]' audit.jsonl

# Count events by type
jq -r '.event_type' audit.jsonl | sort | uniq -c | sort -rn
```

Or in Python:

```python
import json
from pathlib import Path

entries = [
    json.loads(line)
    for line in Path("audit.jsonl").read_text().splitlines()
    if line.strip()
]
logins = [e for e in entries if e["event_type"] == "user.login"]
```

## How these systems work together

In a production deployment, these four systems form layers with complementary coverage:

**Request flow:**

1. `RequestIDMiddleware` assigns a UUID to every request and binds it to structlog contextvars
2. `RequestTimingMiddleware` records method, path, status, and duration for every request
3. On 5xx: the timing middleware writes to `ErrorLog` (SQLite) for persistent dashboard access
4. On 5xx: Sentry receives the exception with full stack trace for alerting and triage
5. On 4xx: Sentry's `before_send` filter drops the event (not actionable)
6. structlog emits a JSON line to stderr for every request (debug level) and any explicit logger calls
7. `AuditLog` records domain events (logins, deployments, config changes) as called by application code

**What each system is best at:**

- **structlog** -- real-time observability, log aggregation (ELK/Loki/Datadog), request-level tracing via request ID correlation
- **Sentry** -- alerting, error grouping, release tracking, stack traces with local variables
- **ErrorLog** -- self-hosted 5xx dashboard without external dependencies, post-mortem queries on the SQLite file
- **AuditLog** -- compliance trail, "what happened and when" for domain events, portable JSONL format

### Complete setup example

```python
from fastware import Router, create_app, AppConfig, serve
from fastware.logging import configure_logging, init_sentry, get_logger
from fastware.error_log import ErrorLog
from fastware.audit import AuditLog

# -- Observability -----------------------------------------------------------

configure_logging()
init_sentry(dsn="https://...")

error_log = ErrorLog("data/errors.db")
audit = AuditLog("data/audit.jsonl")
logger = get_logger("app")

# -- Routes ------------------------------------------------------------------

router = Router()

@router.post("/api/deploy")
async def deploy(request):
    body = await request.json()
    audit.log("deployment.started", {
        "version": body["version"],
        "user": request.state.get("user", "anonymous"),
    })
    logger.info("deployment started", version=body["version"])
    # ... deployment logic ...
    return {"status": "ok"}

@router.get("/admin/errors")
async def list_errors(request):
    return error_log.recent(limit=50)

# -- App ---------------------------------------------------------------------

async def lifespan(app):
    logger.info("server starting")
    audit.log("system.startup")
    yield
    error_log.close()
    audit.log("system.shutdown")
    logger.info("server stopped")

app = create_app(router, config=AppConfig(
    request_id=True,
    error_log=error_log,
    lifespan=lifespan,
))

if __name__ == "__main__":
    serve(app, foreground=True, host="0.0.0.0", port=8000)
```

## API reference

### fastware.logging

:-: ref path="src.fastware.logging"

### fastware.error_log

:-: ref path="src.fastware.error_log"

### fastware.audit

:-: ref path="src.fastware.audit"
