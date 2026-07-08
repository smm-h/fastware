"""Dependency injection container providing per-request resolution with automatic caching, generator cleanup, and scope-aware dependency override support.

Supports sync/async factory callables and sync/async generator factories
(yield pattern). Generator factories get cleanup after the handler returns.
Results are cached per-request: the same factory called twice returns the
same resolved instance.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Callable

__all__ = [
    "DependencyResolver",
]

logger = logging.getLogger("fastware.di")


def _request_call_mode(factory: Callable) -> str | None:
    """Determine how *factory* should receive the request, by introspection.

    Returns:
    - ``"positional"`` if the factory has a parameter that can accept the
      request positionally (positional-only, positional-or-keyword, or
      ``*args``). The request is passed as ``factory(request)``.
    - ``"keyword"`` if the factory has a keyword-only parameter literally
      named ``request``. The request is passed as ``factory(request=request)``.
    - ``None`` if the factory takes no request slot (e.g. ``def f()`` or
      ``def f(*, x=1)``). The factory is called with no arguments.

    This prevents wrongly calling ``factory(request)`` on a factory whose only
    parameters are keyword-only (which raises ``TypeError``).
    """
    params = inspect.signature(factory).parameters
    for param in params.values():
        if param.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.VAR_POSITIONAL,
        ):
            return "positional"
    if "request" in params and (
        params["request"].kind == inspect.Parameter.KEYWORD_ONLY
    ):
        return "keyword"
    return None


class DependencyResolver:
    """Resolves a dict of ``{name: factory}`` into ``{name: value}`` per request.

    *overrides* maps an original factory to a replacement factory. When an
    override exists for a factory, the replacement is called instead.
    """

    def __init__(self, overrides: dict[Callable, Callable] | None = None):
        self._overrides = overrides if overrides is not None else {}

    async def resolve(
        self,
        deps: dict[str, Callable],
        request: Any,
    ) -> tuple[dict[str, Any], list[tuple[str, Any]]]:
        """Resolve *deps* against *request*.

        Returns ``(resolved, cleanups)`` where *resolved* is a
        ``{name: value}`` dict and *cleanups* is a list of
        ``("sync" | "async", generator)`` pairs to pass to :meth:`cleanup`.

        If a factory raises during resolution, any generator cleanups
        accumulated so far are run before the exception propagates.
        """
        resolved: dict[str, Any] = {}
        cleanups: list[tuple[str, Any]] = []
        cache: dict[int, Any] = {}  # id(actual_factory) -> resolved value

        try:
            for name, factory in deps.items():
                actual_factory = self._overrides.get(factory, factory)
                factory_id = id(actual_factory)

                if factory_id in cache:
                    resolved[name] = cache[factory_id]
                    continue

                # Call the factory with request only if its signature actually
                # has a slot for it, otherwise call with no args. This supports
                # test override factories like
                # ``async def fake(): return {"sub": "test"}`` that don't need
                # request context, as well as factories whose only parameters
                # are keyword-only with defaults (``def f(*, x=1)``) -- those
                # must not be called as ``f(request)``.
                mode = _request_call_mode(actual_factory)
                if mode == "positional":
                    result = actual_factory(request)
                elif mode == "keyword":
                    result = actual_factory(request=request)
                else:
                    result = actual_factory()

                # Handle awaitable (async def factory)
                if inspect.isawaitable(result):
                    result = await result

                # Handle generators (sync and async)
                if inspect.isgenerator(result):
                    value = next(result)
                    cleanups.append(("sync", result))
                elif inspect.isasyncgen(result):
                    value = await result.__anext__()
                    cleanups.append(("async", result))
                else:
                    value = result

                cache[factory_id] = value
                resolved[name] = value
        except Exception:
            # Clean up any generators that already yielded
            await self.cleanup(cleanups)
            raise

        return resolved, cleanups

    @staticmethod
    async def cleanup(cleanups: list[tuple[str, Any]]) -> None:
        """Run generator cleanups in reverse order.

        Each generator dependency must yield exactly once. If a generator
        yields a second time during cleanup (a multi-yield generator, like a
        FastAPI-style dependency written with two ``yield`` statements), that is
        a programming error: :class:`RuntimeError` is raised after all cleanups
        have run.

        Errors raised *inside* a generator's cleanup code (e.g. in a ``finally``
        block) are logged at ERROR level and suppressed -- a failing cleanup
        must not mask the handler's response or exception. They are never
        swallowed silently.
        """
        multi_yield_error: RuntimeError | None = None
        for kind, gen in reversed(cleanups):
            try:
                if kind == "sync":
                    try:
                        next(gen)
                    except StopIteration:
                        continue
                else:
                    try:
                        await gen.__anext__()
                    except StopAsyncIteration:
                        continue
            except Exception:
                logger.exception("Error during dependency generator cleanup")
                continue

            # Reaching here means the generator yielded a second time: it is a
            # multi-yield generator, which is not allowed. Close it and remember
            # to raise once every cleanup has been attempted.
            if multi_yield_error is None:
                multi_yield_error = RuntimeError(
                    "Dependency generator yielded more than once; generator "
                    "dependencies must yield exactly once."
                )
            try:
                if kind == "sync":
                    gen.close()
                else:
                    await gen.aclose()
            except Exception:
                logger.exception(
                    "Error closing multi-yield dependency generator"
                )

        if multi_yield_error is not None:
            raise multi_yield_error
