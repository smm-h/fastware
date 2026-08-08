"""Shared test fixtures for fastware tests."""

from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
from collections.abc import Callable, Iterator

import pytest
from httpx import ASGITransport, AsyncClient

from fastware import JSONResponse, Router, create_app

# ---------------------------------------------------------------------------
# Unreaped children (zombies) for the server liveness-probe tests
# ---------------------------------------------------------------------------
# `os.kill(pid, 0)` -- the liveness probe every server.py entry point uses --
# succeeds for a zombie, so the probes must reap before they believe it. Pinning
# that needs a *real* zombie: a child of this very process that has exited and
# that nobody has waited on. These fixtures build one deterministically, with no
# dependence on granian, signal delivery, or wall-clock timing.


# Children are spawned with subprocess, not os.fork(): the pytest process is
# multi-threaded (background event loops, threaded HTTPServers), and forking a
# multi-threaded process is both deprecated and a real deadlock hazard.


class UnreapedChildTimeout(RuntimeError):
    """A spawned child neither signalled readiness nor exited in time."""


# Prints one byte on stdout as its readiness signal, then ignores SIGTERM for
# long enough that only an escalation to SIGKILL can end it.
_SIGTERM_DEAF_SOURCE = (
    "import signal, sys, time; "
    "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "sys.stdout.buffer.write(b'r'); sys.stdout.buffer.flush(); "
    "time.sleep(30)"
)


@pytest.fixture
def unreaped_child() -> Iterator[Callable[..., int]]:
    """Factory yielding PIDs of child processes this process never waits on.

    Call it with the Python source for the child; it returns the child's PID
    once the child has written its readiness byte to stdout or exited. The
    ``Popen`` handles are held (never polled) so nothing reaps the children
    behind the code under test's back. Teardown SIGKILLs and reaps every child,
    so one left running -- or one the code under test failed to reap -- never
    leaks into the rest of the session.
    """
    started: list[subprocess.Popen] = []

    def _spawn(source: str, *, timeout: float = 30.0) -> int:
        proc = subprocess.Popen(
            [sys.executable, "-c", source], stdout=subprocess.PIPE
        )
        started.append(proc)
        assert proc.stdout is not None
        readable, _, _ = select.select([proc.stdout], [], [], timeout)
        if not readable:
            raise UnreapedChildTimeout(
                f"child {proc.pid} neither signalled readiness nor exited "
                f"within {timeout}s"
            )
        return proc.pid

    yield _spawn

    for proc in started:
        try:
            os.kill(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        if proc.stdout is not None:
            proc.stdout.close()
        # Returns normally even when the code under test already reaped the
        # child: CPython's _try_wait swallows the ChildProcessError. Calling it
        # is what clears the handle's "still running" state.
        proc.wait(timeout=10)


@pytest.fixture
def zombie_pid(unreaped_child: Callable[..., int]) -> int:
    """PID of an exited-but-unreaped child of this process.

    ``os.kill(pid, 0)`` succeeds for it and keeps succeeding until someone
    waits on it, which makes it a deterministic stand-in for the
    "``serve_background`` child already died" case.
    """
    pid = unreaped_child("pass")
    # Stdout EOF only proves the child closed its file descriptors, which
    # happens slightly before it becomes waitable -- a `waitpid(WNOHANG)` in
    # that gap reports "still running" and the test would race. WNOWAIT blocks
    # until the child is genuinely waitable and, unlike waitpid, leaves the
    # zombie in place for the code under test to reap.
    os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT)
    return pid


@pytest.fixture
def sigterm_deaf_pid(unreaped_child: Callable[..., int]) -> int:
    """PID of a live child of this process that ignores SIGTERM.

    Forces ``stop()`` down its SIGKILL escalation path: the graceful signal has
    no effect, so the grace window expires and only SIGKILL ends the child.
    """
    return unreaped_child(_SIGTERM_DEAF_SOURCE)


@pytest.fixture
def client_for():
    """Factory fixture: pass a fastware ASGI app, get an httpx AsyncClient."""

    def _make_client(app, base_url: str = "http://test") -> AsyncClient:
        return AsyncClient(
            transport=ASGITransport(app=app),
            base_url=base_url,
        )

    return _make_client


@pytest.fixture
def sample_app():
    """Minimal fastware app with GET /health."""
    router = Router()

    @router.get("/health")
    async def health(req):
        return JSONResponse({"status": "ok"})

    return create_app(router)


@pytest.fixture
async def sample_client(sample_app, client_for):
    """AsyncClient pointed at sample_app."""
    async with client_for(sample_app) as client:
        yield client
