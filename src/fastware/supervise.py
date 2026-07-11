"""Process supervision for the ``fastware dev`` CLI.

Provides :class:`DevProcess` (a single supervised child in its own process group,
with graceful-then-kill teardown) and the orchestration that turns a validated
:class:`~fastware.devconfig.DevConfig` into a running dev environment:

1. pre-spawn gates (must pass before anything spawns),
2. ordered, health-gated aux services,
3. the topology-ordered vite + backend pair with readiness probes,
4. a supervise loop that, on any component death, tears the rest down in reverse
   order and exits non-zero naming the dead component.

Daemon mode detaches the whole supervisor and registers it in the Phase 11
instance registry (entry name ``"<app-name>-dev"``), which ``dev status`` and
``dev stop`` read.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from fastware.devconfig import DevConfig, HealthCheck, PreSpawnGate

__all__ = [
    "DevRunError",
    "PreSpawnGateError",
    "ReadinessError",
    "PortCollisionError",
    "SWModeConflictError",
    "ComponentDied",
    "DevProcess",
    "teardown",
    "supervise_loop",
    "run_pre_spawn_gates",
    "check_sw_mode",
    "resolve_backend_sw_mode",
    "run_dev",
    "list_dev_instances",
    "stop_dev_instances",
    "dev_pid_path",
]

logger = logging.getLogger("fastware.dev")

GRACE_DEFAULT = 10.0
_VITE_READY_TIMEOUT = 20.0
_BACKEND_READY_TIMEOUT = 20.0


class DevRunError(Exception):
    """Base class for dev-run failures."""


class PreSpawnGateError(DevRunError):
    """A pre-spawn gate did not pass; nothing was spawned."""


class ReadinessError(DevRunError):
    """A component did not become ready within its timeout."""


class PortCollisionError(DevRunError):
    """A required port is already in use."""


class SWModeConflictError(DevRunError):
    """The resolved in-process app is configured with sw_mode='cache'."""


class ComponentDied(DevRunError):
    """A supervised component exited; the environment was torn down."""

    def __init__(self, component: str, returncode: int | None):
        self.component = component
        self.returncode = returncode
        super().__init__(
            f"component {component!r} exited (code {returncode}); "
            "tearing down the rest and exiting."
        )


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------


def _http_answers(url: str, timeout: float) -> bool:
    """True if *url* answers at the HTTP level (any status), else False."""
    from fastware.server import _probe_health

    return _probe_health(url, timeout) is not None


def _tcp_open(hostport: str, timeout: float) -> bool:
    host, sep, port = hostport.rpartition(":")
    if not sep:
        return False
    try:
        with socket.create_connection((host or "localhost", int(port)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def _port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError:
            return True
    return False


def _check_health(health: HealthCheck, base_dir: Path, env: dict[str, str]) -> bool:
    if health.http is not None:
        return _http_answers(health.http, 2.0)
    if health.tcp is not None:
        return _tcp_open(health.tcp, 2.0)
    # cmd health check
    full_env = {**os.environ, **env}
    try:
        proc = subprocess.run(
            health.cmd,
            cwd=str(base_dir),
            env=full_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _wait_health(health: HealthCheck, timeout_s: float, base_dir: Path, env: dict[str, str]) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _check_health(health, base_dir, env):
            return True
        time.sleep(0.25)
    return False


# ---------------------------------------------------------------------------
# Pre-spawn gates
# ---------------------------------------------------------------------------


def _check_gate(gate: PreSpawnGate, base_dir: Path) -> bool:
    if gate.cmd is not None:
        try:
            proc = subprocess.run(
                gate.cmd,
                cwd=str(base_dir),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return proc.returncode == 0
    if gate.file is not None:
        p = Path(gate.file)
        p = p if p.is_absolute() else (base_dir / p)
        return p.exists()
    if gate.http is not None:
        return _http_answers(gate.http, 2.0)
    if gate.tcp is not None:
        return _tcp_open(gate.tcp, 2.0)
    return False


def run_pre_spawn_gates(cfg: DevConfig) -> None:
    """Run every pre-spawn gate; raise :class:`PreSpawnGateError` on the first failure."""
    for gate in cfg.pre_spawn:
        if not _check_gate(gate, cfg.base_dir):
            raise PreSpawnGateError(f"pre-spawn gate failed: {gate.message}")


# ---------------------------------------------------------------------------
# DevProcess
# ---------------------------------------------------------------------------


class DevProcess:
    """A supervised child process running in its own process group."""

    def __init__(
        self,
        name: str,
        cmd: list[str],
        *,
        cwd: Path | str | None = None,
        env: dict[str, str] | None = None,
        grace_s: float = GRACE_DEFAULT,
    ) -> None:
        self.name = name
        self.cmd = list(cmd)
        self.cwd = str(cwd) if cwd is not None else None
        self.env = env
        self.grace_s = grace_s
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        full_env = None
        if self.env is not None:
            full_env = {**os.environ, **self.env}
        logger.info("starting %s: %s", self.name, " ".join(self.cmd))
        self.proc = subprocess.Popen(
            self.cmd,
            cwd=self.cwd,
            env=full_env,
            start_new_session=True,  # own process group / session
        )

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc else None

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def returncode(self) -> int | None:
        return self.proc.poll() if self.proc else None

    def _signal_group(self, sig: int) -> None:
        if self.proc is None:
            return
        try:
            os.killpg(os.getpgid(self.proc.pid), sig)
        except (ProcessLookupError, PermissionError):
            pass

    def forward_signal(self, sig: int) -> None:
        """Forward *sig* to the child's process group (if still running)."""
        if self.is_running():
            self._signal_group(sig)

    def stop(self, grace_s: float | None = None) -> None:
        """Graceful-then-kill: SIGTERM the group, wait *grace_s*, then SIGKILL."""
        if self.proc is None or self.proc.poll() is not None:
            return
        grace = self.grace_s if grace_s is None else grace_s
        logger.info("stopping %s (pid %s)", self.name, self.pid)
        self._signal_group(signal.SIGTERM)
        try:
            self.proc.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            logger.warning("%s did not stop within %.1fs; sending SIGKILL", self.name, grace)
        self._signal_group(signal.SIGKILL)
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.error("%s survived SIGKILL", self.name)


def teardown(components: list[DevProcess], grace_s: float = GRACE_DEFAULT) -> None:
    """Stop *components* in reverse start order."""
    for comp in reversed(components):
        comp.stop(grace_s)


def supervise_loop(
    started: list[DevProcess],
    stop_flag: dict,
    poll_s: float = 0.2,
) -> DevProcess | None:
    """Block until a component dies or ``stop_flag['flag']`` is set.

    Returns the first dead :class:`DevProcess`, or ``None`` when a stop was
    requested (clean shutdown). Does not tear anything down -- the caller owns
    teardown so ordering stays in one place.
    """
    while not stop_flag.get("flag"):
        for comp in started:
            if not comp.is_running():
                return comp
        time.sleep(poll_s)
    return None


# ---------------------------------------------------------------------------
# Component builders
# ---------------------------------------------------------------------------


def _vite_process(cfg: DevConfig, grace_s: float) -> DevProcess:
    v = cfg.vite
    return DevProcess("vite", v.cmd, cwd=cfg.resolve_cwd(v.cwd), env=v.env, grace_s=grace_s)


def _backend_process(cfg: DevConfig, grace_s: float) -> DevProcess:
    b = cfg.backend
    if b.is_in_process:
        # Spawn a launcher that serves the app in-process, wrapped with ViteDevProxy
        # so unmatched requests proxy to the running Vite dev server.
        launcher = (
            "from fastware.supervise import _serve_in_process; "
            f"_serve_in_process({b.app!r}, {b.host!r}, {int(b.port)}, {int(cfg.vite.port)})"
        )
        cmd = [sys.executable, "-c", launcher]
        return DevProcess("backend", cmd, cwd=cfg.resolve_cwd(b.cwd), env=b.env, grace_s=grace_s)
    return DevProcess("backend", b.cmd, cwd=cfg.resolve_cwd(b.cwd), env=b.env, grace_s=grace_s)


def _serve_in_process(app_path: str, host: str, port: int, vite_port: int) -> None:  # pragma: no cover - subprocess entry
    """Subprocess entry point: import an app, wrap with ViteDevProxy, serve it."""
    import importlib

    from fastware.middleware import ViteDevProxy
    from fastware.server import serve

    module_path, _, attr = app_path.partition(":")
    module = importlib.import_module(module_path)
    app = getattr(module, attr)
    wrapped = ViteDevProxy(app, vite_port=vite_port)
    serve(wrapped, foreground=True, host=host, port=port, single_instance=False)


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------


def _wait_vite_ready(cfg: DevConfig, proc: DevProcess, timeout: float = _VITE_READY_TIMEOUT) -> None:
    url = f"http://localhost:{cfg.vite.port}/"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not proc.is_running():
            raise ReadinessError(
                f"vite exited (code {proc.returncode()}) before becoming ready on "
                f"port {cfg.vite.port}"
            )
        if _http_answers(url, 1.0):
            return
        time.sleep(0.3)
    raise ReadinessError(
        f"vite did not answer on port {cfg.vite.port} within {timeout:.0f}s"
    )


def _wait_backend_ready(cfg: DevConfig, proc: DevProcess, timeout: float = _BACKEND_READY_TIMEOUT) -> None:
    b = cfg.backend
    if b.is_in_process:
        url = f"http://{b.host}:{b.port}/health"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not proc.is_running():
                raise ReadinessError(
                    f"backend exited (code {proc.returncode()}) before becoming ready"
                )
            if _http_answers(url, 1.0):
                return
            time.sleep(0.3)
        raise ReadinessError(
            f"backend did not answer on {b.host}:{b.port} within {timeout:.0f}s"
        )
    # cmd-form backend: no known port; readiness == survived a short settle window.
    time.sleep(0.5)
    if not proc.is_running():
        raise ReadinessError(
            f"backend command exited immediately (code {proc.returncode()})"
        )


def _start_ordered(cfg: DevConfig, grace_s: float, started: list[DevProcess]) -> None:
    """Start vite + backend in the order dictated by topology, readying each."""
    # Ensure the vite port is free before spawning (--strictPort would fail late).
    if _port_in_use(cfg.vite.port):
        raise PortCollisionError(
            f"vite port {cfg.vite.port} is already in use; free it or change vite.port"
        )
    if cfg.backend.is_in_process and _port_in_use(cfg.backend.port, cfg.backend.host):
        raise PortCollisionError(
            f"backend port {cfg.backend.port} is already in use; free it or change backend.port"
        )

    def start_vite() -> None:
        p = _vite_process(cfg, grace_s)
        p.start()
        started.append(p)
        _wait_vite_ready(cfg, p)

    def start_backend() -> None:
        p = _backend_process(cfg, grace_s)
        p.start()
        started.append(p)
        _wait_backend_ready(cfg, p)

    if cfg.topology == "vite-first":
        start_vite()
        start_backend()
    else:  # backend-first
        start_backend()
        start_vite()


# ---------------------------------------------------------------------------
# SW interplay
# ---------------------------------------------------------------------------


def resolve_backend_sw_mode(cfg: DevConfig) -> str | None:
    """Import the in-process app and return its resolved ``sw_mode`` (or ``None``).

    Only meaningful for the ``app`` backend form; returns ``None`` for cmd-form
    backends (whose sw_mode cannot be introspected without running them).
    """
    if not cfg.backend.is_in_process:
        return None
    import importlib

    module_path, _, attr = cfg.backend.app.partition(":")
    module = importlib.import_module(module_path)
    app = getattr(module, attr)
    return getattr(app, "fastware_sw_mode", None)


def check_sw_mode(cfg: DevConfig) -> None:
    """Hard-error if the resolved in-process app uses ``sw_mode='cache'``.

    cmd-form backends run as opaque subprocesses whose ``sw_mode`` cannot be
    introspected without executing them, so the guard cannot verify them. Rather
    than skip silently, emit a one-line stderr notice so the operator knows the
    guard did not run for this backend.
    """
    if not cfg.backend.is_in_process:
        print(
            "fastware dev: SW-mode guard skipped -- the cmd-form backend is not "
            "introspectable for sw_mode. Ensure the command does not serve a "
            "caching service worker (sw_mode='cache') in development.",
            file=sys.stderr,
        )
        return
    if resolve_backend_sw_mode(cfg) == "cache":
        raise SWModeConflictError(
            "the app resolved from backend.app is configured with sw_mode='cache', "
            "which is incompatible with `fastware dev run`: Vite serves dev assets "
            "with non-content hashes, so a caching service worker would pin stale "
            "dev artifacts. Set sw_mode='off' for development, or run without the "
            "dev CLI."
        )


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


def dev_pid_path(cfg: DevConfig, pid_dir: Path | str | None = None) -> Path:
    base = Path(pid_dir) if pid_dir is not None else cfg.base_dir
    return base / f".fastware-dev-{cfg.app_name}.pid"


def _dev_entry_name(cfg: DevConfig) -> str:
    return f"{cfg.app_name}-dev"


def list_dev_instances(cfg: DevConfig, pid_dir: Path | str | None = None):
    """Return live registered dev instances (registry entries with the dev role)."""
    from fastware import server

    pid_path = dev_pid_path(cfg, pid_dir)
    return [e for e in server.list_instances(pid_path) if e.name.endswith("-dev")]


def _terminate_pid(pid: int, grace_s: float) -> None:
    from fastware.server import _pid_alive

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        return
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(0.2)
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def stop_dev_instances(cfg: DevConfig, pid_dir: Path | str | None = None, grace_s: float = GRACE_DEFAULT):
    """Terminate every registered dev instance gracefully-then-kill. Returns them."""
    entries = list_dev_instances(cfg, pid_dir)
    for entry in entries:
        _terminate_pid(entry.pid, grace_s)
    return entries


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _spawn_daemon(cfg: DevConfig, pid_dir: Path | str | None) -> int:
    """Spawn a detached ``fastware dev run --no-daemon`` and wait for it to register."""
    base = cfg.base_dir
    log_path = dev_pid_path(cfg, pid_dir).with_suffix(".log")
    log_file = open(log_path, "ab")  # noqa: SIM115 - handed to the child, closed here after spawn
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "fastware", "dev", "run", "--no-daemon"],
            cwd=str(base),
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        log_file.close()
    # Wait for the child to register itself so `dev status` sees it immediately.
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise DevRunError(
                f"daemon exited during startup (code {proc.returncode}); see {log_path}"
            )
        if any(e.pid == proc.pid for e in list_dev_instances(cfg, pid_dir)):
            return proc.pid
        time.sleep(0.3)
    raise DevRunError(
        f"daemon did not register within 15s; see {log_path}"
    )


def _run_foreground(cfg: DevConfig, *, grace_s: float, pid_dir: Path | str | None) -> int:
    from fastware import server

    started: list[DevProcess] = []
    stop_requested = {"flag": False}

    def _handle_signal(signum, _frame):
        stop_requested["flag"] = True
        for comp in started:
            comp.forward_signal(signum)

    run_pre_spawn_gates(cfg)

    prev_int = signal.getsignal(signal.SIGINT)
    prev_term = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    pid_path = dev_pid_path(cfg, pid_dir)
    dead: DevProcess | None = None
    try:
        # aux services, ordered + health-gated
        for aux in cfg.aux:
            proc = DevProcess(
                aux.name, aux.cmd, cwd=cfg.resolve_cwd(aux.cwd), env=aux.env, grace_s=grace_s
            )
            proc.start()
            started.append(proc)
            if not _wait_health(aux.health, aux.timeout_s, cfg.base_dir, aux.env):
                raise ReadinessError(
                    f"aux service {aux.name!r} did not become healthy within "
                    f"{aux.timeout_s}s"
                )
            if not proc.is_running():
                raise ComponentDied(aux.name, proc.returncode())

        # topology-ordered vite + backend
        _start_ordered(cfg, grace_s, started)

        # register in the instance registry (dev role)
        port = cfg.backend.port or cfg.vite.port
        server.register_instance(pid_path, port, _dev_entry_name(cfg))
        logger.info("dev environment ready (%s components)", len(started))

        # supervise
        dead = supervise_loop(started, stop_requested)
    finally:
        signal.signal(signal.SIGINT, prev_int)
        signal.signal(signal.SIGTERM, prev_term)
        teardown(started, grace_s)
        server.deregister_instance(pid_path)

    if dead is not None:
        raise ComponentDied(dead.name, dead.returncode())
    return 0


def run_dev(
    cfg: DevConfig,
    *,
    daemon: bool,
    grace_s: float = GRACE_DEFAULT,
    pid_dir: Path | str | None = None,
) -> int:
    """Run the dev environment. Foreground blocks; daemon detaches and returns.

    Always performs the sw_mode='cache' hard-error check up front.
    """
    check_sw_mode(cfg)
    if daemon:
        pid = _spawn_daemon(cfg, pid_dir)
        print(f"fastware dev daemon started (pid {pid}), name {_dev_entry_name(cfg)!r}")
        return 0
    return _run_foreground(cfg, grace_s=grace_s, pid_dir=pid_dir)
