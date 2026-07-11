"""Tests for the shared _build_headers helper used by both the buffered
response sender (responses._send_response) and the streaming sender
(app._send_stream). Locks in exact header ordering across both paths.
"""

from __future__ import annotations

import pytest

from fastware.app import _send_stream
from fastware.responses import StreamResponse, _build_headers, _send_response


def test_build_headers_order_content_type_extra_cookies():
    headers = _build_headers(
        "application/json",
        {"x-a": "1", "x-b": "2"},
        ["s=1", "c=2"],
    )
    assert headers == [
        [b"content-type", b"application/json"],
        [b"x-a", b"1"],
        [b"x-b", b"2"],
        [b"set-cookie", b"s=1"],
        [b"set-cookie", b"c=2"],
    ]


def test_build_headers_no_extras():
    assert _build_headers("text/plain") == [[b"content-type", b"text/plain"]]


class _Capture:
    def __init__(self):
        self.messages = []

    async def __call__(self, message):
        self.messages.append(message)


@pytest.mark.anyio
async def test_send_response_inserts_content_length_after_content_type():
    send = _Capture()
    await _send_response(
        send,
        200,
        b"hello",
        "text/plain",
        extra_headers={"x-a": "1"},
        cookies=["s=1"],
    )
    start = send.messages[0]
    assert start["headers"] == [
        [b"content-type", b"text/plain"],
        [b"content-length", b"5"],
        [b"x-a", b"1"],
        [b"set-cookie", b"s=1"],
    ]


@pytest.mark.anyio
async def test_send_stream_headers_have_no_content_length():
    async def gen():
        yield "chunk"

    resp = StreamResponse(
        gen(),
        content_type="text/event-stream",
        headers={"x-a": "1"},
        cookies=["s=1"],
    )
    send = _Capture()
    await _send_stream(send, resp)
    start = send.messages[0]
    assert start["headers"] == [
        [b"content-type", b"text/event-stream"],
        [b"x-a", b"1"],
        [b"set-cookie", b"s=1"],
    ]
    assert not any(h[0] == b"content-length" for h in start["headers"])


class _SpyAsyncGen:
    """Async-iterable stand-in that records whether aclose() was called.

    Real async generators forbid attribute assignment, so a spy object is
    needed to observe that _send_stream guaranteed aclose() even when the
    very first send (http.response.start) failed before any iteration.
    """

    def __init__(self):
        self.aclose_called = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def aclose(self):
        self.aclose_called = True


@pytest.mark.anyio
async def test_send_stream_acloses_generator_when_initial_start_send_fails():
    """If http.response.start fails, the generator must still be aclosed.

    Previously the initial send sat outside the try/finally that guarantees
    resp.generator.aclose(), so a failure on the very first send leaked the
    generator. The aclose guarantee now covers that edge too.
    """
    spy = _SpyAsyncGen()
    resp = StreamResponse(spy, content_type="text/event-stream")

    async def failing_send(message):
        raise RuntimeError("start send failed")

    with pytest.raises(RuntimeError, match="start send failed"):
        await _send_stream(failing_send, resp)

    assert spy.aclose_called is True
