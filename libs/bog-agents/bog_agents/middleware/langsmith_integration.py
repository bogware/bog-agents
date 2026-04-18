"""LangSmith integration middleware — traces, runs, datasets, evaluations.

Provides the agent with tools to query LangSmith for traces, evaluation
results, and feedback. Optionally sets up OTEL span export.

Env vars:
    LANGCHAIN_API_KEY or LANGSMITH_API_KEY: LangSmith API key.
    LANGCHAIN_PROJECT or LANGSMITH_PROJECT: Default project name.
    LANGCHAIN_ENDPOINT or LANGSMITH_ENDPOINT: API endpoint.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from langchain.agents.middleware.types import AgentMiddleware, ContextT, ResponseT
from langchain_core.tools import BaseTool, StructuredTool
from typing_extensions import TypedDict

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------


def _get_api_key() -> str | None:
    """Return the LangSmith API key from environment variables.

    Returns:
        The API key string, or ``None`` if not set.
    """
    return os.environ.get("LANGCHAIN_API_KEY") or os.environ.get("LANGSMITH_API_KEY")


def _get_project() -> str:
    """Return the active LangSmith project name from environment variables.

    Returns:
        Project name, falling back to ``"default"`` if not set.
    """
    return os.environ.get("LANGCHAIN_PROJECT") or os.environ.get("LANGSMITH_PROJECT") or "default"


# Module-level client cache: keyed by (api_key, endpoint) tuple.
# Avoids creating a new Client on every tool invocation.
_CLIENT_CACHE: dict[tuple[str | None, str | None], Any] = {}


def _make_client() -> Any:
    """Create a LangSmith client from environment variables.

    Returns:
        A configured `langsmith.Client` instance.

    Raises:
        ImportError: If the ``langsmith`` package is not installed.
    """
    try:
        from langsmith import Client
    except ImportError as exc:
        msg = "langsmith is not installed. Run: pip install langsmith"
        raise ImportError(msg) from exc
    api_key = _get_api_key()
    endpoint = os.environ.get("LANGCHAIN_ENDPOINT") or os.environ.get("LANGSMITH_ENDPOINT")
    cache_key = (api_key, endpoint)
    if cache_key not in _CLIENT_CACHE:
        kwargs: dict[str, Any] = {}
        if api_key:
            kwargs["api_key"] = api_key
        if endpoint:
            kwargs["api_url"] = endpoint
        _CLIENT_CACHE[cache_key] = Client(**kwargs)
    return _CLIENT_CACHE[cache_key]


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class _State(TypedDict, total=False):
    langsmith_project: str


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class LangSmithMiddleware(AgentMiddleware[_State, ContextT, ResponseT]):
    """Middleware that exposes LangSmith tracing, datasets, and evaluation tools.

    The agent receives 11 tools covering run inspection, dataset browsing,
    evaluation comparison, feedback submission, and connection diagnostics.

    Args:
        project: Default LangSmith project name. Falls back to the
            ``LANGCHAIN_PROJECT`` / ``LANGSMITH_PROJECT`` env var, then
            ``"default"``.
        enable_otel: When ``True``, configure an OTEL trace provider that
            exports spans to LangSmith via ``LangSmithOTELSpanExporter``.
        otel_batch_timeout_ms: Batch export timeout in milliseconds passed to
            ``BatchSpanProcessor``.
    """

    state_schema = _State

    def __init__(
        self,
        *,
        project: str | None = None,
        enable_otel: bool = False,
        otel_batch_timeout_ms: int = 5000,
    ) -> None:
        self._project = project or _get_project()
        self._otel_enabled = False
        if enable_otel:
            self._setup_otel(batch_timeout_ms=otel_batch_timeout_ms)
        self._tools = self._build_tools()

    def _setup_otel(self, *, batch_timeout_ms: int) -> None:
        """Configure an OTEL trace provider that exports to LangSmith.

        Args:
            batch_timeout_ms: Batch export timeout in milliseconds.
        """
        try:
            from langsmith.otel import LangSmithOTELSpanExporter
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
        except ImportError:
            logger.warning("opentelemetry or langsmith.otel not available; OTEL export skipped")
            return
        try:
            exporter = LangSmithOTELSpanExporter()
            provider = TracerProvider()
            provider.add_span_processor(BatchSpanProcessor(exporter, export_timeout_millis=batch_timeout_ms))
            from opentelemetry import trace as otel_trace
            otel_trace.set_tracer_provider(provider)
            self._otel_enabled = True
        except Exception:
            logger.warning("Failed to configure OTEL export to LangSmith", exc_info=True)

    @property
    def tools(self) -> list[BaseTool]:
        """Expose LangSmith tools to the agent."""
        return self._tools

    def _build_tools(self) -> list[BaseTool]:
        """Construct all 11 LangSmith tools.

        Returns:
            List of `BaseTool` instances.
        """
        middleware = self

        # ------------------------------------------------------------------
        # 1. langsmith_list_runs
        # ------------------------------------------------------------------

        def list_runs(
            project: Annotated[str, "Project name (empty = middleware default)"] = "",
            limit: Annotated[int, "Max runs (1-100)"] = 20,
            run_type: Annotated[str, "Filter by type: chain/llm/tool — empty for all"] = "",
            hours: Annotated[int, "Restrict to last N hours (0 = no filter)"] = 24,
        ) -> str:
            """List recent LangSmith runs for a project."""
            try:
                client = _make_client()
            except ImportError as exc:
                return str(exc)
            proj = project or middleware._project
            kwargs: dict[str, Any] = {"project_name": proj, "limit": limit, "is_root": True}
            if hours > 0:
                kwargs["start_time"] = datetime.now(UTC) - timedelta(hours=hours)
            if run_type:
                kwargs["run_type"] = run_type
            try:
                runs = list(client.list_runs(**kwargs))
            except Exception as exc:
                return f"Error listing runs: {exc}"
            if not runs:
                return f"No runs found in project '{proj}'."
            lines = [f"Runs in '{proj}' (newest first):"]
            for run in runs:
                run_id = str(getattr(run, "id", "?"))
                status = getattr(run, "status", "unknown") or "unknown"
                start = getattr(run, "start_time", None)
                start_str = start.strftime("%Y-%m-%d %H:%M") if start else "          "
                name = str(getattr(run, "name", "") or "")[:40]
                total_tokens = getattr(run, "total_tokens", None)
                tok_str = f"  {total_tokens:,} tok" if total_tokens else ""
                lines.append(f"  {run_id[:8]}  [{status:^9}]  {start_str}  {name}{tok_str}")
            return "\n".join(lines)

        # ------------------------------------------------------------------
        # 2. langsmith_read_run
        # ------------------------------------------------------------------

        def read_run(
            run_id: Annotated[str, "LangSmith run UUID"],
            include_inputs: Annotated[bool, "Include input/output data"] = False,
        ) -> str:
            """Return detailed information about a single LangSmith run."""
            try:
                client = _make_client()
            except ImportError as exc:
                return str(exc)
            try:
                run = client.read_run(run_id)
            except Exception as exc:
                return f"Error reading run '{run_id}': {exc}"
            url = ""
            try:
                url = client.get_run_url(run=run)
            except Exception:
                pass
            start = getattr(run, "start_time", None)
            end = getattr(run, "end_time", None)
            duration = ""
            if start and end:
                secs = (end - start).total_seconds()
                duration = f"{secs:.1f}s"
            elif start:
                duration = "in progress"
            total_tokens = getattr(run, "total_tokens", None)
            tok_str = f"{total_tokens:,}" if total_tokens else "n/a"
            error = getattr(run, "error", None)
            lines = [
                f"Name:     {getattr(run, 'name', '')}",
                f"Type:     {getattr(run, 'run_type', '')}",
                f"Status:   {getattr(run, 'status', '')}",
                f"Started:  {start.strftime('%Y-%m-%d %H:%M:%S') if start else 'n/a'}",
                f"Duration: {duration or 'n/a'}",
                f"Tokens:   {tok_str}",
                f"Error:    {error or 'none'}",
                f"URL:      {url or 'n/a'}",
            ]
            if include_inputs:
                inputs = getattr(run, "inputs", None)
                outputs = getattr(run, "outputs", None)
                if inputs:
                    lines.append(f"Inputs:   {json.dumps(inputs)[:400]}")
                if outputs:
                    lines.append(f"Outputs:  {json.dumps(outputs)[:400]}")
            return "\n".join(lines)

        # ------------------------------------------------------------------
        # 3. langsmith_get_run_url
        # ------------------------------------------------------------------

        def get_run_url(
            run_id: Annotated[str, "LangSmith run UUID"],
        ) -> str:
            """Return the LangSmith UI URL for a run."""
            try:
                client = _make_client()
            except ImportError as exc:
                return str(exc)
            try:
                run = client.read_run(run_id)
                return client.get_run_url(run=run)
            except Exception as exc:
                return f"Error fetching URL for run '{run_id}': {exc}"

        # ------------------------------------------------------------------
        # 4. langsmith_list_projects
        # ------------------------------------------------------------------

        def list_projects() -> str:
            """List all accessible LangSmith projects."""
            try:
                client = _make_client()
            except ImportError as exc:
                return str(exc)
            try:
                projects = list(client.list_projects())
            except Exception as exc:
                return f"Error listing projects: {exc}"
            if not projects:
                return "No projects found."
            lines = ["Projects:"]
            for proj in projects:
                name = str(getattr(proj, "name", "") or "")
                run_count = getattr(proj, "run_count", None)
                count_str = f"{run_count:,} runs" if run_count is not None else ""
                lines.append(f"  {name}  {count_str}".rstrip())
            return "\n".join(lines)

        # ------------------------------------------------------------------
        # 5. langsmith_list_datasets
        # ------------------------------------------------------------------

        def list_datasets(
            limit: Annotated[int, "Max datasets to return"] = 20,
        ) -> str:
            """List LangSmith datasets."""
            try:
                client = _make_client()
            except ImportError as exc:
                return str(exc)
            try:
                datasets = list(client.list_datasets(limit=limit))
            except Exception as exc:
                return f"Error listing datasets: {exc}"
            if not datasets:
                return "No datasets found."
            lines = ["Datasets:"]
            for ds in datasets:
                ds_id = str(getattr(ds, "id", "") or "")
                name = str(getattr(ds, "name", "") or "")
                example_count = getattr(ds, "example_count", None)
                count_str = f"{example_count} examples" if example_count is not None else ""
                lines.append(f"  {ds_id[:8]}  {name}  {count_str}".rstrip())
            return "\n".join(lines)

        # ------------------------------------------------------------------
        # 6. langsmith_read_dataset
        # ------------------------------------------------------------------

        def read_dataset(
            dataset_name: Annotated[str, "Dataset name or UUID"],
            examples_limit: Annotated[int, "Max examples to show (0 = metadata only)"] = 5,
        ) -> str:
            """Return metadata and optional examples for a LangSmith dataset."""
            try:
                client = _make_client()
            except ImportError as exc:
                return str(exc)
            try:
                ds = client.read_dataset(dataset_name=dataset_name)
            except Exception as exc:
                return f"Error reading dataset '{dataset_name}': {exc}"
            ds_id = str(getattr(ds, "id", "") or "")
            name = str(getattr(ds, "name", "") or "")
            description = str(getattr(ds, "description", "") or "")
            example_count = getattr(ds, "example_count", None)
            lines = [
                f"Dataset:     {name}",
                f"ID:          {ds_id}",
                f"Description: {description or 'n/a'}",
                f"Examples:    {example_count if example_count is not None else 'unknown'}",
            ]
            if examples_limit > 0:
                try:
                    examples = list(client.list_examples(dataset_name=dataset_name, limit=examples_limit))
                    if examples:
                        lines.append(f"\nFirst {len(examples)} example(s):")
                        for ex in examples:
                            ex_id = str(getattr(ex, "id", "") or "")
                            inputs = getattr(ex, "inputs", {}) or {}
                            lines.append(f"  [{ex_id[:8]}] inputs={json.dumps(inputs)[:80]}")
                except Exception as exc:
                    lines.append(f"(Could not load examples: {exc})")
            return "\n".join(lines)

        # ------------------------------------------------------------------
        # 7. langsmith_list_evals
        # ------------------------------------------------------------------

        def list_evals(
            project: Annotated[str, "Project name (empty = middleware default)"] = "",
            limit: Annotated[int, "Max evals to return"] = 20,
        ) -> str:
            """List LangSmith evaluation experiments for a project."""
            try:
                client = _make_client()
            except ImportError as exc:
                return str(exc)
            proj = project or middleware._project
            try:
                evals = list(client.list_tests(project_name=proj, limit=limit))
            except Exception as exc:
                return f"Error listing evals in '{proj}': {exc}"
            if not evals:
                return f"No evals found in project '{proj}'."
            lines = [f"Evals in '{proj}':"]
            for ev in evals:
                ev_id = str(getattr(ev, "id", "") or "")
                status = str(getattr(ev, "status", "unknown") or "unknown")
                name = str(getattr(ev, "name", "") or "")
                result_count = getattr(ev, "result_count", None)
                count_str = f"{result_count} results" if result_count is not None else ""
                lines.append(f"  {ev_id[:8]}  [{status:^9}]  {name}  {count_str}".rstrip())
            return "\n".join(lines)

        # ------------------------------------------------------------------
        # 8. langsmith_read_eval
        # ------------------------------------------------------------------

        def read_eval(
            eval_name: Annotated[str, "Eval experiment name or UUID"],
        ) -> str:
            """Return detailed results for a LangSmith evaluation experiment."""
            try:
                client = _make_client()
            except ImportError as exc:
                return str(exc)
            try:
                ev = client.read_test(eval_name)
            except Exception as exc:
                return f"Error reading eval '{eval_name}': {exc}"
            ev_id = str(getattr(ev, "id", "") or "")
            name = str(getattr(ev, "name", "") or "")
            status = str(getattr(ev, "status", "") or "")
            result_count = getattr(ev, "result_count", None)
            lines = [
                f"Eval:    {name}",
                f"ID:      {ev_id}",
                f"Status:  {status}",
                f"Results: {result_count if result_count is not None else 'unknown'}",
            ]
            feedback_stats = getattr(ev, "feedback_stats", None) or {}
            if feedback_stats:
                lines.append("\nFeedback stats:")
                for key, stats in feedback_stats.items():
                    if isinstance(stats, dict):
                        avg = stats.get("avg")
                        n = stats.get("n")
                        avg_str = f"{avg:.3f}" if avg is not None else "n/a"
                        n_str = str(n) if n is not None else "n/a"
                        lines.append(f"  {key}: avg={avg_str}  n={n_str}")
                    else:
                        lines.append(f"  {key}: {stats}")
            return "\n".join(lines)

        # ------------------------------------------------------------------
        # 9. langsmith_compare_evals
        # ------------------------------------------------------------------

        def compare_evals(
            eval_a: Annotated[str, "First eval experiment name or UUID"],
            eval_b: Annotated[str, "Second eval experiment name or UUID"],
        ) -> str:
            """Compare two LangSmith evaluation experiments side by side."""
            try:
                client = _make_client()
            except ImportError as exc:
                return str(exc)
            try:
                a = client.read_test(eval_a)
                b = client.read_test(eval_b)
            except Exception as exc:
                return f"Error reading evals: {exc}"
            name_a = str(getattr(a, "name", eval_a) or eval_a)
            name_b = str(getattr(b, "name", eval_b) or eval_b)
            stats_a: dict[str, Any] = getattr(a, "feedback_stats", None) or {}
            stats_b: dict[str, Any] = getattr(b, "feedback_stats", None) or {}
            all_keys = sorted(set(stats_a) | set(stats_b))
            col = max(len(name_a), len(name_b), 20)
            header = f"{'Metric':<30}  {name_a:<{col}}  {name_b:<{col}}  {'Delta':>10}  Winner"
            lines = [header, "-" * len(header)]
            for key in all_keys:
                sa = stats_a.get(key, {})
                sb = stats_b.get(key, {})
                avg_a = sa.get("avg") if isinstance(sa, dict) else None
                avg_b = sb.get("avg") if isinstance(sb, dict) else None
                str_a = f"{avg_a:.3f}" if avg_a is not None else "n/a"
                str_b = f"{avg_b:.3f}" if avg_b is not None else "n/a"
                if avg_a is not None and avg_b is not None:
                    delta = avg_b - avg_a
                    delta_str = f"{delta:+.3f}"
                    if abs(delta) < 1e-6:
                        winner = "tie"
                    elif avg_b > avg_a:
                        winner = "B"
                    else:
                        winner = "A"
                else:
                    delta_str = "n/a"
                    winner = "n/a"
                lines.append(f"  {key:<28}  {str_a:<{col}}  {str_b:<{col}}  {delta_str:>10}  {winner}")
            if not all_keys:
                lines.append("  (no feedback stats available for either eval)")
            return "\n".join(lines)

        # ------------------------------------------------------------------
        # 10. langsmith_log_feedback
        # ------------------------------------------------------------------

        def log_feedback(
            run_id: Annotated[str, "LangSmith run UUID"],
            key: Annotated[str, "Feedback key (e.g. 'correctness')"],
            score: Annotated[float, "Numeric score in the range 0.0-1.0"],
            comment: Annotated[str, "Optional explanatory comment"] = "",
        ) -> str:
            """Submit feedback for a LangSmith run."""
            try:
                client = _make_client()
            except ImportError as exc:
                return str(exc)
            try:
                kwargs: dict[str, Any] = {"run_id": run_id, "key": key, "score": score}
                if comment:
                    kwargs["comment"] = comment
                client.create_feedback(**kwargs)
                return f"Feedback recorded — run={run_id[:8]}  key='{key}'  score={score}"
            except Exception as exc:
                return f"Error logging feedback: {exc}"

        # ------------------------------------------------------------------
        # 11. langsmith_status
        # ------------------------------------------------------------------

        def get_langsmith_status() -> str:
            """Show LangSmith configuration and test the API connection."""
            api_key = _get_api_key()
            if api_key:
                masked = api_key[:4] + "****" + api_key[-4:] if len(api_key) >= 8 else "****"
            else:
                masked = "(not set)"
            endpoint = os.environ.get("LANGCHAIN_ENDPOINT") or os.environ.get("LANGSMITH_ENDPOINT") or "https://api.smith.langchain.com"
            lines = [
                f"API key:  {masked}",
                f"Project:  {middleware._project}",
                f"Endpoint: {endpoint}",
                f"OTEL:     {'enabled' if middleware._otel_enabled else 'disabled'}",
            ]
            try:
                client = _make_client()
                list(client.list_projects())
                lines.append("Connection: OK")
            except ImportError as exc:
                lines.append(f"Connection: ERROR — {exc}")
            except Exception as exc:
                lines.append(f"Connection: ERROR — {exc}")
            return "\n".join(lines)

        # ------------------------------------------------------------------
        # Return all tools
        # ------------------------------------------------------------------

        return [
            StructuredTool.from_function(list_runs, name="langsmith_list_runs"),
            StructuredTool.from_function(read_run, name="langsmith_read_run"),
            StructuredTool.from_function(get_run_url, name="langsmith_get_run_url"),
            StructuredTool.from_function(list_projects, name="langsmith_list_projects"),
            StructuredTool.from_function(list_datasets, name="langsmith_list_datasets"),
            StructuredTool.from_function(read_dataset, name="langsmith_read_dataset"),
            StructuredTool.from_function(list_evals, name="langsmith_list_evals"),
            StructuredTool.from_function(read_eval, name="langsmith_read_eval"),
            StructuredTool.from_function(compare_evals, name="langsmith_compare_evals"),
            StructuredTool.from_function(log_feedback, name="langsmith_log_feedback"),
            StructuredTool.from_function(get_langsmith_status, name="langsmith_status"),
        ]
