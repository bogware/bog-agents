"""Expose the bog-agents coding agent **as** an MCP server (ROADMAP killer #4).

bog-agents is already a strong MCP *client* (it can call other MCP servers), but
until now it was an island — no other agent or orchestrator could delegate a
whole coding task *to* it. ``bog-agents mcp-server`` closes that: it builds the
CLI agent in-process and serves it over the Model Context Protocol (stdio), so
Claude Desktop, Cursor, Zed, GitHub Copilot, or another bog-agents instance can
hand us a task via a ``run_task`` tool and get the result back. This turns
bog-agents into a composable node in the multi-agent meshes forming in 2026.

Design notes:
- stdio is the MCP transport, so **nothing may be written to stdout** except the
  protocol itself. All diagnostics go to stderr via ``logging``.
- An MCP client has no interactive approval surface, so tool approval maps onto
  the permission mode: ``bypass`` (approve everything) or ``acceptEdits``
  (smart auto-approval) make the delegated task actually run; ``default``/
  ``plan``/``paranoid`` would stall waiting for a human and are rejected.
- Thread continuity: ``run_task`` accepts an optional ``thread_id`` so a caller
  can resume a prior delegated task (checkpointing is enabled).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

logger = logging.getLogger(__name__)

# Permission modes that can run unattended over MCP (no human approval surface).
_AUTONOMOUS_MODES = {"bypass", "acceptEdits", "accept-edits"}


def _extract_final_text(result: dict[str, Any]) -> str:
    """Pull the agent's final assistant text out of an ``ainvoke`` result.

    Handles both plain-string content and Anthropic-style list-of-block content.

    Args:
        result: The state dict returned by ``agent.ainvoke``.

    Returns:
        The final assistant message text, or a diagnostic string if none found.
    """
    messages = result.get("messages") if isinstance(result, dict) else None
    if not messages:
        return "(agent returned no messages)"
    last = messages[-1]
    content = getattr(last, "content", last)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "\n".join(p for p in parts if p) or "(no text content)"
    return str(content)


async def run_mcp_server(
    *,
    model_name: str | None = None,
    permission_mode: str = "acceptEdits",
    cwd: str | Path | None = None,
    assistant_id: str = "mcp",
) -> int:
    """Run bog-agents as an MCP stdio server exposing a ``run_task`` tool.

    Args:
        model_name: Model spec (e.g. ``anthropic:claude-sonnet-4-6``). ``None``
            auto-detects from config/env.
        permission_mode: Approval posture; must be an autonomous mode
            (``bypass`` or ``acceptEdits``) since MCP has no human approver.
        cwd: Workspace root the agent operates in. Defaults to the process cwd.
        assistant_id: Agent/memory namespace.

    Returns:
        Process exit code (0 on clean shutdown, 2 on bad configuration).
    """
    if permission_mode not in _AUTONOMOUS_MODES:
        logger.error(
            "mcp-server requires an autonomous --permission-mode (bypass or "
            "acceptEdits); %r needs a human approver which MCP stdio can't "
            "provide.",
            permission_mode,
        )
        return 2

    from mcp.server.fastmcp import FastMCP

    from bog_agents_cli.agent import create_cli_agent
    from bog_agents_cli.config import create_model_with_fallback
    from bog_agents_cli.sessions import generate_thread_id

    workspace = Path(cwd) if cwd else Path.cwd()
    # bypass -> approve all; acceptEdits -> smart auto-approval (rule engine).
    auto_approve = permission_mode == "bypass"

    logger.info(
        "Building bog-agents MCP agent (model=%s, mode=%s, cwd=%s)",
        model_name or "(auto)",
        permission_mode,
        workspace,
    )
    resolved = create_model_with_fallback(model_name)
    agent, _backend = create_cli_agent(
        resolved.model,
        assistant_id,
        auto_approve=auto_approve,
        enable_plan_mode=False,
        interactive=False,
        cwd=workspace,
    )

    mcp = FastMCP("bog-agents")

    @mcp.tool()
    async def run_task(prompt: str, thread_id: str | None = None) -> str:
        """Delegate a coding/engineering task to the bog-agents agent.

        The agent runs in the server's workspace with file, shell, and git
        tools and returns its final answer. Pass a prior ``thread_id`` to
        continue an earlier task with full context.

        Args:
            prompt: The task, question, or instruction for the agent.
            thread_id: Optional id of a previous task to resume.

        Returns:
            The agent's final response text.
        """
        from langchain_core.messages import HumanMessage

        tid = thread_id or generate_thread_id()
        config: RunnableConfig = {"configurable": {"thread_id": tid}}
        logger.info("run_task thread=%s prompt=%.80s", tid, prompt)
        try:
            result = await agent.ainvoke(
                {"messages": [HumanMessage(content=prompt)]}, config=config
            )
        except Exception as exc:  # surface a clean error to the MCP client
            logger.exception("run_task failed")
            return f"Task failed: {type(exc).__name__}: {exc}"
        text = _extract_final_text(result)
        return f"[thread:{tid}]\n{text}"

    @mcp.tool()
    def get_info() -> str:
        """Return basic info about this bog-agents MCP server."""
        from bog_agents_cli._version import __version__

        return (
            f"bog-agents MCP server v{__version__}\n"
            f"workspace: {workspace}\n"
            f"model: {model_name or '(auto-detected)'}\n"
            f"permission mode: {permission_mode}\n"
            "tools: run_task(prompt, thread_id?), get_info()"
        )

    logger.info("bog-agents MCP server ready on stdio")
    await mcp.run_stdio_async()
    return 0
