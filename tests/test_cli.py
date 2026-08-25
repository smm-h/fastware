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
    # strictcli resolves --dump-schema's destination ONCE, at App construction,
    # against the cwd that was live then -- a chdir at dispatch time no longer
    # redirects the write. `app` is constructed at import, so its destination is
    # the repo's own .strictcli/schema.json; point it at tmp_path instead rather
    # than chdir-ing and expecting the write to follow (which would otherwise
    # rewrite the committed schema from inside the suite). The project id is
    # still read from the pyproject in the cwd.
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "fastware"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(app, "_schema_out_path", str(tmp_path / ".strictcli" / "schema.json"))
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


def _capture_run_dev(monkeypatch) -> dict:
    """Replace supervise.run_dev with a recorder and return the captured kwargs."""
    import fastware.supervise

    seen: dict = {}

    def fake_run_dev(cfg, *, daemon, grace_s):
        seen["daemon"] = daemon
        seen["grace_s"] = grace_s
        return 0

    monkeypatch.setattr(fastware.supervise, "run_dev", fake_run_dev)
    return seen


def _write_run_config(tmp_path) -> None:
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
            app = "tests.dev_fixture_apps:api_only_app"
            port = {port}

            [tool.fastware.dev.vite]
            cmd = ["stub"]
            port = {vport}
            """
        )
    )


def test_run_without_daemon_flag_stays_in_the_foreground(tmp_path, monkeypatch):
    # --daemon and --grace declare presence="optional" (strictcli forbids a
    # value default= on a mutating command's flags), so absence arrives at the
    # handler as None and the handler applies the fallback its help states.
    # These are the fallbacks: foreground, and a 10-second grace period.
    _write_run_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    seen = _capture_run_dev(monkeypatch)
    r = app.test(["dev", "run"])
    assert r.exit_code == 0
    assert seen == {"daemon": False, "grace_s": 10.0}


def test_run_flag_absence_matches_explicit_negation(tmp_path, monkeypatch):
    _write_run_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    seen = _capture_run_dev(monkeypatch)
    assert app.test(["dev", "run", "--no-daemon", "--grace", "10"]).exit_code == 0
    assert seen == {"daemon": False, "grace_s": 10.0}


def test_run_daemon_flag_is_still_honored(tmp_path, monkeypatch):
    _write_run_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    seen = _capture_run_dev(monkeypatch)
    assert app.test(["dev", "run", "--daemon", "--grace", "3"]).exit_code == 0
    assert seen == {"daemon": True, "grace_s": 3.0}


def test_stop_grace_falls_back_to_ten_seconds(tmp_path, monkeypatch):
    import fastware.supervise

    _write_run_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    seen: dict = {}

    def fake_stop(cfg, *, grace_s):
        seen["grace_s"] = grace_s
        return []

    monkeypatch.setattr(fastware.supervise, "stop_dev_instances", fake_stop)
    assert app.test(["dev", "stop"]).exit_code == 0
    assert seen == {"grace_s": 10.0}
    assert app.test(["dev", "stop", "--grace", "2"]).exit_code == 0
    assert seen == {"grace_s": 2.0}


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
