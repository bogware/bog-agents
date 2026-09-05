"""ROADMAP #72: code mode is wired through FeatureConfig and bound to the assembled agent."""

from __future__ import annotations

from bog_agents.feature_config import FeatureConfig
from bog_agents.graph import create_agent
from bog_agents.middleware.code_mode import CodeModeMiddleware
from bog_agents.token_audit import RecordingChatModel, capture_assembly


def _assemble(config: FeatureConfig, **kwargs: object) -> tuple[list[object], list[object]]:
    captured: dict[str, list[object]] = {}
    with capture_assembly(lambda a: captured.update(mw=list(a.middleware), tools=list(a.tools))):
        create_agent(model=RecordingChatModel(), config=config, **kwargs)  # type: ignore[arg-type]
    return captured["mw"], captured["tools"]


def test_off_by_default() -> None:
    middleware, _tools = _assemble(FeatureConfig())
    assert not any(isinstance(m, CodeModeMiddleware) for m in middleware)


def test_enabled_binds_tools_and_hitl_gates() -> None:
    middleware, _tools = _assemble(
        FeatureConfig(enable_code_mode=True, code_mode_timeout=15, code_mode_max_calls=7),
        interrupt_on={"write_file": True, "read_file": False},
    )
    code = next(m for m in middleware if isinstance(m, CodeModeMiddleware))
    assert "run_code" in {t.name for t in code.tools}
    names = set(code.tool_names)
    assert {"read_file", "write_file", "task"} <= names and "run_code" not in names
    # A HITL-gated tool is refused inside code mode; an ungated one is not.
    assert "needs human approval" in code.invoke_tool("write_file", {"file_path": "/x", "content": "y"}, runtime=None)
    assert "needs human approval" not in code.invoke_tool("read_file", {"file_path": "/missing"}, runtime=None)
    assert code._timeout == 15 and code._max_calls == 7


def test_allowlist_from_config() -> None:
    middleware, _tools = _assemble(FeatureConfig(enable_code_mode=True, code_mode_allowed_tools=["read_file"]))
    code = next(m for m in middleware if isinstance(m, CodeModeMiddleware))
    assert code.tool_names == ["read_file"]
