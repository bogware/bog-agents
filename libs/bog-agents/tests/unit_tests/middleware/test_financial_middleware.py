"""Tests for financial advisor middleware modules.

Tests for features: #9 (Audit Trail), #10 (Citations), #31 (Hallucination Detection),
#34 (Reasoning Chain), #45 (Meeting Prep), #46 (Enhanced Skills), #47 (Saved Prompts).
"""

from __future__ import annotations


class TestAuditTrailMiddleware:
    """Tests for AuditTrailMiddleware (#9)."""

    def test_init(self) -> None:
        """Test middleware initialization."""
        from bog_agents.middleware.audit_trail import AuditTrailMiddleware

        mw = AuditTrailMiddleware(session_id="test-123", advisor_id="FA-001")
        assert len(mw.tools) == 3
        assert mw.audit_log.session_id == "test-123"
        assert mw.audit_log.advisor_id == "FA-001"

    def test_tool_names(self) -> None:
        """Test that expected tools are registered."""
        from bog_agents.middleware.audit_trail import AuditTrailMiddleware

        mw = AuditTrailMiddleware()
        names = {t.name for t in mw.tools}
        assert "audit_log" in names
        assert "export_audit_log" in names
        assert "add_audit_note" in names

    def test_audit_log_add_entry(self) -> None:
        """Test adding entries to the audit log."""
        from bog_agents.middleware.audit_trail import AuditLog

        log = AuditLog(session_id="test", advisor_id="FA-001")
        entry = log.add_entry(
            action_type="llm_call",
            description="Test LLM call",
            data_sources=["SEC EDGAR"],
            reasoning="Testing audit trail",
        )
        assert entry.entry_id == 1
        assert entry.action_type == "llm_call"
        assert log.entry_count == 1

    def test_audit_log_immutable_ordering(self) -> None:
        """Test that entries are ordered by insertion."""
        from bog_agents.middleware.audit_trail import AuditLog

        log = AuditLog()
        log.add_entry(action_type="first", description="First entry")
        log.add_entry(action_type="second", description="Second entry")
        log.add_entry(action_type="third", description="Third entry")

        assert log.entry_count == 3
        assert log.entries[0].entry_id == 1
        assert log.entries[2].entry_id == 3

    def test_audit_log_format_summary(self) -> None:
        """Test formatted summary output."""
        from bog_agents.middleware.audit_trail import AuditLog

        log = AuditLog(session_id="sess-1", advisor_id="FA-001")
        log.add_entry(action_type="llm_call", description="Analyzed filing")
        summary = log.format_summary()
        assert "Compliance Audit Trail" in summary
        assert "sess-1" in summary
        assert "Analyzed filing" in summary

    def test_audit_log_export_json(self) -> None:
        """Test JSON export for regulatory submission."""
        import json

        from bog_agents.middleware.audit_trail import AuditLog

        log = AuditLog(session_id="test", advisor_id="FA-001")
        log.add_entry(action_type="tool_call", description="Test export")
        exported = log.export_json()
        data = json.loads(exported)
        assert data["session_id"] == "test"
        assert len(data["entries"]) == 1

    def test_audit_log_last_n(self) -> None:
        """Test viewing only the last N entries."""
        from bog_agents.middleware.audit_trail import AuditLog

        log = AuditLog()
        for i in range(10):
            log.add_entry(action_type="test", description=f"Entry {i}")

        summary = log.format_summary(last_n=3)
        assert "Entry 7" in summary
        assert "Entry 9" in summary
        assert "Entry 0" not in summary


class TestCitationsMiddleware:
    """Tests for CitationsMiddleware (#10)."""

    def test_init(self) -> None:
        """Test middleware initialization."""
        from bog_agents.middleware.citations import CitationsMiddleware

        mw = CitationsMiddleware()
        assert len(mw.tools) == 3

    def test_tool_names(self) -> None:
        """Test that expected tools are registered."""
        from bog_agents.middleware.citations import CitationsMiddleware

        mw = CitationsMiddleware()
        names = {t.name for t in mw.tools}
        assert "register_source" in names
        assert "add_citation" in names
        assert "show_bibliography" in names

    def test_register_source(self) -> None:
        """Test source registration."""
        from bog_agents.middleware.citations import CitationRegistry

        registry = CitationRegistry()
        source = registry.register_source(
            title="Apple 10-K Filing",
            source_type="filing",
            url="https://sec.gov/cgi-bin/...",
            author="Apple Inc.",
            date="2024-11-01",
        )
        assert source.source_id == 1
        assert source.title == "Apple 10-K Filing"

    def test_add_citation(self) -> None:
        """Test adding citations to registered sources."""
        from bog_agents.middleware.citations import CitationRegistry

        registry = CitationRegistry()
        registry.register_source(title="Test Source")
        citation = registry.add_citation(
            source_id=1,
            claim="Revenue was $100M",
            relation="supports",
            confidence="high",
        )
        assert citation is not None
        assert citation.citation_id == 1
        assert citation.relation == "supports"

    def test_citation_invalid_source(self) -> None:
        """Test that citing an invalid source returns None."""
        from bog_agents.middleware.citations import CitationRegistry

        registry = CitationRegistry()
        result = registry.add_citation(source_id=999, claim="Test")
        assert result is None

    def test_format_bibliography(self) -> None:
        """Test formatted bibliography output."""
        from bog_agents.middleware.citations import CitationRegistry

        registry = CitationRegistry()
        registry.register_source(title="Test Source", source_type="report")
        registry.add_citation(source_id=1, claim="Test claim", relation="supports")
        bib = registry.format_bibliography()
        assert "Bibliography" in bib
        assert "Test Source" in bib
        assert "Test claim" in bib

    def test_citation_relations(self) -> None:
        """Test different citation relations are formatted correctly."""
        from bog_agents.middleware.citations import CitationRegistry

        registry = CitationRegistry()
        registry.register_source(title="Source A")
        registry.add_citation(source_id=1, claim="Claim 1", relation="supports")
        registry.add_citation(source_id=1, claim="Claim 2", relation="contradicts")
        registry.add_citation(source_id=1, claim="Claim 3", relation="mentions")
        bib = registry.format_bibliography()
        assert "Cited 3 time(s)" in bib


class TestReasoningChainMiddleware:
    """Tests for ReasoningChainMiddleware (#34)."""

    def test_init(self) -> None:
        """Test middleware initialization."""
        from bog_agents.middleware.reasoning_chain import ReasoningChainMiddleware

        mw = ReasoningChainMiddleware()
        assert len(mw.tools) == 5

    def test_tool_names(self) -> None:
        """Test that expected tools are registered."""
        from bog_agents.middleware.reasoning_chain import ReasoningChainMiddleware

        mw = ReasoningChainMiddleware()
        names = {t.name for t in mw.tools}
        assert "add_reasoning_step" in names
        assert "set_conclusion" in names
        assert "show_reasoning" in names
        assert "reasoning_graph" in names
        assert "clear_reasoning" in names

    def test_add_step(self) -> None:
        """Test adding reasoning steps."""
        from bog_agents.middleware.reasoning_chain import ReasoningChain

        chain = ReasoningChain()
        step = chain.add_step(
            step_type="observation",
            content="Revenue increased 15% YoY",
            data_sources=["10-K Filing"],
            confidence=0.95,
        )
        assert step.step_id == 1
        assert step.step_type == "observation"

    def test_dependencies(self) -> None:
        """Test step dependencies."""
        from bog_agents.middleware.reasoning_chain import ReasoningChain

        chain = ReasoningChain()
        chain.add_step(step_type="observation", content="Fact A")
        chain.add_step(step_type="observation", content="Fact B")
        step3 = chain.add_step(
            step_type="inference",
            content="Therefore C",
            depends_on=[1, 2],
        )
        assert step3.depends_on == [1, 2]

    def test_overall_confidence(self) -> None:
        """Test that overall confidence is the minimum."""
        from bog_agents.middleware.reasoning_chain import ReasoningChain

        chain = ReasoningChain()
        chain.add_step(step_type="observation", content="A", confidence=0.9)
        chain.add_step(step_type="assumption", content="B", confidence=0.3)
        chain.add_step(step_type="inference", content="C", confidence=0.7)
        assert chain.overall_confidence == 0.3

    def test_format_chain(self) -> None:
        """Test formatted chain output."""
        from bog_agents.middleware.reasoning_chain import ReasoningChain

        chain = ReasoningChain()
        chain.add_step(step_type="observation", content="Test observation")
        chain.set_conclusion("Test conclusion")
        output = chain.format_chain()
        assert "Reasoning Chain" in output
        assert "Test observation" in output
        assert "Test conclusion" in output

    def test_format_graph(self) -> None:
        """Test reasoning graph output."""
        from bog_agents.middleware.reasoning_chain import ReasoningChain

        chain = ReasoningChain()
        chain.add_step(step_type="observation", content="Data point")
        chain.add_step(step_type="inference", content="Conclusion", depends_on=[1])
        graph = chain.format_graph()
        assert "Reasoning Graph" in graph

    def test_clear(self) -> None:
        """Test clearing the chain."""
        from bog_agents.middleware.reasoning_chain import ReasoningChain

        chain = ReasoningChain()
        chain.add_step(step_type="observation", content="Test")
        chain.set_conclusion("Done")
        chain.clear()
        assert len(chain.steps) == 0
        assert chain.conclusion == ""


class TestHallucinationDetectionMiddleware:
    """Tests for HallucinationDetectionMiddleware (#31)."""

    def test_init(self) -> None:
        """Test middleware initialization."""
        from bog_agents.middleware.hallucination_detection import HallucinationDetectionMiddleware

        mw = HallucinationDetectionMiddleware()
        assert len(mw.tools) == 3

    def test_tool_names(self) -> None:
        """Test that expected tools are registered."""
        from bog_agents.middleware.hallucination_detection import HallucinationDetectionMiddleware

        mw = HallucinationDetectionMiddleware()
        names = {t.name for t in mw.tools}
        assert "register_fact" in names
        assert "verify_claim" in names
        assert "verification_report" in names

    def test_register_fact(self) -> None:
        """Test fact registration."""
        from bog_agents.middleware.hallucination_detection import FactDatabase

        db = FactDatabase()
        fact = db.register_fact(
            content="AAPL revenue was $383B in FY2023",
            source="Apple 10-K Filing",
            category="financial",
            numerical_value=383e9,
            unit="USD",
        )
        assert fact.fact_id == 1
        assert fact.numerical_value == 383e9

    def test_check_claim_verified(self) -> None:
        """Test verifying a claim with matching facts."""
        from bog_agents.middleware.hallucination_detection import FactDatabase

        db = FactDatabase()
        db.register_fact(content="AAPL revenue $383B", source="10-K")
        check = db.check_claim(
            claim="Apple's revenue was approximately $383B",
            matching_fact_ids=[1],
            confidence=0.9,
        )
        assert check.status == "verified"

    def test_check_claim_contradicted(self) -> None:
        """Test a contradicted claim."""
        from bog_agents.middleware.hallucination_detection import FactDatabase

        db = FactDatabase()
        db.register_fact(content="AAPL revenue $383B", source="10-K")
        check = db.check_claim(
            claim="Apple's revenue was $500B",
            contradicting_fact_ids=[1],
            notes="Actual was $383B, not $500B",
        )
        assert check.status == "contradicted"

    def test_trust_score(self) -> None:
        """Test trust score calculation."""
        from bog_agents.middleware.hallucination_detection import FactDatabase

        db = FactDatabase()
        db.register_fact(content="Fact 1", source="Source 1")
        db.check_claim(claim="Verified claim", matching_fact_ids=[1], confidence=0.9)
        db.check_claim(claim="Unverified claim", confidence=0.1)
        assert db.trust_score == 0.5

    def test_format_report(self) -> None:
        """Test formatted report output."""
        from bog_agents.middleware.hallucination_detection import FactDatabase

        db = FactDatabase()
        db.register_fact(content="Test fact", source="Test source")
        db.check_claim(claim="Verified", matching_fact_ids=[1], confidence=0.9)
        db.check_claim(claim="Not verified", confidence=0.1)
        report = db.format_report()
        assert "Hallucination Detection Report" in report
        assert "Trust Score: 50%" in report

    def test_verification_stats(self) -> None:
        """Test verification statistics."""
        from bog_agents.middleware.hallucination_detection import FactDatabase

        db = FactDatabase()
        db.register_fact(content="Fact", source="Src")
        db.check_claim(claim="A", matching_fact_ids=[1])
        db.check_claim(claim="B", contradicting_fact_ids=[1])
        db.check_claim(claim="C")
        stats = db.verification_stats
        assert stats["verified"] == 1
        assert stats["contradicted"] == 1
        assert stats["unverified"] == 1


class TestMeetingPrepMiddleware:
    """Tests for MeetingPrepMiddleware (#45)."""

    def test_init(self) -> None:
        """Test middleware initialization."""
        from bog_agents.middleware.meeting_prep import MeetingPrepMiddleware

        mw = MeetingPrepMiddleware()
        assert len(mw.tools) == 9

    def test_tool_names(self) -> None:
        """Test that expected tools are registered."""
        from bog_agents.middleware.meeting_prep import MeetingPrepMiddleware

        mw = MeetingPrepMiddleware()
        names = {t.name for t in mw.tools}
        assert "set_client_profile" in names
        assert "set_meeting_date" in names
        assert "set_portfolio_summary" in names
        assert "set_market_summary" in names
        assert "add_talking_point" in names
        assert "add_action_item" in names
        assert "add_compliance_note" in names
        assert "generate_briefing" in names
        assert "reset_briefing" in names

    def test_client_profile(self) -> None:
        """Test client profile data structure."""
        from bog_agents.middleware.meeting_prep import ClientProfile

        profile = ClientProfile(
            name="John Smith",
            account_id="ACC-001",
            risk_tolerance="moderate",
            investment_objectives="Growth with income",
        )
        assert profile.name == "John Smith"
        assert profile.risk_tolerance == "moderate"

    def test_talking_point(self) -> None:
        """Test talking point data structure."""
        from bog_agents.middleware.meeting_prep import TalkingPoint

        tp = TalkingPoint(
            topic="Portfolio Rebalancing",
            content="Tech allocation has drifted to 45%, target is 35%",
            priority="high",
            supporting_data=["Current: 45%", "Target: 35%"],
            action_required=True,
        )
        assert tp.priority == "high"
        assert tp.action_required is True
        assert len(tp.supporting_data) == 2

    def test_briefing_format(self) -> None:
        """Test complete briefing formatting."""
        from bog_agents.middleware.meeting_prep import ClientProfile, MeetingBriefing, TalkingPoint

        briefing = MeetingBriefing(
            client=ClientProfile(
                name="Jane Doe",
                account_id="ACC-002",
                risk_tolerance="conservative",
                investment_objectives="Capital preservation",
            ),
            meeting_date="2026-03-20",
            portfolio_summary="Portfolio up 8% YTD",
            market_summary="S&P 500 up 12% YTD",
            talking_points=[
                TalkingPoint(topic="Performance", content="Outperforming", priority="high"),
                TalkingPoint(topic="Bonds", content="Consider extending duration", priority="medium"),
            ],
            action_items=["Rebalance fixed income allocation"],
            compliance_notes=["Past performance does not guarantee future results"],
            prepared_at="2026-03-15T10:00:00",
        )
        output = briefing.format_briefing()
        assert "Meeting Briefing" in output
        assert "Jane Doe" in output
        assert "Portfolio up 8% YTD" in output
        assert "Performance" in output
        assert "Rebalance fixed income" in output
        assert "Past performance" in output

    def test_talking_points_priority_sort(self) -> None:
        """Test that talking points are sorted by priority."""
        from bog_agents.middleware.meeting_prep import MeetingBriefing, TalkingPoint

        briefing = MeetingBriefing()
        briefing.talking_points = [
            TalkingPoint(topic="Low", content="...", priority="low"),
            TalkingPoint(topic="High", content="...", priority="high"),
            TalkingPoint(topic="Medium", content="...", priority="medium"),
        ]
        priority_order = {"high": 0, "medium": 1, "low": 2}
        briefing.talking_points.sort(key=lambda t: priority_order.get(t.priority, 1))
        assert briefing.talking_points[0].topic == "High"
        assert briefing.talking_points[2].topic == "Low"


class TestSavedPromptsMiddleware:
    """Tests for SavedPromptsMiddleware (#47)."""

    def test_init(self) -> None:
        """Test middleware initialization."""
        from bog_agents.middleware.saved_prompts import SavedPromptsMiddleware

        mw = SavedPromptsMiddleware(backend=lambda rt: None, sources=["/prompts/"])
        assert len(mw.tools) == 3

    def test_tool_names(self) -> None:
        """Test that expected tools are registered."""
        from bog_agents.middleware.saved_prompts import SavedPromptsMiddleware

        mw = SavedPromptsMiddleware(backend=lambda rt: None, sources=["/prompts/"])
        names = {t.name for t in mw.tools}
        assert "list_prompts" in names
        assert "get_prompt" in names
        assert "use_prompt" in names

    def test_parse_prompt_file(self) -> None:
        """Test prompt file parsing."""
        from bog_agents.middleware.saved_prompts import _parse_prompt_file

        content = """---
name: test-prompt
description: A test prompt
category: testing
variables:
  - client_name
  - date
---

Hello {{client_name}}, this is your report for {{date}}.
"""
        result = _parse_prompt_file(content, "/test.md")
        assert result is not None
        assert result["name"] == "test-prompt"
        assert result["description"] == "A test prompt"
        assert result["category"] == "testing"
        assert result["variables"] == ["client_name", "date"]
        assert "{{client_name}}" in result["content"]

    def test_parse_prompt_file_no_frontmatter(self) -> None:
        """Test that files without frontmatter are skipped."""
        from bog_agents.middleware.saved_prompts import _parse_prompt_file

        result = _parse_prompt_file("Just plain text", "/test.md")
        assert result is None

    def test_render_template(self) -> None:
        """Test template variable substitution."""
        from bog_agents.middleware.saved_prompts import _render_template

        template = "Hello {{name}}, your account is {{account_id}}."
        result = _render_template(template, {"name": "John", "account_id": "ACC-001"})
        assert result == "Hello John, your account is ACC-001."


class TestEnhancedSkillsMiddleware:
    """Tests for EnhancedSkillsMiddleware (#46)."""

    def test_init(self) -> None:
        """Test middleware initialization."""
        from bog_agents.middleware.enhanced_skills import EnhancedSkillsMiddleware

        mw = EnhancedSkillsMiddleware(
            backend=lambda rt: None,
            sources=["/skills/local/"],
        )
        assert len(mw.tools) == 2

    def test_tool_names(self) -> None:
        """Test that expected tools are registered."""
        from bog_agents.middleware.enhanced_skills import EnhancedSkillsMiddleware

        mw = EnhancedSkillsMiddleware(backend=lambda rt: None, sources=["/skills/"])
        names = {t.name for t in mw.tools}
        assert "refresh_skills" in names
        assert "list_skill_sources" in names

    def test_source_detection(self) -> None:
        """Test source type detection."""
        from bog_agents.middleware.enhanced_skills import _is_git_source, _is_http_source

        assert _is_git_source("git+https://github.com/org/repo.git")
        assert _is_git_source("git://github.com/org/repo.git")
        assert not _is_git_source("/local/path/")
        assert not _is_git_source("https://example.com/")

        assert _is_http_source("https://example.com/skills/")
        assert _is_http_source("http://localhost:8080/skills/")
        assert not _is_http_source("/local/path/")
        assert not _is_http_source("git+https://github.com/org/repo.git")

    def test_cache_key_deterministic(self) -> None:
        """Test that cache keys are deterministic."""
        from bog_agents.middleware.enhanced_skills import _cache_key

        key1 = _cache_key("https://github.com/org/repo.git")
        key2 = _cache_key("https://github.com/org/repo.git")
        assert key1 == key2
        assert len(key1) == 16

    def test_cache_key_unique(self) -> None:
        """Test that different sources get different cache keys."""
        from bog_agents.middleware.enhanced_skills import _cache_key

        key1 = _cache_key("https://github.com/org/repo1.git")
        key2 = _cache_key("https://github.com/org/repo2.git")
        assert key1 != key2
