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


def _opt(value, fallback):
    """Resolve an absent optional flag to the fallback its own help declares.

    strictcli forbids a value ``default=`` on any flag of a ``mutating``
    command: a value the framework picks is a value the framework writes
    without the invocation ever having stated it. All three ``dev`` commands
    are mutating, so their flags declare ``presence="optional"`` -- absence
    arrives as ``None`` -- and name their fallback in their help text. This is
    the one place absence becomes that fallback, so nothing downstream ever
    reads a ``None`` as false or as zero.
    """
    return fallback if value is None else value


@dev.command(
    "run",
    # mutating: spawns the Vite dev server and the Granian backend as real
    # child processes, writes a PID file, and registers the instance in the
    # on-disk registry (which --daemon then leaves behind after detaching).
    effect="mutating",
    help="Start the dev environment by reading [tool.fastware.dev] from the nearest "
    "pyproject.toml, running pre-spawn gates to check prerequisites, launching "
    "auxiliary services and the Vite frontend dev server, wrapping the ASGI app with "
    "ViteDevProxy for backend-first routing, and starting the Granian server in the "
    "foreground by default",
)
@flag(
    "daemon",
    type=bool,
    presence="optional",
    help="Detach and run the dev environment in the background, registering it in "
    "the instance registry (query with 'dev status', stop with 'dev stop'). "
    "When neither --daemon nor --no-daemon is passed, the dev environment runs "
    "in the foreground and blocks until Ctrl+C.",
)
@flag(
    "grace",
    type=int,
    presence="optional",
    help="Seconds to wait for a component to stop gracefully before SIGKILL. "
    "When the flag is not passed, 10 seconds is used.",
)
def dev_run(ctx, daemon: bool | None, grace: int | None) -> int:
    """Handler for ``fastware dev run``."""
    from fastware.devconfig import DevConfigError
    from fastware.supervise import ComponentDied, DevRunError, run_dev

    daemon = _opt(daemon, False)
    grace = _opt(grace, 10)
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
    # mutating, which is not obvious and is the reason this comment exists:
    # listing the registry prunes it. `server.list_instances` unlinks every
    # descriptor whose PID is no longer alive and every descriptor it cannot
    # decode, so a plain `dev status` deletes files from disk. That is
    # bookkeeping rather than anything the user asked for, but it is still a
    # delete, and declaring it read-only would make routing the unlink through
    # `ctx.effects.remove` a hard error at call time later on.
    effect="mutating",
    help="List all running fastware dev environments registered in the instance "
    "registry, showing the instance name, process ID, and port for each entry. "
    "Instances register when started with --daemon and are automatically removed "
    "when they exit or are stopped with dev stop",
)
def dev_status(ctx) -> int:
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
    # mutating: sends SIGTERM (then SIGKILL) to every registered dev process
    # and drops the corresponding registry entries.
    effect="mutating",
    help="Stop all running dev environments by sending SIGTERM for a graceful "
    "shutdown. If a process does not exit within the grace period (default 10 "
    "seconds, configurable with --grace), it is forcibly terminated with SIGKILL. "
    "Stopped instances are removed from the instance registry",
)
@flag(
    "grace",
    type=int,
    presence="optional",
    help="Seconds to wait for a dev environment to stop gracefully before SIGKILL. "
    "When the flag is not passed, 10 seconds is used.",
)
def dev_stop(ctx, grace: int | None) -> int:
    """Handler for ``fastware dev stop``."""
    from fastware.devconfig import DevConfigError
    from fastware.supervise import stop_dev_instances

    grace = _opt(grace, 10)
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
