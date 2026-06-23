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

    @staticmethod
    def _make_request(messages: list) -> object:  # type: ignore[type-arg]
        from langchain_core.messages import SystemMessage

        try:
            from langchain.agents.middleware.types import ModelRequest
        except ImportError:  # pragma: no cover - import-path fallback
            from langchain.agents.middleware import ModelRequest  # type: ignore[no-redef,attr-defined]

        return ModelRequest(
            model=object(),
            messages=messages,
            system_message=SystemMessage(content="base"),
            tools=[],
            runtime=None,
            state={"messages": messages},
        )

    def test_pack_uses_override_and_does_not_mutate_request(self) -> None:
        """Packing must produce a new request via override, not mutate request.messages."""
        from langchain_core.messages import AIMessage, HumanMessage

        from bog_agents.middleware.context_packing import ContextPackingMiddleware

        # 30 large messages so the estimate clears the (tiny) threshold and len > 10.
        big = "x" * 4000
        messages = [HumanMessage(content=big) if i % 2 == 0 else AIMessage(content=big) for i in range(30)]
        request = self._make_request(messages)
        original_messages = request.messages

        mw = ContextPackingMiddleware(context_window=1000, threshold_pct=0.1)
        new_request = mw._maybe_pack(request)

        # Original request untouched (no in-place mutation).
        assert request.messages is original_messages
        assert len(request.messages) == 30
        # New request is packed and shorter.
        assert new_request is not request
        assert len(new_request.messages) < 30

    def test_pack_does_not_orphan_tool_result(self) -> None:
        """The kept tail must never begin on an orphaned ToolMessage."""
        from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

        from bog_agents.middleware.context_packing import ContextPackingMiddleware

        big = "y" * 4000
        messages: list = []
        for i in range(12):
            messages.append(HumanMessage(content=big))
            messages.append(AIMessage(content=big, tool_calls=[{"name": "t", "args": {}, "id": f"id{i}"}]))
            messages.append(ToolMessage(content=big, tool_call_id=f"id{i}", name="t"))

        request = self._make_request(messages)
        mw = ContextPackingMiddleware(context_window=1000, threshold_pct=0.1)
        new_request = mw._maybe_pack(request)

        # First message is the packed system summary; the message right after must not be
        # a ToolMessage (which would be an orphaned tool_result).
        assert len(new_request.messages) >= 2
        assert not isinstance(new_request.messages[1], ToolMessage)

    def test_pack_passes_through_on_failure(self) -> None:
        """A failure inside packing must return the original request unchanged."""
        from langchain_core.messages import AIMessage, HumanMessage

        from bog_agents.middleware import context_packing
        from bog_agents.middleware.context_packing import ContextPackingMiddleware

        big = "z" * 4000
        messages = [HumanMessage(content=big) if i % 2 == 0 else AIMessage(content=big) for i in range(30)]
        request = self._make_request(messages)

        def _boom(*_args: object, **_kwargs: object) -> str:
            raise RuntimeError("pack exploded")

        original = context_packing.pack_messages
        context_packing.pack_messages = _boom  # type: ignore[assignment]
        try:
            mw = ContextPackingMiddleware(context_window=1000, threshold_pct=0.1)
            result = mw._maybe_pack(request)
        finally:
            context_packing.pack_messages = original  # type: ignore[assignment]

        assert result is request


class TestPdfReader:
    """Tests for PDF reader (#9)."""

    def test_import(self) -> None:
        """Test that PDF reader module can be imported."""
        from bog_agents.middleware.pdf_reader import read_pdf

        assert callable(read_pdf)

    def test_is_pdf_file(self) -> None:
        from bog_agents.middleware.pdf_reader import is_pdf_file

        assert is_pdf_file("/docs/report.pdf") is True
        assert is_pdf_file("/docs/REPORT.PDF") is True
        assert is_pdf_file("/docs/notes.txt") is False

    def test_read_pdf_from_bytes_roundtrip(self) -> None:
        """read_pdf parses raw bytes (the path used by read_file's backend)."""
        import io

        import pytest

        pypdf = pytest.importorskip("pypdf")

        writer = pypdf.PdfWriter()
        writer.add_blank_page(width=72, height=72)
        buf = io.BytesIO()
        writer.write(buf)

        from bog_agents.middleware.pdf_reader import read_pdf

        result = read_pdf("/tmp/blank.pdf", data=buf.getvalue())
        assert "/tmp/blank.pdf" in result
        assert "Page 1" in result
        assert "Error" not in result

    def test_read_file_routes_pdf_to_read_pdf(self, monkeypatch) -> None:
        """The read_file tool downloads PDF bytes and hands them to read_pdf."""
        from types import SimpleNamespace

        from bog_agents.middleware import pdf_reader
        from bog_agents.middleware.filesystem import FilesystemMiddleware

        mw = FilesystemMiddleware()
        read_tool = next(t for t in mw.tools if t.name == "read_file")

        fake_backend = SimpleNamespace(download_files=lambda paths: [SimpleNamespace(content=b"%PDF-1.4 bytes", error=None)])
        monkeypatch.setattr(mw, "_get_backend", lambda runtime: fake_backend)
        monkeypatch.setattr(
            pdf_reader,
            "read_pdf",
            lambda path, *, data, start_page=0: f"PARSED {len(data)} bytes from {path}",
        )

        result = read_tool.func(
            file_path="/doc.pdf",
            runtime=SimpleNamespace(tool_call_id="t1"),
        )
        assert result == "PARSED 14 bytes from /doc.pdf"


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
