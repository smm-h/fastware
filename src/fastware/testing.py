"""Sync and async test clients for fastware apps, wrapping httpx with ASGITransport to exercise routes without starting a real network server.

Provides sync and async test clients that wrap httpx with ASGITransport,
so tests can exercise a fastware app without starting a real server.
Both clients run the ASGI lifespan protocol on enter and shut down on exit.

httpx's ``ASGITransport`` does NOT emit lifespan events itself, so each
client drives the lifespan protocol explicitly via ``_LifespanManager``:
startup on enter, shutdown on exit.  This ensures handlers observe the
state populated by an app's lifespan context manager, matching real
server behaviour.

httpx is a dev/test dependency -- this module should only be imported
in test contexts.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

from httpx import ASGITransport, AsyncClient

__all__ = [
    "AsyncTestClient",
]


class _LifespanManager:
    """Drive the ASGI lifespan protocol around a test client.

    httpx's ``ASGITransport`` never sends ``lifespan`` events, so apps that
    populate state in a lifespan context manager would otherwise see empty
    state under test.  This manager runs the app's lifespan handler as a
    background task on the current event loop, feeding it ``lifespan.startup``
    on :meth:`startup` and ``lifespan.shutdown`` on :meth:`shutdown`, so the
    same app object then serves requests with its startup state in place.

    A ``lifespan.startup.failed`` / ``lifespan.shutdown.failed`` message from
    the app is raised as a hard error -- failures are never swallowed.  Apps
    that do not implement lifespan (the task returns without emitting any
    lifespan event) are handled transparently.
    """

    def __init__(self, app: Any) -> None:
        self._app = app
        self._receive_queue: asyncio.Queue = asyncio.Queue()
        self._startup_complete = asyncio.Event()
        self._shutdown_complete = asyncio.Event()
        self._error: str | None = None
        self._task: asyncio.Task | None = None

    async def _receive(self) -> dict:
        return await self._receive_queue.get()

    async def _send(self, message: dict) -> None:
        mtype = message.get("type")
        if mtype == "lifespan.startup.complete":
            self._startup_complete.set()
        elif mtype == "lifespan.startup.failed":
            self._error = message.get("message", "startup failed")
            self._startup_complete.set()
        elif mtype == "lifespan.shutdown.complete":
            self._shutdown_complete.set()
        elif mtype == "lifespan.shutdown.failed":
            self._error = message.get("message", "shutdown failed")
            self._shutdown_complete.set()

    async def startup(self) -> None:
        self._task = asyncio.ensure_future(
            self._app({"type": "lifespan"}, self._receive, self._send)
        )
        await self._receive_queue.put({"type": "lifespan.startup"})
        await self._wait(self._startup_complete)
        if self._error is not None:
            raise RuntimeError(f"ASGI lifespan startup failed: {self._error}")

    async def shutdown(self) -> None:
        if self._task is None:
            return
        await self._receive_queue.put({"type": "lifespan.shutdown"})
        await self._wait(self._shutdown_complete)
        # Surface anything the app task raised (or let it finish cleanly).
        await self._task
        if self._error is not None:
            raise RuntimeError(f"ASGI lifespan shutdown failed: {self._error}")

    async def _wait(self, event: asyncio.Event) -> None:
        """Wait for *event*, or for the app task to finish first.

        An app that does not support lifespan returns without ever setting the
        event; in that case we stop waiting once the task completes (and
        re-raise if it errored) so enter/exit never hang.
        """
        assert self._task is not None
        event_wait = asyncio.ensure_future(event.wait())
        try:
            await asyncio.wait(
                {event_wait, self._task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            event_wait.cancel()
        if not event.is_set() and self._task.done():
            # App exited without emitting the expected lifespan event.
            exc = self._task.exception()
            if exc is not None:
                raise exc


class AsyncTestClient:
    """Async test client for fastware apps.

    Runs the ASGI lifespan protocol (startup on enter, shutdown on exit) so
    handlers observe the state populated by the app's lifespan handler.

    Use as an async context manager::

        async with AsyncTestClient(app) as client:
            resp = await client.get("/health")
    """

    def __init__(self, app: Any, base_url: str = "http://test") -> None:
        self._app = app
        self._base_url = base_url
        self._client: AsyncClient | None = None
        self._lifespan: _LifespanManager | None = None

    async def __aenter__(self) -> AsyncClient:
        self._lifespan = _LifespanManager(self._app)
        await self._lifespan.startup()
        self._client = AsyncClient(
            transport=ASGITransport(app=self._app),
            base_url=self._base_url,
        )
        return self._client

    async def __aexit__(self, *args: Any) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
        if self._lifespan:
            await self._lifespan.shutdown()
            self._lifespan = None


class _SyncTestClient:
    """Sync test client for fastware apps.

    Runs an ``AsyncClient`` on a background event loop so that sync test
    code can call ``.get()``, ``.post()`` etc. without ``await``.

    Usage::

        with _SyncTestClient(app) as client:
            resp = client.get("/health")
            assert resp.status_code == 200

    The class is named ``_SyncTestClient`` internally and exported as
    ``TestClient`` to avoid pytest treating it as a test class (pytest
    skips classes whose name starts with ``_``).
    """

    def __init__(self, app: Any, base_url: str = "http://test") -> None:
        self._app = app
        self._base_url = base_url
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._client: AsyncClient | None = None
        self._lifespan: _LifespanManager | None = None

    def _start_loop(self) -> None:
        """Run the event loop in a background thread."""
        assert self._loop is not None
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _run(self, coro: Any) -> Any:
        """Schedule a coroutine on the background loop and wait for result."""
        assert self._loop is not None
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=30)

    def _open(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._start_loop, daemon=True)
        self._thread.start()
        # Run the lifespan startup on the same background loop the client and
        # its requests use, so the app's lifespan task and state stay coherent.
        self._lifespan = _LifespanManager(self._app)
        self._run(self._lifespan.startup())
        self._client = self._run(self._create_client())

    async def _create_client(self) -> AsyncClient:
        return AsyncClient(
            transport=ASGITransport(app=self._app),
            base_url=self._base_url,
        )

    def _close(self) -> None:
        if self._client:
            self._run(self._client.aclose())
            self._client = None
        if self._lifespan:
            self._run(self._lifespan.shutdown())
            self._lifespan = None
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        if self._loop:
            self._loop.close()
            self._loop = None

    def _ensure_open(self) -> None:
        if self._client is None:
            self._open()

    def get(self, *args: Any, **kwargs: Any) -> Any:
        self._ensure_open()
        return self._run(self._client.get(*args, **kwargs))

    def post(self, *args: Any, **kwargs: Any) -> Any:
        self._ensure_open()
        return self._run(self._client.post(*args, **kwargs))

    def put(self, *args: Any, **kwargs: Any) -> Any:
        self._ensure_open()
        return self._run(self._client.put(*args, **kwargs))

    def patch(self, *args: Any, **kwargs: Any) -> Any:
        self._ensure_open()
        return self._run(self._client.patch(*args, **kwargs))

    def delete(self, *args: Any, **kwargs: Any) -> Any:
        self._ensure_open()
        return self._run(self._client.delete(*args, **kwargs))

    def options(self, *args: Any, **kwargs: Any) -> Any:
        self._ensure_open()
        return self._run(self._client.options(*args, **kwargs))

    def head(self, *args: Any, **kwargs: Any) -> Any:
        self._ensure_open()
        return self._run(self._client.head(*args, **kwargs))

    def close(self) -> None:
        self._close()

    def __enter__(self) -> _SyncTestClient:
        self._open()
        return self

    def __exit__(self, *args: Any) -> None:
        self._close()


# Export with a pytest-friendly name (not starting with "Test")
TestClient = _SyncTestClient
