"""Tests for ``bog_agents_cli.project_memory`` — T-4 cascade in REVIEW.md.

Verifies:

* Backward-compat: a flat ``.bog-agents.md`` at the repo root still loads.
* Forward-compat: ``AGENTS.md`` and ``CLAUDE.md`` are picked up at every
  directory level between the git root and the cwd.
* Ordering: outermost-to-innermost so the most-specific context appears
  closest to the LLM's "recent" attention.
* Boundaries: nothing outside the project root is ever loaded (no leaks
  from ``~/AGENTS.md`` or ``/etc/CLAUDE.md``).
* Hardening: symlinked memory files are skipped (no
  ``.bog-agents.md -> ~/.ssh/id_rsa`` cute exfil tricks).
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

import pytest

from bog_agents_cli.project_memory import (
    collect_memory_sources,
    load_project_memory,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A directory rooted as a git repo for the cascade-stop logic."""
    (tmp_path / ".git").mkdir()
    return tmp_path


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------


class TestBackwardCompat:
    def test_existing_dot_bog_agents_md_still_loads(self, repo: Path) -> None:
        _write(repo / ".bog-agents.md", "# project rules\nUse pytest.\n")
        out = load_project_memory(cwd=repo)
        assert "## Memory" in out
        assert "Use pytest." in out
        assert ".bog-agents.md" in out

    def test_no_memory_returns_empty_string(self, repo: Path) -> None:
        assert load_project_memory(cwd=repo) == ""


# ---------------------------------------------------------------------------
# AGENTS.md / CLAUDE.md cascade
# ---------------------------------------------------------------------------


class TestCascade:
    def test_root_agents_md_loads(self, repo: Path) -> None:
        _write(repo / "AGENTS.md", "agents-style rule\n")
        out = load_project_memory(cwd=repo)
        assert "agents-style rule" in out
        assert "AGENTS.md (repo root)" in out

    def test_root_claude_md_loads(self, repo: Path) -> None:
        _write(repo / "CLAUDE.md", "claude-style rule\n")
        out = load_project_memory(cwd=repo)
        assert "claude-style rule" in out
        assert "CLAUDE.md (repo root)" in out

    def test_cascade_root_then_subdir_preserves_order(self, repo: Path) -> None:
        _write(repo / "AGENTS.md", "ROOT_CONTENT\n")
        sub = repo / "libs" / "cli"
        _write(sub / "AGENTS.md", "SUB_CONTENT\n")
        out = load_project_memory(cwd=sub)
        assert "ROOT_CONTENT" in out
        assert "SUB_CONTENT" in out
        # Outermost first (root before subdir) so LLM ends with most-specific.
        assert out.index("ROOT_CONTENT") < out.index("SUB_CONTENT")

    def test_all_three_filenames_per_dir(self, repo: Path) -> None:
        _write(repo / "AGENTS.md", "AGENTS root\n")
        _write(repo / "CLAUDE.md", "CLAUDE root\n")
        _write(repo / ".bog-agents.md", "bog root\n")
        out = load_project_memory(cwd=repo)
        # Order within one directory: AGENTS → CLAUDE → .bog-agents
        assert out.index("AGENTS root") < out.index("CLAUDE root") < out.index("bog root")

    def test_subdir_only_doesnt_pull_root(self, repo: Path) -> None:
        sub = repo / "src"
        _write(sub / "AGENTS.md", "only-sub\n")
        out = load_project_memory(cwd=sub)
        assert "only-sub" in out
        assert out.count("AGENTS.md") == 1  # only the one file


# ---------------------------------------------------------------------------
# Project-root boundary
# ---------------------------------------------------------------------------


class TestBoundary:
    def test_cascade_stops_at_git_root(self, tmp_path: Path) -> None:
        """A file above the git root must never be loaded."""
        outer = tmp_path / "outer"
        repo_dir = outer / "myrepo"
        sub = repo_dir / "lib"
        for d in (outer, repo_dir, sub):
            d.mkdir(parents=True, exist_ok=True)
        (repo_dir / ".git").mkdir()
        # Place a hostile file ABOVE the repo root.
        _write(outer / "AGENTS.md", "OUT_OF_BOUNDS\n")
        _write(repo_dir / "AGENTS.md", "IN_REPO\n")
        out = load_project_memory(cwd=sub)
        assert "IN_REPO" in out
        assert "OUT_OF_BOUNDS" not in out

    def test_no_git_falls_back_to_cwd_only(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When no ``.git`` exists from cwd up, the cascade is cwd-only.

        Forced via monkeypatch — pytest's ``tmp_path`` lives inside a
        real git repo in this checkout, so the un-monkeypatched
        ``_find_project_root`` would walk all the way up to the repo's
        ``.git`` and treat every intermediate directory as in-scope.
        """
        sub = tmp_path / "sub"
        sub.mkdir()
        _write(sub / "AGENTS.md", "scoped\n")
        _write(tmp_path / "AGENTS.md", "parent\n")

        # Simulate the no-git case: _find_project_root returns cwd unchanged.
        from bog_agents_cli import project_memory

        monkeypatch.setattr(
            project_memory, "_find_project_root", lambda start: start.resolve()
        )
        out = load_project_memory(cwd=sub)
        assert "scoped" in out
        assert "parent" not in out  # walk never goes above cwd without git


# ---------------------------------------------------------------------------
# Symlink rejection (skill-loader-style hardening)
# ---------------------------------------------------------------------------


class TestSymlinkRejection:
    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Windows symlink creation needs admin / dev-mode",
    )
    def test_symlinked_memory_file_skipped(self, repo: Path, tmp_path: Path) -> None:
        secret = tmp_path / "outside" / "secret.txt"
        _write(secret, "DO NOT LEAK\n")
        link = repo / "AGENTS.md"
        link.symlink_to(secret)
        out = load_project_memory(cwd=repo)
        assert "DO NOT LEAK" not in out


# ---------------------------------------------------------------------------
# collect_memory_sources (the introspection API)
# ---------------------------------------------------------------------------


class TestCollectSources:
    def test_returns_paths_for_loaded_files_only(self, repo: Path) -> None:
        _write(repo / "AGENTS.md", "a\n")
        sub = repo / "x"
        _write(sub / ".bog-agents.md", "b\n")
        sources = collect_memory_sources(cwd=sub)
        names = {p.name for p in sources}
        assert "AGENTS.md" in names
        assert ".bog-agents.md" in names

    def test_returns_empty_when_no_files(self, repo: Path) -> None:
        assert collect_memory_sources(cwd=repo) == []
