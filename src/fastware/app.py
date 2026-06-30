"""ASGI application factory with middleware, static files, SPA fallback, and lifespan."""

from __future__ import annotations

import dataclasses
import logging
import mimetypes
from pathlib import Path
from typing import Any, Callable

import msgspec

from fastware.types import Scope, Receive, Send
from fastware.responses import (
    JSONResponse,
    TextResponse,
    HTMLResponse,
    BytesResponse,
    StreamResponse,
    FileResponse,
    HTTPError,
    _send_response,
    send_error,
)
from fastware.request import Request, State
from fastware.routing import Router
from fastware.websocket import WebSocket


# ---------------------------------------------------------------------------
# ASGI send helpers (stream + result dispatch)
# ---------------------------------------------------------------------------

async def _send_stream(send: Callable, resp: StreamResponse) -> None:
    """Send a streaming HTTP response, iterating the async generator."""
    headers = [
        [b"content-type", resp.content_type.encode()],
    ]
    for key, value in resp.headers.items():
        headers.append([key.encode(), value.encode()])
    for cookie in resp.cookies:
        headers.append([b"set-cookie", cookie.encode()])
    await send({"type": "http.response.start", "status": resp.status, "headers": headers})
    async for chunk in resp.generator:
        payload = chunk.encode() if isinstance(chunk, str) else chunk
        await send({"type": "http.response.body", "body": payload, "more_body": True})
    await send({"type": "http.response.body", "body": b"", "more_body": False})


def _deep_convert_pydantic(obj: Any) -> Any:
    """Recursively convert Pydantic models to plain dicts/lists.

    Walks dicts and lists, calling ``.model_dump(mode="json")`` on any
    object that has ``model_dump`` (i.e. Pydantic BaseModel instances).
    """
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if isinstance(obj, dict):
        return {k: _deep_convert_pydantic(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_deep_convert_pydantic(item) for item in obj]
    return obj


async def _send_result(send: Callable, result: Any) -> None:
    """Dispatch a handler return value to the appropriate sender."""
    # Pydantic BaseModel instances: serialize via model_dump before JSON encoding
    if hasattr(result, "model_dump"):
        result = result.model_dump(mode="json")

    # Auto-wrap plain dicts/lists as JSON responses, converting nested Pydantic models
    if isinstance(result, (dict, list)):
        result = JSONResponse(_deep_convert_pydantic(result))

    if isinstance(result, JSONResponse):
        await _send_response(
            send, result.status, msgspec.json.encode(result.data),
            "application/json", extra_headers=result.headers, cookies=result.cookies,
        )
    elif isinstance(result, TextResponse):
        await _send_response(
            send, result.status, result.text.encode(),
            result.content_type, extra_headers=result.headers, cookies=result.cookies,
        )
    elif isinstance(result, HTMLResponse):
        await _send_response(
            send, result.status, result.html.encode(),
            "text/html", extra_headers=result.headers, cookies=result.cookies,
        )
    elif isinstance(result, BytesResponse):
        await _send_response(
            send, result.status, result.data,
            result.content_type, extra_headers=result.headers, cookies=result.cookies,
        )
    elif isinstance(result, StreamResponse):
        await _send_stream(send, result)
    elif isinstance(result, FileResponse):
        content_type = result.content_type
        if content_type is None:
            mime, _ = mimetypes.guess_type(str(result.path))
            content_type = mime or "application/octet-stream"
        body = result.path.read_bytes()
        await _send_response(
            send, result.status, body, content_type,
            extra_headers=result.headers, cookies=result.cookies,
        )
    else:
        # Fallback: try JSON-encoding anything else
        await _send_response(send, 200, msgspec.json.encode(result), "application/json")


# ---------------------------------------------------------------------------
# Static file + SPA helpers
# ---------------------------------------------------------------------------

async def _serve_static(send: Callable, static_dir: Path, rel_path: str) -> bool:
    """Serve a static file. Returns True if served, False if not found."""
    file_path = (static_dir / rel_path).resolve()
    # Prevent path traversal
    if not str(file_path).startswith(str(static_dir.resolve())):
        return False
    if not file_path.is_file():
        return False
    mime, _ = mimetypes.guess_type(str(file_path))
    body = file_path.read_bytes()
    await _send_response(send, 200, body, mime or "application/octet-stream")
    return True


async def _serve_spa_fallback(send: Callable, spa_fallback: Path) -> None:
    """Serve the SPA fallback file (typically index.html)."""
    if spa_fallback.is_file():
        body = spa_fallback.read_bytes()
        await _send_response(send, 200, body, "text/html")
    else:
        await _send_response(send, 404, msgspec.json.encode({"detail": "Not found"}), "application/json")


# ---------------------------------------------------------------------------
# App configuration
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class AppConfig:
    """Configuration for :func:`create_app`.

    All fields correspond to the keyword arguments of ``create_app``.
    Pass an ``AppConfig`` instance as the *config* parameter, and/or
    supply individual keyword arguments.  Keyword arguments override
    matching fields on the config object.
    """

    middleware: list[Callable] | None = None
    static_dir: Path | None = None
    static_path: str = "/assets"
    spa_fallback: Path | None = None
    api_prefix: str | None = None
    lifespan: Callable | None = None
    name: str | None = None
    exception_handlers: dict[type, Callable] | None = None
    dependency_overrides: dict[Callable, Callable] | None = None
    cors_origins: list[str] | None = None
    trusted_hosts: list[str] | None = None
    request_id: bool = True
    request_timing: bool = True
    vite_dev_port: int | None = None


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(
    router: Router,
    config: AppConfig | None = None,
    **kwargs: Any,
) -> Callable:
    """Create an ASGI application callable.

    Accepts an optional *config* (:class:`AppConfig`) and/or keyword
    arguments.  Keyword arguments override matching fields on the config
    object.  If neither is supplied, defaults from ``AppConfig`` are used.

    If *api_prefix* is set (e.g. ``"/api"``), the SPA fallback will not
    serve index.html for paths that start with the prefix -- they fall
    through to the 404 handler instead.

    Built-in middleware (applied when their parameters are truthy):
    - ``trusted_hosts``: TrustedHostMiddleware (outermost)
    - ``vite_dev_port``: ViteDevProxy
    - ``cors_origins``: CORSMiddleware
    - ``request_id``: RequestIDMiddleware
    - ``request_timing``: RequestTimingMiddleware (innermost)

    Custom middleware supplied via *middleware* wraps after built-in
    middleware (between the app and the built-in stack).
    """
    # Build effective config: start from defaults, overlay config, overlay kwargs.
    if config is None:
        config = AppConfig()
    _field_names = {f.name for f in dataclasses.fields(AppConfig)}
    unknown = set(kwargs) - _field_names
    if unknown:
        raise TypeError(
            f"create_app() got unexpected keyword argument(s): {', '.join(sorted(unknown))}"
        )
    # Merge: kwargs override config fields.
    effective = dataclasses.replace(config, **kwargs)

    middleware = effective.middleware
    static_dir = effective.static_dir
    static_path = effective.static_path
    spa_fallback = effective.spa_fallback
    api_prefix = effective.api_prefix
    lifespan = effective.lifespan
    name = effective.name
    exception_handlers = effective.exception_handlers
    dependency_overrides = effective.dependency_overrides
    cors_origins = effective.cors_origins
    trusted_hosts = effective.trusted_hosts
    request_id = effective.request_id
    request_timing = effective.request_timing
    vite_dev_port = effective.vite_dev_port

    from fastware.di import DependencyResolver

    log = logging.getLogger(name or "fastware")
    _resolver = DependencyResolver(overrides=dependency_overrides)

    # Pre-sort exception handlers by MRO depth (most specific first) so
    # that a handler for a subclass is checked before a handler for its
    # parent.  Ties are broken by insertion order.
    _exc_handlers: list[tuple[type, Callable]] = []
    if exception_handlers:
        _exc_handlers = sorted(
            exception_handlers.items(),
            key=lambda pair: len(pair[0].__mro__),
            reverse=True,
        )
    _lifespan_state: dict[str, Any] = {}

    async def app(scope: dict, receive: Callable, send: Callable) -> None:
        nonlocal _lifespan_state

        # -- Lifespan protocol --
        if scope["type"] == "lifespan":
            message = await receive()
            if message["type"] == "lifespan.startup":
                if lifespan is not None:
                    # Enter the context manager; it stays open until shutdown
                    ctx = lifespan(app)
                    yielded = await ctx.__aenter__()
                    if isinstance(yielded, dict):
                        _lifespan_state = yielded
                    await send({"type": "lifespan.startup.complete"})
                    await receive()  # blocks until lifespan.shutdown
                    await ctx.__aexit__(None, None, None)
                else:
                    await send({"type": "lifespan.startup.complete"})
                    await receive()
                await send({"type": "lifespan.shutdown.complete"})
            return

        # -- WebSocket routing (app-scoped via router) --
        if scope["type"] == "websocket":
            # Merge lifespan state into scope for WebSocket connections
            if "state" not in scope:
                scope["state"] = {}
            scope["state"].update(_lifespan_state)

            path = scope.get("path", "")
            ws_match = router._match_ws_with_deps(path)
            if ws_match:
                ws_handler, ws_params, ws_deps = ws_match
                scope["path_params"] = ws_params
                ws = WebSocket(scope, receive, send)
                if ws_deps:
                    resolved, cleanups = await _resolver.resolve(ws_deps, ws)
                    try:
                        await ws_handler(ws, **resolved)
                    finally:
                        await DependencyResolver.cleanup(cleanups)
                else:
                    await ws_handler(ws)
            else:
                # Reject unknown WebSocket paths
                await receive()  # consume websocket.connect per ASGI spec
                await send({"type": "websocket.close", "code": 4004})
            return

        if scope["type"] != "http":
            return

        # Merge lifespan state into scope for every request
        if "state" not in scope:
            scope["state"] = {}
        scope["state"].update(_lifespan_state)

        method = scope["method"]
        path = scope["path"]

        # -- Route matching --
        match = router._match_with_deps(method, path)
        if match:
            handler, path_params, route_deps, response_model = match
            try:
                # Read body for all methods (GET with empty body costs nothing)
                body = b""
                while True:
                    msg = await receive()
                    body += msg.get("body", b"")
                    if not msg.get("more_body", False):
                        break
                body = body or None

                request = Request(scope, path_params, body, receive=receive)

                if route_deps:
                    resolved, cleanups = await _resolver.resolve(
                        route_deps, request,
                    )
                    try:
                        # Filter resolved deps to only those the handler
                        # actually accepts, so router-level deps (e.g. auth)
                        # don't cause TypeError on handlers that don't need
                        # the resolved value.
                        import inspect as _inspect
                        _sig = _inspect.signature(handler)
                        _params = _sig.parameters
                        if any(
                            p.kind == _inspect.Parameter.VAR_KEYWORD
                            for p in _params.values()
                        ):
                            filtered = resolved
                        else:
                            accepted = set(_params.keys())
                            filtered = {
                                k: v for k, v in resolved.items()
                                if k in accepted
                            }
                        result = await handler(request, **filtered)
                    finally:
                        await DependencyResolver.cleanup(cleanups)
                else:
                    result = await handler(request)

                # Apply response_model validation if configured and result
                # is a plain dict (skip if handler returned a Response type)
                if response_model is not None and isinstance(result, dict):
                    from pydantic import ValidationError
                    try:
                        validated = response_model.model_validate(result)
                        result = validated.model_dump(mode="json")
                    except ValidationError as ve:
                        log.error(
                            "response_model validation failed on %s %s: %s",
                            method, path, ve,
                        )
                        await _send_response(
                            send, 500,
                            msgspec.json.encode({"detail": str(ve)}),
                            "application/json",
                        )
                        return

                await _send_result(send, result)
            except HTTPError as exc:
                await _send_response(
                    send, exc.status_code,
                    msgspec.json.encode({"detail": exc.detail}),
                    "application/json",
                )
            except Exception as exc:
                # Check registered exception handlers (most specific first)
                handled = False
                for exc_type, exc_handler in _exc_handlers:
                    if isinstance(exc, exc_type):
                        try:
                            result = await exc_handler(request, exc)
                            await _send_result(send, result)
                            handled = True
                        except Exception:
                            log.exception(
                                "Exception handler error on %s %s",
                                method, path,
                            )
                        break
                if not handled:
                    log.exception("Handler error on %s %s", method, path)
                    await _send_response(
                        send, 500,
                        msgspec.json.encode({"detail": "Internal server error"}),
                        "application/json",
                    )
            return

        # -- Static files --
        if static_dir and path.startswith(static_path + "/"):
            rel = path[len(static_path) + 1:]
            if await _serve_static(send, static_dir, rel):
                return

        # -- SPA fallback (GET only, skip API paths) --
        if spa_fallback and method == "GET":
            # Never serve index.html for API paths -- they should 404
            if api_prefix and path.startswith(api_prefix):
                pass  # fall through to 404
            else:
                # Before returning index.html, check if the path maps to an
                # actual file under the static root (spa_fallback's parent dir).
                # This lets sub-directories be served without registering each
                # one as a separate static_path prefix.
                static_root = spa_fallback.parent
                candidate = path.lstrip("/")
                if candidate and await _serve_static(send, static_root, candidate):
                    return
                await _serve_spa_fallback(send, spa_fallback)
                return

        # -- 404 --
        await _send_response(send, 404, msgspec.json.encode({"detail": "Not found"}), "application/json")

    # -- Apply middleware in reverse so the first in the list is outermost --
    wrapped = app
    for mw in reversed(middleware or []):
        wrapped = mw(wrapped)

    # -- Built-in middleware (innermost first, outermost last) --
    from fastware.middleware import (
        CORSMiddleware as _CORSMiddleware,
        RequestIDMiddleware as _RequestIDMiddleware,
        RequestTimingMiddleware as _RequestTimingMiddleware,
        TrustedHostMiddleware as _TrustedHostMiddleware,
        ViteDevProxy as _ViteDevProxy,
    )

    if request_timing:
        wrapped = _RequestTimingMiddleware(wrapped)
    if request_id:
        wrapped = _RequestIDMiddleware(wrapped)
    if cors_origins:
        wrapped = _CORSMiddleware(wrapped, allow_origins=cors_origins)
    if vite_dev_port is not None:
        wrapped = _ViteDevProxy(wrapped, vite_port=vite_dev_port)
    if trusted_hosts:
        wrapped = _TrustedHostMiddleware(wrapped, allowed_hosts=trusted_hosts)

    return wrapped
