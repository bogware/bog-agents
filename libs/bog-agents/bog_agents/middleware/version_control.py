"""Version-controlled research middleware.

Feature #33: Git-like versioning of research outputs with diffs, blame,
and side-by-side comparison as new data arrives.

## Tools

- `save_version`: Save the current research state as a named version
- `list_versions`: List all saved versions
- `compare_versions`: Compare two versions side-by-side
- `restore_version`: Restore a previous version
- `version_history`: Show the change log

## Usage

```python
from bog_agents.middleware.version_control import VersionControlMiddleware

middleware = VersionControlMiddleware()
```
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated

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

logger = logging.getLogger(__name__)


@dataclass
class ResearchVersion:
    """A snapshot of research content at a point in time."""

    version_id: int
    label: str
    content: str
    summary: str = ""
    timestamp: str = ""
    parent_id: int | None = None


@dataclass
class VersionStore:
    """Store for research versions."""

    versions: list[ResearchVersion] = field(default_factory=list)
    current_content: str = ""
    _next_id: int = field(default=1, repr=False)

    def save(self, *, label: str, content: str, summary: str = "") -> ResearchVersion:
        """Save a new version.

        Args:
            label: Version label.
            content: Research content to snapshot.
            summary: Description of changes.

        Returns:
            The saved version.
        """
        parent = self.versions[-1].version_id if self.versions else None
        version = ResearchVersion(
            version_id=self._next_id,
            label=label,
            content=content,
            summary=summary,
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime()),
            parent_id=parent,
        )
        self.versions.append(version)
        self.current_content = content
        self._next_id += 1
        return version

    def get(self, version_id: int) -> ResearchVersion | None:
        """Get a version by ID."""
        for v in self.versions:
            if v.version_id == version_id:
                return v
        return None

    def compare(self, id_a: int, id_b: int) -> str:
        """Compare two versions.

        Args:
            id_a: First version ID.
            id_b: Second version ID.

        Returns:
            Formatted comparison.
        """
        a = self.get(id_a)
        b = self.get(id_b)
        if not a or not b:
            return "Error: Version not found."

        lines_a = a.content.splitlines()
        lines_b = b.content.splitlines()

        lines = [
            f"## Comparison: v{id_a} ({a.label}) vs v{id_b} ({b.label})",
            "",
        ]

        # Simple line-by-line diff
        max_lines = max(len(lines_a), len(lines_b))
        added = 0
        removed = 0
        unchanged = 0
        for i in range(max_lines):
            la = lines_a[i] if i < len(lines_a) else ""
            lb = lines_b[i] if i < len(lines_b) else ""
            if la == lb:
                unchanged += 1
            elif la and not lb:
                removed += 1
            elif lb and not la:
                added += 1
            else:
                removed += 1
                added += 1

        lines.extend(
            [
                f"Lines added: {added}",
                f"Lines removed: {removed}",
                f"Lines unchanged: {unchanged}",
                "",
                f"### v{id_a}: {a.label} ({a.timestamp})",
                a.content[:500] + ("..." if len(a.content) > 500 else ""),  # noqa: PLR2004
                "",
                f"### v{id_b}: {b.label} ({b.timestamp})",
                b.content[:500] + ("..." if len(b.content) > 500 else ""),  # noqa: PLR2004
            ]
        )

        return "\n".join(lines)

    def format_history(self) -> str:
        """Format version history."""
        if not self.versions:
            return "No versions saved yet."

        lines = ["## Version History", ""]
        for v in self.versions:
            parent = f" (from v{v.parent_id})" if v.parent_id else " (initial)"
            lines.append(f"**v{v.version_id}**: {v.label}{parent} — {v.timestamp}")
            if v.summary:
                lines.append(f"  {v.summary}")
        return "\n".join(lines)


class VersionControlState(TypedDict):
    """State for version control middleware."""


class VersionControlMiddleware(AgentMiddleware[VersionControlState, ContextT, ResponseT]):
    """Middleware for version-controlled research outputs.

    Every research output can be versioned, compared, and restored.
    """

    state_schema = VersionControlState

    def __init__(self) -> None:
        self.store = VersionStore()
        self.tools: list[BaseTool] = self._build_tools()

    def _build_tools(self) -> list[BaseTool]:
        """Build version control tools."""
        mw = self

        def save_version(
            runtime: ToolRuntime[None, VersionControlState],
            label: Annotated[str, "Version label (e.g., 'draft-1', 'after-earnings')"],
            content: Annotated[str, "Research content to save"],
            summary: Annotated[str, "Description of what changed"] = "",
        ) -> str:
            """Save the current research state as a named version."""
            v = mw.store.save(label=label, content=content, summary=summary)
            return f"Version v{v.version_id} saved: {label}"

        def list_versions(
            runtime: ToolRuntime[None, VersionControlState],
        ) -> str:
            """List all saved research versions."""
            return mw.store.format_history()

        def compare_versions(
            runtime: ToolRuntime[None, VersionControlState],
            version_a: Annotated[int, "First version ID"],
            version_b: Annotated[int, "Second version ID"],
        ) -> str:
            """Compare two research versions side-by-side."""
            return mw.store.compare(version_a, version_b)

        def restore_version(
            runtime: ToolRuntime[None, VersionControlState],
            version_id: Annotated[int, "Version ID to restore"],
        ) -> str:
            """Restore a previous version as the current content."""
            v = mw.store.get(version_id)
            if not v:
                return f"Version {version_id} not found."
            mw.store.current_content = v.content
            return f"Restored v{version_id}: {v.label}"

        def get_current(
            runtime: ToolRuntime[None, VersionControlState],
        ) -> str:
            """Get the current research content."""
            return mw.store.current_content or "(No content saved yet)"

        return [
            StructuredTool.from_function(name="save_version", description="Save research content as a named version.", func=save_version),
            StructuredTool.from_function(name="list_versions", description="List all saved research versions.", func=list_versions),
            StructuredTool.from_function(name="compare_versions", description="Compare two research versions.", func=compare_versions),
            StructuredTool.from_function(name="restore_version", description="Restore a previous version.", func=restore_version),
            StructuredTool.from_function(name="get_current_research", description="Get the current research content.", func=get_current),
        ]

    def wrap_model_call(
        self, request: ModelRequest[ContextT], call_next: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]]
    ) -> ModelResponse[ResponseT]:
        """Pass through."""
        return call_next(request)

    async def awrap_model_call(
        self, request: ModelRequest[ContextT], call_next: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]]
    ) -> ModelResponse[ResponseT]:
        """Async pass through."""
        return await call_next(request)


__all__ = ["ResearchVersion", "VersionControlMiddleware", "VersionStore"]
