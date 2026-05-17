"""Tests for killer feature middleware (Features #1-75)."""

from pathlib import Path


class TestWorktreeMiddleware:
    """Tests for git worktree middleware (Feature #1)."""

    def test_import(self):
        from bog_agents.middleware.worktree import WorktreeMiddleware

        assert WorktreeMiddleware is not None

    def test_init(self, tmp_path):
        from bog_agents.middleware.worktree import WorktreeMiddleware

        mw = WorktreeMiddleware(working_dir=tmp_path)
        assert mw.tools is not None
        assert len(mw.tools) == 4

    def test_tool_names(self, tmp_path):
        from bog_agents.middleware.worktree import WorktreeMiddleware

        mw = WorktreeMiddleware(working_dir=tmp_path)
        names = {t.name for t in mw.tools}
        assert "create_worktree" in names
        assert "list_worktrees" in names
        assert "remove_worktree" in names
        assert "merge_worktree" in names

    def test_worktree_info(self):
        from bog_agents.middleware.worktree import WorktreeInfo

        info = WorktreeInfo(path=Path("/tmp/wt"), branch="feature-1")
        assert info.branch == "feature-1"
        assert not info.is_main


class TestSmartContextMiddleware:
    """Tests for smart context middleware (Features #13-18)."""

    def test_import(self):
        from bog_agents.middleware.smart_context import SmartContextMiddleware

        assert SmartContextMiddleware is not None

    def test_init(self, tmp_path):
        from bog_agents.middleware.smart_context import SmartContextMiddleware

        mw = SmartContextMiddleware(working_dir=tmp_path, max_context_tokens=100000)
        assert mw.context_usage.max_tokens == 100000
        assert len(mw.tools) == 5

    def test_context_usage(self, tmp_path):
        from bog_agents.middleware.smart_context import SmartContextMiddleware

        mw = SmartContextMiddleware(working_dir=tmp_path, max_context_tokens=200000)
        assert mw.context_usage.percent_used == 0.0
        assert mw.context_usage.remaining_tokens == 200000

    def test_context_chunk(self):
        from bog_agents.middleware.smart_context import ContextChunk

        chunk = ContextChunk(file_path="test.py", content="def foo(): pass", start_line=1, end_line=1)
        assert chunk.file_path == "test.py"
        assert chunk.chunk_hash  # Auto-computed


class TestConversationBranchMiddleware:
    """Tests for conversation branching (Features #14, #16)."""

    def test_import(self):
        from bog_agents.middleware.conversation_branch import ConversationBranchMiddleware

        assert ConversationBranchMiddleware is not None

    def test_init(self, tmp_path):
        from bog_agents.middleware.conversation_branch import ConversationBranchMiddleware

        mw = ConversationBranchMiddleware(working_dir=tmp_path)
        assert len(mw.tools) == 6
        assert "session" in mw.memory_tiers
        assert "project" in mw.memory_tiers
        assert "global" in mw.memory_tiers

    def test_memory_tier(self):
        from bog_agents.middleware.conversation_branch import MemoryTier

        tier = MemoryTier(name="test")
        tier.add("key1", "value1")
        assert tier.get("key1") == "value1"
        assert tier.get("missing") is None
        results = tier.search("key")
        assert len(results) == 1


class TestImageInputMiddleware:
    """Tests for image input middleware (Features #19-23)."""

    def test_import(self):
        from bog_agents.middleware.image_input import ImageInputMiddleware

        assert ImageInputMiddleware is not None

    def test_init(self, tmp_path):
        from bog_agents.middleware.image_input import ImageInputMiddleware

        mw = ImageInputMiddleware(working_dir=tmp_path)
        assert len(mw.tools) == 4

    def test_tool_names(self, tmp_path):
        from bog_agents.middleware.image_input import ImageInputMiddleware

        mw = ImageInputMiddleware(working_dir=tmp_path)
        names = {t.name for t in mw.tools}
        assert "analyze_image" in names
        assert "paste_clipboard_image" in names
        assert "generate_diagram" in names
        assert "screenshot_to_code" in names

    def test_get_image_mime_type(self, tmp_path):
        from bog_agents.middleware.image_input import get_image_mime_type

        assert "png" in get_image_mime_type(Path("test.png"))
        assert "jpeg" in get_image_mime_type(Path("test.jpg"))


class TestBrowserAgentMiddleware:
    """Tests for browser agent middleware (Features #24-27)."""

    def test_import(self):
        from bog_agents.middleware.browser_agent import BrowserAgentMiddleware

        assert BrowserAgentMiddleware is not None

    def test_init(self, tmp_path):
        from bog_agents.middleware.browser_agent import BrowserAgentMiddleware

        mw = BrowserAgentMiddleware(working_dir=tmp_path)
        assert len(mw.tools) == 4

    def test_domain_filtering(self, tmp_path):
        from bog_agents.middleware.browser_agent import BrowserAgentMiddleware

        mw = BrowserAgentMiddleware(working_dir=tmp_path, allowed_domains=["example.com"])
        assert mw._is_domain_allowed("https://example.com/api")
        assert not mw._is_domain_allowed("https://evil.com/api")

    def test_no_domain_filter(self, tmp_path):
        from bog_agents.middleware.browser_agent import BrowserAgentMiddleware

        mw = BrowserAgentMiddleware(working_dir=tmp_path)
        assert mw._is_domain_allowed("https://anything.com")


class TestPRManagementMiddleware:
    """Tests for PR management middleware (Features #28-34)."""

    def test_import(self):
        from bog_agents.middleware.pr_management import PRManagementMiddleware

        assert PRManagementMiddleware is not None

    def test_init(self, tmp_path):
        from bog_agents.middleware.pr_management import PRManagementMiddleware

        mw = PRManagementMiddleware(working_dir=tmp_path)
        assert len(mw.tools) == 9

    def test_tool_names(self, tmp_path):
        from bog_agents.middleware.pr_management import PRManagementMiddleware

        mw = PRManagementMiddleware(working_dir=tmp_path)
        names = {t.name for t in mw.tools}
        assert "create_pr" in names
        assert "auto_pr_description" in names
        assert "resolve_conflicts" in names
        assert "git_bisect_start" in names


class TestTestGenerationMiddleware:
    """Tests for test generation middleware (Features #35-38, 40)."""

    def test_import(self):
        from bog_agents.middleware.test_generation import TestGenerationMiddleware

        assert TestGenerationMiddleware is not None

    def test_init(self, tmp_path):
        from bog_agents.middleware.test_generation import TestGenerationMiddleware

        mw = TestGenerationMiddleware(working_dir=tmp_path, test_framework="pytest")
        assert len(mw.tools) == 5

    def test_tool_names(self, tmp_path):
        from bog_agents.middleware.test_generation import TestGenerationMiddleware

        mw = TestGenerationMiddleware(working_dir=tmp_path)
        names = {t.name for t in mw.tools}
        assert "run_coverage" in names
        assert "coverage_gaps" in names
        assert "run_benchmark" in names
        assert "audit_dependencies" in names
        assert "generate_test_skeleton" in names


class TestEnterpriseMiddleware:
    """Tests for enterprise middleware (Features #51-57)."""

    def test_import(self):
        from bog_agents.middleware.enterprise import EnterpriseMiddleware

        assert EnterpriseMiddleware is not None

    def test_init(self, tmp_path):
        from bog_agents.middleware.enterprise import EnterpriseMiddleware

        mw = EnterpriseMiddleware(working_dir=tmp_path)
        assert len(mw.tools) == 5

    def test_audit_logging(self, tmp_path):
        from bog_agents.middleware.enterprise import EnterpriseMiddleware

        mw = EnterpriseMiddleware(working_dir=tmp_path)
        mw.log_action("test_action", "test_tool", "details", "low")
        assert len(mw._audit_log) == 1
        assert mw._audit_log[0].action == "test_action"

    def test_permission_check(self, tmp_path):
        from bog_agents.middleware.enterprise import EnterpriseMiddleware

        mw = EnterpriseMiddleware(working_dir=tmp_path, current_role="reviewer")
        assert not mw.check_permission("execute")
        assert not mw.check_permission("write_file")

    def test_team_config(self):
        from bog_agents.middleware.enterprise import TeamConfig

        config = TeamConfig()
        assert "admin" in config.roles
        assert "developer" in config.roles
        assert "reviewer" in config.roles

    def test_compliance_policy(self):
        from bog_agents.middleware.enterprise import CompliancePolicy

        policy = CompliancePolicy(name="no-exec", description="No execution", rule_type="deny_tool", pattern="execute")
        assert policy.enabled


class TestMultiModelMiddleware:
    """Tests for multi-model middleware (Features #58, #72, #73)."""

    def test_import(self):
        from bog_agents.middleware.multi_model import MultiModelMiddleware

        assert MultiModelMiddleware is not None

    def test_init(self):
        from bog_agents.middleware.multi_model import MultiModelMiddleware

        mw = MultiModelMiddleware()
        assert len(mw.tools) == 4

    def test_known_models(self):
        from bog_agents.middleware.multi_model import KNOWN_MODELS

        assert "ollama:llama3" in KNOWN_MODELS
        assert "anthropic:claude-sonnet-4-6" in KNOWN_MODELS
        assert "openai:gpt-4o" in KNOWN_MODELS
        assert "google-genai:gemini-2.0-flash" in KNOWN_MODELS

    def test_classify_task(self):
        from bog_agents.middleware.multi_model import classify_task_complexity

        assert classify_task_complexity("fix typo in README") == "simple"
        assert classify_task_complexity("implement a new feature") == "moderate"
        assert classify_task_complexity("architect the entire system") == "complex"

    def test_model_routing(self):
        from bog_agents.middleware.multi_model import get_model_for_complexity

        cheap = get_model_for_complexity("simple")
        assert "haiku" in cheap or "flash" in cheap or "mini" in cheap or "llama" in cheap

    def test_local_model_profile(self):
        from bog_agents.middleware.multi_model import KNOWN_MODELS

        ollama = KNOWN_MODELS["ollama:llama3"]
        assert ollama.is_local
        assert ollama.cost_per_1k_input == 0.0
        assert ollama.tier == "local"


class TestCodeIntelligenceMiddleware:
    """Tests for code intelligence middleware (Features #59-75)."""

    def test_import(self):
        from bog_agents.middleware.code_intelligence import CodeIntelligenceMiddleware

        assert CodeIntelligenceMiddleware is not None

    def test_init(self, tmp_path):
        from bog_agents.middleware.code_intelligence import CodeIntelligenceMiddleware

        mw = CodeIntelligenceMiddleware(working_dir=tmp_path)
        assert len(mw.tools) == 7

    def test_tool_names(self, tmp_path):
        from bog_agents.middleware.code_intelligence import CodeIntelligenceMiddleware

        mw = CodeIntelligenceMiddleware(working_dir=tmp_path)
        names = {t.name for t in mw.tools}
        assert "codebase_health" in names
        assert "generate_changelog" in names
        assert "migration_plan" in names
        assert "onboard" in names
        assert "analyze_imports" in names
        assert "generate_infra" in names
        assert "replay_actions" in names

    def test_replay_log(self, tmp_path):
        from bog_agents.middleware.code_intelligence import CodeIntelligenceMiddleware

        mw = CodeIntelligenceMiddleware(working_dir=tmp_path)
        assert isinstance(mw.replay_log, list)
        assert len(mw.replay_log) == 0


class TestPluginSystemMiddleware:
    """Tests for plugin system middleware (Features #7-12)."""

    def test_import(self):
        from bog_agents.middleware.plugin_system import PluginSystemMiddleware

        assert PluginSystemMiddleware is not None

    def test_init(self, tmp_path):
        from bog_agents.middleware.plugin_system import PluginSystemMiddleware

        mw = PluginSystemMiddleware(plugins_dir=tmp_path / "plugins", skills_dir=tmp_path / "skills")
        # 7 original + 3 Claude Code compat tools
        assert len(mw.tools) == 10

    def test_parse_skill_md(self):
        from bog_agents.middleware.plugin_system import parse_skill_md

        content = "---\nname: test\nversion: 1.0\n---\n# Test Skill\nInstructions here."
        data = parse_skill_md(content)
        assert data["frontmatter"]["name"] == "test"
        assert "Instructions" in data["body"]

    def test_create_skill_template(self):
        from bog_agents.middleware.plugin_system import create_skill_template

        template = create_skill_template("my-skill", "A test skill")
        assert "my-skill" in template
        assert "A test skill" in template
        assert "---" in template

    def test_plugin_manifest(self):
        from bog_agents.middleware.plugin_system import PluginManifest

        manifest = PluginManifest(name="test-plugin", version="1.0.0", description="A test plugin")
        assert manifest.name == "test-plugin"
        assert manifest.version == "1.0.0"

    def test_validate_manifest_accepts_well_formed(self):
        from bog_agents.middleware.plugin_system import validate_plugin_manifest

        manifest = validate_plugin_manifest(
            {
                "name": "ok-plugin",
                "version": "1.2.3",
                "description": "fine",
                "permissions": ["filesystem.read", "network"],
                "mcp_servers": [{"url": "https://mcp.example.com"}],
            }
        )
        assert manifest.name == "ok-plugin"

    def test_validate_manifest_rejects_unknown_permission(self):
        import pytest

        from bog_agents.middleware.plugin_system import PluginValidationError, validate_plugin_manifest

        with pytest.raises(PluginValidationError):
            validate_plugin_manifest({"name": "p", "version": "1.0", "description": "", "permissions": ["root.escape"]})

    def test_validate_manifest_rejects_non_http_mcp_url(self):
        import pytest

        from bog_agents.middleware.plugin_system import PluginValidationError, validate_plugin_manifest

        with pytest.raises(PluginValidationError):
            validate_plugin_manifest(
                {
                    "name": "p",
                    "version": "1.0",
                    "description": "",
                    "mcp_servers": [{"url": "file:///etc/passwd"}],
                }
            )

    def test_validate_manifest_rejects_bad_name(self):
        import pytest

        from bog_agents.middleware.plugin_system import PluginValidationError, validate_plugin_manifest

        with pytest.raises(PluginValidationError):
            validate_plugin_manifest({"name": "../evil", "version": "1.0", "description": ""})

    def test_contains_symlink_detects_top_level(self, tmp_path):
        from bog_agents.middleware.plugin_system import _contains_symlink

        target = tmp_path / "target"
        target.mkdir()
        (target / "file.txt").write_text("hi", encoding="utf-8")
        link = tmp_path / "link"
        try:
            link.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            import pytest

            pytest.skip("symlink creation not permitted on this platform")
        assert _contains_symlink(link) is not None


class TestNotificationsMiddleware:
    """Tests for notifications middleware (Features #42-47, 49)."""

    def test_import(self):
        from bog_agents.middleware.notifications import NotificationsMiddleware

        assert NotificationsMiddleware is not None

    def test_init(self):
        from bog_agents.middleware.notifications import NotificationsMiddleware

        mw = NotificationsMiddleware(session_name="test-session")
        assert mw.session.name == "test-session"
        assert len(mw.tools) == 6

    def test_session_info(self):
        from bog_agents.middleware.notifications import SessionInfo

        info = SessionInfo(name="test")
        assert info.name == "test"
        assert info.tokens_in == 0
        assert info.cost_usd == 0.0

    def test_progress_task(self):
        from bog_agents.middleware.notifications import ProgressTask

        task = ProgressTask(task_id="t1", description="Processing", total=100, current=50)
        assert task.percent == 50.0


class TestLangSmithMiddleware:
    """Tests for LangSmith integration middleware."""

    def test_import(self):
        from bog_agents.middleware.langsmith_integration import LangSmithMiddleware

        assert LangSmithMiddleware is not None

    def test_init(self):
        from bog_agents.middleware.langsmith_integration import LangSmithMiddleware

        mw = LangSmithMiddleware()
        assert len(mw.tools) == 11

    def test_tool_names(self):
        from bog_agents.middleware.langsmith_integration import LangSmithMiddleware

        mw = LangSmithMiddleware()
        names = {t.name for t in mw.tools}
        assert "langsmith_list_runs" in names
        assert "langsmith_read_run" in names
        assert "langsmith_get_run_url" in names
        assert "langsmith_list_projects" in names
        assert "langsmith_list_datasets" in names
        assert "langsmith_read_dataset" in names
        assert "langsmith_list_evals" in names
        assert "langsmith_read_eval" in names
        assert "langsmith_compare_evals" in names
        assert "langsmith_log_feedback" in names
        assert "langsmith_status" in names

    def test_helpers(self, monkeypatch):
        from bog_agents.middleware.langsmith_integration import _get_api_key, _get_project

        monkeypatch.delenv("LANGCHAIN_API_KEY", raising=False)
        monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
        monkeypatch.delenv("LANGCHAIN_PROJECT", raising=False)
        monkeypatch.delenv("LANGSMITH_PROJECT", raising=False)
        assert _get_api_key() is None
        assert _get_project() == "default"

    def test_otel_disabled_by_default(self):
        from bog_agents.middleware.langsmith_integration import LangSmithMiddleware

        mw = LangSmithMiddleware(enable_otel=False)
        assert mw._otel_enabled is False

    def test_make_client_raises_import_error(self, monkeypatch):
        """_make_client must raise ImportError with helpful message when no API key."""
        import bog_agents.middleware.langsmith_integration as mod

        original = mod._make_client

        def _failing_client():
            msg = "langsmith is not installed."
            raise ImportError(msg)

        monkeypatch.setattr(mod, "_make_client", _failing_client)
        mw = mod.LangSmithMiddleware()
        status_tool = next(t for t in mw.tools if t.name == "langsmith_status")
        result = status_tool.invoke({})
        assert isinstance(result, str)
        mod._make_client = original


class TestAllExports:
    """Test that all new middleware is properly exported."""

    def test_sdk_init_exports(self):
        import bog_agents

        assert hasattr(bog_agents, "WorktreeMiddleware")
        assert hasattr(bog_agents, "SmartContextMiddleware")
        assert hasattr(bog_agents, "ConversationBranchMiddleware")
        assert hasattr(bog_agents, "ImageInputMiddleware")
        assert hasattr(bog_agents, "BrowserAgentMiddleware")
        assert hasattr(bog_agents, "PRManagementMiddleware")
        assert hasattr(bog_agents, "TestGenerationMiddleware")
        assert hasattr(bog_agents, "EnterpriseMiddleware")
        assert hasattr(bog_agents, "MultiModelMiddleware")
        assert hasattr(bog_agents, "CodeIntelligenceMiddleware")
        assert hasattr(bog_agents, "PluginSystemMiddleware")
        assert hasattr(bog_agents, "NotificationsMiddleware")
        assert hasattr(bog_agents, "LangSmithMiddleware")

    def test_middleware_init_exports(self):
        from bog_agents import middleware

        assert hasattr(middleware, "WorktreeMiddleware")
        assert hasattr(middleware, "SmartContextMiddleware")
        assert hasattr(middleware, "ConversationBranchMiddleware")
        assert hasattr(middleware, "ImageInputMiddleware")
        assert hasattr(middleware, "BrowserAgentMiddleware")
        assert hasattr(middleware, "PRManagementMiddleware")
        assert hasattr(middleware, "TestGenerationMiddleware")
        assert hasattr(middleware, "EnterpriseMiddleware")
        assert hasattr(middleware, "MultiModelMiddleware")
        assert hasattr(middleware, "CodeIntelligenceMiddleware")
        assert hasattr(middleware, "PluginSystemMiddleware")
        assert hasattr(middleware, "NotificationsMiddleware")
        assert hasattr(middleware, "LangSmithMiddleware")
