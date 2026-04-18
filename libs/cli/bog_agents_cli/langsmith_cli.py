"""LangSmith CLI helpers — traces, projects, evals, datasets, and OTEL setup."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any


def _get_api_key() -> str | None:
    return os.environ.get("LANGCHAIN_API_KEY") or os.environ.get("LANGSMITH_API_KEY")


def _get_project() -> str:
    return os.environ.get("LANGCHAIN_PROJECT") or os.environ.get("LANGSMITH_PROJECT") or "default"


def _get_endpoint() -> str:
    return os.environ.get("LANGCHAIN_ENDPOINT") or os.environ.get("LANGSMITH_ENDPOINT") or "https://api.smith.langchain.com"


def _make_client() -> object:
    try:
        from langsmith import Client
    except ImportError as exc:
        msg = "langsmith is not installed.\nInstall it with: pip install langsmith"
        raise ImportError(
            msg
        ) from exc
    kwargs: dict[str, Any] = {}
    api_key = _get_api_key()
    if api_key:
        kwargs["api_key"] = api_key
    endpoint = os.environ.get("LANGCHAIN_ENDPOINT") or os.environ.get("LANGSMITH_ENDPOINT")
    if endpoint:
        kwargs["api_url"] = endpoint
    return Client(**kwargs)


def format_langsmith_status() -> str:
    """Show LangSmith config and connection status.

    Returns:
        Formatted status string with Rich markup.
    """
    try:
        api_key = _get_api_key()
        project = _get_project()
        endpoint = _get_endpoint()
        tracing_v2 = os.environ.get("LANGCHAIN_TRACING_V2", "").lower() == "true"

        if api_key and len(api_key) > 4:
            masked_key = f"****{api_key[-4:]}"
            key_display = f"[green]{masked_key}[/green]"
        elif api_key:
            key_display = "[green]****[/green]"
        else:
            key_display = "[red]NOT SET[/red]"

        tracing_display = "[green]enabled[/green]" if tracing_v2 else "[dim]disabled[/dim]"

        try:
            client = _make_client()
            projects = list(client.list_projects())
            conn_display = f"[green]OK[/green] ({len(projects)} projects visible)"
        except ImportError:
            conn_display = "[red]FAILED[/red] (langsmith not installed)"
        except Exception as exc:
            conn_display = f"[red]FAILED[/red] ({exc})"

        lines = [
            "[bold]LangSmith[/bold] — Observability & Evaluation Platform",
            "",
            f"  API key:     {key_display}",
            f"  Project:     [cyan]{project}[/cyan]",
            f"  Endpoint:    {endpoint}",
            f"  Tracing v2:  {tracing_display}",
            f"  Connection:  {conn_display}",
            "",
            "[dim]Commands: /langsmith projects · /langsmith runs · /langsmith evals · /langsmith datasets[/dim]",
        ]
        return "\n".join(lines)
    except Exception as exc:
        return f"[red]Error checking LangSmith status: {exc}[/red]"


def format_langsmith_projects() -> str:
    """List all LangSmith projects.

    Returns:
        Formatted project list string with Rich markup.
    """
    try:
        client = _make_client()
        projects = list(client.list_projects())

        if not projects:
            return "[bold]LangSmith Projects[/bold]\n\n  [dim]No projects found.[/dim]"

        lines = [f"[bold]LangSmith Projects[/bold] ({len(projects)} found)", ""]
        for proj in projects:
            name = getattr(proj, "name", str(proj))
            run_count = getattr(proj, "run_count", None)
            last_run = getattr(proj, "last_run_start_time", None)

            run_str = f"  {run_count:,} runs" if run_count is not None else ""
            last_str = ""
            if last_run is not None:
                try:
                    if hasattr(last_run, "strftime"):
                        last_str = f"  last: {last_run.strftime('%m-%d %H:%M')}"
                    else:
                        dt = datetime.fromisoformat(str(last_run))
                        last_str = f"  last: {dt.strftime('%m-%d %H:%M')}"
                except (ValueError, TypeError):
                    pass

            lines.append(f"  [cyan]{name}[/cyan]{run_str}{last_str}")

        return "\n".join(lines)
    except ImportError as exc:
        return f"[red]Error: {exc}[/red]"
    except Exception as exc:
        return f"[red]Error listing LangSmith projects: {exc}[/red]"


def format_langsmith_runs(
    project: str = "",
    *,
    hours: int = 24,
    limit: int = 25,
    run_type: str = "",
) -> str:
    """List recent runs in a LangSmith project.

    Args:
        project: Project name (defaults to env var or 'default').
        hours: How many hours back to look (0 = no time filter).
        limit: Maximum number of runs to return.
        run_type: Optional filter by run type (chain, llm, tool, etc.).

    Returns:
        Formatted runs list string with Rich markup.
    """
    try:
        client = _make_client()
        proj = project or _get_project()

        kwargs: dict[str, Any] = {
            "project_name": proj,
            "limit": limit,
            "is_root": True,
        }
        if hours > 0:
            kwargs["start_time"] = datetime.now(UTC) - timedelta(hours=hours)
        if run_type:
            kwargs["run_type"] = run_type

        runs = list(client.list_runs(**kwargs))

        if not runs:
            return (
                f"[bold]Recent runs[/bold] in '[cyan]{proj}[/cyan]'\n\n"
                "  [dim]No runs found.[/dim]"
            )

        lines = [f"[bold]Recent runs[/bold] in '[cyan]{proj}[/cyan]' ({len(runs)} found)", ""]
        for run in runs:
            run_id = str(getattr(run, "id", ""))
            short_id = run_id[:8] if len(run_id) >= 8 else run_id
            name = getattr(run, "name", "") or ""
            status = getattr(run, "status", "") or ""
            start_time = getattr(run, "start_time", None)
            total_tokens = getattr(run, "total_tokens", None)
            error = getattr(run, "error", None)

            if status == "success":
                status_str = f"[green]{status:<9}[/green]"
            elif status == "error":
                status_str = f"[red]{status:<9}[/red]"
            else:
                status_str = f"[yellow]{status:<9}[/yellow]"

            time_str = ""
            if start_time is not None:
                try:
                    if hasattr(start_time, "strftime"):
                        time_str = start_time.strftime("%m-%d %H:%M")
                    else:
                        dt = datetime.fromisoformat(str(start_time))
                        time_str = dt.strftime("%m-%d %H:%M")
                except (ValueError, TypeError):
                    pass

            tok_str = f"  {total_tokens:,}tok" if total_tokens is not None else ""
            err_str = "  [red]ERR[/red]" if error else ""

            lines.append(f"  {short_id}  {status_str}  {time_str}  {name}{tok_str}{err_str}")

        return "\n".join(lines)
    except ImportError as exc:
        return f"[red]Error: {exc}[/red]"
    except Exception as exc:
        return f"[red]Error listing LangSmith runs: {exc}[/red]"


def format_langsmith_run(run_id: str) -> str:
    """Show detailed information for a single run.

    Args:
        run_id: The run ID to look up.

    Returns:
        Formatted run detail string with Rich markup.
    """
    try:
        client = _make_client()
        run = client.read_run(run_id)

        rid = str(getattr(run, "id", run_id))
        name = getattr(run, "name", "") or ""
        run_type = getattr(run, "run_type", "") or ""
        status = getattr(run, "status", "") or ""
        start_time = getattr(run, "start_time", None)
        end_time = getattr(run, "end_time", None)
        total_tokens = getattr(run, "total_tokens", None)
        prompt_tokens = getattr(run, "prompt_tokens", None)
        completion_tokens = getattr(run, "completion_tokens", None)
        error = getattr(run, "error", None)
        inputs = getattr(run, "inputs", None)
        outputs = getattr(run, "outputs", None)
        child_run_ids = getattr(run, "child_run_ids", None) or []

        duration_str = ""
        if start_time is not None and end_time is not None:
            try:
                if hasattr(start_time, "timestamp") and hasattr(end_time, "timestamp"):
                    secs = (end_time - start_time).total_seconds()
                else:
                    st = datetime.fromisoformat(str(start_time))
                    et = datetime.fromisoformat(str(end_time))
                    secs = (et - st).total_seconds()
                duration_str = f"{secs:.2f}s"
            except (ValueError, TypeError, AttributeError):
                pass

        start_str = ""
        if start_time is not None:
            try:
                if hasattr(start_time, "strftime"):
                    start_str = start_time.strftime("%Y-%m-%d %H:%M:%S UTC")
                else:
                    dt = datetime.fromisoformat(str(start_time))
                    start_str = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
            except (ValueError, TypeError):
                pass

        try:
            url = client.get_run_url(run=run)
        except Exception:
            url = ""

        if status == "success":
            status_display = f"[green]{status}[/green]"
        elif status == "error":
            status_display = f"[red]{status}[/red]"
        else:
            status_display = f"[yellow]{status}[/yellow]"

        inputs_preview = ""
        if inputs:
            try:
                inputs_preview = json.dumps(inputs)[:200]
            except (TypeError, ValueError):
                inputs_preview = str(inputs)[:200]

        outputs_preview = ""
        if outputs:
            try:
                outputs_preview = json.dumps(outputs)[:200]
            except (TypeError, ValueError):
                outputs_preview = str(outputs)[:200]

        lines = [
            f"[bold]Run Detail[/bold] — [cyan]{rid}[/cyan]",
            "",
            f"  Name:        {name}",
            f"  Type:        {run_type}",
            f"  Status:      {status_display}",
            f"  Started:     {start_str}",
            f"  Duration:    {duration_str}",
        ]

        if total_tokens is not None:
            tok_line = f"  Tokens:      total={total_tokens:,}"
            if prompt_tokens is not None:
                tok_line += f"  prompt={prompt_tokens:,}"
            if completion_tokens is not None:
                tok_line += f"  completion={completion_tokens:,}"
            lines.append(tok_line)

        if error:
            lines.append(f"  Error:       [red]{error[:200]}[/red]")

        if inputs_preview:
            lines.append(f"  Inputs:      [dim]{inputs_preview}[/dim]")

        if outputs_preview:
            lines.append(f"  Outputs:     [dim]{outputs_preview}[/dim]")

        lines.append(f"  Child runs:  {len(child_run_ids)}")

        if url:
            lines.append(f"  URL:         {url}")

        return "\n".join(lines)
    except ImportError as exc:
        return f"[red]Error: {exc}[/red]"
    except Exception as exc:
        return f"[red]Error fetching run {run_id}: {exc}[/red]"


def format_langsmith_trace(run_id: str) -> str:
    """Show the trace URL for a run.

    Args:
        run_id: The run ID to look up.

    Returns:
        Formatted trace URL string with Rich markup.
    """
    try:
        client = _make_client()
        run = client.read_run(run_id)
        name = getattr(run, "name", "") or run_id
        url = client.get_run_url(run=run)
        short_id = run_id[:8] if len(run_id) >= 8 else run_id

        lines = [
            f"[bold]Trace URL[/bold] for run {short_id}",
            "",
            f"  Name: {name}",
            f"  URL:  {url}",
        ]
        return "\n".join(lines)
    except ImportError as exc:
        return f"[red]Error: {exc}[/red]"
    except Exception as exc:
        return f"[red]Error fetching trace for run {run_id}: {exc}[/red]"


def format_langsmith_datasets(limit: int = 20) -> str:
    """List LangSmith datasets.

    Args:
        limit: Maximum number of datasets to return.

    Returns:
        Formatted dataset list string with Rich markup.
    """
    try:
        client = _make_client()
        datasets = list(client.list_datasets(limit=limit))

        if not datasets:
            return "[bold]LangSmith Datasets[/bold]\n\n  [dim]No datasets found.[/dim]"

        lines = [f"[bold]LangSmith Datasets[/bold] ({len(datasets)} found)", ""]
        for ds in datasets:
            ds_id = str(getattr(ds, "id", ""))
            short_id = ds_id[:8] if len(ds_id) >= 8 else ds_id
            name = getattr(ds, "name", "") or ""
            description = getattr(ds, "description", "") or ""
            example_count = getattr(ds, "example_count", None)

            count_str = f"  {example_count} examples" if example_count is not None else ""
            desc_str = f"  — {description}" if description else ""

            lines.append(f"  {short_id}  [cyan]{name}[/cyan]{count_str}{desc_str}")

        return "\n".join(lines)
    except ImportError as exc:
        return f"[red]Error: {exc}[/red]"
    except Exception as exc:
        return f"[red]Error listing LangSmith datasets: {exc}[/red]"


def format_langsmith_dataset(name: str, *, examples_limit: int = 5) -> str:
    """Show detailed information about a LangSmith dataset.

    Args:
        name: Dataset name to look up.
        examples_limit: Number of sample examples to show.

    Returns:
        Formatted dataset detail string with Rich markup.
    """
    try:
        client = _make_client()
        dataset = client.read_dataset(dataset_name=name)

        ds_id = str(getattr(dataset, "id", ""))
        created_at = getattr(dataset, "created_at", None)
        description = getattr(dataset, "description", "") or ""
        example_count = getattr(dataset, "example_count", None)

        created_str = ""
        if created_at is not None:
            try:
                if hasattr(created_at, "strftime"):
                    created_str = created_at.strftime("%Y-%m-%d %H:%M UTC")
                else:
                    dt = datetime.fromisoformat(str(created_at))
                    created_str = dt.strftime("%Y-%m-%d %H:%M UTC")
            except (ValueError, TypeError):
                pass

        lines = [
            f"[bold]Dataset:[/bold] [cyan]{name}[/cyan]",
            "",
            f"  ID:          {ds_id}",
            f"  Created:     {created_str}",
        ]
        if example_count is not None:
            lines.append(f"  Examples:    {example_count}")
        if description:
            lines.append(f"  Description: {description}")

        examples = list(client.list_examples(dataset_name=name, limit=examples_limit))
        if examples:
            lines.append("")
            lines.append(f"  [bold]Sample examples[/bold] ({len(examples)} shown):")
            for ex in examples:
                ex_id = str(getattr(ex, "id", ""))
                short_id = ex_id[:8] if len(ex_id) >= 8 else ex_id
                inputs = getattr(ex, "inputs", {}) or {}
                try:
                    inputs_str = json.dumps(inputs)[:100]
                except (TypeError, ValueError):
                    inputs_str = str(inputs)[:100]
                lines.append(f"    [{short_id}] {inputs_str}")

        return "\n".join(lines)
    except ImportError as exc:
        return f"[red]Error: {exc}[/red]"
    except Exception as exc:
        return f"[red]Error fetching dataset '{name}': {exc}[/red]"


def format_langsmith_evals(project: str = "", *, limit: int = 20) -> str:
    """List evaluation experiments in a LangSmith project.

    Args:
        project: Project name (defaults to env var or 'default').
        limit: Maximum number of experiments to return.

    Returns:
        Formatted evals list string with Rich markup.
    """
    try:
        client = _make_client()
        proj = project or _get_project()

        experiments = list(client.list_tests(project_name=proj, limit=limit))

        if not experiments:
            return (
                f"[bold]Evaluation Experiments[/bold] in '[cyan]{proj}[/cyan]'\n\n"
                "  [dim]No experiments found.[/dim]"
            )

        lines = [
            f"[bold]Evaluation Experiments[/bold] in '[cyan]{proj}[/cyan]' ({len(experiments)} found)",
            "",
        ]
        for exp in experiments:
            exp_id = str(getattr(exp, "id", ""))
            short_id = exp_id[:8] if len(exp_id) >= 8 else exp_id
            name = getattr(exp, "name", "") or ""
            status = getattr(exp, "status", "") or ""
            result_count = getattr(exp, "result_count", None)

            if status == "completed":
                status_str = f"[green]{status:<11}[/green]"
            elif status in {"error", "failed"}:
                status_str = f"[red]{status:<11}[/red]"
            else:
                status_str = f"[yellow]{status:<11}[/yellow]"

            count_str = f"  {result_count} results" if result_count is not None else ""

            lines.append(f"  {short_id}  {status_str}  [cyan]{name}[/cyan]{count_str}")

        return "\n".join(lines)
    except ImportError as exc:
        return f"[red]Error: {exc}[/red]"
    except Exception as exc:
        return f"[red]Error listing LangSmith evals: {exc}[/red]"


def format_langsmith_eval(eval_name: str) -> str:
    """Show detailed information for a single evaluation experiment.

    Args:
        eval_name: Experiment name to look up.

    Returns:
        Formatted eval detail string with Rich markup.
    """
    try:
        client = _make_client()
        experiment = client.read_test(eval_name)

        exp_id = str(getattr(experiment, "id", ""))
        status = getattr(experiment, "status", "") or ""
        result_count = getattr(experiment, "result_count", None)
        feedback_stats = getattr(experiment, "feedback_stats", None) or {}

        if status == "completed":
            status_display = f"[green]{status}[/green]"
        elif status in {"error", "failed"}:
            status_display = f"[red]{status}[/red]"
        else:
            status_display = f"[yellow]{status}[/yellow]"

        lines = [
            f"[bold]Evaluation:[/bold] [cyan]{eval_name}[/cyan]",
            "",
            f"  ID:      {exp_id}",
            f"  Status:  {status_display}",
        ]
        if result_count is not None:
            lines.append(f"  Results: {result_count}")

        if feedback_stats:
            lines.append("")
            lines.append("  Metrics:")
            for metric_name, stats in feedback_stats.items():
                if isinstance(stats, dict):
                    avg = stats.get("avg", stats.get("mean", None))
                    n = stats.get("n", stats.get("count", None))
                    avg_str = f"avg={avg:.4f}" if avg is not None else ""
                    n_str = f"  n={n}" if n is not None else ""
                    lines.append(f"    {metric_name:<28}{avg_str}{n_str}")
                else:
                    lines.append(f"    {metric_name:<28}{stats}")

        return "\n".join(lines)
    except ImportError as exc:
        return f"[red]Error: {exc}[/red]"
    except Exception as exc:
        return f"[red]Error fetching eval '{eval_name}': {exc}[/red]"


def format_langsmith_eval_compare(eval_a: str, eval_b: str) -> str:
    """Compare two evaluation experiments side by side.

    Args:
        eval_a: First experiment name (baseline).
        eval_b: Second experiment name (comparison).

    Returns:
        Formatted comparison table string with Rich markup.
    """
    try:
        client = _make_client()
        exp_a = client.read_test(eval_a)
        exp_b = client.read_test(eval_b)

        stats_a: dict[str, Any] = getattr(exp_a, "feedback_stats", None) or {}
        stats_b: dict[str, Any] = getattr(exp_b, "feedback_stats", None) or {}

        all_metrics = sorted(set(list(stats_a.keys()) + list(stats_b.keys())))

        lines = [
            "[bold]Evaluation Comparison[/bold]",
            "",
            f"  A: [cyan]{eval_a}[/cyan]",
            f"  B: [cyan]{eval_b}[/cyan]",
            "",
            f"  {'Metric':<26}  {'A':<14} {'B':<14} {'Δ':<10} Winner",
            "  " + "\u2500" * 69,
        ]

        for metric in all_metrics:
            a_stats = stats_a.get(metric, {}) if isinstance(stats_a.get(metric), dict) else {}
            b_stats = stats_b.get(metric, {}) if isinstance(stats_b.get(metric), dict) else {}

            a_avg = a_stats.get("avg", a_stats.get("mean", None))
            b_avg = b_stats.get("avg", b_stats.get("mean", None))

            a_str = f"{a_avg:.4f}" if a_avg is not None else "N/A"
            b_str = f"{b_avg:.4f}" if b_avg is not None else "N/A"

            if a_avg is not None and b_avg is not None:
                delta = b_avg - a_avg
                delta_str = f"{delta:+.4f}"
                if delta > 0:
                    winner = "[cyan]B \u25b2[/cyan]"
                elif delta < 0:
                    winner = "[green]A \u25b2[/green]"
                else:
                    winner = "[dim]tie[/dim]"
            else:
                delta_str = "N/A"
                winner = "[dim]N/A[/dim]"

            lines.append(
                f"  {metric:<26}  {a_str:<14} {b_str:<14} {delta_str:<10} {winner}"
            )

        return "\n".join(lines)
    except ImportError as exc:
        return f"[red]Error: {exc}[/red]"
    except Exception as exc:
        return f"[red]Error comparing evals '{eval_a}' vs '{eval_b}': {exc}[/red]"


def format_langsmith_feedback(run_id: str) -> str:
    """Show feedback items for a run.

    Args:
        run_id: The run ID to look up feedback for.

    Returns:
        Formatted feedback string with Rich markup.
    """
    try:
        client = _make_client()
        feedback_items = list(client.list_feedback(run_ids=[run_id]))

        short_id = run_id[:8] if len(run_id) >= 8 else run_id

        if not feedback_items:
            return (
                f"[bold]Feedback for run {short_id}[/bold]\n\n"
                "  [dim]No feedback found.[/dim]"
            )

        lines = [f"[bold]Feedback for run {short_id}[/bold] ({len(feedback_items)} items)", ""]
        for fb in feedback_items:
            key = getattr(fb, "key", "") or ""
            score = getattr(fb, "score", None)
            comment = getattr(fb, "comment", None) or ""

            score_str = f"score={score}" if score is not None else ""
            comment_str = f'  "{comment}"' if comment else ""

            lines.append(f"  {key:<20} {score_str}{comment_str}")

        return "\n".join(lines)
    except ImportError as exc:
        return f"[red]Error: {exc}[/red]"
    except Exception as exc:
        return f"[red]Error fetching feedback for run {run_id}: {exc}[/red]"


def format_langsmith_otel_setup() -> str:
    """Return OTEL tracing setup instructions for LangSmith.

    Returns:
        Formatted setup instructions string with Rich markup.
    """
    return """\
[bold]LangSmith OTEL Tracing Setup[/bold]

[bold]1. Install dependencies[/bold]

  [cyan]pip install langsmith opentelemetry-sdk opentelemetry-api[/cyan]

[bold]2. Set environment variables[/bold]

  [cyan]export LANGCHAIN_API_KEY="ls__..."[/cyan]
  [cyan]export LANGCHAIN_PROJECT="my-project"[/cyan]
  [cyan]export LANGCHAIN_TRACING_V2="true"[/cyan]

[bold]3. Enable via bog-agents middleware (recommended)[/bold]

  [dim]# In your agent setup code:[/dim]
  [cyan]from bog_agents import create_agent
from bog_agents.middleware.langsmith import LangSmithMiddleware

agent = create_agent(
    middleware=[
        LangSmithMiddleware(enable_otel=True),
    ]
)[/cyan]

[bold]4. Alternative: raw OTEL setup[/bold]

  [dim]# Manual OpenTelemetry configuration:[/dim]
  [cyan]from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from langsmith.wrappers import wrap_openai  # if using OpenAI

provider = TracerProvider()
# Add LangSmith exporter
trace.set_tracer_provider(provider)
tracer = trace.get_tracer(__name__)[/cyan]

[dim]Run /langsmith status to verify the connection after setup.[/dim]"""
