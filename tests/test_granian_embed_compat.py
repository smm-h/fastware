"""Granian embed server API compatibility test.

Catches API breakage on Granian upgrade before it reaches production.
The embed server is used for background mode (foreground=False) because
the standard MPServer cannot run in a daemon thread.
"""

from __future__ import annotations

import asyncio
import inspect


class TestGranianEmbedAPIContract:
    """Verify that the Granian embed server exposes the API surface fastware depends on."""

    def test_embed_server_importable(self) -> None:
        """granian.server.embed.Server must be importable."""
        from granian.server.embed import Server

        assert Server is not None

    def test_accepts_target_parameter(self) -> None:
        """Server.__init__ must accept a 'target' parameter."""
        from granian.server.embed import Server

        sig = inspect.signature(Server.__init__)
        assert "target" in sig.parameters, (
            f"Server.__init__ does not accept 'target'; params: {list(sig.parameters)}"
        )

    def test_has_serve_method(self) -> None:
        """Server must have a serve() method that is async (coroutine function)."""
        from granian.server.embed import Server

        assert hasattr(Server, "serve"), "Server is missing serve() method"
        assert asyncio.iscoroutinefunction(Server.serve), (
            "Server.serve() must be a coroutine function"
        )

    def test_has_stop_method(self) -> None:
        """Server must have a stop() method."""
        from granian.server.embed import Server

        assert hasattr(Server, "stop"), "Server is missing stop() method"
