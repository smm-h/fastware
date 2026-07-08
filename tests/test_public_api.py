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


def test_types_all_is_exactly_scope_receive_send():
    """fastware.types must declare __all__ so star-imports don't leak helpers."""
    from fastware import types

    assert types.__all__ == ["Scope", "Receive", "Send"]


def test_types_star_import_does_not_leak():
    """'from fastware.types import *' must yield only the ASGI aliases --
    not typing helpers (Any, Awaitable, Callable) or the 'annotations' future
    flag bound at module level."""
    ns: dict = {}
    exec("from fastware.types import *", ns)
    names = {n for n in ns if not n.startswith("__")}
    assert names == {"Scope", "Receive", "Send"}
