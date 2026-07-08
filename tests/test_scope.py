"""Tests for the shared scope-level header/cookie helpers (fastware._scope).

These lock in the single implementation used by request.py, auth.py, and
middleware.py so the three consumers cannot drift apart again.
"""

from __future__ import annotations

from fastware import _scope


def _scope_with(headers):
    return {"type": "http", "headers": headers}


def test_header_bytes_returns_first_match():
    scope = _scope_with([(b"authorization", b"Bearer abc"), (b"host", b"x")])
    assert _scope.header_bytes(scope, b"authorization") == b"Bearer abc"


def test_header_bytes_absent_returns_empty():
    assert _scope.header_bytes(_scope_with([]), b"authorization") == b""


def test_header_bytes_case_insensitive():
    scope = _scope_with([(b"Authorization", b"Bearer abc")])
    assert _scope.header_bytes(scope, b"authorization") == b"Bearer abc"


def test_header_str_decodes_latin1():
    scope = _scope_with([(b"x-token", b"caf\xe9")])
    assert _scope.header_str(scope, "x-token") == "café"


def test_header_str_absent_returns_default():
    scope = _scope_with([])
    assert _scope.header_str(scope, "x-token") is None
    assert _scope.header_str(scope, "x-token", "fallback") == "fallback"


def test_header_str_present_but_empty_is_not_default():
    scope = _scope_with([(b"x-token", b"")])
    assert _scope.header_str(scope, "x-token", "fallback") == ""


def test_header_str_case_insensitive_name():
    scope = _scope_with([(b"content-type", b"application/json")])
    assert _scope.header_str(scope, "Content-Type") == "application/json"


def test_cookies_parses_multiple():
    scope = _scope_with([(b"cookie", b"session=abc; csrf_token=xyz")])
    assert _scope.cookies(scope) == {"session": "abc", "csrf_token": "xyz"}


def test_cookies_absent_returns_empty_dict():
    assert _scope.cookies(_scope_with([])) == {}


def test_cookie_single_value_and_default():
    scope = _scope_with([(b"cookie", b"session=abc")])
    assert _scope.cookie(scope, "session") == "abc"
    assert _scope.cookie(scope, "missing") == ""
    assert _scope.cookie(scope, "missing", "d") == "d"
