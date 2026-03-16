"""Tests for batch 4 (final) financial advisor middleware modules.

Tests for features: #1 (Browser Agent), #2 (Agent Teams), #3 (Automations),
#4 (Image/PDF Input), #5 (Cloud Sandbox), #6 (Computer Use), #16 (OpenSearch RAG),
#22 (Firm Deployment), #23 (Air-Gapped), #24 (SSO Auth), #26 (Dashboard),
#27 (Scheduled Reports), #28 (Collaborative Sessions), #29 (Messaging Integration),
#30 (Voice I/O), #40 (Due Diligence), #41 (Market Sentiment), #44 (Competitive Intel).
"""

from __future__ import annotations


class TestBrowserAgentFAMiddleware:
    """Tests for BrowserAgentFAMiddleware (#1)."""

    def test_init(self) -> None:
        from bog_agents.middleware.browser_agent_fa import BrowserAgentFAMiddleware

        mw = BrowserAgentFAMiddleware()
        assert len(mw.tools) == 5

    def test_tool_names(self) -> None:
        from bog_agents.middleware.browser_agent_fa import BrowserAgentFAMiddleware

        mw = BrowserAgentFAMiddleware()
        names = {t.name for t in mw.tools}
        assert names == {"navigate_to", "extract_content", "fill_form", "browser_history", "clear_browser"}

    def test_browser_action_dataclass(self) -> None:
        from bog_agents.middleware.browser_agent_fa import BrowserAction

        action = BrowserAction(
            action_id="act-1",
            url="https://example.com",
            action_type="navigate",
            selector="",
            value="",
            result="OK",
            timestamp="2025-01-01T00:00:00",
        )
        assert action.action_id == "act-1"
        assert action.url == "https://example.com"
        assert action.action_type == "navigate"

    def test_browser_session_tracking(self) -> None:
        from bog_agents.middleware.browser_agent_fa import BrowserAction, BrowserSession

        session = BrowserSession(session_id="s1")
        assert session.current_url == ""
        assert session.history == []
        session.current_url = "https://sec.gov"
        session.history.append("https://sec.gov")
        action = BrowserAction(
            action_id="act-1",
            url="https://sec.gov",
            action_type="navigate",
            selector="",
            value="",
            result="Navigated",
            timestamp="2025-01-01T00:00:00",
        )
        session.actions.append(action)
        assert len(session.actions) == 1
        assert len(session.history) == 1
        assert session.current_url == "https://sec.gov"

    def test_session_default_values(self) -> None:
        from bog_agents.middleware.browser_agent_fa import BrowserSession

        session = BrowserSession(session_id="default")
        assert session.actions == []
        assert session.history == []
        assert session.current_url == ""


class TestAgentTeamsMiddleware:
    """Tests for AgentTeamsMiddleware (#2)."""

    def test_init(self) -> None:
        from bog_agents.middleware.agent_teams import AgentTeamsMiddleware

        mw = AgentTeamsMiddleware()
        assert len(mw.tools) == 5

    def test_tool_names(self) -> None:
        from bog_agents.middleware.agent_teams import AgentTeamsMiddleware

        mw = AgentTeamsMiddleware()
        names = {t.name for t in mw.tools}
        assert names == {"create_project", "add_team_member", "assign_task", "project_status", "clear_projects"}

    def test_team_member_dataclass(self) -> None:
        from bog_agents.middleware.agent_teams import TeamMember

        member = TeamMember(name="Alice", role="analyst", capabilities=["research", "modeling"])
        assert member.name == "Alice"
        assert member.role == "analyst"
        assert len(member.capabilities) == 2
        assert member.status == "active"

    def test_shared_project_with_members_and_tasks(self) -> None:
        from bog_agents.middleware.agent_teams import SharedProject, TeamMember, TeamTask

        project = SharedProject(project_id="proj-1", name="Q4 Research", description="Quarterly analysis")
        member = TeamMember(name="Bob", role="lead")
        project.members.append(member)
        task = TeamTask(task_id="task-1", title="Gather data", assigned_to="Bob")
        project.tasks.append(task)
        assert len(project.members) == 1
        assert len(project.tasks) == 1
        assert project.tasks[0].status == "pending"

    def test_team_store_project_counter(self) -> None:
        from bog_agents.middleware.agent_teams import TeamStore

        store = TeamStore()
        assert store._next_project_id == 1
        assert store._next_task_id == 1
        assert len(store.projects) == 0


class TestAutomationsMiddleware:
    """Tests for AutomationsMiddleware (#3)."""

    def test_init(self) -> None:
        from bog_agents.middleware.automations import AutomationsMiddleware

        mw = AutomationsMiddleware()
        assert len(mw.tools) == 5

    def test_tool_names(self) -> None:
        from bog_agents.middleware.automations import AutomationsMiddleware

        mw = AutomationsMiddleware()
        names = {t.name for t in mw.tools}
        assert names == {"create_trigger", "create_automation", "list_automations", "fire_trigger", "clear_automations"}

    def test_trigger_condition_dataclass(self) -> None:
        from bog_agents.middleware.automations import TriggerCondition

        trigger = TriggerCondition(
            trigger_id="trig-1",
            event_type="price_alert",
            condition="AAPL > 200",
            threshold=200.0,
        )
        assert trigger.is_active is True
        assert trigger.event_type == "price_alert"
        assert trigger.threshold == 200.0

    def test_automation_rule_with_trigger(self) -> None:
        from bog_agents.middleware.automations import AutomationRule, TriggerCondition

        trigger = TriggerCondition(
            trigger_id="trig-1",
            event_type="threshold_breach",
            condition="VIX > 30",
            threshold=30.0,
        )
        rule = AutomationRule(
            rule_id="rule-1",
            name="VIX Alert",
            trigger=trigger,
            action_description="Send alert to compliance",
        )
        assert rule.trigger_count == 0
        assert rule.last_triggered == ""
        rule.trigger_count += 1
        assert rule.trigger_count == 1

    def test_automation_store_empty(self) -> None:
        from bog_agents.middleware.automations import AutomationStore

        store = AutomationStore()
        assert len(store.rules) == 0
        assert store._next_rule_id == 1
        assert store._next_trigger_id == 1


class TestImagePdfInputMiddleware:
    """Tests for ImagePdfInputMiddleware (#4)."""

    def test_init(self) -> None:
        from bog_agents.middleware.image_pdf_input import ImagePdfInputMiddleware

        mw = ImagePdfInputMiddleware()
        assert len(mw.tools) == 5

    def test_tool_names(self) -> None:
        from bog_agents.middleware.image_pdf_input import ImagePdfInputMiddleware

        mw = ImagePdfInputMiddleware()
        names = {t.name for t in mw.tools}
        assert names == {"register_document", "extract_text", "list_documents", "document_summary", "clear_documents"}

    def test_document_input_dataclass(self) -> None:
        from bog_agents.middleware.image_pdf_input import DocumentInput

        doc = DocumentInput(
            doc_id="doc-1",
            filename="report.pdf",
            doc_type="pdf",
            page_count=10,
            extracted_text="",
        )
        assert doc.doc_id == "doc-1"
        assert doc.doc_type == "pdf"
        assert doc.page_count == 10
        assert doc.extracted_text == ""
        assert doc.metadata == {}

    def test_document_input_with_metadata(self) -> None:
        from bog_agents.middleware.image_pdf_input import DocumentInput

        doc = DocumentInput(
            doc_id="doc-2",
            filename="chart.png",
            doc_type="image",
            page_count=1,
            extracted_text="",
            metadata={"source": "Bloomberg"},
        )
        assert doc.metadata["source"] == "Bloomberg"

    def test_input_store_empty(self) -> None:
        from bog_agents.middleware.image_pdf_input import InputStore

        store = InputStore()
        assert len(store.documents) == 0
        assert store._next_id == 1


class TestCloudSandboxMiddleware:
    """Tests for CloudSandboxMiddleware (#5)."""

    def test_init(self) -> None:
        from bog_agents.middleware.cloud_sandbox import CloudSandboxMiddleware

        mw = CloudSandboxMiddleware()
        assert len(mw.tools) == 5

    def test_tool_names(self) -> None:
        from bog_agents.middleware.cloud_sandbox import CloudSandboxMiddleware

        mw = CloudSandboxMiddleware()
        names = {t.name for t in mw.tools}
        assert names == {"create_sandbox", "activate_sandbox", "list_sandboxes", "sandbox_status", "clear_sandboxes"}

    def test_sandbox_config_dataclass(self) -> None:
        from bog_agents.middleware.cloud_sandbox import SandboxConfig

        config = SandboxConfig(
            sandbox_id="sandbox-1",
            name="Dev Environment",
            environment="development",
            preloaded_data=["client_data", "market_data"],
        )
        assert config.sandbox_id == "sandbox-1"
        assert config.environment == "development"
        assert len(config.preloaded_data) == 2
        assert config.status == "created"

    def test_sandbox_store_active_tracking(self) -> None:
        from bog_agents.middleware.cloud_sandbox import SandboxConfig, SandboxStore

        store = SandboxStore()
        assert store.active_sandbox == ""
        config = SandboxConfig(
            sandbox_id="sandbox-1",
            name="Test",
            environment="testing",
        )
        store.sandboxes["sandbox-1"] = config
        store.active_sandbox = "sandbox-1"
        assert store.active_sandbox == "sandbox-1"

    def test_sandbox_config_defaults(self) -> None:
        from bog_agents.middleware.cloud_sandbox import SandboxConfig

        config = SandboxConfig(sandbox_id="s-1", name="Empty", environment="staging")
        assert config.preloaded_data == []
        assert config.status == "created"
        assert config.created_at == ""


class TestComputerUseMiddleware:
    """Tests for ComputerUseMiddleware (#6)."""

    def test_init(self) -> None:
        from bog_agents.middleware.computer_use import ComputerUseMiddleware

        mw = ComputerUseMiddleware()
        assert len(mw.tools) == 5

    def test_tool_names(self) -> None:
        from bog_agents.middleware.computer_use import ComputerUseMiddleware

        mw = ComputerUseMiddleware()
        names = {t.name for t in mw.tools}
        assert names == {"connect_application", "desktop_action", "list_connected_apps", "action_history", "clear_desktop"}

    def test_desktop_action_dataclass(self) -> None:
        from bog_agents.middleware.computer_use import DesktopAction

        action = DesktopAction(
            action_id="dact-1",
            application="bloomberg",
            action_type="click",
            parameters={"target": "button"},
        )
        assert action.action_id == "dact-1"
        assert action.application == "bloomberg"
        assert action.action_type == "click"
        assert action.parameters["target"] == "button"

    def test_desktop_session_connected_apps(self) -> None:
        from bog_agents.middleware.computer_use import DesktopSession

        session = DesktopSession(session_id="default")
        assert session.connected_apps == []
        session.connected_apps.append("bloomberg")
        session.connected_apps.append("excel")
        assert len(session.connected_apps) == 2
        assert "bloomberg" in session.connected_apps

    def test_desktop_session_action_tracking(self) -> None:
        from bog_agents.middleware.computer_use import DesktopAction, DesktopSession

        session = DesktopSession(session_id="s1")
        action = DesktopAction(
            action_id="dact-1",
            application="excel",
            action_type="type",
            parameters={"target": "A1", "value": "100"},
            result="Typed 100",
            timestamp="2025-01-01T00:00:00",
        )
        session.actions.append(action)
        assert len(session.actions) == 1
        assert session.actions[0].result == "Typed 100"


class TestOpenSearchRAGMiddleware:
    """Tests for OpenSearchRAGMiddleware (#16)."""

    def test_init(self) -> None:
        from bog_agents.middleware.opensearch_rag import OpenSearchRAGMiddleware

        mw = OpenSearchRAGMiddleware()
        assert len(mw.tools) == 5

    def test_tool_names(self) -> None:
        from bog_agents.middleware.opensearch_rag import OpenSearchRAGMiddleware

        mw = OpenSearchRAGMiddleware()
        names = {t.name for t in mw.tools}
        assert names == {"index_document", "search_documents", "list_indexed", "search_history", "clear_index"}

    def test_index_document(self) -> None:
        from bog_agents.middleware.opensearch_rag import RAGStore

        store = RAGStore()
        doc = store.index_document(
            title="SEC Filing 10-K",
            content="Annual report for FY2024 with revenue growth of 15%.",
            source="SEC EDGAR",
            doc_type="filing",
        )
        assert doc.doc_id == "doc-1"
        assert doc.title == "SEC Filing 10-K"
        assert len(store.documents) == 1

    def test_search_keyword_matching(self) -> None:
        from bog_agents.middleware.opensearch_rag import RAGStore

        store = RAGStore()
        store.index_document(title="Revenue Report", content="Q4 revenue reached $5B, up 20% YoY.")
        store.index_document(title="Risk Assessment", content="Credit risk exposure decreased in Q4.")
        store.index_document(title="Marketing Plan", content="New advertising campaign for 2025.")

        query = store.search("revenue Q4")
        assert len(query.results) == 2
        # Revenue Report should rank higher (term in both title and content)
        assert query.results[0].title == "Revenue Report"

    def test_format_results_no_matches(self) -> None:
        from bog_agents.middleware.opensearch_rag import RAGQuery, RAGStore

        store = RAGStore()
        query = RAGQuery(query_id="q-1", query_text="nonexistent")
        result = store.format_results(query)
        assert "No results found" in result

    def test_format_results_with_matches(self) -> None:
        from bog_agents.middleware.opensearch_rag import RAGStore

        store = RAGStore()
        store.index_document(title="Bond Analysis", content="Corporate bond yields rising.", source="Internal")
        query = store.search("bond")
        result = store.format_results(query)
        assert "Search Results" in result
        assert "Bond Analysis" in result
        assert "score" in result

    def test_search_history_tracking(self) -> None:
        from bog_agents.middleware.opensearch_rag import RAGStore

        store = RAGStore()
        store.index_document(title="Test", content="Test content")
        store.search("test")
        store.search("content")
        assert len(store.queries) == 2
        assert store.queries[0].query_text == "test"
        assert store.queries[1].query_text == "content"


class TestFirmDeploymentMiddleware:
    """Tests for FirmDeploymentMiddleware (#22)."""

    def test_init(self) -> None:
        from bog_agents.middleware.firm_deployment import FirmDeploymentMiddleware

        mw = FirmDeploymentMiddleware()
        assert len(mw.tools) == 5

    def test_tool_names(self) -> None:
        from bog_agents.middleware.firm_deployment import FirmDeploymentMiddleware

        mw = FirmDeploymentMiddleware()
        names = {t.name for t in mw.tools}
        assert names == {"set_firm_config", "record_usage", "usage_analytics", "list_active_users", "clear_firm_data"}

    def test_set_config(self) -> None:
        from bog_agents.middleware.firm_deployment import FirmStore

        store = FirmStore()
        config = store.set_config(
            firm_name="Acme Capital",
            allowed_models=["claude-sonnet-4-6", "gpt-4"],
            compliance_mode=True,
            max_tokens_per_session=50000,
        )
        assert config.firm_name == "Acme Capital"
        assert len(config.allowed_models) == 2
        assert config.compliance_mode is True
        assert config.max_tokens_per_session == 50000

    def test_record_usage_and_active_users(self) -> None:
        from bog_agents.middleware.firm_deployment import FirmStore

        store = FirmStore()
        record = store.record_usage(user_id="advisor-1", action="query", tokens_used=1500)
        assert record.user_id == "advisor-1"
        assert record.tokens_used == 1500
        assert "advisor-1" in store.active_users
        store.record_usage(user_id="advisor-2", action="report", tokens_used=3000)
        assert len(store.active_users) == 2

    def test_format_analytics(self) -> None:
        from bog_agents.middleware.firm_deployment import FirmStore

        store = FirmStore()
        store.set_config(firm_name="Test Firm", compliance_mode=True)
        store.record_usage(user_id="u1", action="query", tokens_used=1000)
        store.record_usage(user_id="u1", action="report", tokens_used=2000)
        store.record_usage(user_id="u2", action="query", tokens_used=500)
        output = store.format_analytics()
        assert "Test Firm" in output
        assert "3,500" in output  # total tokens
        assert "Unique Users: 2" in output
        assert "Compliance Mode: ON" in output

    def test_format_analytics_empty(self) -> None:
        from bog_agents.middleware.firm_deployment import FirmStore

        store = FirmStore()
        assert store.format_analytics() == "No usage data recorded."


class TestAirGappedMiddleware:
    """Tests for AirGappedMiddleware (#23)."""

    def test_init(self) -> None:
        from bog_agents.middleware.air_gapped import AirGappedMiddleware

        mw = AirGappedMiddleware()
        assert len(mw.tools) == 5

    def test_tool_names(self) -> None:
        from bog_agents.middleware.air_gapped import AirGappedMiddleware

        mw = AirGappedMiddleware()
        names = {t.name for t in mw.tools}
        assert names == {"register_local_model", "set_data_policy", "check_data_flow", "air_gap_status", "clear_air_gap"}

    def test_register_local_model(self) -> None:
        from bog_agents.middleware.air_gapped import AirGapStore

        store = AirGapStore()
        model = store.register_model(
            name="local-llama",
            endpoint="http://localhost:8080",
            model_type="llm",
            is_available=True,
        )
        assert model.name == "local-llama"
        assert model.endpoint == "http://localhost:8080"
        assert model.is_available is True
        assert len(store.models) == 1

    def test_data_policy_blocks_external(self) -> None:
        from bog_agents.middleware.air_gapped import AirGapStore

        store = AirGapStore()
        # Default policy blocks external
        allowed, reason = store.check_allowed("api.openai.com")
        assert allowed is False
        assert "disabled" in reason.lower()

    def test_data_policy_allowed_domains(self) -> None:
        from bog_agents.middleware.air_gapped import AirGapStore

        store = AirGapStore()
        store.set_policy(
            allow_external=True,
            allowed_domains=["internal.firm.com"],
        )
        allowed, _ = store.check_allowed("internal.firm.com")
        assert allowed is True
        blocked, reason = store.check_allowed("external.com")
        assert blocked is False
        assert "not in the allowed list" in reason

    def test_data_policy_blocked_patterns(self) -> None:
        from bog_agents.middleware.air_gapped import AirGapStore

        store = AirGapStore()
        store.set_policy(
            allow_external=True,
            allowed_domains=[],
            blocked_patterns=["SSN", "account_number"],
        )
        allowed, _ = store.check_allowed("api.example.com", data="Normal query")
        assert allowed is True
        blocked, reason = store.check_allowed("api.example.com", data="Client SSN is 123-45-6789")
        assert blocked is False
        assert "blocked pattern" in reason.lower()

    def test_format_status(self) -> None:
        from bog_agents.middleware.air_gapped import AirGapStore

        store = AirGapStore()
        store.register_model("llama-3", "http://localhost:8080", "llm")
        output = store.format_status()
        assert "Air-Gap Deployment Status" in output
        assert "llama-3" in output
        assert "BLOCKED" in output  # default policy blocks external


class TestSSOAuthMiddleware:
    """Tests for SSOAuthMiddleware (#24)."""

    def test_init(self) -> None:
        from bog_agents.middleware.sso_auth import SSOAuthMiddleware

        mw = SSOAuthMiddleware()
        assert len(mw.tools) == 5

    def test_tool_names(self) -> None:
        from bog_agents.middleware.sso_auth import SSOAuthMiddleware

        mw = SSOAuthMiddleware()
        names = {t.name for t in mw.tools}
        assert names == {"register_sso_provider", "authenticate", "whoami", "auth_status", "clear_auth"}

    def test_register_provider(self) -> None:
        from bog_agents.middleware.sso_auth import AuthStore

        store = AuthStore()
        provider = store.register_provider(
            name="Okta",
            protocol="oidc",
            issuer_url="https://firm.okta.com",
            client_id="abc123",
        )
        assert provider.name == "Okta"
        assert provider.protocol == "oidc"
        assert provider.is_configured is True
        assert len(store.providers) == 1

    def test_create_session(self) -> None:
        from bog_agents.middleware.sso_auth import AuthStore

        store = AuthStore()
        store.register_provider("Okta", "oidc", "https://firm.okta.com", "abc123")
        session = store.create_session(
            user_id="advisor@firm.com",
            provider="Okta",
            roles=["advisor", "compliance"],
            duration_hours=4,
        )
        assert session.user_id == "advisor@firm.com"
        assert session.provider == "Okta"
        assert len(session.roles) == 2
        assert "advisor" in session.roles
        assert session.session_id != ""
        assert store.active_session_id == session.session_id

    def test_get_active_session(self) -> None:
        from bog_agents.middleware.sso_auth import AuthStore

        store = AuthStore()
        assert store.get_active_session() is None
        store.register_provider("AD", "saml", "https://ad.firm.com", "def456")
        session = store.create_session(user_id="user1", provider="AD")
        active = store.get_active_session()
        assert active is not None
        assert active.session_id == session.session_id

    def test_format_status(self) -> None:
        from bog_agents.middleware.sso_auth import AuthStore

        store = AuthStore()
        store.register_provider("Okta", "oidc", "https://firm.okta.com", "abc")
        store.create_session("user1", "Okta", roles=["admin"])
        output = store.format_status()
        assert "SSO / Authentication Status" in output
        assert "Okta" in output
        assert "ACTIVE" in output


class TestDashboardMiddleware:
    """Tests for DashboardMiddleware (#26)."""

    def test_init(self) -> None:
        from bog_agents.middleware.dashboard import DashboardMiddleware

        mw = DashboardMiddleware()
        assert len(mw.tools) == 5

    def test_tool_names(self) -> None:
        from bog_agents.middleware.dashboard import DashboardMiddleware

        mw = DashboardMiddleware()
        names = {t.name for t in mw.tools}
        assert names == {"create_layout", "add_widget", "list_layouts", "dashboard_preview", "clear_dashboards"}

    def test_create_layout(self) -> None:
        from bog_agents.middleware.dashboard import DashboardStore

        store = DashboardStore()
        layout = store.create_layout("Portfolio Overview")
        assert layout.name == "Portfolio Overview"
        assert layout.widgets == []
        assert store.active_layout == "Portfolio Overview"

    def test_add_widget(self) -> None:
        from bog_agents.middleware.dashboard import DashboardStore

        store = DashboardStore()
        store.create_layout("Main")
        widget = store.add_widget(
            layout_name="Main",
            title="Returns Chart",
            widget_type="chart",
            content="Monthly returns data",
            position=1,
            refresh_interval=60,
        )
        assert widget is not None
        assert widget.widget_id == "w-1"
        assert widget.title == "Returns Chart"
        assert widget.widget_type == "chart"
        assert widget.refresh_interval == 60

    def test_add_widget_to_missing_layout(self) -> None:
        from bog_agents.middleware.dashboard import DashboardStore

        store = DashboardStore()
        result = store.add_widget("nonexistent", "Widget", "chart")
        assert result is None

    def test_format_preview(self) -> None:
        from bog_agents.middleware.dashboard import DashboardStore

        store = DashboardStore()
        store.create_layout("Risk Dashboard")
        store.add_widget("Risk Dashboard", "VaR Metric", "metric", content="$1.2M")
        store.add_widget("Risk Dashboard", "Alert Panel", "alert", content="No alerts")
        output = store.format_preview("Risk Dashboard")
        assert "Dashboard: Risk Dashboard" in output
        assert "[METRIC]" in output
        assert "[ALERT]" in output
        assert "VaR Metric" in output

    def test_widget_position_sorting(self) -> None:
        from bog_agents.middleware.dashboard import DashboardStore

        store = DashboardStore()
        store.create_layout("Test")
        store.add_widget("Test", "Third", "text", position=3)
        store.add_widget("Test", "First", "text", position=1)
        store.add_widget("Test", "Second", "text", position=2)
        layout = store.layouts["Test"]
        assert layout.widgets[0].title == "First"
        assert layout.widgets[1].title == "Second"
        assert layout.widgets[2].title == "Third"


class TestScheduledReportsMiddleware:
    """Tests for ScheduledReportsMiddleware (#27)."""

    def test_init(self) -> None:
        from bog_agents.middleware.scheduled_reports import ScheduledReportsMiddleware

        mw = ScheduledReportsMiddleware()
        assert len(mw.tools) == 5

    def test_tool_names(self) -> None:
        from bog_agents.middleware.scheduled_reports import ScheduledReportsMiddleware

        mw = ScheduledReportsMiddleware()
        names = {t.name for t in mw.tools}
        assert names == {"create_schedule", "list_schedules", "toggle_schedule", "run_report_now", "clear_schedules"}

    def test_add_schedule(self) -> None:
        from bog_agents.middleware.scheduled_reports import ScheduledReportStore

        store = ScheduledReportStore()
        schedule = store.add_schedule(
            name="Weekly Portfolio",
            report_type="portfolio_summary",
            cron_expression="0 9 * * 1",
            recipients=["advisor@firm.com", "manager@firm.com"],
        )
        assert schedule.schedule_id == "sched-1"
        assert schedule.name == "Weekly Portfolio"
        assert schedule.report_type == "portfolio_summary"
        assert len(schedule.recipients) == 2
        assert schedule.is_active is True

    def test_toggle_schedule(self) -> None:
        from bog_agents.middleware.scheduled_reports import ScheduledReportStore

        store = ScheduledReportStore()
        store.add_schedule("Test", "compliance", "0 8 * * *")
        result = store.toggle("sched-1")
        assert result is not None
        assert result.is_active is False
        store.toggle("sched-1")
        assert result.is_active is True

    def test_record_run(self) -> None:
        from bog_agents.middleware.scheduled_reports import ScheduledReportStore

        store = ScheduledReportStore()
        store.add_schedule("Daily Risk", "risk", "0 6 * * *")
        store.record_run("sched-1")
        store.record_run("sched-1")
        schedule = store.get("sched-1")
        assert schedule is not None
        assert schedule.run_count == 2
        assert schedule.last_run != ""

    def test_format_listing(self) -> None:
        from bog_agents.middleware.scheduled_reports import ScheduledReportStore

        store = ScheduledReportStore()
        store.add_schedule("Active Report", "performance", "0 9 * * 1")
        store.add_schedule("Paused Report", "custom", "0 12 * * *")
        store.toggle("sched-2")
        output = store.format_listing()
        assert "Scheduled Reports" in output
        assert "Active Report" in output
        assert "PAUSED" in output

    def test_get_nonexistent_schedule(self) -> None:
        from bog_agents.middleware.scheduled_reports import ScheduledReportStore

        store = ScheduledReportStore()
        assert store.get("sched-999") is None


class TestCollaborativeSessionsMiddleware:
    """Tests for CollaborativeSessionsMiddleware (#28)."""

    def test_init(self) -> None:
        from bog_agents.middleware.collaborative_sessions import CollaborativeSessionsMiddleware

        mw = CollaborativeSessionsMiddleware()
        assert len(mw.tools) == 5

    def test_tool_names(self) -> None:
        from bog_agents.middleware.collaborative_sessions import CollaborativeSessionsMiddleware

        mw = CollaborativeSessionsMiddleware()
        names = {t.name for t in mw.tools}
        assert names == {"create_collab_session", "join_session", "send_message", "session_transcript", "clear_sessions"}

    def test_create_session(self) -> None:
        from bog_agents.middleware.collaborative_sessions import CollabStore

        store = CollabStore()
        session = store.create_session("Q4 Planning")
        assert session.session_id == 1
        assert session.title == "Q4 Planning"
        assert store.active_session_id == 1
        assert session.created_at != ""

    def test_add_message(self) -> None:
        from bog_agents.middleware.collaborative_sessions import CollaborativeSession

        session = CollaborativeSession(session_id=1, title="Test")
        msg1 = session.add_message("user_1", "Hello everyone", "chat")
        msg2 = session.add_message("user_2", "Reviewing report", "annotation")
        assert msg1.msg_id == 1
        assert msg2.msg_id == 2
        assert msg1.msg_type == "chat"
        assert msg2.msg_type == "annotation"
        assert len(session.messages) == 2

    def test_participant_dataclass(self) -> None:
        from bog_agents.middleware.collaborative_sessions import Participant

        p = Participant(
            user_id="user_1",
            name="Alice",
            role="lead analyst",
            joined_at="2025-01-01T00:00:00",
        )
        assert p.is_active is True
        assert p.name == "Alice"
        assert p.role == "lead analyst"

    def test_session_message_types(self) -> None:
        from bog_agents.middleware.collaborative_sessions import SessionMessage

        msg = SessionMessage(
            msg_id=1,
            sender_id="user_1",
            content="Action item noted",
            timestamp="2025-01-01T00:00:00",
            msg_type="action",
        )
        assert msg.msg_type == "action"
        assert msg.content == "Action item noted"


class TestMessagingIntegrationMiddleware:
    """Tests for MessagingIntegrationMiddleware (#29)."""

    def test_init(self) -> None:
        from bog_agents.middleware.messaging_integration import MessagingIntegrationMiddleware

        mw = MessagingIntegrationMiddleware()
        assert len(mw.tools) == 5

    def test_tool_names(self) -> None:
        from bog_agents.middleware.messaging_integration import MessagingIntegrationMiddleware

        mw = MessagingIntegrationMiddleware()
        names = {t.name for t in mw.tools}
        assert names == {"register_channel", "send_to_channel", "list_channels", "message_history", "clear_messaging"}

    def test_register_channel(self) -> None:
        from bog_agents.middleware.messaging_integration import MessagingStore

        store = MessagingStore()
        channel = store.register_channel("slack", "#research", "https://hooks.slack.com/xxx")
        assert channel.channel_id == 1
        assert channel.platform == "slack"
        assert channel.channel_name == "#research"
        assert channel.is_active is True

    def test_send_message(self) -> None:
        from bog_agents.middleware.messaging_integration import MessagingStore

        store = MessagingStore()
        store.register_channel("teams", "Finance Team", "https://webhook.teams.com/xxx")
        msg = store.send_message(channel_id=1, content="Market update: SPY up 1.5%", fmt="markdown")
        assert msg.msg_id == 1
        assert msg.channel_id == 1
        assert msg.format == "markdown"
        assert msg.status == "sent"

    def test_outbound_message_dataclass(self) -> None:
        from bog_agents.middleware.messaging_integration import OutboundMessage

        msg = OutboundMessage(
            msg_id=1,
            channel_id=2,
            content="Test",
            format="html",
            sent_at="2025-01-01T00:00:00",
            status="pending",
        )
        assert msg.format == "html"
        assert msg.status == "pending"

    def test_multiple_channels(self) -> None:
        from bog_agents.middleware.messaging_integration import MessagingStore

        store = MessagingStore()
        store.register_channel("slack", "#alerts", "https://hook1.com")
        store.register_channel("email", "team@firm.com", "smtp://mail.firm.com")
        store.register_channel("webhook", "CRM Hook", "https://crm.firm.com/webhook")
        assert len(store.channels) == 3
        assert store.channels[1].platform == "slack"
        assert store.channels[2].platform == "email"
        assert store.channels[3].platform == "webhook"


class TestVoiceIOMiddleware:
    """Tests for VoiceIOMiddleware (#30)."""

    def test_init(self) -> None:
        from bog_agents.middleware.voice_io import VoiceIOMiddleware

        mw = VoiceIOMiddleware()
        assert len(mw.tools) == 5

    def test_tool_names(self) -> None:
        from bog_agents.middleware.voice_io import VoiceIOMiddleware

        mw = VoiceIOMiddleware()
        names = {t.name for t in mw.tools}
        assert names == {"process_voice_input", "generate_voice_response", "set_voice_language", "voice_history", "clear_voice"}

    def test_voice_command_dataclass(self) -> None:
        from bog_agents.middleware.voice_io import VoiceCommand

        cmd = VoiceCommand(
            cmd_id=1,
            transcript="Show me AAPL performance",
            confidence=0.95,
            language="en",
            processed_at="2025-01-01T00:00:00",
        )
        assert cmd.transcript == "Show me AAPL performance"
        assert cmd.confidence == 0.95
        assert cmd.language == "en"

    def test_voice_response_dataclass(self) -> None:
        from bog_agents.middleware.voice_io import VoiceResponse

        resp = VoiceResponse(
            response_id=1,
            text="Apple stock is up 2% today",
            audio_format="mp3",
            duration_secs=3.5,
            created_at="2025-01-01T00:00:00",
        )
        assert resp.audio_format == "mp3"
        assert resp.duration_secs == 3.5

    def test_voice_store_default_language(self) -> None:
        from bog_agents.middleware.voice_io import VoiceStore

        store = VoiceStore()
        assert store.active_language == "en"
        assert store.commands == []
        assert store.responses == []


class TestDueDiligenceMiddleware:
    """Tests for DueDiligenceMiddleware (#40)."""

    def test_init(self) -> None:
        from bog_agents.middleware.due_diligence import DueDiligenceMiddleware

        mw = DueDiligenceMiddleware()
        assert len(mw.tools) == 5

    def test_tool_names(self) -> None:
        from bog_agents.middleware.due_diligence import DueDiligenceMiddleware

        mw = DueDiligenceMiddleware()
        names = {t.name for t in mw.tools}
        assert names == {"start_due_diligence", "add_checklist_item", "update_item_status", "dd_report", "clear_due_diligence"}

    def test_dd_workflow_creation(self) -> None:
        from bog_agents.middleware.due_diligence import DDStore

        store = DDStore()
        wf_id = store._next_workflow_id
        wf = DDWorkflow_helper(store, "Acme Corp", "equity")
        assert wf.workflow_id == wf_id
        assert wf.company == "Acme Corp"
        assert wf.deal_type == "equity"
        assert wf.overall_status == "pending"

    def test_checklist_item_status_updates(self) -> None:
        from bog_agents.middleware.due_diligence import DDChecklistItem

        item = DDChecklistItem(
            item_id=1,
            category="financial",
            description="Review audited financials",
        )
        assert item.status == "pending"
        item.status = "pass"
        item.findings = "Clean audit opinion, no material weaknesses"
        item.reviewed_by = "analyst-1"
        assert item.status == "pass"
        assert "Clean audit" in item.findings

    def test_dd_workflow_checklist_management(self) -> None:
        from bog_agents.middleware.due_diligence import DDChecklistItem, DDWorkflow

        wf = DDWorkflow(workflow_id=1, company="TestCo", deal_type="ma")
        item1 = DDChecklistItem(item_id=wf._next_item_id, category="legal", description="Check IP rights")
        wf._next_item_id += 1
        wf.checklist.append(item1)
        item2 = DDChecklistItem(item_id=wf._next_item_id, category="operational", description="Review supply chain")
        wf._next_item_id += 1
        wf.checklist.append(item2)
        assert len(wf.checklist) == 2
        assert wf.checklist[0].category == "legal"
        assert wf.checklist[1].category == "operational"

    def test_dd_checklist_item_evidence_sources(self) -> None:
        from bog_agents.middleware.due_diligence import DDChecklistItem

        item = DDChecklistItem(
            item_id=1,
            category="regulatory",
            description="Verify regulatory filings",
            evidence_sources=["SEC EDGAR", "State regulator", "FINRA"],
        )
        assert len(item.evidence_sources) == 3
        assert "SEC EDGAR" in item.evidence_sources

    def test_dd_store_defaults(self) -> None:
        from bog_agents.middleware.due_diligence import DDStore

        store = DDStore()
        assert store.active_workflow_id is None
        assert len(store.workflows) == 0
        assert store._next_workflow_id == 1


class TestMarketSentimentMiddleware:
    """Tests for MarketSentimentMiddleware (#41)."""

    def test_init(self) -> None:
        from bog_agents.middleware.market_sentiment import MarketSentimentMiddleware

        mw = MarketSentimentMiddleware()
        assert len(mw.tools) == 5

    def test_tool_names(self) -> None:
        from bog_agents.middleware.market_sentiment import MarketSentimentMiddleware

        mw = MarketSentimentMiddleware()
        names = {t.name for t in mw.tools}
        assert names == {"add_sentiment_signal", "sentiment_dashboard", "ticker_sentiment", "list_signals", "clear_sentiment"}

    def test_add_signal(self) -> None:
        from bog_agents.middleware.market_sentiment import SentimentStore

        store = SentimentStore()
        signal = store.add_signal(
            source="news",
            ticker="aapl",
            sentiment=0.8,
            confidence=0.9,
            category="bullish",
        )
        assert signal.signal_id == 1
        assert signal.ticker == "AAPL"  # uppercased
        assert signal.sentiment == 0.8
        assert signal.category == "bullish"

    def test_aggregate_computes_averages(self) -> None:
        from bog_agents.middleware.market_sentiment import SentimentStore

        store = SentimentStore()
        store.add_signal("news", "AAPL", 0.6, 0.9, "bullish")
        store.add_signal("social_media", "AAPL", -0.2, 0.7, "bearish")
        store.add_signal("analyst", "AAPL", 0.4, 0.85, "bullish")
        aggs = store.aggregate("AAPL")
        assert len(aggs) == 1
        a = aggs[0]
        assert a.ticker == "AAPL"
        assert a.signal_count == 3
        expected_avg = round((0.6 + -0.2 + 0.4) / 3, 4)
        assert abs(a.avg_sentiment - expected_avg) < 1e-4
        assert abs(a.bullish_pct - 66.7) < 0.1
        assert abs(a.bearish_pct - 33.3) < 0.1

    def test_aggregate_multiple_tickers(self) -> None:
        from bog_agents.middleware.market_sentiment import SentimentStore

        store = SentimentStore()
        store.add_signal("news", "AAPL", 0.5, 0.9, "bullish")
        store.add_signal("news", "GOOG", -0.3, 0.8, "bearish")
        store.add_signal("news", "MSFT", 0.1, 0.7, "neutral")
        aggs = store.aggregate()
        assert len(aggs) == 3
        tickers = [a.ticker for a in aggs]
        assert "AAPL" in tickers
        assert "GOOG" in tickers
        assert "MSFT" in tickers

    def test_format_dashboard(self) -> None:
        from bog_agents.middleware.market_sentiment import SentimentStore

        store = SentimentStore()
        store.add_signal("news", "TSLA", 0.7, 0.85, "bullish")
        store.add_signal("analyst", "TSLA", 0.3, 0.9, "bullish")
        output = store.format_dashboard()
        assert "Market Sentiment Dashboard" in output
        assert "TSLA" in output
        assert "bullish" in output

    def test_format_dashboard_empty(self) -> None:
        from bog_agents.middleware.market_sentiment import SentimentStore

        store = SentimentStore()
        assert store.format_dashboard() == "No sentiment data available."


class TestCompetitiveIntelMiddleware:
    """Tests for CompetitiveIntelMiddleware (#44)."""

    def test_init(self) -> None:
        from bog_agents.middleware.competitive_intel import CompetitiveIntelMiddleware

        mw = CompetitiveIntelMiddleware()
        assert len(mw.tools) == 5

    def test_tool_names(self) -> None:
        from bog_agents.middleware.competitive_intel import CompetitiveIntelMiddleware

        mw = CompetitiveIntelMiddleware()
        names = {t.name for t in mw.tools}
        assert names == {"add_competitor", "add_intel_event", "competitor_briefing", "intel_timeline", "clear_intel"}

    def test_add_competitor(self) -> None:
        from bog_agents.middleware.competitive_intel import IntelStore

        store = IntelStore()
        profile = store.add_competitor(
            name="Rival Capital",
            ticker="rvl",
            sector="Asset Management",
            market_cap="5B",
            description="Mid-size asset manager focused on fixed income",
        )
        assert profile.ticker == "RVL"  # uppercased
        assert profile.name == "Rival Capital"
        assert "RVL" in store.competitors

    def test_add_event(self) -> None:
        from bog_agents.middleware.competitive_intel import IntelStore

        store = IntelStore()
        store.add_competitor("Rival", "RVL", "AM", "5B", "Competitor")
        event = store.add_event(
            competitor="RVL",
            event_type="earnings",
            title="Q4 Earnings Beat",
            description="Revenue up 12% YoY",
            source="SEC Filing",
            date="2025-01-15",
            impact="high",
        )
        assert event.event_id == 1
        assert event.competitor == "RVL"
        assert event.event_type == "earnings"
        assert event.impact == "high"

    def test_get_competitor_events(self) -> None:
        from bog_agents.middleware.competitive_intel import IntelStore

        store = IntelStore()
        store.add_competitor("A Corp", "ACOR", "Tech", "10B", "Tech competitor")
        store.add_competitor("B Corp", "BCOR", "Tech", "8B", "Another competitor")
        store.add_event("ACOR", "filing", "10-K Filed", "Annual report", "SEC", "2025-03-01", "medium")
        store.add_event("BCOR", "earnings", "Q1 Miss", "Revenue miss", "PR", "2025-04-01", "high")
        store.add_event("ACOR", "acquisition", "M&A Deal", "Acquired startup", "News", "2025-02-15", "high")
        acor_events = store.get_competitor_events("ACOR")
        assert len(acor_events) == 2
        bcor_events = store.get_competitor_events("BCOR")
        assert len(bcor_events) == 1

    def test_format_briefing(self) -> None:
        from bog_agents.middleware.competitive_intel import IntelStore

        store = IntelStore()
        store.add_competitor("Rival Fund", "RFND", "AM", "3B", "Hedge fund")
        store.add_event("RFND", "leadership_change", "New CIO", "Hired new CIO from Goldman", "Bloomberg", "2025-01-10", "medium")
        output = store.format_briefing("RFND")
        assert "Competitive Intelligence Briefing" in output
        assert "Rival Fund" in output
        assert "New CIO" in output
        assert "!!" in output  # medium impact marker

    def test_format_briefing_empty(self) -> None:
        from bog_agents.middleware.competitive_intel import IntelStore

        store = IntelStore()
        assert store.format_briefing() == "No competitors tracked."


def DDWorkflow_helper(store: object, company: str, deal_type: str) -> object:
    """Helper to create a DDWorkflow via the store pattern used in middleware."""
    import time

    from bog_agents.middleware.due_diligence import DDWorkflow

    wf = DDWorkflow(
        workflow_id=store._next_workflow_id,  # type: ignore[attr-defined]
        company=company,
        deal_type=deal_type,
        started_at=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime()),
    )
    store.workflows[store._next_workflow_id] = wf  # type: ignore[attr-defined]
    store.active_workflow_id = store._next_workflow_id  # type: ignore[attr-defined]
    store._next_workflow_id += 1  # type: ignore[attr-defined]
    return wf
