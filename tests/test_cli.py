"""Tests for the fastware CLI surface (Phase 13.1)."""

from __future__ import annotations

import socket
import textwrap
from pathlib import Path

from fastware.cli import app


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_help_lists_dev_group():
    r = app.test(["--help"])
    assert r.exit_code == 0
    assert "dev" in r.stdout


def test_dev_run_help_shows_daemon_flag():
    r = app.test(["dev", "run", "--help"])
    assert r.exit_code == 0
    assert "--daemon" in r.stdout and "--no-daemon" in r.stdout
    assert "--grace" in r.stdout


def test_dump_schema_writes_file(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "probe"\n')
    monkeypatch.chdir(tmp_path)
    r = app.test(["--dump-schema"])
    assert r.exit_code == 0
    assert (tmp_path / ".strictcli" / "schema.json").is_file()


def test_status_no_config_is_loud(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = app.test(["dev", "status"])
    assert r.exit_code == 1
    assert "[tool.fastware.dev]" in r.stderr


def test_stop_no_config_is_loud(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = app.test(["dev", "stop"])
    assert r.exit_code == 1
    assert "[tool.fastware.dev]" in r.stderr


def test_run_sw_cache_is_loud(tmp_path, monkeypatch):
    port = _free_port()
    vport = _free_port()
    (tmp_path / "pyproject.toml").write_text(
        textwrap.dedent(
            f"""
            [project]
            name = "probe"

            [tool.fastware.dev]
            topology = "backend-first"

            [tool.fastware.dev.backend]
            app = "tests.dev_fixture_apps:cache_app"
            port = {port}

            [tool.fastware.dev.vite]
            cmd = ["stub"]
            port = {vport}
            """
        )
    )
    monkeypatch.chdir(tmp_path)
    r = app.test(["dev", "run", "--no-daemon"])
    assert r.exit_code == 1
    assert "cache" in r.stderr


def test_run_bad_config_is_loud(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text(
        textwrap.dedent(
            """
            [tool.fastware.dev]
            topology = "sideways"

            [tool.fastware.dev.backend]
            cmd = ["x"]

            [tool.fastware.dev.vite]
            cmd = ["v"]
            port = 1234
            """
        )
    )
    monkeypatch.chdir(tmp_path)
    r = app.test(["dev", "run", "--no-daemon"])
    assert r.exit_code == 1
    assert "topology" in r.stderr
