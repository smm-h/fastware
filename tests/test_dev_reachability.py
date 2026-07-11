"""Dev-mode reachability: /__fastware/ is always a backend prefix.

ViteDevProxy must route the entire /__fastware/ namespace to the backend and
never proxy it to Vite, so diagnostic endpoints like /__fastware/version work
identically in dev and prod.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from fastware import Router, create_app
from fastware.middleware import ViteDevProxy


def test_is_api_request_treats_fastware_namespace_as_backend():
    proxy = ViteDevProxy(lambda s, r, snd: None, vite_port=5173)
    assert proxy._is_api_request("/__fastware/version") is True
    assert proxy._is_api_request("/__fastware/anything") is True


@pytest.mark.anyio
async def test_version_routes_to_backend_through_proxy():
    """Through ViteDevProxy, /__fastware/version reaches the backend (no Vite).

    Vite is pointed at a dead port; if the request were proxied there it would
    fail, so a 200 with build_id proves backend routing.
    """
    app = create_app(Router(), name="devapp")
    # Wrap with the proxy exactly as dev() does. Dead vite port on purpose.
    proxied = ViteDevProxy(app, vite_port=1)

    transport = ASGITransport(app=proxied)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/__fastware/version")

    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "devapp"
    assert "build_id" in body
