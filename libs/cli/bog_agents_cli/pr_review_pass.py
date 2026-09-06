"""Post-PR jury pass for `--pr --pr-review` (ROADMAP #67).

After `run_pr_mode` opens the pull request, run the configured jury over the
branch diff and post the verdicts as a GitHub review — line comments where a
finding names `path:line`, the rest in the review body — deduped on the diff
fingerprint so a re-run never double-posts. The jurors, the jury runner and
the `gh` runner are all injectable, so the pass unit-tests without models or
GitHub; the CLI entry point wires the real ones.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bog_agents_cli.github_review import post_jury_review
from bog_agents_cli.self_review_memo import (
    SelfReviewMemo,
    diff_fingerprint,
    review_diff_text,
    save_memo,
)

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


def _load_jurors(
    profile_override: dict[str, Any] | None = None,
) -> list[tuple[str, Any]]:
    """`(label, model)` pairs from `[jury].models` in config.toml; unusable specs are skipped."""
    from bog_agents_cli.config import create_model
    from bog_agents_cli.jury import load_jury_model_specs

    jurors: list[tuple[str, Any]] = []
    for index, spec in enumerate(load_jury_model_specs(), start=1):
        try:
            resolved = create_model(spec, profile_overrides=profile_override)
        except Exception:  # a bad juror spec must not sink the pass
            logger.debug("Skipping juror %s", spec, exc_info=True)
            continue
        jurors.append((f"juror-{index} ({spec})", resolved.model))
    return jurors


async def run_post_pr_review(
    pr_result: Any,  # noqa: ANN401 - pr_output.PRResult
    *,
    base_branch: str = "main",
    effort: str = "default",
    cwd: str | Path | None = None,
    jurors: list[tuple[str, Any]] | None = None,
    run_jury_fn: Callable[..., Any] | None = None,
    run_gh: Callable[[list[str]], tuple[bool, str]] | None = None,
) -> tuple[bool, str]:
    """Review the new PR's diff with the jury and post the result; `(ok, message)`.

    Args:
        pr_result: The `PRResult` from `run_pr_mode` (needs `pr_url`, `branch_name`, `files_changed`).
        base_branch: Base the branch was cut from (the diff is `base...HEAD`).
        effort: Review effort recorded in the memo and named in the review body.
        cwd: Repository directory (default: current directory).
        jurors: Injected `(label, model)` pairs; loaded from config when `None`.
        run_jury_fn: Injected jury runner (tests); `jury.run_jury` when `None`.
        run_gh: Injected `gh` runner; `pr_output._run_gh` bound to `cwd` when `None`.

    Returns:
        Whether a review was posted (or already present) and a one-line message.
    """
    repo = Path(cwd) if cwd is not None else Path.cwd()
    pr_url = str(getattr(pr_result, "pr_url", "") or "")
    if not pr_url:
        return False, "no PR URL on the result; nothing to review"
    diff_text = review_diff_text(repo, scope="branch", ref=base_branch)
    sha = diff_fingerprint(diff_text)
    if not sha:
        return False, f"no diff against {base_branch}; nothing to review"
    active_jurors = jurors if jurors is not None else _load_jurors()
    if not active_jurors:
        return False, "no usable jurors — configure [jury].models in config.toml"
    if run_jury_fn is None:
        from bog_agents_cli.jury import run_jury

        run_jury_fn = run_jury
    report = await run_jury_fn(diff_text, active_jurors)
    if run_gh is None:
        from bog_agents_cli.pr_output import _run_gh

        def run_gh(args: list[str]) -> tuple[bool, str]:
            return _run_gh(args, cwd=str(repo))

    changed = {
        str(f).replace("\\", "/")
        for f in (getattr(pr_result, "files_changed", None) or [])
    }
    ok, message = post_jury_review(
        pr_url=pr_url,
        report=report,
        diff_sha=sha,
        changed_files=changed or None,
        effort=effort,
        run_gh=run_gh,
    )
    branch = str(getattr(pr_result, "branch_name", "") or "pr")
    save_memo(
        repo,
        SelfReviewMemo(
            branch=branch,
            scope="branch",
            base=base_branch,
            diff_sha=sha,
            effort=effort,
            verdict=str(getattr(report, "consensus", "")),
        ),
    )
    return ok, message


__all__ = ["run_post_pr_review"]
