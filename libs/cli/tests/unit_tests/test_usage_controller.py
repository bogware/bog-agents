"""ROADMAP #52 (CLI): usage strip, session ledger, status bar spend, /cost explain, cache report."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bog_agents_cli import usage_controller as uc

MODEL = "anthropic:claude-sonnet-4-6"  # $3 in / $15 out per 1M


class TestRecords:
    def test_usage_from_metadata_prices_cache_tiers(self) -> None:
        usage = {
            "input_tokens": 12_000,
            "output_tokens": 850,
            "input_token_details": {"cache_read": 10_000, "cache_creation": 1_000},
        }
        rec = uc.usage_from_metadata(usage, model=MODEL, ttft_s=1.2, duration_s=4.2)
        assert (
            rec.input_tokens,
            rec.cache_read,
            rec.cache_write,
            rec.output_tokens,
        ) == (1_000, 10_000, 1_000, 850)
        # 1k*3 + 10k*0.3 + 1k*3.75 + 850*15 = 3 + 3 + 3.75 + 12.75 = 22.5 per 1M → $0.0225
        assert rec.usd == pytest.approx(0.0225)
        assert rec.tokens_per_second == pytest.approx(850 / 3.0)
        strip = uc.format_usage_strip(rec)
        assert "1.0k→ 850←" in strip
        assert "cache 10.0k read, 1.0k written" in strip
        assert "$0.0225" in strip
        assert "TTFT 1.2s" in strip
        assert "283 tok/s" in strip

    def test_total_only_and_unpriced(self) -> None:
        rec = uc.usage_from_metadata(
            {"total_tokens": 500}, model="ollama:llama3", category="subagent"
        )
        assert rec.input_tokens == 500
        assert rec.usd == 0.0
        assert rec.tokens_per_second is None
        strip = uc.format_usage_strip(rec)
        assert "unpriced" in strip
        assert strip.endswith("subagent")


class TestLedger:
    def test_totals_ratio_tree_and_status(self) -> None:
        ledger = uc.UsageLedger()
        assert uc.status_bar_text(ledger) == ""
        assert ledger.cache_hit_ratio is None
        ledger.add(
            uc.UsageRecord(
                MODEL,
                "main",
                input_tokens=1_000,
                output_tokens=100,
                cache_read=3_000,
                usd=0.5,
            )
        )
        ledger.add(
            uc.UsageRecord(
                MODEL, "subagent", input_tokens=1_000, output_tokens=50, usd=0.25
            )
        )
        assert ledger.usd == pytest.approx(0.75)
        assert ledger.cache_hit_ratio == pytest.approx(3_000 / 5_000)
        tree = ledger.format_tree()
        assert tree.index("- main:") < tree.index("- subagent:")
        assert "cache hit 60%" in tree
        assert uc.status_bar_text(ledger) == "$0.7500 · cache 60%"
        data = ledger.to_dict()
        assert data["by_category"]["main"]["requests"] == 1
        assert len(data["requests"]) == 2

    def test_empty_tree(self) -> None:
        assert "No model requests" in uc.UsageLedger().format_tree()


class TestAppWiring:
    def test_install_feeds_ledger_and_status_bar(self) -> None:
        sinks: list[Any] = []
        spend: list[str] = []
        app = SimpleNamespace(
            _ui_adapter=SimpleNamespace(set_usage_sink=lambda s: sinks.append(s)),
            _status_bar=SimpleNamespace(set_spend=lambda t: spend.append(t)),
        )
        uc.install_usage_tracking(app)
        assert isinstance(app._usage_ledger, uc.UsageLedger)
        sinks[0](uc.UsageRecord(MODEL, usd=0.1, input_tokens=10, output_tokens=5))
        assert len(app._usage_ledger.records) == 1
        assert spend == ["$0.1000 · cache 0%"]

    async def test_record_stream_usage_attaches_strip(self) -> None:
        seen: list[uc.UsageRecord] = []
        strips: list[str] = []

        class _Msg:
            async def set_usage(self, text: str) -> None:
                strips.append(text)

        rec = await uc.record_stream_usage(
            seen.append,
            {"input_tokens": 10, "output_tokens": 5},
            model=MODEL,
            category="main",
            ttft_s=None,
            duration_s=1.0,
            message_widget=_Msg(),
        )
        assert seen == [rec]
        assert strips and "10→ 5←" in strips[0]
        # No widget (tool-call-only response) still records.
        await uc.record_stream_usage(
            seen.append,
            {"input_tokens": 1, "output_tokens": 1},
            model=MODEL,
            category="main",
            ttft_s=None,
            duration_s=None,
            message_widget=None,
        )
        assert len(seen) == 2


class TestExplain:
    async def test_explain_uses_ledger_json_and_handles_failures(self) -> None:
        ledger = uc.UsageLedger()
        ledger.add(uc.UsageRecord(MODEL, usd=0.42, input_tokens=100, output_tokens=10))
        prompts: list[str] = []

        async def invoke(prompt: str) -> str:
            prompts.append(prompt)
            return "  You spent $0.42 on one main request.  "

        assert (
            await uc.explain_usage("why?", ledger, invoke)
            == "You spent $0.42 on one main request."
        )
        assert '"total_usd": 0.42' in prompts[0]
        assert "QUESTION: why?" in prompts[0]

        async def broken(_p: str) -> str:
            raise RuntimeError("no model")

        assert (
            await uc.explain_usage("q", ledger, broken)
            == "Could not explain usage: no model"
        )

        async def empty(_p: str) -> str:
            return ""

        assert "no explanation" in await uc.explain_usage("", ledger, empty)


class TestCacheReport:
    def test_reads_the_threads_events(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bog_agents.middleware.cache_diagnostics import CacheBustDetectorMiddleware

        monkeypatch.setattr(uc, "cache_events_dir", lambda: tmp_path / "cache")
        app = SimpleNamespace(_session_state=SimpleNamespace(thread_id="thread-1"))
        assert "No cache busts" in uc.cache_report_for_app(app)
        mw = CacheBustDetectorMiddleware(events_dir=tmp_path / "cache")
        mw._emit(
            {"thread_id": "thread-1", "kind": "system_prompt", "segment": "## Memory"}
        )
        report = uc.cache_report_for_app(app)
        assert "1x  system prompt segment '## Memory'" in report
