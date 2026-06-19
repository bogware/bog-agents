"""Standing Orders — curated catalog of ambient-agent job templates.

Each entry is a ready-to-install daemon job spec. The catalog is the
"productized" face of the daemon: instead of asking the user to write
``triggers``/``outputs``/``prompt`` from scratch, ``/standing-orders``
shows a small curated list of the highest-leverage ambient agents and
installs the chosen template via the daemon REST API.

Schema mirrors the JSON the daemon ``POST /jobs`` endpoint expects, with
``id``, ``title``, ``summary``, and ``tags`` for the menu rendering.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class StandingOrder:
    """One curated daemon-job template."""

    id: str
    title: str
    summary: str
    tags: tuple[str, ...]
    job: dict[str, Any]
    notes: str = ""

    def to_create_payload(self) -> dict[str, Any]:
        """Return the body to POST to the daemon's ``/jobs`` endpoint."""
        # Make a defensive copy so the caller can mutate without
        # corrupting the catalog singleton.
        return {k: _deepcopy_jsonish(v) for k, v in self.job.items()}


def _deepcopy_jsonish(value: Any) -> Any:  # noqa: ANN401
    """Cheap deep copy that preserves dict/list/scalar JSON shapes."""
    if isinstance(value, dict):
        return {k: _deepcopy_jsonish(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_deepcopy_jsonish(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Curated catalog
# ---------------------------------------------------------------------------

CATALOG: tuple[StandingOrder, ...] = (
    StandingOrder(
        id="bug-finder",
        title="Bug Finder",
        summary=(
            "On every push, run static analysis + an agent triage pass; "
            "post a structured report to log."
        ),
        tags=("quality", "git-push", "static-analysis"),
        job={
            "name": "bug-finder",
            "description": (
                "Static-analysis sweep on every push. Routes findings through "
                "an agent that triages severity and suggests fixes."
            ),
            "prompt": (
                "Run repository static analysis and synthesize a triaged bug "
                "report. Steps:\n"
                "1. Run available linters (ruff, eslint, tsc --noEmit, mypy "
                "if configured).\n"
                "2. For each high-severity finding, attempt to identify the "
                "root cause and propose a minimal fix.\n"
                "3. Group findings by file and severity (critical / high / "
                "medium / low).\n"
                "4. Output a markdown report with a triage table and "
                "next-step recommendations.\n"
                "Do NOT modify files — this is a read-only audit job."
            ),
            "triggers": [{"type": "git_push", "git_branch_pattern": "*"}],
            "outputs": [{"target": "log"}],
            "enabled": True,
        },
        notes=(
            "Pair with `bog-agents daemon install-git-hook --repo .` so "
            "every `git push` to this repo triggers the audit."
        ),
    ),
    StandingOrder(
        id="pr-summarizer",
        title="PR Summarizer",
        summary="Hourly: summarize open PRs awaiting review and post to Slack.",
        tags=("productivity", "github", "slack"),
        job={
            "name": "pr-summarizer",
            "description": (
                "Summarize open PRs awaiting review. Posts a digest to Slack "
                "every hour during business hours."
            ),
            "prompt": (
                "List open PRs in the current repo that are awaiting review. "
                "For each, output: title, author, age, files-changed, and a "
                "one-line summary. Group by reviewer when assignees exist."
            ),
            "triggers": [{"type": "cron", "cron": "0 9-17 * * 1-5"}],
            "outputs": [{"target": "slack"}],
            "enabled": True,
        },
        notes="Configure `slack_webhook_url` on the output before installing.",
    ),
    StandingOrder(
        id="issue-triager",
        title="Issue Triager",
        summary="Every 30 min: triage new GitHub issues, label and reply.",
        tags=("productivity", "github", "support"),
        job={
            "name": "issue-triager",
            "description": (
                "Triage newly opened GitHub issues — apply labels, ask "
                "clarifying questions, surface duplicates."
            ),
            "prompt": (
                "Find newly opened issues without bog-agents triage. For each:\n"
                "1. Identify the issue category (bug / feature / question / "
                "duplicate).\n"
                "2. Suggest labels (use existing repo labels only).\n"
                "3. Draft a friendly first-response message that asks for any "
                "missing reproduction details.\n"
                "Output a markdown table the human can review before applying."
            ),
            "triggers": [{"type": "interval", "interval_seconds": 1800}],
            "outputs": [{"target": "log"}],
            "enabled": True,
        },
    ),
    StandingOrder(
        id="dependency-watcher",
        title="Dependency Watcher",
        summary="Daily: scan for outdated/insecure dependencies and propose upgrades.",
        tags=("security", "dependencies", "scheduled"),
        job={
            "name": "dependency-watcher",
            "description": (
                "Daily dependency audit — checks for known CVEs and outdated "
                "majors, proposes upgrade order."
            ),
            "prompt": (
                "Audit the project's dependencies. Steps:\n"
                "1. Run language-specific audit tools (npm audit, pip-audit, "
                "cargo audit, etc.) where available.\n"
                "2. Check pyproject.toml / package.json for pins more than two "
                "minors behind upstream.\n"
                "3. Group findings by severity. For CVEs, include CVSS score "
                "and the version that fixes them.\n"
                "Do NOT mutate lockfiles — this is read-only reporting."
            ),
            "triggers": [{"type": "cron", "cron": "0 6 * * *"}],
            "outputs": [
                {"target": "file", "file_path": "~/bog-deps-report.md", "append": True}
            ],
            "enabled": True,
        },
    ),
    StandingOrder(
        id="weekly-summary",
        title="Weekly Repository Summary",
        summary="Mondays 9am: summarize last week's commits, PRs, and incidents.",
        tags=("reporting", "scheduled"),
        job={
            "name": "weekly-summary",
            "description": (
                "Weekly digest — last 7 days of commits, merged PRs, closed "
                "issues, plus a what-shipped paragraph."
            ),
            "prompt": (
                "Summarize repository activity from the last 7 days. Include:\n"
                "- merged PRs with one-line summaries\n"
                "- closed issues grouped by label\n"
                "- newly introduced TODO/FIXME comments\n"
                "- a short 'what shipped' paragraph in plain English\n"
                "Format as markdown suitable for a Monday morning standup."
            ),
            "triggers": [{"type": "cron", "cron": "0 9 * * 1"}],
            "outputs": [{"target": "log"}],
            "enabled": True,
        },
    ),
    StandingOrder(
        id="failed-test-detective",
        title="Failed-Test Detective",
        summary="On webhook from CI: investigate the failing test and propose a fix.",
        tags=("ci", "webhook", "debug"),
        job={
            "name": "failed-test-detective",
            "description": (
                "Webhook target for CI: receives a failing test name + log "
                "and produces a root-cause analysis with a candidate fix."
            ),
            "prompt": (
                "A CI run failed. Read the trigger context for the failing test "
                "name and stack trace. Then:\n"
                "1. Locate the test file and the system-under-test.\n"
                "2. Reproduce the failure mentally; identify the root cause.\n"
                "3. Propose the minimal patch.\n"
                "4. Output: { test, root_cause, proposed_fix, confidence: "
                "low|med|high }."
            ),
            "triggers": [
                {"type": "webhook", "webhook_path": "/hooks/ci-failure"},
            ],
            "outputs": [{"target": "log"}],
            "enabled": True,
        },
        notes="Set ``webhook_secret`` on the trigger before exposing the daemon.",
    ),
)


def list_orders(*, tag: str | None = None) -> list[StandingOrder]:
    """Return the catalog, optionally filtered by tag."""
    if tag is None:
        return list(CATALOG)
    needle = tag.lower()
    return [o for o in CATALOG if needle in (t.lower() for t in o.tags)]


def get_order(order_id: str) -> StandingOrder | None:
    """Look up one Standing Order by id (case-insensitive)."""
    needle = order_id.strip().lower()
    for order in CATALOG:
        if order.id.lower() == needle:
            return order
    return None


__all__ = [
    "CATALOG",
    "StandingOrder",
    "get_order",
    "list_orders",
]
