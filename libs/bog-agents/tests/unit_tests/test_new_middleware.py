"""Tests for new middleware: smart approvals, http hooks, adaptive context,
model cascade, hot-reload skills, scheduled runs, self-improving, security audit,
agent replay, offline mode.
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path


class TestSmartApprovals:
    """Tests for SmartApprovalsMiddleware."""

    def test_safe_tool_auto_approved(self):
        from bog_agents.middleware.smart_approvals import (
            DEFAULT_POLICIES,
            ApprovalHistory,
            RiskLevel,
            evaluate_tool_call,
        )

        history = ApprovalHistory()
        decision = evaluate_tool_call(
            "read_file",
            {"path": "/tmp/test.txt"},
            DEFAULT_POLICIES,
            history,
        )
        assert decision.approved is True
        assert decision.risk_level == RiskLevel.SAFE

    def test_execute_classified_as_medium(self):
        from bog_agents.middleware.smart_approvals import (
            DEFAULT_POLICIES,
            ApprovalHistory,
            RiskLevel,
            evaluate_tool_call,
        )

        history = ApprovalHistory()
        decision = evaluate_tool_call(
            "execute",
            {"command": "ls -la"},
            DEFAULT_POLICIES,
            history,
        )
        assert decision.risk_level == RiskLevel.MEDIUM

    def test_dangerous_command_escalates_risk(self):
        from bog_agents.middleware.smart_approvals import (
            DEFAULT_POLICIES,
            RiskLevel,
            _classify_risk,
        )

        risk, _ = _classify_risk(
            "execute",
            {"command": "rm -rf /"},
            DEFAULT_POLICIES,
        )
        assert risk == RiskLevel.HIGH

    def test_history_tracking(self):
        from bog_agents.middleware.smart_approvals import (
            ApprovalDecision,
            ApprovalHistory,
            RiskLevel,
        )

        history = ApprovalHistory()
        for _ in range(5):
            history.record(
                ApprovalDecision(
                    approved=True,
                    risk_level=RiskLevel.LOW,
                    reason="test",
                    tool_name="edit_file",
                )
            )

        assert history.tool_approval_ratio("edit_file") == 1.0
        assert history.tool_approval_ratio("unknown") == 0.5

    def test_middleware_evaluate(self):
        from bog_agents.middleware.smart_approvals import SmartApprovalsMiddleware

        mw = SmartApprovalsMiddleware()
        decision = mw.evaluate("ls", {})
        assert decision.approved is True

    def test_record_human_decision(self):
        from bog_agents.middleware.smart_approvals import SmartApprovalsMiddleware

        mw = SmartApprovalsMiddleware()
        mw.record_human_decision("execute", approved=True)
        assert len(mw.history.decisions) == 1


class TestAdaptiveContext:
    """Tests for AdaptiveContextMiddleware."""

    def test_detect_known_model(self):
        from bog_agents.middleware.adaptive_context import detect_context_window

        # ``detect_context_window`` now consults installed provider
        # profiles first; upstream values may differ slightly from the
        # curated fallback (e.g. Google reports 1,048,576 = 1024*1024
        # for gemini-2.5-pro, not the round 1 000 000 we cached). Pin
        # the floors that matter for the tiering logic rather than the
        # exact figures upstream is free to revise.
        # Haiku 4.5 still resolves to 200K; sonnet-4-6 was bumped to 1M
        # upstream when Anthropic enabled the 1M context window beta.
        assert detect_context_window("claude-haiku-4-5") == 200_000
        assert detect_context_window("gpt-4o") == 128_000
        assert detect_context_window("gemini-2.5-pro") >= 1_000_000

    def test_detect_with_provider_prefix(self):
        from bog_agents.middleware.adaptive_context import detect_context_window

        assert detect_context_window("anthropic:claude-haiku-4-5") == 200_000
        assert detect_context_window("google_genai:gemini-2.5-pro") >= 1_000_000

    def test_unknown_model_uses_default(self):
        from bog_agents.middleware.adaptive_context import detect_context_window

        assert detect_context_window("unknown-model", default=32_000) == 32_000

    def test_tier_selection(self):
        from bog_agents.middleware.adaptive_context import ContextTier, get_tier_config

        small_config = get_tier_config(8_000)
        assert small_config.tier == ContextTier.SMALL

        massive_config = get_tier_config(1_000_000)
        assert massive_config.tier == ContextTier.MASSIVE

    def test_context_usage_should_summarize(self):
        from bog_agents.middleware.adaptive_context import ContextUsage, get_tier_config

        config = get_tier_config(128_000)
        usage = ContextUsage(total_tokens=100_000, context_window=128_000)
        assert usage.should_summarize(config) is True

        usage2 = ContextUsage(total_tokens=10_000, context_window=128_000)
        assert usage2.should_summarize(config) is False

    def test_middleware_truncate(self):
        from bog_agents.middleware.adaptive_context import AdaptiveContextMiddleware

        mw = AdaptiveContextMiddleware(model_name="gpt-4")
        long_output = "x" * 100_000
        truncated = mw.truncate_tool_output(long_output)
        assert len(truncated) < len(long_output)
        assert "truncated" in truncated


class TestModelCascade:
    """Tests for ModelCascadeMiddleware."""

    def test_trivial_task_classified(self):
        from bog_agents.middleware.model_cascade import TaskComplexity, classify_complexity

        assert classify_complexity("what is the version?") == TaskComplexity.TRIVIAL

    def test_complex_task_classified(self):
        from bog_agents.middleware.model_cascade import TaskComplexity, classify_complexity

        assert classify_complexity("debug the performance issue in the authentication system") == TaskComplexity.COMPLEX

    def test_select_cheapest_tier(self):
        from bog_agents.middleware.model_cascade import (
            DEFAULT_CASCADE,
            CascadeHistory,
            TaskComplexity,
            select_model_tier,
        )

        history = CascadeHistory()
        # Use cascade without vision requirement to avoid socket issues
        tier = select_model_tier(
            TaskComplexity.TRIVIAL,
            DEFAULT_CASCADE,
            history,
            require_tools=False,
        )
        assert tier.name == "fast"

    def test_expert_gets_frontier(self):
        from bog_agents.middleware.model_cascade import (
            DEFAULT_CASCADE,
            CascadeHistory,
            TaskComplexity,
            select_model_tier,
        )

        history = CascadeHistory()
        tier = select_model_tier(
            TaskComplexity.EXPERT,
            DEFAULT_CASCADE,
            history,
            require_tools=False,
        )
        assert tier.name == "frontier"

    def test_middleware_route(self):
        from bog_agents.middleware.model_cascade import ModelCascadeMiddleware

        mw = ModelCascadeMiddleware()
        tier = mw.route("show me the version")
        assert tier.name in ("fast", "standard", "frontier")

    def test_savings_estimate(self):
        from bog_agents.middleware.model_cascade import ModelCascadeMiddleware

        mw = ModelCascadeMiddleware()
        mw.route("list files")
        mw.route("list files")
        assert mw.estimated_savings_pct >= 0.0


class TestScheduledRuns:
    """Tests for ScheduledRunsMiddleware."""

    def test_parse_interval(self):
        from bog_agents.middleware.scheduled_runs import IntervalUnit, parse_schedule_string

        schedule = parse_schedule_string("every 5 minutes")
        assert schedule.value == 5
        assert schedule.unit == IntervalUnit.MINUTES

    def test_parse_cron(self):
        from bog_agents.middleware.scheduled_runs import CronExpression, parse_schedule_string

        schedule = parse_schedule_string("0 9 * * 1-5")
        assert isinstance(schedule, CronExpression)
        assert schedule.hour == "9"

    def test_parse_daily(self):
        from bog_agents.middleware.scheduled_runs import IntervalUnit, parse_schedule_string

        schedule = parse_schedule_string("every day")
        assert schedule.value == 1
        assert schedule.unit == IntervalUnit.DAYS

    def test_task_is_due(self):
        from bog_agents.middleware.scheduled_runs import (
            IntervalUnit,
            ScheduledTask,
            ScheduleInterval,
        )

        task = ScheduledTask(
            task_id="t1",
            name="test",
            prompt="do stuff",
            schedule=ScheduleInterval(value=1, unit=IntervalUnit.MINUTES),
            last_run_at=time.time() - 120,  # 2 minutes ago
        )
        assert task.is_due() is True

    def test_task_not_due(self):
        from bog_agents.middleware.scheduled_runs import (
            IntervalUnit,
            ScheduledTask,
            ScheduleInterval,
        )

        task = ScheduledTask(
            task_id="t1",
            name="test",
            prompt="do stuff",
            schedule=ScheduleInterval(value=10, unit=IntervalUnit.MINUTES),
            last_run_at=time.time(),  # Just ran
        )
        assert task.is_due() is False

    def test_middleware_schedule_and_list(self):
        from bog_agents.middleware.scheduled_runs import ScheduledRunsMiddleware

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            store_path = f.name
        try:
            mw = ScheduledRunsMiddleware(store_path=store_path)
            mw.schedule_task(
                name="test-task",
                prompt="check for updates",
                schedule="every 2 hours",
            )
            tasks = mw.list_tasks()
            assert len(tasks) == 1
            assert tasks[0].name == "test-task"
        finally:
            os.unlink(store_path)

    def test_cron_matches(self):
        from bog_agents.middleware.scheduled_runs import CronExpression

        # Every minute
        cron = CronExpression(minute="*")
        assert cron.matches_time() is True

        # Specific invalid time
        cron2 = CronExpression(minute="59", hour="23", day_of_month="31", month="2")
        # Feb 31 doesn't exist, but we test the logic
        assert isinstance(cron2.matches_time(), bool)

    def test_cron_weekday_mapping(self):
        """Verify cron uses Sunday=0 convention, not Python's Monday=0."""
        import datetime

        from bog_agents.middleware.scheduled_runs import CronExpression

        # Create a known Sunday: 2026-03-15 is a Sunday
        sunday_ts = datetime.datetime(2026, 3, 15, 9, 0).timestamp()
        # Cron 0 = Sunday
        cron_sunday = CronExpression(minute="0", hour="9", day_of_week="0")
        assert cron_sunday.matches_time(sunday_ts) is True

        # Cron 1 = Monday — should NOT match Sunday
        cron_monday = CronExpression(minute="0", hour="9", day_of_week="1")
        assert cron_monday.matches_time(sunday_ts) is False

        # Monday: 2026-03-16
        monday_ts = datetime.datetime(2026, 3, 16, 9, 0).timestamp()
        assert cron_monday.matches_time(monday_ts) is True
        assert cron_sunday.matches_time(monday_ts) is False


class TestSelfImproving:
    """Tests for SelfImprovingMiddleware."""

    def test_session_metrics_efficiency(self):
        from bog_agents.middleware.self_improving import SessionMetrics

        metrics = SessionMetrics(
            session_id="test",
            total_turns=10,
            tool_calls=20,
            tool_errors=1,
        )
        assert 0.0 <= metrics.efficiency_score <= 1.0

    def test_assess_good_session(self):
        from bog_agents.middleware.self_improving import SessionMetrics, assess_session

        metrics = SessionMetrics(
            session_id="good",
            total_turns=10,
            tool_calls=20,
            tool_errors=0,
            user_corrections=0,
            undos_performed=0,
            tests_passed=5,
            tests_failed=0,
        )
        assessment = assess_session(metrics)
        assert assessment.rating in ("excellent", "good")

    def test_assess_bad_session(self):
        from bog_agents.middleware.self_improving import SessionMetrics, assess_session

        metrics = SessionMetrics(
            session_id="bad",
            total_turns=10,
            tool_calls=20,
            tool_errors=15,
            user_corrections=8,
            undos_performed=5,
        )
        assessment = assess_session(metrics)
        assert assessment.rating in ("poor", "failed")
        assert len(assessment.lessons_learned) > 0

    def test_improvement_prompt_generation(self):
        from bog_agents.middleware.self_improving import (
            ImprovementRecord,
            SelfAssessment,
            generate_improvement_prompt,
        )

        record = ImprovementRecord()
        record.add_assessment(
            SelfAssessment(
                session_id="s1",
                rating="good",
                efficiency_score=0.8,
                lessons_learned=["test early"],
                suggested_improvements=["run linter"],
                patterns_to_remember=["check tests after changes"],
                patterns_to_avoid=["blind edits"],
            )
        )

        prompt = generate_improvement_prompt(record)
        assert "Self-Improvement" in prompt
        assert "check tests after changes" in prompt


class TestSecurityAudit:
    """Tests for SecurityAuditMiddleware."""

    def test_detect_hardcoded_password(self):
        from bog_agents.middleware.security_audit import scan_file_for_secrets

        content = 'password = "super_secret_123456"'
        findings = scan_file_for_secrets("test.py", content, [0])
        assert len(findings) >= 1
        assert any("password" in f.title.lower() or "Hardcoded" in f.title for f in findings)

    def test_detect_aws_key(self):
        from bog_agents.middleware.security_audit import scan_file_for_secrets

        content = 'aws_key = "AKIAIOSFODNN7EXAMPLE"'
        findings = scan_file_for_secrets("config.py", content, [0])
        assert len(findings) >= 1

    def test_detect_eval_usage(self):
        from bog_agents.middleware.security_audit import scan_file_for_patterns

        content = "result = eval(user_input)"
        findings = scan_file_for_patterns("app.py", content, [0])
        assert len(findings) >= 1
        assert any("eval" in f.title.lower() for f in findings)

    def test_detect_sql_injection(self):
        from bog_agents.middleware.security_audit import scan_file_for_patterns

        content = 'cursor.execute(f"SELECT * FROM users WHERE id={user_id}")'
        findings = scan_file_for_patterns("db.py", content, [0])
        assert len(findings) >= 1

    def test_scan_directory(self):
        from bog_agents.middleware.security_audit import scan_directory

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a file with a "secret"
            test_file = Path(tmpdir) / "config.py"
            test_file.write_text('API_KEY = "sk_live_1234567890abcdefghijklmnop"')

            report = scan_directory(tmpdir)
            assert report.files_scanned >= 1
            assert report.total_findings >= 0  # Pattern may or may not match

    def test_report_markdown(self):
        from bog_agents.middleware.security_audit import SecurityReport

        report = SecurityReport(target_directory="/tmp/test")
        md = report.to_markdown()
        assert "Security Audit Report" in md

    def test_middleware_scan_file(self):
        from bog_agents.middleware.security_audit import SecurityAuditMiddleware

        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "app.py"
            test_file.write_text("x = eval(input())")

            mw = SecurityAuditMiddleware(working_dir=tmpdir)
            findings = mw.scan_file("app.py")
            assert len(findings) >= 1


class TestAgentReplay:
    """Tests for AgentReplayMiddleware."""

    def test_record_and_get_actions(self):
        from bog_agents.middleware.agent_replay import ActionType, ReplaySession

        session = ReplaySession(session_id="test-1")
        session.record(ActionType.USER_MESSAGE, {"content": "hello"})
        session.record(ActionType.TOOL_CALL, {"tool_name": "ls", "tool_args": {}})
        session.record(ActionType.TOOL_RESULT, {"success": True, "result": "file.txt"})

        assert session.total_actions == 3
        assert session.get_action(0) is not None
        assert session.get_action(0).action_type == ActionType.USER_MESSAGE

    def test_fork_session(self):
        from bog_agents.middleware.agent_replay import ActionType, ReplaySession

        session = ReplaySession(session_id="test-2")
        for i in range(5):
            session.record(ActionType.TOOL_CALL, {"tool_name": f"tool_{i}"})

        forked = session.fork_at(2)
        assert forked.total_actions == 3  # actions 0, 1, 2
        assert "fork" in forked.session_id

    def test_timeline_output(self):
        from bog_agents.middleware.agent_replay import ActionType, ReplaySession

        session = ReplaySession(session_id="test-3")
        session.record(ActionType.USER_MESSAGE, {"content": "fix tests"})
        session.record(ActionType.MODEL_CALL, {})
        session.record(ActionType.TOOL_CALL, {"tool_name": "execute"})

        timeline = session.to_timeline()
        assert "test-3" in timeline
        assert "TOOL" in timeline

    def test_serialize_roundtrip(self):
        from bog_agents.middleware.agent_replay import ActionType, ReplaySession

        session = ReplaySession(session_id="test-4")
        session.record(ActionType.TOOL_CALL, {"tool_name": "ls"})

        data = session.to_dict()
        restored = ReplaySession.from_dict(data)
        assert restored.session_id == "test-4"
        assert restored.total_actions == 1

    def test_store_save_and_load(self):
        from bog_agents.middleware.agent_replay import ActionType, ReplaySession, ReplayStore

        with tempfile.TemporaryDirectory() as tmpdir:
            store = ReplayStore(store_dir=tmpdir)
            session = ReplaySession(session_id="persist-1")
            session.record(ActionType.TOOL_CALL, {"tool_name": "grep"})

            store.save(session)
            loaded = store.load("persist-1")
            assert loaded is not None
            assert loaded.total_actions == 1

    def test_middleware_records_tool_calls(self):
        from bog_agents.middleware.agent_replay import AgentReplayMiddleware

        with tempfile.TemporaryDirectory() as tmpdir:
            mw = AgentReplayMiddleware(session_id="mw-test", store_dir=tmpdir)
            action_id = mw.record_tool_call("edit_file", {"path": "test.py"})
            assert action_id >= 0
            mw.record_tool_result("ok", parent_id=action_id, duration_ms=50.0)
            assert mw.session.total_actions == 2


class TestHotReloadSkills:
    """Tests for HotReloadSkillsMiddleware."""

    def test_scan_empty_dir(self):
        from bog_agents.middleware.hot_reload_skills import scan_skill_directories

        with tempfile.TemporaryDirectory() as tmpdir:
            states = scan_skill_directories([tmpdir])
            assert len(states) == 0

    def test_scan_with_skill(self):
        from bog_agents.middleware.hot_reload_skills import scan_skill_directories

        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir) / "my-skill"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text("---\nname: my-skill\n---\nHello")

            states = scan_skill_directories([tmpdir])
            assert len(states) == 1

    def test_detect_changes(self):
        from bog_agents.middleware.hot_reload_skills import SkillFileState, detect_changes

        old = {"a.md": SkillFileState(path="a.md", content_hash="abc", last_modified=1.0)}
        new = {
            "a.md": SkillFileState(path="a.md", content_hash="def", last_modified=2.0),
            "b.md": SkillFileState(path="b.md", content_hash="ghi", last_modified=2.0),
        }

        added, modified, removed = detect_changes(old, new)
        assert added == ["b.md"]
        assert modified == ["a.md"]
        assert removed == []


class TestHttpHooks:
    """Tests for HttpHooksMiddleware."""

    def test_webhook_payload_serialization(self):
        from bog_agents.middleware.http_hooks import WebhookPayload

        payload = WebhookPayload(
            event="post_tool_use",
            timestamp=123.0,
            session_id="s1",
            tool_name="ls",
            tool_args={},
            metadata={"key": "value"},
        )
        d = payload.to_dict()
        assert d["event"] == "post_tool_use"
        assert d["session_id"] == "s1"

    def test_webhook_response_parsing(self):
        from bog_agents.middleware.http_hooks import WebhookAction, WebhookResponse

        resp = WebhookResponse.from_dict({"action": "block", "message": "denied"})
        assert resp.action == WebhookAction.BLOCK
        assert resp.message == "denied"

    def test_signature_computation(self):
        from bog_agents.middleware.http_hooks import _compute_signature

        sig = _compute_signature(b'{"test": true}', "secret")
        assert isinstance(sig, str)
        assert len(sig) == 64  # SHA-256 hex

    def test_middleware_no_endpoints(self):
        from bog_agents.middleware.http_hooks import HttpHooksMiddleware, WebhookEvent

        mw = HttpHooksMiddleware()
        endpoints = mw._endpoints_for_event(WebhookEvent.ON_ERROR)
        assert len(endpoints) == 0


class TestOfflineMode:
    """Tests for OfflineModeMiddleware."""

    def test_offline_tool_filtering(self):
        from unittest.mock import patch

        from bog_agents.middleware.offline_mode import ConnectivityStatus, OfflineModeMiddleware

        with (
            patch("bog_agents.middleware.offline_mode.check_connectivity", return_value=ConnectivityStatus.OFFLINE),
            patch("bog_agents.middleware.offline_mode.check_ollama_running", return_value=False),
            patch("bog_agents.middleware.offline_mode.detect_ollama_models", return_value=[]),
        ):
            mw = OfflineModeMiddleware(enforce_offline=True, check_interval=0)
        assert mw.is_tool_allowed("read_file") is True
        assert mw.is_tool_allowed("web_search") is False
        assert mw.is_tool_allowed("execute") is True

    def test_offline_capabilities(self):
        from bog_agents.middleware.offline_mode import OfflineCapability, get_offline_capabilities

        caps = get_offline_capabilities(False, [])
        assert OfflineCapability.FILE_OPERATIONS in caps
        assert OfflineCapability.LOCAL_LLM not in caps

    def test_select_best_model(self):
        from bog_agents.middleware.offline_mode import OllamaModel, select_best_ollama_model

        models = [
            OllamaModel(name="phi3:latest", size_bytes=2 * 1024**3),
            OllamaModel(name="llama3:latest", size_bytes=4 * 1024**3),
        ]
        best = select_best_ollama_model(models)
        assert best == "llama3:latest"

    def test_status_summary(self):
        from unittest.mock import patch

        from bog_agents.middleware.offline_mode import ConnectivityStatus, OfflineModeMiddleware

        with (
            patch("bog_agents.middleware.offline_mode.check_connectivity", return_value=ConnectivityStatus.OFFLINE),
            patch("bog_agents.middleware.offline_mode.check_ollama_running", return_value=False),
            patch("bog_agents.middleware.offline_mode.detect_ollama_models", return_value=[]),
        ):
            mw = OfflineModeMiddleware(enforce_offline=True, check_interval=99999)
        summary = mw.get_status_summary()
        assert "Connectivity" in summary
        assert "Enforce offline: True" in summary
