"""Tests for static-asset Cache-Control headers.

Hashed Vite-style assets (name-<8+ alnum hash>.ext) are immutable and cached
for a year; unhashed assets, index.html, and the SPA fallback are no-cache so
clients always revalidate.
"""

from __future__ import annotations

import pytest

from fastware import Router, create_app
from fastware.app import _cache_control_for

IMMUTABLE = "public, max-age=31536000, immutable"
NO_CACHE = "no-cache"


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("index-4a2b8c9d.js", IMMUTABLE),
        ("app-Bsy8xR2k.css", IMMUTABLE),
        ("vendor-0123456789abcdef.js", IMMUTABLE),
        ("index.js", NO_CACHE),
        ("styles.css", NO_CACHE),
        ("logo.png", NO_CACHE),
        ("chunk-vendor.js", NO_CACHE),  # "vendor" is <8 chars, not a hash
        ("index.html", NO_CACHE),
    ],
)
def test_cache_control_classification(filename, expected):
    assert _cache_control_for(filename) == expected


def _make_static_app(tmp_path):
    static = tmp_path / "static"
    static.mkdir()
    (static / "index-4a2b8c9d.js").write_bytes(b"hashed")
    (static / "styles.css").write_bytes(b"unhashed")
    (static / "index.html").write_bytes(b"<html></html>")
    return static


@pytest.mark.anyio
async def test_hashed_asset_is_immutable(client_for, tmp_path):
    static = _make_static_app(tmp_path)
    app = create_app(Router(), static_dir=static, static_path="/assets", sw_mode="off")
    async with client_for(app) as client:
        resp = await client.get("/assets/index-4a2b8c9d.js")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == IMMUTABLE


@pytest.mark.anyio
async def test_unhashed_asset_is_no_cache(client_for, tmp_path):
    static = _make_static_app(tmp_path)
    app = create_app(Router(), static_dir=static, static_path="/assets", sw_mode="off")
    async with client_for(app) as client:
        resp = await client.get("/assets/styles.css")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == NO_CACHE


@pytest.mark.anyio
async def test_index_html_via_spa_static_is_no_cache(client_for, tmp_path):
    """index.html served as an actual static file under the SPA root is no-cache."""
    static = _make_static_app(tmp_path)
    app = create_app(Router(), spa_fallback=static / "index.html", sw_mode="off")
    async with client_for(app) as client:
        resp = await client.get("/index.html")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == NO_CACHE


@pytest.mark.anyio
async def test_spa_fallback_path_is_no_cache(client_for, tmp_path):
    """A non-file GET path falls back to index.html with no-cache."""
    static = _make_static_app(tmp_path)
    app = create_app(Router(), spa_fallback=static / "index.html", sw_mode="off")
    async with client_for(app) as client:
        resp = await client.get("/some/client/route")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == NO_CACHE
