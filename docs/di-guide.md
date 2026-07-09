---
title: Dependency Injection
description: "Guide to fastware dependency injection: per-request resolution with caching, sync/async factories, generator factories with reverse-order cleanup, router-level dependencies, and factory overrides for testing."
date: 2026-07-01
---

# Dependency Injection

fastware's `DependencyResolver` provides per-request dependency resolution with caching and automatic cleanup. It supports sync and async factory functions, the yield pattern for resource lifecycle management, and dependency overrides for testing.

## What DI solves

Web handlers often need shared resources -- database connections, authenticated user objects, configuration, external API clients. Managing these manually leads to 3 common problems that grow worse as the application scales. Dependency injection solves all 3 by letting the framework create, cache, and clean up resources per request. Without DI, you either:

- Create resources inside each handler (wasteful, no sharing)
- Use module-level globals (hard to test, no per-request lifecycle)
- Pass everything through middleware and request state (boilerplate)

DI lets you declare what a handler needs, and the framework resolves it once per request, caches it (so the same factory called twice returns the same instance), and cleans it up when the request is done. Generator factories are cleaned up in reverse order -- last resolved, first cleaned.

## Basic usage

### 1. Define factory functions

A factory function receives a `Request` and returns the dependency value. Factories can be either sync or async, and can optionally use the yield pattern for resource lifecycle management with automatic cleanup:

```python
async def get_db(request):
    return await create_connection(DATABASE_URL)
```

Factories that don't need request context can omit the parameter:

```python
async def get_config():
    return load_config()
```

### 2. Declare dependencies on routes

Pass a `deps` dict mapping parameter names to factory callables on any route decorator. The resolver calls each factory once per request, caches the result, and injects it as a keyword argument:

```python
from fastware import Router

router = Router()

@router.get("/users", deps={"db": get_db})
async def list_users(request, db):
    rows = await db.fetch_all("SELECT * FROM users")
    return [dict(r) for r in rows]
```

The resolved dependency is passed as a keyword argument matching the dict key. The handler signature must accept the parameter name.

### 3. Create the app

```python
from fastware import create_app

app = create_app(router)
```

The app factory creates a `DependencyResolver` internally and uses it to resolve route dependencies on each request.

## Sync and async factories

Both sync and async factory functions are supported. The resolver detects whether a factory returns an awaitable and handles it automatically, so you can mix sync and async factories freely in the same route's deps dict:

```python
# Sync factory
def get_settings(request):
    return Settings(debug=request.header("X-Debug") == "true")

# Async factory
async def get_db(request):
    return await create_connection(DATABASE_URL)
```

If a factory returns an awaitable, the resolver awaits it automatically.

## Generator factories (yield pattern)

For resources that need cleanup (database connections, file handles, transactions), use the yield pattern. The factory yields the value, and the code after `yield` runs automatically after the handler completes, even if the handler raises an exception. This ensures resources are never leaked:

### Sync generator

```python
def get_db_session(request):
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
```

### Async generator

```python
async def get_db(request):
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        yield conn
    finally:
        await conn.close()
```

Cleanup runs in reverse order (last resolved, first cleaned up). Cleanup errors are suppressed -- a failing cleanup does not mask the handler's response or exception.

## Per-request caching

When the same factory is used in multiple deps on the same request, it is resolved exactly once and the cached result is reused for all subsequent references. This prevents creating duplicate database connections or re-authenticating on every dependency resolution:

```python
async def get_db(request):
    return await create_connection(DATABASE_URL)

async def get_user_repo(request):
    # If get_db is also in this request's deps, it returns the same connection
    return UserRepository(await create_connection(DATABASE_URL))
```

Caching is based on the factory function's identity (`id(factory)`). If two dependency names point to the same factory callable, they get the same resolved value.

## Router-level dependencies

Apply dependencies to all routes in a sub-router using `include_router`, so every route under that prefix automatically receives the specified dependencies without repeating the `deps` dict on each route decorator:

```python
api_router = Router()

@api_router.get("/items")
async def list_items(request, db):
    return await db.fetch_all("SELECT * FROM items")

@api_router.get("/items/{id:int}")
async def get_item(request, db):
    item_id = request.path_params["id"]
    return await db.fetch_one("SELECT * FROM items WHERE id = $1", item_id)

main_router = Router()
main_router.include_router(api_router, prefix="/api", deps={"db": get_db})
```

Router-level deps are merged with per-route deps. Per-route deps override router-level deps if there's a name conflict.

Handlers that don't accept a particular dependency keyword are not affected -- the resolver filters resolved deps to only those the handler's signature accepts.

## Dependency overrides for testing

Replace factory functions with test doubles using `dependency_overrides` on `create_app`. This lets you substitute real databases, API clients, and auth providers with in-memory fakes during testing, without changing any route code:

```python
from fastware import create_app, AppConfig

# Production factory
async def get_db(request):
    return await create_connection(REAL_DATABASE_URL)

# Test factory
async def get_test_db():
    return FakeDatabase()

# Create app with overrides
app = create_app(router, config=AppConfig(
    dependency_overrides={get_db: get_test_db},
))
```

When the resolver encounters `get_db` in a route's deps, it calls `get_test_db` instead. The override factory can omit the `request` parameter if it doesn't need it.

This integrates with fastware's test client:

```python
from fastware.testing import TestClient

def test_list_users():
    app = create_app(router, dependency_overrides={get_db: get_test_db})
    with TestClient(app) as client:
        resp = client.get("/api/users")
        assert resp.status_code == 200
```

## Error handling during resolution

If a factory raises an exception during resolution, all generator cleanups that have already yielded are run before the exception propagates. For example, if 5 generators have yielded and the 6th factory raises, all 5 are cleaned up in reverse order. This ensures resources are not leaked even when resolution fails partway through.

## Complete example

```python
from fastware import Router, create_app, serve

# -- Factories ---------------------------------------------------------------

async def get_db(request):
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        yield conn
    finally:
        await conn.close()

async def get_current_user(request):
    token = request.header("Authorization", "").removeprefix("Bearer ")
    if not token:
        raise HTTPError(401, "Missing token")
    return await verify_token(token)

# -- Routes ------------------------------------------------------------------

router = Router()

@router.get("/profile", deps={"user": get_current_user, "db": get_db})
async def get_profile(request, user, db):
    profile = await db.fetchrow(
        "SELECT * FROM profiles WHERE user_id = $1", user["id"]
    )
    return dict(profile)

@router.get("/health")
async def health(request):
    return {"status": "ok"}

# -- App ---------------------------------------------------------------------

app = create_app(router)

if __name__ == "__main__":
    serve(app, foreground=True, host="127.0.0.1", port=8000)
```

## API reference

See the full `DependencyResolver` class reference below, which documents all public methods for factory registration, per-request resolution with identity-based caching, generator cleanup ordering, and dependency override management for test environments:

:-: ref path="src.fastware.di"
