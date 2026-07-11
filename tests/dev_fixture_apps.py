"""Importable fixture apps for dev-CLI sw_mode resolution tests.

These are module-level ASGI apps so a subprocess (or the sw_mode probe) can
re-import them by ``tests.dev_fixture_apps:<name>``.
"""

from __future__ import annotations

from pathlib import Path

from fastware import Router, create_app

# static_dir need not exist at create_app time; it is only consulted per-request.
_STATIC = Path(__file__).parent / "_nonexistent_static"

cache_app = create_app(Router(), static_dir=_STATIC, sw_mode="cache")
off_app = create_app(Router(), static_dir=_STATIC, sw_mode="off")
api_only_app = create_app(Router())
