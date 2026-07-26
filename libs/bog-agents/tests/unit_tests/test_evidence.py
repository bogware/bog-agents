"""Tests for evidence bundles (#29) — render, collect, and middleware assembly."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from bog_agents.evidence import (
    CommandRun,
    EvidenceBundle,
    RubricVerdict,
    Screenshot,
    collect_git_evidence,
    render_evidence_markdown,
    run_checks,
)
from bog_agents.middleware.evidence_bundle import EvidenceBundleMiddleware


def _init_repo(path: Path) -> None:
    def _git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True)

    _git("init", "-q")
    _git("config", "user.email", "t@t.t")
    _git("config", "user.name", "t")
    (path / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-q", "-m", "init")


# --------------------------------------------------------------------------- #
# render_evidence_markdown / bundle logic
# --------------------------------------------------------------------------- #


class TestRender:
    def test_merge_ready_bundle_renders_green(self) -> None:
        bundle = EvidenceBundle(
            title="My change",
            summary="Refactored the widget.",
            diff_stat=" a.py | 2 +-\n 1 file changed",
            commands=[CommandRun("pytest -q", exit_code=0, output="ok")],
            rubric=RubricVerdict(result="satisfied", summary="all good", criteria=[{"name": "tests", "passed": True}]),
        )
        assert bundle.merge_ready is True
        md = render_evidence_markdown(bundle)
        assert "# My change" in md
        assert "✅ merge-ready" in md
        assert "Checks (1/1 passed)" in md
        assert "Rubric verdict: ✅ satisfied" in md
        assert "a.py | 2 +-" in md

    def test_failing_check_marks_not_ready_and_expands_output(self) -> None:
        bundle = EvidenceBundle(
            commands=[CommandRun("ruff check .", exit_code=1, output="E501 line too long")],
        )
        assert bundle.merge_ready is False
        md = render_evidence_markdown(bundle)
        assert "⚠️ needs attention" in md
        assert "❌ `ruff check .`" in md
        assert "E501 line too long" in md  # failing output is expanded

    def test_needs_revision_rubric_blocks_merge_ready(self) -> None:
        bundle = EvidenceBundle(
            commands=[CommandRun("pytest", exit_code=0)],
            rubric=RubricVerdict(
                result="needs_revision",
                criteria=[{"name": "handles empty input", "passed": False, "gap": "no empty-input test"}],
            ),
        )
        assert bundle.merge_ready is False
        md = render_evidence_markdown(bundle)
        assert "❌ handles empty input — no empty-input test" in md

    def test_empty_sections_are_omitted(self) -> None:
        md = render_evidence_markdown(EvidenceBundle(title="Bare"))
        assert "## Checks" not in md
        assert "## Rubric" not in md
        assert "## Changes" not in md
        assert "# Bare" in md

    def test_screenshots_rendered(self) -> None:
        md = render_evidence_markdown(EvidenceBundle(screenshots=[Screenshot(path="/tmp/before.png", caption="before")]))
        assert "## Screenshots" in md
        assert "before: `/tmp/before.png`" in md


# --------------------------------------------------------------------------- #
# collect_git_evidence / run_checks
# --------------------------------------------------------------------------- #


class TestCollectGit:
    def test_captures_working_tree_diff(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        (tmp_path / "a.py").write_text("x = 2\n", encoding="utf-8")
        diff_stat, diff = collect_git_evidence(tmp_path)
        assert "a.py" in diff_stat
        assert "-x = 1" in diff and "+x = 2" in diff

    def test_git_failure_yields_empty(self, tmp_path: Path) -> None:
        # A nonexistent working dir makes git unlaunchable → empty, never raises.
        # (The project's tmp_path is repo-local, so a real dir would resolve to
        # bog's own repo — this exercises the error path deterministically.)
        diff_stat, diff = collect_git_evidence(tmp_path / "nope-does-not-exist")
        assert diff_stat == "" and diff == ""

    def test_include_diff_false_skips_body(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        (tmp_path / "a.py").write_text("x = 3\n", encoding="utf-8")
        _stat, diff = collect_git_evidence(tmp_path, include_diff=False)
        assert diff == ""


class TestRunChecks:
    def test_pass_and_fail_recorded(self, tmp_path: Path) -> None:
        results = run_checks(
            [[sys.executable, "-c", "import sys; sys.exit(0)"], [sys.executable, "-c", "import sys; sys.exit(1)"]],
            cwd=tmp_path,
        )
        assert results[0].ok is True
        assert results[1].ok is False

    def test_missing_binary_records_none_exit(self, tmp_path: Path) -> None:
        results = run_checks([["definitely-not-a-real-binary-xyz"]], cwd=tmp_path)
        assert results[0].exit_code is None
        assert results[0].ok is False


# --------------------------------------------------------------------------- #
# EvidenceBundleMiddleware
# --------------------------------------------------------------------------- #


class TestMiddleware:
    def test_provides_emit_tool(self, tmp_path: Path) -> None:
        mw = EvidenceBundleMiddleware(working_dir=tmp_path)
        assert [t.name for t in mw.tools] == ["emit_evidence_bundle"]

    def test_assemble_collects_diff_and_checks(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        (tmp_path / "a.py").write_text("x = 9\n", encoding="utf-8")
        mw = EvidenceBundleMiddleware(
            working_dir=tmp_path,
            check_commands=[[sys.executable, "-c", "import sys; sys.exit(0)"]],
        )
        bundle = mw._assemble(title="t", summary="s", include_diff=True, rubric=None, screenshots=[])
        assert "a.py" in bundle.diff_stat
        assert len(bundle.commands) == 1 and bundle.commands[0].ok
        assert bundle.merge_ready is True

    def test_rubric_read_from_state(self, tmp_path: Path) -> None:
        mw = EvidenceBundleMiddleware(working_dir=tmp_path)
        verdict = mw._rubric_from_state({"rubric_evaluation": {"result": "satisfied", "summary": "ok", "criteria": [{"name": "c", "passed": True}]}})
        assert verdict is not None
        assert verdict.satisfied is True
        # No evaluation in state → None.
        assert mw._rubric_from_state({}) is None
        assert mw._rubric_from_state(None) is None

    def test_write_produces_file(self, tmp_path: Path) -> None:
        mw = EvidenceBundleMiddleware(working_dir=tmp_path)
        path = mw._write("# hi\n")
        assert path.exists()
        assert path.read_text(encoding="utf-8") == "# hi\n"
        assert path.parent == tmp_path / ".bog-agents" / "evidence"
