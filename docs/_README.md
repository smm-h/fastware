---
title: fastware
description: A fast, batteries-included ASGI framework built on msgspec and Granian
date: 2026-07-01
---

# fastware

:-: var key="project.description"

[![PyPI version](https://img.shields.io/pypi/v/fastware)](https://pypi.org/project/fastware/)
[![Python 3.11+](https://img.shields.io/pypi/pyversions/fastware)](https://pypi.org/project/fastware/)
[![MIT License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![PyPI downloads](https://img.shields.io/pypi/dm/fastware)](https://pypi.org/project/fastware/)

## Why fastware?

- **msgspec for JSON** -- 10-75x faster serialization than Pydantic. No schema compilation step, no startup penalty.
- **Granian server included** -- Rust-based ASGI server with managed lifecycle, PID files, and signal handling. No need to install or configure a separate server.
- **Batteries included** -- SSE broadcasting, dependency injection, authentication (JWT + passwords + CSRF), middleware suite (CORS, tracing, trusted host), test client, background tasks, and structured logging are all built in.
- **Minimal core dependencies** -- The framework core depends only on msgspec and granian. Everything else is opt-in via extras.

## Quick example

```python
import msgspec
from fastware import Router, JSONResponse, create_app, serve

router = Router()

class User(msgspec.Struct):
    id: int
    name: str
    email: str

USERS = {
    1: User(id=1, name="Alice", email="alice@example.com"),
    2: User(id=2, name="Bob", email="bob@example.com"),
}

@router.get("/users")
async def list_users(req):
    return JSONResponse([u for u in USERS.values()])

@router.get("/users/{id:int}")
async def get_user(req):
    user = USERS.get(req.path_params["id"])
    if not user:
        return JSONResponse({"error": "not found"}, status=404)
    return JSONResponse(user)

app = create_app(router)

if __name__ == "__main__":
    serve(app, foreground=True, host="127.0.0.1", port=8000)
```

## Feature overview

:-: list-features path="src/fastware/"

## Installation

```bash
pip install fastware            # core only
pip install fastware[auth]      # + JWT, password hashing, CSRF
pip install fastware[testing]   # + async test client (httpx)
pip install fastware[all]       # everything
```

## Dependencies

:-: table-dep path="pyproject.toml"

## Project structure

:-: list-tree path="src/fastware/" depth="1"

## How it compares

| Feature | fastware | FastAPI | Starlette | Litestar |
|---|---|---|---|---|
| JSON engine | msgspec | Pydantic | none (BYO) | msgspec or attrs |
| ASGI server | Granian (included) | BYO (uvicorn) | BYO (uvicorn) | BYO (uvicorn) |
| SSE built-in | yes | no | no | yes |
| DI built-in | yes | yes | no | yes |
| Server lifecycle | PID files, signals, status checks | none | none | none |
| SPA fallback | yes | no | no | no |
| Test client | built-in (httpx) | via Starlette | yes | yes |
| Middleware suite | CORS, tracing, trusted host, Vite proxy | via Starlette | yes | yes |

## Documentation

Full documentation is available at [fastware.smmh.dev](https://fastware.smmh.dev).

## Built on fastware

- [wesktop](https://github.com/smm-h/wesktop) -- a Python framework for building web-based desktop applications, using fastware for its ASGI layer and server lifecycle.

## License

[MIT](LICENSE)
