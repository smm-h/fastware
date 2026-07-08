"""Authentication module providing JWT token creation and verification, bcrypt password hashing, user storage, CSRF protection, and rate limiting.

Pure functions and DI-compatible factories with no framework-specific dependencies
beyond fastware's own asgi types. Keeps auth logic testable and reusable.
"""

from __future__ import annotations

import functools
import json
import os
import re
import secrets
import tempfile
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from fastware import _scope
from fastware.responses import HTTPError, delete_cookie, send_error, set_cookie

__all__ = [
    "create_token",
    "verify_token",
    "hash_password",
    "verify_password",
    "UserStore",
    "JSONFileUserStore",
    "get_current_user",
    "require_role",
    "CSRFMiddleware",
    "set_session_cookies",
    "clear_session_cookies",
    "rate_limit",
]

# ---------------------------------------------------------------------------
# JWT token operations
# ---------------------------------------------------------------------------


def create_token(
    username: str,
    role: str,
    secret: str,
    expires_hours: int = 720,
) -> str:
    """Create a signed JWT with sub, role, exp, and iat claims (HS256)."""
    import jwt

    now = datetime.now(UTC)
    payload = {
        "sub": username,
        "role": role,
        "iat": now,
        "exp": now + timedelta(hours=expires_hours),
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def verify_token(token: str, secret: str) -> dict[str, Any] | None:
    """Decode and validate a JWT. Returns claims dict or None if invalid.

    Only token-validation failures map to None; programming errors
    (e.g. a None secret) propagate.
    """
    import jwt

    try:
        return jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.InvalidTokenError:
        # Base class of all PyJWT validation errors, incl. ExpiredSignatureError.
        return None


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------


def hash_password(plain: str) -> str:
    """Hash a plaintext password with bcrypt.

    Raises ValueError for passwords longer than 72 bytes (UTF-8): bcrypt
    ignores everything past byte 72, so accepting them would silently
    weaken the password.
    """
    import bcrypt

    encoded = plain.encode()
    if len(encoded) > 72:
        raise ValueError(
            f"Password exceeds bcrypt's 72-byte limit "
            f"(got {len(encoded)} bytes); longer passwords would be "
            f"silently truncated."
        )
    return bcrypt.hashpw(encoded, bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    """Check a plaintext password against a bcrypt hash."""
    import bcrypt

    return bcrypt.checkpw(plain.encode(), hashed.encode())


# ---------------------------------------------------------------------------
# User storage interface
# ---------------------------------------------------------------------------


class UserStore:
    """Abstract user storage interface.

    Subclasses overriding ``__init__`` must call ``super().__init__()``
    so the read-modify-write lock is set up.
    """

    def __init__(self) -> None:
        # Serializes read-modify-write operations (create/delete) so
        # concurrent callers cannot lose each other's updates.
        self._lock = threading.Lock()

    def load_users(self) -> list[dict[str, str]]:
        raise NotImplementedError

    def save_users(self, users: list[dict[str, str]]) -> None:
        raise NotImplementedError

    def find_user(self, username: str) -> dict[str, str] | None:
        for user in self.load_users():
            if user["username"] == username:
                return user
        return None

    def create_user(
        self, username: str, password: str, role: str,
    ) -> dict[str, str]:
        """Create a new user. Raises ValueError if username already exists."""
        with self._lock:
            users = self.load_users()
            for u in users:
                if u["username"] == username:
                    raise ValueError(f"User '{username}' already exists")
            user = {
                "username": username,
                "password_hash": hash_password(password),
                "role": role,
                "created_at": datetime.now(UTC).isoformat(),
            }
            users.append(user)
            self.save_users(users)
            return user

    def delete_user(self, username: str) -> None:
        """Delete a user by username. Raises LookupError if not found."""
        with self._lock:
            users = self.load_users()
            for i, u in enumerate(users):
                if u["username"] == username:
                    users.pop(i)
                    self.save_users(users)
                    return
            raise LookupError(f"User '{username}' not found")


class JSONFileUserStore(UserStore):
    """User storage backed by a JSON file."""

    def __init__(self, path: str | Path) -> None:
        super().__init__()
        self.path = Path(path)

    def load_users(self) -> list[dict[str, str]]:
        if not self.path.is_file():
            return []
        return json.loads(self.path.read_text())

    def save_users(self, users: list[dict[str, str]]) -> None:
        """Atomically write the user list (temp file + os.replace).

        A crash mid-write can never truncate or corrupt the existing file.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(users, indent=2) + "\n"
        fd, tmp_path = tempfile.mkstemp(
            dir=self.path.parent, prefix=self.path.name, suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.path)
        except BaseException:
            os.unlink(tmp_path)
            raise


# ---------------------------------------------------------------------------
# Auth dependencies (DI-compatible)
# ---------------------------------------------------------------------------


def get_current_user(
    request: Any, *, allow_query_token: bool = False,
) -> dict[str, Any]:
    """Extract and validate JWT from Authorization header or session cookie.

    Token resolution order:
    1. Authorization: Bearer <token> header
    2. session cookie
    3. ?token= query parameter -- only if ``allow_query_token=True``.
       Off by default because query strings leak into access logs,
       proxies, and browser history. Opt in via a wrapper dep:
       ``deps={"user": lambda request: get_current_user(request, allow_query_token=True)}``

    Reads the JWT secret from request.state["config"]["jwt_secret"].
    Returns decoded claims dict. Raises HTTPError(401) on failure.
    """
    token: str | None = None

    # 1. Bearer header
    auth_header = request.header("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]

    # 2. Session cookie
    if not token:
        token = request.cookie("session") or None

    # 3. Query parameter (opt-in only)
    if not token and allow_query_token:
        token = request.query_params.get("token") or None

    if not token:
        raise HTTPError(401, "Not authenticated")

    config = request.state["config"]
    jwt_secret = config["jwt_secret"]
    claims = verify_token(token, jwt_secret)
    if not claims:
        raise HTTPError(401, "Invalid or expired token")

    return claims


def require_role(role: str) -> Callable:
    """Return a DI factory that checks the current user has the given role.

    Usage: @router.get("/admin", deps={"user": require_role("admin")})
    """
    def dep(request: Any) -> dict[str, Any]:
        user = get_current_user(request)
        if user.get("role") != role:
            raise HTTPError(403, f"Role '{role}' required")
        return user
    return dep


# ---------------------------------------------------------------------------
# CSRF double-submit middleware (pure ASGI)
# ---------------------------------------------------------------------------

# Methods that do not change state -- exempt from CSRF checks.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class CSRFMiddleware:
    """Double-submit cookie CSRF protection (pure ASGI).

    For state-changing requests (POST, PUT, PATCH, DELETE) that aren't
    exempt, validates that:
      1. A ``csrf_token`` cookie is present.
      2. An ``X-CSRF-Token`` header is present.
      3. The two values match.

    Constructor args:
      app: inner ASGI application
      exempt_paths: list of path prefixes to skip CSRF checks
      disabled: bypass all checks (for testing)
    """

    def __init__(
        self,
        app: Any,
        *,
        exempt_paths: list[str] | None = None,
        disabled: bool = False,
    ) -> None:
        self.app = app
        self.exempt_paths = tuple(exempt_paths or [])
        self.disabled = disabled

    async def __call__(
        self, scope: dict[str, Any], receive: Any, send: Any,
    ) -> None:
        # Only inspect HTTP requests; let websocket/lifespan pass through.
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if self.disabled:
            await self.app(scope, receive, send)
            return

        method: str = scope["method"]
        path: str = scope["path"]

        # Safe methods are always exempt.
        if method in _SAFE_METHODS:
            await self.app(scope, receive, send)
            return

        # Bearer token callers are not vulnerable to CSRF (cookie-only attack).
        # Structural check: a real JWT has exactly 3 dot-separated segments.
        auth_header = _scope.header_bytes(scope, b"authorization")
        if auth_header.startswith(b"Bearer "):
            token_bytes = auth_header[7:]
            parts = token_bytes.split(b".")
            if len(parts) == 3 and all(parts):
                await self.app(scope, receive, send)
                return

        # Configurable exempt paths.
        if any(path.startswith(prefix) for prefix in self.exempt_paths):
            await self.app(scope, receive, send)
            return

        # Validate double-submit: cookie value must match header value.
        cookie_token = _scope.cookie(scope, "csrf_token")
        header_token = _scope.header_bytes(
            scope, b"x-csrf-token",
        ).decode("latin-1")

        if not cookie_token or not header_token:
            await send_error(send, 403, "Missing CSRF token")
            return

        if not secrets.compare_digest(cookie_token, header_token):
            await send_error(send, 403, "CSRF token mismatch")
            return

        await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# Session cookie helpers
# ---------------------------------------------------------------------------


def set_session_cookies(token: str, csrf_token: str) -> list[str]:
    """Build Set-Cookie header strings for session and CSRF cookies.

    Returns a list of two Set-Cookie strings:
    - session: httponly, samesite=lax (not readable by JS)
    - csrf_token: js-readable (no httponly), samesite=lax
    """
    return [
        set_cookie("session", token, httponly=True, samesite="lax"),
        set_cookie("csrf_token", csrf_token, httponly=False, samesite="lax"),
    ]


def clear_session_cookies() -> list[str]:
    """Build Set-Cookie header strings that clear session and CSRF cookies."""
    return [
        delete_cookie("session"),
        delete_cookie("csrf_token"),
    ]


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

# Pattern: "N/unit" where unit is second, minute, or hour
_RATE_PATTERN = re.compile(r"^(\d+)/(second|minute|hour)$")

_UNIT_SECONDS = {
    "second": 1,
    "minute": 60,
    "hour": 3600,
}


def rate_limit(
    rate: str,
    key_func: Callable | None = None,
) -> Callable:
    """Decorator for per-client rate limiting using a token bucket.

    Usage:
        @router.get("/api/search")
        @rate_limit("5/minute")
        async def search(request):
            ...

    Args:
        rate: Rate string like "5/minute", "10/second", "100/hour".
        key_func: Optional callable(request) -> str for custom bucket keys.
                  Defaults to client IP from ASGI scope.
    """
    match = _RATE_PATTERN.match(rate)
    if not match:
        raise ValueError(
            f"Invalid rate format: {rate!r}. Expected 'N/second', 'N/minute', or 'N/hour'."
        )
    max_tokens = int(match.group(1))
    window_seconds = _UNIT_SECONDS[match.group(2)]
    refill_rate = max_tokens / window_seconds  # tokens per second

    # In-memory token bucket: key -> (tokens_remaining, last_refill_time)
    buckets: dict[str, tuple[float, float]] = {}
    last_sweep = time.monotonic()

    def decorator(fn: Callable) -> Callable:
        # Explicit (request, **kwargs) signature plus functools.wraps so
        # create_app's dep filtering sees fn's real signature (via
        # __wrapped__) instead of a VAR_KEYWORD catch-all.
        @functools.wraps(fn)
        async def wrapper(request: Any, **kwargs: Any) -> Any:
            nonlocal last_sweep

            # Derive client key
            if key_func is not None:
                client_key = key_func(request)
            else:
                client = request.scope.get("client")
                client_key = client[0] if client else "unknown"

            now = time.monotonic()

            # Evict stale buckets at most once per window. A bucket idle
            # for >= window_seconds is fully refilled, so dropping it is
            # indistinguishable from keeping it -- but keeping every key
            # ever seen leaks memory under client churn.
            if now - last_sweep >= window_seconds:
                stale = [
                    key
                    for key, (_, last_refill) in buckets.items()
                    if now - last_refill >= window_seconds
                ]
                for key in stale:
                    del buckets[key]
                last_sweep = now

            # Refill tokens
            if client_key in buckets:
                tokens, last_refill = buckets[client_key]
                elapsed = now - last_refill
                tokens = min(max_tokens, tokens + elapsed * refill_rate)
            else:
                tokens = float(max_tokens)

            # Check limit
            if tokens < 1.0:
                raise HTTPError(429, "Rate limit exceeded")

            # Consume one token
            buckets[client_key] = (tokens - 1.0, now)

            return await fn(request, **kwargs)

        # Exposed for introspection and tests.
        wrapper._buckets = buckets
        return wrapper

    return decorator
