"""Tests for the self-review gate (ROADMAP #3) — arg parsing + prompt build."""

from __future__ import annotations

from bog_agents_cli.self_review_controller import (
    REVIEW_LENSES,
    generate_self_review_prompt,
    parse_self_review_args,
)


class TestParseSelfReviewArgs:
    def test_default_is_working_tree(self) -> None:
        t = parse_self_review_args("")
        assert t.scope == "working"
        assert t.fix is False

    def test_staged(self) -> None:
        assert parse_self_review_args("--staged").scope == "staged"

    def test_fix_flag(self) -> None:
        t = parse_self_review_args("--fix")
        assert t.fix is True
        assert t.scope == "working"

    def test_staged_and_fix(self) -> None:
        t = parse_self_review_args("--staged --fix")
        assert t.scope == "staged"
        assert t.fix is True

    def test_branch(self) -> None:
        t = parse_self_review_args("--branch main")
        assert t.scope == "branch"
        assert t.ref == "main"

    def test_commit_ref(self) -> None:
        t = parse_self_review_args("HEAD~1")
        assert t.scope == "commit"
        assert t.ref == "HEAD~1"


class TestGenerateSelfReviewPrompt:
    def test_includes_all_five_lenses(self) -> None:
        prompt = generate_self_review_prompt(parse_self_review_args(""))
        assert len(REVIEW_LENSES) == 5
        for name, _ in REVIEW_LENSES:
            assert name in prompt

    def test_has_verdict_and_severity_scale(self) -> None:
        prompt = generate_self_review_prompt(parse_self_review_args(""))
        assert "VERDICT: SHIP" in prompt
        assert "VERDICT: FIX-FIRST" in prompt
        assert "blocker" in prompt

    def test_working_scope_targets_uncommitted(self) -> None:
        prompt = generate_self_review_prompt(parse_self_review_args(""))
        assert "uncommitted" in prompt

    def test_branch_scope_mentions_base_ref(self) -> None:
        prompt = generate_self_review_prompt(parse_self_review_args("--branch develop"))
        assert "develop" in prompt

    def test_fix_appends_fix_section(self) -> None:
        plain = generate_self_review_prompt(parse_self_review_args(""))
        fixing = generate_self_review_prompt(parse_self_review_args("--fix"))
        assert "Then fix" not in plain
        assert "Then fix" in fixing
