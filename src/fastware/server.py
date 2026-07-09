"""Granian ASGI server lifecycle management with PID file tracking, port availability checks, foreground and background serve modes, and graceful stop."""

from __future__ import annotations

import asyncio
import atexit
import logging
import os
import signal
import socket
import sys
import threading
import time
import types
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from granian import Granian

# Explicit event-loop choices exposed to callers. Deliberately excludes
# Granian's "auto", which silently resolves rloop -> uvloop -> asyncio based
# on which packages happen to be installed. fastware pins the default to
# "asyncio" so behaviour never changes just because a transitive dependency
# pulls in rloop/uvloop (a "no silent degradation" hazard, and rloop in
# particular breaks asyncio-subprocess workloads like Playwright).
LoopChoice = Literal["asyncio", "uvloop", "rloop"]

__all__ = [
    "check_already_running",
    "ensure_port_available",
    "read_port_file",
    "serve_background",
    "serve",
    "stop",
    "stop_background",
    "status",
    "ServerStatus",
    "PortInUseError",
    "AlreadyRunningError",
]

log = logging.getLogger(__name__)


class PortInUseError(RuntimeError):
    """Raised when the requested port is already in use."""


class AlreadyRunningError(RuntimeError):
    """Raised when a single-instance server is already running."""


def _probe_health(url: str, timeout: float) -> int | None:
    """Probe *url* over HTTP within *timeout* seconds.

    Returns the HTTP status code if the server answered at the HTTP level --
    including error statuses like 404, which still prove the server is up and
    listening. Returns ``None`` if the connection failed outright (refused,
    reset, timed out, DNS failure): the server is not reachable.

    Callers pick their own health criterion from the result: "answered at all"
    (``code is not None``) proves the process is up; "answered with a success
    status" (``200 <= code < 400``) proves the endpoint is healthy.
    """
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:
        return None


# Counter for generating unique synthetic module names when serving callables.
_callable_counter = 0
_callable_counter_lock = threading.Lock()

# Background embed server instances keyed by URL, for stop_background().
_background_servers: dict[str, object] = {}


def _write_pid(pid_path: Path, *, exclusive: bool = False) -> None:
    """Write the current PID to disk, become process group leader, and register cleanup.

    Becoming a process group leader (via os.setpgid(0, 0)) lets stop() signal
    our entire process group with os.killpg, so granian worker subprocesses
    die with us instead of being orphaned.

    When *exclusive* is True, the PID file is created atomically with
    O_CREAT|O_EXCL: if it already exists, another instance won the race
    between the single-instance check and PID creation, and
    AlreadyRunningError is raised instead of overwriting its PID file.

    No signal handlers are installed here: Granian installs its own
    SIGTERM/SIGINT handlers during startup (which trigger a graceful shutdown,
    after which the atexit hook removes the PID file), and group-wide
    termination is stop()'s responsibility.
    """
    try:
        os.setpgid(0, 0)  # 0,0 = current process becomes its own group leader
    except OSError:
        # Already a group leader, or not permitted in this context (e.g. some
        # supervised contexts) -- not fatal.
        pass
    if exclusive:
        try:
            fd = os.open(pid_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            raise AlreadyRunningError(
                f"PID file {pid_path} already exists -- another instance "
                f"started concurrently."
            ) from None
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))
    else:
        pid_path.write_text(str(os.getpid()))
    atexit.register(_remove_pid, pid_path)


def _remove_pid(pid_path: Path) -> None:
    """Remove the PID file if it exists."""
    try:
        pid_path.unlink(missing_ok=True)
    except OSError:
        pass


def check_already_running(pid_path: Path) -> int | None:
    """Check if another instance is running. Returns the PID if running, None otherwise.

    Stale PID files (process dead) are cleaned up automatically.
    """
    if not pid_path.exists():
        return None
    try:
        pid = int(pid_path.read_text().strip())
    except (ValueError, OSError):
        # Corrupt or unreadable PID file -- treat as stale.
        _remove_pid(pid_path)
        return None
    try:
        os.kill(pid, 0)  # probe whether the process is alive
    except ProcessLookupError:
        # Process is dead; stale PID file.
        _remove_pid(pid_path)
        return None
    except PermissionError:
        pass  # Process exists but we can't signal it
    return pid  # Process is alive


def ensure_port_available(
    host: str,
    port: int,
    name: str = "server",
    pid_path: Path | None = None,
) -> int:
    """Ensure port is available. Returns *port* on success.

    If the port is occupied and *pid_path* names a previous instance of this
    server that verifiably holds the port (the PID in the PID file matches a
    PID reported as holding the port), that stale instance is stopped and the
    port reclaimed. Any other occupant raises PortInUseError -- ownership is
    never guessed from response contents.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        # SO_REUSEADDR matches Granian's behavior so our probe doesn't spuriously
        # fail during the kernel's brief TIME_WAIT after a recent stop. Without
        # this the bind raises EADDRINUSE while no process is actually listening.
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return port
        except OSError:
            pass  # Port in use -- investigate

    # Port is occupied. Only kill the holder if our PID file proves it is a
    # previous instance of this server.
    stale_pid: int | None = None
    if pid_path is not None:
        try:
            stale_pid = int(pid_path.read_text().strip())
        except (ValueError, OSError):
            stale_pid = None

    if stale_pid is not None and stale_pid in _find_port_holder_pids(port):
        log.warning(
            "Port %d is held by a stale %s instance (PID %d). Stopping it.",
            port, name, stale_pid,
        )
        try:
            os.kill(stale_pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        # Verify port is now free
        for _ in range(10):
            time.sleep(0.5)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s2:
                s2.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    s2.bind((host, port))
                    return port
                except OSError:
                    continue

    msg = (
        f"Port {port} on {host} is already in use. Use a different port "
        f"or stop the process holding it."
    )
    log.error("%s", msg)
    raise PortInUseError(msg)


def _find_port_holder_pids(port: int) -> list[int]:
    """Return the PIDs of processes holding a TCP port, via lsof or fuser.

    Returns an empty list when no holder can be identified (including when
    neither tool is available) -- callers must treat that as "ownership not
    proven" and refuse to kill anything.
    """
    import subprocess

    try:
        result = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return [int(p) for p in result.stdout.split() if p.strip().isdigit()]
        return []
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    # lsof not available -- try fuser
    try:
        result = subprocess.run(
            ["fuser", f"{port}/tcp"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return [int(p) for p in result.stdout.split() if p.strip().isdigit()]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return []


def _resolve_target(target: str | Callable) -> str:
    """Convert a target to a Granian-compatible string.

    If target is already a string (e.g. "myapp:app"), return as-is.
    If target is a callable, register it on a synthetic module so Granian
    can import it via its string-based loader.
    """
    if isinstance(target, str):
        return target
    global _callable_counter
    with _callable_counter_lock:
        _callable_counter += 1
        mod_name = f"_fastware_target_{_callable_counter}"
    mod = types.ModuleType(mod_name)
    mod.app = target  # type: ignore[attr-defined]
    sys.modules[mod_name] = mod
    return f"{mod_name}:app"


def _materialize_target(target: str | Callable) -> tuple[str, Path | None]:
    """Resolve a target for use in a *separate* process.

    Strings pass through unchanged (with no shim directory). Callables are
    materialized as a real shim module file in a fresh temp directory: the
    child process adds that directory to sys.path and imports the shim, which
    re-imports the callable from its defining module. Returns
    ``(target_str, shim_dir)``; the caller must clean up *shim_dir* once the
    child has imported it.

    Only module-level callables of importable modules can be materialized.
    Anything else (locals, lambdas, objects defined in __main__) cannot be
    re-imported by another process -- raise a clear error immediately instead
    of letting the child crash.
    """
    if isinstance(target, str):
        return target, None

    mod_name = getattr(target, "__module__", None)
    qualname = getattr(target, "__qualname__", None)

    def _reject(reason: str) -> ValueError:
        return ValueError(
            f"Callable target {target!r} cannot be used with serve_background/"
            f"reload: {reason}. These modes spawn a separate process that must "
            f"re-import the target, so it must be an importable module-level "
            f"object. Pass a module path string like 'mypkg.app:app' instead."
        )

    if not mod_name or not qualname:
        raise _reject("it has no __module__/__qualname__")
    if mod_name == "__main__":
        raise _reject("it is defined in __main__, which a child process cannot re-import")
    if "<locals>" in qualname or "<lambda>" in qualname:
        raise _reject("it is a local function, closure, or lambda and is not importable")

    # Verify the reference actually resolves back to this exact object.
    parent_mod = sys.modules.get(mod_name)
    if parent_mod is not None:
        obj: object = parent_mod
        try:
            for part in qualname.split("."):
                obj = getattr(obj, part)
        except AttributeError:
            raise _reject(f"'{qualname}' is not reachable in module '{mod_name}'")
        if obj is not target:
            raise _reject(
                f"'{mod_name}.{qualname}' resolves to a different object "
                f"(the target was rebound or decorated)"
            )

    import tempfile

    global _callable_counter
    with _callable_counter_lock:
        _callable_counter += 1
        shim_name = f"_fastware_target_{os.getpid()}_{_callable_counter}"
    shim_dir = Path(tempfile.mkdtemp(prefix="fastware-target-"))
    shim_file = shim_dir / f"{shim_name}.py"
    shim_file.write_text(
        "# Auto-generated by fastware.server._materialize_target -- re-imports\n"
        "# a callable ASGI target so a spawned server process can load it.\n"
        "import importlib\n"
        f"_obj = importlib.import_module({mod_name!r})\n"
        f"for _part in {qualname!r}.split('.'):\n"
        "    _obj = getattr(_obj, _part)\n"
        "app = _obj\n"
    )
    return f"{shim_name}:app", shim_dir


def _remove_shim_dir(shim_dir: Path | None) -> None:
    """Remove a temp shim directory created by _materialize_target."""
    if shim_dir is None:
        return
    import shutil

    shutil.rmtree(shim_dir, ignore_errors=True)


def _find_free_port(host: str = "127.0.0.1") -> int:
    """Find an ephemeral port that is currently free on *host*."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def _port_file_path(pid_path: Path) -> Path:
    """Derive the port file path from a PID file path.

    E.g. ``.pixelweaver.pid`` -> ``.pixelweaver.port``.
    """
    return pid_path.with_suffix(".port")


def _write_port_file(pid_path: Path, port: int) -> None:
    """Write the bound port alongside the PID file."""
    port_path = _port_file_path(pid_path)
    port_path.write_text(str(port))
    atexit.register(_remove_port_file, port_path)


def _remove_port_file(port_path: Path) -> None:
    """Remove a port file if it exists."""
    try:
        port_path.unlink(missing_ok=True)
    except OSError:
        pass


def read_port_file(pid_path: Path) -> int | None:
    """Read the port stored alongside a PID file. Returns None if missing or unreadable."""
    port_path = _port_file_path(pid_path)
    if not port_path.exists():
        return None
    try:
        return int(port_path.read_text().strip())
    except (ValueError, OSError):
        return None


def _resolve_host_port(
    host: str | None,
    port: int | None,
    name: str,
) -> tuple[str, int]:
    """Resolve host and port from explicit args or env vars.

    Explicit args override env vars. If neither is provided, raise ValueError.
    """
    env_prefix = name.upper()

    if host is None:
        env_host = os.environ.get(f"{env_prefix}_HOST")
        if env_host is not None:
            host = env_host
        else:
            raise ValueError(
                f"host must be provided explicitly or via {env_prefix}_HOST env var"
            )

    if port is None:
        env_port = os.environ.get(f"{env_prefix}_PORT")
        if env_port is not None:
            try:
                port = int(env_port)
            except ValueError:
                raise ValueError(
                    f"{env_prefix}_PORT env var must be an integer, got {env_port!r}"
                )
        else:
            raise ValueError(
                f"port must be provided explicitly or via {env_prefix}_PORT env var"
            )

    return host, port


def _make_server(
    target: str,
    host: str,
    port: int,
    *,
    loop: LoopChoice = "asyncio",
    workers: int = 1,
) -> Granian:
    """Create a Granian instance bound to *host* on *port*.

    *target* is an ASGI module path, e.g. ``"myapp:app"``.

    *loop* pins the event-loop implementation (see :func:`serve`).
    *workers* is the number of worker processes (see :func:`serve`).
    """
    from granian.constants import Loops

    return Granian(
        target=target,
        address=host,
        port=port,
        interface="asgi",
        loop=Loops(loop),
        workers=workers,
    )


def _make_embed_server(
    target: Callable,
    host: str,
    port: int,
) -> object:
    """Create a Granian embed server for in-process background serving.

    *target* is the ASGI callable (not a string).
    Returns the embed server instance (with ``serve()`` and ``stop()`` methods).
    """
    from granian.server.common import Interfaces
    from granian.server.embed import Server as EmbedServer

    return EmbedServer(
        target=target,
        address=host,
        port=port,
        interface=Interfaces.ASGI,
    )


def _run_server(
    target: str,
    host: str,
    port: int,
    extra_sys_path: str | None = None,
    loop: LoopChoice = "asyncio",
    workers: int = 1,
) -> None:
    """Create and run a Granian server. Used as the reload subprocess target.

    *extra_sys_path* lets the reload child import the temp shim module
    created for callable targets (see _materialize_target).
    """
    if extra_sys_path is not None and extra_sys_path not in sys.path:
        sys.path.insert(0, extra_sys_path)
    server = _make_server(target, host, port, loop=loop, workers=workers)
    server.serve()


def _serve_subprocess(
    target: str,
    host: str,
    port: int,
    pid_path_str: str,
    loop: LoopChoice = "asyncio",
    workers: int = 1,
) -> None:
    """Entry point for the server subprocess. Runs granian in foreground mode."""
    pid_path = Path(pid_path_str)
    _write_pid(pid_path)
    _write_port_file(pid_path, port)
    server = _make_server(target, host, port, loop=loop, workers=workers)
    server.serve()


def serve_background(
    target: str | Callable,
    *,
    host: str,
    port: int,
    pid_path: Path,
    name: str = "FASTWARE",
    loop: LoopChoice = "asyncio",
    workers: int = 1,
) -> str:
    """Start the server as an independent background process. Returns the URL.

    Unlike ``serve(foreground=False)`` which uses a daemon thread (dies with the
    parent), this spawns a fully detached subprocess that survives the parent
    exiting. The subprocess writes its own PID and port files.

    Parameters
    ----------
    target:
        ASGI application -- either a module path string (e.g. "myapp:app")
        or a callable ASGI application object.
    host:
        Bind address.
    port:
        Bind port. Pass 0 to pick a random free port.
    pid_path:
        Path for the PID file. The subprocess writes this, not the caller.
    name:
        Application name for log messages.
    loop:
        Event-loop implementation. See :func:`serve` for the rationale behind
        the pinned ``"asyncio"`` default.
    workers:
        Number of worker processes. See :func:`serve` for the duplication
        hazard when ``workers > 1`` with lifespan-started background tasks.

    Returns
    -------
    str
        The URL the server is listening on (e.g. "http://127.0.0.1:8000").

    Raises
    ------
    RuntimeError
        If the server process exits prematurely or fails to start within 10s.
    """
    import subprocess as _subprocess

    target_str, shim_dir = _materialize_target(target)

    # Find a free port if port is 0
    if port == 0:
        port = _find_free_port(host)

    try:
        # Spawn server subprocess, fully detached from the parent process group.
        # For callable targets, the child prepends the shim directory to
        # sys.path so it can import the materialized target module.
        prelude = (
            f"import sys; sys.path.insert(0, {str(shim_dir)!r}); "
            if shim_dir is not None
            else ""
        )
        proc = _subprocess.Popen(
            [
                sys.executable, "-c",
                f"{prelude}"
                f"from fastware.server import _serve_subprocess; "
                f"_serve_subprocess({target_str!r}, {host!r}, {port!r}, "
                f"{str(pid_path)!r}, {loop!r}, {workers!r})",
            ],
            stdin=_subprocess.DEVNULL,
            stdout=_subprocess.DEVNULL,
            stderr=_subprocess.DEVNULL,
            start_new_session=True,
        )

        # Wait for the server to be ready by polling it over HTTP. Any HTTP
        # response -- including an error status like 404 (no /health route) --
        # proves the server is up; only connection-level failures mean not-ready.
        url = f"http://{host}:{port}"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if _probe_health(f"{url}/health", 1) is not None:
                break  # the server answered (any status) -- it is up
            if proc.poll() is not None:
                raise RuntimeError(
                    f"Server process exited with code {proc.returncode}"
                )
            time.sleep(0.3)
        else:
            proc.terminate()
            raise RuntimeError("Server did not start within 10 seconds")
    finally:
        # Once the server answers (or startup failed), the child has imported
        # the shim -- the temp module file is no longer needed.
        _remove_shim_dir(shim_dir)

    log.info("%s started as background process (PID %d) on %s", name, proc.pid, url)
    return url


def serve(
    target: str | Callable,
    *,
    foreground: bool,
    host: str | None = None,
    port: int | None = None,
    pid_path: Path | None = None,
    name: str = "FASTWARE",
    pre_serve: Callable[[], None] | None = None,
    reload: bool = False,
    single_instance: bool = True,
    loop: LoopChoice = "asyncio",
    workers: int = 1,
) -> str | None:
    """Start Granian serving the ASGI app.

    Parameters
    ----------
    target:
        ASGI application -- either a module path string (e.g. "myapp:app")
        or a callable ASGI application object.
    foreground:
        Required. When True, blocks the calling thread. When False, spawns a
        daemon thread and returns the URL string.
    host:
        Bind address. Falls back to {NAME}_HOST env var. ValueError if neither.
    port:
        Bind port. Falls back to {NAME}_PORT env var. ValueError if neither.
    pid_path:
        If given, enables PID file management (detect existing instances,
        write PID, register cleanup).
    name:
        Application name for env var prefix (uppercased) and log messages.
        Default "FASTWARE".
    pre_serve:
        Optional callable invoked synchronously after PID/port checks but
        before Granian starts.
    reload:
        When True, watches .py files in the current working directory and
        restarts the server on changes. Requires foreground=True.
    single_instance:
        When True (default), checks for an existing running instance via the
        PID file and exits with an error if one is found. When False, skips
        the PID check (but still writes the PID file if pid_path is given).
    loop:
        Event-loop implementation: ``"asyncio"`` (default), ``"uvloop"``, or
        ``"rloop"``. fastware deliberately does NOT expose Granian's ``"auto"``
        mode, which silently resolves rloop -> uvloop -> asyncio based on which
        packages are installed -- meaning the same code would pick a different
        loop just because a transitive dependency pulled in rloop or uvloop.
        The pinned ``"asyncio"`` default keeps behaviour environment-independent
        and preserves stdlib asyncio-subprocess semantics (rloop, which would
        win "auto" resolution, breaks asyncio.create_subprocess_* workloads such
        as Playwright). Choose ``"uvloop"``/``"rloop"`` explicitly if you want
        their throughput and understand the trade-offs.
    workers:
        Number of Granian worker processes (default 1). WARNING: ``workers > 1``
        forks the ENTIRE app -- including the lifespan handler -- once per
        worker. Any singleton or background task started in the lifespan (a
        scheduler, a connection pool, an asyncio worker loop) is DUPLICATED
        per worker, which is almost never what you want. For apps with
        lifespan-started singletons, ``workers=1`` is the only correct value.

    Returns
    -------
    str | None
        When foreground=False, returns the URL string (e.g. "http://127.0.0.1:8000").
        When foreground=True, returns None (blocks until server stops).
    """
    if reload and not foreground:
        raise ValueError("reload requires foreground=True")

    resolved_host, resolved_port = _resolve_host_port(host, port, name)
    if reload:
        # The reload child is a separate process -- callable targets must be
        # materialized as an importable shim module (or rejected clearly).
        target_str, shim_dir = _materialize_target(target)
    else:
        target_str = _resolve_target(target)
        shim_dir = None

    # Port 0 means "pick a random free port"
    if resolved_port == 0:
        resolved_port = _find_free_port(resolved_host)

    if pid_path is not None and single_instance:
        existing_pid = check_already_running(pid_path)
        if existing_pid is not None:
            msg = (
                f"{name} is already running (PID {existing_pid}). "
                f"Stop it before starting another."
            )
            log.error("%s", msg)
            raise AlreadyRunningError(msg)
    ensure_port_available(resolved_host, resolved_port, name, pid_path=pid_path)
    if pid_path is not None:
        # Exclusive creation closes the race window between the
        # check_already_running probe above and PID file creation.
        _write_pid(pid_path, exclusive=single_instance)
        _write_port_file(pid_path, resolved_port)

    if pre_serve is not None:
        pre_serve()

    url = f"http://{resolved_host}:{resolved_port}"

    if reload:
        from watchfiles import PythonFilter, run_process

        log.info("Starting %s on %s (reload enabled)", name, url)
        try:
            run_process(
                ".",
                target=_run_server,
                args=(
                    target_str,
                    resolved_host,
                    resolved_port,
                    str(shim_dir) if shim_dir is not None else None,
                    loop,
                    workers,
                ),
                watch_filter=PythonFilter(),
                callback=lambda changes: log.info(
                    "Detected changes, restarting: %s",
                    {path for _, path in changes},
                ),
            )
        finally:
            # Reload restarts re-import the shim on every file change, so it
            # can only be removed once the reload loop has exited.
            _remove_shim_dir(shim_dir)
        return None

    if foreground:
        server = _make_server(
            target_str, resolved_host, resolved_port, loop=loop, workers=workers
        )
        log.info("Starting %s on %s", name, url)
        server.serve()
        return None
    else:
        # Use Granian's embed server for background mode. The embed server
        # runs async workers in the caller's event loop (no fork, no
        # subprocess, no signal.signal calls), so it works cleanly in a
        # daemon thread.

        # The embed server takes the ASGI callable directly (not a string).
        if callable(target):
            asgi_callable = target
        else:
            from granian._internal import load_target
            asgi_callable = load_target(target_str)

        embed = _make_embed_server(asgi_callable, resolved_host, resolved_port)

        def _run_embed():
            asyncio.run(embed.serve())

        _background_servers[url] = embed
        thread = threading.Thread(target=_run_embed, daemon=True)
        thread.start()

        log.info("%s started in background on %s", name, url)
        return url


def stop_background(url: str) -> None:
    """Stop an in-process background server started by ``serve(foreground=False)``.

    Parameters
    ----------
    url:
        The URL returned by ``serve(foreground=False)``.

    Raises
    ------
    KeyError
        If no background server is tracked for *url*.
    """
    try:
        embed = _background_servers.pop(url)
    except KeyError:
        raise KeyError(f"No background server tracked for {url!r}") from None
    embed.stop()
    log.info("Background server on %s stopped", url)


@dataclass
class ServerStatus:
    """Result of a status check on a server process."""

    running: bool
    pid: int | None
    healthy: bool | None


def _cleanup_pid_and_port(pid_path: Path) -> None:
    """Remove both the PID file and its companion port file."""
    _remove_pid(pid_path)
    _remove_port_file(_port_file_path(pid_path))


def stop(pid_path: Path) -> None:
    """Stop a server by reading its PID file.

    Sends SIGTERM to the server's process group when the server is its own
    group leader (as arranged by _write_pid), so granian worker subprocesses
    die with it. If the server does not lead its group (setpgid failed at
    startup), only the single PID is signalled -- a group the server does not
    lead may contain unrelated processes such as the launching shell.

    Waits up to 10s polling with os.kill(pid, 0), then escalates to SIGKILL.
    Cleans up the PID file and port file.

    Raises FileNotFoundError if PID file does not exist.
    If the process is already gone (stale PID file), removes the PID file
    and returns normally.
    """
    if not pid_path.exists():
        raise FileNotFoundError(f"PID file not found: {pid_path}")

    try:
        pid = int(pid_path.read_text().strip())
    except (ValueError, OSError) as exc:
        _cleanup_pid_and_port(pid_path)
        raise FileNotFoundError(f"Corrupt or unreadable PID file: {pid_path}") from exc

    # Check if process is alive first
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        _cleanup_pid_and_port(pid_path)
        log.info("Process %d is not running (stale PID file removed)", pid)
        return
    except PermissionError:
        pass  # process exists but we can't probe -- proceed with SIGTERM

    # Signal the whole group only when the server leads it (pid == pgid).
    try:
        use_group = os.getpgid(pid) == pid
    except OSError:
        use_group = False

    def _signal_server(sig: int) -> None:
        if use_group:
            os.killpg(pid, sig)
        else:
            os.kill(pid, sig)

    # Send SIGTERM
    try:
        _signal_server(signal.SIGTERM)
    except ProcessLookupError:
        _cleanup_pid_and_port(pid_path)
        return
    except PermissionError:
        _cleanup_pid_and_port(pid_path)
        raise

    # Wait up to 10s for process to exit
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            # Process exited cleanly
            _cleanup_pid_and_port(pid_path)
            return
        except PermissionError:
            break  # can't check -- fall through to SIGKILL
        time.sleep(0.1)

    # Escalate to SIGKILL
    try:
        _signal_server(signal.SIGKILL)
    except ProcessLookupError:
        _cleanup_pid_and_port(pid_path)
        return
    except PermissionError:
        _cleanup_pid_and_port(pid_path)
        raise

    _cleanup_pid_and_port(pid_path)


def status(pid_path: Path, health_url: str | None = None) -> ServerStatus:
    """Check the status of a server process.

    Parameters
    ----------
    pid_path:
        Path to the PID file.
    health_url:
        Optional URL to probe for health (e.g. "http://127.0.0.1:8000/health").
        If provided and the process is running, an HTTP GET is attempted with
        a short timeout.

    Returns
    -------
    ServerStatus
        Dataclass with running, pid, and healthy fields.
    """
    if not pid_path.exists():
        return ServerStatus(running=False, pid=None, healthy=None)

    try:
        pid = int(pid_path.read_text().strip())
    except (ValueError, OSError):
        return ServerStatus(running=False, pid=None, healthy=None)

    # Check liveness
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return ServerStatus(running=False, pid=pid, healthy=None)
    except PermissionError:
        # Process exists but can't signal -- still consider running
        pass

    # Process is running -- check health if URL provided
    healthy: bool | None = None
    if health_url is not None:
        code = _probe_health(health_url, 2)
        healthy = code is not None and 200 <= code < 400

    return ServerStatus(running=True, pid=pid, healthy=healthy)
