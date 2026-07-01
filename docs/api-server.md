---
title: Server API Reference
description: "API reference for fastware server: Granian ASGI lifecycle management with foreground and background serving, PID files, port checks, and hot reload."
date: 2026-07-01
---

# Server API Reference

The server module manages the Granian ASGI server lifecycle: PID file management, port availability checks, single-instance enforcement, background and foreground serving, hot reload, and graceful shutdown.

Server symbols are lazily imported from the top-level `fastware` package to avoid the ~60ms cost of importing Granian when only the routing/response layer is needed.

:-: ref path="src.fastware.server"

## ServerStatus

The `ServerStatus` enum represents the 3 possible states of a fastware server instance: running, stopped, or unknown. It is returned by `status()` after checking the PID file and probing the process.

:-: table-schema path="src/fastware/server.py" target="ServerStatus"

## Error Types

### PortInUseError

Raised by `ensure_port_available` when the requested port is already in use and cannot be reclaimed. The error message includes the host and port, and suggests using a different port or stopping the process holding it.

Before raising, `ensure_port_available` attempts to detect whether the port holder is a stale instance of the same server (by probing `GET /health`). If it is, the stale process is killed automatically and the port is reclaimed without error.

### AlreadyRunningError

Raised by `serve` (when `single_instance=True`) if a PID file exists and the corresponding process is still alive. The error message includes the PID of the running instance. This prevents accidentally starting duplicate servers on the same port, which would cause bind failures or silent request splitting.

Stale PID files (where the process has died) are cleaned up automatically and do not trigger this error.

## serve() vs serve_background()

`serve()` is the primary entry point for starting the Granian ASGI server. It supports both foreground (blocking) and background (daemon thread) modes, with optional PID file management, single-instance enforcement, and hot reload for development workflows:

```python
from fastware.server import serve

# Foreground mode -- blocks until the server stops
serve(
    "myapp:app",
    foreground=True,
    host="127.0.0.1",
    port=8000,
    pid_path=Path(".myapp.pid"),
)

# Background mode -- returns the URL, server runs in a daemon thread
url = serve(
    "myapp:app",
    foreground=False,
    host="127.0.0.1",
    port=8000,
)
# url == "http://127.0.0.1:8000"
```

`serve_background()` spawns a fully detached subprocess that survives the parent process exiting. Use this when you need the server to outlive the caller (e.g., starting a server from a CLI command):

```python
from fastware.server import serve_background

# Detached subprocess -- survives parent exit
url = serve_background(
    "myapp:app",
    host="127.0.0.1",
    port=8000,
    pid_path=Path(".myapp.pid"),
)
```

| Feature | `serve(foreground=False)` | `serve_background()` |
|---------|--------------------------|----------------------|
| Process lifetime | Dies with parent (daemon thread) | Survives parent exit (subprocess) |
| Use case | Desktop apps, test harnesses | CLI start/stop commands |
| Returns | URL string | URL string |
| PID file | Optional | Required |

## Hot Reload

Pass `reload=True` to `serve()` for development. This uses `watchfiles` to monitor all `.py` files in the project directory and automatically restart the Granian server on changes, providing sub-second feedback during development. Requires `foreground=True`:

```python
serve(
    "myapp:app",
    foreground=True,
    host="127.0.0.1",
    port=8000,
    reload=True,
)
```
