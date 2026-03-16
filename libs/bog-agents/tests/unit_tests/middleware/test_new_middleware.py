"""Tests for new middleware modules.

Tests for features: #1, #3, #8, #9, #11, #13, #15, #16, #23, #35, #37, #38, #48.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


class TestGitToolsMiddleware:
    """Tests for GitToolsMiddleware (#15, #43)."""

    def test_init(self, tmp_path: Path) -> None:
        """Test middleware initialization."""
        from bog_agents.middleware.git_tools import GitToolsMiddleware

        mw = GitToolsMiddleware(working_dir=tmp_path)
        assert len(mw.tools) > 0

    def test_tool_names(self, tmp_path: Path) -> None:
        """Test that expected tools are registered."""
        from bog_agents.middleware.git_tools import GitToolsMiddleware

        mw = GitToolsMiddleware(working_dir=tmp_path)
        names = {t.name for t in mw.tools}
        assert "git_status" in names
        assert "git_diff" in names
        assert "git_log" in names
        assert "git_commit" in names


class TestRepoMapMiddleware:
    """Tests for RepoMapMiddleware (#13)."""

    def test_init(self, tmp_path: Path) -> None:
        """Test middleware initialization."""
        from bog_agents.middleware.repo_map import RepoMapMiddleware

        mw = RepoMapMiddleware(working_dir=tmp_path)
        assert len(mw.tools) > 0

    def test_tool_name(self, tmp_path: Path) -> None:
        """Test that repo_map tool is registered."""
        from bog_agents.middleware.repo_map import RepoMapMiddleware

        mw = RepoMapMiddleware(working_dir=tmp_path)
        names = {t.name for t in mw.tools}
        assert "repo_map" in names


class TestCheckpointingMiddleware:
    """Tests for CheckpointingMiddleware (#3, #5, #39, #43)."""

    def test_init(self, tmp_path: Path) -> None:
        """Test middleware initialization."""
        from bog_agents.middleware.checkpointing import CheckpointingMiddleware

        mw = CheckpointingMiddleware(working_dir=tmp_path)
        assert len(mw.get_tools()) > 0

    def test_tool_names(self, tmp_path: Path) -> None:
        """Test expected tools."""
        from bog_agents.middleware.checkpointing import CheckpointingMiddleware

        mw = CheckpointingMiddleware(working_dir=tmp_path)
        names = {t.name for t in mw.get_tools()}
        assert "show_diff" in names
        assert "undo_last_change" in names


class TestCostTrackerMiddleware:
    """Tests for CostTrackerMiddleware (#8, #34, #36, #47)."""

    def test_init(self) -> None:
        """Test middleware initialization."""
        from bog_agents.middleware.cost_tracker import CostTrackerMiddleware

        mw = CostTrackerMiddleware()
        assert len(mw.tools) > 0

    def test_tool_names(self) -> None:
        """Test expected tools."""
        from bog_agents.middleware.cost_tracker import CostTrackerMiddleware

        mw = CostTrackerMiddleware()
        names = {t.name for t in mw.tools}
        assert "show_cost" in names
        assert "set_budget" in names
        assert "set_effort" in names

    def test_effort_level(self) -> None:
        """Test effort level setting."""
        from bog_agents.middleware.cost_tracker import CostTrackerMiddleware

        mw = CostTrackerMiddleware(effort_level="low")
        assert mw._effort_level == "low"


class TestPlanModeMiddleware:
    """Tests for PlanModeMiddleware (#38)."""

    def test_init(self) -> None:
        """Test middleware initialization."""
        from bog_agents.middleware.plan_mode import PlanModeMiddleware

        mw = PlanModeMiddleware()
        assert len(mw.tools) > 0

    def test_tool_name(self) -> None:
        """Test that toggle_plan_mode tool is registered."""
        from bog_agents.middleware.plan_mode import PlanModeMiddleware

        mw = PlanModeMiddleware()
        names = {t.name for t in mw.tools}
        assert "toggle_plan_mode" in names


class TestAutoQualityMiddleware:
    """Tests for AutoQualityMiddleware (#11, #12, #44)."""

    def test_init(self, tmp_path: Path) -> None:
        """Test middleware initialization."""
        from bog_agents.middleware.auto_quality import AutoQualityMiddleware

        mw = AutoQualityMiddleware(working_dir=tmp_path)
        assert len(mw.tools) > 0

    def test_detect_project(self, tmp_path: Path) -> None:
        """Test project detection for Python."""
        from bog_agents.middleware.auto_quality import detect_project

        (tmp_path / "pyproject.toml").write_text("[project]")
        info = detect_project(tmp_path)
        assert info.language == "python"


class TestArchitectMiddleware:
    """Tests for ArchitectMiddleware (#16, #41)."""

    def test_init(self) -> None:
        """Test middleware initialization."""
        from bog_agents.middleware.architect import ArchitectMiddleware

        mw = ArchitectMiddleware()
        assert len(mw.tools) > 0

    def test_consult_tool(self) -> None:
        """Test that consult_model tool is always available."""
        from bog_agents.middleware.architect import ArchitectMiddleware

        mw = ArchitectMiddleware()
        names = {t.name for t in mw.tools}
        assert "consult_model" in names


class TestSafeToolsConfig:
    """Tests for SafeToolsConfig (#37)."""

    def test_is_tool_safe_no_rules(self) -> None:
        """Test that tools are unsafe by default when no rules match."""
        from bog_agents.middleware.safe_tools import SafeToolsConfig, is_tool_safe

        config = SafeToolsConfig(rules=[])
        assert not is_tool_safe("execute", {}, config)

    def test_is_tool_safe_with_rule(self) -> None:
        """Test that tools matching rules are safe."""
        from bog_agents.middleware.safe_tools import SafeToolRule, SafeToolsConfig, is_tool_safe

        config = SafeToolsConfig(
            rules=[
                SafeToolRule(tool_name="read_file"),
            ]
        )
        assert is_tool_safe("read_file", {}, config)


class TestParallelAgentsMiddleware:
    """Tests for ParallelAgentsMiddleware (#23)."""

    def test_init(self) -> None:
        """Test middleware initialization."""
        from bog_agents.middleware.parallel_agents import ParallelAgentsMiddleware

        mw = ParallelAgentsMiddleware()
        assert len(mw.tools) == 1
        assert mw.tools[0].name == "parallel_tasks"


class TestContextPackingMiddleware:
    """Tests for ContextPackingMiddleware (#48)."""

    def test_init(self) -> None:
        """Test middleware initialization."""
        from bog_agents.middleware.context_packing import ContextPackingMiddleware

        mw = ContextPackingMiddleware()
        assert mw is not None


class TestPdfReader:
    """Tests for PDF reader (#9)."""

    def test_import(self) -> None:
        """Test that PDF reader module can be imported."""
        from bog_agents.middleware.pdf_reader import read_pdf

        assert callable(read_pdf)


class TestSandbox:
    """Tests for OS-level sandbox (#2)."""

    def test_sandbox_level_enum(self) -> None:
        """Test SandboxLevel enum values."""
        from bog_agents.sandbox.local_sandbox import SandboxLevel

        assert SandboxLevel.DISABLED == "disabled"
        assert SandboxLevel.READ_ONLY == "read-only"
        assert SandboxLevel.WORKSPACE_WRITE == "workspace-write"

    def test_platform_detection(self) -> None:
        """Test platform sandbox support detection."""
        from bog_agents.sandbox.local_sandbox import get_platform_sandbox_support

        support = get_platform_sandbox_support()
        assert hasattr(support, "platform")
        assert support.platform in ("linux", "darwin", "windows", "unknown")
