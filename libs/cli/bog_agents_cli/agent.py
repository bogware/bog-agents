"""Agent management and creation for the CLI."""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bog_agents import create_agent
from bog_agents.backends import CompositeBackend, LocalShellBackend
from bog_agents.backends.filesystem import FilesystemBackend
from bog_agents.middleware import MemoryMiddleware, SkillsMiddleware

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from bog_agents.backends.sandbox import SandboxBackendProtocol
    from bog_agents.middleware.subagents import CompiledSubAgent, SubAgent
    from langchain.agents.middleware import InterruptOnConfig
    from langchain.agents.middleware.types import AgentState
    from langchain.messages import ToolCall
    from langchain.tools import BaseTool
    from langchain_core.language_models import BaseChatModel
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.pregel import Pregel
    from langgraph.runtime import Runtime

    from bog_agents_cli.mcp_tools import MCPServerInfo
    from bog_agents_cli.output import OutputFormat

from bog_agents_cli.config import (
    COLORS,
    config,
    console,
    get_default_coding_instructions,
    get_glyphs,
    settings,
)
from bog_agents_cli.configurable_model import ConfigurableModelMiddleware
from bog_agents_cli.integrations.sandbox_factory import get_default_working_dir
from bog_agents_cli.local_context import LocalContextMiddleware, _ExecutableBackend
from bog_agents_cli.project_utils import ProjectContext, get_server_project_context
from bog_agents_cli.subagents import list_subagents
from bog_agents_cli.unicode_security import (
    check_url_safety,
    detect_dangerous_unicode,
    format_warning_detail,
    render_with_unicode_markers,
    strip_dangerous_unicode,
    summarize_issues,
)

logger = logging.getLogger(__name__)

DEFAULT_AGENT_NAME = "agent"
"""The default agent name used when no `-a` flag is provided."""

REQUIRE_COMPACT_TOOL_APPROVAL: bool = True
"""When `True`, `compact_conversation` requires HITL approval like other gated tools."""

_RESERVED_AGENT_HOME_DIRS = frozenset(
    {
        "daemon",  # bog-agents-daemon state (token, runs/, daemon.pid)
        "logs",
        "pipelines",  # CLI pipeline definitions, not an agent
        "plugins",
        "skills",
    }
)
"""Directories under `~/.bog-agents` reserved for global CLI state, not agents."""


def _iter_listed_agent_dirs(agents_dir: Path) -> list[Path]:
    """Return agent directories that should appear in `bog-agents list`.

    The user-level `.bog-agents` directory also contains shared CLI state
    such as logs and plugin installs. Filter those reserved directories
    so `list` only shows real agent workspaces.

    Args:
        agents_dir: Base `~/.bog-agents` directory.

    Returns:
        Sorted list of agent directories.
    """
    return [
        agent_path
        for agent_path in sorted(agents_dir.iterdir())
        if (agent_path.is_dir() and agent_path.name not in _RESERVED_AGENT_HOME_DIRS)
    ]


def list_agents(*, output_format: OutputFormat = "text") -> None:
    """List all available agents.

    Args:
        output_format: Output format — `'text'` (Rich) or `'json'`.
    """
    agents_dir = settings.user_agents_dir

    if not agents_dir.exists() or not any(agents_dir.iterdir()):
        if output_format == "json":
            from bog_agents_cli.output import write_json

            write_json("list", [])
            return
        console.print("[yellow]No agents found.[/yellow]")
        console.print(
            "[dim]Agents will be created in ~/.bog-agents/ "
            "when you first use them.[/dim]",
            style=COLORS["dim"],
        )
        return

    agent_dirs = _iter_listed_agent_dirs(agents_dir)

    if not agent_dirs:
        if output_format == "json":
            from bog_agents_cli.output import write_json

            write_json("list", [])
            return
        console.print("[yellow]No agents found.[/yellow]")
        console.print(
            "[dim]Agents will be created in ~/.bog-agents/ "
            "when you first use them.[/dim]",
            style=COLORS["dim"],
        )
        return

    if output_format == "json":
        from bog_agents_cli.output import write_json

        agents = []
        for agent_path in agent_dirs:
            agent_name = agent_path.name
            agents.append(
                {
                    "name": agent_name,
                    "path": str(agent_path),
                    "has_agents_md": (agent_path / "AGENTS.md").exists(),
                    "is_default": agent_name == DEFAULT_AGENT_NAME,
                }
            )
        write_json("list", agents)
        return

    console.print("\n[bold]Available Agents:[/bold]\n", style=COLORS["primary"])

    for agent_path in agent_dirs:
        agent_name = agent_path.name
        agent_md = agent_path / "AGENTS.md"
        is_default = agent_name == DEFAULT_AGENT_NAME
        default_label = " [dim](default)[/dim]" if is_default else ""

        bullet = get_glyphs().bullet
        if agent_md.exists():
            console.print(
                f"  {bullet} [bold]{agent_name}[/bold]{default_label}",
                style=COLORS["primary"],
            )
            console.print(f"    {agent_path}", style=COLORS["dim"])
        else:
            console.print(
                f"  {bullet} [bold]{agent_name}[/bold]{default_label}"
                " [dim](incomplete)[/dim]",
                style=COLORS["tool"],
            )
            console.print(f"    {agent_path}", style=COLORS["dim"])

    console.print()


def reset_agent(
    agent_name: str,
    source_agent: str | None = None,
    *,
    output_format: OutputFormat = "text",
) -> None:
    """Reset an agent to default or copy from another agent.

    Args:
        agent_name: Name of the agent to reset.
        source_agent: Copy AGENTS.md from this agent instead of default.
        output_format: Output format — `'text'` (Rich) or `'json'`.
    """
    agents_dir = settings.user_agents_dir
    agent_dir = agents_dir / agent_name

    if source_agent:
        source_dir = agents_dir / source_agent
        source_md = source_dir / "AGENTS.md"

        if not source_md.exists():
            console.print(
                f"[bold red]Error:[/bold red] Source agent '{source_agent}' not found "
                "or has no AGENTS.md"
            )
            return

        source_content = source_md.read_text()
        action_desc = f"contents of agent '{source_agent}'"
    else:
        source_content = get_default_coding_instructions()
        action_desc = "default"

    if agent_dir.exists():
        shutil.rmtree(agent_dir)
        if output_format != "json":
            console.print(
                f"Removed existing agent directory: {agent_dir}", style=COLORS["tool"]
            )

    agent_dir.mkdir(parents=True, exist_ok=True)
    agent_md = agent_dir / "AGENTS.md"
    agent_md.write_text(source_content)

    if output_format == "json":
        from bog_agents_cli.output import write_json

        write_json(
            "reset",
            {
                "agent": agent_name,
                "reset_to": source_agent or "default",
                "path": str(agent_dir),
            },
        )
        return

    console.print(
        f"{get_glyphs().checkmark} Agent '{agent_name}' reset to {action_desc}",
        style=COLORS["primary"],
    )
    console.print(f"Location: {agent_dir}\n", style=COLORS["dim"])


def get_system_prompt(
    assistant_id: str,
    sandbox_type: str | None = None,
    *,
    interactive: bool = True,
    cwd: str | Path | None = None,
) -> str:
    """Get the base system prompt for the agent.

    Loads the base system prompt template from `system_prompt.md` and
    interpolates dynamic sections (model identity, working directory,
    skills path, execution mode).

    Args:
        assistant_id: The agent identifier for path references
        sandbox_type: Type of sandbox provider
            (`'daytona'`, `'langsmith'`, `'modal'`, `'runloop'`).

            If `None`, agent is operating in local mode.
        interactive: When `False`, the prompt is tailored for headless
            non-interactive execution (no human in the loop).
        cwd: Override the working directory shown in the prompt.

    Returns:
        The system prompt string

    Example:
        ```txt
        You are running as model {MODEL} (provider: {PROVIDER}).

        Your context window is {CONTEXT_WINDOW} tokens.

        ... {CONDITIONAL SECTIONS} ...
        ```
    """
    template = (Path(__file__).parent / "system_prompt.md").read_text()

    skills_path = f"~/.bog-agents/{assistant_id}/skills"

    if interactive:
        mode_description = "an interactive CLI on the user's computer"
        interactive_preamble = (
            "The user sends you messages and you respond with text and tool "
            "calls. Your tools run on the user's machine. The user can see "
            "your responses and tool outputs in real time, so keep them "
            "informed — but don't over-explain."
        )
        ambiguity_guidance = (
            "- If the request is ambiguous, ask questions before acting.\n"
            "- If asked how to approach something, explain first, then act."
        )
    else:
        mode_description = (
            "non-interactive (headless) mode — there is no human operator "
            "monitoring your output in real time"
        )
        interactive_preamble = (
            "You received a single task and must complete it fully and "
            "autonomously. There is no human available to answer follow-up "
            "questions, so do NOT ask for clarification — make reasonable "
            "assumptions and proceed."
        )
        ambiguity_guidance = (
            "- Do NOT ask clarifying questions — there is no human to answer "
            "them. Make reasonable assumptions and proceed.\n"
            "- If you encounter ambiguity, choose the most reasonable "
            "interpretation and note your assumption briefly.\n"
            "- Always use non-interactive command variants — no human is "
            "available to respond to prompts. Examples: `npm init -y` not "
            "`npm init`, `apt-get install -y` not `apt-get install`, "
            "`yes |` or `--no-input`/`--non-interactive` flags where "
            "available. Never run commands that block waiting for stdin."
        )

    # Build model identity section
    model_identity_section = ""
    if settings.model_name:
        model_identity_section = (
            f"### Model Identity\n\nYou are running as model `{settings.model_name}`"
        )
        if settings.model_provider:
            model_identity_section += f" (provider: {settings.model_provider})"
        model_identity_section += ".\n"
        if settings.model_context_limit:
            model_identity_section += (
                f"Your context window is {settings.model_context_limit:,} tokens.\n"
            )
        model_identity_section += "\n"

    # Build working directory section (local vs sandbox)
    if sandbox_type:
        working_dir = get_default_working_dir(sandbox_type)
        working_dir_section = (
            f"### Current Working Directory\n\n"
            f"You are operating in a **remote Linux sandbox** at `{working_dir}`.\n\n"
            f"All code execution and file operations happen in this sandbox "
            f"environment.\n\n"
            f"**Important:**\n"
            f"- The CLI is running locally on the user's machine, but you execute "
            f"code remotely\n"
            f"- Use `{working_dir}` as your working directory for all operations\n\n"
        )
    else:
        if cwd is not None:
            resolved_cwd = Path(cwd)
        else:
            try:
                resolved_cwd = Path.cwd()
            except OSError:
                logger.warning(
                    "Could not determine working directory for system prompt",
                    exc_info=True,
                )
                resolved_cwd = Path()
        cwd = resolved_cwd
        working_dir_section = (
            f"### Current Working Directory\n\n"
            f"The filesystem backend is currently operating in: `{cwd}`\n\n"
            f"### File System and Paths\n\n"
            f"**IMPORTANT - Path Handling:**\n"
            f"- All file paths must be absolute paths (e.g., `{cwd}/file.txt`)\n"
            f"- Use the working directory to construct absolute paths\n"
            f"- Example: To create a file in your working directory, "
            f"use `{cwd}/research_project/file.md`\n"
            f"- Never use relative paths - always construct full absolute paths\n\n"
        )

    result = (
        template.replace("{mode_description}", mode_description)
        .replace("{interactive_preamble}", interactive_preamble)
        .replace("{ambiguity_guidance}", ambiguity_guidance)
        .replace("{model_identity_section}", model_identity_section)
        .replace("{working_dir_section}", working_dir_section)
        .replace("{skills_path}", skills_path)
    )

    # Detect unreplaced placeholders (defense-in-depth for template typos)
    unreplaced = re.findall(r"\{[a-z_]+\}", result)
    if unreplaced:
        logger.warning("System prompt contains unreplaced placeholders: %s", unreplaced)

    # Append project + global memory if present
    from bog_agents_cli.project_memory import load_project_memory

    memory_block = load_project_memory(cwd=cwd)
    if memory_block:
        result = result + memory_block

    return result


def _format_write_file_description(
    tool_call: ToolCall, _state: AgentState[Any], _runtime: Runtime[Any]
) -> str:
    """Format write_file tool call for approval prompt.

    Returns:
        Formatted description string for the write_file tool call.
    """
    args = tool_call["args"]
    file_path = args.get("file_path", "unknown")
    content = args.get("content", "")

    action = "Overwrite" if Path(file_path).exists() else "Create"
    line_count = len(content.splitlines())

    return f"File: {file_path}\nAction: {action} file\nLines: {line_count}"


def _format_edit_file_description(
    tool_call: ToolCall, _state: AgentState[Any], _runtime: Runtime[Any]
) -> str:
    """Format edit_file tool call for approval prompt.

    Returns:
        Formatted description string for the edit_file tool call.
    """
    args = tool_call["args"]
    file_path = args.get("file_path", "unknown")
    replace_all = bool(args.get("replace_all", False))

    scope = "all occurrences" if replace_all else "single occurrence"
    return f"File: {file_path}\nAction: Replace text ({scope})"


def _format_web_search_description(
    tool_call: ToolCall, _state: AgentState[Any], _runtime: Runtime[Any]
) -> str:
    """Format web_search tool call for approval prompt.

    Returns:
        Formatted description string for the web_search tool call.
    """
    args = tool_call["args"]
    query = args.get("query", "unknown")
    max_results = args.get("max_results", 5)

    return (
        f"Query: {query}\nMax results: {max_results}\n\n"
        f"{get_glyphs().warning}  This will use Tavily API credits"
    )


def _format_fetch_url_description(
    tool_call: ToolCall, _state: AgentState[Any], _runtime: Runtime[Any]
) -> str:
    """Format fetch_url tool call for approval prompt.

    Returns:
        Formatted description string for the fetch_url tool call.
    """
    args = tool_call["args"]
    url = str(args.get("url", "unknown"))
    display_url = strip_dangerous_unicode(url)
    timeout = args.get("timeout", 30)
    safety = check_url_safety(url)

    warning_lines: list[str] = []
    if not safety.safe:
        detail = format_warning_detail(safety.warnings)
        warning_lines.append(f"{get_glyphs().warning}  URL warning: {detail}")
    if safety.decoded_domain:
        warning_lines.append(
            f"{get_glyphs().warning}  Decoded domain: {safety.decoded_domain}"
        )

    warning_block = "\n".join(warning_lines)
    if warning_block:
        warning_block = f"\n{warning_block}"

    return (
        f"URL: {display_url}\nTimeout: {timeout}s\n\n"
        f"{get_glyphs().warning}  Will fetch and convert web content to markdown"
        f"{warning_block}"
    )


def _format_task_description(
    tool_call: ToolCall, _state: AgentState[Any], _runtime: Runtime[Any]
) -> str:
    """Format task (subagent) tool call for approval prompt.

    The task tool signature is: task(description: str, subagent_type: str)
    The description contains all instructions that will be sent to the subagent.

    Returns:
        Formatted description string for the task tool call.
    """
    args = tool_call["args"]
    description = args.get("description", "unknown")
    subagent_type = args.get("subagent_type", "unknown")

    # Truncate description if too long for display
    description_preview = description
    if len(description) > 500:  # Subagent description length threshold
        description_preview = description[:500] + "..."

    glyphs = get_glyphs()
    separator = glyphs.box_horizontal * 40
    warning_msg = "Subagent will have access to file operations and shell commands"
    return (
        f"Subagent Type: {subagent_type}\n\n"
        f"Task Instructions:\n"
        f"{separator}\n"
        f"{description_preview}\n"
        f"{separator}\n\n"
        f"{glyphs.warning}  {warning_msg}"
    )


def _format_execute_description(
    tool_call: ToolCall, _state: AgentState[Any], _runtime: Runtime[Any]
) -> str:
    """Format execute tool call for approval prompt.

    Returns:
        Formatted description string for the execute tool call.
    """
    args = tool_call["args"]
    command_raw = str(args.get("command", "N/A"))
    command = strip_dangerous_unicode(command_raw)
    project_context = get_server_project_context()
    effective_cwd = (
        str(project_context.user_cwd)
        if project_context is not None
        else str(Path.cwd())
    )
    lines = [f"Execute Command: {command}", f"Working Directory: {effective_cwd}"]

    issues = detect_dangerous_unicode(command_raw)
    if issues:
        summary = summarize_issues(issues)
        lines.append(f"{get_glyphs().warning}  Hidden Unicode detected: {summary}")
        raw_marked = render_with_unicode_markers(command_raw)
        if len(raw_marked) > 220:  # UI display truncation threshold
            raw_marked = raw_marked[:220] + "..."
        lines.append(f"Raw: {raw_marked}")

    return "\n".join(lines)


def _add_interrupt_on() -> dict[str, InterruptOnConfig]:
    """Configure human-in-the-loop interrupt settings for all gated tools.

    Every tool that can have side effects or access external resources
    (shell execution, file writes/edits, web search, URL fetch, task
    delegation) is gated behind an approval prompt unless auto-approve
    is enabled.

    Returns:
        Dictionary mapping tool names to their interrupt configuration.
    """
    execute_interrupt_config: InterruptOnConfig = {
        "allowed_decisions": ["approve", "reject"],
        "description": _format_execute_description,  # type: ignore[typeddict-item]  # Callable description narrower than TypedDict expects
    }

    write_file_interrupt_config: InterruptOnConfig = {
        "allowed_decisions": ["approve", "reject"],
        "description": _format_write_file_description,  # type: ignore[typeddict-item]  # Callable description narrower than TypedDict expects
    }

    edit_file_interrupt_config: InterruptOnConfig = {
        "allowed_decisions": ["approve", "reject"],
        "description": _format_edit_file_description,  # type: ignore[typeddict-item]  # Callable description narrower than TypedDict expects
    }

    web_search_interrupt_config: InterruptOnConfig = {
        "allowed_decisions": ["approve", "reject"],
        "description": _format_web_search_description,  # type: ignore[typeddict-item]  # Callable description narrower than TypedDict expects
    }

    fetch_url_interrupt_config: InterruptOnConfig = {
        "allowed_decisions": ["approve", "reject"],
        "description": _format_fetch_url_description,  # type: ignore[typeddict-item]  # Callable description narrower than TypedDict expects
    }

    task_interrupt_config: InterruptOnConfig = {
        "allowed_decisions": ["approve", "reject"],
        "description": _format_task_description,  # type: ignore[typeddict-item]  # Callable description narrower than TypedDict expects
    }

    interrupt_map: dict[str, InterruptOnConfig] = {
        "execute": execute_interrupt_config,
        "write_file": write_file_interrupt_config,
        "edit_file": edit_file_interrupt_config,
        "web_search": web_search_interrupt_config,
        "fetch_url": fetch_url_interrupt_config,
        "task": task_interrupt_config,
    }

    if REQUIRE_COMPACT_TOOL_APPROVAL:
        interrupt_map["compact_conversation"] = {
            "allowed_decisions": ["approve", "reject"],
            "description": (
                "Summarizes older messages into a shorter summary "
                "using an LLM call, then replaces them in context. "
                "Recent messages are kept as-is. Full history is "
                "written to backend storage for agent retrieval."
            ),
        }

    return interrupt_map


def create_cli_agent(
    model: str | BaseChatModel,
    assistant_id: str,
    *,
    tools: Sequence[BaseTool | Callable | dict[str, Any]] | None = None,
    sandbox: SandboxBackendProtocol | None = None,
    sandbox_type: str | None = None,
    system_prompt: str | None = None,
    interactive: bool = True,
    auto_approve: bool = False,
    enable_memory: bool = True,
    enable_skills: bool = True,
    enable_shell: bool = True,
    enable_git_tools: bool = True,
    enable_repo_map: bool = False,
    enable_checkpointing: bool = True,
    enable_cost_tracking: bool = True,
    enable_plan_mode: bool = True,
    effort_level: str = "high",
    budget_usd: float = 0.0,
    auto_lint: bool = False,
    auto_test: bool = False,
    profile: str = "",
    checkpointer: BaseCheckpointSaver | None = None,
    mcp_server_info: list[MCPServerInfo] | None = None,
    cwd: str | Path | None = None,
    project_context: ProjectContext | None = None,
) -> tuple[Pregel, CompositeBackend]:
    """Create a CLI-configured agent with flexible options.

    This is the main entry point for creating a bog-agents CLI agent, usable
    both internally and from external code (e.g., benchmarking frameworks).

    Args:
        model: LLM model to use (e.g., `'anthropic:claude-sonnet-4-6'`)
        assistant_id: Agent identifier for memory/state storage
        tools: Additional tools to provide to agent
        sandbox: Optional sandbox backend for remote execution
            (e.g., `ModalBackend`).

            If `None`, uses local filesystem + shell.
        sandbox_type: Type of sandbox provider
            (`'daytona'`, `'langsmith'`, `'modal'`, `'runloop'`).
            Used for system prompt generation.
        system_prompt: Override the default system prompt.

            If `None`, generates one based on `sandbox_type`, `assistant_id`,
            and `interactive`.
        interactive: When `False`, the auto-generated system prompt is
            tailored for headless non-interactive execution. Ignored when
            `system_prompt` is provided explicitly.
        auto_approve: If `True`, no tools trigger human-in-the-loop
            interrupts — all calls (shell execution, file writes/edits,
            web search, URL fetch) run automatically.

            If `False`, tools pause for user confirmation via the approval menu.
            See `_add_interrupt_on` for the full list of gated tools.
        enable_memory: Enable `MemoryMiddleware` for persistent memory
        enable_skills: Enable `SkillsMiddleware` for custom agent skills
        enable_shell: Enable shell execution via `LocalShellBackend`
            (only in local mode). When enabled, the `execute` tool is available.
        checkpointer: Optional checkpointer for session persistence.
            When `None`, the graph is compiled without a checkpointer.
        mcp_server_info: MCP server metadata to surface in the system prompt.
        enable_git_tools: Enable built-in git workflow tools.
        enable_repo_map: Enable repository structural map middleware.
        enable_checkpointing: Enable git-based checkpointing before file changes.
        enable_cost_tracking: Enable token/cost tracking middleware.
        enable_plan_mode: Enable read-only plan mode toggle.
        effort_level: AI effort level ('low', 'medium', 'high', 'max').
        budget_usd: Maximum budget in USD (0 = unlimited).
        auto_lint: Auto-run linter after file edits.
        auto_test: Auto-run tests after file edits.
        profile: Configuration profile name to apply.
        cwd: Override the working directory for the agent's filesystem backend
            and system prompt.
        project_context: Explicit project path context for project-sensitive
            behavior such as project `AGENTS.md` files, skills, subagents, and
            MCP trust.

    Returns:
        2-tuple of `(agent_graph, backend)`

            - `agent_graph`: Configured LangGraph Pregel instance ready
                for execution
            - `composite_backend`: `CompositeBackend` for file operations
    """
    if isinstance(model, str):
        from bog_agents_cli.config import create_model as _create_model

        model = _create_model(model).model

    tools = tools or []
    effective_cwd = (
        Path(cwd)
        if cwd is not None
        else (project_context.user_cwd if project_context is not None else None)
    )

    # Setup agent directory for persistent memory (if enabled)
    if enable_memory or enable_skills:
        agent_dir = settings.ensure_agent_dir(assistant_id)
        agent_md = agent_dir / "AGENTS.md"
        if not agent_md.exists():
            # Create empty file for user customizations
            # Base instructions are loaded fresh from get_system_prompt()
            agent_md.touch()

    # Skills directories (if enabled)
    skills_dir = None
    user_agent_skills_dir = None
    project_skills_dir = None
    project_agent_skills_dir = None
    if enable_skills:
        skills_dir = settings.ensure_user_skills_dir(assistant_id)
        user_agent_skills_dir = settings.get_user_agent_skills_dir()
        project_skills_dir = (
            project_context.project_skills_dir()
            if project_context is not None
            else settings.get_project_skills_dir()
        )
        project_agent_skills_dir = (
            project_context.project_agent_skills_dir()
            if project_context is not None
            else settings.get_project_agent_skills_dir()
        )

    # Load custom subagents from filesystem
    custom_subagents: list[SubAgent | CompiledSubAgent] = []
    user_agents_dir = settings.get_user_agents_dir(assistant_id)
    project_agents_dir = (
        project_context.project_agents_dir()
        if project_context is not None
        else settings.get_project_agents_dir()
    )

    # Bundled-agents seeding: if the project is Python/Node/Rust/Go and
    # the user hasn't authored their own subagents, this pulls in
    # code-reviewer, test-author, and language-specific specialists from
    # the package's bundled_agents/ tree. User and project subagents
    # override on name conflict.
    project_root_for_bundled = effective_cwd if effective_cwd is not None else None
    for subagent_meta in list_subagents(
        user_agents_dir=user_agents_dir,
        project_agents_dir=project_agents_dir,
        project_root=project_root_for_bundled,
    ):
        subagent: SubAgent = {
            "name": subagent_meta["name"],
            "description": subagent_meta["description"],
            "system_prompt": subagent_meta["system_prompt"],
        }
        if subagent_meta["model"]:
            subagent["model"] = subagent_meta["model"]
        custom_subagents.append(subagent)

    # Build middleware stack based on enabled features
    agent_middleware = []
    agent_middleware.append(ConfigurableModelMiddleware())

    # Auto-enable tool-call parser for Ollama models. Many local models emit
    # tool calls as text (Mistral [TOOL_CALLS], Hermes <tool_call>, fenced
    # JSON) instead of using OpenAI's structured tool_calls field; the parser
    # recovers them so the agent loop can proceed. No-op for cloud providers.
    if (settings.model_provider or "").lower() == "ollama":
        from bog_agents.middleware import ToolCallParserMiddleware

        agent_middleware.append(ToolCallParserMiddleware())

    # Add ask_user middleware (must be early so its tool is available).
    # Skip in non-interactive mode: there is no user to answer, and a stray
    # `ask_user` call mid-run produces a malformed HITL interrupt that the
    # CLI rejects, which derails the agent without recourse. Headless agents
    # should make a best-effort decision and proceed instead.
    if interactive:
        from bog_agents_cli.ask_user import AskUserMiddleware

        agent_middleware.append(AskUserMiddleware())

    # Add memory middleware
    if enable_memory:
        memory_sources = [str(settings.get_user_agent_md_path(assistant_id))]
        if project_context is not None:
            # Walk home → project → ancestor dirs → cwd so the deepest
            # AGENTS.md (closest to the user's actual cwd) is loaded last
            # and gets the most attention from the model.
            hierarchical_paths = project_context.hierarchical_agent_md_paths()
        else:
            hierarchical_paths = list(settings.get_project_agent_md_path())
        memory_sources.extend(str(p) for p in hierarchical_paths)

        agent_middleware.append(
            MemoryMiddleware(
                # virtual_mode=False: memory sources are CLI-controlled absolute paths
                # spanning multiple roots (user home + project), not agent-supplied input.
                backend=FilesystemBackend(virtual_mode=False),
                sources=memory_sources,
            )
        )

    # Add skills middleware
    if enable_skills:
        from bog_agents_cli.extensibility import get_extension_skill_dirs

        # Lowest to highest precedence:
        # built-in -> extensions -> user .bog-agents -> user .agents
        # -> project .bog-agents -> project .agents
        extension_config_dir = (
            settings.user_agents_dir
            if isinstance(getattr(settings, "user_agents_dir", None), Path)
            else (
                user_agents_dir.parent
                if user_agents_dir.name == "agents"
                else user_agents_dir
            )
        )
        sources = [str(settings.get_built_in_skills_dir())]
        sources.extend(
            str(path) for path in get_extension_skill_dirs(extension_config_dir)
        )
        sources.extend([str(skills_dir), str(user_agent_skills_dir)])
        if project_skills_dir:
            sources.append(str(project_skills_dir))
        if project_agent_skills_dir:
            sources.append(str(project_agent_skills_dir))

        # Hierarchical skill layering: walk from project root → cwd so a
        # subdirectory can override skills from a shallower .bog-agents/
        # skills directory. The deepest layer loads last, so SkillsMiddleware
        # honours its "last source wins on name conflict" rule.
        if project_context is not None:
            seen_skill_dirs = {Path(s).resolve() for s in sources if Path(s).exists()}
            for hier_dir in project_context.hierarchical_skill_dirs():
                resolved = hier_dir.resolve()
                if resolved in seen_skill_dirs:
                    continue
                seen_skill_dirs.add(resolved)
                sources.append(str(hier_dir))

        agent_middleware.append(
            SkillsMiddleware(
                # virtual_mode=False: skill sources are CLI-controlled absolute paths
                # spanning multiple roots (built-in, extensions, user, project).
                backend=FilesystemBackend(virtual_mode=False),
                sources=sources,
            )
        )

    # CONDITIONAL SETUP: Local vs Remote Sandbox
    if sandbox is None:
        # ========== LOCAL MODE ==========
        root_dir = effective_cwd if effective_cwd is not None else Path.cwd()
        if enable_shell:
            # Create environment for shell commands
            # Restore user's original LANGSMITH_PROJECT so their code traces separately
            shell_env = os.environ.copy()
            if settings.user_langchain_project:
                shell_env["LANGSMITH_PROJECT"] = settings.user_langchain_project

            # Use LocalShellBackend for filesystem + shell execution.
            # The SDK's FilesystemMiddleware exposes per-command timeout
            # on the execute tool natively.
            backend = LocalShellBackend(
                root_dir=root_dir,
                inherit_env=True,
                env=shell_env,
            )
        else:
            # No shell access - use plain FilesystemBackend
            # virtual_mode=False: agent file tools use real absolute paths.
            # Explicit to survive SDK default flips (0.7.1 changed default to True).
            backend = FilesystemBackend(root_dir=root_dir, virtual_mode=False)
    else:
        # ========== REMOTE SANDBOX MODE ==========
        backend = sandbox  # Remote sandbox (ModalBackend, etc.)
        # Note: Shell middleware not used in sandbox mode
        # File operations and execute tool are provided by the sandbox backend

    # Local context middleware (git info, directory tree, etc.)
    # Uses backend.execute() so it works in both local shell and remote sandbox modes.
    # Only enabled when the backend supports shell execution.
    if isinstance(backend, _ExecutableBackend):
        agent_middleware.append(
            LocalContextMiddleware(backend=backend, mcp_server_info=mcp_server_info)
        )

    # Get or use custom system prompt
    if system_prompt is None:
        system_prompt = get_system_prompt(
            assistant_id=assistant_id,
            sandbox_type=sandbox_type,
            interactive=interactive,
            cwd=effective_cwd,
        )

    # Configure interrupt_on based on auto_approve setting
    interrupt_on: dict[str, bool | InterruptOnConfig] | None = None
    if auto_approve:  # noqa: SIM108  # if-else more readable for interrupt_on config
        # No interrupts - all tools run automatically
        interrupt_on = {}
    else:
        # Full HITL for destructive operations
        interrupt_on = _add_interrupt_on()  # type: ignore[assignment]  # InterruptOnConfig is compatible at runtime

    # Set up composite backend with routing
    # For local FilesystemBackend, route large tool results to a temp directory to avoid
    # polluting the working directory. For sandbox backends, no special routing is needed.
    if sandbox is None:
        # Local mode: Route large results to a unique temp directory
        large_results_backend = FilesystemBackend(
            root_dir=tempfile.mkdtemp(prefix="bog_agents_large_results_"),
            virtual_mode=True,
        )
        conversation_history_backend = FilesystemBackend(
            root_dir=tempfile.mkdtemp(prefix="bog_agents_conversation_history_"),
            virtual_mode=True,
        )
        composite_backend = CompositeBackend(
            default=backend,
            routes={
                "/large_tool_results/": large_results_backend,
                "/conversation_history/": conversation_history_backend,
            },
        )
    else:
        # Sandbox mode: No special routing needed
        composite_backend = CompositeBackend(
            default=backend,
            routes={},
        )

    from bog_agents.middleware.summarization import create_summarization_tool_middleware

    agent_middleware.append(
        create_summarization_tool_middleware(model, composite_backend)
    )

    # Apply profile overrides (if specified)
    if profile:
        from bog_agents_cli.profiles import load_profiles

        profiles = load_profiles(settings.user_agents_dir)
        if profile in profiles:
            p = profiles[profile]
            if p.effort_level:
                effort_level = p.effort_level
            if p.auto_approve is not None:
                auto_approve = p.auto_approve
            if p.enable_git_tools is not None:
                enable_git_tools = p.enable_git_tools
            if p.enable_repo_map is not None:
                enable_repo_map = p.enable_repo_map
            if p.auto_lint is not None:
                auto_lint = p.auto_lint
            if p.auto_test is not None:
                auto_test = p.auto_test

    # Git tools middleware (#15, #43)
    if enable_git_tools and sandbox is None:
        from bog_agents.middleware.git_tools import GitToolsMiddleware

        working_dir = effective_cwd or Path.cwd()
        agent_middleware.append(GitToolsMiddleware(working_dir=working_dir))

    # Repository map middleware (#13)
    if enable_repo_map and sandbox is None:
        from bog_agents.middleware.repo_map import RepoMapMiddleware

        working_dir = effective_cwd or Path.cwd()
        agent_middleware.append(RepoMapMiddleware(working_dir=working_dir))

    # Checkpointing middleware (#3, #5, #39, #43)
    if enable_checkpointing and sandbox is None:
        from bog_agents.middleware.checkpointing import CheckpointingMiddleware

        working_dir = effective_cwd or Path.cwd()
        agent_middleware.append(CheckpointingMiddleware(working_dir=working_dir))

    # Cost tracking middleware (#8, #34, #36, #47)
    if enable_cost_tracking:
        from bog_agents.middleware.cost_tracker import CostTrackerMiddleware

        agent_middleware.append(
            CostTrackerMiddleware(
                effort_level=effort_level,
                budget_usd=budget_usd if budget_usd > 0 else None,
            )
        )

    # Plan mode middleware (#38)
    if enable_plan_mode:
        from bog_agents.middleware.plan_mode import PlanModeMiddleware

        agent_middleware.append(PlanModeMiddleware())

    # Auto quality middleware (#11, #12, #44)
    if auto_lint or auto_test:
        from bog_agents.middleware.auto_quality import AutoQualityMiddleware

        working_dir = effective_cwd or Path.cwd()
        agent_middleware.append(
            AutoQualityMiddleware(
                working_dir=working_dir,
                auto_lint=auto_lint,
                auto_test=auto_test,
            )
        )

    # Worktree isolation middleware (Feature #1)
    if sandbox is None and enable_git_tools:
        from bog_agents.middleware.worktree import WorktreeMiddleware

        working_dir = effective_cwd or Path.cwd()
        agent_middleware.append(WorktreeMiddleware(working_dir=working_dir))

    # Multi-agent orchestrator middleware (Features #2-6)
    from bog_agents.middleware.multi_agent_orchestrator import (
        MultiAgentOrchestratorMiddleware,
    )

    agent_middleware.append(MultiAgentOrchestratorMiddleware())

    # Smart context middleware (Features #13-18)
    from bog_agents.middleware.smart_context import SmartContextMiddleware

    working_dir = effective_cwd or Path.cwd()
    agent_middleware.append(SmartContextMiddleware(working_dir=working_dir))

    # Conversation branching middleware (Features #14, #16)
    from bog_agents.middleware.conversation_branch import ConversationBranchMiddleware

    agent_middleware.append(ConversationBranchMiddleware(working_dir=working_dir))

    # Image input middleware (Features #19-23)
    from bog_agents.middleware.image_input import ImageInputMiddleware

    agent_middleware.append(ImageInputMiddleware(working_dir=working_dir))

    # Browser agent middleware (Features #24-27)
    from bog_agents.middleware.browser_agent import BrowserAgentMiddleware

    agent_middleware.append(BrowserAgentMiddleware(working_dir=working_dir))

    # PR management middleware (Features #28-34)
    if sandbox is None:
        from bog_agents.middleware.pr_management import PRManagementMiddleware

        agent_middleware.append(PRManagementMiddleware(working_dir=working_dir))

    # Test generation middleware (Features #35-38, 40)
    from bog_agents.middleware.test_generation import TestGenerationMiddleware

    agent_middleware.append(TestGenerationMiddleware(working_dir=working_dir))

    # Enterprise middleware (Features #51-57)
    from bog_agents.middleware.enterprise import EnterpriseMiddleware

    agent_middleware.append(EnterpriseMiddleware(working_dir=working_dir))

    # Multi-model middleware (Features #58, #72, #73)
    from bog_agents.middleware.multi_model import MultiModelMiddleware

    agent_middleware.append(MultiModelMiddleware())

    # Code intelligence middleware (Features #59-75)
    from bog_agents.middleware.code_intelligence import CodeIntelligenceMiddleware

    agent_middleware.append(CodeIntelligenceMiddleware(working_dir=working_dir))

    # Plugin system middleware (Features #7-12)
    from bog_agents.middleware.plugin_system import PluginSystemMiddleware

    agent_middleware.append(PluginSystemMiddleware())

    # Notifications middleware (Features #42-47, 49)
    from bog_agents.middleware.notifications import NotificationsMiddleware

    agent_middleware.append(NotificationsMiddleware())

    # Create the agent
    agent = create_agent(
        model=model,
        system_prompt=system_prompt,
        tools=tools,
        backend=composite_backend,
        middleware=agent_middleware,
        interrupt_on=interrupt_on,
        checkpointer=checkpointer,
        subagents=custom_subagents or None,
    ).with_config(config)
    return agent, composite_backend
