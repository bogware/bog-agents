"""Tests for the GitHub webhook event parser (#30, Assign-to-bog front door)."""

from __future__ import annotations

from bog_agents_daemon.github_events import (
    CI_FAILURE,
    ISSUE_ASSIGNED,
    ISSUE_COMMENT,
    ISSUE_LABELED,
    parse_github_event,
)

_REPO = {"full_name": "acme/widget"}


class TestIssueAssigned:
    def test_assigned_to_bot_is_actionable(self) -> None:
        ev = parse_github_event(
            "issues",
            {
                "action": "assigned",
                "assignee": {"login": "bog-bot"},
                "issue": {"number": 7, "title": "Fix it", "body": "details"},
                "repository": _REPO,
            },
            bot_login="bog-bot",
        )
        assert ev is not None
        assert ev.kind == ISSUE_ASSIGNED
        assert ev.number == 7
        assert ev.title == "Fix it"
        assert ev.repo == "acme/widget"

    def test_assigned_to_someone_else_ignored(self) -> None:
        ev = parse_github_event(
            "issues",
            {"action": "assigned", "assignee": {"login": "alice"}, "issue": {"number": 7}},
            bot_login="bog-bot",
        )
        assert ev is None

    def test_any_assignment_when_bot_login_unset(self) -> None:
        ev = parse_github_event(
            "issues",
            {"action": "assigned", "assignee": {"login": "alice"}, "issue": {"number": 7}},
        )
        assert ev is not None and ev.kind == ISSUE_ASSIGNED


class TestIssueLabeled:
    def test_matching_label_actionable(self) -> None:
        ev = parse_github_event(
            "issues",
            {"action": "labeled", "label": {"name": "bog"}, "issue": {"number": 3, "title": "t"}},
            trigger_label="bog",
        )
        assert ev is not None and ev.kind == ISSUE_LABELED and ev.label == "bog"

    def test_other_label_ignored(self) -> None:
        ev = parse_github_event(
            "issues",
            {"action": "labeled", "label": {"name": "bug"}, "issue": {"number": 3}},
            trigger_label="bog",
        )
        assert ev is None

    def test_label_requires_opt_in(self) -> None:
        # No trigger_label configured → never fire on labels.
        ev = parse_github_event(
            "issues",
            {"action": "labeled", "label": {"name": "bog"}, "issue": {"number": 3}},
        )
        assert ev is None


class TestIssueComment:
    def test_comment_is_revision_request(self) -> None:
        ev = parse_github_event(
            "issue_comment",
            {
                "action": "created",
                "comment": {"body": "please also handle empty input", "user": {"login": "alice"}},
                "issue": {"number": 9, "title": "t"},
            },
            bot_login="bog-bot",
        )
        assert ev is not None
        assert ev.kind == ISSUE_COMMENT
        assert ev.body == "please also handle empty input"
        assert ev.actor == "alice"

    def test_bot_own_comment_ignored_to_avoid_loop(self) -> None:
        ev = parse_github_event(
            "issue_comment",
            {"action": "created", "comment": {"body": "working on it", "user": {"login": "bog-bot"}}, "issue": {"number": 9}},
            bot_login="bog-bot",
        )
        assert ev is None


class TestCIFailure:
    def test_check_run_failure_actionable(self) -> None:
        ev = parse_github_event(
            "check_run",
            {
                "action": "completed",
                "check_run": {"conclusion": "failure", "name": "pytest", "head_branch": "feature-x", "pull_requests": [{"number": 42}]},
                "repository": _REPO,
            },
        )
        assert ev is not None
        assert ev.kind == CI_FAILURE
        assert ev.branch == "feature-x"
        assert ev.number == 42

    def test_workflow_run_failure_actionable(self) -> None:
        ev = parse_github_event(
            "workflow_run",
            {"action": "completed", "workflow_run": {"conclusion": "failure", "head_branch": "main"}},
        )
        assert ev is not None and ev.kind == CI_FAILURE and ev.branch == "main"

    def test_passing_check_ignored(self) -> None:
        ev = parse_github_event(
            "check_run",
            {"action": "completed", "check_run": {"conclusion": "success", "head_branch": "x"}},
        )
        assert ev is None


class TestNonActionable:
    def test_unknown_event_ignored(self) -> None:
        assert parse_github_event("star", {"action": "created"}) is None

    def test_issue_opened_not_actionable(self) -> None:
        # Only assignment/labeling picks up an issue, not mere opening.
        assert parse_github_event("issues", {"action": "opened", "issue": {"number": 1}}) is None

    def test_non_dict_payload(self) -> None:
        assert parse_github_event("issues", None) is None  # type: ignore[arg-type]
