"""Middleware for conversation branching and hierarchical memory.

Feature #14: Hierarchical memory — session, project, global tiers.
Feature #16: Conversation branching — branch at any point.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ResponseT,
)
from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, StructuredTool
from typing_extensions import TypedDict

logger = logging.getLogger(__name__)


@dataclass
class ConversationBranch:
    """A branch point in the conversation."""

    branch_id: str
    parent_id: str | None
    label: str
    created_at: float = field(default_factory=time.time)
    message_index: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class MemoryTier:
    """A tier of hierarchical memory."""

    name: str  # session, project, global
    entries: dict[str, str] = field(default_factory=dict)
    source_path: Path | None = None

    def add(self, key: str, value: str) -> None:
        """Add or update a memory entry."""
        self.entries[key] = value

    def get(self, key: str) -> str | None:
        """Retrieve a memory entry."""
        return self.entries.get(key)

    def search(self, query: str) -> list[tuple[str, str]]:
        """Search entries matching a query."""
        query_lower = query.lower()
        return [(k, v) for k, v in self.entries.items() if query_lower in k.lower() or query_lower in v.lower()]


class ConversationBranchState(TypedDict):
    """State for conversation branch middleware."""


class ConversationBranchMiddleware(AgentMiddleware[ConversationBranchState, ContextT, ResponseT]):
    """Middleware for conversation branching and hierarchical memory.

    Allows creating branch points in conversations and managing
    three tiers of memory: session, project, and global.

    Args:
        working_dir: Project root for project-level memory.
        global_memory_dir: Directory for global memory.
    """

    state_schema = ConversationBranchState

    def __init__(
        self,
        *,
        working_dir: Path | None = None,
        global_memory_dir: Path | None = None,
    ) -> None:
        self._working_dir = working_dir or Path.cwd()
        self._global_dir = global_memory_dir or Path.home() / ".bog-agents"
        self._branches: dict[str, ConversationBranch] = {}
        self._current_branch: str = "main"
        self._branch_counter = 0

        # Initialize memory tiers.
        # The project tier persists to a DEDICATED managed file, never to the
        # user's hand-authored AGENTS.md — _save_memory_tier rewrites the whole
        # file as a key/value dump, which would destroy AGENTS.md prose/markdown
        # on the first remember()/promote_memory() call. AGENTS.md stays
        # read-only context owned by MemoryMiddleware. (REVIEW.md v2 P0-1.)
        self._memory = {
            "session": MemoryTier(name="session"),
            "project": MemoryTier(
                name="project",
                source_path=self._working_dir / ".bog-agents" / "memory" / "project.md",
            ),
            "global": MemoryTier(name="global", source_path=self._global_dir / "memory.md"),
        }
        self._load_memory_files()
        self.tools = self._build_tools()

    @property
    def memory_tiers(self) -> dict[str, MemoryTier]:
        """Access memory tiers."""
        return self._memory

    def _load_memory_files(self) -> None:
        """Load memory from disk files."""
        for tier in self._memory.values():
            if tier.source_path and tier.source_path.exists():
                try:
                    content = tier.source_path.read_text(encoding="utf-8")
                    # Parse simple key: value format
                    for line in content.split("\n"):
                        if ":" in line and not line.startswith("#"):
                            key, _, value = line.partition(":")
                            tier.add(key.strip(), value.strip())
                except OSError:
                    pass

    def _save_memory_tier(self, tier_name: str) -> None:
        """Persist a memory tier to disk."""
        tier = self._memory.get(tier_name)
        if tier and tier.source_path:
            try:
                tier.source_path.parent.mkdir(parents=True, exist_ok=True)
                lines = [f"# {tier.name} memory\n"]
                for key, value in tier.entries.items():
                    lines.append(f"{key}: {value}")
                # Atomic write: a crash/Ctrl+C mid-write must not truncate the file.
                tmp = tier.source_path.with_suffix(tier.source_path.suffix + ".tmp")
                tmp.write_text("\n".join(lines), encoding="utf-8")
                tmp.replace(tier.source_path)
            except OSError as e:
                logger.warning("Failed to save %s memory: %s", tier_name, e)

    def _build_tools(self) -> list[BaseTool]:
        """Build conversation branch and memory tools."""
        middleware = self

        def create_branch(
            runtime: ToolRuntime[None, ConversationBranchState],
            label: Annotated[str, "Label for this branch point"],
        ) -> str:
            """Create a conversation branch point to explore alternatives."""
            middleware._branch_counter += 1
            branch_id = f"branch-{middleware._branch_counter}"
            branch = ConversationBranch(
                branch_id=branch_id,
                parent_id=middleware._current_branch,
                label=label,
            )
            middleware._branches[branch_id] = branch
            middleware._current_branch = branch_id
            return f"Created branch '{label}' (id={branch_id}). Now exploring this branch."

        def list_branches(
            runtime: ToolRuntime[None, ConversationBranchState],
        ) -> str:
            """List all conversation branches."""
            if not middleware._branches:
                return "No branches created. Currently on 'main'."
            lines = ["Conversation branches:"]
            for bid, b in middleware._branches.items():
                marker = " *" if bid == middleware._current_branch else ""
                parent = f" (from: {b.parent_id})" if b.parent_id else ""
                lines.append(f"  {b.label} ({bid}){marker}{parent}")
            return "\n".join(lines)

        def switch_branch(
            runtime: ToolRuntime[None, ConversationBranchState],
            branch_id: Annotated[str, "Branch ID to switch to, or 'main'"],
        ) -> str:
            """Switch to a different conversation branch."""
            if branch_id == "main" or branch_id in middleware._branches:
                middleware._current_branch = branch_id
                label = "main" if branch_id == "main" else middleware._branches[branch_id].label
                return f"Switched to branch '{label}'"
            return f"Branch '{branch_id}' not found."

        def remember(
            runtime: ToolRuntime[None, ConversationBranchState],
            key: Annotated[str, "Short descriptive key for this memory"],
            value: Annotated[str, "Content to remember"],
            tier: Annotated[str, "Memory tier: 'session', 'project', or 'global'"] = "project",
        ) -> str:
            """Store a memory in the specified tier (session/project/global)."""
            if tier not in middleware._memory:
                return f"Invalid tier '{tier}'. Use 'session', 'project', or 'global'."
            middleware._memory[tier].add(key, value)
            if tier in ("project", "global"):
                middleware._save_memory_tier(tier)
            return f"Remembered [{tier}] {key}: {value[:60]}..."

        def recall(
            runtime: ToolRuntime[None, ConversationBranchState],
            query: Annotated[str, "Search query for memories"],
            tier: str | None = None,
        ) -> str:
            """Search memories across tiers or within a specific tier."""
            results: list[str] = []
            tiers_to_search = [tier] if tier and tier in middleware._memory else list(middleware._memory.keys())

            for t in tiers_to_search:
                matches = middleware._memory[t].search(query)
                for key, value in matches:
                    results.append(f"  [{t}] {key}: {value}")

            if not results:
                return f"No memories matching '{query}'."
            return f"Found {len(results)} memories:\n" + "\n".join(results)

        def promote_memory(
            runtime: ToolRuntime[None, ConversationBranchState],
            key: Annotated[str, "Key of the memory to promote"],
            from_tier: Annotated[str, "Source tier"] = "session",
            to_tier: Annotated[str, "Target tier"] = "project",
        ) -> str:
            """Promote a memory from one tier to a higher tier."""
            source = middleware._memory.get(from_tier)
            if not source:
                return f"Invalid source tier '{from_tier}'."
            value = source.get(key)
            if value is None:
                return f"Key '{key}' not found in {from_tier} memory."
            middleware._memory[to_tier].add(key, value)
            if to_tier in ("project", "global"):
                middleware._save_memory_tier(to_tier)
            return f"Promoted '{key}' from {from_tier} to {to_tier} memory."

        return [
            StructuredTool.from_function(name="create_branch", description="Create a conversation branch.", func=create_branch),
            StructuredTool.from_function(name="list_branches", description="List conversation branches.", func=list_branches),
            StructuredTool.from_function(name="switch_branch", description="Switch to a branch.", func=switch_branch),
            StructuredTool.from_function(name="remember", description="Store memory (session/project/global).", func=remember),
            StructuredTool.from_function(name="recall", description="Search memories.", func=recall),
            StructuredTool.from_function(name="promote_memory", description="Promote memory between tiers.", func=promote_memory),
        ]
