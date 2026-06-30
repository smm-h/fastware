"""ASGI type aliases used throughout fastware."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, Callable

__all__ = [
    "Scope",
    "Receive",
    "Send",
]

# ---------------------------------------------------------------------------
# ASGI type aliases
# ---------------------------------------------------------------------------

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]
