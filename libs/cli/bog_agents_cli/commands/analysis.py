"""Analysis, benchmarking, and observability commands."""

from __future__ import annotations

from bog_agents_cli._spec import SlashCommandSpec
from bog_agents_cli.commands._base import SlashCommand

COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand(
        spec=SlashCommandSpec(
            "/benchmark",
            "Run evaluation suites and view recent benchmark results",
            "eval score perf suite tasks trajectory",
            "analysis",
            available=True,
        ),
        handler_method="_handle_benchmark_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/harbor",
            "Harbor benchmark evaluation — run tasks, view results, check evaluation status",
            "benchmark eval terminal-bench evaluation trajectory langsmith",
            "analysis",
            available=True,
        ),
        handler_method="_handle_harbor_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/health",
            "Codebase health score and analysis",
            "quality complexity coverage",
            "analysis",
            available=True,
        ),
        handler_method="_handle_health_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/infra",
            "Generate infrastructure code (Docker/K8s/Terraform)",
            "devops deploy",
            "analysis",
            available=True,
        ),
        handler_method="_handle_infra_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/langsmith",
            "LangSmith integration — browse traces, runs, evals, datasets, feedback, and OTEL setup",
            "tracing observability langchain eval runs traces feedback otel opentelemetry langchain-hub",
            "analysis",
            available=True,
        ),
        handler_method="_handle_langsmith_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/migrate",
            "Plan technology migration",
            "upgrade convert",
            "analysis",
            available=True,
        ),
        handler_method="_handle_migrate_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/repomap",
            "Show or refresh the semantic repository map (classes, functions, imports)",
            "index codebase symbols architecture structure repo-map",
            "analysis",
            available=True,
        ),
        handler_method="_handle_repomap_command",
    ),
)
