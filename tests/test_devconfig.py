"""Tests for file-driven dev config loading and validation (Phase 13.1)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from fastware.devconfig import (
    DevConfig,
    DevConfigError,
    build_dev_config,
    find_dev_pyproject,
    load_dev_config,
)

# ---------------------------------------------------------------------------
# Fixtures: two topologies
# ---------------------------------------------------------------------------

CLAUDETIMELINE_SHAPED = """
[project]
name = "claudetimeline"

[tool.fastware.dev]
topology = "vite-first"

[tool.fastware.dev.backend]
app = "claudetimeline.app:app"
port = 8000

[tool.fastware.dev.vite]
cmd = ["npm", "run", "dev"]
cwd = "frontend"
port = 5173
env = { NODE_ENV = "development" }

[[tool.fastware.dev.pre_spawn]]
message = "npm is required on PATH"
cmd = ["python", "-c", "import shutil,sys; sys.exit(0 if shutil.which('python') else 1)"]

[[tool.fastware.dev.pre_spawn]]
message = "frontend/ directory must exist"
file = "frontend"

[[tool.fastware.dev.pre_spawn]]
message = "postgres must be reachable"
tcp = "localhost:5432"
"""

VELIU_SHAPED = """
[project]
name = "veliu"

[tool.fastware.dev]
topology = "backend-first"

[tool.fastware.dev.backend]
cmd = ["granian", "--interface", "asgi", "veliu.app:app"]
env = { VELIU_ENV = "dev" }

[tool.fastware.dev.vite]
cmd = ["npm", "run", "dev"]
port = 5174

[[tool.fastware.dev.aux]]
name = "postgres"
cmd = ["docker", "compose", "up", "pg"]
timeout_s = 30
health = { tcp = "localhost:5432" }
"""


def _write(tmp_path: Path, body: str, subdir: str = "") -> Path:
    d = tmp_path / subdir if subdir else tmp_path
    d.mkdir(parents=True, exist_ok=True)
    pp = d / "pyproject.toml"
    pp.write_text(textwrap.dedent(body))
    return pp


# ---------------------------------------------------------------------------
# Location rule
# ---------------------------------------------------------------------------


def test_location_rule_zero_found(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n')
    with pytest.raises(DevConfigError, match="no pyproject.toml with a \\[tool.fastware.dev\\]"):
        find_dev_pyproject(tmp_path)


def test_location_rule_exactly_one(tmp_path):
    pp = _write(tmp_path, VELIU_SHAPED)
    nested = tmp_path / "src" / "pkg"
    nested.mkdir(parents=True)
    assert find_dev_pyproject(nested) == pp


def test_location_rule_nested_multiple_is_ambiguous(tmp_path):
    outer = _write(tmp_path, VELIU_SHAPED)
    inner = _write(tmp_path, CLAUDETIMELINE_SHAPED, subdir="packages/app")
    with pytest.raises(DevConfigError, match="ambiguous dev config") as exc:
        find_dev_pyproject(inner.parent)
    assert str(outer) in str(exc.value)
    assert str(inner) in str(exc.value)


# ---------------------------------------------------------------------------
# Parsing both shapes
# ---------------------------------------------------------------------------


def test_claudetimeline_shape(tmp_path):
    _write(tmp_path, CLAUDETIMELINE_SHAPED)
    cfg = load_dev_config(tmp_path)
    assert cfg.topology == "vite-first"
    assert cfg.app_name == "claudetimeline"
    assert cfg.backend.is_in_process
    assert cfg.backend.app == "claudetimeline.app:app"
    assert cfg.backend.port == 8000
    assert cfg.vite.port == 5173
    assert cfg.vite.env == {"NODE_ENV": "development"}
    assert len(cfg.pre_spawn) == 3
    kinds = {("cmd" if g.cmd else "file" if g.file else "tcp") for g in cfg.pre_spawn}
    assert kinds == {"cmd", "file", "tcp"}


def test_veliu_shape(tmp_path):
    _write(tmp_path, VELIU_SHAPED)
    cfg = load_dev_config(tmp_path)
    assert cfg.topology == "backend-first"
    assert not cfg.backend.is_in_process
    assert cfg.backend.cmd[0] == "granian"
    assert cfg.backend.env == {"VELIU_ENV": "dev"}
    assert len(cfg.aux) == 1
    aux = cfg.aux[0]
    assert aux.name == "postgres"
    assert aux.timeout_s == 30
    assert aux.health.tcp == "localhost:5432"


# ---------------------------------------------------------------------------
# strictPort enforcement
# ---------------------------------------------------------------------------


def test_strictport_appended_when_absent(tmp_path):
    _write(tmp_path, VELIU_SHAPED)
    cfg = load_dev_config(tmp_path)
    assert cfg.vite.cmd == ["npm", "run", "dev", "--strictPort"]


def test_strictport_not_duplicated_when_present(tmp_path):
    body = VELIU_SHAPED.replace(
        'cmd = ["npm", "run", "dev"]\nport = 5174',
        'cmd = ["npm", "run", "dev", "--strictPort"]\nport = 5174',
    )
    _write(tmp_path, body)
    cfg = load_dev_config(tmp_path)
    assert cfg.vite.cmd.count("--strictPort") == 1


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_topology_required():
    with pytest.raises(DevConfigError, match="topology is required"):
        build_dev_config({"backend": {"cmd": ["x"]}, "vite": {"cmd": ["v"], "port": 1}})


def test_topology_invalid():
    with pytest.raises(DevConfigError, match="topology must be one of"):
        build_dev_config(
            {"topology": "sideways", "backend": {"cmd": ["x"]}, "vite": {"cmd": ["v"], "port": 1}}
        )


def test_backend_requires_exactly_one_form():
    with pytest.raises(DevConfigError, match="exactly one of 'app'"):
        build_dev_config(
            {"topology": "backend-first", "backend": {"app": "m:a", "cmd": ["x"], "port": 1},
             "vite": {"cmd": ["v"], "port": 1}}
        )
    with pytest.raises(DevConfigError, match="exactly one of 'app'"):
        build_dev_config(
            {"topology": "backend-first", "backend": {}, "vite": {"cmd": ["v"], "port": 1}}
        )


def test_backend_app_requires_port():
    with pytest.raises(DevConfigError, match="backend.port is required"):
        build_dev_config(
            {"topology": "backend-first", "backend": {"app": "m:a"},
             "vite": {"cmd": ["v"], "port": 1}}
        )


def test_unknown_key_rejected():
    with pytest.raises(DevConfigError, match="unknown key"):
        build_dev_config(
            {"topology": "backend-first", "backend": {"cmd": ["x"]},
             "vite": {"cmd": ["v"], "port": 1}, "bogus": 1}
        )


def test_pre_spawn_requires_message():
    with pytest.raises(DevConfigError, match="missing required key 'message'"):
        build_dev_config(
            {"topology": "backend-first", "backend": {"cmd": ["x"]},
             "vite": {"cmd": ["v"], "port": 1}, "pre_spawn": [{"cmd": ["y"]}]}
        )


def test_pre_spawn_requires_exactly_one_probe():
    with pytest.raises(DevConfigError, match="exactly one of 'cmd'"):
        build_dev_config(
            {"topology": "backend-first", "backend": {"cmd": ["x"]},
             "vite": {"cmd": ["v"], "port": 1},
             "pre_spawn": [{"message": "m", "cmd": ["y"], "file": "z"}]}
        )


def test_aux_health_requires_exactly_one():
    with pytest.raises(DevConfigError, match="exactly one of 'http'"):
        build_dev_config(
            {"topology": "backend-first", "backend": {"cmd": ["x"]},
             "vite": {"cmd": ["v"], "port": 1},
             "aux": [{"name": "n", "cmd": ["c"], "timeout_s": 1,
                      "health": {"http": "u", "tcp": "t"}}]}
        )


def test_resolve_cwd_is_relative_to_pyproject(tmp_path):
    _write(tmp_path, CLAUDETIMELINE_SHAPED)
    cfg = load_dev_config(tmp_path)
    assert cfg.resolve_cwd("frontend") == (tmp_path / "frontend").resolve() or (tmp_path / "frontend")
    assert cfg.resolve_cwd(None) == tmp_path
