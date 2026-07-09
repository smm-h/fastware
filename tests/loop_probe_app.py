"""Module-level ASGI app that reports the running event loop's type.

Used by serve()/serve_background tests to verify which event loop Granian
actually runs under. Must stay importable as ``tests.loop_probe_app`` so a
spawned server subprocess can re-import the ``app`` callable by reference.
"""

from __future__ import annotations

import asyncio


async def app(scope, receive, send):
    if scope["type"] != "http":
        return
    loop = asyncio.get_running_loop()
    # e.g. "asyncio.unix_events._UnixSelectorEventLoop", "uvloop.Loop",
    # "rloop.EventLoop" -- the module prefix identifies the loop implementation.
    info = f"{type(loop).__module__}.{type(loop).__qualname__}"
    body = info.encode()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain")],
        }
    )
    await send({"type": "http.response.body", "body": body})
