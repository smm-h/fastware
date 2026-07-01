"""HTTP response types, cookie helpers, and low-level ASGI send functions."""

from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncGenerator, Callable

import msgspec

__all__ = [
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
]


# ---------------------------------------------------------------------------
# Cookie helpers
# ---------------------------------------------------------------------------

def set_cookie(
    name: str,
    value: str,
    *,
    httponly: bool = False,
    samesite: str = "lax",
    max_age: int | None = None,
    path: str = "/",
    secure: bool = False,
) -> str:
    """Build a Set-Cookie header string."""
    parts = [f"{name}={value}", f"Path={path}", f"SameSite={samesite}"]
    if httponly:
        parts.append("HttpOnly")
    if secure:
        parts.append("Secure")
    if max_age is not None:
        parts.append(f"Max-Age={max_age}")
    return "; ".join(parts)


def delete_cookie(name: str, *, path: str = "/") -> str:
    """Build a Set-Cookie header string that clears the cookie."""
    return f"{name}=; Path={path}; Max-Age=0"


# ---------------------------------------------------------------------------
# HTTPError exception
# ---------------------------------------------------------------------------

class HTTPError(Exception):
    """Raise from handlers to return a specific HTTP error status."""

    def __init__(self, status_code: int, detail: str = ""):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


# ---------------------------------------------------------------------------
# Response types
# ---------------------------------------------------------------------------

class JSONResponse:
    """JSON response with optional status code, headers, and cookies."""

    def __init__(
        self,
        data: Any,
        status: int = 200,
        headers: dict[str, str] | None = None,
        cookies: list[str] | None = None,
    ):
        self.data = data
        self.status = status
        self.headers = headers or {}
        self.cookies = cookies or []


class TextResponse:
    """Plain text or CSS response.

    ``headers`` (optional) is merged with the framework's default response
    headers; values must already be plain strings.
    """

    def __init__(
        self,
        text: str,
        content_type: str = "text/plain",
        status: int = 200,
        headers: dict[str, str] | None = None,
        cookies: list[str] | None = None,
    ):
        self.text = text
        self.content_type = content_type
        self.status = status
        self.headers = headers or {}
        self.cookies = cookies or []


class HTMLResponse:
    """HTML response."""

    def __init__(
        self,
        html: str,
        status: int = 200,
        headers: dict[str, str] | None = None,
        cookies: list[str] | None = None,
    ):
        self.html = html
        self.status = status
        self.headers = headers or {}
        self.cookies = cookies or []


class BytesResponse:
    """Raw bytes response with an explicit content type."""

    def __init__(
        self,
        data: bytes,
        content_type: str = "application/octet-stream",
        status: int = 200,
        headers: dict[str, str] | None = None,
        cookies: list[str] | None = None,
    ):
        self.data = data
        self.content_type = content_type
        self.status = status
        self.headers = headers or {}
        self.cookies = cookies or []


class StreamResponse:
    """Streaming response (for SSE)."""

    def __init__(
        self,
        generator: AsyncGenerator,
        content_type: str,
        headers: dict[str, str] | None = None,
        status: int = 200,
        cookies: list[str] | None = None,
    ):
        self.generator = generator
        self.content_type = content_type
        self.headers = headers or {}
        self.status = status
        self.cookies = cookies or []


class FileResponse:
    """Serve a file from disk with MIME detection and Content-Length."""

    def __init__(
        self,
        path: str | Path,
        content_type: str | None = None,
        status: int = 200,
        headers: dict[str, str] | None = None,
        cookies: list[str] | None = None,
    ):
        self.path = Path(path)
        self.content_type = content_type
        self.status = status
        self.headers = headers or {}
        self.cookies = cookies or []


# ---------------------------------------------------------------------------
# ASGI send helpers
# ---------------------------------------------------------------------------

async def _send_response(
    send: Callable,
    status: int,
    body: bytes,
    content_type: str,
    extra_headers: dict[str, str] | None = None,
    cookies: list[str] | None = None,
) -> None:
    """Send a complete HTTP response (headers + body)."""
    headers: list[list[bytes]] = [
        [b"content-type", content_type.encode()],
        [b"content-length", str(len(body)).encode()],
    ]
    if extra_headers:
        for k, v in extra_headers.items():
            headers.append([k.encode(), v.encode()])
    if cookies:
        for cookie in cookies:
            headers.append([b"set-cookie", cookie.encode()])
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": headers,
    })
    await send({"type": "http.response.body", "body": body})


async def send_error(send: Callable, status: int, detail: str) -> None:
    """Send a complete JSON error response via raw ASGI send calls.

    Intended for use by middleware that needs to short-circuit with an error
    without constructing response objects.
    """
    await _send_response(
        send, status,
        msgspec.json.encode({"detail": detail}),
        "application/json",
    )
