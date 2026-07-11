"""ASGI application factory with middleware chain composition, static file serving, SPA fallback routing, async lifespan hooks, and WebSocket support."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import inspect
import logging
import mimetypes
from pathlib import Path
from typing import Any, Callable

import msgspec

from fastware.request import Request
from fastware.responses import (
    BytesResponse,
    FileResponse,
    HTMLResponse,
    HTTPError,
    JSONResponse,
    StreamResponse,
    TextResponse,
    _build_headers,
    _send_response,
)
from fastware.routing import Router
from fastware.websocket import WebSocket, WebSocketDisconnect

__all__ = [
    "AppConfig",
    "create_app",
]


# ---------------------------------------------------------------------------
# ASGI send helpers (stream + result dispatch)
# ---------------------------------------------------------------------------

_log = logging.getLogger("fastware")


async def _disconnect_watcher(receive: Callable, request: Request) -> None:
    """Own the ASGI receive channel for a request's post-body lifetime.

    A single watcher task is the only consumer of ``receive`` once the body
    has been read. It blocks on ``receive()`` and, on ``http.disconnect``,
    records the fact on the request (which also cancels any in-flight
    stream-driving task) and returns. This makes disconnects observable even
    when the server never surfaces them as a ``send()`` failure -- the failure
    mode that caused generators to leak forever.

    Non-disconnect messages are ignored; a well-behaved ASGI server sends only
    ``http.disconnect`` after the body. The ``sleep(0)`` yields control so a
    test double that replays the same message cannot spin the loop.
    """
    while True:
        message = await receive()
        if message.get("type") == "http.disconnect":
            request._mark_disconnected()
            return
        await asyncio.sleep(0)


async def _send_stream(
    send: Callable, resp: StreamResponse, request: Request | None = None
) -> None:
    """Send a streaming HTTP response, driving the generator in a child task.

    The generator is iterated in a CHILD task so the request's disconnect
    watcher can cancel it the instant the client goes away. granian frequently
    never raises from ``send()`` on disconnect, so a generator blocked between
    yields would otherwise leak forever and its ``finally`` cleanup would never
    run. ``resp.generator.aclose()`` is guaranteed in every path: normal
    completion, generator error, send failure, and disconnect cancellation.

    Send failures (client vanished mid-stream) are swallowed cleanly -- the
    response has already started and cannot be changed. Generator errors are
    NOT swallowed; they propagate to the caller (which must not attempt a
    second response after start).
    """
    headers = _build_headers(resp.content_type, resp.headers, resp.cookies)

    async def _drive() -> None:
        async for chunk in resp.generator:
            payload = chunk.encode() if isinstance(chunk, str) else chunk
            try:
                await send({"type": "http.response.body", "body": payload, "more_body": True})
            except Exception:
                # Client disconnected mid-stream (send failed). Belt-and-braces
                # alongside the watcher for servers that only surface disconnect
                # this way. CancelledError is a BaseException, not Exception, so
                # watcher-driven cancellation is never swallowed here.
                _log.debug("Client disconnected during streaming response; stopping stream")
                return
        try:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        except Exception:
            _log.debug("Client disconnected before streaming response finished")

    # The initial http.response.start send is inside this try so its failure
    # (client gone before the first byte) still runs aclose() in the finally --
    # otherwise a generator suspended at its first yield would leak. If start
    # fails, no child task is ever created, so the inner await/cancel dance is
    # skipped and the exception propagates after aclose() runs.
    try:
        await send(
            {"type": "http.response.start", "status": resp.status, "headers": headers}
        )

        child = asyncio.create_task(_drive())
        if request is not None:
            request._register_stream_task(child)
        try:
            await child
        except asyncio.CancelledError:
            # The watcher cancels the child on disconnect. A generator that
            # catches CancelledError and returns normally settles the child
            # without error (we never reach here); one that lets it propagate
            # lands here. Settle the child, then re-raise only for a genuine
            # external cancellation of the request task -- swallow when this is
            # a client disconnect.
            child.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await child
            if request is None or not request._disconnected:
                raise
    finally:
        with contextlib.suppress(Exception):
            await resp.generator.aclose()


def _accepted_params(handler: Callable) -> frozenset[str] | None:
    """Return the set of keyword names *handler* accepts, or None if it
    takes ``**kwargs`` (accepts everything)."""
    params = inspect.signature(handler).parameters
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return None
    return frozenset(params)


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


async def _send_result(send: Callable, result: Any, request: Request | None = None) -> None:
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
        await _send_stream(send, result, request)
    elif isinstance(result, FileResponse):
        content_type = result.content_type
        if content_type is None:
            mime, _ = mimetypes.guess_type(str(result.path))
            content_type = mime or "application/octet-stream"
        # Offload the blocking file read so it doesn't stall the event loop
        body = await asyncio.to_thread(result.path.read_bytes)
        await _send_response(
            send, result.status, body, content_type,
            extra_headers=result.headers, cookies=result.cookies,
        )
    else:
        # Fallback: try JSON-encoding anything else
        await _send_response(send, 200, msgspec.json.encode(result), "application/json")


# ---------------------------------------------------------------------------
# Mounted sub-app lifespan driver
# ---------------------------------------------------------------------------

class _MountLifespan:
    """Drives the ASGI lifespan protocol for one mounted sub-app.

    The sub-app runs in its own task with private message queues. Per the
    ASGI lifespan spec, state the sub-app sets on the lifespan scope's
    ``state`` dict is carried into every request scope forwarded to it.
    Sub-apps that finish (raise or return) without sending any lifespan
    message are treated as not supporting lifespan, matching the server
    convention (e.g. uvicorn).
    """

    # Internal sentinels (never sent to the server):
    # __unsupported__ -- app finished without speaking lifespan
    # __error__       -- app raised after speaking lifespan
    # __exited__      -- app returned cleanly after speaking lifespan

    def __init__(self, mount_app: Any) -> None:
        self.app = mount_app
        self.state: dict[str, Any] = {}
        self.supports_lifespan = True
        self._to_app: asyncio.Queue = asyncio.Queue()
        self._from_app: asyncio.Queue = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._spoke = False

    async def _app_send(self, message: dict) -> None:
        self._spoke = True
        await self._from_app.put(message)

    async def _run(self) -> None:
        scope = {
            "type": "lifespan",
            "asgi": {"version": "3.0", "spec_version": "2.0"},
            "state": self.state,
        }
        try:
            await self.app(scope, self._to_app.get, self._app_send)
        except BaseException as exc:
            if self._spoke:
                await self._from_app.put({"type": "__error__", "message": str(exc)})
            else:
                await self._from_app.put({"type": "__unsupported__"})
        else:
            await self._from_app.put(
                {"type": "__exited__"} if self._spoke else {"type": "__unsupported__"}
            )

    async def startup(self) -> str | None:
        """Send lifespan.startup; returns an error message on failure, or
        None on success (including apps that don't support lifespan)."""
        self._task = asyncio.create_task(self._run())
        await self._to_app.put({"type": "lifespan.startup"})
        msg = await self._from_app.get()
        msg_type = msg["type"]
        if msg_type == "lifespan.startup.complete":
            return None
        if msg_type == "__unsupported__":
            self.supports_lifespan = False
            return None
        if msg_type in ("lifespan.startup.failed", "__error__"):
            return msg.get("message", "")
        return f"unexpected lifespan message from mounted app: {msg_type}"

    async def shutdown(self) -> str | None:
        """Send lifespan.shutdown; returns an error message or None."""
        if self._task is None or not self.supports_lifespan:
            return None
        await self._to_app.put({"type": "lifespan.shutdown"})
        msg = await self._from_app.get()
        msg_type = msg["type"]
        if msg_type in ("lifespan.shutdown.complete", "__exited__"):
            result = None
        elif msg_type in ("lifespan.shutdown.failed", "__error__"):
            result = msg.get("message", "")
        else:
            result = f"unexpected lifespan message from mounted app: {msg_type}"
        await self._task
        return result


# ---------------------------------------------------------------------------
# Static file + SPA helpers
# ---------------------------------------------------------------------------

async def _serve_static(send: Callable, static_dir: Path, rel_path: str) -> bool:
    """Serve a static file. Returns True if served, False if not found."""
    root = static_dir.resolve()
    file_path = (root / rel_path).resolve()
    # Prevent path traversal. A string-prefix check would let sibling dirs
    # escape (e.g. /srv/static-private passes startswith("/srv/static")),
    # so compare resolved paths structurally.
    if not file_path.is_relative_to(root):
        return False
    if not file_path.is_file():
        return False
    mime, _ = mimetypes.guess_type(str(file_path))
    # Offload the blocking file read so it doesn't stall the event loop
    body = await asyncio.to_thread(file_path.read_bytes)
    await _send_response(send, 200, body, mime or "application/octet-stream")
    return True


async def _serve_spa_fallback(send: Callable, spa_fallback: Path) -> None:
    """Serve the SPA fallback file (typically index.html)."""
    if spa_fallback.is_file():
        body = await asyncio.to_thread(spa_fallback.read_bytes)
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
    # Path prefixes routed to the backend (not proxied to Vite) in dev mode.
    # None applies ViteDevProxy's default (["/events", "/ws"]), so the
    # conventional /ws WebSocket reaches the backend out of the box.
    vite_backend_prefixes: list[str] | None = None
    # Maximum request body size in bytes; requests exceeding it get a 413.
    # None disables the cap (unbounded -- only for trusted deployments).
    max_body_size: int | None = 10 * 1024 * 1024


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
    vite_backend_prefixes = effective.vite_backend_prefixes
    max_body_size = effective.max_body_size

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
    # Lifespan handles for mounted sub-apps, keyed by id(mount_app);
    # populated during lifespan.startup, cleared on shutdown.
    _mount_handles: dict[int, _MountLifespan] = {}

    # Precompute accepted-parameter sets for handlers with deps so the hot
    # path never calls inspect.signature per request. Routes registered
    # after create_app() are computed once on first use and cached.
    _handler_params: dict[Callable, frozenset[str] | None] = {}
    for _rt in router._routes:
        _rt_handler, _rt_deps = _rt[2], _rt[3]
        if _rt_deps and _rt_handler not in _handler_params:
            _handler_params[_rt_handler] = _accepted_params(_rt_handler)
    for _wt in router._ws_routes:
        _wt_handler, _wt_deps = _wt[1], _wt[2]
        if _wt_deps and _wt_handler not in _handler_params:
            _handler_params[_wt_handler] = _accepted_params(_wt_handler)

    def _filter_to_handler(handler: Callable, resolved: dict[str, Any]) -> dict[str, Any]:
        """Drop resolved deps the handler does not accept, so router-level
        deps (e.g. auth) don't cause TypeError on handlers that don't
        need the resolved value."""
        if handler not in _handler_params:
            _handler_params[handler] = _accepted_params(handler)
        accepted = _handler_params[handler]
        if accepted is None:
            return resolved
        return {k: v for k, v in resolved.items() if k in accepted}

    async def app(scope: dict, receive: Callable, send: Callable) -> None:
        nonlocal _lifespan_state

        # -- Lifespan protocol --
        if scope["type"] == "lifespan":
            message = await receive()
            if message["type"] == "lifespan.startup":
                ctx = None
                if lifespan is not None:
                    # Enter the context manager; it stays open until shutdown
                    ctx = lifespan(app)
                    try:
                        yielded = await ctx.__aenter__()
                    except BaseException as exc:
                        await send({
                            "type": "lifespan.startup.failed",
                            "message": str(exc),
                        })
                        return
                    if isinstance(yielded, dict):
                        _lifespan_state = yielded

                # Forward lifespan to mounted sub-apps (started after the
                # parent lifespan, shut down before it, in reverse order)
                started_mounts: list[_MountLifespan] = []
                startup_error: str | None = None
                for _mount_prefix, mount_app in router._mounts:
                    handle = _MountLifespan(mount_app)
                    err = await handle.startup()
                    if err is not None:
                        startup_error = err
                        break
                    _mount_handles[id(mount_app)] = handle
                    started_mounts.append(handle)
                if startup_error is not None:
                    # Roll back already-started mounts and the parent ctx
                    for handle in reversed(started_mounts):
                        await handle.shutdown()
                    _mount_handles.clear()
                    if ctx is not None:
                        try:
                            await ctx.__aexit__(None, None, None)
                        except BaseException:
                            log.exception(
                                "Lifespan cleanup error after mounted app startup failure"
                            )
                    await send({
                        "type": "lifespan.startup.failed",
                        "message": startup_error,
                    })
                    return

                await send({"type": "lifespan.startup.complete"})
                await receive()  # blocks until lifespan.shutdown

                shutdown_errors: list[str] = []
                for handle in reversed(started_mounts):
                    err = await handle.shutdown()
                    if err is not None:
                        shutdown_errors.append(err)
                _mount_handles.clear()
                if ctx is not None:
                    try:
                        await ctx.__aexit__(None, None, None)
                    except BaseException as exc:
                        shutdown_errors.append(str(exc))
                if shutdown_errors:
                    await send({
                        "type": "lifespan.shutdown.failed",
                        "message": "; ".join(shutdown_errors),
                    })
                    return
                await send({"type": "lifespan.shutdown.complete"})
            return

        # -- Mounted sub-apps (checked before regular routing) --
        if scope["type"] in ("http", "websocket"):
            path = scope.get("path", "")
            for mount_prefix, mount_app in router._mounts:
                if path == mount_prefix or path.startswith(mount_prefix + "/"):
                    # Rewrite scope: strip prefix from path, extend root_path.
                    # Merge parent lifespan state and the mount's own lifespan
                    # state into the forwarded scope.
                    inner_path = path[len(mount_prefix):] or "/"
                    sub_state = dict(scope.get("state") or {})
                    sub_state.update(_lifespan_state)
                    mount_handle = _mount_handles.get(id(mount_app))
                    if mount_handle is not None:
                        sub_state.update(mount_handle.state)
                    scope = {
                        **scope,
                        "path": inner_path,
                        "root_path": scope.get("root_path", "") + mount_prefix,
                        "state": sub_state,
                    }
                    await mount_app(scope, receive, send)
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
                    try:
                        resolved, cleanups = await _resolver.resolve(ws_deps, ws)
                    except HTTPError as exc:
                        # A dependency rejected the connection (e.g. auth).
                        # Consume websocket.connect per ASGI spec, then close
                        # with 1008 (policy violation).
                        await receive()
                        await send({
                            "type": "websocket.close",
                            "code": 1008,
                            "reason": exc.detail,
                        })
                        return
                    except Exception:
                        log.exception("WebSocket dependency error on %s", path)
                        await receive()
                        await send({"type": "websocket.close", "code": 1011})
                        return
                    try:
                        await ws_handler(ws, **_filter_to_handler(ws_handler, resolved))
                    except WebSocketDisconnect:
                        pass  # normal client disconnect, not an error
                    finally:
                        await DependencyResolver.cleanup(cleanups)
                else:
                    try:
                        await ws_handler(ws)
                    except WebSocketDisconnect:
                        pass  # normal client disconnect, not an error
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

            # Track whether http.response.start has been sent so error
            # paths never attempt a second response (protocol violation).
            response_started = False

            # HEAD is served by the matched GET handler, but the entity body
            # must not be transmitted (RFC 7231 4.3.2). Strip the body from
            # every http.response.body message while preserving the start
            # message headers (content-type, content-length, etc.) so the
            # HEAD response headers match the GET response.
            is_head = method == "HEAD"

            async def tracked_send(message: dict) -> None:
                nonlocal response_started
                if message["type"] == "http.response.start":
                    response_started = True
                elif is_head and message["type"] == "http.response.body":
                    message = {**message, "body": b""}
                await send(message)

            # Bound before the try so error paths can tell whether the
            # Request was ever constructed (receive() may raise first).
            request: Request | None = None

            try:
                # Read body for all methods (GET with empty body costs nothing).
                # Accumulate chunks in a list (bytes += is O(n^2)) and enforce
                # the configured size cap to prevent unbounded buffering.
                chunks: list[bytes] = []
                body_len = 0
                while True:
                    msg = await receive()
                    chunk = msg.get("body", b"")
                    if chunk:
                        body_len += len(chunk)
                        if max_body_size is not None and body_len > max_body_size:
                            await _send_response(
                                tracked_send, 413,
                                msgspec.json.encode({"detail": "Request body too large"}),
                                "application/json",
                            )
                            return
                        chunks.append(chunk)
                    if not msg.get("more_body", False):
                        break
                body = b"".join(chunks) or None

                request = Request(scope, path_params, body, receive=receive)

                # A single disconnect watcher owns the receive channel for the
                # rest of this request's lifetime -- streaming and non-streaming
                # alike -- maintaining request._disconnected and cancelling any
                # in-flight stream-driving task. It is cancelled in the finally
                # when the request ends.
                watcher = asyncio.create_task(_disconnect_watcher(receive, request))
                try:
                    # DI cleanup runs in a finally that wraps the response send,
                    # so a generator-provided resource stays alive across the
                    # entire streaming body and is torn down exactly once --
                    # only after the stream completes, errors, or is cancelled
                    # by a disconnect. (For buffered responses the result is
                    # already materialised, so the ordering is unobservable.)
                    cleanups: list = []
                    try:
                        if route_deps:
                            resolved, cleanups = await _resolver.resolve(
                                route_deps, request,
                            )
                            # Filter resolved deps to only those the handler
                            # actually accepts (accepted-param sets are
                            # precomputed at app creation, not per request).
                            filtered = _filter_to_handler(handler, resolved)
                            result = await handler(request, **filtered)
                        else:
                            result = await handler(request)

                        # Apply response_model validation if configured and
                        # result is a plain dict (skip if handler returned a
                        # Response type).
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
                                    tracked_send, 500,
                                    msgspec.json.encode({"detail": str(ve)}),
                                    "application/json",
                                )
                                return

                        await _send_result(tracked_send, result, request)
                    finally:
                        await DependencyResolver.cleanup(cleanups)
                finally:
                    watcher.cancel()
                    try:
                        await watcher
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        log.exception(
                            "Disconnect watcher failed on %s %s", method, path
                        )
            except HTTPError as exc:
                if response_started:
                    # Response already started -- cannot change it now
                    log.debug(
                        "HTTPError after response started on %s %s: %s",
                        method, path, exc,
                    )
                    return
                await _send_response(
                    tracked_send, exc.status_code,
                    msgspec.json.encode({"detail": exc.detail}),
                    "application/json",
                )
            except Exception as exc:
                if response_started:
                    # Never attempt a 500 after http.response.start -- a
                    # second start is an ASGI protocol violation.
                    log.exception(
                        "Handler error after response started on %s %s",
                        method, path,
                    )
                    return
                # Check registered exception handlers (most specific first).
                # Handlers need the Request; skip them if receive() failed
                # before the Request was constructed.
                handled = False
                for exc_type, exc_handler in _exc_handlers if request is not None else []:
                    if isinstance(exc, exc_type):
                        try:
                            result = await exc_handler(request, exc)
                            await _send_result(tracked_send, result, request)
                            handled = True
                        except Exception:
                            log.exception(
                                "Exception handler error on %s %s",
                                method, path,
                            )
                        break
                if not handled:
                    # Test clients can opt into re-raising the real exception
                    # (with its traceback) instead of receiving an opaque 500 --
                    # set via a scope flag by AsyncTestClient/TestClient. This
                    # only fires before the response has started (guarded above).
                    if scope.get("fastware.raise_server_exceptions"):
                        raise
                    log.exception("Handler error on %s %s", method, path)
                    await _send_response(
                        tracked_send, 500,
                        msgspec.json.encode({"detail": "Internal server error"}),
                        "application/json",
                    )
            return

        # -- 405 Method Not Allowed --
        # No route matched this method. If the path matches one or more routes
        # under other methods, this is a method mismatch (405), not a missing
        # resource -- return 405 with an Allow header rather than falling
        # through to static/SPA/404. Genuinely-unmatched paths (empty set)
        # continue to the static/SPA/404 handling below.
        allowed = router.allowed_methods(path)
        if allowed:
            await _send_response(
                send, 405,
                msgspec.json.encode({"detail": "Method not allowed"}),
                "application/json",
                extra_headers={"Allow": ", ".join(sorted(allowed))},
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
    )
    from fastware.middleware import (
        RequestIDMiddleware as _RequestIDMiddleware,
    )
    from fastware.middleware import (
        RequestTimingMiddleware as _RequestTimingMiddleware,
    )
    from fastware.middleware import (
        TrustedHostMiddleware as _TrustedHostMiddleware,
    )
    from fastware.middleware import (
        ViteDevProxy as _ViteDevProxy,
    )

    if request_timing:
        wrapped = _RequestTimingMiddleware(wrapped)
    if request_id:
        wrapped = _RequestIDMiddleware(wrapped)
    if cors_origins:
        wrapped = _CORSMiddleware(wrapped, allow_origins=cors_origins)
    if vite_dev_port is not None:
        wrapped = _ViteDevProxy(
            wrapped,
            vite_port=vite_dev_port,
            backend_prefixes=vite_backend_prefixes,
        )
    if trusted_hosts:
        wrapped = _TrustedHostMiddleware(wrapped, allowed_hosts=trusted_hosts)

    return wrapped
