"""Evidence bundles (#29) — proof-of-work for an autonomous change.

Packages the artifacts a reviewer needs to *trust* an agent-authored change —
the diff, the test/QA/lint output, the rubric verdict, screenshots, and the
commands that were run — into a single markdown artifact that can be attached
to a PR or handed to a daemon dispatch target. "Shows it works" instead of
"says it works" (the merge-ready bar the market moved to).

This module is the pure, IO-light core: dataclasses + a deterministic markdown
renderer + small collection helpers. The `EvidenceBundleMiddleware`
(`bog_agents.middleware.evidence_bundle`) is the thin lifecycle/tool wrapper
that assembles a bundle from a live run and writes/attaches it.
"""

from __future__ import annotations

import os
import subprocess  # bounded, argv-form git/check invocations only
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Full-diff bodies are collapsed into a <details> block; beyond this many
# characters the diff is truncated (the stat summary always stays intact) so a
# giant refactor doesn't produce a multi-megabyte comment.
_MAX_DIFF_CHARS = 60_000

# Per-command captured output is truncated to keep the artifact bounded; the
# exit code (pass/fail) is always preserved.
_MAX_COMMAND_OUTPUT_CHARS = 4_000


@dataclass
class CommandRun:
    """One verification command and its result (a test/lint/QA invocation)."""

    command: str
    exit_code: int | None = None
    output: str = ""

    @property
    def ok(self) -> bool:
        """True when the command exited 0."""
        return self.exit_code == 0


@dataclass
class RubricVerdict:
    """A rubric grader's terminal verdict, denormalized for rendering."""

    result: str
    """Grader result, e.g. ``"satisfied"`` / ``"needs_revision"``."""
    summary: str = ""
    criteria: list[dict[str, Any]] = field(default_factory=list)
    """Per-criterion entries: ``{"name": str, "passed": bool, "gap": str}``."""

    @property
    def satisfied(self) -> bool:
        """True when the grader marked the work satisfied."""
        return self.result.strip().lower() == "satisfied"


@dataclass
class Screenshot:
    """A captured before/after or browser-session image."""

    path: str
    caption: str = ""


@dataclass
class EvidenceBundle:
    """The full set of proof-of-work artifacts for one change."""

    title: str = "Evidence bundle"
    summary: str = ""
    diff_stat: str = ""
    diff: str = ""
    commands: list[CommandRun] = field(default_factory=list)
    rubric: RubricVerdict | None = None
    screenshots: list[Screenshot] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def all_checks_passed(self) -> bool:
        """True when every recorded verification command passed (vacuously true with none)."""
        return all(c.ok for c in self.commands)

    @property
    def merge_ready(self) -> bool:
        """True when checks pass and, if a rubric was run, it is satisfied."""
        return self.all_checks_passed and (self.rubric is None or self.rubric.satisfied)


def _truncate(text: str, limit: int) -> str:
    """Trim `text` to `limit` chars with a marker, preserving readability."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… [truncated {len(text) - limit} more characters]"


def render_evidence_markdown(bundle: EvidenceBundle) -> str:
    """Render an `EvidenceBundle` as a review-ready markdown artifact.

    Deterministic and side-effect free — safe to unit-test and to diff across
    runs. Sections are omitted when empty so a bundle with only tests, or only
    a diff, still renders cleanly.

    Args:
        bundle: The assembled evidence.

    Returns:
        A markdown string suitable for a PR comment or dispatch payload.
    """
    lines: list[str] = [f"# {bundle.title}", ""]

    badge = "✅ merge-ready" if bundle.merge_ready else "⚠️ needs attention"
    lines.append(f"**Status:** {badge}")
    lines.append("")
    if bundle.summary:
        lines.append(bundle.summary)
        lines.append("")

    if bundle.commands:
        passed = sum(1 for c in bundle.commands if c.ok)
        lines.append(f"## Checks ({passed}/{len(bundle.commands)} passed)")
        lines.append("")
        for cmd in bundle.commands:
            mark = "✅" if cmd.ok else "❌"
            code = "?" if cmd.exit_code is None else str(cmd.exit_code)
            lines.append(f"- {mark} `{cmd.command}` (exit {code})")
            if cmd.output.strip() and not cmd.ok:
                # Only expand output for failing checks — that's what a reviewer needs.
                lines.append("")
                lines.append("  <details><summary>output</summary>")
                lines.append("")
                lines.append("  ```")
                lines.extend(f"  {out_line}" for out_line in _truncate(cmd.output.rstrip(), _MAX_COMMAND_OUTPUT_CHARS).splitlines())
                lines.append("  ```")
                lines.append("")
                lines.append("  </details>")
        lines.append("")

    if bundle.rubric is not None:
        r = bundle.rubric
        mark = "✅" if r.satisfied else "⚠️"
        lines.append(f"## Rubric verdict: {mark} {r.result}")
        lines.append("")
        if r.summary:
            lines.append(r.summary)
            lines.append("")
        for crit in r.criteria:
            passed = bool(crit.get("passed"))
            cmark = "✅" if passed else "❌"
            name = str(crit.get("name", "criterion"))
            entry = f"- {cmark} {name}"
            gap = str(crit.get("gap", "")).strip()
            if not passed and gap:
                entry += f" — {gap}"
            lines.append(entry)
        lines.append("")

    if bundle.diff_stat.strip():
        lines.append("## Changes")
        lines.append("")
        if bundle.diff.strip():
            # ROADMAP #66: files in explanatory order (entry points and public
            # signatures first; tests, snapshots and lockfiles last) so a
            # reviewer reads the proof in the order it explains itself.
            from bog_agents.diff_ordering import render_ordered_stat, split_unified_diff

            ordered = render_ordered_stat(split_unified_diff(bundle.diff))
            if ordered:
                lines.append("Files in explanatory order:")
                lines.append("")
                lines.append("```")
                lines.append(ordered)
                lines.append("```")
                lines.append("")
        lines.append("```")
        lines.append(bundle.diff_stat.rstrip())
        lines.append("```")
        lines.append("")
        if bundle.diff.strip():
            lines.append("<details><summary>Full diff</summary>")
            lines.append("")
            lines.append("```diff")
            lines.append(_truncate(bundle.diff.rstrip(), _MAX_DIFF_CHARS))
            lines.append("```")
            lines.append("")
            lines.append("</details>")
            lines.append("")

    if bundle.screenshots:
        lines.append("## Screenshots")
        lines.append("")
        for shot in bundle.screenshots:
            caption = shot.caption or Path(shot.path).name
            lines.append(f"- {caption}: `{shot.path}`")
        lines.append("")

    lines.append("---")
    lines.append("_Generated by bog-agents evidence bundle (#29)._")
    return "\n".join(lines)


def collect_git_evidence(repo_dir: str | Path, *, ref: str = "HEAD", include_diff: bool = True) -> tuple[str, str]:
    """Collect `git diff --stat` and (optionally) the full diff for `repo_dir`.

    Uses argv-form git with a bounded timeout; never raises — a non-repo or a
    git error yields empty strings so the bundle still renders.

    Args:
        repo_dir: Repository (or worktree) directory.
        ref: The base ref to diff against (default the working tree vs `HEAD`).
        include_diff: When True, also capture the full unified diff.

    Returns:
        A ``(diff_stat, diff)`` tuple; either may be empty.
    """
    cwd = str(Path(repo_dir))
    # Scrub GIT_DIR / GIT_WORK_TREE so an inherited environment (a daemon run, a
    # git hook, a CI job) can't redirect the diff to the wrong repository — the
    # bundle must describe `repo_dir`, not whatever git env happened to be set.
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE")}

    def _git(*args: str) -> str:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return result.stdout if result.returncode == 0 else ""

    diff_stat = _git("diff", "--stat", ref)
    diff = _git("diff", ref) if include_diff else ""
    return diff_stat, diff


def run_checks(commands: list[list[str]], *, cwd: str | Path, timeout: float = 600.0) -> list[CommandRun]:
    """Run verification commands (argv form) and capture their results.

    Each command is an argv list (no shell) so a check can't be a
    metacharacter-injection vector. A command that can't launch is recorded
    with `exit_code=None`.

    Args:
        commands: Argv-form commands, e.g. ``[["pytest", "-q"], ["ruff", "check", "."]]``.
        cwd: Working directory to run them in.
        timeout: Per-command timeout in seconds.

    Returns:
        A `CommandRun` per input command, in order.
    """
    results: list[CommandRun] = []
    for argv in commands:
        display = " ".join(argv)
        try:
            proc = subprocess.run(
                argv,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            results.append(CommandRun(command=display, exit_code=None, output=f"timed out after {timeout:.0f}s"))
            continue
        except (OSError, subprocess.SubprocessError) as exc:
            results.append(CommandRun(command=display, exit_code=None, output=f"could not run: {exc}"))
            continue
        combined = (proc.stdout or "") + (proc.stderr or "")
        results.append(CommandRun(command=display, exit_code=proc.returncode, output=combined))
    return results
