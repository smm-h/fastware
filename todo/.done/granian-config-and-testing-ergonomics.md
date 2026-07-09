# Expose Granian configuration + fix testing/serialization ergonomics

## Context

A production evaluation of fastware 0.2.0 (editable install, granian 2.7.9, Python 3.13) probed the framework for a long-running service embedding background asyncio workers and asyncio-subprocess workloads (headless browsers via Playwright). The core verdicts were positive: graceful SIGTERM shutdown through the lifespan async-CM works fully, subprocess spawning under Granian's worker works, 300s+ requests are not truncated, and the in-process test client is immune to the known "Granian signal handling in threads" issue. Four concrete gaps surfaced along the way. They are collected here as one document because three of them touch the same theme (production-readiness ergonomics) and would sensibly ship together.

## Issue 1: Granian configuration is hardcoded — no way to pin the event loop (the important one)

### Problem

`_make_server` constructs Granian with only four arguments — `Granian(target, address=..., port=..., interface="asgi")` (`src/fastware/server.py:453-463`). `serve()` (`server.py:581-709`) exposes none of Granian's knobs: no `loop`, no `workers`, no `blocking_threads`, no HTTP settings.

The loop is the dangerous part. Granian defaults to `loop=auto`, which resolves **rloop → uvloop → asyncio** by probing what is installed (`granian/_loops.py:100-108`). Today a stock fastware install resolves to stdlib asyncio because neither rloop nor uvloop is a dependency. But if a consumer (or a transitive dependency) ever installs rloop or uvloop, the loop **silently switches** — and rloop, which wins the resolution order, is the classic risk for asyncio-subprocess workloads (Playwright, asyncio.create_subprocess_*). A consumer relying on subprocess semantics has no way to pin `Loops.asyncio` through fastware. This is a "no silent degradation" violation by proxy: the same code behaves differently depending on which packages happen to be present.

Secondary: `workers>1` (if ever exposed) forks multiple worker processes, each running its own lifespan — background tasks started in the lifespan get duplicated per worker. Any exposure of `workers` must document this loudly; for apps with lifespan-started singletons, `workers=1` is the only correct value.

### Solutions

- **(a) Curated explicit parameters on `serve()`** — e.g. `loop: Literal["asyncio","uvloop","rloop"]` (note: no `"auto"` — that would preserve the silent-switching footgun), plus optionally `workers: int = 1` with the duplication warning in the docstring. Pros: typed, documented, testable, consistent with the "mandatory explicit flags" philosophy; the loop default can be pinned to `"asyncio"` making behavior environment-independent. Cons: each new Granian knob needs a fastware release. Changing the effective default from `auto` to `"asyncio"` is technically breaking for anyone relying on uvloop being auto-picked (unlikely but real; needs a changelog `breaking` entry).
- **(b) `granian_kwargs: dict` passthrough** — forward arbitrary kwargs to the `Granian(...)` constructor. Pros: zero maintenance, everything reachable. Cons: untyped, leaks Granian's API surface as fastware's contract, invites misuse (`workers=8` with a lifespan singleton).
- **(c) Both** — curated params for the load-bearing knobs (loop, workers), passthrough for the long tail. Pros: right ergonomics where it matters. Cons: two mechanisms to document.

Recommendation: (a), with `loop="asyncio"` as the default. Predictability beats the marginal throughput of auto-picked rloop/uvloop, and consumers who want an alternative loop should have to say so.

### Affected files

- `src/fastware/server.py` (`_make_server`, `serve`, `serve_background`)
- `docs/` (serve() reference; a "production notes" section on loop choice and workers semantics)
- Tests: a test asserting the loop actually used matches the parameter (introspect `asyncio.get_running_loop()` type from a handler)

### Effort

Small–medium. The passthrough itself is a few lines; the care is in choosing defaults, the breaking-change changelog entry, and tests.

## Issue 2: Test client swallows handler exceptions — no re-raise option

### Problem

Unhandled handler exceptions are always converted to a 500 response (`src/fastware/app.py:651-682` path), and the test clients (`src/fastware/testing.py`) offer no equivalent of Starlette's `TestClient(raise_server_exceptions=True)` (which re-raises the real exception into the test). In fastware tests, a bug manifests as a bare `assert response.status_code == 500` with the actual traceback only in logs — materially worse debugging during TDD.

### Solution

A `raise_server_exceptions: bool = True` flag on `AsyncTestClient`/`TestClient` (matching Starlette's default), implemented by having the ASGI-transport path re-raise instead of serializing when the flag is set. Pros: massively better test debugging, familiar semantics. Cons: touches the app's exception-handling path (needs a scope/state signal from the client), and the default (True per Starlette convention) changes existing behavior of tests that assert on 500s — those callers set it to False.

### Affected files

`src/fastware/testing.py`, exception-handling branch in `src/fastware/app.py`, test-client docs, changelog (`breaking` if default is True).

### Effort

Small.

## Issue 3: `PytestCollectionWarning` on every test module importing `TestClient`

### Problem

The sync client's real class name is `_SyncTestClient` (`src/fastware/testing.py:148`), exported under the alias `TestClient`. When a test module does `from fastware.testing import TestClient`, pytest matches the `Test*` name in the module namespace and warns: `cannot collect test class '_SyncTestClient' because it has a __init__ constructor`. Harmless but emitted once per importing module — noise that trains people to ignore warnings.

### Solution

Set `__test__ = False` as a class attribute on both `TestClient`/`_SyncTestClient` and `AsyncTestClient`. This is the canonical pytest opt-out; one line per class, no behavior change.

### Affected files

`src/fastware/testing.py`.

### Effort

Trivial.

## Issue 4: `request.json_as` is Pydantic-only in a msgspec-first framework

### Problem

`request.json_as` (`src/fastware/request.py:281`) validates only against Pydantic models, while fastware's core serializer — and headline performance claim — is msgspec. Consumers using `msgspec.Struct` request bodies must hand-roll `msgspec.json.decode(req.body, type=MyStruct)` (works fine, but the convenience API steers users toward the slower path the framework exists to avoid).

### Solution

Type-dispatch in `json_as`: if the target is a `msgspec.Struct` subclass (or any msgspec-decodable type), use `msgspec.json.decode(body, type=T)`; fall back to the Pydantic path otherwise. Decode errors map to the same 400/422 behavior as the Pydantic branch. Pros: one method, right tool chosen automatically, no API growth. Cons: two validation stacks behind one method name — error message shapes differ between branches unless normalized.

### Affected files

`src/fastware/request.py`, tests for both branches, docs.

### Effort

Small.
