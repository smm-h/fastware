---
title: SSE Broadcasting
description: How to use fastware's built-in SSE broadcaster for real-time server-to-client communication
date: 2026-07-01
---

# SSE Broadcasting

Server-Sent Events (SSE) provide a simple, HTTP-based mechanism for pushing data from server to client. Unlike WebSockets, SSE is unidirectional (server to client only), uses plain HTTP, works through proxies and firewalls, and reconnects automatically.

## When to use SSE vs WebSocket

| Criterion | SSE | WebSocket |
|---|---|---|
| Direction | Server to client only | Bidirectional |
| Protocol | HTTP (text/event-stream) | Upgraded connection (ws://) |
| Reconnection | Built into the browser (EventSource auto-reconnects) | Manual reconnection logic required |
| Proxy/firewall | Works through standard HTTP infrastructure | May be blocked by some proxies |
| Use case | Live dashboards, notifications, progress updates, log tailing | Chat, collaborative editing, gaming |

Use SSE when you only need to push data to the client. Use WebSockets when the client needs to send data back over the same connection.

## Basic setup

### 1. Create a Broadcaster

```python
from fastware import Broadcaster, sse_route

broadcaster = Broadcaster()
```

The Broadcaster manages a list of connected clients. Each client gets its own async queue. When you broadcast an event, it is pushed to every client's queue.

### 2. Register event types

```python
broadcaster.register_event("update")
broadcaster.register_event("error")
broadcaster.register_event("heartbeat")
```

By default, the Broadcaster runs in **strict mode** -- broadcasting an unregistered event name raises `ValueError`. This prevents typos and ensures the event vocabulary is explicit.

### 3. Wire to a route

```python
from fastware import Router, create_app

router = Router()
router.add_route("GET", "/events", sse_route(broadcaster))
```

The `sse_route` helper returns an async handler that calls `broadcaster.stream(request)`, which creates a per-client queue and returns a `StreamResponse` with `content-type: text/event-stream`.

### 4. Broadcast from handlers

```python
@router.post("/items")
async def create_item(request):
    item = request.json
    # ... save to database ...
    broadcaster.broadcast("update", {"action": "created", "item": item})
    return {"ok": True}
```

`broadcast()` is synchronous -- it pushes the formatted SSE message to every client queue without awaiting. Clients whose queues are full (they fell behind) are pruned automatically.

### 5. Create the app

```python
app = create_app(router)
```

## Client-side JavaScript

```javascript
const source = new EventSource("/events");

source.addEventListener("update", (event) => {
    const data = JSON.parse(event.data);
    console.log("Update received:", data);
});

source.addEventListener("error", (event) => {
    const data = JSON.parse(event.data);
    console.error("Server error:", data);
});

// Connection status
source.onopen = () => console.log("SSE connected");
source.onerror = () => console.log("SSE reconnecting...");
```

The browser's `EventSource` automatically reconnects if the connection drops. Events are dispatched by their `event:` field, which maps to the first argument of `broadcaster.broadcast()`.

## Heartbeat configuration

Long-lived SSE connections can be silently dropped by proxies, load balancers, or firewalls that enforce idle timeouts. Heartbeats prevent this by sending periodic SSE comments (`: heartbeat\n\n`) that keep the connection alive without triggering client-side event handlers.

```python
broadcaster = Broadcaster(heartbeat_interval=30)  # seconds
```

When `heartbeat_interval` is set, the event generator sends a comment line if no real event arrives within the interval. SSE comments (lines starting with `:`) are ignored by `EventSource` -- they keep the TCP connection alive without producing a JavaScript event.

If `heartbeat_interval` is `None` (the default), no heartbeats are sent and the generator blocks indefinitely waiting for real events.

## Strict mode vs permissive mode

**Strict mode** (default, `strict=True`):

```python
broadcaster = Broadcaster(strict=True)
broadcaster.register_event("update")
broadcaster.broadcast("update", {"ok": True})   # works
broadcaster.broadcast("typo", {"ok": True})     # raises ValueError
```

Strict mode catches typos and enforces a declared event vocabulary. Register all event types before broadcasting.

**Permissive mode** (`strict=False`):

```python
broadcaster = Broadcaster(strict=False)
broadcaster.broadcast("anything", {"ok": True})  # works without registration
```

Permissive mode skips event type validation. Use this when event types are dynamic or user-defined.

## Buffer size

Each client gets a queue with a maximum size (default 256 messages):

```python
broadcaster = Broadcaster(buffer_size=512)
```

When a client's queue is full (the client is not consuming messages fast enough), the client is pruned from the client list on the next `broadcast()` call. This prevents a slow consumer from causing memory growth.

## Introspection

```python
broadcaster.client_count   # number of connected clients
broadcaster.event_types    # frozenset of registered event types
```

## Complete example

```python
from fastware import Router, Broadcaster, sse_route, create_app, serve

broadcaster = Broadcaster(heartbeat_interval=30)
broadcaster.register_event("message")
broadcaster.register_event("status")

router = Router()
router.add_route("GET", "/events", sse_route(broadcaster))

@router.post("/send")
async def send_message(request):
    data = request.json
    broadcaster.broadcast("message", {"text": data["text"]})
    return {"sent": True}

@router.get("/status")
async def get_status(request):
    broadcaster.broadcast("status", {"clients": broadcaster.client_count})
    return {"clients": broadcaster.client_count}

app = create_app(router)

if __name__ == "__main__":
    serve(app, foreground=True, host="127.0.0.1", port=8000)
```

## API reference

:-: ref path="src.fastware.sse"
