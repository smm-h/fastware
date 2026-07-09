"""Tests for Request.json_as msgspec.Struct dispatch.

json_as type-dispatches: a msgspec.Struct target decodes via
msgspec.json.decode(body, type=...); anything else falls back to the Pydantic
path. Decode/validation failures raise HTTPError(422) in both branches.
"""

from __future__ import annotations

import msgspec
import pytest

from fastware import Router, create_app
from fastware.request import Request
from fastware.responses import HTTPError


class _User(msgspec.Struct):
    name: str
    age: int


def _make_request(body: bytes | None) -> Request:
    scope = {"type": "http", "method": "POST", "path": "/user", "headers": []}
    return Request(scope, {}, body)


def test_json_as_msgspec_struct_round_trips():
    """A valid body decodes into the msgspec.Struct instance."""
    req = _make_request(b'{"name": "Alice", "age": 30}')
    user = req.json_as(_User)
    assert isinstance(user, _User)
    assert user.name == "Alice"
    assert user.age == 30


def test_json_as_msgspec_missing_field_raises_422():
    """A missing required field maps to HTTPError(422), like the Pydantic path."""
    req = _make_request(b'{"name": "Alice"}')
    with pytest.raises(HTTPError) as excinfo:
        req.json_as(_User)
    assert excinfo.value.status_code == 422


def test_json_as_msgspec_wrong_type_raises_422():
    """A wrong field type maps to HTTPError(422)."""
    req = _make_request(b'{"name": "Alice", "age": "nope"}')
    with pytest.raises(HTTPError) as excinfo:
        req.json_as(_User)
    assert excinfo.value.status_code == 422


def test_json_as_msgspec_invalid_json_raises_422():
    """Malformed JSON maps to HTTPError(422)."""
    req = _make_request(b"not json")
    with pytest.raises(HTTPError) as excinfo:
        req.json_as(_User)
    assert excinfo.value.status_code == 422


def test_json_as_msgspec_empty_body_raises_422():
    """An empty body decodes to null, which fails Struct validation -> 422."""
    req = _make_request(None)
    with pytest.raises(HTTPError) as excinfo:
        req.json_as(_User)
    assert excinfo.value.status_code == 422


@pytest.mark.anyio
async def test_json_as_msgspec_through_app():
    """End-to-end: a handler using json_as with a msgspec.Struct body."""
    from httpx import ASGITransport, AsyncClient

    router = Router()

    @router.post("/user")
    async def create_user(req):
        user = req.json_as(_User)
        return {"name": user.name, "age": user.age}

    app = create_app(router, request_id=False, request_timing=False)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/user", json={"name": "Bob", "age": 42})
        assert resp.status_code == 200
        assert resp.json() == {"name": "Bob", "age": 42}

        bad = await client.post("/user", json={"name": "Bob"})
        assert bad.status_code == 422
