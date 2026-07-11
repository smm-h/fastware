"""Tests for the /__fastware/version endpoint and the static-asset build id.

The build id is a SHA-256 content hash over static asset file *contents* in
sorted relative-path order. It must be stable across mtime changes and must
change iff the bytes change. The version endpoint is unconditional on every
create_app and is intercepted at the top of the http branch, ahead of mounts
and routes, so a near-root mount cannot shadow it.
"""

from __future__ import annotations

import hashlib

import pytest

from fastware import JSONResponse, Router, create_app
from fastware.app import _EMPTY_BUILD_ID, _compute_static_build_id


def _write_assets(root, files: dict[str, bytes]) -> None:
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


def test_empty_build_id_is_stable_constant():
    """No static assets -> hash of the empty set, a stable constant."""
    assert _EMPTY_BUILD_ID == hashlib.sha256().hexdigest()
    assert _compute_static_build_id(None) == _EMPTY_BUILD_ID


def test_build_id_ignores_mtime(tmp_path):
    """Identical bytes with different mtimes yield the same id."""
    a = tmp_path / "a"
    a.mkdir()
    _write_assets(a, {"index.js": b"console.log(1)", "sub/x.css": b"body{}"})
    id1 = _compute_static_build_id(a)

    # Touch every file to bump mtime without changing bytes.
    import os
    import time

    future = time.time() + 10_000
    for p in a.rglob("*"):
        if p.is_file():
            os.utime(p, (future, future))
    id2 = _compute_static_build_id(a)

    assert id1 == id2


def test_build_id_changes_iff_bytes_change(tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    _write_assets(a, {"index.js": b"console.log(1)"})
    id1 = _compute_static_build_id(a)

    (a / "index.js").write_bytes(b"console.log(2)")
    id2 = _compute_static_build_id(a)
    assert id1 != id2


def test_build_id_identical_across_two_apps_same_bytes(tmp_path):
    """Two independent static dirs with identical bytes hash identically."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    files = {"index.js": b"x=1", "assets/app.css": b".c{}"}
    _write_assets(a, files)
    _write_assets(b, files)
    assert _compute_static_build_id(a) == _compute_static_build_id(b)


@pytest.mark.anyio
async def test_version_endpoint_present_with_zero_config(client_for):
    app = create_app(Router())
    async with client_for(app) as client:
        resp = await client.get("/__fastware/version")
    assert resp.status_code == 200
    data = resp.json()
    assert data["build_id"] == _EMPTY_BUILD_ID
    assert "name" not in data


@pytest.mark.anyio
async def test_version_endpoint_includes_name_when_configured(client_for):
    app = create_app(Router(), name="myapp")
    async with client_for(app) as client:
        resp = await client.get("/__fastware/version")
    data = resp.json()
    assert data["name"] == "myapp"
    assert data["build_id"] == _EMPTY_BUILD_ID


@pytest.mark.anyio
async def test_version_endpoint_reflects_static_bytes(client_for, tmp_path):
    static = tmp_path / "static"
    static.mkdir()
    _write_assets(static, {"index.js": b"hello"})
    app = create_app(Router(), static_dir=static)
    async with client_for(app) as client:
        resp = await client.get("/__fastware/version")
    assert resp.json()["build_id"] == _compute_static_build_id(static)
    assert resp.json()["build_id"] != _EMPTY_BUILD_ID


@pytest.mark.anyio
async def test_near_root_mount_does_not_shadow_version(client_for):
    """A sub-app mounted at "/" must not intercept /__fastware/version."""
    sub = Router()

    @sub.get("/{path:path}")
    async def catch_all(req):
        return JSONResponse({"from": "sub"})

    sub_app = create_app(sub)

    root = Router()
    root.mount("/", sub_app)
    app = create_app(root)

    async with client_for(app) as client:
        resp = await client.get("/__fastware/version")
    assert resp.status_code == 200
    assert "build_id" in resp.json()
    assert resp.json().get("from") != "sub"
