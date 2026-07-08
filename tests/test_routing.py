"""Unit tests for fastware.routing.Router method dispatch.

Covers HEAD-served-via-GET, 405 method-mismatch detection via
``allowed_methods``, and the collapsed method-decorator factory.
"""

from __future__ import annotations

from fastware import Router


# ---------------------------------------------------------------------------
# HEAD is served by the GET handler
# ---------------------------------------------------------------------------


class TestHeadServedByGet:
    def test_head_matches_get_route(self):
        router = Router()

        @router.get("/thing")
        async def handler(req):
            return {"ok": True}

        match = router.match("HEAD", "/thing")
        assert match is not None
        assert match[0] is handler

    def test_head_matches_get_route_with_params(self):
        router = Router()

        @router.get("/item/{id:int}")
        async def handler(req):
            return {"ok": True}

        match = router.match("HEAD", "/item/42")
        assert match is not None
        assert match[0] is handler
        assert match[1] == {"id": 42}

    def test_head_prefers_explicit_head_route(self):
        router = Router()

        @router.get("/thing")
        async def get_handler(req):
            return {"g": True}

        # Register an explicit HEAD route programmatically; it must win.
        async def head_handler(req):
            return {"h": True}

        router.add_route("HEAD", "/thing", head_handler)

        match = router.match("HEAD", "/thing")
        assert match is not None
        assert match[0] is head_handler

    def test_head_on_unknown_path_returns_none(self):
        router = Router()

        @router.get("/thing")
        async def handler(req):
            return {"ok": True}

        assert router.match("HEAD", "/other") is None

    def test_head_with_no_get_route_returns_none(self):
        router = Router()

        @router.post("/thing")
        async def handler(req):
            return {"ok": True}

        assert router.match("HEAD", "/thing") is None


# ---------------------------------------------------------------------------
# 405: path matched but method mismatched
# ---------------------------------------------------------------------------


class TestAllowedMethods:
    def test_post_to_get_only_path_does_not_match(self):
        router = Router()

        @router.get("/thing")
        async def handler(req):
            return {"ok": True}

        assert router.match("POST", "/thing") is None

    def test_allowed_methods_reports_registered_method(self):
        router = Router()

        @router.get("/thing")
        async def handler(req):
            return {"ok": True}

        assert router.allowed_methods("/thing") == {"GET", "HEAD"}

    def test_allowed_methods_multiple(self):
        router = Router()

        @router.get("/thing")
        async def get_handler(req):
            return {}

        @router.post("/thing")
        async def post_handler(req):
            return {}

        @router.delete("/thing")
        async def delete_handler(req):
            return {}

        assert router.allowed_methods("/thing") == {"GET", "HEAD", "POST", "DELETE"}

    def test_allowed_methods_no_head_without_get(self):
        router = Router()

        @router.post("/thing")
        async def handler(req):
            return {}

        assert router.allowed_methods("/thing") == {"POST"}

    def test_allowed_methods_empty_for_unknown_path(self):
        router = Router()

        @router.get("/thing")
        async def handler(req):
            return {}

        assert router.allowed_methods("/nope") == set()

    def test_allowed_methods_with_path_params(self):
        router = Router()

        @router.get("/item/{id:int}")
        async def handler(req):
            return {}

        assert router.allowed_methods("/item/7") == {"GET", "HEAD"}
        # Non-coercible param means the path does not match this route.
        assert router.allowed_methods("/item/abc") == set()

    def test_allowed_methods_with_greedy_path(self):
        router = Router()

        @router.get("/files/{rest:path}")
        async def handler(req):
            return {}

        assert router.allowed_methods("/files/a/b/c") == {"GET", "HEAD"}


# ---------------------------------------------------------------------------
# Collapsed method-decorator factory still registers all five verbs
# ---------------------------------------------------------------------------


class TestMethodDecorators:
    def test_all_verbs_register(self):
        router = Router()
        recorded = {}

        for verb, path in [
            ("get", "/g"),
            ("post", "/po"),
            ("put", "/pu"),
            ("patch", "/pa"),
            ("delete", "/d"),
        ]:
            deco = getattr(router, verb)

            @deco(path)
            async def handler(req):
                return {}

            recorded[verb] = (handler, path)

        assert router.match("GET", "/g")[0] is recorded["get"][0]
        assert router.match("POST", "/po")[0] is recorded["post"][0]
        assert router.match("PUT", "/pu")[0] is recorded["put"][0]
        assert router.match("PATCH", "/pa")[0] is recorded["patch"][0]
        assert router.match("DELETE", "/d")[0] is recorded["delete"][0]

    def test_decorator_returns_original_function(self):
        router = Router()

        async def handler(req):
            return {}

        wrapped = router.get("/x")(handler)
        assert wrapped is handler

    def test_deps_and_response_model_forwarded(self):
        router = Router()

        def dep():
            return 1

        class Model:
            pass

        @router.post("/x", deps={"d": dep}, response_model=Model)
        async def handler(req):
            return {}

        result = router._match_with_deps("POST", "/x")
        assert result is not None
        _handler, _params, deps, resp_model = result
        assert deps == {"d": dep}
        assert resp_model is Model


# ---------------------------------------------------------------------------
# WebSocket matching routes through the shared _match_pattern core
# ---------------------------------------------------------------------------


class TestWebSocketMatching:
    """WS matching must behave identically to HTTP matching now that both
    share Router._match_pattern -- literals, typed coercion, and greedy
    :path params all included."""

    def test_ws_literal_match(self):
        router = Router()

        @router.ws("/chat")
        async def chat(ws):
            return None

        result = router.match_ws("/chat")
        assert result is not None
        handler, params = result
        assert handler is chat
        assert params == {}

    def test_ws_typed_param_coerced(self):
        router = Router()

        @router.ws("/room/{id:int}")
        async def room(ws):
            return None

        result = router.match_ws("/room/42")
        assert result is not None
        _handler, params = result
        assert params == {"id": 42}
        assert isinstance(params["id"], int)

    def test_ws_typed_param_coercion_failure_does_not_match(self):
        router = Router()

        @router.ws("/room/{id:int}")
        async def room(ws):
            return None

        assert router.match_ws("/room/notanint") is None

    def test_ws_greedy_path_param(self):
        router = Router()

        @router.ws("/files/{rest:path}")
        async def files(ws):
            return None

        result = router.match_ws("/files/a/b/c")
        assert result is not None
        _handler, params = result
        assert params == {"rest": "a/b/c"}

    def test_ws_no_match_returns_none(self):
        router = Router()

        @router.ws("/chat")
        async def chat(ws):
            return None

        assert router.match_ws("/nope") is None

    def test_ws_deps_forwarded(self):
        router = Router()

        def dep():
            return 1

        @router.ws("/chat", deps={"d": dep})
        async def chat(ws):
            return None

        result = router._match_ws_with_deps("/chat")
        assert result is not None
        _handler, _params, deps = result
        assert deps == {"d": dep}
