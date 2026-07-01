---
title: Core API Reference
description: Reference for fastware's core routing, request handling, response types, and application factory
date: 2026-07-01
---

# Core API Reference

This page documents fastware's core modules: the foundational types, response classes, request handling, routing, WebSocket support, and the application factory. These are the building blocks for every fastware application.

## ASGI Types

Low-level ASGI type aliases used throughout fastware. These are re-exported from the top-level package for convenience.

:-: ref path="src.fastware.types"

## Response Types

HTTP response types for returning data from route handlers. Handlers can return any of these types, or plain `dict`/`list` values (which are automatically wrapped in `JSONResponse`). Also includes cookie helpers and the `HTTPError` exception for error responses.

:-: ref path="src.fastware.responses"

## Request Handling

The `Request` wrapper provides lazy body parsing, query parameter extraction with type coercion and validation, header access, cookie parsing, and per-request state. Handlers receive a `Request` as their first argument.

:-: ref path="src.fastware.request"

## Routing

Path-based HTTP router with `{param}` placeholder syntax, typed parameters (`{id:int}`), greedy path segments (`{path:path}`), and sub-router composition via `include_router`. Supports all standard HTTP methods and WebSocket routes.

:-: ref path="src.fastware.routing"

## WebSocket

WebSocket helper class wrapping the raw ASGI triple into a convenient interface with `accept`, `close`, `send_json`, `receive_json`, and similar methods. Handlers registered via `router.ws()` receive a `WebSocket` instance.

:-: ref path="src.fastware.websocket"

## Application Factory

The `create_app` function assembles a `Router`, optional middleware, static file serving, SPA fallback, lifespan management, and built-in middleware (CORS, request ID, request timing, trusted hosts, Vite dev proxy) into a single ASGI application callable.

:-: table-schema path="src/fastware/app.py" target="AppConfig"

:-: ref path="src.fastware.app"
