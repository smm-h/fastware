"""Tests for the blessed service worker (Phase 12.1).

Covers sw_mode validation (mandatory with a frontend, forbidden without),
per-mode serving of /__fastware/sw.js and /__fastware/sw-register.js, the
reset-mode legacy path interception, and golden assertions on the generated
worker content (cache name embeds the build id, excluded prefixes, headers).
"""

from __future__ import annotations

import pytest

from fastware import Router, create_app
from fastware._assets import load_template, render_asset


def _static_dir(tmp_path):
    d = tmp_path / "static"
    d.mkdir()
    (d / "index.html").write_text("<!DOCTYPE html><html></html>")
    (d / "app-1a2b3c4d.js").write_text("console.log(1)")
    return d


# ---------------------------------------------------------------------------
# Validation: mandatory / forbidden / invalid
# ---------------------------------------------------------------------------


def test_sw_mode_mandatory_with_static_dir(tmp_path):
    with pytest.raises(ValueError, match="sw_mode is required"):
        create_app(Router(), static_dir=_static_dir(tmp_path))


def test_sw_mode_mandatory_with_spa_fallback(tmp_path):
    index = tmp_path / "index.html"
    index.write_text("<html></html>")
    with pytest.raises(ValueError, match="sw_mode is required"):
        create_app(Router(), spa_fallback=index)


def test_sw_mode_forbidden_without_frontend():
    with pytest.raises(ValueError, match="sw_mode is forbidden"):
        create_app(Router(), sw_mode="off")


def test_invalid_sw_mode_rejected(tmp_path):
    with pytest.raises(ValueError, match="invalid sw_mode"):
        create_app(Router(), static_dir=_static_dir(tmp_path), sw_mode="bogus")


def test_sw_mode_off_with_frontend_is_valid(tmp_path):
    # Explicitly opting out is allowed and constructs cleanly.
    create_app(Router(), static_dir=_static_dir(tmp_path), sw_mode="off")


# ---------------------------------------------------------------------------
# cache mode
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_cache_worker_served_with_headers(client_for, tmp_path):
    static = _static_dir(tmp_path)
    app = create_app(Router(), static_dir=static, sw_mode="cache")
    async with client_for(app) as client:
        ver = (await client.get("/__fastware/version")).json()["build_id"]
        resp = await client.get("/__fastware/sw.js")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/javascript"
    assert resp.headers["service-worker-allowed"] == "/"
    body = resp.text
    # Cache name embeds the build id (BUILD_ID holds the exact build id and is
    # concatenated into the cache name).
    assert "fastware-cache-" in body
    assert ver in body


@pytest.mark.anyio
async def test_cache_worker_excludes_api_and_reserved(client_for, tmp_path):
    static = _static_dir(tmp_path)
    app = create_app(
        Router(), static_dir=static, sw_mode="cache", api_prefix="/api"
    )
    async with client_for(app) as client:
        body = (await client.get("/__fastware/sw.js")).text
    # Excluded-prefix array is embedded; must list the reserved namespace, the
    # API prefix, and the backend (SSE/WS) prefixes.
    assert "/__fastware/" in body
    assert "/api" in body
    assert "/events" in body
    assert "/ws" in body


@pytest.mark.anyio
async def test_register_snippet_served_in_cache_mode(client_for, tmp_path):
    static = _static_dir(tmp_path)
    app = create_app(
        Router(), static_dir=static, sw_mode="cache",
        foreign_sw_paths=["/push-sw.js"],
    )
    async with client_for(app) as client:
        resp = await client.get("/__fastware/sw-register.js")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/javascript"
    body = resp.text
    assert "/__fastware/sw.js" in body
    assert "scope" in body
    # Declared foreign path is embedded for the exemption check.
    assert "/push-sw.js" in body


# ---------------------------------------------------------------------------
# reset mode
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_reset_worker_served_at_reserved_path(client_for, tmp_path):
    static = _static_dir(tmp_path)
    app = create_app(Router(), static_dir=static, sw_mode="reset")
    async with client_for(app) as client:
        resp = await client.get("/__fastware/sw.js")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/javascript"
    assert resp.headers["service-worker-allowed"] == "/"
    assert "unregister" in resp.text


@pytest.mark.anyio
async def test_reset_worker_served_at_legacy_path_with_js_mime(client_for, tmp_path):
    static = _static_dir(tmp_path)
    app = create_app(Router(), static_dir=static, sw_mode="reset")
    async with client_for(app) as client:
        resp = await client.get("/sw.js")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/javascript"
    assert "unregister" in resp.text


@pytest.mark.anyio
async def test_reset_custom_legacy_paths(client_for, tmp_path):
    static = _static_dir(tmp_path)
    app = create_app(
        Router(), static_dir=static, sw_mode="reset",
        legacy_sw_paths=["/service-worker.js", "/sw.js"],
    )
    async with client_for(app) as client:
        r1 = await client.get("/service-worker.js")
        r2 = await client.get("/sw.js")
    assert r1.status_code == 200 and "unregister" in r1.text
    assert r2.status_code == 200 and "unregister" in r2.text


@pytest.mark.anyio
async def test_register_snippet_404_in_reset_mode(client_for, tmp_path):
    static = _static_dir(tmp_path)
    app = create_app(Router(), static_dir=static, sw_mode="reset")
    async with client_for(app) as client:
        resp = await client.get("/__fastware/sw-register.js")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# off mode
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_off_mode_sw_404(client_for, tmp_path):
    static = _static_dir(tmp_path)
    app = create_app(Router(), static_dir=static, sw_mode="off")
    async with client_for(app) as client:
        assert (await client.get("/__fastware/sw.js")).status_code == 404
        assert (await client.get("/__fastware/sw-register.js")).status_code == 404


@pytest.mark.anyio
async def test_off_mode_does_not_intercept_legacy_path(client_for, tmp_path):
    """In off mode /sw.js must NOT be seized -- an app may serve its own worker
    there via static files."""
    static = _static_dir(tmp_path)
    (static / "sw.js").write_text("/* the app's own push worker */")
    app = create_app(
        Router(), static_dir=static, static_path="/static",
        spa_fallback=static / "index.html", sw_mode="off",
    )
    async with client_for(app) as client:
        resp = await client.get("/sw.js")
    # Served from the app's static files, not the framework reset worker.
    assert resp.status_code == 200
    assert "push worker" in resp.text
    assert "unregister" not in resp.text


# ---------------------------------------------------------------------------
# client.js is always served (independent of sw_mode; API-only apps included)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_client_js_served_for_api_only_app(client_for):
    app = create_app(Router())  # API-only, no sw_mode
    async with client_for(app) as client:
        resp = await client.get("/__fastware/client.js")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/javascript"
    assert "EventSource" in resp.text
    assert "/__fastware/events" in resp.text


# ---------------------------------------------------------------------------
# Package-data / templating
# ---------------------------------------------------------------------------


def test_templates_loadable_via_importlib_resources():
    """Templates load from the installed distribution via importlib.resources,
    never a source-tree-relative path."""
    for name in (
        "sw_cache.js.tmpl",
        "sw_reset.js.tmpl",
        "sw_register.js.tmpl",
        "client.js.tmpl",
    ):
        text = load_template(name)
        assert text and "fastware" in text


def test_render_asset_raises_on_unresolved_placeholder():
    with pytest.raises(KeyError, match="unresolved placeholder"):
        # Provide no substitution for {{BUILD_ID_JSON}} etc.
        render_asset("sw_cache.js.tmpl", {})
