"""Parse GitHub webhook events into agent-trigger decisions (#30, Assign-to-bog).

The acquisition UX everyone shipped (Copilot, Jules): assign an issue to the bot
(or label it), and it picks the issue up, opens a draft PR, and works it; a red
CI check re-enters the session to repair. This module is the pure, testable
front door — it turns a GitHub webhook (event type + JSON payload) into a
structured `GitHubEvent` decision, with no network or side effects.

The daemon's `/webhooks/github` endpoint verifies the `X-Hub-Signature-256`
HMAC (reusing the existing per-trigger secret machinery), calls
`parse_github_event`, and dispatches the resulting trigger. The draft-PR
etiquette and CI-red re-entry are the execution layer built on this decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Decision kinds.
ISSUE_ASSIGNED = "issue_assigned"
ISSUE_LABELED = "issue_labeled"
ISSUE_COMMENT = "issue_comment"
CI_FAILURE = "ci_failure"


@dataclass
class GitHubEvent:
    """A parsed, actionable GitHub event.

    Attributes:
        kind: One of the module's decision-kind constants.
        number: Issue or PR number the event concerns.
        title: Issue/PR title (when available).
        body: Issue/PR/comment body (the instruction text to act on).
        actor: The login that triggered it (assigner / commenter).
        label: The label name for a `labeled` event.
        branch: Head branch for a PR / CI event (for checkout + repair).
        repo: ``owner/repo`` slug.
        raw: The original payload (for downstream context).
    """

    kind: str
    number: int = 0
    title: str = ""
    body: str = ""
    actor: str = ""
    label: str = ""
    branch: str = ""
    repo: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


def _repo_slug(payload: dict[str, Any]) -> str:
    repo = payload.get("repository")
    if isinstance(repo, dict):
        return str(repo.get("full_name", ""))
    return ""


def parse_github_event(
    event_type: str,
    payload: dict[str, Any],
    *,
    bot_login: str = "",
    trigger_label: str = "",
) -> GitHubEvent | None:
    """Turn a GitHub webhook into an actionable `GitHubEvent`, or None.

    Actionability rules (anything else returns None so the daemon stays quiet on
    the firehose of events GitHub sends):

    - ``issues`` / ``assigned``: actionable when assigned to ``bot_login`` (or
      any assignment when ``bot_login`` is unset).
    - ``issues`` / ``labeled``: actionable when the added label equals
      ``trigger_label`` (skipped entirely when ``trigger_label`` is unset — you
      must opt into label triggering to avoid firing on every label).
    - ``issue_comment`` / ``created``: a revision request — but never on the
      bot's own comments (that would loop).
    - ``check_run`` / ``workflow_run`` completed with ``conclusion == "failure"``:
      a CI-red repair trigger, carrying the head branch.

    Args:
        event_type: The ``X-GitHub-Event`` header value.
        payload: The parsed JSON body.
        bot_login: The bot's GitHub login (gates assignment/comment loops).
        trigger_label: Label that opts an issue into bot pickup.

    Returns:
        A `GitHubEvent`, or None when the event isn't actionable.
    """
    if not isinstance(payload, dict):
        return None
    action = str(payload.get("action", ""))
    repo = _repo_slug(payload)

    if event_type == "issues":
        issue = payload.get("issue")
        if not isinstance(issue, dict):
            return None
        number = int(issue.get("number", 0) or 0)
        base = {
            "number": number,
            "title": str(issue.get("title", "")),
            "body": str(issue.get("body", "") or ""),
            "repo": repo,
            "raw": payload,
        }
        if action == "assigned":
            assignee = payload.get("assignee") or {}
            login = str(assignee.get("login", "")) if isinstance(assignee, dict) else ""
            if bot_login and login != bot_login:
                return None
            return GitHubEvent(kind=ISSUE_ASSIGNED, actor=login, **base)
        if action == "labeled":
            label = payload.get("label") or {}
            name = str(label.get("name", "")) if isinstance(label, dict) else ""
            if not trigger_label or name != trigger_label:
                return None
            return GitHubEvent(kind=ISSUE_LABELED, label=name, **base)
        return None

    if event_type == "issue_comment" and action == "created":
        comment = payload.get("comment") or {}
        issue = payload.get("issue") or {}
        if not isinstance(comment, dict) or not isinstance(issue, dict):
            return None
        commenter = str((comment.get("user") or {}).get("login", "")) if isinstance(comment.get("user"), dict) else ""
        # Never act on our own comments — that loops.
        if bot_login and commenter == bot_login:
            return None
        return GitHubEvent(
            kind=ISSUE_COMMENT,
            number=int(issue.get("number", 0) or 0),
            title=str(issue.get("title", "")),
            body=str(comment.get("body", "") or ""),
            actor=commenter,
            repo=repo,
            raw=payload,
        )

    if event_type in ("check_run", "workflow_run") and action == "completed":
        node = payload.get(event_type) or {}
        if not isinstance(node, dict):
            return None
        if str(node.get("conclusion", "")) != "failure":
            return None
        # Both event shapes expose a head branch and (0..n) associated PRs.
        branch = str(node.get("head_branch", "") or "")
        prs = node.get("pull_requests") or []
        number = int(prs[0].get("number", 0)) if prs and isinstance(prs[0], dict) else 0
        return GitHubEvent(
            kind=CI_FAILURE,
            number=number,
            title=str(node.get("name", "")),
            branch=branch,
            repo=repo,
            raw=payload,
        )

    return None
