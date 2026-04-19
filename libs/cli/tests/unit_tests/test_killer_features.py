"""Tests for killer feature CLI modules (Features #1-75)."""

from pathlib import Path

import pytest


class TestMultiAgentCLI:
    """Tests for multi-agent CLI module."""

    def test_import(self):
        from bog_agents_cli.multi_agent import (
            ThreadInfo,
            format_thread_list,
            parse_agent_command,
        )

        assert parse_agent_command is not None
        assert format_thread_list is not None

    def test_parse_agent_command_list(self):
        from bog_agents_cli.multi_agent import parse_agent_command

        result = parse_agent_command("list")
        assert result["action"] == "list"

    def test_parse_agent_command_spawn(self):
        from bog_agents_cli.multi_agent import parse_agent_command

        result = parse_agent_command("spawn implement auth feature")
        assert result["action"] == "spawn"
        assert "auth" in result["argument"]

    def test_format_empty_threads(self):
        from bog_agents_cli.multi_agent import format_thread_list

        assert "No active" in format_thread_list([])

    def test_format_threads(self):
        from bog_agents_cli.multi_agent import ThreadInfo, format_thread_list

        threads = [
            ThreadInfo(
                thread_id="t1", label="auth-work", status="running", task="Fix auth"
            )
        ]
        result = format_thread_list(threads)
        assert "auth-work" in result
        assert "running" in result


class TestPluginMarketplaceCLI:
    """Tests for plugin marketplace CLI module."""

    def test_import(self):
        from bog_agents_cli.plugin_marketplace import (
            create_skill_file,
            list_installed_plugins,
        )

        assert list_installed_plugins is not None

    def test_list_empty_plugins(self, tmp_path):
        from bog_agents_cli.plugin_marketplace import list_installed_plugins

        plugins = list_installed_plugins(tmp_path)
        assert plugins == []

    def test_create_skill(self, tmp_path):
        from bog_agents_cli.plugin_marketplace import create_skill_file

        path = create_skill_file("test-skill", "A test skill", skills_dir=tmp_path)
        assert path.exists()
        content = path.read_text()
        assert "test-skill" in content
        assert "A test skill" in content

    def test_format_plugin_list_empty(self):
        from bog_agents_cli.plugin_marketplace import format_plugin_list

        assert "No plugins" in format_plugin_list([])

    def test_format_plugin_list(self):
        from bog_agents_cli.plugin_marketplace import PluginInfo, format_plugin_list

        plugins = [PluginInfo(name="test", version="1.0", description="A test plugin")]
        result = format_plugin_list(plugins)
        assert "test" in result
        assert "1.0" in result

    def test_uninstall_missing(self, tmp_path):
        from bog_agents_cli.plugin_marketplace import uninstall_plugin

        result = uninstall_plugin("nonexistent", plugins_dir=tmp_path)
        assert "not found" in result


class TestSmartContextCLI:
    """Tests for smart context CLI module."""

    def test_import(self):
        from bog_agents_cli.smart_context_cli import (
            ContextInfo,
            format_context_bar,
            parse_context_command,
        )

        assert ContextInfo is not None

    def test_context_info(self):
        from bog_agents_cli.smart_context_cli import ContextInfo

        info = ContextInfo(max_tokens=200000, used_tokens=50000)
        assert info.percent_used == 25.0
        assert info.remaining == 150000

    def test_format_context_bar(self):
        from bog_agents_cli.smart_context_cli import ContextInfo, format_context_bar

        info = ContextInfo(max_tokens=200000, used_tokens=100000)
        bar = format_context_bar(info)
        assert "50.0%" in bar
        assert "█" in bar

    def test_parse_context_command(self):
        from bog_agents_cli.smart_context_cli import parse_context_command

        result = parse_context_command("breakdown")
        assert result["action"] == "breakdown"


class TestImageCLI:
    """Tests for image CLI module."""

    def test_import(self):
        from bog_agents_cli.image_cli import (
            detect_image_in_input,
            is_image_file,
            parse_image_command,
        )

        assert is_image_file is not None

    def test_is_image_file(self):
        from bog_agents_cli.image_cli import is_image_file

        assert is_image_file("test.png")
        assert is_image_file("test.jpg")
        assert is_image_file("test.jpeg")
        assert not is_image_file("test.py")
        assert not is_image_file("test.txt")

    def test_parse_image_command(self):
        from bog_agents_cli.image_cli import parse_image_command

        result = parse_image_command("analyze test.png")
        assert result["action"] == "analyze"
        assert result["arg1"] == "test.png"


class TestBrowserCLI:
    """Tests for browser CLI module."""

    def test_import(self):
        from bog_agents_cli.browser_cli import (
            APIRequest,
            format_api_response,
            parse_api_command,
        )

        assert parse_api_command is not None

    def test_parse_api_get(self):
        from bog_agents_cli.browser_cli import parse_api_command

        req = parse_api_command("GET https://api.example.com/users")
        assert req.method == "GET"
        assert req.url == "https://api.example.com/users"

    def test_parse_api_post(self):
        from bog_agents_cli.browser_cli import parse_api_command

        req = parse_api_command(
            'POST https://api.example.com/users -d \'{"name": "test"}\''
        )
        assert req.method == "POST"
        assert req.body

    def test_parse_preview_command(self):
        from bog_agents_cli.browser_cli import parse_preview_command

        result = parse_preview_command("start npm run dev")
        assert result["action"] == "start"


class TestPRCLI:
    """Tests for PR management CLI module."""

    def test_import(self):
        from bog_agents_cli.pr_cli import PRInfo, generate_pr_prompt, parse_pr_command

        assert parse_pr_command is not None

    def test_parse_pr_create(self):
        from bog_agents_cli.pr_cli import parse_pr_command

        result = parse_pr_command("create Add auth feature")
        assert result["action"] == "create"
        assert "auth" in result["argument"]

    def test_generate_pr_prompt(self):
        from bog_agents_cli.pr_cli import PRInfo, generate_pr_prompt

        info = PRInfo(number=0, title="Add auth", base_branch="main")
        prompt = generate_pr_prompt(info)
        assert "Add auth" in prompt
        assert "main" in prompt

    def test_conflict_resolution_prompt(self):
        from bog_agents_cli.pr_cli import generate_conflict_resolution_prompt

        prompt = generate_conflict_resolution_prompt()
        assert "conflict" in prompt.lower()

    def test_bisect_prompt(self):
        from bog_agents_cli.pr_cli import generate_bisect_prompt

        prompt = generate_bisect_prompt("HEAD", "abc123", "pytest")
        assert "HEAD" in prompt
        assert "abc123" in prompt


class TestTestToolsCLI:
    """Tests for test tools CLI module."""

    def test_import(self):
        from bog_agents_cli.test_tools_cli import (
            generate_test_prompt,
            parse_test_command,
        )

        assert parse_test_command is not None

    def test_parse_test_generate(self):
        from bog_agents_cli.test_tools_cli import parse_test_command

        result = parse_test_command("generate src/auth.py")
        assert result["action"] == "generate"
        assert "auth" in result["argument"]

    def test_generate_test_prompt(self):
        from bog_agents_cli.test_tools_cli import generate_test_prompt

        prompt = generate_test_prompt("src/auth.py", "pytest")
        assert "auth.py" in prompt
        assert "pytest" in prompt

    def test_audit_prompt(self):
        from bog_agents_cli.test_tools_cli import generate_audit_prompt

        prompt = generate_audit_prompt()
        assert "vulnerabilities" in prompt.lower()


class TestEnterpriseCLI:
    """Tests for enterprise CLI module."""

    def test_import(self):
        from bog_agents_cli.enterprise_cli import (
            TeamSettings,
            format_team_settings,
            parse_team_command,
        )

        assert TeamSettings is not None

    def test_team_settings_default(self):
        from bog_agents_cli.enterprise_cli import TeamSettings

        settings = TeamSettings()
        assert settings.name == ""
        assert settings.members == []

    def test_parse_team_command(self):
        from bog_agents_cli.enterprise_cli import parse_team_command

        result = parse_team_command("roles")
        assert result["action"] == "roles"

    def test_format_team_settings(self):
        from bog_agents_cli.enterprise_cli import TeamSettings, format_team_settings

        settings = TeamSettings(name="My Team")
        result = format_team_settings(settings)
        assert "My Team" in result

    def test_load_missing_config(self, tmp_path):
        from bog_agents_cli.enterprise_cli import load_team_settings

        settings = load_team_settings(tmp_path / "nonexistent.json")
        assert settings.name == ""

    def test_save_and_load_config(self, tmp_path):
        from bog_agents_cli.enterprise_cli import (
            TeamMember,
            TeamSettings,
            load_team_settings,
            save_team_settings,
        )

        config_path = tmp_path / "team.json"
        settings = TeamSettings(
            name="Test Team", members=[TeamMember(name="Alice", role="admin")]
        )
        save_team_settings(settings, config_path)
        loaded = load_team_settings(config_path)
        assert loaded.name == "Test Team"
        assert len(loaded.members) == 1
        assert loaded.members[0].name == "Alice"


class TestMultiModelCLI:
    """Tests for multi-model CLI module."""

    def test_import(self):
        from bog_agents_cli.multi_model_cli import (
            detect_local_models,
            recommend_model_for_task,
        )

        assert detect_local_models is not None

    def test_recommend_simple_task(self):
        from bog_agents_cli.multi_model_cli import recommend_model_for_task

        model = recommend_model_for_task("fix typo in README")
        assert model  # Should return some model

    def test_recommend_complex_task(self):
        from bog_agents_cli.multi_model_cli import recommend_model_for_task

        model = recommend_model_for_task("architect the entire system")
        assert "opus" in model

    def test_parse_model_route(self):
        from bog_agents_cli.multi_model_cli import parse_model_route_command

        result = parse_model_route_command("auto")
        assert result["strategy"] == "auto"

    def test_format_model_list_empty(self):
        from bog_agents_cli.multi_model_cli import format_model_list

        result = format_model_list([])
        assert "No local models" in result


class TestCodeIntelligenceCLI:
    """Tests for code intelligence CLI module."""

    def test_import(self):
        from bog_agents_cli.code_intelligence_cli import (
            generate_health_prompt,
            parse_health_command,
        )

        assert parse_health_command is not None

    def test_parse_health_command(self):
        from bog_agents_cli.code_intelligence_cli import parse_health_command

        result = parse_health_command("quick")
        assert result["action"] == "quick"

    def test_parse_migrate_command(self):
        from bog_agents_cli.code_intelligence_cli import parse_migrate_command

        result = parse_migrate_command("javascript typescript")
        assert result["from"] == "javascript"
        assert result["to"] == "typescript"

    def test_generate_health_prompt(self):
        from bog_agents_cli.code_intelligence_cli import generate_health_prompt

        prompt = generate_health_prompt(["src/", "lib/"])
        assert "src/" in prompt

    def test_generate_onboard_prompt(self):
        from bog_agents_cli.code_intelligence_cli import generate_onboard_prompt

        prompt = generate_onboard_prompt()
        assert "onboarding" in prompt.lower()

    def test_generate_docs_prompt(self):
        from bog_agents_cli.code_intelligence_cli import generate_docs_prompt

        assert "API" in generate_docs_prompt("api")
        assert "README" in generate_docs_prompt("readme")


class TestSessionManager:
    """Tests for session manager CLI module."""

    def test_import(self):
        from bog_agents_cli.session_manager import (
            COMMAND_PALETTE,
            SessionStats,
            search_command_palette,
        )

        assert SessionStats is not None
        assert len(COMMAND_PALETTE) > 0

    def test_session_stats(self):
        from bog_agents_cli.session_manager import SessionStats

        stats = SessionStats(name="test", tokens_in=1000, tokens_out=500, cost_usd=0.01)
        assert stats.name == "test"
        assert stats.elapsed_seconds >= 0  # >= 0 to handle low-resolution Windows timers

    def test_format_session_stats(self):
        from bog_agents_cli.session_manager import SessionStats, format_session_stats

        stats = SessionStats(name="test", messages=5)
        result = format_session_stats(stats)
        assert "test" in result
        assert "5" in result

    def test_format_token_counter(self):
        from bog_agents_cli.session_manager import format_token_counter

        result = format_token_counter(1000, 500, 0.01)
        assert "1,000" in result
        assert "500" in result

    def test_search_command_palette(self):
        from bog_agents_cli.session_manager import search_command_palette

        results = search_command_palette("model")
        assert len(results) > 0
        assert any(r.name == "/model" for r in results)

    def test_search_command_palette_empty(self):
        from bog_agents_cli.session_manager import search_command_palette

        results = search_command_palette("zzzznonexistent")
        assert len(results) == 0

    def test_format_command_palette(self):
        from bog_agents_cli.session_manager import (
            CommandPaletteEntry,
            format_command_palette,
        )

        entries = [CommandPaletteEntry("/test", "Test command", "", "testing")]
        result = format_command_palette(entries)
        assert "/test" in result
        assert "Test command" in result

    def test_command_palette_stays_in_sync_with_registry(self):
        from bog_agents_cli.command_registry import get_command_palette_specs
        from bog_agents_cli.session_manager import COMMAND_PALETTE

        palette_names = {entry.name for entry in COMMAND_PALETTE}
        registry_names = {spec.name for spec in get_command_palette_specs()}
        assert palette_names == registry_names


class TestSlashCommands:
    """Tests for new slash commands in autocomplete."""

    def test_new_commands_exist(self):
        from bog_agents_cli.widgets.autocomplete import SLASH_COMMANDS

        names = {cmd[0] for cmd in SLASH_COMMANDS}
        new_commands = {
            "/commands",
            "/doctor",
            "/logs",
            "/onboard",
            "/review",
            "/session",
            "/skills",
        }
        for cmd in new_commands:
            assert cmd in names, f"Missing slash command: {cmd}"

    def test_total_command_count(self):
        from bog_agents_cli.widgets.autocomplete import SLASH_COMMANDS

        assert len(SLASH_COMMANDS) >= 25

    def test_wired_commands_exist(self):
        """New wired slash commands should be in the list."""
        from bog_agents_cli.widgets.autocomplete import SLASH_COMMANDS

        names = {cmd[0] for cmd in SLASH_COMMANDS}
        wired_commands = {"/background", "/dashboard", "/recommend"}
        for cmd in wired_commands:
            assert cmd in names, f"Missing wired slash command: {cmd}"


class TestRecommendModule:
    """Tests for the /recommend command module."""

    def test_parse_defaults(self):
        from bog_agents_cli.recommend import parse_recommend_args

        config = parse_recommend_args("")
        assert config.persona.value == "balanced"
        assert config.focus.value == "general"
        assert config.num_questions == 3
        assert config.max_findings == 25
        assert config.include_examples is True

    def test_parse_all_flags(self):
        from bog_agents_cli.recommend import parse_recommend_args

        config = parse_recommend_args(
            "--persona architect --focus security --questions 5 "
            "--max 10 --severity high --no-examples"
        )
        assert config.persona.value == "architect"
        assert config.focus.value == "security"
        assert config.num_questions == 5
        assert config.max_findings == 10
        assert config.severity_threshold == "high"
        assert config.include_examples is False

    def test_parse_scope(self):
        from bog_agents_cli.recommend import parse_recommend_args

        config = parse_recommend_args("--scope libs/bog-agents")
        assert config.scope_path == "libs/bog-agents"
        assert config.scope.value == "directory"

    def test_parse_invalid_values_use_defaults(self):
        from bog_agents_cli.recommend import parse_recommend_args

        config = parse_recommend_args("--persona nonexistent --focus invalid")
        assert config.persona.value == "balanced"
        assert config.focus.value == "general"

    def test_build_clarifying_prompt(self):
        from bog_agents_cli.recommend import RecommendConfig, build_clarifying_prompt

        config = RecommendConfig(num_questions=3)
        prompt = build_clarifying_prompt(config)
        assert "3 clarifying questions" in prompt
        assert "Staff Engineer" in prompt  # balanced persona

    def test_build_clarifying_prompt_no_questions(self):
        from bog_agents_cli.recommend import RecommendConfig, build_clarifying_prompt

        config = RecommendConfig(num_questions=0)
        prompt = build_clarifying_prompt(config)
        assert "clarifying questions" not in prompt

    def test_build_review_prompt(self):
        from bog_agents_cli.recommend import (
            Persona,
            RecommendConfig,
            build_review_prompt,
        )

        config = RecommendConfig(persona=Persona.SECURITY)
        prompt = build_review_prompt(config)
        assert "Security Engineer" in prompt
        assert "Executive Summary" in prompt
        assert "Findings" in prompt

    def test_format_help(self):
        from bog_agents_cli.recommend import format_recommend_help

        help_text = format_recommend_help()
        assert "--persona" in help_text
        assert "--focus" in help_text
        assert "--questions" in help_text
        assert "architect" in help_text
        assert "Examples:" in help_text

    def test_questions_clamped(self):
        from bog_agents_cli.recommend import parse_recommend_args

        config = parse_recommend_args("--questions 99")
        assert config.num_questions == 10  # max is 10

        config2 = parse_recommend_args("--questions -5")
        assert config2.num_questions == 0  # min is 0


class TestBackgroundAgentManager:
    """Tests for the background agent manager."""

    def test_init_empty(self):
        from bog_agents_cli.background_agents import BackgroundAgentManager

        mgr = BackgroundAgentManager()
        assert mgr.running_count == 0
        assert len(mgr.all_tasks) == 0
        assert "No background tasks" in mgr.format_status_table()

    def test_cleanup_empty(self):
        from bog_agents_cli.background_agents import BackgroundAgentManager

        mgr = BackgroundAgentManager()
        assert mgr.cleanup_completed() == 0

    def test_cancel_nonexistent(self):
        from bog_agents_cli.background_agents import BackgroundAgentManager

        mgr = BackgroundAgentManager()
        assert mgr.cancel("bg-999") is False

    def test_get_status_nonexistent(self):
        from bog_agents_cli.background_agents import BackgroundAgentManager

        mgr = BackgroundAgentManager()
        assert mgr.get_status("bg-999") is None


class TestDashboardModule:
    """Tests for the dashboard module."""

    def test_dashboard_state_basic(self):
        from bog_agents_cli.dashboard import DashboardState

        state = DashboardState()
        agent = state.add_agent("a1", "Test Agent")
        assert state.running_count == 0
        assert state.completed_count == 0

        agent.status = "running"
        assert state.running_count == 1

    def test_dashboard_state_totals(self):
        from bog_agents_cli.dashboard import DashboardState

        state = DashboardState()
        a1 = state.add_agent("a1", "Agent 1")
        a1.cost_usd = 0.05
        a1.tokens_used = 1000
        a2 = state.add_agent("a2", "Agent 2")
        a2.cost_usd = 0.03
        a2.tokens_used = 500

        state.update_totals()
        assert state.total_cost_usd == 0.08
        assert state.total_tokens == 1500

    def test_dashboard_layout_rendering(self):
        from bog_agents_cli.dashboard import DashboardState, create_dashboard_layout

        state = DashboardState()
        state.add_agent("a1", "Primary")
        output = create_dashboard_layout(state)
        assert "BOG AGENTS DASHBOARD" in output
        assert "Primary" in output
        assert "Agents: 1" in output

    def test_dashboard_remove_agent(self):
        from bog_agents_cli.dashboard import DashboardState

        state = DashboardState()
        state.add_agent("a1", "Test")
        assert len(state.agents) == 1
        state.remove_agent("a1")
        assert len(state.agents) == 0

    def test_dashboard_format_summary(self):
        from bog_agents_cli.dashboard import DashboardState

        state = DashboardState()
        a = state.add_agent("a1", "Worker")
        a.status = "running"
        a.tool_calls = 5
        summary = state.format_summary()
        assert "1 running" in summary
        assert "Worker" in summary

    def test_dashboard_screen_render(self):
        from bog_agents_cli.dashboard import DashboardScreen, DashboardState

        def builder():
            s = DashboardState()
            s.add_agent("a1", "Test")
            return s

        screen = DashboardScreen(state_builder=builder)
        output = screen.render_once()
        assert "Test" in output
        assert "BOG AGENTS DASHBOARD" in output

    def test_dashboard_screen_start_stop(self):
        from bog_agents_cli.dashboard import DashboardScreen

        screen = DashboardScreen()
        output = screen.start()
        assert screen.is_running is True
        assert "DASHBOARD" in output
        screen.stop()
        assert screen.is_running is False


class TestPROutput:
    """Tests for the PR output module."""

    def test_generate_pr_title_basic(self):
        from bog_agents_cli.pr_output import generate_pr_title

        title = generate_pr_title("fix the login bug")
        assert title == "Fix the login bug"

    def test_generate_pr_title_truncate(self):
        from bog_agents_cli.pr_output import generate_pr_title

        title = generate_pr_title("x" * 100)
        assert len(title) <= 70
        assert title.endswith("...")

    def test_generate_pr_body(self):
        from bog_agents_cli.pr_output import generate_pr_body

        body = generate_pr_body(
            "fix login",
            ["src/auth.py", "tests/test_auth.py"],
            ["abc1234 fix login"],
            test_results="5 passed",
        )
        assert "## Summary" in body
        assert "src/auth.py" in body
        assert "5 passed" in body
        assert "bog-agents" in body

    def test_pr_config_defaults(self):
        from bog_agents_cli.pr_output import PRConfig

        config = PRConfig()
        assert config.base_branch == "main"
        assert config.draft is False
        assert config.run_tests_before_pr is True
