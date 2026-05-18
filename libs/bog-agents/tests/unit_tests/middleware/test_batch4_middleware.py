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
