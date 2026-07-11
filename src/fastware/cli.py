"""The ``fastware`` command-line interface (strictcli).

Exposes the ``dev`` command group -- ``run`` / ``status`` / ``stop`` -- driven by
the ``[tool.fastware.dev]`` table in the nearest owning ``pyproject.toml`` (see
:mod:`fastware.devconfig`). Kept out of ``import fastware`` so the framework's
import stays light; this module (and strictcli) load only when the CLI runs.
"""

from __future__ import annotations

import sys

from strictcli import App, flag

from fastware import __version__

app = App(
    name="fastware",
    help="fastware framework CLI: run a file-driven Vite + backend dev environment",
    version=__version__,
)

dev = app.group("dev", help="Run and manage the file-driven development environment")


def _load_cfg():
    from fastware.devconfig import load_dev_config

    return load_dev_config()


@dev.command(
    "run",
    help="Start the dev environment (pre-spawn gates, aux services, Vite + backend) "
    "from the nearest [tool.fastware.dev] config",
)
@flag(
    "daemon",
    type=bool,
    default=False,
    help="Detach and run the dev environment in the background, registering it in "
    "the instance registry (query with 'dev status', stop with 'dev stop'). "
    "Default runs in the foreground and blocks until Ctrl+C.",
)
@flag(
    "grace",
    type=int,
    default=10,
    help="Seconds to wait for a component to stop gracefully before SIGKILL.",
)
def dev_run(daemon: bool, grace: int, **_kw: object) -> int:
    """Handler for ``fastware dev run``."""
    from fastware.devconfig import DevConfigError
    from fastware.supervise import ComponentDied, DevRunError, run_dev

    try:
        cfg = _load_cfg()
    except DevConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        return run_dev(cfg, daemon=daemon, grace_s=float(grace))
    except ComponentDied as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except DevRunError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


@dev.command(
    "status",
    help="List running dev environments registered in the instance registry",
)
def dev_status(**_kw: object) -> int:
    """Handler for ``fastware dev status``."""
    from fastware.devconfig import DevConfigError
    from fastware.supervise import list_dev_instances

    try:
        cfg = _load_cfg()
    except DevConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    entries = list_dev_instances(cfg)
    if not entries:
        print("no running fastware dev environments")
        return 0
    print(f"{'NAME':<28} {'PID':>8} {'PORT':>6}")
    for e in entries:
        print(f"{e.name:<28} {e.pid:>8} {e.port:>6}")
    return 0


@dev.command(
    "stop",
    help="Stop running dev environments gracefully (then SIGKILL after the grace period)",
)
@flag(
    "grace",
    type=int,
    default=10,
    help="Seconds to wait for a dev environment to stop gracefully before SIGKILL.",
)
def dev_stop(grace: int, **_kw: object) -> int:
    """Handler for ``fastware dev stop``."""
    from fastware.devconfig import DevConfigError
    from fastware.supervise import stop_dev_instances

    try:
        cfg = _load_cfg()
    except DevConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    entries = stop_dev_instances(cfg, grace_s=float(grace))
    if not entries:
        print("no running fastware dev environments to stop")
        return 0
    for e in entries:
        print(f"stopped {e.name} (pid {e.pid})")
    return 0


def main() -> None:
    """Entry point for the ``fastware`` console script."""
    app.run()


if __name__ == "__main__":
    main()
