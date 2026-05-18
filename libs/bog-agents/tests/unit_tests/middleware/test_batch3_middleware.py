"""Tests for batch 3 financial advisor middleware modules.

Tests for features: #7 (Code Review), #11 (Financial Data), #13 (Regulatory Alerts),
#15 (Model Portfolio), #17 (Knowledge Graph), #20 (Client Knowledge Base),
#21 (RBAC), #32 (Fact Check), #35 (Approval Gates), #37 (Earnings Analysis),
#38 (Regulatory Impact).
"""

from __future__ import annotations

from typing import Any, ClassVar


class TestCodeReviewMiddleware:
    """Tests for CodeReviewMiddleware (#7)."""

    def test_init(self) -> None:
        from bog_agents.middleware.code_review import CodeReviewMiddleware

        mw = CodeReviewMiddleware()
        assert len(mw.tools) == 5
        assert mw.session.submitted_content == ""
        assert mw.session.overall_status == "pending"

    def test_tool_names(self) -> None:
        from bog_agents.middleware.code_review import CodeReviewMiddleware

        mw = CodeReviewMiddleware()
        names = {t.name for t in mw.tools}
        assert names == {
            "submit_for_review",
            "add_review_comment",
            "review_checklist",
            "review_summary",
            "clear_review",
        }

    def test_review_comment_dataclass(self) -> None:
        from bog_agents.middleware.code_review import ReviewComment

        comment = ReviewComment(
            comment_id=1,
            section="Methodology",
            content="Needs more detail",
            severity="warning",
            reviewer="analyst",
        )
        assert comment.comment_id == 1
        assert comment.section == "Methodology"
        assert comment.severity == "warning"
        assert comment.reviewer == "analyst"

    def test_add_comment(self) -> None:
        from bog_agents.middleware.code_review import ReviewSession

        session = ReviewSession(submitted_content="Test research output")
        comment = session.add_comment(
            section="Data Sources",
            content="Missing primary source",
            severity="error",
            reviewer="compliance",
        )
        assert comment.comment_id == 1
        assert comment.severity == "error"
        assert len(session.comments) == 1

    def test_generate_checklist(self) -> None:
        from bog_agents.middleware.code_review import ReviewSession

        session = ReviewSession(submitted_content="Test content")
        items = session.generate_checklist()
        assert len(items) == 6
        assert "Sources cited?" in items
        assert "Compliance checked?" in items

    def test_format_summary(self) -> None:
        from bog_agents.middleware.code_review import ReviewSession

        session = ReviewSession(submitted_content="Revenue grew 15% YoY based on 10-K filings.")
        session.add_comment(section="Sources", content="Good citation", severity="info")
        session.add_comment(section="Data", content="Stale numbers", severity="error")
        session.overall_status = "needs_revision"
        summary = session.format_summary()
        assert "Review Summary" in summary
        assert "needs_revision" in summary
        assert "1 errors" in summary
        assert "Stale numbers" in summary

    def test_overall_status_tracking(self) -> None:
        from bog_agents.middleware.code_review import ReviewSession

        session = ReviewSession()
        assert session.overall_status == "pending"
        session.overall_status = "approved"
        assert session.overall_status == "approved"

    def test_format_summary_no_content(self) -> None:
        from bog_agents.middleware.code_review import ReviewSession

        session = ReviewSession()
        assert session.format_summary() == "No content submitted for review."


class TestModelPortfolioMiddleware:
    """Tests for ModelPortfolioMiddleware (#15)."""

    def test_init(self) -> None:
        from bog_agents.middleware.model_portfolio import ModelPortfolioMiddleware

        mw = ModelPortfolioMiddleware()
        assert len(mw.tools) == 5

    def test_tool_names(self) -> None:
        from bog_agents.middleware.model_portfolio import ModelPortfolioMiddleware

        mw = ModelPortfolioMiddleware()
        names = {t.name for t in mw.tools}
        assert names == {
            "create_model_portfolio",
            "add_target_allocation",
            "set_current_allocation",
            "rebalancing_proposal",
            "clear_model_portfolio",
        }

    def test_add_allocation(self) -> None:
        from bog_agents.middleware.model_portfolio import ModelPortfolio

        portfolio = ModelPortfolio(name="Growth")
        alloc = portfolio.add_allocation("US Equity", target_pct=60.0, min_pct=50.0, max_pct=70.0)
        assert alloc.asset_class == "US Equity"
        assert alloc.target_pct == 60.0
        assert len(portfolio.allocations) == 1

    def test_set_current(self) -> None:
        from bog_agents.middleware.model_portfolio import ModelPortfolio

        portfolio = ModelPortfolio(name="Growth")
        portfolio.add_allocation("Bonds", target_pct=40.0)
        assert portfolio.set_current("Bonds", 35.0) is True
        assert portfolio.set_current("Cash", 5.0) is False
        assert portfolio.allocations[0].current_pct == 35.0

    def test_generate_rebalance(self) -> None:
        from bog_agents.middleware.model_portfolio import ModelPortfolio

        portfolio = ModelPortfolio(name="Balanced")
        portfolio.add_allocation("US Equity", target_pct=60.0)
        portfolio.add_allocation("Bonds", target_pct=40.0)
        portfolio.set_current("US Equity", 70.0)
        portfolio.set_current("Bonds", 30.0)
        trades = portfolio.generate_rebalance()
        assert len(trades) == 2
        equity_trade = next(t for t in trades if t.asset_class == "US Equity")
        assert equity_trade.direction == "sell"
        assert abs(equity_trade.amount_pct - 10.0) < 1e-9

    def test_is_within_bounds(self) -> None:
        from bog_agents.middleware.model_portfolio import ModelPortfolio

        portfolio = ModelPortfolio(name="Test")
        portfolio.add_allocation("Equity", target_pct=60.0, min_pct=50.0, max_pct=70.0)
        portfolio.set_current("Equity", 65.0)
        assert portfolio.is_within_bounds() is True
        portfolio.set_current("Equity", 80.0)
        assert portfolio.is_within_bounds() is False

    def test_format_proposal(self) -> None:
        from bog_agents.middleware.model_portfolio import ModelPortfolio

        portfolio = ModelPortfolio(name="Conservative")
        portfolio.add_allocation("Bonds", target_pct=70.0, min_pct=60.0, max_pct=80.0)
        portfolio.add_allocation("Equity", target_pct=30.0, min_pct=20.0, max_pct=40.0)
        portfolio.set_current("Bonds", 65.0)
        portfolio.set_current("Equity", 35.0)
        output = portfolio.format_proposal()
        assert "Rebalancing Proposal" in output
        assert "Conservative" in output
        assert "Proposed Trades" in output

    def test_format_proposal_empty(self) -> None:
        from bog_agents.middleware.model_portfolio import ModelPortfolio

        portfolio = ModelPortfolio(name="Empty")
        assert portfolio.format_proposal() == "No allocations defined."


class TestKnowledgeGraphMiddleware:
    """Tests for KnowledgeGraphMiddleware (#17)."""

    def test_init(self) -> None:
        from bog_agents.middleware.knowledge_graph import KnowledgeGraphMiddleware

        mw = KnowledgeGraphMiddleware()
        assert len(mw.tools) == 5

    def test_tool_names(self) -> None:
        from bog_agents.middleware.knowledge_graph import KnowledgeGraphMiddleware

        mw = KnowledgeGraphMiddleware()
        names = {t.name for t in mw.tools}
        assert names == {
            "add_entity",
            "add_relationship",
            "query_entity",
            "graph_summary",
            "clear_graph",
        }

    def test_add_entity(self) -> None:
        from bog_agents.middleware.knowledge_graph import KnowledgeGraph

        graph = KnowledgeGraph()
        entity = graph.add_entity(name="Apple Inc.", entity_type="company", attributes={"ticker": "AAPL"})
        assert entity.entity_id == 1
        assert entity.name == "Apple Inc."
        assert entity.attributes["ticker"] == "AAPL"

    def test_add_entity_updates_existing(self) -> None:
        from bog_agents.middleware.knowledge_graph import KnowledgeGraph

        graph = KnowledgeGraph()
        graph.add_entity(name="Apple", entity_type="company")
        updated = graph.add_entity(name="Apple", entity_type="tech_company", attributes={"sector": "Tech"})
        assert updated.entity_type == "tech_company"
        assert updated.attributes["sector"] == "Tech"
        assert len(graph.entities) == 1

    def test_add_relationship(self) -> None:
        from bog_agents.middleware.knowledge_graph import KnowledgeGraph

        graph = KnowledgeGraph()
        graph.add_entity(name="Apple", entity_type="company")
        graph.add_entity(name="Tim Cook", entity_type="person")
        rel = graph.add_relationship(
            from_entity="Tim Cook",
            to_entity="Apple",
            relationship_type="manages",
            properties={"since": "2011"},
        )
        assert rel.from_entity == "Tim Cook"
        assert rel.relationship_type == "manages"
        assert rel.properties["since"] == "2011"

    def test_get_entity_relationships(self) -> None:
        from bog_agents.middleware.knowledge_graph import KnowledgeGraph

        graph = KnowledgeGraph()
        graph.add_entity(name="A", entity_type="company")
        graph.add_entity(name="B", entity_type="company")
        graph.add_entity(name="C", entity_type="company")
        graph.add_relationship(from_entity="A", to_entity="B", relationship_type="competes_with")
        graph.add_relationship(from_entity="C", to_entity="A", relationship_type="supplies")
        rels = graph.get_entity_relationships("A")
        assert len(rels) == 2

    def test_format_summary(self) -> None:
        from bog_agents.middleware.knowledge_graph import KnowledgeGraph

        graph = KnowledgeGraph()
        graph.add_entity(name="Apple", entity_type="company")
        graph.add_entity(name="SEC", entity_type="organization")
        graph.add_relationship(from_entity="SEC", to_entity="Apple", relationship_type="regulates")
        summary = graph.format_summary()
        assert "Knowledge Graph Summary" in summary
        assert "Entities: 2" in summary
        assert "Relationships: 1" in summary

    def test_format_summary_empty(self) -> None:
        from bog_agents.middleware.knowledge_graph import KnowledgeGraph

        graph = KnowledgeGraph()
        assert "empty" in graph.format_summary().lower()


class TestRBACMiddleware:
    """Tests for RBACMiddleware (#21)."""

    def test_init(self) -> None:
        from bog_agents.middleware.rbac import RBACMiddleware

        mw = RBACMiddleware()
        assert len(mw.tools) == 4

    def test_tool_names(self) -> None:
        from bog_agents.middleware.rbac import RBACMiddleware

        mw = RBACMiddleware()
        names = {t.name for t in mw.tools}
        assert names == {"define_role", "set_active_role", "check_permission", "list_roles"}

    def test_role_can_use_tool_wildcard(self) -> None:
        from bog_agents.middleware.rbac import Role

        role = Role(name="admin", allowed_tools=["*"])
        assert role.can_use_tool("any_tool") is True
        assert role.can_use_tool("delete_everything") is True

    def test_role_can_use_tool_pattern(self) -> None:
        from bog_agents.middleware.rbac import Role

        role = Role(name="reader", allowed_tools=["read_*", "list_*"])
        assert role.can_use_tool("read_portfolio") is True
        assert role.can_use_tool("list_clients") is True
        assert role.can_use_tool("delete_client") is False

    def test_denied_takes_precedence(self) -> None:
        from bog_agents.middleware.rbac import Role

        role = Role(name="restricted", allowed_tools=["*"], denied_tools=["delete_*", "clear_*"])
        assert role.can_use_tool("read_data") is True
        assert role.can_use_tool("delete_client") is False
        assert role.can_use_tool("clear_portfolio") is False

    def test_define_and_set_active(self) -> None:
        from bog_agents.middleware.rbac import RBACStore

        store = RBACStore()
        store.define_role(name="analyst", allowed_tools=["read_*", "query_*"])
        role = store.set_active("analyst")
        assert role is not None
        assert store.active_role == "analyst"
        assert store.set_active("nonexistent") is None

    def test_is_allowed(self) -> None:
        from bog_agents.middleware.rbac import RBACStore

        store = RBACStore()
        store.define_role(name="viewer", allowed_tools=["read_*"], denied_tools=["read_secret"])
        store.set_active("viewer")
        assert store.is_allowed("read_portfolio") is True
        assert store.is_allowed("read_secret") is False
        assert store.is_allowed("delete_data") is False

    def test_no_active_role_allows_all(self) -> None:
        from bog_agents.middleware.rbac import RBACStore

        store = RBACStore()
        assert store.is_allowed("any_tool") is True

    def test_modify_request_strips_denied_tools(self) -> None:
        from types import SimpleNamespace

        from bog_agents.middleware.rbac import RBACMiddleware

        mw = RBACMiddleware()
        mw.store.define_role(name="reader", allowed_tools=["read_*"], denied_tools=["delete_*"])
        mw.store.set_active("reader")

        captured: dict[str, object] = {}

        class FakeRequest:
            tools: ClassVar[list] = [
                SimpleNamespace(name="read_file"),
                SimpleNamespace(name="delete_file"),
                SimpleNamespace(name="write_file"),
            ]
            system_message = None

            def override(self, **kwargs: object) -> Any:
                captured.update(kwargs)
                return self

        modified = mw.modify_request(FakeRequest())  # type: ignore[arg-type]
        names = {getattr(t, "name", None) for t in captured["tools"]}  # type: ignore[union-attr]
        # read_file is allowed; delete_file is denied; write_file lacks an allow match.
        # RBAC admin tools are also implicitly always allowed but aren't in this fake request.
        assert "read_file" in names
        assert "delete_file" not in names
        assert "write_file" not in names
        assert modified is not None

    def test_modify_request_preserves_admin_tools(self) -> None:
        from types import SimpleNamespace

        from bog_agents.middleware.rbac import RBACMiddleware

        mw = RBACMiddleware()
        mw.store.define_role(name="locked", allowed_tools=[])  # nothing allowed
        mw.store.set_active("locked")

        captured: dict[str, object] = {}

        class FakeRequest:
            tools: ClassVar[list] = [
                SimpleNamespace(name="define_role"),
                SimpleNamespace(name="set_active_role"),
                SimpleNamespace(name="forbidden_tool"),
            ]
            system_message = None

            def override(self, **kwargs: object) -> Any:
                captured.update(kwargs)
                return self

        mw.modify_request(FakeRequest())  # type: ignore[arg-type]
        names = {getattr(t, "name", None) for t in captured["tools"]}  # type: ignore[union-attr]
        assert "define_role" in names
        assert "set_active_role" in names
        assert "forbidden_tool" not in names

    def test_modify_request_no_active_role_passes_through(self) -> None:
        from types import SimpleNamespace

        from bog_agents.middleware.rbac import RBACMiddleware

        mw = RBACMiddleware()
        captured: dict[str, object] = {}

        class FakeRequest:
            tools: ClassVar[list] = [SimpleNamespace(name="anything")]
            system_message = None

            def override(self, **kwargs: object) -> Any:
                captured.update(kwargs)
                return self

        mw.modify_request(FakeRequest())  # type: ignore[arg-type]
        # Without an active role, tools must not be stripped.
        assert "tools" not in captured


class TestFactCheckMiddleware:
    """Tests for FactCheckMiddleware (#32)."""

    def test_init(self) -> None:
        from bog_agents.middleware.fact_check import FactCheckMiddleware

        mw = FactCheckMiddleware()
        assert len(mw.tools) == 5

    def test_tool_names(self) -> None:
        from bog_agents.middleware.fact_check import FactCheckMiddleware

        mw = FactCheckMiddleware()
        names = {t.name for t in mw.tools}
        assert names == {
            "submit_claim",
            "add_evidence",
            "verdict",
            "fact_check_report",
            "clear_fact_checks",
        }

    def test_submit_claim(self) -> None:
        from bog_agents.middleware.fact_check import FactCheckStore

        store = FactCheckStore()
        claim = store.submit(text="Revenue was $383B", source="10-K", category="financial")
        assert claim.claim_id == 1
        assert claim.text == "Revenue was $383B"
        assert claim.verdict == "pending"

    def test_get_claim(self) -> None:
        from bog_agents.middleware.fact_check import FactCheckStore

        store = FactCheckStore()
        store.submit(text="Test", source="src", category="general")
        assert store.get(1) is not None
        assert store.get(999) is None

    def test_add_evidence(self) -> None:
        from bog_agents.middleware.fact_check import FactCheckStore

        store = FactCheckStore()
        store.submit(text="Revenue was $383B", source="analyst", category="financial")
        ev = store.add_evidence(claim_id=1, text="10-K confirms $383B", source="SEC EDGAR", supports=True)
        assert ev is not None
        assert ev.supports is True
        assert store.add_evidence(claim_id=999, text="X", source="Y", supports=True) is None

    def test_set_verdict(self) -> None:
        from bog_agents.middleware.fact_check import FactCheckStore

        store = FactCheckStore()
        store.submit(text="Test claim", source="src", category="general")
        claim = store.set_verdict(claim_id=1, verdict="verified", explanation="Confirmed by 10-K")
        assert claim is not None
        assert claim.verdict == "verified"
        assert store.set_verdict(claim_id=999, verdict="false", explanation="N/A") is None

    def test_format_report(self) -> None:
        from bog_agents.middleware.fact_check import FactCheckStore

        store = FactCheckStore()
        store.submit(text="Claim A", source="src", category="financial")
        store.submit(text="Claim B", source="src", category="legal")
        store.add_evidence(claim_id=1, text="Evidence", source="10-K", supports=True)
        store.set_verdict(claim_id=1, verdict="verified", explanation="Confirmed")
        report = store.format_report()
        assert "Fact-Check Report" in report
        assert "Verified: 1" in report
        assert "Pending: 1" in report

    def test_format_report_empty(self) -> None:
        from bog_agents.middleware.fact_check import FactCheckStore

        store = FactCheckStore()
        assert "No claims submitted" in store.format_report()


class TestApprovalGatesMiddleware:
    """Tests for ApprovalGatesMiddleware (#35)."""

    def test_init(self) -> None:
        from bog_agents.middleware.approval_gates import ApprovalGatesMiddleware

        mw = ApprovalGatesMiddleware()
        assert len(mw.tools) == 5

    def test_tool_names(self) -> None:
        from bog_agents.middleware.approval_gates import ApprovalGatesMiddleware

        mw = ApprovalGatesMiddleware()
        names = {t.name for t in mw.tools}
        assert names == {
            "create_approval_gate",
            "submit_for_approval",
            "record_approval",
            "gate_status",
            "clear_gates",
        }

    def test_create_gate(self) -> None:
        from bog_agents.middleware.approval_gates import ApprovalStore

        store = ApprovalStore()
        gate = store.create_gate("trade_review", 2, "Review large trades")
        assert gate.name == "trade_review"
        assert gate.required_approvers == 2

    def test_submit(self) -> None:
        from bog_agents.middleware.approval_gates import ApprovalStore

        store = ApprovalStore()
        store.create_gate("compliance", 1, "Compliance check")
        sub = store.submit("compliance", "Sell 1000 AAPL shares", "high")
        assert sub.submission_id == 1
        assert sub.status == "pending"
        assert sub.risk_level == "high"

    def test_record_approval_approves(self) -> None:
        from bog_agents.middleware.approval_gates import ApprovalStore

        store = ApprovalStore()
        store.create_gate("review", 2, "Two approvers needed")
        store.submit("review", "Rebalance portfolio", "medium")
        store.record_approval(1, "Alice", "approved", "Looks good")
        sub = store.record_approval(1, "Bob", "approved", "Agreed")
        assert sub is not None
        assert sub.status == "approved"

    def test_record_rejection_immediately_rejects(self) -> None:
        from bog_agents.middleware.approval_gates import ApprovalStore

        store = ApprovalStore()
        store.create_gate("review", 3, "Three approvers needed")
        store.submit("review", "Risky trade", "critical")
        sub = store.record_approval(1, "Alice", "rejected", "Too risky")
        assert sub is not None
        assert sub.status == "rejected"

    def test_record_approval_not_found(self) -> None:
        from bog_agents.middleware.approval_gates import ApprovalStore

        store = ApprovalStore()
        assert store.record_approval(999, "Alice", "approved", "") is None

    def test_format_status(self) -> None:
        from bog_agents.middleware.approval_gates import ApprovalStore

        store = ApprovalStore()
        store.create_gate("compliance", 1, "Compliance review")
        store.submit("compliance", "New account opening", "low")
        status = store.format_status()
        assert "Approval Gates Status" in status
        assert "compliance" in status
        assert "Pending Submissions" in status
