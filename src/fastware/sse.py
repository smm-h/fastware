"""SSE (Server-Sent Events) broadcaster with typed event registration, per-client async queues, automatic disconnect pruning, and strict mode enforcement."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from typing import Any

import msgspec

from fastware.request import Request
from fastware.responses import StreamResponse

__all__ = [
    "Broadcaster",
    "sse_route",
]

log = logging.getLogger(__name__)


class Broadcaster:
    """Manages SSE client connections and broadcasts typed events.

    Event types must be registered via ``register_event`` before they can be
    broadcast.  In strict mode (the default), broadcasting an unregistered
    event raises ``ValueError``.  Pass ``strict=False`` to skip validation.
    """

    def __init__(
        self,
        buffer_size: int = 256,
        *,
        strict: bool = True,
        heartbeat_interval: float | None = None,
    ):
        self._clients: list[asyncio.Queue[str]] = []
        self._buffer_size = buffer_size
        self._event_types: set[str] = set()
        self._strict = strict
        self._heartbeat_interval = heartbeat_interval

    # -- Event type registry --------------------------------------------------

    def register_event(self, name: str) -> None:
        """Declare an allowed event type."""
        self._event_types.add(name)

    @property
    def event_types(self) -> frozenset[str]:
        """Currently registered event types."""
        return frozenset(self._event_types)

    # -- Broadcasting ---------------------------------------------------------

    def _format_sse(self, event: str, data: dict[str, Any] | str) -> str:
        """Format a payload as an SSE wire message.

        Dict payloads are serialized with msgspec (project convention). A
        multi-line payload is emitted as one ``data:`` line per line, per the
        SSE spec, so a stray newline in the payload cannot terminate the event
        early or inject additional SSE fields.
        """
        payload = msgspec.json.encode(data).decode() if isinstance(data, dict) else data
        data_lines = "".join(f"data: {line}\n" for line in payload.split("\n"))
        return f"event: {event}\n{data_lines}\n"

    def broadcast(self, event: str, data: dict[str, Any] | str) -> None:
        """Send an event to all connected clients.

        Prunes clients whose queues are full (they fell behind and are
        presumed disconnected or stuck).

        Raises ``ValueError`` if *event* was not previously registered and
        the broadcaster is in strict mode.
        """
        if self._strict and event not in self._event_types:
            raise ValueError(
                f"unregistered event type {event!r}; "
                f"call register_event({event!r}) first "
                f"(registered: {sorted(self._event_types)})"
            )
        message = self._format_sse(event, data)
        disconnected: list[asyncio.Queue[str]] = []
        for q in self._clients:
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                disconnected.append(q)
        for q in disconnected:
            self._clients.remove(q)
            log.debug("pruned full SSE client queue (%d remain)", len(self._clients))

    # -- Client streaming -----------------------------------------------------

    async def _event_generator(self, queue: asyncio.Queue[str]) -> AsyncGenerator[str, None]:
        """Yield SSE messages from a per-client queue.

        The queue is registered as a client only once iteration begins, and the
        ``finally`` block guarantees it is unregistered when the generator is
        closed (e.g. client disconnect). Registering here — rather than in
        ``stream()`` — ensures a ``StreamResponse`` whose body is never consumed
        does not leak a queue into ``self._clients``.

        When heartbeat_interval is set, yields SSE comment heartbeats
        (": heartbeat\\n\\n") if no real message arrives within the interval.
        """
        self._clients.append(queue)
        try:
            while True:
                if self._heartbeat_interval is not None:
                    try:
                        msg = await asyncio.wait_for(
                            queue.get(), timeout=self._heartbeat_interval
                        )
                    except asyncio.TimeoutError:
                        yield ": heartbeat\n\n"
                        continue
                else:
                    msg = await queue.get()
                yield msg
        except asyncio.CancelledError:
            return
        finally:
            if queue in self._clients:
                self._clients.remove(queue)

    async def stream(self, request: Request) -> StreamResponse:
        """Return a ``StreamResponse`` for an SSE endpoint.

        Creates a per-client queue and wraps the async generator in the
        framework's streaming response type. The queue is registered as a
        client by ``_event_generator`` when iteration starts, not here, so an
        unconsumed response never leaks a queue.
        """
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=self._buffer_size)
        return StreamResponse(
            self._event_generator(queue),
            content_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    # -- Introspection --------------------------------------------------------

    @property
    def client_count(self) -> int:
        """Number of currently connected SSE clients."""
        return len(self._clients)


# -- Convenience --------------------------------------------------------------


def sse_route(broadcaster: Broadcaster):
    """Return an async handler suitable for ``router.add_route("GET", "/events", handler)``."""

    async def handler(request: Request) -> StreamResponse:
        return await broadcaster.stream(request)

    return handler
