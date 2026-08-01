"""Public tool factories for callers that don't need middleware overhead.

A drawback of the middleware-as-tool-contributor pattern (many of the
``bog_agents/middleware/*.py`` modules inherit ``AgentMiddleware`` only
to expose a ``self.tools`` list) is that every tool drag-along brings
the full middleware machinery — state schema, lifecycle hooks, wrapper
indirection — for what is really a fixed list of ``BaseTool`` objects.

This package gives callers a leaner alternative: import a
``*_bundle()`` function, get back a ``list[BaseTool]``, hand it to
``create_agent(tools=...)``. No middleware class, no wrap stack
overhead, no implicit ordering interactions with the rest of the
middleware list.

The corresponding ``Middleware`` classes are kept as thin compatibility
shims that delegate to these bundles, so existing call sites continue
to work without change. See ``REVIEW.md`` W4 / Bucket B for the
broader rationale.
"""

from __future__ import annotations

from bog_agents.tools.bundles import (
    background_shell_tools_bundle,
    git_tools_bundle,
    memory_search_tool_bundle,
    multi_edit_tool,
    pty_tools_bundle,
    read_many_files_tool,
)
from bog_agents.tools.coercion import (
    SemanticBool,
    SemanticNumber,
    semantic_bool,
    semantic_number,
)

__all__ = [
    "SemanticBool",
    "SemanticNumber",
    "background_shell_tools_bundle",
    "git_tools_bundle",
    "memory_search_tool_bundle",
    "multi_edit_tool",
    "pty_tools_bundle",
    "read_many_files_tool",
    "semantic_bool",
    "semantic_number",
]
