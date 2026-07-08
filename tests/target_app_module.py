"""Module-level ASGI app used by serve_background/reload callable-target tests.

Must stay importable as ``tests.target_app_module`` so that a spawned server
subprocess can re-import the ``app`` callable by reference.
"""

from __future__ import annotations


async def app(scope, receive, send):
    if scope["type"] != "http":
        return
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain")],
        }
    )
    await send({"type": "http.response.body", "body": b"hello from target_app_module"})
