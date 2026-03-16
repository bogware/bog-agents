"""Saved prompts middleware for loading and using prompt libraries.

Feature #47: Load, list, and use saved prompts from both local directories
and remote sources (git repos, HTTP endpoints).

## Overview

Saved prompts are reusable prompt templates stored as Markdown files with
YAML frontmatter. They can be organized in local directories or fetched
from remote git repositories and HTTP endpoints.

## Prompt Structure

Each prompt is a `.md` file with YAML frontmatter:

```markdown
---
name: quarterly-review
description: Generate a quarterly portfolio review for a client
category: reports
variables:
  - client_name
  - quarter
  - year
---

# Quarterly Portfolio Review

Generate a comprehensive quarterly review for {{client_name}} covering
Q{{quarter}} {{year}}. Include:

1. Portfolio performance summary
2. Asset allocation changes
...
```

## Sources

Prompts can be loaded from:

- **Local directories**: `/prompts/`, `~/.bog-agents/prompts/`
- **Git repositories**: `git://github.com/org/prompts.git`
- **HTTP endpoints**: `https://example.com/prompts/index.json`

## Usage

```python
from bog_agents.middleware.saved_prompts import SavedPromptsMiddleware

middleware = SavedPromptsMiddleware(
    sources=[
        "/prompts/local/",
        "https://raw.githubusercontent.com/org/prompts/main/",
    ],
)
```
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Annotated, NotRequired

import yaml
from langchain.agents.middleware.types import PrivateStateAttr
from typing_extensions import TypedDict

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig
    from langgraph.runtime import Runtime

    from bog_agents.backends.protocol import BACKEND_TYPES, BackendProtocol

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)
from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, StructuredTool

from bog_agents.middleware._utils import append_to_system_message

logger = logging.getLogger(__name__)

MAX_PROMPT_FILE_SIZE = 1 * 1024 * 1024  # 1MB


class PromptMetadata(TypedDict):
    """Metadata for a saved prompt."""

    name: str
    """Prompt identifier."""

    description: str
    """What this prompt does."""

    category: str
    """Prompt category (reports, analysis, compliance, etc.)."""

    variables: list[str]
    """Template variables that must be filled in."""

    path: str
    """Path to the prompt file."""

    content: str
    """Full prompt content (after frontmatter)."""


class SavedPromptsState(AgentState):
    """State for saved prompts middleware."""

    prompts_metadata: NotRequired[Annotated[list[PromptMetadata], PrivateStateAttr]]


class SavedPromptsStateUpdate(TypedDict):
    """State update for saved prompts middleware."""

    prompts_metadata: list[PromptMetadata]


def _parse_prompt_file(content: str, file_path: str) -> PromptMetadata | None:
    """Parse a prompt file with YAML frontmatter.

    Args:
        content: File content.
        file_path: Path for error messages.

    Returns:
        Parsed prompt metadata, or None if parsing fails.
    """
    if len(content) > MAX_PROMPT_FILE_SIZE:
        logger.warning("Skipping %s: content too large", file_path)
        return None

    frontmatter_pattern = r"^---\s*\n(.*?)\n---\s*\n(.*)$"
    match = re.match(frontmatter_pattern, content, re.DOTALL)

    if not match:
        logger.warning("Skipping %s: no valid YAML frontmatter", file_path)
        return None

    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError as e:
        logger.warning("Invalid YAML in %s: %s", file_path, e)
        return None

    if not isinstance(data, dict):
        logger.warning("Skipping %s: frontmatter is not a mapping", file_path)
        return None

    name = str(data.get("name", "")).strip()
    description = str(data.get("description", "")).strip()
    if not name or not description:
        logger.warning("Skipping %s: missing required 'name' or 'description'", file_path)
        return None

    variables_raw = data.get("variables", [])
    variables = [str(v) for v in variables_raw] if isinstance(variables_raw, list) else []

    return PromptMetadata(
        name=name,
        description=description,
        category=str(data.get("category", "general")).strip(),
        variables=variables,
        path=file_path,
        content=match.group(2).strip(),
    )


def _list_prompts(backend: BackendProtocol, source_path: str) -> list[PromptMetadata]:
    """List prompts from a backend source.

    Args:
        backend: Backend instance.
        source_path: Path to the prompts directory.

    Returns:
        List of parsed prompt metadata.
    """
    prompts: list[PromptMetadata] = []

    try:
        items = backend.ls_info(source_path)
    except Exception:
        logger.warning("Could not list prompts from %s", source_path)
        return []

    md_files = [item["path"] for item in items if not item.get("is_dir") and item["path"].endswith(".md")]

    if not md_files:
        return []

    responses = backend.download_files(md_files)

    for file_path, response in zip(md_files, responses, strict=True):
        if response.error or response.content is None:
            continue
        try:
            content = response.content.decode("utf-8")
        except UnicodeDecodeError:
            continue

        prompt = _parse_prompt_file(content, file_path)
        if prompt:
            prompts.append(prompt)

    return prompts


async def _alist_prompts(backend: BackendProtocol, source_path: str) -> list[PromptMetadata]:
    """Async version of _list_prompts.

    Args:
        backend: Backend instance.
        source_path: Path to the prompts directory.

    Returns:
        List of parsed prompt metadata.
    """
    prompts: list[PromptMetadata] = []

    try:
        items = await backend.als_info(source_path)
    except Exception:
        logger.warning("Could not list prompts from %s", source_path)
        return []

    md_files = [item["path"] for item in items if not item.get("is_dir") and item["path"].endswith(".md")]

    if not md_files:
        return []

    responses = await backend.adownload_files(md_files)

    for file_path, response in zip(md_files, responses, strict=True):
        if response.error or response.content is None:
            continue
        try:
            content = response.content.decode("utf-8")
        except UnicodeDecodeError:
            continue

        prompt = _parse_prompt_file(content, file_path)
        if prompt:
            prompts.append(prompt)

    return prompts


def _render_template(template: str, variables: dict[str, str]) -> str:
    """Render a prompt template with variable substitution.

    Uses simple {{variable}} syntax.

    Args:
        template: The template string.
        variables: Variable name-value mapping.

    Returns:
        Rendered template.
    """
    result = template
    for key, value in variables.items():
        result = result.replace(f"{{{{{key}}}}}", value)
    return result


PROMPTS_SYSTEM_PROMPT = """## Saved Prompts

You have access to a library of saved prompts — reusable templates for common tasks.

{prompts_list}

**How to Use Saved Prompts:**
1. Use `list_prompts` to see all available prompts
2. Use `get_prompt` to read a specific prompt's full content
3. Use `use_prompt` to render a prompt with variables filled in
4. Follow the rendered prompt's instructions to complete the task

Saved prompts encode best practices and workflows. When a user's request matches a prompt, use it!"""


class SavedPromptsMiddleware(AgentMiddleware[SavedPromptsState, ContextT, ResponseT]):
    """Middleware for loading and using saved prompt libraries.

    Loads prompts from backend sources and provides tools for listing,
    viewing, and rendering prompt templates.

    Args:
        backend: Backend instance or factory for file operations.
        sources: List of prompt source paths.
    """

    state_schema = SavedPromptsState

    def __init__(self, *, backend: BACKEND_TYPES, sources: list[str]) -> None:
        self._backend = backend
        self.sources = sources
        self.tools: list[BaseTool] = self._build_tools()

    def _get_backend(self, state: SavedPromptsState, runtime: Runtime, config: RunnableConfig) -> BackendProtocol:
        """Resolve backend from instance or factory.

        Args:
            state: Current agent state.
            runtime: Runtime context.
            config: Runnable config.

        Returns:
            Resolved backend instance.
        """
        if callable(self._backend):
            tool_runtime = ToolRuntime(
                state=state,
                context=runtime.context,
                stream_writer=runtime.stream_writer,
                store=runtime.store,
                config=config,
                tool_call_id=None,
            )
            return self._backend(tool_runtime)  # ty: ignore[call-top-callable, invalid-argument-type]
        return self._backend

    def _build_tools(self) -> list[BaseTool]:
        """Build saved prompts tools."""
        mw = self

        def list_prompts(
            runtime: ToolRuntime[None, SavedPromptsState],
            category: Annotated[str, "Filter by category (optional)"] = "",
        ) -> str:
            """List all available saved prompts, optionally filtered by category."""
            prompts = runtime.state.get("prompts_metadata", [])
            if not prompts:
                return "No saved prompts available. Add prompt files to your prompt sources."

            if category:
                prompts = [p for p in prompts if p["category"] == category]

            if not prompts:
                return f"No prompts in category '{category}'."

            lines = ["## Available Prompts", ""]
            categories: dict[str, list[PromptMetadata]] = {}
            for p in prompts:
                categories.setdefault(p["category"], []).append(p)

            for cat, cat_prompts in sorted(categories.items()):
                lines.append(f"### {cat.capitalize()}")
                for p in cat_prompts:
                    vars_str = f" (variables: {', '.join(p['variables'])})" if p["variables"] else ""
                    lines.append(f"- **{p['name']}**: {p['description']}{vars_str}")
                lines.append("")

            return "\n".join(lines)

        def get_prompt(
            runtime: ToolRuntime[None, SavedPromptsState],
            name: Annotated[str, "Name of the prompt to retrieve"],
        ) -> str:
            """Get the full content of a saved prompt by name."""
            prompts = runtime.state.get("prompts_metadata", [])
            for p in prompts:
                if p["name"] == name:
                    lines = [
                        f"## Prompt: {p['name']}",
                        f"Description: {p['description']}",
                        f"Category: {p['category']}",
                    ]
                    if p["variables"]:
                        lines.append(f"Variables: {', '.join(p['variables'])}")
                    lines.extend(["", "---", "", p["content"]])
                    return "\n".join(lines)
            return f"Prompt '{name}' not found. Use `list_prompts` to see available prompts."

        def use_prompt(
            runtime: ToolRuntime[None, SavedPromptsState],
            name: Annotated[str, "Name of the prompt to use"],
            variables: Annotated[str, "Variable assignments as 'key=value' pairs, comma-separated"] = "",
        ) -> str:
            """Render a saved prompt with variable substitution and return the result."""
            prompts = runtime.state.get("prompts_metadata", [])
            prompt = None
            for p in prompts:
                if p["name"] == name:
                    prompt = p
                    break

            if prompt is None:
                return f"Prompt '{name}' not found. Use `list_prompts` to see available prompts."

            var_dict: dict[str, str] = {}
            if variables:
                for pair in variables.split(","):
                    pair = pair.strip()
                    if "=" in pair:
                        key, value = pair.split("=", 1)
                        var_dict[key.strip()] = value.strip()

            # Check for missing variables
            missing = [v for v in prompt["variables"] if v not in var_dict]
            if missing:
                return f"Missing required variables: {', '.join(missing)}. Provide as 'key=value' pairs."

            rendered = _render_template(prompt["content"], var_dict)
            return f"## Rendered Prompt: {prompt['name']}\n\n{rendered}"

        return [
            StructuredTool.from_function(
                name="list_prompts",
                description="List all available saved prompts, optionally filtered by category.",
                func=list_prompts,
            ),
            StructuredTool.from_function(
                name="get_prompt",
                description="Get the full content of a saved prompt by name.",
                func=get_prompt,
            ),
            StructuredTool.from_function(
                name="use_prompt",
                description="Render a saved prompt with variable substitution. Pass variables as 'key=value' pairs.",
                func=use_prompt,
            ),
        ]

    def _format_prompts_list(self, prompts: list[PromptMetadata]) -> str:
        """Format prompts for the system prompt."""
        if not prompts:
            return "(No saved prompts available)"

        lines = []
        for p in prompts:
            vars_str = f" (variables: {', '.join(p['variables'])})" if p["variables"] else ""
            lines.append(f"- **{p['name']}** [{p['category']}]: {p['description']}{vars_str}")
        return "\n".join(lines)

    def before_agent(self, state: SavedPromptsState, runtime: Runtime, config: RunnableConfig) -> SavedPromptsStateUpdate | None:  # ty: ignore[invalid-method-override]
        """Load prompts metadata before agent execution.

        Args:
            state: Current agent state.
            runtime: Runtime context.
            config: Runnable config.

        Returns:
            State update with prompts loaded.
        """
        if "prompts_metadata" in state:
            return None

        backend = self._get_backend(state, runtime, config)
        all_prompts: dict[str, PromptMetadata] = {}

        for source_path in self.sources:
            source_prompts = _list_prompts(backend, source_path)
            for prompt in source_prompts:
                all_prompts[prompt["name"]] = prompt

        return SavedPromptsStateUpdate(prompts_metadata=list(all_prompts.values()))

    async def abefore_agent(self, state: SavedPromptsState, runtime: Runtime, config: RunnableConfig) -> SavedPromptsStateUpdate | None:  # ty: ignore[invalid-method-override]
        """Async version of before_agent.

        Args:
            state: Current agent state.
            runtime: Runtime context.
            config: Runnable config.

        Returns:
            State update with prompts loaded.
        """
        if "prompts_metadata" in state:
            return None

        backend = self._get_backend(state, runtime, config)
        all_prompts: dict[str, PromptMetadata] = {}

        for source_path in self.sources:
            source_prompts = await _alist_prompts(backend, source_path)
            for prompt in source_prompts:
                all_prompts[prompt["name"]] = prompt

        return SavedPromptsStateUpdate(prompts_metadata=list(all_prompts.values()))

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Inject prompts listing into system message.

        Args:
            request: Model request to modify.

        Returns:
            Modified request.
        """
        prompts = request.state.get("prompts_metadata", [])
        prompts_list = self._format_prompts_list(prompts)
        section = PROMPTS_SYSTEM_PROMPT.format(prompts_list=prompts_list)
        new_system_message = append_to_system_message(request.system_message, section)
        return request.override(system_message=new_system_message)

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """Inject prompts listing into system prompt.

        Args:
            request: Model request.
            call_next: Handler function.

        Returns:
            Model response.
        """
        modified = self.modify_request(request)
        return call_next(modified)

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        """Async version of wrap_model_call.

        Args:
            request: Model request.
            call_next: Async handler function.

        Returns:
            Model response.
        """
        modified = self.modify_request(request)
        return await call_next(modified)


__all__ = ["PromptMetadata", "SavedPromptsMiddleware"]
