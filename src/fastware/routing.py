"""Path-based HTTP router with {param} placeholders and type coercion."""

from __future__ import annotations

from typing import Any, Callable


# ---------------------------------------------------------------------------
# Path parameter type converters
# ---------------------------------------------------------------------------

_TYPE_CONVERTERS: dict[str, type] = {
    "str": str,
    "int": int,
}


def _parse_segment(seg: str) -> tuple[str | None, str | None, type | None]:
    """Parse a route pattern segment into (literal, param_name, converter).

    Returns one of:
    - (literal_str, None, None) for plain segments like "api"
    - (None, param_name, converter) for parameterized segments like {id:int}
    - (None, param_name, None) for :path segments (greedy)
    """
    if not (seg.startswith("{") and seg.endswith("}")):
        return (seg, None, None)
    inner = seg[1:-1]
    if ":" in inner:
        name, type_name = inner.split(":", 1)
        if type_name == "path":
            # Greedy path segment -- converter is None, caller checks name
            return (None, name, None)
        converter = _TYPE_CONVERTERS.get(type_name)
        if converter is None:
            raise ValueError(f"Unknown path parameter type: {type_name!r}")
        return (None, name, converter)
    # Plain {param} -- defaults to str
    return (None, inner, str)


# Parsed segment tuple: (literal | None, param_name | None, converter | None)
ParsedSegment = tuple[str | None, str | None, type | None]


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

class Router:
    """Simple path-based HTTP router using {param} placeholders and type coercion.

    Supports {param}, {param:str}, {param:int}, and {param:path} syntax.
    """

    def __init__(self) -> None:
        # (method, parsed_segments, handler, deps_dict, response_model)
        self._routes: list[tuple[str, list[ParsedSegment], Callable, dict[str, Callable], type | None]] = []
        # WebSocket routes: (parsed_segments, handler, deps_dict)
        self._ws_routes: list[tuple[list[ParsedSegment], Callable, dict[str, Callable]]] = []

    def get(self, path: str, *, deps: dict[str, Callable] | None = None, response_model: type | None = None) -> Callable:
        """Decorator to register a GET handler."""
        def decorator(fn: Callable) -> Callable:
            self.add_route("GET", path, fn, deps=deps, response_model=response_model)
            return fn
        return decorator

    def post(self, path: str, *, deps: dict[str, Callable] | None = None, response_model: type | None = None) -> Callable:
        """Decorator to register a POST handler."""
        def decorator(fn: Callable) -> Callable:
            self.add_route("POST", path, fn, deps=deps, response_model=response_model)
            return fn
        return decorator

    def delete(self, path: str, *, deps: dict[str, Callable] | None = None, response_model: type | None = None) -> Callable:
        """Decorator to register a DELETE handler."""
        def decorator(fn: Callable) -> Callable:
            self.add_route("DELETE", path, fn, deps=deps, response_model=response_model)
            return fn
        return decorator

    def put(self, path: str, *, deps: dict[str, Callable] | None = None, response_model: type | None = None) -> Callable:
        """Decorator to register a PUT handler."""
        def decorator(fn: Callable) -> Callable:
            self.add_route("PUT", path, fn, deps=deps, response_model=response_model)
            return fn
        return decorator

    def patch(self, path: str, *, deps: dict[str, Callable] | None = None, response_model: type | None = None) -> Callable:
        """Decorator to register a PATCH handler."""
        def decorator(fn: Callable) -> Callable:
            self.add_route("PATCH", path, fn, deps=deps, response_model=response_model)
            return fn
        return decorator

    def add_route(
        self,
        method: str,
        path: str,
        handler: Callable,
        *,
        deps: dict[str, Callable] | None = None,
        response_model: type | None = None,
    ) -> None:
        """Programmatic route registration."""
        raw_segments = path.strip("/").split("/")
        parsed = [_parse_segment(s) for s in raw_segments]
        self._routes.append((method, parsed, handler, deps or {}, response_model))

    def ws(self, path: str, *, deps: dict[str, Callable] | None = None) -> Callable:
        """Decorator to register a WebSocket handler."""
        def decorator(fn: Callable) -> Callable:
            self.add_ws_route(path, fn, deps=deps)
            return fn
        return decorator

    def add_ws_route(
        self,
        path: str,
        handler: Callable,
        *,
        deps: dict[str, Callable] | None = None,
    ) -> None:
        """Register a WebSocket handler for a path pattern (supports {param})."""
        raw_segments = path.strip("/").split("/")
        parsed = [_parse_segment(s) for s in raw_segments]
        self._ws_routes.append((parsed, handler, deps or {}))

    def include_router(
        self,
        other: Router,
        prefix: str | None = None,
        deps: dict[str, Callable] | None = None,
    ) -> None:
        """Copy all routes from *other* into this router.

        If *prefix* is given (e.g. ``"/api/v1"``), its segments are
        prepended to every copied route's pattern.

        If *deps* is given (a dict mapping names to factory callables),
        they are merged into each copied route's deps. Router-level deps
        are listed first so that per-handler deps can override them.
        """
        prefix_segs: list[ParsedSegment] = []
        if prefix:
            prefix_segs = [_parse_segment(s) for s in prefix.strip("/").split("/")]

        extra_deps = deps or {}

        for method, pattern, handler, existing_deps, resp_model in other._routes:
            merged_pattern = prefix_segs + pattern
            # Router-level deps first, per-handler deps override
            merged_deps = {**extra_deps, **existing_deps}
            self._routes.append((method, merged_pattern, handler, merged_deps, resp_model))

        # Copy WebSocket routes with prefix and deps
        for pattern, handler, existing_ws_deps in other._ws_routes:
            merged_pattern = prefix_segs + pattern
            merged_ws_deps = {**extra_deps, **existing_ws_deps}
            self._ws_routes.append((merged_pattern, handler, merged_ws_deps))

    def match(self, method: str, path: str) -> tuple[Callable, dict[str, Any]] | None:
        """Return (handler, path_params) or None if no route matches.

        Path parameter values are coerced to their declared types (e.g.,
        {id:int} produces an int).  If coercion fails the route does not
        match, allowing fall-through to 404.
        """
        result = self._match_with_deps(method, path)
        if result is None:
            return None
        handler, params, _deps, _resp_model = result
        return handler, params

    def _match_with_deps(
        self, method: str, path: str,
    ) -> tuple[Callable, dict[str, Any], dict[str, Callable], type | None] | None:
        """Return (handler, path_params, deps, response_model) or None.

        Internal variant of :meth:`match` that also returns the merged
        dependency dict and response_model for the matched route. Used by
        ``create_app`` for DI resolution and response validation.
        """
        segments = path.strip("/").split("/")
        for route_method, pattern, handler, route_deps, resp_model in self._routes:
            if route_method != method:
                continue

            # Check for :path segments -- they change matching semantics
            path_seg_idx = None
            for i, (lit, name, conv) in enumerate(pattern):
                if name is not None and lit is None and conv is None:
                    path_seg_idx = i
                    break

            if path_seg_idx is not None:
                # Greedy :path matching
                result = self._match_with_path_param(
                    pattern, segments, path_seg_idx
                )
                if result is not None:
                    return handler, result, route_deps, resp_model
                continue

            # Normal segment-count matching
            if len(pattern) != len(segments):
                continue

            params: dict[str, Any] = {}
            matched = True
            for (lit, name, conv), req_seg in zip(pattern, segments):
                if lit is not None:
                    # Literal segment
                    if lit != req_seg:
                        matched = False
                        break
                else:
                    # Parameterized segment -- attempt type coercion
                    try:
                        params[name] = conv(req_seg)
                    except (ValueError, TypeError):
                        matched = False
                        break
            if matched:
                return handler, params, route_deps, resp_model
        return None

    @staticmethod
    def _match_with_path_param(
        pattern: list[ParsedSegment],
        segments: list[str],
        path_idx: int,
    ) -> dict[str, Any] | None:
        """Match a route pattern containing a :path greedy parameter.

        Literal/typed segments before the :path param must match exactly.
        Literal/typed segments after the :path param are matched from the
        end of the path.  Everything in between is consumed by the :path
        parameter (joined with "/").
        """
        prefix = pattern[:path_idx]
        suffix = pattern[path_idx + 1:]
        _, path_name, _ = pattern[path_idx]

        # Need enough segments for prefix + suffix + at least 1 for :path
        if len(segments) < len(prefix) + len(suffix) + 1:
            return None

        params: dict[str, Any] = {}

        # Match prefix (literal and typed segments before :path)
        for (lit, name, conv), req_seg in zip(prefix, segments):
            if lit is not None:
                if lit != req_seg:
                    return None
            else:
                try:
                    params[name] = conv(req_seg)
                except (ValueError, TypeError):
                    return None

        # Match suffix from the end
        suffix_start = len(segments) - len(suffix)
        for i, (lit, name, conv) in enumerate(suffix):
            req_seg = segments[suffix_start + i]
            if lit is not None:
                if lit != req_seg:
                    return None
            else:
                try:
                    params[name] = conv(req_seg)
                except (ValueError, TypeError):
                    return None

        # Everything between prefix and suffix is the :path value
        path_segments = segments[len(prefix):suffix_start]
        params[path_name] = "/".join(path_segments)

        return params

    def match_ws(self, path: str) -> tuple[Callable, dict[str, Any]] | None:
        """Return (handler, path_params) for a WebSocket path, or None."""
        result = self._match_ws_with_deps(path)
        if result is None:
            return None
        handler, params, _deps = result
        return handler, params

    def _match_ws_with_deps(
        self, path: str,
    ) -> tuple[Callable, dict[str, Any], dict[str, Callable]] | None:
        """Return (handler, path_params, deps) for a WebSocket path, or None.

        Internal variant of :meth:`match_ws` that also returns deps.
        """
        segments = path.strip("/").split("/")
        for pattern, handler, route_deps in self._ws_routes:
            # Check for :path segments
            path_seg_idx = None
            for i, (lit, name, conv) in enumerate(pattern):
                if name is not None and lit is None and conv is None:
                    path_seg_idx = i
                    break

            if path_seg_idx is not None:
                result = self._match_with_path_param(pattern, segments, path_seg_idx)
                if result is not None:
                    return handler, result, route_deps
                continue

            if len(pattern) != len(segments):
                continue

            params: dict[str, Any] = {}
            matched = True
            for (lit, name, conv), req_seg in zip(pattern, segments):
                if lit is not None:
                    if lit != req_seg:
                        matched = False
                        break
                else:
                    try:
                        params[name] = conv(req_seg)
                    except (ValueError, TypeError):
                        matched = False
                        break
            if matched:
                return handler, params, route_deps
        return None
