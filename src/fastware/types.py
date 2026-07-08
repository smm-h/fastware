"""ASGI type aliases (Scope, Receive, Send) used throughout fastware for consistent type-checked request and response handling."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, Callable

# ---------------------------------------------------------------------------
# ASGI type aliases
# ---------------------------------------------------------------------------

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]
