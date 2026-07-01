"""HTTP request wrapper providing lazy body parsing, query parameter extraction, JSON deserialization via msgspec, header access, and per-request state."""

from __future__ import annotations

import asyncio
import http.cookies
from typing import Any, Callable
from urllib.parse import parse_qs

import msgspec

from fastware.responses import HTTPError

__all__ = [
    "State",
    "Request",
]


# ---------------------------------------------------------------------------
# State wrapper
# ---------------------------------------------------------------------------

class State:
    """Dict-backed state that supports both attribute and dict access.

    ``state.key``, ``state["key"]``, and ``state.get("key")`` all work.
    Attribute assignment (``state.key = val``) also works.
    """

    def __init__(self, data: dict[str, Any] | None = None):
        # Use object.__setattr__ to avoid triggering our custom __setattr__
        object.__setattr__(self, "_data", data if data is not None else {})

    def __getattr__(self, name: str) -> Any:
        data = object.__getattribute__(self, "_data")
        try:
            return data[name]
        except KeyError:
            raise AttributeError(f"State has no attribute {name!r}") from None

    def __setattr__(self, name: str, value: Any) -> None:
        object.__getattribute__(self, "_data")[name] = value

    def __getitem__(self, key: str) -> Any:
        return object.__getattribute__(self, "_data")[key]

    def __setitem__(self, key: str, value: Any) -> None:
        object.__getattribute__(self, "_data")[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return object.__getattribute__(self, "_data").get(key, default)

    def __contains__(self, key: str) -> bool:
        return key in object.__getattribute__(self, "_data")


# ---------------------------------------------------------------------------
# Sentinel for missing query parameter defaults
# ---------------------------------------------------------------------------

_MISSING = object()


# ---------------------------------------------------------------------------
# Request wrapper
# ---------------------------------------------------------------------------

class Request:
    """Wraps ASGI scope with parsed body and params."""

    __slots__ = (
        "scope", "path_params", "_body", "_json", "_json_decoded",
        "_receive", "_disconnected", "_cookies", "_cookies_parsed",
        "_query_params", "_state",
    )

    def __init__(
        self,
        scope: dict,
        path_params: dict,
        body: bytes | None,
        receive: Callable | None = None,
    ):
        self.scope = scope
        self.path_params = path_params
        self._body = body
        self._json = None
        self._json_decoded = False
        self._receive = receive
        self._disconnected = False
        self._cookies = None
        self._cookies_parsed = False
        self._query_params = None
        self._state = None

    @property
    def json(self) -> dict | list | None:
        """Lazily decode the JSON body on first access, then cache."""
        if not self._json_decoded:
            if self._body:
                try:
                    self._json = msgspec.json.decode(self._body)
                except (msgspec.DecodeError, ValueError):
                    self._json = None
            self._json_decoded = True
        return self._json

    @property
    def body(self) -> bytes | None:
        """Raw request body bytes."""
        return self._body

    def query(
        self,
        name: str,
        default: Any = _MISSING,
        *,
        type_: type = str,
        ge: int | float | None = None,
        le: int | float | None = None,
        min_length: int | None = None,
        max_length: int | None = None,
    ) -> Any:
        """Get a query parameter by name, with optional type conversion and constraints.

        The *default* is only used when the key is **absent** from the query
        string.  When no default is given and the key is absent, returns
        ``None``.

        Type coercion failure (key present but unconvertible) always raises
        ``HTTPError(422)`` -- the default is **not** used as a fallback for
        bad input.

        Constraints (checked after type coercion):
        - ``ge``: value must be >= this (numeric)
        - ``le``: value must be <= this (numeric)
        - ``min_length``: len(value) must be >= this (strings)
        - ``max_length``: len(value) must be <= this (strings)

        Raises HTTPError(422) on coercion failure or constraint violation.
        """
        qs = parse_qs(self.scope.get("query_string", b"").decode())
        values = qs.get(name)
        if not values:
            if default is _MISSING:
                return None
            return default
        raw = values[0]
        try:
            value = type_(raw)
        except (ValueError, TypeError):
            raise HTTPError(
                422,
                f"Query parameter '{name}': cannot convert '{raw}' to {type_.__name__}",
            )
        # Validate constraints
        if ge is not None and value < ge:
            raise HTTPError(422, f"Query parameter '{name}' must be >= {ge}")
        if le is not None and value > le:
            raise HTTPError(422, f"Query parameter '{name}' must be <= {le}")
        if min_length is not None and len(value) < min_length:
            raise HTTPError(422, f"Query parameter '{name}' must have length >= {min_length}")
        if max_length is not None and len(value) > max_length:
            raise HTTPError(422, f"Query parameter '{name}' must have length <= {max_length}")
        return value

    def query_list(self, name: str, type_: type = str) -> list:
        """Return all values for a multi-value query key with optional type coercion.

        Returns an empty list if the key is absent.  Raises
        ``HTTPError(422)`` if any value cannot be converted to *type_*.
        """
        qs = parse_qs(self.scope.get("query_string", b"").decode())
        values = qs.get(name)
        if not values:
            return []
        try:
            return [type_(v) for v in values]
        except (ValueError, TypeError):
            raise HTTPError(
                422,
                f"Query parameter '{name}': cannot convert values to {type_.__name__}",
            )

    @property
    def query_params(self) -> dict[str, str]:
        """Parsed query string as a dict (first value per key). Cached."""
        if self._query_params is None:
            qs = parse_qs(self.scope.get("query_string", b"").decode())
            self._query_params = {k: v[0] for k, v in qs.items()}
        return self._query_params

    def header(self, name: str, default: str | None = None) -> str | None:
        """Return a request header by name (case-insensitive).

        ASGI headers arrive as a list of ``(bytes, bytes)`` tuples. This
        helper decodes both to ``str`` and looks the name up
        case-insensitively, matching HTTP semantics.
        """
        target = name.lower().encode()
        for k, v in self.scope.get("headers", []):
            if k.lower() == target:
                return v.decode("latin-1")
        return default

    @property
    def body_size(self) -> int:
        """Length in bytes of the raw request body (0 if no body)."""
        return len(self._body) if self._body is not None else 0

    @property
    def state(self) -> State:
        """Lifespan + per-request state, supporting both attribute and dict access. Cached."""
        if self._state is None:
            self._state = State(self.scope.get("state", {}))
        return self._state

    @property
    def method(self) -> str:
        """HTTP method (GET, POST, etc.)."""
        return self.scope["method"]

    @property
    def path(self) -> str:
        """Request path."""
        return self.scope.get("path", "")

    async def is_disconnected(self) -> bool:
        """Check whether the client has disconnected."""
        if self._disconnected:
            return True
        if self._receive is None:
            return False
        try:
            msg = await asyncio.wait_for(self._receive(), timeout=0.0)
            if msg.get("type") == "http.disconnect":
                self._disconnected = True
                return True
        except (asyncio.TimeoutError, Exception):
            pass
        return False

    @property
    def cookies(self) -> dict[str, str]:
        """Parse Cookie header and return a dict of cookie name-value pairs."""
        if not self._cookies_parsed:
            cookie_header = self.header("cookie", "")
            result: dict[str, str] = {}
            if cookie_header:
                sc = http.cookies.SimpleCookie()
                sc.load(cookie_header)
                for key, morsel in sc.items():
                    result[key] = morsel.value
            self._cookies = result
            self._cookies_parsed = True
        return self._cookies  # type: ignore[return-value]

    def cookie(self, name: str, default: str | None = None) -> str | None:
        """Get a single cookie value by name."""
        return self.cookies.get(name, default)

    def json_as(self, model: type):
        """Parse request body as a Pydantic model. Raises HTTPError(422) on failure."""
        from pydantic import ValidationError
        try:
            return model.model_validate(self.json)
        except ValidationError as e:
            raise HTTPError(422, str(e)) from e
