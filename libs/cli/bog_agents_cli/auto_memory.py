"""Agent-written auto-memories (ROADMAP killer #13).

Users re-explain project conventions and gotchas every session. This gives the
agent a `remember` tool to proactively record durable facts it learns mid-task
— conventions, decisions, gotchas, fix patterns — so they're auto-recalled in
future sessions. The agent decides what's worth keeping (the Windsurf Cascade /
Devin Knowledge pattern), and entries are provenance-tagged so users can audit
and prune them.

Storage rides the existing memory cascade (no new wiring): `project` memories
append to the repo's `AGENTS.md` (loaded by `project_memory.load_project_memory`
along with CLAUDE.md/.bog-agents.md), and `global` memories append to
`~/.bog-agents/memory.md`. Both are auto-injected into the system prompt on the
next session.
"""

from __future__ import annotations

import logging
from pathlib import Path

from langchain_core.tools import BaseTool, tool

logger = logging.getLogger(__name__)

_SECTION = "## Agent-Recorded Memories"
_PROVENANCE = (
    "<!-- bog-agents auto-memories: written by the agent via the `remember` "
    "tool. Safe to edit, reorganize, or delete. -->"
)
_GLOBAL_MEMORY = Path.home() / ".bog-agents" / "memory.md"
VALID_SCOPES = ("project", "global")


def _format_entry(fact: str, category: str) -> str:
    """Render one memory line, e.g. ``- (gotcha) The CLI re-wraps stdout...``."""
    fact = " ".join(fact.split())  # collapse whitespace/newlines
    category = (category or "note").strip().lower()
    return f"- ({category}) {fact}"


def append_memory(path: Path, fact: str, category: str) -> bool:
    """Append a memory entry to ``path`` under the managed section.

    Creates the file/section if missing. No-ops (returns False) when the exact
    entry already exists, so repeated learning doesn't duplicate.

    Args:
        path: Target memory file (AGENTS.md or ~/.bog-agents/memory.md).
        fact: The durable fact to record.
        category: A short tag (convention/decision/gotcha/fix-pattern/note).

    Returns:
        True if a new entry was written, False if it was already present.
    """
    entry = _format_entry(fact, category)
    existing = ""
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if entry in existing:
            return False

    path.parent.mkdir(parents=True, exist_ok=True)
    if _SECTION in existing:
        # Append the entry at the end of the managed section: just before the
        # next top-level heading after it, or at EOF if it's the last section.
        idx = existing.index(_SECTION) + len(_SECTION)
        nxt = existing.find("\n## ", idx)
        if nxt == -1:
            new_text = existing.rstrip() + "\n" + entry + "\n"
        else:
            new_text = existing[:nxt].rstrip() + "\n" + entry + "\n" + existing[nxt:]
        path.write_text(new_text, encoding="utf-8")
        return True

    block = f"\n{_SECTION}\n{_PROVENANCE}\n\n{entry}\n"
    new_text = (
        (existing.rstrip() + "\n" + block) if existing.strip() else block.lstrip("\n")
    )
    path.write_text(new_text, encoding="utf-8")
    return True


def auto_memory_tools(working_dir: str | Path | None = None) -> list[BaseTool]:
    """Return the `remember` tool bound to ``working_dir`` (ROADMAP #13).

    Args:
        working_dir: Project root for ``scope="project"`` memories (the repo's
            AGENTS.md). Defaults to the process CWD.

    Returns:
        A one-tool list suitable for ``create_agent(tools=[*auto_memory_tools()])``.
    """
    wd = Path(working_dir) if working_dir else Path.cwd()

    @tool
    def remember(fact: str, category: str = "note", scope: str = "project") -> str:
        """Record a durable fact so it is auto-recalled in future sessions.

        Use this PROACTIVELY whenever you learn something worth keeping: a
        project convention, an architectural decision, a non-obvious gotcha, or
        a fix pattern that took effort to discover. Keep each fact to one clear
        sentence.

        Args:
            fact: The durable fact, as one concise sentence.
            category: One of convention, decision, gotcha, fix-pattern, note.
            scope: "project" (this repo's AGENTS.md — the default) or "global"
                (~/.bog-agents/memory.md, applies to every project).

        Returns:
            A confirmation string.
        """
        fact = (fact or "").strip()
        if not fact:
            return "Nothing recorded: the fact was empty."
        scope = scope.strip().lower()
        if scope not in VALID_SCOPES:
            scope = "project"
        target = _GLOBAL_MEMORY if scope == "global" else (wd / "AGENTS.md")
        try:
            wrote = append_memory(target, fact, category)
        except OSError as exc:
            logger.warning("remember: could not write %s: %s", target, exc)
            return f"Could not record memory: {exc}"
        where = "globally" if scope == "global" else f"for this project ({target.name})"
        if not wrote:
            return f"Already remembered {where}; no duplicate added."
        return f"Recorded {where}. It will be auto-recalled in future sessions."

    return [remember]
