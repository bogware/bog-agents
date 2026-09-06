"""ROADMAP #72: governed code mode — scripts call tools through the real tool path."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar

from langchain.agents.middleware.types import AgentMiddleware
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool

from bog_agents.cost_ledger import CostLedger, RunawayCaps
from bog_agents.middleware.code_mode import CodeModeMiddleware

CALLS: list[tuple[str, dict[str, Any]]] = []


@tool
def add(a: int, b: int) -> str:
    """Add two numbers."""
    CALLS.append(("add", {"a": a, "b": b}))
    return str(a + b)


@tool
def secret(name: str) -> str:
    """Return a secret."""
    CALLS.append(("secret", {"name": name}))
    return f"secret for {name}"


@tool
def task(description: str, subagent_type: str) -> str:
    """Pretend subagent."""
    CALLS.append(("task", {"description": description, "subagent_type": subagent_type}))
    return f"{subagent_type} did: {description}"


class _DenySecret(AgentMiddleware):
    """Stands in for Expert rules: refuses the `secret` tool."""

    def wrap_tool_call(self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Any]) -> Any:
        if request.tool_call["name"] == "secret":
            return ToolMessage(content="denied by policy", tool_call_id=request.tool_call["id"], name="secret", status="error")
        return handler(request)


class _Hitl:
    """Shaped like `HumanInTheLoopMiddleware` for the gate check."""

    interrupt_on: ClassVar[dict[str, bool]] = {"add": False, "secret": True}


_Hitl.__name__ = "HumanInTheLoopMiddleware"


def _mode(**kwargs: Any) -> CodeModeMiddleware:
    CALLS.clear()
    mw = CodeModeMiddleware(timeout=30, **kwargs)
    mw.bind([add, secret, task], [_DenySecret(), mw])
    return mw


def test_script_calls_tools_through_the_chain_and_prints() -> None:
    mw = _mode()
    out = mw.execute(
        "total = tools.add(a=2, b=3)\nprint('total', total)\nprint(fanout(lambda n: tools.add(a=n, b=1), [1, 2, 3]))", runtime=None, namespace=None
    )
    assert "total 5" in out and "['2', '3', '4']" in out
    assert CALLS[0] == ("add", {"a": 2, "b": 3}) and len(CALLS) == 4
    assert [t.name for t in mw.tools] == ["run_code"]
    assert "add, secret, task" in mw.tools[0].description


def test_denied_tool_raises_inside_the_script() -> None:
    mw = _mode()
    out = mw.execute("try:\n    tools.secret(name='x')\nexcept ToolDenied as e:\n    print('blocked:', e)\n", runtime=None, namespace=None)
    assert "blocked: denied by policy" in out
    assert CALLS == []  # the tool body never ran


def test_hitl_gated_tool_is_refused() -> None:
    CALLS.clear()
    mw = CodeModeMiddleware(timeout=30)
    mw.bind([add, secret], [_Hitl(), mw])  # type: ignore[list-item]
    out = mw.execute("print(tools.secret(name='y'))", runtime=None, namespace=None)
    assert "needs human approval" in out and CALLS == []
    out = mw.execute("print(tools.add(a=1, b=1))", runtime=None, namespace=None)
    assert "2" in out


def test_spawns_count_against_runaway_caps() -> None:
    ledger = CostLedger(caps=RunawayCaps(max_subagents=1))
    mw = _mode(cost_ledger=ledger)
    out = mw.execute(
        "print(tools.task(subagent_type='scout', description='one'))\n"
        "try:\n    tools.task(subagent_type='scout', description='two')\nexcept ToolDenied as e:\n    print('capped:', e)\n",
        runtime=None,
        namespace=None,
    )
    assert "scout did: one" in out and "capped: spawn refused" in out
    assert len([c for c in CALLS if c[0] == "task"]) == 1
    assert ledger.subagent_spawns == 1  # the cap refuses without counting


def test_namespace_allowlist_unknown_and_errors() -> None:
    mw = _mode(allowed_tools=["add"])
    out = mw.execute(
        "try:\n    tools.secret(name='z')\nexcept ToolDenied as e:\n    print(e)\nprint(tools.add(a=1, b=2))", runtime=None, namespace=None
    )
    assert "not allowed in code mode" in out and "3" in out
    out = mw.execute("tools.nope(x=1)", runtime=None, namespace=None)
    assert "ERROR" in out and "nope" in out  # outside the allowlist (or unknown)
    out = _mode().execute("tools.nope(x=1)", runtime=None, namespace=None)
    assert "unknown tool" in out
    out = mw.execute("print(1/0)", runtime=None, namespace=None)
    assert "ZeroDivisionError" in out
    assert mw.execute("   ", runtime=None, namespace=None).startswith("ERROR")
    out = mw.execute("print(vote(['a', 'b', 'a']))", runtime=None, namespace=None)
    assert out.strip() == "a"


def test_mcp_namespace_and_timeout() -> None:
    CALLS.clear()
    mw = CodeModeMiddleware(timeout=2, mcp_tool_names=["secret"])
    mw.bind([add, secret], [mw])
    assert [t.name for t in mw.tools] == ["run_code", "execute_mcp_script"]
    out = mw.execute(
        "try:\n    tools.add(a=1, b=1)\nexcept ToolDenied as e:\n    print(e)\nprint(tools.secret(name='m'))", runtime=None, namespace={"secret"}
    )
    assert "outside this script's namespace" in out and "secret for m" in out
    out = mw.execute("import time\ntime.sleep(30)", runtime=None, namespace=None)
    assert "exceeded" in out


def test_call_budget() -> None:
    mw = _mode(max_calls=2)
    out = mw.execute(
        "for i in range(3):\n    try:\n        tools.add(a=i, b=i)\n    except ToolDenied as e:\n        print('stop:', e)\n",
        runtime=None,
        namespace=None,
    )
    assert "call budget exhausted" in out and len(CALLS) == 2


def test_unbound_middleware_refuses() -> None:
    assert CodeModeMiddleware().execute("print(1)", runtime=None, namespace=None).startswith("ERROR: code mode is not bound")
