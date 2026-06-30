"""WebSocket helper class wrapping the raw ASGI triple."""

from __future__ import annotations

from typing import Any, Callable

import msgspec


class WebSocket:
    """Wraps the raw ASGI (scope, receive, send) triple for WebSocket connections.

    Handlers receive a WebSocket instance instead of the raw triple, providing
    convenient methods for accept/close/send/receive and properties for
    path_params, headers, and query_string.
    """

    __slots__ = ("scope", "_receive", "_send")

    def __init__(self, scope: dict, receive: Callable, send: Callable) -> None:
        self.scope = scope
        self._receive = receive
        self._send = send

    @property
    def path_params(self) -> dict[str, Any]:
        return self.scope.get("path_params", {})

    @property
    def headers(self) -> dict[str, str]:
        """Parse ASGI headers into a case-preserving dict (first value wins)."""
        result: dict[str, str] = {}
        for k, v in self.scope.get("headers", []):
            name = k.decode("latin-1")
            if name not in result:
                result[name] = v.decode("latin-1")
        return result

    @property
    def query_string(self) -> str:
        return self.scope.get("query_string", b"").decode()

    async def accept(self, subprotocol: str | None = None) -> None:
        # Consume the websocket.connect message per ASGI spec
        await self._receive()
        msg: dict[str, Any] = {"type": "websocket.accept"}
        if subprotocol:
            msg["subprotocol"] = subprotocol
        await self._send(msg)

    async def close(self, code: int = 1000) -> None:
        await self._send({"type": "websocket.close", "code": code})

    async def send_json(self, data: Any) -> None:
        await self._send({"type": "websocket.send", "bytes": msgspec.json.encode(data)})

    async def send_bytes(self, data: bytes) -> None:
        await self._send({"type": "websocket.send", "bytes": data})

    async def send_text(self, text: str) -> None:
        await self._send({"type": "websocket.send", "text": text})

    async def receive_json(self) -> Any:
        msg = await self._receive()
        raw = msg.get("bytes") or msg.get("text", b"")
        return msgspec.json.decode(raw)

    async def receive_bytes(self) -> bytes:
        msg = await self._receive()
        return msg.get("bytes", b"")

    async def receive_text(self) -> str:
        msg = await self._receive()
        return msg.get("text", "")

    async def receive_raw(self) -> dict[str, Any]:
        """Return the raw ASGI message dict from the WebSocket connection.

        The dict contains keys like "type", "bytes", "text" depending on
        the frame type. Useful for handlers that need to distinguish between
        binary and text frames without committing to one receive method.
        """
        return await self._receive()
