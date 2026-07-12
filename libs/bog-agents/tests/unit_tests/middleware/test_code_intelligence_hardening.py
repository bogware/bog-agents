"""Hardening tests for code_intelligence scan bounds (finding S23).

Verifies that `codebase_health` and `onboard` prune vendored/build/VCS
directories and never walk an unbounded number of files.
"""

from __future__ import annotations

from pathlib import Path

from langchain.tools import ToolRuntime
from langgraph.store.memory import InMemoryStore

from bog_agents.middleware.code_intelligence import (
    _MAX_FILES,
    _SKIP_DIRS,
    CodeIntelligenceMiddleware,
    _is_skipped,
)


def _make_runtime() -> ToolRuntime:
    return ToolRuntime(
        state={"messages": []},
        context=None,
        tool_call_id="tc",
        store=InMemoryStore(),
        stream_writer=lambda _: None,
        config={},
    )


def _call(mw: CodeIntelligenceMiddleware, name: str) -> str:
    """Invoke a code-intelligence tool's underlying function with a runtime."""
    tool = next(t for t in mw.tools if t.name == name)
    return tool.func(_make_runtime())


def test_skip_dirs_match_repo_map() -> None:
    """The skip set must cover the common vendored/build directories."""
    for expected in ("node_modules", "__pycache__", ".venv", "site-packages", ".git"):
        assert expected in _SKIP_DIRS


def test_is_skipped_detects_vendored_paths() -> None:
    assert _is_skipped(Path("project/.venv/lib/site-packages/foo.py"))
    assert _is_skipped(Path("project/node_modules/pkg/index.js"))
    assert not _is_skipped(Path("project/src/main.py"))


def test_codebase_health_ignores_vendored_files(tmp_path: Path) -> None:
    """Health scan must count project source only, not site-packages."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "real_a.py").write_text("def a() -> int:\n    return 1\n", encoding="utf-8")
    (src / "real_b.py").write_text("def b() -> int:\n    return 2\n", encoding="utf-8")

    vendored = tmp_path / ".venv" / "lib" / "site-packages" / "dep"
    vendored.mkdir(parents=True)
    for i in range(50):
        (vendored / f"mod_{i}.py").write_text("x = 1\n", encoding="utf-8")

    mw = CodeIntelligenceMiddleware(working_dir=tmp_path)
    report = _call(mw, "codebase_health")

    assert "Files: 2" in report


def test_onboard_counts_exclude_vendored(tmp_path: Path) -> None:
    """Onboarding file counts must exclude vendored dirs."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (tmp_path / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (tmp_path / "app.ts").write_text("export const x = 1;\n", encoding="utf-8")

    node_modules = tmp_path / "node_modules" / "pkg"
    node_modules.mkdir(parents=True)
    for i in range(30):
        (node_modules / f"f_{i}.js").write_text("module.exports = {};\n", encoding="utf-8")

    mw = CodeIntelligenceMiddleware(working_dir=tmp_path)
    guide = _call(mw, "onboard")

    # 1 real .py, 1 real .ts, 0 real .js (vendored .js are pruned)
    assert "Python: 1, TypeScript: 1, JavaScript: 0" in guide


def test_codebase_health_respects_max_files_cap(tmp_path: Path) -> None:
    """Health scan must stop at the global file cap."""
    src = tmp_path / "src"
    src.mkdir()
    # Create more files than the cap so the early break is exercised.
    for i in range(_MAX_FILES + 25):
        (src / f"f_{i}.py").write_text("x = 1\n", encoding="utf-8")

    mw = CodeIntelligenceMiddleware(working_dir=tmp_path)
    report = _call(mw, "codebase_health")

    assert f"Files: {_MAX_FILES}" in report
