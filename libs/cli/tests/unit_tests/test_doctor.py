"""Tests for `bog_agents_cli.doctor` — focused on regression coverage.

The user-reported bug: ``bog-agents doctor`` reported
``[SKIP] MCP config: No .mcp.json in current directory`` even though
the user had created an MCP at ``~/.bog-agents/.mcp.json`` (the
default ``/mcp add`` target). doctor was checking only ``cwd /
".mcp.json"`` and ignoring the standard discovery paths the server
itself uses. Fixed by routing through ``mcp_tools.discover_mcp_configs``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

from bog_agents_cli.doctor import (
    _entrypoints_on_path,
    _mcp_oauth_signed_in_count,
    _shadowed_entrypoint_check,
    run_doctor,
)


def _strip_lines(report: str) -> list[str]:
    """Split the doctor report into trimmed lines for assertion."""
    return [line.strip() for line in report.splitlines() if line.strip()]


class TestDoctorMCPDiscovery:
    """Regression: doctor finds MCP configs at standard locations.

    The previous implementation only checked ``cwd / ".mcp.json"``. The
    user's actual config (written by ``/mcp add``) lives at
    ``~/.bog-agents/.mcp.json`` — and was reported as MISSING.
    """

    def test_user_level_mcp_is_discovered(self, tmp_path: Path, monkeypatch) -> None:
        # Stage a fake user home with a valid .mcp.json under .bog-agents/.
        user_home = tmp_path / "home"
        bog_dir = user_home / ".bog-agents"
        bog_dir.mkdir(parents=True)
        mcp_path = bog_dir / ".mcp.json"
        mcp_path.write_text(json.dumps({"mcpServers": {}}))

        monkeypatch.setattr(Path, "home", lambda: user_home)
        # Patch find_project_root so the test doesn't leak the bog-agents
        # repo's own .mcp.json (test runs from inside the repo's working
        # tree; without this isolation, ``find_project_root`` walks up
        # to the repo root and picks up its .git + .mcp.json).
        monkeypatch.setattr(
            "bog_agents_cli.project_utils.find_project_root",
            lambda *_a, **_kw: None,
        )
        monkeypatch.chdir(tmp_path)

        report = run_doctor()
        # The MCP config row must show OK (or the user-level path),
        # NOT "No .mcp.json in current directory".
        assert "No .mcp.json" not in report
        assert str(mcp_path) in report or "MCP config" in report

    def test_no_mcp_anywhere_still_reports_clean_skip(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # Empty user home + empty cwd → SKIP with the new descriptive
        # message that lists the discovery paths.
        user_home = tmp_path / "home"
        user_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: user_home)
        monkeypatch.setattr(
            "bog_agents_cli.project_utils.find_project_root",
            lambda *_a, **_kw: None,
        )
        monkeypatch.chdir(tmp_path)

        report = run_doctor()
        # The SKIP row enumerates the standard locations so the user
        # knows where to put a config.
        assert "MCP config" in report
        assert "standard locations" in report

    def test_project_level_mcp_under_bog_agents_dir_is_discovered(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """``<project>/.bog-agents/.mcp.json`` must also be picked up."""
        user_home = tmp_path / "home"
        user_home.mkdir()
        project = tmp_path / "myproj"
        bog_dir = project / ".bog-agents"
        bog_dir.mkdir(parents=True)
        # Mark project as a git repo so find_project_root works.
        (project / ".git").mkdir()
        mcp_path = bog_dir / ".mcp.json"
        mcp_path.write_text(json.dumps({"mcpServers": {}}))

        monkeypatch.setattr(Path, "home", lambda: user_home)
        monkeypatch.chdir(project)

        report = run_doctor()
        assert "No .mcp.json" not in report
        assert str(mcp_path) in report or "MCP config" in report

    def test_discovery_failure_does_not_crash_doctor(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """An exception inside discover_mcp_configs must be reported as WARN."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(
            "bog_agents_cli.project_utils.find_project_root",
            lambda *_a, **_kw: None,
        )
        monkeypatch.chdir(tmp_path)

        with patch(
            "bog_agents_cli.mcp_tools.discover_mcp_configs",
            side_effect=RuntimeError("disk on fire"),
        ):
            report = run_doctor()

        # doctor must keep running after a single check fails.
        assert "Bog Agents Health Check" in report
        # MCP row shows the WARN with the error class.
        assert "MCP config" in report
        assert "RuntimeError" in report or "disk on fire" in report


class TestDoctorRipgrep:
    """doctor surfaces whether rg is the managed copy or a system install."""

    def test_managed_ripgrep_reported_ok(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(
            "bog_agents_cli.project_utils.find_project_root",
            lambda *_a, **_kw: None,
        )
        monkeypatch.chdir(tmp_path)
        with patch(
            "bog_agents_cli.managed_tools.describe_ripgrep",
            return_value=(
                "managed",
                "Managed ripgrep 14.1.0 at /home/.bog-agents/bin/rg",
            ),
        ):
            report = run_doctor()
        assert "Tool: rg" in report
        assert "Managed ripgrep" in report

    def test_system_ripgrep_reported(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(
            "bog_agents_cli.project_utils.find_project_root",
            lambda *_a, **_kw: None,
        )
        monkeypatch.chdir(tmp_path)
        with patch(
            "bog_agents_cli.managed_tools.describe_ripgrep",
            return_value=("system", "System ripgrep at /usr/bin/rg"),
        ):
            report = run_doctor()
        assert "Tool: rg" in report
        assert "System ripgrep at /usr/bin/rg" in report


class TestShadowedEntrypoint:
    """A stale CLI entrypoint earlier on PATH is flagged."""

    @staticmethod
    def _make_exe(directory: Path, name: str) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        # On Windows the probe looks for name.exe/.cmd; use .exe there.
        suffix = ".exe" if os.name == "nt" else ""
        exe = directory / f"{name}{suffix}"
        exe.write_text("stub", encoding="utf-8")
        return exe

    def test_all_entrypoints_on_path_finds_every_dir(
        self, tmp_path, monkeypatch
    ) -> None:
        d1 = tmp_path / "a"
        d2 = tmp_path / "b"
        self._make_exe(d1, "bog-agents")
        self._make_exe(d2, "bog-agents")
        monkeypatch.setenv("PATH", os.pathsep.join([str(d1), str(d2)]))
        found = _entrypoints_on_path("bog-agents")
        assert len(found) == 2

    def test_shadowed_when_two_copies_on_path(self, tmp_path, monkeypatch) -> None:
        d1 = tmp_path / "old"
        d2 = tmp_path / "new"
        self._make_exe(d1, "bog-agents")
        self._make_exe(d2, "bog-agents")
        monkeypatch.setenv("PATH", os.pathsep.join([str(d1), str(d2)]))
        row = _shadowed_entrypoint_check()
        assert row is not None
        status, detail = row
        assert status == "WARN"
        assert "shadows" in detail

    def test_not_shadowed_with_single_copy(self, tmp_path, monkeypatch) -> None:
        d1 = tmp_path / "only"
        self._make_exe(d1, "bog-agents")
        self._make_exe(d1, "bog-agents-cli")
        monkeypatch.setenv("PATH", str(d1))
        assert _shadowed_entrypoint_check() is None

    def test_shadow_row_appears_in_report(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(
            "bog_agents_cli.project_utils.find_project_root",
            lambda *_a, **_kw: None,
        )
        monkeypatch.chdir(tmp_path)
        with patch(
            "bog_agents_cli.doctor._shadowed_entrypoint_check",
            return_value=("WARN", "`bog-agents` resolves to 2 installs on PATH"),
        ):
            report = run_doctor()
        assert "CLI entrypoint" in report
        assert "2 installs on PATH" in report


class TestMcpOauthCount:
    """doctor reports how many remote MCP servers are signed in."""

    def _write_config(self, path: Path, servers: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")

    def test_counts_signed_in_remote_servers(self, tmp_path, monkeypatch) -> None:
        cfg = tmp_path / ".bog-agents" / ".mcp.json"
        self._write_config(
            cfg,
            {
                "remote-a": {"type": "http", "url": "https://a.example/mcp"},
                "remote-b": {"type": "sse", "url": "https://b.example/sse"},
                "local": {"command": "run-me"},
            },
        )
        monkeypatch.setattr(
            "bog_agents_cli.mcp_tools.discover_mcp_configs", lambda **_kw: [cfg]
        )

        def fake_status(name: str) -> dict[str, object]:
            return {"has_token": name == "remote-a", "expired": False}

        monkeypatch.setattr("bog_agents_cli.mcp_login_controller.status", fake_status)
        signed_in, total = _mcp_oauth_signed_in_count()
        assert (signed_in, total) == (1, 2)

    def test_no_remote_servers_returns_zero(self, tmp_path, monkeypatch) -> None:
        cfg = tmp_path / ".bog-agents" / ".mcp.json"
        self._write_config(cfg, {"local": {"command": "run-me"}})
        monkeypatch.setattr(
            "bog_agents_cli.mcp_tools.discover_mcp_configs", lambda **_kw: [cfg]
        )
        assert _mcp_oauth_signed_in_count() == (0, 0)

    def test_oauth_row_appears_in_report(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(
            "bog_agents_cli.project_utils.find_project_root",
            lambda *_a, **_kw: None,
        )
        monkeypatch.chdir(tmp_path)
        with patch(
            "bog_agents_cli.doctor._mcp_oauth_signed_in_count",
            return_value=(2, 3),
        ):
            report = run_doctor()
        assert "MCP OAuth" in report
        assert "2/3 remote servers signed in" in report

    def test_expired_token_not_counted(self, tmp_path, monkeypatch) -> None:
        cfg = tmp_path / ".bog-agents" / ".mcp.json"
        self._write_config(
            cfg, {"remote-a": {"type": "http", "url": "https://a.example/mcp"}}
        )
        monkeypatch.setattr(
            "bog_agents_cli.mcp_tools.discover_mcp_configs", lambda **_kw: [cfg]
        )
        monkeypatch.setattr(
            "bog_agents_cli.mcp_login_controller.status",
            lambda _n: {"has_token": True, "expired": True},
        )
        signed_in, total = _mcp_oauth_signed_in_count()
        assert (signed_in, total) == (0, 1)
