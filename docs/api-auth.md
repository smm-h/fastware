---
title: Auth API Reference
description: JWT tokens, password hashing, user stores, CSRF protection, rate limiting, and session management
date: 2026-07-01
---

# Auth API Reference

:<: callout-warning
::: The auth module requires the `fastware[auth]` extra, which installs `PyJWT` and `bcrypt`. Install with:
:::
::: ```bash
::: uv add "fastware[auth]"
::: ```
:::
::: Importing `fastware.auth` without these dependencies will raise `ImportError` at call time.
:>:

The auth module provides JWT token operations, password hashing, user storage, CSRF protection, rate limiting, and session cookie management. All functions are pure and DI-compatible, with no framework-specific dependencies beyond fastware's own ASGI types.

:-: ref path="src.fastware.auth"

## Practical Example: JWT Auth on a Route

Setting up JWT authentication on a protected route using the dependency injection system:

```python
from fastware import Router, create_app
from fastware.auth import (
    create_token,
    get_current_user,
    require_role,
    hash_password,
    verify_password,
    CSRFMiddleware,
    set_session_cookies,
    clear_session_cookies,
)

router = Router()


@router.post("/login")
async def login(request):
    """Authenticate a user and return a JWT token with session cookies."""
    data = request.json
    username = data["username"]
    password = data["password"]

    # Look up the user (your storage layer here)
    user = user_store.find_user(username)
    if not user or not verify_password(password, user["password_hash"]):
        raise HTTPError(401, "Invalid credentials")

    # Create a JWT token
    jwt_secret = request.state["config"]["jwt_secret"]
    token = create_token(username, user["role"], jwt_secret)
    csrf_token = secrets.token_urlsafe(32)

    return JSONResponse(
        {"token": token, "username": username},
        cookies=set_session_cookies(token, csrf_token),
    )


@router.get("/profile", deps={"user": get_current_user})
async def profile(request, user):
    """Return the authenticated user's profile."""
    return {"username": user["sub"], "role": user["role"]}


@router.get("/admin", deps={"user": require_role("admin")})
async def admin_dashboard(request, user):
    """Admin-only endpoint using role-based access control."""
    return {"message": f"Welcome, admin {user['sub']}"}


# Apply CSRF protection as middleware
app = create_app(
    router,
    middleware=[
        lambda app: CSRFMiddleware(app, exempt_paths=["/login"]),
    ],
)
```

The `get_current_user` dependency reads the JWT from the `Authorization` header, `session` cookie, or `?token=` query parameter (in that order). It validates the token against the secret stored in `request.state["config"]["jwt_secret"]` and returns the decoded claims dict. The `require_role` factory wraps `get_current_user` with an additional role check.
