"""Post a jury pass as a GitHub pull-request review (ROADMAP #67).

Findings that name a `path:line` become line comments on the diff; the rest
go into the review body, together with the `<!-- bog-review:<sha> -->`
marker so a second run (or CI) can see the diff was already reviewed. Every
GitHub call goes through an injected `run_gh` callable with the same shape as
`pr_output._run_gh`, so the module unit-tests without `gh` and never posts
from a test.
"""

from __future__ import annotations

import contextlib
import json
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bog_agents_cli.self_review_memo import marker_comment, parse_marker

if TYPE_CHECKING:
    from collections.abc import Callable

_PR_URL_RE = re.compile(r"github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)")
_LOCATION_RE = re.compile(r"(?P<path>[A-Za-z0-9_./\\-]+\.[A-Za-z0-9]+):(?P<line>\d+)")
MAX_LINE_COMMENTS = 40


@dataclass
class ReviewComment:
    """One line-anchored review comment."""

    path: str
    line: int
    body: str


@dataclass
class ReviewPayload:
    """What `POST /repos/{r}/pulls/{n}/reviews` receives."""

    body: str
    comments: list[ReviewComment] = field(default_factory=list)
    event: str = "COMMENT"

    def to_json(self) -> dict[str, Any]:
        """GitHub's request shape."""
        return {
            "body": self.body,
            "event": self.event,
            "comments": [
                {"path": c.path, "line": c.line, "body": c.body} for c in self.comments
            ],
        }


def pr_ref_from_url(pr_url: str) -> tuple[str, int] | None:
    """`("owner/repo", number)` from a PR URL, or `None`."""
    match = _PR_URL_RE.search(pr_url or "")
    return (match.group(1), int(match.group(2))) if match else None


def parse_finding_location(
    text: str, *, changed_files: set[str] | None = None
) -> tuple[str, int] | None:
    """`(path, line)` when a finding names one and the path is in the diff (any path when `changed_files` is `None`)."""
    for match in _LOCATION_RE.finditer(text or ""):
        path = match.group("path").replace("\\", "/").lstrip("./")
        if (
            changed_files is None
            or path in changed_files
            or any(f.endswith("/" + path) for f in changed_files)
        ):
            return path, int(match.group("line"))
    return None


def build_review_payload(
    report: Any,  # noqa: ANN401 - jury.JuryReport
    *,
    diff_sha: str,
    changed_files: set[str] | None = None,
    effort: str = "default",
) -> ReviewPayload:
    """Turn a `JuryReport` into a review: located findings become line comments, the rest go into the body."""
    marker = marker_comment(diff_sha)
    comments: list[ReviewComment] = []
    unlocated: list[str] = []
    for verdict in getattr(report, "verdicts", ()):
        for issue in getattr(verdict, "issues", ()):
            location = parse_finding_location(str(issue), changed_files=changed_files)
            text = f"**{verdict.juror}**: {issue}"
            if location is not None and len(comments) < MAX_LINE_COMMENTS:
                comments.append(
                    ReviewComment(path=location[0], line=location[1], body=text)
                )
            else:
                unlocated.append(text)
    consensus = getattr(report, "consensus", "")
    score = getattr(report, "avg_score", 0.0)
    lines = [
        marker,
        f"### bog-agents jury pass — {consensus or 'no consensus'} (avg score {score:.1f}, effort {effort})",
        "",
    ]
    for verdict in getattr(report, "verdicts", ()):
        lines.append(
            f"- **{verdict.juror}** — {verdict.verdict} ({verdict.score}): {verdict.summary}"
        )
    if unlocated:
        lines.extend(
            ["", "Findings without a line anchor:", *[f"- {t}" for t in unlocated]]
        )
    if comments:
        lines.extend(["", f"{len(comments)} finding(s) posted as line comments."])
    return ReviewPayload(body="\n".join(lines), comments=comments)


def already_reviewed(
    run_gh: Callable[[list[str]], tuple[bool, str]],
    repo: str,
    number: int,
    diff_sha: str,
) -> bool:
    """Whether a review carrying this fingerprint's marker is already on the PR."""
    ok, out = run_gh(["api", f"repos/{repo}/pulls/{number}/reviews", "--paginate"])
    if not ok or not out.strip():
        return False
    try:
        reviews = json.loads(out)
    except ValueError:
        return False
    wanted = marker_comment(diff_sha)
    marker_sha = parse_marker(wanted)
    return any(
        parse_marker(str(r.get("body", ""))) == marker_sha
        for r in reviews
        if isinstance(r, dict)
    )


def _post_json(
    run_gh: Callable[[list[str]], tuple[bool, str]], path: str, payload: dict[str, Any]
) -> tuple[bool, str]:
    """`gh api --method POST <path> --input <tmpfile>` (gh reads the body from the file)."""
    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8"
    ) as handle:
        json.dump(payload, handle)
        tmp = Path(handle.name)
    try:
        return run_gh(["api", "--method", "POST", path, "--input", str(tmp)])
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def post_review(
    run_gh: Callable[[list[str]], tuple[bool, str]],
    repo: str,
    number: int,
    payload: ReviewPayload,
) -> tuple[bool, str]:
    """`POST` the review; returns `(ok, message)`."""
    path = f"repos/{repo}/pulls/{number}/reviews"
    ok, out = _post_json(run_gh, path, payload.to_json())
    if not ok and payload.comments:
        # A stale line number makes GitHub reject the whole review; retry without anchors.
        fallback = ReviewPayload(
            body=payload.body
            + "\n\n"
            + "\n".join(f"- `{c.path}:{c.line}` {c.body}" for c in payload.comments)
        )
        ok, out = _post_json(run_gh, path, fallback.to_json())
        if ok:
            return (
                True,
                f"posted review without line anchors ({len(payload.comments)} finding(s) listed in the body)",
            )
    return ok, out[:300] if out else ("posted review" if ok else "gh api failed")


def post_jury_review(
    *,
    pr_url: str,
    report: Any,  # noqa: ANN401 - jury.JuryReport
    diff_sha: str,
    changed_files: set[str] | None,
    effort: str,
    run_gh: Callable[[list[str]], tuple[bool, str]],
) -> tuple[bool, str]:
    """Dedupe on the marker, then post; `(ok, message)` for the caller to print."""
    ref = pr_ref_from_url(pr_url)
    if ref is None:
        return False, f"not a GitHub PR URL: {pr_url!r}"
    repo, number = ref
    if already_reviewed(run_gh, repo, number, diff_sha):
        return (
            True,
            f"PR #{number} already carries a review for diff {diff_sha[:12]}; nothing posted",
        )
    payload = build_review_payload(
        report, diff_sha=diff_sha, changed_files=changed_files, effort=effort
    )
    ok, message = post_review(run_gh, repo, number, payload)
    return ok, (
        f"posted jury review on PR #{number} ({len(payload.comments)} line comment(s))"
        if ok and message.startswith("{")
        else message
    )


__all__ = [
    "MAX_LINE_COMMENTS",
    "ReviewComment",
    "ReviewPayload",
    "already_reviewed",
    "build_review_payload",
    "parse_finding_location",
    "post_jury_review",
    "post_review",
    "pr_ref_from_url",
]
