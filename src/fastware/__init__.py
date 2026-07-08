"""A fast, batteries-included ASGI framework. The FastAPI alternative."""

from __future__ import annotations

import importlib.metadata

from fastware.app import AppConfig, create_app
from fastware.di import DependencyResolver
from fastware.request import Request, State
from fastware.responses import (
    BytesResponse,
    FileResponse,
    HTMLResponse,
    HTTPError,
    JSONResponse,
    StreamResponse,
    TextResponse,
    delete_cookie,
    send_error,
    set_cookie,
)
from fastware.routing import Router
from fastware.sse import Broadcaster, sse_route

# -- Tier 1: Core symbols (eager imports -- lightweight modules) -------------
from fastware.types import Receive, Scope, Send
from fastware.websocket import WebSocket, WebSocketDisconnect

__version__ = importlib.metadata.version("fastware")

# -- Tier 1: Server symbols (lazy -- granian is ~60ms to import) ------------

_SERVER_SYMBOLS = {
    "check_already_running",
    "ensure_port_available",
    "read_port_file",
    "serve_background",
    "serve",
    "stop",
    "status",
    "ServerStatus",
    "PortInUseError",
    "AlreadyRunningError",
}


def __getattr__(name: str) -> object:
    if name in _SERVER_SYMBOLS:
        from fastware import server
        return getattr(server, name)
    raise AttributeError(f"module 'fastware' has no attribute {name!r}")


# -- Tier 2+: Feature modules stay in sub-modules ---------------------------
# fastware.auth, fastware.middleware, fastware.logging, fastware.testing,
# fastware.features, fastware.audit, fastware.error_log, fastware.tasks,
# fastware.config, fastware.mcp, fastware.dev

__all__ = [
    # types
    "Scope",
    "Receive",
    "Send",
    # responses
    "set_cookie",
    "delete_cookie",
    "HTTPError",
    "JSONResponse",
    "TextResponse",
    "HTMLResponse",
    "BytesResponse",
    "StreamResponse",
    "FileResponse",
    "send_error",
    # request
    "State",
    "Request",
    # routing
    "Router",
    # websocket
    "WebSocket",
    "WebSocketDisconnect",
    # app
    "AppConfig",
    "create_app",
    # di
    "DependencyResolver",
    # sse
    "Broadcaster",
    "sse_route",
    # server (lazy)
    "check_already_running",
    "ensure_port_available",
    "read_port_file",
    "serve_background",
    "serve",
    "stop",
    "status",
    "ServerStatus",
    "PortInUseError",
    "AlreadyRunningError",
    # metadata
    "__version__",
]
