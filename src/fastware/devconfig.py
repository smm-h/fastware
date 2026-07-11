"""File-driven dev configuration for the ``fastware dev`` CLI.

Reads a ``[tool.fastware.dev]`` table from the nearest owning ``pyproject.toml``
and validates it into a :class:`DevConfig`. All fields are explicit -- there are
no silent defaults for anything that changes behaviour; invalid or ambiguous
configuration is a hard error (:class:`DevConfigError`).

Location rule (enforced by :func:`find_dev_pyproject`): starting from a directory,
walk up to the filesystem root collecting every ``pyproject.toml`` that contains a
``[tool.fastware.dev]`` table. Zero found is an error naming the rule; two or more
(a table nested under another table) is an ambiguity error listing both. Exactly
one is required. In a monorepo the table lives in the package that owns the
fastware app + frontend.
"""

from __future__ import annotations

import dataclasses
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "DevConfigError",
    "BackendSpec",
    "ViteSpec",
    "HealthCheck",
    "AuxService",
    "PreSpawnGate",
    "DevConfig",
    "find_dev_pyproject",
    "load_dev_config",
]

_TOPOLOGIES = ("backend-first", "vite-first")


class DevConfigError(Exception):
    """Raised for any missing, unknown, or contradictory dev-config value."""


def _require_str_list(value: object, where: str) -> list[str]:
    if not isinstance(value, list) or not value or not all(isinstance(x, str) for x in value):
        raise DevConfigError(f"{where} must be a non-empty array of strings, got {value!r}")
    return list(value)


def _require_str_dict(value: object, where: str) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    ):
        raise DevConfigError(f"{where} must be a table of string->string, got {value!r}")
    return dict(value)


def _reject_unknown(table: dict, allowed: set[str], where: str) -> None:
    unknown = set(table) - allowed
    if unknown:
        raise DevConfigError(
            f"{where}: unknown key(s) {', '.join(sorted(unknown))}; "
            f"allowed: {', '.join(sorted(allowed))}"
        )


# ---------------------------------------------------------------------------
# Component specs
# ---------------------------------------------------------------------------


@dataclass
class BackendSpec:
    """How to run the backend: in-process app OR an arbitrary spawn command.

    Exactly one of ``app`` (``"module:attr"`` served in-process via ``serve``,
    wrapped with ViteDevProxy) or ``cmd`` (arbitrary argv spawned directly) must
    be set -- never both, never neither.
    """

    app: str | None = None
    cmd: list[str] | None = None
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    # Only meaningful for the ``app`` form: bind host/port for the in-process serve.
    host: str = "127.0.0.1"
    port: int | None = None

    def __post_init__(self) -> None:
        has_app = self.app is not None
        has_cmd = self.cmd is not None
        if has_app == has_cmd:
            raise DevConfigError(
                "backend must set exactly one of 'app' (in-process) or 'cmd' "
                "(arbitrary spawn); "
                + ("both were set" if has_app else "neither was set")
            )
        if has_app:
            if not isinstance(self.app, str) or ":" not in self.app:
                raise DevConfigError(
                    f"backend.app must be a 'module:attr' string, got {self.app!r}"
                )
            if self.port is None:
                raise DevConfigError(
                    "backend.port is required for the in-process 'app' form "
                    "(the server has to bind a port)."
                )
            if not isinstance(self.port, int) or isinstance(self.port, bool):
                raise DevConfigError(f"backend.port must be an integer, got {self.port!r}")
        else:
            self.cmd = _require_str_list(self.cmd, "backend.cmd")
            if self.port is not None:
                raise DevConfigError("backend.port is only valid with the 'app' form")

    @property
    def is_in_process(self) -> bool:
        return self.app is not None


@dataclass
class ViteSpec:
    """Vite dev server: command, working dir, port, and env.

    ``--strictPort`` is always enforced (appended if absent) so Vite fails loudly
    on a port collision instead of silently drifting to another port that the
    readiness probe and proxy would then miss.
    """

    cmd: list[str]
    port: int
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.cmd = _require_str_list(self.cmd, "vite.cmd")
        if not isinstance(self.port, int) or isinstance(self.port, bool):
            raise DevConfigError(f"vite.port must be an integer, got {self.port!r}")
        if "--strictPort" not in self.cmd:
            self.cmd = [*self.cmd, "--strictPort"]


@dataclass
class HealthCheck:
    """A readiness check for an aux service: exactly one of http/tcp/cmd."""

    http: str | None = None
    tcp: str | None = None
    cmd: list[str] | None = None

    def __post_init__(self) -> None:
        chosen = [k for k, v in (("http", self.http), ("tcp", self.tcp), ("cmd", self.cmd)) if v is not None]
        if len(chosen) != 1:
            raise DevConfigError(
                "aux.health must set exactly one of 'http', 'tcp', or 'cmd'; "
                f"got {chosen or 'none'}"
            )
        if self.cmd is not None:
            self.cmd = _require_str_list(self.cmd, "aux.health.cmd")
        if self.http is not None and not isinstance(self.http, str):
            raise DevConfigError("aux.health.http must be a URL string")
        if self.tcp is not None and not isinstance(self.tcp, str):
            raise DevConfigError("aux.health.tcp must be a 'host:port' string")


@dataclass
class AuxService:
    """An ordered auxiliary service started (and health-gated) before the backend."""

    name: str
    cmd: list[str]
    health: HealthCheck
    timeout_s: float
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise DevConfigError("aux.name must be a non-empty string")
        self.cmd = _require_str_list(self.cmd, f"aux[{self.name}].cmd")
        if not isinstance(self.health, HealthCheck):
            raise DevConfigError(f"aux[{self.name}].health must be a HealthCheck")
        if isinstance(self.timeout_s, bool) or not isinstance(self.timeout_s, (int, float)):
            raise DevConfigError(f"aux[{self.name}].timeout_s must be a number")
        if self.timeout_s <= 0:
            raise DevConfigError(f"aux[{self.name}].timeout_s must be > 0")


@dataclass
class PreSpawnGate:
    """A pre-flight gate that must pass BEFORE anything spawns.

    Exactly one probe kind (cmd/http/tcp/file) plus a mandatory human-readable
    ``message`` shown when the gate fails.
    """

    message: str
    cmd: list[str] | None = None
    http: str | None = None
    tcp: str | None = None
    file: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.message, str) or not self.message:
            raise DevConfigError("pre_spawn gate requires a non-empty 'message'")
        chosen = [
            k
            for k, v in (("cmd", self.cmd), ("http", self.http), ("tcp", self.tcp), ("file", self.file))
            if v is not None
        ]
        if len(chosen) != 1:
            raise DevConfigError(
                "pre_spawn gate must set exactly one of 'cmd', 'http', 'tcp', or "
                f"'file'; got {chosen or 'none'}"
            )
        if self.cmd is not None:
            self.cmd = _require_str_list(self.cmd, "pre_spawn.cmd")


@dataclass
class DevConfig:
    """Validated dev configuration resolved from ``[tool.fastware.dev]``."""

    topology: str
    backend: BackendSpec
    vite: ViteSpec
    pre_spawn: list[PreSpawnGate] = field(default_factory=list)
    aux: list[AuxService] = field(default_factory=list)
    # The pyproject.toml this config was resolved from (base for relative cwds).
    source_path: Path | None = None
    # Human-facing app name (from [project].name or the owning dir), used for the
    # dev instance registry entry name.
    app_name: str = "fastware"

    def __post_init__(self) -> None:
        if self.topology not in _TOPOLOGIES:
            raise DevConfigError(
                f"topology must be one of {_TOPOLOGIES}, got {self.topology!r}"
            )

    @property
    def base_dir(self) -> Path:
        return self.source_path.parent if self.source_path else Path.cwd()

    def resolve_cwd(self, cwd: str | None) -> Path:
        """Resolve a component cwd relative to the owning pyproject directory."""
        if cwd is None:
            return self.base_dir
        p = Path(cwd)
        return p if p.is_absolute() else (self.base_dir / p)


# ---------------------------------------------------------------------------
# Table parsing
# ---------------------------------------------------------------------------


def _build_backend(table: dict) -> BackendSpec:
    if not isinstance(table, dict):
        raise DevConfigError("[tool.fastware.dev.backend] must be a table")
    _reject_unknown(table, {"app", "cmd", "env", "cwd", "host", "port"}, "backend")
    return BackendSpec(
        app=table.get("app"),
        cmd=table.get("cmd"),
        env=_require_str_dict(table.get("env", {}), "backend.env"),
        cwd=table.get("cwd"),
        host=table.get("host", "127.0.0.1"),
        port=table.get("port"),
    )


def _build_vite(table: dict) -> ViteSpec:
    if not isinstance(table, dict):
        raise DevConfigError("[tool.fastware.dev.vite] must be a table")
    _reject_unknown(table, {"cmd", "cwd", "port", "env"}, "vite")
    if "cmd" not in table:
        raise DevConfigError("vite.cmd is required")
    if "port" not in table:
        raise DevConfigError("vite.port is required")
    return ViteSpec(
        cmd=table["cmd"],
        port=table["port"],
        cwd=table.get("cwd"),
        env=_require_str_dict(table.get("env", {}), "vite.env"),
    )


def _build_health(table: dict) -> HealthCheck:
    if not isinstance(table, dict):
        raise DevConfigError("aux.health must be a table")
    _reject_unknown(table, {"http", "tcp", "cmd"}, "aux.health")
    return HealthCheck(http=table.get("http"), tcp=table.get("tcp"), cmd=table.get("cmd"))


def _build_aux(items: object) -> list[AuxService]:
    if items in (None, []):
        return []
    if not isinstance(items, list):
        raise DevConfigError("[[tool.fastware.dev.aux]] must be an array of tables")
    result: list[AuxService] = []
    for item in items:
        if not isinstance(item, dict):
            raise DevConfigError("each aux entry must be a table")
        _reject_unknown(item, {"name", "cmd", "health", "timeout_s", "env", "cwd"}, "aux")
        for req in ("name", "cmd", "health", "timeout_s"):
            if req not in item:
                raise DevConfigError(f"aux entry is missing required key '{req}'")
        result.append(
            AuxService(
                name=item["name"],
                cmd=item["cmd"],
                health=_build_health(item["health"]),
                timeout_s=item["timeout_s"],
                env=_require_str_dict(item.get("env", {}), "aux.env"),
                cwd=item.get("cwd"),
            )
        )
    return result


def _build_pre_spawn(items: object) -> list[PreSpawnGate]:
    if items in (None, []):
        return []
    if not isinstance(items, list):
        raise DevConfigError("[[tool.fastware.dev.pre_spawn]] must be an array of tables")
    result: list[PreSpawnGate] = []
    for item in items:
        if not isinstance(item, dict):
            raise DevConfigError("each pre_spawn entry must be a table")
        _reject_unknown(item, {"message", "cmd", "http", "tcp", "file"}, "pre_spawn")
        if "message" not in item:
            raise DevConfigError("pre_spawn entry is missing required key 'message'")
        result.append(
            PreSpawnGate(
                message=item["message"],
                cmd=item.get("cmd"),
                http=item.get("http"),
                tcp=item.get("tcp"),
                file=item.get("file"),
            )
        )
    return result


def build_dev_config(table: dict, *, source_path: Path | None = None, app_name: str = "fastware") -> DevConfig:
    """Build a :class:`DevConfig` from a raw ``[tool.fastware.dev]`` mapping."""
    if not isinstance(table, dict):
        raise DevConfigError("[tool.fastware.dev] must be a table")
    _reject_unknown(table, {"topology", "backend", "vite", "pre_spawn", "aux"}, "[tool.fastware.dev]")
    if "topology" not in table:
        raise DevConfigError(
            "topology is required in [tool.fastware.dev] "
            f"(one of {_TOPOLOGIES})"
        )
    if "backend" not in table:
        raise DevConfigError("[tool.fastware.dev.backend] is required")
    if "vite" not in table:
        raise DevConfigError("[tool.fastware.dev.vite] is required")
    return DevConfig(
        topology=table["topology"],
        backend=_build_backend(table["backend"]),
        vite=_build_vite(table["vite"]),
        pre_spawn=_build_pre_spawn(table.get("pre_spawn")),
        aux=_build_aux(table.get("aux")),
        source_path=source_path,
        app_name=app_name,
    )


# ---------------------------------------------------------------------------
# Location rule
# ---------------------------------------------------------------------------


def _has_dev_table(pyproject: Path) -> bool:
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    return isinstance(data.get("tool"), dict) and isinstance(
        data["tool"].get("fastware"), dict
    ) and isinstance(data["tool"]["fastware"].get("dev"), dict)


def find_dev_pyproject(start: Path | str | None = None) -> Path:
    """Return the single owning ``pyproject.toml`` per the location rule.

    Walks up from *start* (default CWD) to the filesystem root collecting every
    ``pyproject.toml`` that contains a ``[tool.fastware.dev]`` table:

    - zero found -> :class:`DevConfigError` naming the location rule
    - two or more -> :class:`DevConfigError` listing all candidates (ambiguous)
    - exactly one -> that path
    """
    start_dir = Path(start).resolve() if start is not None else Path.cwd().resolve()
    candidates: list[Path] = []
    for d in (start_dir, *start_dir.parents):
        pp = d / "pyproject.toml"
        if pp.is_file() and _has_dev_table(pp):
            candidates.append(pp)
    if not candidates:
        raise DevConfigError(
            "no pyproject.toml with a [tool.fastware.dev] table was found walking "
            f"up from {start_dir}. The fastware dev CLI is driven by a "
            "[tool.fastware.dev] table in the nearest owning pyproject.toml; in a "
            "monorepo it lives in the package that owns the fastware app + frontend."
        )
    if len(candidates) > 1:
        listed = "\n".join(f"  - {c}" for c in candidates)
        raise DevConfigError(
            "ambiguous dev config: multiple pyproject.toml files with a "
            "[tool.fastware.dev] table were found in the ancestor chain "
            f"(a table nested under another). Exactly one must own the config:\n{listed}"
        )
    return candidates[0]


def load_dev_config(start: Path | str | None = None) -> DevConfig:
    """Locate and parse the dev config per the location rule."""
    pyproject = find_dev_pyproject(start)
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    table = data["tool"]["fastware"]["dev"]
    project = data.get("project")
    app_name = "fastware"
    if isinstance(project, dict) and isinstance(project.get("name"), str) and project["name"]:
        app_name = project["name"]
    else:
        app_name = pyproject.parent.name or "fastware"
    return build_dev_config(table, source_path=pyproject, app_name=app_name)
