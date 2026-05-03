"""Tests for batch 2 financial advisor middleware modules.

Tests for features: #12 (Portfolio Analysis), #14 (Client Reports),
#19 (Deep Research), #25 (DLP), #33 (Version Control), #36 (Scenario Engine),
#39 (Peer Comparison), #42 (Tax Optimization), #43 (NL Query).
"""

from __future__ import annotations


class TestPortfolioAnalysisMiddleware:
    """Tests for PortfolioAnalysisMiddleware (#12)."""

    def test_init(self) -> None:
        from bog_agents.middleware.portfolio_analysis import PortfolioAnalysisMiddleware

        mw = PortfolioAnalysisMiddleware()
        assert len(mw.tools) == 4

    def test_tool_names(self) -> None:
        from bog_agents.middleware.portfolio_analysis import PortfolioAnalysisMiddleware

        mw = PortfolioAnalysisMiddleware()
        names = {t.name for t in mw.tools}
        assert names == {"add_holding", "portfolio_metrics", "monte_carlo_sim", "clear_portfolio"}

    def test_holding_dataclass(self) -> None:
        from bog_agents.middleware.portfolio_analysis import Holding

        h = Holding(ticker="AAPL", weight=0.3, sector="Tech", asset_class="equity", returns=[0.02, -0.01, 0.03])
        assert h.ticker == "AAPL"
        assert h.weight == 0.3
        assert len(h.returns) == 3

    def test_portfolio_returns(self) -> None:
        from bog_agents.middleware.portfolio_analysis import Holding, Portfolio

        p = Portfolio(
            holdings=[
                Holding(ticker="A", weight=0.6, returns=[0.10, -0.05]),
                Holding(ticker="B", weight=0.4, returns=[0.02, 0.03]),
            ]
        )
        rets = p.portfolio_returns
        assert len(rets) == 2
        assert abs(rets[0] - (0.6 * 0.10 + 0.4 * 0.02)) < 1e-9
        assert abs(rets[1] - (0.6 * -0.05 + 0.4 * 0.03)) < 1e-9

    def test_risk_metrics(self) -> None:
        from bog_agents.middleware.portfolio_analysis import Holding, Portfolio

        p = Portfolio(
            holdings=[
                Holding(ticker="SPY", weight=1.0, returns=[0.02, -0.01, 0.03, -0.02, 0.01, 0.04]),
            ]
        )
        assert p.mean_return() != 0.0
        assert p.std_dev() > 0
        assert isinstance(p.sharpe_ratio(), float)
        assert isinstance(p.max_drawdown(), float)
        assert isinstance(p.var_95(), float)

    def test_allocation_by_sector(self) -> None:
        from bog_agents.middleware.portfolio_analysis import Holding, Portfolio

        p = Portfolio(
            holdings=[
                Holding(ticker="AAPL", weight=0.3, sector="Tech"),
                Holding(ticker="MSFT", weight=0.2, sector="Tech"),
                Holding(ticker="JPM", weight=0.5, sector="Finance"),
            ]
        )
        sectors = p.allocation_by_sector()
        assert abs(sectors["Tech"] - 0.5) < 1e-9
        assert abs(sectors["Finance"] - 0.5) < 1e-9

    def test_format_metrics(self) -> None:
        from bog_agents.middleware.portfolio_analysis import Holding, Portfolio

        p = Portfolio(
            holdings=[
                Holding(ticker="SPY", weight=1.0, returns=[0.02, -0.01, 0.03], sector="Index"),
            ]
        )
        output = p.format_metrics()
        assert "Portfolio Risk Metrics" in output
        assert "Sharpe Ratio" in output
        assert "Sector Allocation" in output

    def test_empty_portfolio_metrics(self) -> None:
        from bog_agents.middleware.portfolio_analysis import Portfolio

        p = Portfolio()
        assert p.mean_return() == 0.0
        assert p.std_dev() == 0.0
        assert p.sharpe_ratio() == 0.0
        assert p.max_drawdown() == 0.0

    def test_custom_risk_free_rate(self) -> None:
        from bog_agents.middleware.portfolio_analysis import PortfolioAnalysisMiddleware

        mw = PortfolioAnalysisMiddleware(risk_free_rate=0.04)
        assert mw.portfolio.risk_free_rate == 0.04


class TestClientReportsMiddleware:
    """Tests for ClientReportsMiddleware (#14)."""

    def test_init(self) -> None:
        from bog_agents.middleware.client_reports import ClientReportsMiddleware

        mw = ClientReportsMiddleware()
        assert len(mw.tools) == 6

    def test_tool_names(self) -> None:
        from bog_agents.middleware.client_reports import ClientReportsMiddleware

        mw = ClientReportsMiddleware()
        names = {t.name for t in mw.tools}
        assert names == {
            "set_report_config",
            "add_report_section",
            "add_disclosure",
            "add_standard_disclosures",
            "generate_report",
            "clear_report",
        }

    def test_report_format(self) -> None:
        from bog_agents.middleware.client_reports import ClientReport, ReportSection

        report = ClientReport(
            client_name="John Smith",
            account_id="ACC-001",
            report_type="quarterly_review",
            period="Q1 2026",
            firm_name="Test Advisors",
            sections=[
                ReportSection(title="Executive Summary", content="Portfolio up 8%", order=1),
                ReportSection(title="Performance", content="Outperformed benchmark", order=2),
            ],
            disclosures=["Past performance does not guarantee future results."],
        )
        output = report.format_report()
        assert "Quarterly Portfolio Review" in output
        assert "John Smith" in output
        assert "Executive Summary" in output
        assert "Past performance" in output

    def test_section_ordering(self) -> None:
        from bog_agents.middleware.client_reports import ClientReport, ReportSection

        report = ClientReport(
            client_name="Test",
            sections=[
                ReportSection(title="Third", content="...", order=3),
                ReportSection(title="First", content="...", order=1),
                ReportSection(title="Second", content="...", order=2),
            ],
        )
        output = report.format_report()
        first_pos = output.index("First")
        second_pos = output.index("Second")
        third_pos = output.index("Third")
        assert first_pos < second_pos < third_pos

    def test_standard_disclosures(self) -> None:
        from bog_agents.middleware.client_reports import STANDARD_DISCLOSURES

        assert len(STANDARD_DISCLOSURES) >= 3
        assert any("Past performance" in d for d in STANDARD_DISCLOSURES)

    def test_firm_branding(self) -> None:
        from bog_agents.middleware.client_reports import ClientReportsMiddleware

        mw = ClientReportsMiddleware(firm_name="Acme Wealth", advisor_name="Jane Doe")
        assert mw.report.firm_name == "Acme Wealth"
        assert mw.report.advisor_name == "Jane Doe"


class TestDeepResearchMiddleware:
    """Tests for DeepResearchMiddleware (#19)."""

    def test_init(self) -> None:
        from bog_agents.middleware.deep_research import DeepResearchMiddleware

        mw = DeepResearchMiddleware()
        assert len(mw.tools) == 6

    def test_tool_names(self) -> None:
        from bog_agents.middleware.deep_research import DeepResearchMiddleware

        mw = DeepResearchMiddleware()
        names = {t.name for t in mw.tools}
        assert names == {
            "start_research",
            "add_finding",
            "add_contradiction",
            "set_research_conclusion",
            "research_summary",
            "research_status",
        }

    def test_add_finding(self) -> None:
        from bog_agents.middleware.deep_research import ResearchTask

        task = ResearchTask(question="Test question")
        f = task.add_finding(content="Revenue grew 15%", source="10-K Filing", confidence=0.95)
        assert f.finding_id == 1
        assert f.source == "10-K Filing"
        assert f.confidence == 0.95

    def test_source_count(self) -> None:
        from bog_agents.middleware.deep_research import ResearchTask

        task = ResearchTask(question="Test")
        task.add_finding(content="A", source="Source 1")
        task.add_finding(content="B", source="Source 1")
        task.add_finding(content="C", source="Source 2")
        assert task.source_count == 2

    def test_avg_confidence(self) -> None:
        from bog_agents.middleware.deep_research import ResearchTask

        task = ResearchTask(question="Test")
        task.add_finding(content="A", source="S1", confidence=0.8)
        task.add_finding(content="B", source="S2", confidence=0.6)
        assert abs(task.avg_confidence - 0.7) < 1e-9

    def test_add_contradiction(self) -> None:
        from bog_agents.middleware.deep_research import ResearchTask

        task = ResearchTask(question="Test")
        task.add_finding(content="Revenue up", source="S1")
        task.add_finding(content="Revenue down", source="S2")
        c = task.add_contradiction(finding_a_id=1, finding_b_id=2, description="Revenue direction conflicts")
        assert c is not None
        assert c.description == "Revenue direction conflicts"

    def test_contradiction_invalid_ids(self) -> None:
        from bog_agents.middleware.deep_research import ResearchTask

        task = ResearchTask(question="Test")
        task.add_finding(content="A", source="S1")
        c = task.add_contradiction(finding_a_id=1, finding_b_id=999, description="Invalid")
        assert c is None

    def test_format_report(self) -> None:
        from bog_agents.middleware.deep_research import ResearchTask

        task = ResearchTask(question="What is Apple's revenue?", started_at="2026-03-15")
        task.add_finding(content="$383B", source="10-K", confidence=0.95, tags=["financial"])
        task.conclusion = "Revenue was $383B in FY2023"
        output = task.format_report()
        assert "Deep Research Report" in output
        assert "$383B" in output
        assert "Conclusion" in output


class TestDLPMiddleware:
    """Tests for DLPMiddleware (#25)."""

    def test_init(self) -> None:
        from bog_agents.middleware.dlp import DLPMiddleware

        mw = DLPMiddleware()
        assert len(mw.tools) == 3

    def test_tool_names(self) -> None:
        from bog_agents.middleware.dlp import DLPMiddleware

        mw = DLPMiddleware()
        names = {t.name for t in mw.tools}
        assert names == {"dlp_report", "scan_text", "redact_text"}

    def test_scan_text_ssn(self) -> None:
        from bog_agents.middleware.dlp import DEFAULT_PATTERNS, _scan_text

        results = _scan_text("My SSN is 123-45-6789", DEFAULT_PATTERNS)
        assert len(results) >= 1
        assert any(p.name == "SSN" for p, _ in results)

    def test_scan_text_email(self) -> None:
        from bog_agents.middleware.dlp import DEFAULT_PATTERNS, _scan_text

        results = _scan_text("Email me at john@example.com", DEFAULT_PATTERNS)
        assert any(p.name == "Email" for p, _ in results)

    def test_scan_text_credit_card(self) -> None:
        from bog_agents.middleware.dlp import DEFAULT_PATTERNS, _scan_text

        results = _scan_text("Card: 4111-1111-1111-1111", DEFAULT_PATTERNS)
        assert any(p.name == "Credit Card" for p, _ in results)

    def test_redact_text(self) -> None:
        from bog_agents.middleware.dlp import DEFAULT_PATTERNS, _redact_text

        text = "SSN: 123-45-6789, email: john@example.com"
        redacted = _redact_text(text, DEFAULT_PATTERNS)
        assert "123-45-6789" not in redacted
        assert "[SSN-REDACTED]" in redacted
        assert "john@example.com" not in redacted
        assert "[EMAIL-REDACTED]" in redacted

    def test_no_sensitive_data(self) -> None:
        from bog_agents.middleware.dlp import DEFAULT_PATTERNS, _scan_text

        results = _scan_text("The market was up 2% today.", DEFAULT_PATTERNS)
        assert len(results) == 0

    def test_dlp_log_summary(self) -> None:
        from bog_agents.middleware.dlp import DLPEvent, DLPLog

        log = DLPLog(
            events=[
                DLPEvent(pattern_name="SSN", category="pii", action="redact", count=2),
                DLPEvent(pattern_name="Email", category="pii", action="warn", count=1),
            ]
        )
        assert log.total_detections == 3
        assert log.total_redactions == 2
        summary = log.format_summary()
        assert "Data Loss Prevention Report" in summary

    def test_redact_mode(self) -> None:
        from bog_agents.middleware.dlp import DLPMiddleware

        mw = DLPMiddleware(mode="redact")
        assert mw._mode == "redact"

    def test_warn_mode(self) -> None:
        from bog_agents.middleware.dlp import DLPMiddleware

        mw = DLPMiddleware(mode="warn")
        assert mw._mode == "warn"

    def test_process_messages_redact_mutates_string_content(self) -> None:
        from types import SimpleNamespace

        from bog_agents.middleware.dlp import DLPMiddleware

        mw = DLPMiddleware(mode="redact")
        msg = SimpleNamespace(content="My SSN is 123-45-6789 and email john@example.com")
        request = SimpleNamespace(messages=[msg])
        mw._process_messages(request)  # type: ignore[arg-type]

        assert "123-45-6789" not in msg.content
        assert "john@example.com" not in msg.content
        assert "[SSN-REDACTED]" in msg.content
        assert "[EMAIL-REDACTED]" in msg.content
        assert mw.log.total_redactions >= 2

    def test_process_messages_warn_does_not_mutate(self) -> None:
        from types import SimpleNamespace

        from bog_agents.middleware.dlp import DLPMiddleware

        mw = DLPMiddleware(mode="warn")
        original = "My SSN is 123-45-6789"
        msg = SimpleNamespace(content=original)
        request = SimpleNamespace(messages=[msg])
        mw._process_messages(request)  # type: ignore[arg-type]

        assert msg.content == original
        assert mw.log.total_detections >= 1
        assert mw.log.total_redactions == 0

    def test_process_messages_redact_multimodal_text_blocks(self) -> None:
        from types import SimpleNamespace

        from bog_agents.middleware.dlp import DLPMiddleware

        mw = DLPMiddleware(mode="redact")
        content = [
            {"type": "text", "text": "Card: 4111-1111-1111-1111"},
            {"type": "image_url", "image_url": "data:image/png;base64,..."},
        ]
        msg = SimpleNamespace(content=content)
        request = SimpleNamespace(messages=[msg])
        mw._process_messages(request)  # type: ignore[arg-type]

        assert "4111-1111-1111-1111" not in content[0]["text"]
        assert "[CC-REDACTED]" in content[0]["text"]
        # Non-text block untouched.
        assert content[1] == {"type": "image_url", "image_url": "data:image/png;base64,..."}


class TestVersionControlMiddleware:
    """Tests for VersionControlMiddleware (#33)."""

    def test_init(self) -> None:
        from bog_agents.middleware.version_control import VersionControlMiddleware

        mw = VersionControlMiddleware()
        assert len(mw.tools) == 5

    def test_tool_names(self) -> None:
        from bog_agents.middleware.version_control import VersionControlMiddleware

        mw = VersionControlMiddleware()
        names = {t.name for t in mw.tools}
        assert names == {
            "save_version",
            "list_versions",
            "compare_versions",
            "restore_version",
            "get_current_research",
        }

    def test_save_version(self) -> None:
        from bog_agents.middleware.version_control import VersionStore

        store = VersionStore()
        v = store.save(label="draft-1", content="Initial research output", summary="First draft")
        assert v.version_id == 1
        assert v.label == "draft-1"
        assert store.current_content == "Initial research output"

    def test_version_chain(self) -> None:
        from bog_agents.middleware.version_control import VersionStore

        store = VersionStore()
        v1 = store.save(label="v1", content="First")
        v2 = store.save(label="v2", content="Second")
        assert v2.parent_id == v1.version_id
        assert v1.parent_id is None

    def test_compare_versions(self) -> None:
        from bog_agents.middleware.version_control import VersionStore

        store = VersionStore()
        store.save(label="v1", content="Line 1\nLine 2\nLine 3")
        store.save(label="v2", content="Line 1\nModified\nLine 3\nLine 4")
        comparison = store.compare(1, 2)
        assert "Comparison" in comparison
        assert "v1" in comparison
        assert "v2" in comparison

    def test_get_version(self) -> None:
        from bog_agents.middleware.version_control import VersionStore

        store = VersionStore()
        store.save(label="v1", content="Test")
        v = store.get(1)
        assert v is not None
        assert v.label == "v1"
        assert store.get(999) is None

    def test_format_history(self) -> None:
        from bog_agents.middleware.version_control import VersionStore

        store = VersionStore()
        store.save(label="draft-1", content="First", summary="Initial version")
        store.save(label="draft-2", content="Second", summary="Updated findings")
        history = store.format_history()
        assert "Version History" in history
        assert "draft-1" in history
        assert "draft-2" in history

    def test_empty_store(self) -> None:
        from bog_agents.middleware.version_control import VersionStore

        store = VersionStore()
        assert store.format_history() == "No versions saved yet."


class TestScenarioEngineMiddleware:
    """Tests for ScenarioEngineMiddleware (#36)."""

    def test_init(self) -> None:
        from bog_agents.middleware.scenario_engine import ScenarioEngineMiddleware

        mw = ScenarioEngineMiddleware()
        assert len(mw.tools) == 6

    def test_tool_names(self) -> None:
        from bog_agents.middleware.scenario_engine import ScenarioEngineMiddleware

        mw = ScenarioEngineMiddleware()
        names = {t.name for t in mw.tools}
        assert names == {
            "create_scenario",
            "add_shock",
            "add_holding_impact",
            "scenario_report",
            "compare_scenarios",
            "clear_scenarios",
        }

    def test_create_scenario(self) -> None:
        from bog_agents.middleware.scenario_engine import ScenarioStore

        store = ScenarioStore()
        s = store.create(name="Rate Hike +200bps", description="Fed raises rates")
        assert s.scenario_id == 1
        assert s.name == "Rate Hike +200bps"

    def test_add_shock(self) -> None:
        from bog_agents.middleware.scenario_engine import ScenarioShock, ScenarioStore

        store = ScenarioStore()
        s = store.create(name="Test")
        s.shocks.append(ScenarioShock(factor="interest_rates", magnitude=2.0, unit="bps"))
        assert len(s.shocks) == 1
        assert s.shocks[0].factor == "interest_rates"

    def test_holding_impact(self) -> None:
        from bog_agents.middleware.scenario_engine import HoldingImpact, ScenarioStore

        store = ScenarioStore()
        s = store.create(name="Test")
        s.impacts.append(HoldingImpact(ticker="AAPL", weight=0.3, estimated_return=-0.15, pnl_contribution=-0.045))
        s.total_portfolio_impact = sum(i.pnl_contribution for i in s.impacts)
        assert abs(s.total_portfolio_impact - (-0.045)) < 1e-9

    def test_scenario_report(self) -> None:
        from bog_agents.middleware.scenario_engine import HoldingImpact, ScenarioShock, ScenarioStore

        store = ScenarioStore()
        s = store.create(name="Bear Market", description="Equities drop 20%")
        s.shocks.append(ScenarioShock(factor="sp500", magnitude=-20.0))
        s.impacts.append(HoldingImpact(ticker="SPY", weight=0.5, estimated_return=-0.20, pnl_contribution=-0.10))
        s.total_portfolio_impact = -0.10
        report = s.format_report()
        assert "Bear Market" in report
        assert "sp500" in report
        assert "SPY" in report

    def test_compare_scenarios(self) -> None:
        from bog_agents.middleware.scenario_engine import ScenarioStore

        store = ScenarioStore()
        s1 = store.create(name="Bull")
        s1.total_portfolio_impact = 0.15
        s2 = store.create(name="Bear")
        s2.total_portfolio_impact = -0.20
        comparison = store.format_comparison()
        assert "Scenario Comparison" in comparison
        assert "Best case" in comparison
        assert "Worst case" in comparison

    def test_empty_store(self) -> None:
        from bog_agents.middleware.scenario_engine import ScenarioStore

        store = ScenarioStore()
        assert store.format_comparison() == "No scenarios created yet."


class TestPeerComparisonMiddleware:
    """Tests for PeerComparisonMiddleware (#39)."""

    def test_init(self) -> None:
        from bog_agents.middleware.peer_comparison import PeerComparisonMiddleware

        mw = PeerComparisonMiddleware()
        assert len(mw.tools) == 5

    def test_tool_names(self) -> None:
        from bog_agents.middleware.peer_comparison import PeerComparisonMiddleware

        mw = PeerComparisonMiddleware()
        names = {t.name for t in mw.tools}
        assert names == {
            "set_target_company",
            "add_peer",
            "peer_comparison_matrix",
            "highlight_outliers",
            "clear_peers",
        }

    def test_company_metrics(self) -> None:
        from bog_agents.middleware.peer_comparison import CompanyMetrics

        c = CompanyMetrics(ticker="AAPL", pe_ratio=28.5, pb_ratio=45.0, roe=1.47)
        assert c.ticker == "AAPL"
        assert c.pe_ratio == 28.5

    def test_peer_group_all_companies(self) -> None:
        from bog_agents.middleware.peer_comparison import CompanyMetrics, PeerGroup

        group = PeerGroup(
            target=CompanyMetrics(ticker="AAPL", is_target=True),
            peers=[CompanyMetrics(ticker="MSFT"), CompanyMetrics(ticker="GOOG")],
        )
        assert len(group.all_companies) == 3
        assert group.all_companies[0].ticker == "AAPL"

    def test_peer_median(self) -> None:
        from bog_agents.middleware.peer_comparison import CompanyMetrics, PeerGroup

        group = PeerGroup(
            peers=[
                CompanyMetrics(ticker="A", pe_ratio=20.0),
                CompanyMetrics(ticker="B", pe_ratio=30.0),
                CompanyMetrics(ticker="C", pe_ratio=25.0),
            ]
        )
        median = group._median("pe_ratio")
        assert median == 25.0

    def test_find_outliers(self) -> None:
        from bog_agents.middleware.peer_comparison import CompanyMetrics, PeerGroup

        group = PeerGroup(
            target=CompanyMetrics(ticker="X", pe_ratio=100.0, is_target=True),
            peers=[
                CompanyMetrics(ticker="A", pe_ratio=20.0),
                CompanyMetrics(ticker="B", pe_ratio=25.0),
                CompanyMetrics(ticker="C", pe_ratio=22.0),
            ],
        )
        outliers = group.find_outliers(threshold=1.5)
        assert len(outliers) >= 1
        assert any(m == "pe_ratio" and d == "above" for m, d, _, _ in outliers)

    def test_format_matrix(self) -> None:
        from bog_agents.middleware.peer_comparison import CompanyMetrics, PeerGroup

        group = PeerGroup(
            target=CompanyMetrics(ticker="AAPL", pe_ratio=28.5, is_target=True),
            peers=[CompanyMetrics(ticker="MSFT", pe_ratio=32.0)],
        )
        matrix = group.format_matrix()
        assert "Peer Comparison Matrix" in matrix
        assert "AAPL" in matrix
        assert "MSFT" in matrix

    def test_empty_group(self) -> None:
        from bog_agents.middleware.peer_comparison import PeerGroup

        group = PeerGroup()
        assert group.format_matrix() == "No companies registered."
        assert group.find_outliers() == []


class TestTaxOptimizationMiddleware:
    """Tests for TaxOptimizationMiddleware (#42)."""

    def test_init(self) -> None:
        from bog_agents.middleware.tax_optimization import TaxOptimizationMiddleware

        mw = TaxOptimizationMiddleware()
        assert len(mw.tools) == 5

    def test_tool_names(self) -> None:
        from bog_agents.middleware.tax_optimization import TaxOptimizationMiddleware

        mw = TaxOptimizationMiddleware()
        names = {t.name for t in mw.tools}
        assert names == {
            "add_tax_lot",
            "find_harvest_opportunities",
            "check_wash_sale",
            "tax_summary",
            "clear_tax_lots",
        }

    def test_tax_lot_gain_loss(self) -> None:
        from bog_agents.middleware.tax_optimization import TaxLot

        lot = TaxLot(lot_id=1, ticker="AAPL", shares=100, cost_basis=15000, current_value=18000, purchase_date="2024-01-01")
        assert lot.gain_loss == 3000
        assert abs(lot.gain_loss_pct - 0.2) < 1e-9

    def test_tax_lot_loss(self) -> None:
        from bog_agents.middleware.tax_optimization import TaxLot

        lot = TaxLot(lot_id=1, ticker="INTC", shares=100, cost_basis=15000, current_value=10000, purchase_date="2025-06-01")
        assert lot.gain_loss == -5000

    def test_harvest_opportunities(self) -> None:
        from bog_agents.middleware.tax_optimization import TaxPortfolio

        portfolio = TaxPortfolio()
        portfolio.add_lot(ticker="AAPL", shares=100, cost_basis=15000, current_value=18000, purchase_date="2024-01-01")
        portfolio.add_lot(ticker="INTC", shares=100, cost_basis=15000, current_value=10000, purchase_date="2025-06-01")
        opps = portfolio.harvest_opportunities()
        assert len(opps) == 1
        assert opps[0].ticker == "INTC"

    def test_harvest_ignores_non_taxable(self) -> None:
        from bog_agents.middleware.tax_optimization import TaxPortfolio

        portfolio = TaxPortfolio()
        portfolio.add_lot(ticker="INTC", shares=100, cost_basis=15000, current_value=10000, purchase_date="2025-06-01", account_type="ira")
        opps = portfolio.harvest_opportunities()
        assert len(opps) == 0

    def test_wash_sale_detection(self) -> None:
        from bog_agents.middleware.tax_optimization import TaxPortfolio

        portfolio = TaxPortfolio()
        portfolio.add_lot(ticker="AAPL", shares=50, cost_basis=7500, current_value=7000, purchase_date="2026-03-10")
        violations = portfolio.check_wash_sales("AAPL", "2026-03-01")
        assert len(violations) == 1

    def test_no_wash_sale(self) -> None:
        from bog_agents.middleware.tax_optimization import TaxPortfolio

        portfolio = TaxPortfolio()
        portfolio.add_lot(ticker="AAPL", shares=50, cost_basis=7500, current_value=7000, purchase_date="2025-01-01")
        violations = portfolio.check_wash_sales("AAPL", "2026-03-01")
        assert len(violations) == 0

    def test_format_tax_summary(self) -> None:
        from bog_agents.middleware.tax_optimization import TaxPortfolio

        portfolio = TaxPortfolio()
        portfolio.add_lot(ticker="AAPL", shares=100, cost_basis=15000, current_value=18000, purchase_date="2024-01-01")
        portfolio.add_lot(ticker="INTC", shares=100, cost_basis=15000, current_value=10000, purchase_date="2025-06-01")
        summary = portfolio.format_tax_summary()
        assert "Tax Summary" in summary
        assert "Unrealized Gains/Losses" in summary

    def test_empty_portfolio(self) -> None:
        from bog_agents.middleware.tax_optimization import TaxPortfolio

        portfolio = TaxPortfolio()
        assert portfolio.format_tax_summary() == "No tax lots registered."


class TestNLQueryMiddleware:
    """Tests for NLQueryMiddleware (#43)."""

    def test_init(self) -> None:
        from bog_agents.middleware.nl_query import NLQueryMiddleware

        mw = NLQueryMiddleware()
        assert len(mw.tools) == 4

    def test_tool_names(self) -> None:
        from bog_agents.middleware.nl_query import NLQueryMiddleware

        mw = NLQueryMiddleware()
        names = {t.name for t in mw.tools}
        assert names == {"register_dataset", "add_data_rows", "query_data", "list_datasets"}

    def test_register_dataset(self) -> None:
        from bog_agents.middleware.nl_query import DatasetColumn, DataStore

        store = DataStore()
        ds = store.register(
            "clients",
            [
                DatasetColumn(name="name", dtype="text"),
                DatasetColumn(name="aum", dtype="float"),
            ],
            description="Client data",
        )
        assert ds.name == "clients"
        assert len(ds.columns) == 2

    def test_add_rows(self) -> None:
        from bog_agents.middleware.nl_query import DatasetColumn, DataStore

        store = DataStore()
        store.register("clients", [DatasetColumn(name="name"), DatasetColumn(name="aum")])
        added = store.add_rows(
            "clients",
            [
                {"name": "Alice", "aum": 1000000},
                {"name": "Bob", "aum": 2000000},
            ],
        )
        assert added == 2
        assert store.datasets["clients"].row_count == 2

    def test_add_rows_invalid_dataset(self) -> None:
        from bog_agents.middleware.nl_query import DataStore

        store = DataStore()
        assert store.add_rows("nonexistent", [{"a": 1}]) == 0

    def test_query_basic(self) -> None:
        from bog_agents.middleware.nl_query import DatasetColumn, DataStore

        store = DataStore()
        store.register("clients", [DatasetColumn(name="name"), DatasetColumn(name="sector")])
        store.add_rows(
            "clients",
            [
                {"name": "Alice", "sector": "Tech"},
                {"name": "Bob", "sector": "Finance"},
                {"name": "Carol", "sector": "Tech"},
            ],
        )
        result = store.query("clients", filters={"sector": "Tech"})
        assert len(result.rows) == 2

    def test_query_sort(self) -> None:
        from bog_agents.middleware.nl_query import DatasetColumn, DataStore

        store = DataStore()
        store.register("nums", [DatasetColumn(name="val")])
        store.add_rows("nums", [{"val": 3}, {"val": 1}, {"val": 2}])
        result = store.query("nums", sort_by="val")
        assert result.rows[0]["val"] == 1

    def test_query_limit(self) -> None:
        from bog_agents.middleware.nl_query import DatasetColumn, DataStore

        store = DataStore()
        store.register("nums", [DatasetColumn(name="val")])
        store.add_rows("nums", [{"val": i} for i in range(50)])
        result = store.query("nums", limit=5)
        assert len(result.rows) == 5

    def test_query_not_found(self) -> None:
        from bog_agents.middleware.nl_query import DataStore

        store = DataStore()
        result = store.query("nonexistent")
        assert result.error != ""

    def test_format_table(self) -> None:
        from bog_agents.middleware.nl_query import QueryResult

        result = QueryResult(
            query="SELECT * FROM test",
            dataset="test",
            rows=[{"name": "Alice", "aum": "1M"}, {"name": "Bob", "aum": "2M"}],
            columns=["name", "aum"],
        )
        table = result.format_table()
        assert "Query Results" in table
        assert "Alice" in table
        assert "Bob" in table

    def test_format_datasets(self) -> None:
        from bog_agents.middleware.nl_query import DatasetColumn, DataStore

        store = DataStore()
        store.register("clients", [DatasetColumn(name="name", dtype="text", description="Client name")])
        output = store.format_datasets()
        assert "Available Datasets" in output
        assert "clients" in output

    def test_empty_datasets(self) -> None:
        from bog_agents.middleware.nl_query import DataStore

        store = DataStore()
        assert store.format_datasets() == "No datasets registered."
