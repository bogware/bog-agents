"""ROADMAP #67: jury verdicts → GitHub review with line comments, marker dedupe, and the post-PR pass."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from bog_agents_cli import github_review as gr
from bog_agents_cli.pr_review_pass import run_post_pr_review
from bog_agents_cli.self_review_memo import load_memo, marker_comment


def _report() -> SimpleNamespace:
    return SimpleNamespace(
        consensus="approve",
        avg_score=7.5,
        verdicts=(
            SimpleNamespace(
                juror="j1",
                verdict="approve",
                score=8,
                summary="fine",
                issues=("src/app.py:12 unused import", "consider more tests"),
            ),
            SimpleNamespace(
                juror="j2",
                verdict="request_changes",
                score=7,
                summary="one bug",
                issues=("bug in src/app.py:40 — off by one",),
            ),
        ),
    )


class _Gh:
    """Fake `gh` runner: records calls, serves existing reviews, reads the --input file."""

    def __init__(
        self,
        existing: list[dict[str, Any]] | None = None,
        *,
        fail_with_comments: bool = False,
    ) -> None:
        self.calls: list[list[str]] = []
        self.posted: list[dict[str, Any]] = []
        self.existing = existing or []
        self.fail_with_comments = fail_with_comments

    def __call__(self, args: list[str]) -> tuple[bool, str]:
        self.calls.append(list(args))
        if args[:2] == ["api", "--method"]:
            payload = json.loads(
                Path(args[args.index("--input") + 1]).read_text(encoding="utf-8")
            )
            if self.fail_with_comments and payload.get("comments"):
                return False, "422 Unprocessable Entity: line must be part of the diff"
            self.posted.append(payload)
            return True, json.dumps({"id": len(self.posted)})
        if args[0] == "api":
            return True, json.dumps(self.existing)
        return False, "unexpected"


class TestPayload:
    def test_locations_and_body(self) -> None:
        assert gr.pr_ref_from_url("https://github.com/bogware/bog-agents/pull/191") == (
            "bogware/bog-agents",
            191,
        )
        assert gr.pr_ref_from_url("https://example.com/x") is None
        assert gr.parse_finding_location("bug in src/app.py:40 — off by one") == (
            "src/app.py",
            40,
        )
        assert (
            gr.parse_finding_location("src/app.py:40", changed_files={"lib/other.py"})
            is None
        )
        assert gr.parse_finding_location(
            "see app.py:7", changed_files={"src/app.py"}
        ) == ("app.py", 7)
        payload = gr.build_review_payload(
            _report(), diff_sha="a" * 64, changed_files={"src/app.py"}, effort="high"
        )
        assert payload.body.startswith(marker_comment("a" * 64))
        assert "approve (avg score 7.5, effort high)" in payload.body
        assert [(c.path, c.line) for c in payload.comments] == [
            ("src/app.py", 12),
            ("src/app.py", 40),
        ]
        assert (
            "consider more tests" in payload.body
            and "2 finding(s) posted as line comments" in payload.body
        )
        assert payload.to_json()["event"] == "COMMENT"


class TestPosting:
    def test_dedupes_on_marker_and_posts_via_input_file(self) -> None:
        gh = _Gh(existing=[{"body": "old"}])
        ok, message = gr.post_jury_review(
            pr_url="https://github.com/o/r/pull/5",
            report=_report(),
            diff_sha="b" * 64,
            changed_files=None,
            effort="default",
            run_gh=gh,
        )
        assert ok and "2 line comment(s)" in message
        assert (
            gh.posted[0]["comments"][0]["path"] == "src/app.py"
            and marker_comment("b" * 64) in gh.posted[0]["body"]
        )
        gh2 = _Gh(existing=[{"body": gh.posted[0]["body"]}])
        ok, message = gr.post_jury_review(
            pr_url="https://github.com/o/r/pull/5",
            report=_report(),
            diff_sha="b" * 64,
            changed_files=None,
            effort="default",
            run_gh=gh2,
        )
        assert ok and "already carries a review" in message and not gh2.posted

    def test_falls_back_without_anchors_when_github_rejects_lines(self) -> None:
        gh = _Gh(fail_with_comments=True)
        ok, message = gr.post_jury_review(
            pr_url="https://github.com/o/r/pull/9",
            report=_report(),
            diff_sha="c" * 64,
            changed_files=None,
            effort="default",
            run_gh=gh,
        )
        assert ok and "without line anchors" in message
        assert (
            gh.posted[-1]["comments"] == []
            and "`src/app.py:12`" in gh.posted[-1]["body"]
        )
        assert not gr.post_jury_review(
            pr_url="nope",
            report=_report(),
            diff_sha="c" * 64,
            changed_files=None,
            effort="default",
            run_gh=gh,
        )[0]


class TestPostPrPass:
    async def test_runs_jury_over_branch_diff_and_records_memo(
        self, tmp_path: Path
    ) -> None:
        def git(*args: str) -> None:
            subprocess.run(
                ["git", *args], cwd=tmp_path, check=True, capture_output=True
            )

        git("init", "-q", "-b", "main")
        git("config", "user.email", "t@t.t")
        git("config", "user.name", "t")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
        git("add", "-A")
        git("commit", "-q", "-m", "init")
        git("switch", "-q", "-c", "bog-agents/feature")
        (tmp_path / "src" / "app.py").write_text("x = 2\n", encoding="utf-8")
        git("commit", "-q", "-am", "change")
        seen: dict[str, Any] = {}

        async def fake_jury(diff_text: str, jurors: list[Any]) -> SimpleNamespace:
            seen["diff"] = diff_text
            seen["jurors"] = jurors
            return _report()

        gh = _Gh()
        result = SimpleNamespace(
            pr_url="https://github.com/o/r/pull/3",
            branch_name="bog-agents/feature",
            files_changed=["src/app.py"],
        )
        ok, message = await run_post_pr_review(
            result,
            base_branch="main",
            effort="high",
            cwd=tmp_path,
            jurors=[("j", object())],
            run_jury_fn=fake_jury,
            run_gh=gh,
        )
        assert ok and "posted jury review on PR #3" in message
        assert "-x = 1" in seen["diff"] and seen["jurors"][0][0] == "j"
        recorded = load_memo(tmp_path, "bog-agents/feature")
        assert (
            recorded is not None
            and recorded.effort == "high"
            and recorded.verdict == "approve"
            and recorded.scope == "branch"
        )
        ok, message = await run_post_pr_review(
            SimpleNamespace(pr_url="", branch_name="", files_changed=[]),
            cwd=tmp_path,
            jurors=[("j", object())],
            run_jury_fn=fake_jury,
            run_gh=gh,
        )
        assert not ok and "no PR URL" in message
