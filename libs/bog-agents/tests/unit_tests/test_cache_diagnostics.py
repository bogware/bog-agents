"""ROADMAP #52: the innermost cache-bust detector names what broke the prefix."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from bog_agents.middleware.cache_diagnostics import (
    CacheBustDetectorMiddleware,
    compare_prefix,
    format_cache_report,
    message_fingerprints,
    read_cache_events,
    section_at,
)

PROMPT = "# Role\nYou are helpful.\n\n## Memory\nRemember X.\n\n## Todos\n- nothing\n"


def _req(prompt: str, messages: list[Any]) -> Any:
    return SimpleNamespace(system_prompt=prompt, messages=messages, runtime=None, state={})


def _handler(calls: list[int]) -> Any:
    def call_next(_request: Any) -> Any:
        calls.append(1)
        return SimpleNamespace(result=[])

    return call_next


class TestPureHelpers:
    def test_section_names_nearest_header(self) -> None:
        assert section_at(PROMPT, PROMPT.index("Remember")) == "## Memory"
        assert section_at(PROMPT, PROMPT.index("nothing")) == "## Todos"
        assert section_at(PROMPT, 0) == "# Role"
        assert section_at("plain preamble\n# H\nbody", 3) == "(start of system prompt)"

    def test_fingerprints_are_stable_and_content_sensitive(self) -> None:
        a = message_fingerprints([HumanMessage("hi"), AIMessage("yo")])
        b = message_fingerprints([HumanMessage("hi"), AIMessage("yo")])
        c = message_fingerprints([HumanMessage("hi"), AIMessage("yo!")])
        assert a == b
        assert a[0] == c[0]
        assert a[1] != c[1]

    def test_compare_prefix_reports_prompt_segment_and_history(self) -> None:
        changed = PROMPT.replace("- nothing", "- ship it")
        events = compare_prefix(PROMPT, ["m1", "m2"], changed, ["m1", "m2", "m3"])
        assert [e["kind"] for e in events] == ["system_prompt"]
        assert events[0]["segment"] == "## Todos"
        compacted = compare_prefix(PROMPT, ["m1", "m2", "m3"], PROMPT, ["s1", "m3"])
        assert compacted[0]["kind"] == "history_compacted"
        assert compacted[0]["index"] == 0
        rewritten = compare_prefix(PROMPT, ["m1", "m2"], PROMPT, ["m1", "x2", "m3"])
        assert rewritten[0]["kind"] == "history_rewritten"
        assert rewritten[0]["index"] == 1
        assert compare_prefix(None, None, PROMPT, ["m1"]) == []
        assert compare_prefix(PROMPT, ["m1"], PROMPT, ["m1", "m2"]) == []


class TestMiddleware:
    def test_stable_prefix_records_nothing(self) -> None:
        mw = CacheBustDetectorMiddleware()
        calls: list[int] = []
        history = [HumanMessage("hi")]
        mw.wrap_model_call(_req(PROMPT, history), _handler(calls))
        history = [*history, AIMessage("yo"), ToolMessage("out", tool_call_id="t1")]
        mw.wrap_model_call(_req(PROMPT, history), _handler(calls))
        assert calls == [1, 1]
        assert mw.events == []
        assert mw.summary() == {"calls": 2, "stable_calls": 1, "busts": {}}

    async def test_bust_names_the_segment_and_writes_jsonl(self, tmp_path: Path) -> None:
        mw = CacheBustDetectorMiddleware(events_dir=tmp_path / "cache", clock=lambda: 1.0)
        calls: list[int] = []

        async def call_next(_request: Any) -> Any:
            calls.append(1)
            return SimpleNamespace(result=[])

        await mw.awrap_model_call(_req(PROMPT, [HumanMessage("hi")]), call_next)
        await mw.awrap_model_call(_req(PROMPT.replace("Remember X", "Remember Y"), [HumanMessage("hi"), AIMessage("yo")]), call_next)
        assert calls == [1, 1]
        assert len(mw.events) == 1
        event = mw.events[0]
        assert event["kind"] == "system_prompt"
        assert event["segment"] == "## Memory"
        assert event["thread_id"] == "session"  # no graph config outside a run
        stored = read_cache_events(tmp_path / "cache", "session")
        assert stored == mw.events
        report = format_cache_report(stored)
        assert "1x  system prompt segment '## Memory'" in report
        assert read_cache_events(tmp_path / "cache", "other") == []
        assert "No cache busts" in format_cache_report([])

    def test_sink_is_preferred_and_never_raises(self) -> None:
        seen: list[dict[str, Any]] = []

        def sink(event: dict[str, Any]) -> None:
            seen.append(event)
            raise RuntimeError("sink down")

        mw = CacheBustDetectorMiddleware(sink=sink)
        mw.wrap_model_call(_req(PROMPT, [HumanMessage("a")]), _handler([]))
        mw.wrap_model_call(_req(PROMPT, [HumanMessage("b")]), _handler([]))  # first message rewritten
        assert seen[0]["kind"] == "history_rewritten"
        assert mw.summary()["busts"] == {"history_rewritten": 1}

    def test_is_innermost_when_enabled(self, monkeypatch: Any) -> None:
        from bog_agents import create_agent, graph as graph_module
        from bog_agents.feature_config import FeatureConfig

        names: list[str] = []
        original = graph_module._validate_middleware_ordering

        def _spy(middleware_list: list[Any]) -> None:
            names.extend(type(m).__name__ for m in middleware_list)
            return original(middleware_list)

        monkeypatch.setattr(graph_module, "_validate_middleware_ordering", _spy)
        create_agent(model="claude-sonnet-4-20250514", config=FeatureConfig(enable_cache_diagnostics=True, enable_street_sweeper=True))
        assert names[-1] == "CacheBustDetectorMiddleware", names
        assert names.index("AnthropicPromptCachingMiddleware") < names.index("CacheBustDetectorMiddleware")
