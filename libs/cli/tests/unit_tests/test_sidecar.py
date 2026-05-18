"""Tests for the /sidecar feature (T-1).

The sidecar is a one-shot read-only Q&A subagent. Tests run offline by
passing a stub model that mimics the LangChain ``BaseChatModel`` shape
just enough for ``run_sidecar_query`` to drive it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from bog_agents_cli.sidecar import (
    SidecarResult,
    build_readonly_tools,
    run_sidecar_query,
    summarize_parent_context,
)
from bog_agents_cli.sidecar_controller import (
    SidecarController,
    dispatch,
    get_controller,
    reset_controllers,
)

# ---------------------------------------------------------------------------
# Stub model
# ---------------------------------------------------------------------------


@dataclass
class _StubScript:
    """Recipe for a fake model: ordered list of responses to return."""

    responses: list[AIMessage] = field(default_factory=list)
    invocations: list[list[Any]] = field(default_factory=list)


class _StubModel:
    """Just enough of ``BaseChatModel`` to satisfy ``run_sidecar_query``."""

    def __init__(self, script: _StubScript) -> None:
        self._script = script
        self._bound_tools: list[Any] = []

    def bind_tools(self, tools: list[Any]) -> _StubModel:
        new = _StubModel(self._script)
        new._bound_tools = list(tools)
        return new

    def invoke(self, messages: list[Any]) -> AIMessage:
        self._script.invocations.append(list(messages))
        if not self._script.responses:
            return AIMessage(content="(stub exhausted)")
        return self._script.responses.pop(0)


@pytest.fixture
def script() -> _StubScript:
    return _StubScript()


@pytest.fixture(autouse=True)
def _isolated_controllers() -> None:
    reset_controllers()


# ---------------------------------------------------------------------------
# Core runner
# ---------------------------------------------------------------------------


class TestCoreRunner:
    def test_empty_question_returns_error(self, script: _StubScript) -> None:
        result = run_sidecar_query(
            question="", model=_StubModel(script), tools=[], context_summary=""
        )
        assert not result.ok
        assert "empty question" in result.error

    def test_zero_tool_call_answer(self, script: _StubScript) -> None:
        script.responses.append(AIMessage(content="42 is the answer"))
        result = run_sidecar_query(
            question="What is the answer?",
            model=_StubModel(script),
            tools=[],
        )
        assert result.ok
        assert result.answer == "42 is the answer"
        assert result.iterations == 1
        assert result.tool_calls_made == []

    def test_one_tool_call_then_answer(
        self, script: _StubScript, tmp_path: Path
    ) -> None:
        readme = tmp_path / "README.md"
        readme.write_text("Project alpha\n", encoding="utf-8")
        tools = build_readonly_tools(working_dir=tmp_path, web_search=False)

        # 1) Model asks to read_file
        script.responses.append(
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_file",
                        "args": {"path": "README.md"},
                        "id": "c1",
                        "type": "tool_call",
                    }
                ],
            )
        )
        # 2) Then produces an answer
        script.responses.append(AIMessage(content="The project is named alpha."))

        result = run_sidecar_query(
            question="What is this project called?",
            model=_StubModel(script),
            tools=tools,
        )
        assert result.ok
        assert "alpha" in result.answer
        assert result.tool_calls_made == ["read_file"]
        assert result.iterations == 2

    def test_max_iterations_hit(self, script: _StubScript, tmp_path: Path) -> None:
        tools = build_readonly_tools(working_dir=tmp_path, web_search=False)
        # Always ask for a tool call → never produces an answer.
        for _ in range(20):
            script.responses.append(
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "glob",
                            "args": {"pattern": "*"},
                            "id": "loop",
                            "type": "tool_call",
                        }
                    ],
                )
            )
        result = run_sidecar_query(
            question="never-answers",
            model=_StubModel(script),
            tools=tools,
            max_iterations=3,
        )
        assert not result.ok
        assert "max_iterations" in result.error

    def test_context_summary_passes_to_model(self, script: _StubScript) -> None:
        script.responses.append(AIMessage(content="ok"))
        run_sidecar_query(
            question="q",
            model=_StubModel(script),
            tools=[],
            context_summary="parent was working on X",
        )
        first_call_messages = script.invocations[0]
        human_msg = next(m for m in first_call_messages if isinstance(m, HumanMessage))
        assert "parent was working on X" in human_msg.content

    def test_system_prompt_is_first_message(self, script: _StubScript) -> None:
        script.responses.append(AIMessage(content="ok"))
        run_sidecar_query(question="q", model=_StubModel(script), tools=[])
        first_call_messages = script.invocations[0]
        assert isinstance(first_call_messages[0], SystemMessage)
        assert "sidecar" in first_call_messages[0].content.lower()


# ---------------------------------------------------------------------------
# Read-only tools
# ---------------------------------------------------------------------------


class TestReadOnlyTools:
    def test_read_file_works(self, tmp_path: Path) -> None:
        target = tmp_path / "hello.txt"
        target.write_text("hello world", encoding="utf-8")
        tools = build_readonly_tools(working_dir=tmp_path, web_search=False)
        read_file = next(t for t in tools if t.name == "read_file")
        result = read_file.invoke({"path": "hello.txt"})
        assert "hello world" in result

    def test_read_file_rejects_traversal(self, tmp_path: Path) -> None:
        tools = build_readonly_tools(working_dir=tmp_path, web_search=False)
        read_file = next(t for t in tools if t.name == "read_file")
        result = read_file.invoke({"path": "../outside.txt"})
        assert "Error" in result
        assert "outside" in result.lower()

    def test_glob_finds_files(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("", encoding="utf-8")
        (tmp_path / "b.py").write_text("", encoding="utf-8")
        (tmp_path / "c.md").write_text("", encoding="utf-8")
        tools = build_readonly_tools(working_dir=tmp_path, web_search=False)
        glob_tool = next(t for t in tools if t.name == "glob")
        out = glob_tool.invoke({"pattern": "*.py"})
        assert "a.py" in out
        assert "b.py" in out
        assert "c.md" not in out

    def test_grep_finds_lines(self, tmp_path: Path) -> None:
        (tmp_path / "code.py").write_text(
            "import os\nimport sys\nx = 1\n", encoding="utf-8"
        )
        tools = build_readonly_tools(working_dir=tmp_path, web_search=False)
        grep_tool = next(t for t in tools if t.name == "grep")
        out = grep_tool.invoke({"pattern": r"^import", "path": "."})
        assert "code.py" in out
        assert "import os" in out
        assert "import sys" in out
        assert "x = 1" not in out

    def test_no_write_tools_in_set(self, tmp_path: Path) -> None:
        tools = build_readonly_tools(working_dir=tmp_path, web_search=False)
        names = {t.name for t in tools}
        for forbidden in (
            "write_file",
            "edit_file",
            "execute",
            "shell_execute",
            "run",
        ):
            assert forbidden not in names, f"sidecar must not expose {forbidden!r}"

    def test_web_search_toggle(self, tmp_path: Path) -> None:
        with_web = {
            t.name for t in build_readonly_tools(working_dir=tmp_path, web_search=True)
        }
        without_web = {
            t.name for t in build_readonly_tools(working_dir=tmp_path, web_search=False)
        }
        assert "web_search" in with_web
        assert "web_search" not in without_web


# ---------------------------------------------------------------------------
# Result rendering
# ---------------------------------------------------------------------------


class TestResultRendering:
    def test_quote_for_parent_success(self) -> None:
        r = SidecarResult(answer="line 1\nline 2", tool_calls_made=["read_file"])
        out = r.quote_for_parent()
        assert "> **Sidecar reply:**" in out
        assert "> line 1" in out
        assert "> line 2" in out
        assert "read_file" in out

    def test_quote_for_parent_error(self) -> None:
        r = SidecarResult(ok=False, error="model exploded")
        out = r.quote_for_parent()
        assert "failed" in out
        assert "model exploded" in out

    def test_quote_handles_empty_answer(self) -> None:
        r = SidecarResult(answer="")
        out = r.quote_for_parent()
        assert "no answer" in out


# ---------------------------------------------------------------------------
# Parent-context summariser
# ---------------------------------------------------------------------------


class TestSummariser:
    def test_summary_empty_when_no_messages(self) -> None:
        assert summarize_parent_context(()) == ""

    def test_summary_keeps_tail(self) -> None:
        msgs = [HumanMessage(content=f"turn {i}") for i in range(30)]
        msgs.append(AIMessage(content="latest"))
        summary = summarize_parent_context(msgs, max_chars=200)
        assert "latest" in summary
        # The earliest turn 0 should NOT make it in.
        assert "turn 0]" not in summary


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


class TestController:
    def test_run_dispatches_to_runner(
        self, script: _StubScript, tmp_path: Path
    ) -> None:
        script.responses.append(AIMessage(content="answer"))
        c = SidecarController(
            working_dir=tmp_path,
            model_factory=lambda: _StubModel(script),
            web_search=False,
        )
        result = c.run("what is X?")
        assert result.ok
        assert result.answer == "answer"

    def test_run_uses_context_override(
        self, script: _StubScript, tmp_path: Path
    ) -> None:
        script.responses.append(AIMessage(content="ok"))
        c = SidecarController(
            working_dir=tmp_path,
            model_factory=lambda: _StubModel(script),
            web_search=False,
        )
        c.run("q", context_override="literal override")
        first_call_messages = script.invocations[0]
        human = next(m for m in first_call_messages if isinstance(m, HumanMessage))
        assert "literal override" in human.content

    def test_run_with_empty_question(self, tmp_path: Path) -> None:
        c = SidecarController(
            working_dir=tmp_path,
            model_factory=lambda: _StubModel(_StubScript()),
            web_search=False,
        )
        r = c.run("")
        assert not r.ok

    def test_run_handles_model_factory_failure(self, tmp_path: Path) -> None:
        def boom() -> object:
            msg = "no key"
            raise RuntimeError(msg)

        c = SidecarController(
            working_dir=tmp_path,
            model_factory=boom,
            web_search=False,
        )
        r = c.run("q")
        assert not r.ok
        assert "could not build sidecar model" in r.error


# ---------------------------------------------------------------------------
# Dispatcher (slash entry point)
# ---------------------------------------------------------------------------


class TestDispatch:
    def test_dispatch_strips_slash_prefix(
        self, script: _StubScript, tmp_path: Path
    ) -> None:
        script.responses.append(AIMessage(content="alpha"))
        out = dispatch(
            "/sidecar what is alpha?",
            working_dir=tmp_path,
            model_factory=lambda: _StubModel(script),
        )
        assert "alpha" in out
        assert "Sidecar reply" in out

    def test_dispatch_empty_question_quoted_error(self, tmp_path: Path) -> None:
        out = dispatch(
            "/sidecar   ",
            working_dir=tmp_path,
            model_factory=lambda: _StubModel(_StubScript()),
        )
        assert "failed" in out
        assert "empty question" in out


class TestControllerRegistry:
    def test_first_call_requires_factory(self, tmp_path: Path) -> None:
        with pytest.raises(RuntimeError, match="model_factory"):
            get_controller(tmp_path)

    def test_second_call_returns_cached(
        self, script: _StubScript, tmp_path: Path
    ) -> None:
        a = get_controller(tmp_path, model_factory=lambda: _StubModel(script))
        b = get_controller(tmp_path)
        assert a is b


# ---------------------------------------------------------------------------
# Slash-command registry consistency
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_sidecar_command_registered(self) -> None:
        from bog_agents_cli.commands import general

        names = {cmd.name for cmd in general.COMMANDS}
        assert "/sidecar" in names

    def test_handler_method_name(self) -> None:
        from bog_agents_cli.commands import general

        handlers = {cmd.name: cmd.handler_method for cmd in general.COMMANDS}
        assert handlers["/sidecar"] == "_handle_sidecar_command"
