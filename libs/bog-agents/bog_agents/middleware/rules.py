"""Project Rules System middleware — auto-inject contextual rules from .bog-agents/rules/.

Implements a Cursor-style ``.mdc`` rules system but richer. Rules are Markdown
files with YAML frontmatter stored in ``.bog-agents/rules/``. They are committed
to the repository so the whole team shares them automatically.

Rule frontmatter fields::

    ---
    glob: ["src/**/*.py"]   # inject when matching files are in context
    always: true             # always inject this rule (overrides glob)
    agent: code-review       # only for specific agent type (optional)
    priority: 10             # higher = injected first (default 0)
    ---

    [Rule body in Markdown follows here]

Usage::

    from bog_agents.middleware.rules import RulesMiddleware

    agent = create_agent(
        model="claude-opus-4-7",
        middleware=[RulesMiddleware()],
    )

Standalone (e.g. from a CLI command)::

    from bog_agents.middleware.rules import load_rules, apply_rules

    rules = load_rules(Path("/my/project"))
    injected = apply_rules(rules, context_files=["src/auth.py"])
"""

from __future__ import annotations

import fnmatch
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)
from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, StructuredTool
from typing_extensions import TypedDict

from bog_agents.middleware._utils import append_to_system_message

logger = logging.getLogger(__name__)

_RULES_DIR = ".bog-agents/rules"
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_YAML_KEY_RE = re.compile(r"^(\w+)\s*:\s*(.+)$", re.MULTILINE)


# ---------------------------------------------------------------------------
# Frontmatter parsing (no external dependency)
# ---------------------------------------------------------------------------


def _parse_yaml_value(raw: str) -> Any:
    """Parse a simple YAML scalar or list value.

    Args:
        raw: Raw YAML value string.

    Returns:
        Parsed Python value (bool, int, str, or list of str).
    """
    raw = raw.strip()
    # Boolean
    if raw.lower() in ("true", "yes", "on"):
        return True
    if raw.lower() in ("false", "no", "off"):
        return False
    # Integer
    try:
        return int(raw)
    except ValueError:
        pass
    # Inline list: ["a", "b"] or ['a', 'b'] or [a, b]
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1]
        items = [item.strip().strip("\"'") for item in re.split(r",\s*", inner) if item.strip()]
        return items
    # Plain string (strip optional quotes)
    return raw.strip("\"'")


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Extract YAML frontmatter and body from a rule file.

    Args:
        text: Raw file content.

    Returns:
        Tuple of (frontmatter_dict, body_text).
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text

    fm_text = match.group(1)
    body = text[match.end() :]

    fm: dict[str, Any] = {}
    for key_match in _YAML_KEY_RE.finditer(fm_text):
        key = key_match.group(1)
        val = key_match.group(2).strip()
        fm[key] = _parse_yaml_value(val)

    return fm, body


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class RuleSpec:
    """A single project rule loaded from .bog-agents/rules/<name>.md.

    Attributes:
        name: Rule name (filename stem).
        content: Rule body (the text to inject).
        glob: Glob patterns — rule is injected when a context file matches.
        always: Always inject regardless of context files.
        agent: If set, only inject for this agent type.
        priority: Higher priority rules appear first. Default 0.
        path: Source file path.
        mtime: Source file modification time (for cache invalidation).
    """

    name: str
    content: str
    glob: list[str] = field(default_factory=list)
    always: bool = False
    agent: str = ""
    priority: int = 0
    path: Path = field(default_factory=Path)
    mtime: float = 0.0

    def matches(
        self,
        context_files: list[str],
        *,
        agent_type: str = "",
    ) -> bool:
        """Return True if this rule should be injected given the context.

        Args:
            context_files: Files currently in the agent's context.
            agent_type: The agent type running (for agent-specific rules).

        Returns:
            True if the rule should be injected.
        """
        # Agent-specific rules only apply when agent_type matches
        if self.agent and agent_type and self.agent.lower() != agent_type.lower():
            return False

        if self.always:
            return True

        if not self.glob:
            # No glob = always inject (treat as implicit always)
            return True

        return any(fnmatch.fnmatch(cf, pattern) for cf in context_files for pattern in self.glob)


# ---------------------------------------------------------------------------
# Rule loading
# ---------------------------------------------------------------------------


def load_rules(project_root: Path) -> list[RuleSpec]:
    """Load all rule files from .bog-agents/rules/.

    Args:
        project_root: Project root directory.

    Returns:
        List of RuleSpec objects sorted by priority (highest first).
    """
    rules_dir = project_root / _RULES_DIR
    if not rules_dir.is_dir():
        return []

    rules: list[RuleSpec] = []
    for path in sorted(rules_dir.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            fm, body = _parse_frontmatter(text)
            glob_val = fm.get("glob", [])
            if isinstance(glob_val, str):
                glob_val = [glob_val]

            agent_val = fm.get("agent", "")
            if isinstance(agent_val, list):
                agent_val = agent_val[0] if agent_val else ""

            rules.append(
                RuleSpec(
                    name=path.stem,
                    content=body.strip(),
                    glob=glob_val if isinstance(glob_val, list) else [],
                    always=bool(fm.get("always", False)),
                    agent=str(agent_val),
                    priority=int(fm.get("priority", 0)),
                    path=path,
                    mtime=path.stat().st_mtime,
                )
            )
        except (OSError, ValueError) as exc:
            logger.warning("Failed to load rule %s: %s", path, exc)

    rules.sort(key=lambda r: (-r.priority, r.name))
    return rules


def apply_rules(
    rules: list[RuleSpec],
    *,
    context_files: list[str] | None = None,
    agent_type: str = "",
    max_rules: int = 20,
) -> str:
    """Return the combined text of all applicable rules.

    Args:
        rules: All loaded rules.
        context_files: Files in the agent's current context.
        agent_type: Agent type (for agent-specific rules).
        max_rules: Maximum rules to inject (prevents prompt bloat).

    Returns:
        Combined rule text, or empty string if no rules match.
    """
    cf = context_files or []
    matching = [r for r in rules if r.matches(cf, agent_type=agent_type)][:max_rules]
    if not matching:
        return ""

    parts = ["## Project Rules\n"]
    for rule in matching:
        parts.append(f"### Rule: {rule.name}\n\n{rule.content}")

    return "\n\n".join(parts)


def format_rule_for_display(rule: RuleSpec) -> str:
    """Format a rule for human-readable display.

    Args:
        rule: Rule to format.

    Returns:
        Formatted string.
    """
    tags: list[str] = []
    if rule.always:
        tags.append("always")
    if rule.glob:
        tags.append(f"glob={rule.glob}")
    if rule.agent:
        tags.append(f"agent={rule.agent}")
    if rule.priority:
        tags.append(f"priority={rule.priority}")

    tag_str = " [" + ", ".join(tags) + "]" if tags else ""
    preview = rule.content[:100].replace("\n", " ") + ("..." if len(rule.content) > 100 else "")
    return f"  • {rule.name}{tag_str}\n    {preview}"


# ---------------------------------------------------------------------------
# Rule creation helpers
# ---------------------------------------------------------------------------


_RULE_TEMPLATE = """\
---
glob: []
always: false
priority: 0
---

{body}
"""


def create_rule_file(
    project_root: Path,
    name: str,
    body: str,
    *,
    glob: list[str] | None = None,
    always: bool = False,
    agent: str = "",
    priority: int = 0,
) -> Path:
    """Write a new rule file to .bog-agents/rules/.

    Args:
        project_root: Project root directory.
        name: Rule name (used as filename stem).
        body: Rule content (Markdown).
        glob: Glob patterns to match.
        always: Always inject this rule.
        agent: Agent-specific rule target.
        priority: Injection priority.

    Returns:
        Path to the created rule file.
    """
    rules_dir = project_root / _RULES_DIR
    rules_dir.mkdir(parents=True, exist_ok=True)

    fm_lines = ["---"]
    if glob:
        glob_str = json.dumps(glob)
        fm_lines.append(f"glob: {glob_str}")
    if always:
        fm_lines.append("always: true")
    if agent:
        fm_lines.append(f"agent: {agent}")
    if priority:
        fm_lines.append(f"priority: {priority}")
    fm_lines.append("---\n")

    content = "\n".join(fm_lines) + "\n" + body.strip()
    path = rules_dir / f"{name}.md"
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# LangGraph state
# ---------------------------------------------------------------------------


class RulesState(TypedDict):
    """State for the rules middleware (empty — rules are stateless)."""


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class RulesMiddleware(AgentMiddleware[RulesState, ContextT, ResponseT]):
    """Auto-inject matching project rules into the agent's system prompt.

    Rules are Markdown files in ``.bog-agents/rules/`` with YAML frontmatter.
    Committed to the repo, they're automatically shared with the whole team.

    Args:
        working_dir: Project root directory.
        agent_type: Agent type for agent-specific rule filtering.
        reload_interval: Seconds between rule reloads. 0 = no reload.
    """

    state_schema = RulesState

    def __init__(
        self,
        *,
        working_dir: Path | None = None,
        agent_type: str = "",
        reload_interval: float = 30.0,
    ) -> None:
        self._working_dir = working_dir or Path.cwd()
        self._agent_type = agent_type
        self._reload_interval = reload_interval
        self._rules: list[RuleSpec] = []
        self._last_loaded: float = 0.0
        self._build_tools()

    def _ensure_loaded(self) -> None:
        """Load or reload rules if stale."""
        now = time.monotonic()
        if self._reload_interval <= 0 or now - self._last_loaded > self._reload_interval:
            self._rules = load_rules(self._working_dir)
            self._last_loaded = now

    @property
    def tools(self) -> list[BaseTool]:
        """Expose rule management tools to the agent."""
        return self._tools

    def _build_tools(self) -> None:
        """Build the rule management tools."""
        mw = self

        def list_project_rules(
            runtime: ToolRuntime[None, RulesState],
        ) -> str:
            """List all project rules in .bog-agents/rules/."""
            mw._ensure_loaded()
            if not mw._rules:
                return "No rules found. Create one with /rules add <name> [--always]"
            lines = ["Project rules:"]
            for rule in mw._rules:
                lines.append(format_rule_for_display(rule))
            return "\n".join(lines)

        def show_rule(
            runtime: ToolRuntime[None, RulesState],
            name: str,
        ) -> str:
            """Show the full content of a named rule.

            Args:
                name: Rule name (filename stem without .md).
            """
            mw._ensure_loaded()
            matched = [r for r in mw._rules if r.name == name]
            if not matched:
                return f"Rule '{name}' not found."
            rule = matched[0]
            return f"# Rule: {rule.name}\n\n{rule.content}"

        def test_rule(
            runtime: ToolRuntime[None, RulesState],
            name: str,
            file_path: str = "",
        ) -> str:
            """Test whether a rule would match a given file path.

            Args:
                name: Rule name to test.
                file_path: File path to test against the rule's glob patterns.
            """
            mw._ensure_loaded()
            matched = [r for r in mw._rules if r.name == name]
            if not matched:
                return f"Rule '{name}' not found."
            rule = matched[0]
            context = [file_path] if file_path else []
            would_match = rule.matches(context, agent_type=mw._agent_type)
            return f"Rule '{name}' would {'✓ match' if would_match else '✗ not match'} for file '{file_path or '(no file)'}'."

        self._tools = [
            StructuredTool.from_function(
                name="list_rules",
                description="List all project rules in .bog-agents/rules/.",
                func=list_project_rules,
            ),
            StructuredTool.from_function(
                name="show_rule",
                description="Show the full content of a named project rule.",
                func=show_rule,
            ),
            StructuredTool.from_function(
                name="test_rule",
                description="Test if a rule matches a given file path.",
                func=test_rule,
            ),
        ]

    def wrap_model_call(
        self,
        system_message: ModelRequest,
        call_next: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Inject matching rules into the system prompt (sync).

        Args:
            system_message: Current model request.
            call_next: Next middleware or model call.

        Returns:
            Model response.
        """
        self._ensure_loaded()
        rules_text = apply_rules(self._rules, agent_type=self._agent_type)
        if rules_text:
            system_message = append_to_system_message(system_message, rules_text)
        return call_next(system_message)

    async def awrap_model_call(
        self,
        system_message: ModelRequest,
        call_next: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Inject matching rules into the system prompt (async).

        Args:
            system_message: Current model request.
            call_next: Next middleware or model call.

        Returns:
            Model response.
        """
        self._ensure_loaded()
        rules_text = apply_rules(self._rules, agent_type=self._agent_type)
        if rules_text:
            system_message = append_to_system_message(system_message, rules_text)
        return await call_next(system_message)
