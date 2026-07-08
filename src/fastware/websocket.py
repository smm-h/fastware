"""WebSocket helper class wrapping the raw ASGI scope/receive/send triple with typed accept, send, receive, and close methods for ergonomic usage."""

from __future__ import annotations

from typing import Any, Callable

import msgspec

__all__ = [
    "WebSocket",
    "WebSocketDisconnect",
]


class WebSocketDisconnect(Exception):
    """Raised when a WebSocket client disconnects.

    The *code* attribute carries the close code from the ASGI
    ``websocket.disconnect`` message (defaults to 1000 / normal closure).
    """

    def __init__(self, code: int = 1000) -> None:
        self.code = code
        super().__init__(f"WebSocket disconnected with code {code}")


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
        # Consume the websocket.connect message per ASGI spec. The client may
        # disconnect before the handshake completes, in which case the ASGI
        # server delivers websocket.disconnect here instead of websocket.connect.
        # Sending websocket.accept in that case is a protocol error, so raise
        # WebSocketDisconnect and let the handler unwind.
        msg = await self._receive()
        if msg.get("type") == "websocket.disconnect":
            raise WebSocketDisconnect(code=msg.get("code", 1000))
        accept_msg: dict[str, Any] = {"type": "websocket.accept"}
        if subprotocol:
            accept_msg["subprotocol"] = subprotocol
        await self._send(accept_msg)

    async def close(self, code: int = 1000) -> None:
        await self._send({"type": "websocket.close", "code": code})

    async def send_json(self, data: Any) -> None:
        await self._send({"type": "websocket.send", "bytes": msgspec.json.encode(data)})

    async def send_bytes(self, data: bytes) -> None:
        await self._send({"type": "websocket.send", "bytes": data})

    async def send_text(self, text: str) -> None:
        await self._send({"type": "websocket.send", "text": text})

    async def _receive_data(self) -> dict[str, Any]:
        """Receive a data message, raising WebSocketDisconnect on disconnect."""
        msg = await self._receive()
        if msg.get("type") == "websocket.disconnect":
            raise WebSocketDisconnect(code=msg.get("code", 1000))
        return msg

    async def receive_json(self) -> Any:
        msg = await self._receive_data()
        raw = msg.get("bytes") or msg.get("text", b"")
        return msgspec.json.decode(raw)

    async def receive_bytes(self) -> bytes:
        msg = await self._receive_data()
        return msg.get("bytes", b"")

    async def receive_text(self) -> str:
        msg = await self._receive_data()
        return msg.get("text", "")

    async def receive_raw(self) -> dict[str, Any]:
        """Return the raw ASGI message dict from the WebSocket connection.

        The dict contains keys like "type", "bytes", "text" depending on
        the frame type. Useful for handlers that need to distinguish between
        binary and text frames without committing to one receive method.
        """
        return await self._receive()
