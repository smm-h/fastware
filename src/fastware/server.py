"""Granian ASGI server lifecycle management with PID file tracking, port availability checks, foreground and background serve modes, and graceful stop."""

from __future__ import annotations

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
from typing import Callable

from granian import Granian

__all__ = [
    "check_already_running",
    "ensure_port_available",
    "read_port_file",
    "serve_background",
    "serve",
    "stop",
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


# Counter for generating unique synthetic module names when serving callables.
_callable_counter = 0
_callable_counter_lock = threading.Lock()

# Original granian set_main_signals, saved when the thread-aware wrapper is
# installed (None means the wrapper is not installed).
_granian_set_main_signals: Callable | None = None


def _install_thread_aware_granian_signals() -> None:
    """Make granian's set_main_signals delegate only on the main thread.

    signal.signal() raises ValueError outside the main thread, so granian's
    startup() would crash when serve(foreground=False) runs it in a daemon
    thread. Replacing it with a plain no-op permanently would silently strip
    signal handling from any later foreground serve() in the same process.
    Instead, the wrapper checks the calling thread on every call: on the main
    thread it delegates to the real implementation (foreground serves keep
    full signal handling), elsewhere it skips the call (daemon-thread serves,
    which need no handlers -- the thread dies with the main thread).
    Idempotent.
    """
    global _granian_set_main_signals
    if _granian_set_main_signals is not None:
        return

    from granian import _signals
    from granian.server import common as _granian_common

    original = _signals.set_main_signals
    _granian_set_main_signals = original

    def _thread_aware_set_main_signals(*args, **kwargs):
        if threading.current_thread() is threading.main_thread():
            original(*args, **kwargs)

    _signals.set_main_signals = _thread_aware_set_main_signals
    _granian_common.set_main_signals = _thread_aware_set_main_signals


def _write_pid(pid_path: Path) -> None:
    """Write the current PID to disk, become process group leader, and register cleanup.

    Becoming a process group leader (via os.setpgid(0, 0)) lets stop() signal
    our entire process group with os.killpg, so granian worker subprocesses
    die with us instead of being orphaned.

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
    pid_path.write_text(str(os.getpid()))
    atexit.register(_remove_pid, pid_path)


def _remove_pid(pid_path: Path) -> None:
    """Remove the PID file if it exists."""
    try:
        pid_path.unlink(missing_ok=True)
    except OSError:
        pass


def check_already_running(pid_path: Path, name: str = "server") -> int | None:
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


def _make_server(target: str, host: str, port: int) -> Granian:
    """Create a Granian instance bound to *host* on *port*.

    *target* is an ASGI module path, e.g. ``"myapp:app"``.
    """
    return Granian(
        target=target,
        address=host,
        port=port,
        interface="asgi",
    )


def _run_server(target: str, host: str, port: int, extra_sys_path: str | None = None) -> None:
    """Create and run a Granian server. Used as the reload subprocess target.

    *extra_sys_path* lets the reload child import the temp shim module
    created for callable targets (see _materialize_target).
    """
    if extra_sys_path is not None and extra_sys_path not in sys.path:
        sys.path.insert(0, extra_sys_path)
    server = _make_server(target, host, port)
    server.serve()


def _serve_subprocess(target: str, host: str, port: int, pid_path_str: str) -> None:
    """Entry point for the server subprocess. Runs granian in foreground mode."""
    pid_path = Path(pid_path_str)
    _write_pid(pid_path)
    _write_port_file(pid_path, port)
    server = _make_server(target, host, port)
    server.serve()


def serve_background(
    target: str | Callable,
    *,
    host: str,
    port: int,
    pid_path: Path,
    name: str = "FASTWARE",
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
                f"{str(pid_path)!r})",
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
            try:
                urllib.request.urlopen(f"{url}/health", timeout=1)
                break
            except urllib.error.HTTPError:
                break  # the server answered -- it is up
            except Exception:
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
        existing_pid = check_already_running(pid_path, name)
        if existing_pid is not None:
            msg = (
                f"{name} is already running (PID {existing_pid}). "
                f"Stop it before starting another."
            )
            log.error("%s", msg)
            raise AlreadyRunningError(msg)
    ensure_port_available(resolved_host, resolved_port, name, pid_path=pid_path)
    if pid_path is not None:
        _write_pid(pid_path)
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

    server = _make_server(target_str, resolved_host, resolved_port)

    if foreground:
        log.info("Starting %s on %s", name, url)
        server.serve()
        return None
    else:
        # Granian registers signal handlers in startup(), which fails in
        # daemon threads. The thread-aware wrapper skips signal setup off the
        # main thread while keeping it intact for later foreground serves.
        _install_thread_aware_granian_signals()

        thread = threading.Thread(target=server.serve, daemon=True)
        thread.start()

        log.info("%s started in background on %s", name, url)
        return url


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
        try:
            req = urllib.request.Request(health_url, method="GET")
            with urllib.request.urlopen(req, timeout=2) as resp:
                healthy = 200 <= resp.status < 400
        except Exception:
            healthy = False

    return ServerStatus(running=True, pid=pid, healthy=healthy)
