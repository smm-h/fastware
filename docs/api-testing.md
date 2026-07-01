---
title: Testing API Reference
description: "API reference for fastware testing: async and sync test clients wrapping httpx with ASGITransport for in-process route testing without a server."
date: 2026-07-01
---

# Testing API Reference

The testing module provides 2 test clients (sync and async) that wrap `httpx` with `ASGITransport`, so tests can exercise a fastware app without starting a real server. Both clients run requests against the ASGI application directly in-process.

`httpx` is a dev/test dependency -- this module should only be imported in test contexts.

:-: ref path="src.fastware.testing"

## AsyncTestClient (async)

Use `AsyncTestClient` in `pytest` async tests for full async/await support. It wraps an `httpx.AsyncClient` with `ASGITransport`, running requests directly against the ASGI application in-process without any network overhead or server startup delay:

```python
import pytest
from fastware import Router, create_app
from fastware.testing import AsyncTestClient

router = Router()

@router.get("/health")
async def health(request):
    return {"status": "ok"}

app = create_app(router)

@pytest.mark.asyncio
async def test_health():
    async with AsyncTestClient(app) as client:
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
```

The `async with` block creates and closes the underlying `httpx.AsyncClient`. Inside the block, `client` is a full `httpx.AsyncClient` with all its methods available (`get`, `post`, `put`, `patch`, `delete`, `options`, `head`).

## TestClient (sync)

Use `TestClient` for synchronous test code when you prefer not to use `async`/`await` in your test functions. It runs an event loop in a background thread, so you can call `.get()`, `.post()`, etc. without `await`, while still exercising the full ASGI application stack including middleware and lifespan:

```python
from fastware import Router, create_app
from fastware.testing import TestClient

router = Router()

@router.get("/health")
async def health(request):
    return {"status": "ok"}

app = create_app(router)

def test_health_sync():
    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
```

`TestClient` supports `get`, `post`, `put`, `patch`, `delete`, `options`, and `head`. It can also be used without a context manager by calling `client.close()` manually.

## Example From the Test Suite

This example from the fastware test suite demonstrates the `AsyncTestClient` in action, testing route matching with path parameter extraction and type coercion, HTTP method dispatch across GET and POST handlers, correct 404 responses for unmatched paths, and proper error handling -- all exercised against a real ASGI application without starting a network server:

:-: code-test path="tests/test_asgi.py" target="TestRouterMatching"

## pytest Integration

A typical `conftest.py` for a fastware project sets up reusable `app` and `client` fixtures that other test modules can inject, reducing boilerplate and ensuring consistent application configuration across all test files:

```python
import pytest
from fastware import Router, create_app
from fastware.testing import AsyncTestClient


def make_app():
    """Create the application for testing."""
    router = Router()

    @router.get("/health")
    async def health(request):
        return {"status": "ok"}

    @router.get("/items/{id:int}")
    async def get_item(request):
        item_id = request.path_params["id"]
        return {"id": item_id}

    return create_app(router)


@pytest.fixture
def app():
    return make_app()


@pytest.fixture
async def client(app):
    async with AsyncTestClient(app) as c:
        yield c
```

Then in test files:

```python
import pytest

@pytest.mark.asyncio
async def test_get_item(client):
    resp = await client.get("/items/42")
    assert resp.status_code == 200
    assert resp.json()["id"] == 42

@pytest.mark.asyncio
async def test_not_found(client):
    resp = await client.get("/nonexistent")
    assert resp.status_code == 404
```
