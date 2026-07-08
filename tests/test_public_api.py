"""Public API surface guards.

ParsedSegment is an internal representation (a bare tuple alias for a parsed
route segment). It must not leak into the top-level public API, but must stay
importable from fastware.routing for internal use.
"""

from __future__ import annotations

import fastware


def test_parsed_segment_not_in_public_all():
    assert "ParsedSegment" not in fastware.__all__


def test_parsed_segment_not_a_top_level_attribute():
    assert not hasattr(fastware, "ParsedSegment")


def test_parsed_segment_still_internal_in_routing():
    from fastware.routing import ParsedSegment  # noqa: F401  (import must succeed)


def test_router_still_public():
    assert "Router" in fastware.__all__
    assert hasattr(fastware, "Router")


def test_all_public_symbols_resolve():
    # Every name advertised in __all__ must actually be accessible (this also
    # exercises the lazy server-symbol __getattr__ path).
    for name in fastware.__all__:
        assert hasattr(fastware, name), f"{name} in __all__ but not accessible"
