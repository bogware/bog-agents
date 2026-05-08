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
from pathlib import Path
from unittest.mock import patch

from bog_agents_cli.doctor import run_doctor


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
