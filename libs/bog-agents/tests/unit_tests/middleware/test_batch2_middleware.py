"""Tests for batch 2 financial advisor middleware modules.

Tests for features: #12 (Portfolio Analysis), #14 (Client Reports),
#19 (Deep Research), #25 (DLP), #33 (Version Control), #36 (Scenario Engine),
#39 (Peer Comparison), #42 (Tax Optimization), #43 (NL Query).
"""

from __future__ import annotations


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
