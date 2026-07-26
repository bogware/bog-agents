"""Evidence bundle middleware (#29).

Wraps `bog_agents.evidence` in an agent-facing tool (`emit_evidence_bundle`)
and an optional end-of-run auto-emit, so an autonomous change ships with
review-ready proof-of-work: the diff, the verification-command results, the
rubric verdict (read from state when a rubric grader ran), and any screenshots.
The rendered markdown is written to `.bog-agents/evidence/<ts>.md` and can be
attached to a GitHub PR in the same call.
"""

from __future__ import annotations

import logging
import subprocess  # bounded argv-form gh invocation only
import time
from pathlib import Path
from typing import Annotated, Any

from langchain.agents.middleware.types import AgentMiddleware, ContextT, ResponseT
from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, StructuredTool
from typing_extensions import TypedDict

from bog_agents.evidence import (
    EvidenceBundle,
    RubricVerdict,
    Screenshot,
    collect_git_evidence,
    render_evidence_markdown,
    run_checks,
)

logger = logging.getLogger(__name__)


class EvidenceBundleState(TypedDict, total=False):
    """State keys the middleware reads defensively when present.

    `rubric_evaluation` mirrors what a rubric grader may deposit: a mapping with
    `result`, `summary`, and a `criteria` list. Absent when no grader ran.
    """

    rubric_evaluation: dict[str, Any]


class EvidenceBundleMiddleware(AgentMiddleware[EvidenceBundleState, ContextT, ResponseT]):
    """Provide `emit_evidence_bundle` and (optionally) auto-emit at run end.

    Args:
        working_dir: Repository/worktree root the diff and checks run in.
        check_commands: Argv-form verification commands to run when a bundle is
            emitted, e.g. ``[["pytest", "-q"], ["ruff", "check", "."]]``. When
            omitted, the bundle records no checks (the agent can still pass the
            diff + rubric verdict).
        output_dir: Where rendered bundles are written. Defaults to
            ``<working_dir>/.bog-agents/evidence``.
        auto_emit: When True, a bundle is written automatically at the end of
            the run (git + checks + any rubric verdict in state) even if the
            agent never called the tool — the "on every autonomous PR" path.
    """

    state_schema = EvidenceBundleState

    def __init__(
        self,
        *,
        working_dir: Path | None = None,
        check_commands: list[list[str]] | None = None,
        output_dir: Path | None = None,
        auto_emit: bool = False,
    ) -> None:
        self._working_dir = working_dir or Path.cwd()
        self._check_commands = check_commands or []
        self._output_dir = output_dir or (self._working_dir / ".bog-agents" / "evidence")
        self._auto_emit = auto_emit
        self.tools = self._build_tools()

    # ------------------------------------------------------------------ #
    # Bundle assembly (shared by the tool and the auto-emit hook)
    # ------------------------------------------------------------------ #

    def _rubric_from_state(self, state: EvidenceBundleState | dict[str, Any] | None) -> RubricVerdict | None:
        """Best-effort read of a rubric grader's verdict from agent state."""
        if not isinstance(state, dict):
            return None
        raw = state.get("rubric_evaluation")
        if not isinstance(raw, dict) or not raw.get("result"):
            return None
        criteria = raw.get("criteria")
        return RubricVerdict(
            result=str(raw.get("result", "")),
            summary=str(raw.get("summary", "")),
            criteria=list(criteria) if isinstance(criteria, list) else [],
        )

    def _assemble(
        self,
        *,
        title: str,
        summary: str,
        include_diff: bool,
        rubric: RubricVerdict | None,
        screenshots: list[Screenshot],
    ) -> EvidenceBundle:
        diff_stat, diff = collect_git_evidence(self._working_dir, include_diff=include_diff)
        commands = run_checks(self._check_commands, cwd=self._working_dir) if self._check_commands else []
        return EvidenceBundle(
            title=title or "Evidence bundle",
            summary=summary,
            diff_stat=diff_stat,
            diff=diff,
            commands=commands,
            rubric=rubric,
            screenshots=screenshots,
        )

    def _write(self, markdown: str) -> Path:
        """Write the rendered bundle to a timestamped file, returning its path."""
        self._output_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        path = self._output_dir / f"evidence-{stamp}.md"
        # Avoid clobbering a same-second emit.
        counter = 1
        while path.exists():
            path = self._output_dir / f"evidence-{stamp}-{counter}.md"
            counter += 1
        path.write_text(markdown, encoding="utf-8")
        return path

    def _attach_to_pr(self, pr_number: int, markdown: str) -> str:
        """Post the bundle as a comment on a GitHub PR via `gh` (best-effort)."""
        try:
            result = subprocess.run(
                ["gh", "pr", "comment", str(pr_number), "--body", markdown],
                cwd=str(self._working_dir),
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return f"could not attach to PR #{pr_number}: {exc}"
        if result.returncode != 0:
            return f"gh pr comment failed for #{pr_number}: {(result.stderr or result.stdout).strip()}"
        return f"attached to PR #{pr_number}"

    # ------------------------------------------------------------------ #
    # Tool
    # ------------------------------------------------------------------ #

    def _build_tools(self) -> list[BaseTool]:
        middleware = self

        def emit_evidence_bundle(
            runtime: ToolRuntime[None, EvidenceBundleState],
            summary: Annotated[str, "One-paragraph summary of what changed and why it's correct."] = "",
            title: Annotated[str, "Bundle title."] = "Evidence bundle",
            include_diff: Annotated[bool, "Embed the full diff (collapsed) in addition to the stat."] = True,
            pr_number: Annotated[int, "GitHub PR number to attach the bundle to (0 = don't attach)."] = 0,
        ) -> str:
            """Package proof-of-work (diff, checks, rubric verdict, screenshots) into a review-ready artifact.

            Runs the configured verification commands, captures the git diff, and
            folds in the rubric verdict if a grader ran. Writes the rendered
            markdown to the evidence directory and, when `pr_number` is given,
            attaches it as a PR comment.
            """
            rubric = middleware._rubric_from_state(getattr(runtime, "state", None))
            bundle = middleware._assemble(
                title=title,
                summary=summary,
                include_diff=include_diff,
                rubric=rubric,
                screenshots=[],
            )
            markdown = render_evidence_markdown(bundle)
            path = middleware._write(markdown)

            status = "merge-ready" if bundle.merge_ready else "needs attention"
            lines = [f"Evidence bundle written to {path} ({status})."]
            if bundle.commands:
                passed = sum(1 for c in bundle.commands if c.ok)
                lines.append(f"Checks: {passed}/{len(bundle.commands)} passed.")
            if pr_number:
                lines.append(middleware._attach_to_pr(pr_number, markdown))
            return "\n".join(lines)

        return [
            StructuredTool.from_function(
                name="emit_evidence_bundle",
                description="Package the diff, test/lint results, rubric verdict, and screenshots into a review-ready evidence artifact (optionally attach to a PR).",
                func=emit_evidence_bundle,
            )
        ]

    # ------------------------------------------------------------------ #
    # Optional auto-emit at run end
    # ------------------------------------------------------------------ #

    def after_agent(self, state: EvidenceBundleState, runtime: Any) -> dict[str, Any] | None:
        """Auto-write a bundle at run end when `auto_emit` is set (best-effort)."""
        if not self._auto_emit:
            return None
        try:
            bundle = self._assemble(
                title="Evidence bundle",
                summary="",
                include_diff=True,
                rubric=self._rubric_from_state(state),
                screenshots=[],
            )
            # Nothing changed and nothing to attest → don't litter the tree.
            if not bundle.diff_stat.strip() and not bundle.commands and bundle.rubric is None:
                return None
            self._write(render_evidence_markdown(bundle))
        except Exception:
            logger.debug("evidence auto-emit failed", exc_info=True)
        return None
