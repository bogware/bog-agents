"""Code quality and review commands."""

from __future__ import annotations

from bog_agents_cli._spec import SlashCommandSpec
from bog_agents_cli.commands._base import SlashCommand

COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand(
        spec=SlashCommandSpec(
            "/audit",
            "Audit dependencies for vulnerabilities",
            "security deps",
            "quality",
            available=True,
        ),
        handler_method="_handle_audit_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/recommend",
            "Run AI-powered code review and recommendation flows",
            "review audit advise persona focus",
            "quality",
            available=True,
        ),
        handler_method="_dispatch_recommend_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/review",
            "Ask the agent for a structured code review",
            "lint check staged files commit",
            "quality",
            available=True,
        ),
        handler_method="_handle_review_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/test",
            "Run tests with coverage and generate test skeletons",
            "coverage pytest",
            "quality",
            available=True,
        ),
        handler_method="_handle_test_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/jury",
            "Run the current diff past N jurors (different models) and aggregate verdicts",
            "review vote panel jurors multi-model judges consensus",
            "quality",
            available=True,
        ),
        handler_method="_handle_jury_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/qa",
            "Author and run acceptance-criteria QA plans against a deployed product",
            "test acceptance criteria ac jira plan deploy verify",
            "quality",
            available=True,
        ),
        handler_method="_handle_qa_command",
    ),
)
