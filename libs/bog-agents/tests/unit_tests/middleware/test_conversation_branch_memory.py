"""Regression test for REVIEW.md v2 P0-1 — AGENTS.md must never be overwritten.

The `project` memory tier used to point at the user's hand-authored AGENTS.md,
and `_save_memory_tier` rewrites the whole file as a key/value dump. The first
`remember(tier="project")` therefore destroyed AGENTS.md. The fix routes the
project tier to a dedicated managed file under `.bog-agents/memory/`.
"""

from __future__ import annotations

from pathlib import Path

from bog_agents.middleware.conversation_branch import ConversationBranchMiddleware

_AGENTS_MD = """# Project guide

This repo uses `uv`. Run tests: make test

## Conventions
- Type hints everywhere
- See https://agents.md/ for the standard

```python
def example() -> None: ...
```
"""


def test_remember_does_not_touch_agents_md(tmp_path: Path) -> None:
    agents = tmp_path / "AGENTS.md"
    agents.write_text(_AGENTS_MD, encoding="utf-8")

    mw = ConversationBranchMiddleware(working_dir=tmp_path, global_memory_dir=tmp_path / "global")

    # The project tier must NOT be backed by AGENTS.md.
    project_path = mw.memory_tiers["project"].source_path
    assert project_path is not None
    assert project_path.name != "AGENTS.md"
    assert "AGENTS.md" not in str(project_path)

    # Simulate what remember(tier="project") does: add + save.
    mw.memory_tiers["project"].add("build", "make test")
    mw._save_memory_tier("project")  # type: ignore[attr-defined]

    # AGENTS.md is byte-for-byte untouched.
    assert agents.read_text(encoding="utf-8") == _AGENTS_MD
    # The remembered entry landed in the dedicated managed file.
    assert project_path.exists()
    assert "build: make test" in project_path.read_text(encoding="utf-8")


def test_agents_md_prose_not_loaded_as_memory(tmp_path: Path) -> None:
    # A pre-existing AGENTS.md must not be parsed into the project tier
    # (it is no longer the project tier's source).
    (tmp_path / "AGENTS.md").write_text(_AGENTS_MD, encoding="utf-8")
    mw = ConversationBranchMiddleware(working_dir=tmp_path, global_memory_dir=tmp_path / "global")
    # The colon-bearing prose lines from AGENTS.md must not appear as entries.
    assert mw.memory_tiers["project"].entries == {}
