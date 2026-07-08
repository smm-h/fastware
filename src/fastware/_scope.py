"""Scope-level header and cookie access shared across the ASGI layer.

ASGI headers arrive on the scope as a list of ``(name, value)`` byte tuples,
with names lowercased per the ASGI spec. These helpers read them with a single
linear scan -- never allocating a dict of all headers just to read one -- and
are the single implementation used by the request wrapper
(:mod:`fastware.request`), the auth/CSRF middleware (:mod:`fastware.auth`), and
the pure-ASGI middleware (:mod:`fastware.middleware`).

Matching is case-insensitive: callers pass a lowercase name, and scope keys are
case-folded before comparison so a non-compliant server that emits mixed-case
header names still matches.
"""

from __future__ import annotations

from http.cookies import SimpleCookie
from typing import Any

Scope = dict[str, Any]


def _first_header(scope: Scope, name: bytes) -> bytes | None:
    """Return the first raw value for header *name*, or ``None`` if absent.

    Distinguishing "absent" from "present but empty" is why this returns
    ``None`` rather than ``b""`` -- :func:`header_str` needs it to honor a
    caller-supplied default only when the header is truly missing.
    """
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return value
    return None


def header_bytes(scope: Scope, name: bytes) -> bytes:
    """Return the first value of header *name* (lowercase bytes), or ``b""``."""
    value = _first_header(scope, name)
    return value if value is not None else b""


def header_str(
    scope: Scope, name: str, default: str | None = None,
) -> str | None:
    """Return header *name* decoded as latin-1, or *default* if absent.

    ASGI header values are opaque bytes; latin-1 is the lossless byte<->str
    mapping used throughout the HTTP layer.
    """
    value = _first_header(scope, name.lower().encode())
    return value.decode("latin-1") if value is not None else default


def cookies(scope: Scope) -> dict[str, str]:
    """Parse the ``Cookie`` header into a name->value dict (``{}`` if none)."""
    raw = header_bytes(scope, b"cookie")
    if not raw:
        return {}
    sc: SimpleCookie = SimpleCookie()
    sc.load(raw.decode("latin-1"))
    return {key: morsel.value for key, morsel in sc.items()}


def cookie(scope: Scope, name: str, default: str = "") -> str:
    """Return a single cookie value by name, or *default* if absent."""
    return cookies(scope).get(name, default)
