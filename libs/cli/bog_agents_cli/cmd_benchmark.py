"""Run Harbor evaluation benchmarks from the CLI.

Provides helpers for listing, running, and viewing results of benchmark suites
defined as YAML files in ``~/.bog-agents/benchmarks/``.  Harbor trajectory
files are read from ``~/.bog-agents/trajectories/``.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_BENCHMARKS_DIR: Path = Path.home() / ".bog-agents" / "benchmarks"
_TRAJECTORIES_DIR: Path = Path.home() / ".bog-agents" / "trajectories"


def _load_yaml(path: Path) -> dict:
    """Load a YAML file into a dict.

    Args:
        path: Path to the YAML file.

    Returns:
        Parsed dict, or empty dict on error.
    """
    try:
        import yaml
    except ImportError:
        logger.warning("PyYAML not available; cannot parse benchmark YAML at %s", path)
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.warning("Failed to parse YAML %s: %s", path, exc)
        return {}


def _harbor_built_in_suites() -> list[Path]:
    """Return paths to built-in Harbor benchmark YAML files, if any.

    Returns:
        List of YAML paths from the Harbor package data directory.
    """
    try:
        import bog_agents_harbor

        pkg_path = Path(bog_agents_harbor.__file__).parent / "benchmarks"
        if pkg_path.is_dir():
            return list(pkg_path.glob("*.yaml")) + list(pkg_path.glob("*.yml"))
    except Exception:
        logger.debug("Harbor built-in suites not available", exc_info=True)
    return []


def list_benchmark_suites() -> str:
    """Return Rich-formatted list of available benchmark suites.

    Looks in ``~/.bog-agents/benchmarks/*.yaml`` and the Harbor package's
    built-in suites.  Shows name, description, and task count.

    Returns:
        Rich markup string with a table of suites, or a hint when none exist.
    """
    user_yamls = (
        sorted(_BENCHMARKS_DIR.glob("*.yaml")) + sorted(_BENCHMARKS_DIR.glob("*.yml"))
        if _BENCHMARKS_DIR.is_dir()
        else []
    )
    builtin_yamls = _harbor_built_in_suites()
    all_yamls = user_yamls + [p for p in builtin_yamls if p not in user_yamls]

    if not all_yamls:
        return (
            "No benchmark suites found.\n\n"
            "Create a YAML file in [cyan]~/.bog-agents/benchmarks/[/cyan] with the format:\n\n"
            "  [dim]name: my-suite[/dim]\n"
            "  [dim]description: Tests coding ability[/dim]\n"
            "  [dim]tasks:[/dim]\n"
            "  [dim]  - prompt: Write a hello world in Python[/dim]\n"
            "  [dim]    expected_keywords: [print, Hello][/dim]\n"
            "  [dim]    max_tokens: 500[/dim]"
        )

    header = f"  {'Name':<28}  {'Tasks':>5}  Description"
    sep = "  " + "\u2500" * 70
    lines: list[str] = [
        "[bold]Benchmark Suites[/bold]",
        "",
        header,
        sep,
    ]
    for yaml_path in all_yamls:
        data = _load_yaml(yaml_path)
        name = data.get("name") or yaml_path.stem
        description = data.get("description", "")
        tasks = data.get("tasks") or []
        task_count = len(tasks)
        source_tag = "[dim](built-in)[/dim]" if yaml_path not in user_yamls else ""
        lines.append(
            f"  [cyan]{name:<28}[/cyan]  {task_count:>5}  [dim]{description}[/dim] {source_tag}"
        )

    return "\n".join(lines)


def run_benchmark(
    suite_name: str | None = None, *, cwd: Path, max_tasks: int = 5
) -> str:
    """Run a benchmark suite and return Rich-formatted results.

    If suite_name is None, shows available suites instead of running.

    Benchmark YAML format::

        name: "my-suite"
        description: "Tests coding ability"
        tasks:
          - prompt: "Write a hello world in Python"
            expected_keywords: ["print", "Hello"]
            max_tokens: 500

    Args:
        suite_name: Name of the benchmark suite to run, or None to list suites.
        cwd: Working directory for the benchmark run.
        max_tasks: Maximum number of tasks to run from the suite.

    Returns:
        Rich-formatted table of task / result / tokens / score.
    """
    if suite_name is None:
        return list_benchmark_suites()

    # Locate the YAML file
    yaml_path: Path | None = None
    candidates: list[Path] = []
    if _BENCHMARKS_DIR.is_dir():
        candidates += list(_BENCHMARKS_DIR.glob(f"{suite_name}.yaml")) + list(
            _BENCHMARKS_DIR.glob(f"{suite_name}.yml")
        )
    candidates += [p for p in _harbor_built_in_suites() if p.stem == suite_name]

    if candidates:
        yaml_path = candidates[0]

    if yaml_path is None:
        return (
            f"[red]Benchmark suite '[cyan]{suite_name}[/cyan]' not found.[/red]\n"
            f"Use [cyan]/benchmark list[/cyan] to see available suites."
        )

    suite_data = _load_yaml(yaml_path)
    tasks: list[dict] = suite_data.get("tasks", [])
    if not tasks:
        return (
            f"[yellow]Suite '[cyan]{suite_name}[/cyan]' has no tasks defined.[/yellow]"
        )

    tasks = tasks[:max_tasks]

    # Try to import Harbor runner; fall back to a lightweight stub
    run_task = _make_task_runner()

    header = f"  {'#':>3}  {'Task':<40}  {'Result':<8}  {'Tokens':>7}  {'Score':>6}"
    sep = "  " + "\u2500" * 72
    lines: list[str] = [
        f"[bold]Running benchmark: {suite_data.get('name', suite_name)}[/bold]",
        f"[dim]{suite_data.get('description', '')}[/dim]",
        "",
        header,
        sep,
    ]

    total_tokens = 0
    total_score = 0.0
    for idx, task in enumerate(tasks, start=1):
        prompt = task.get("prompt", "")
        expected_keywords: list[str] = task.get("expected_keywords", [])
        max_tokens: int = task.get("max_tokens", 500)

        task_result = run_task(
            prompt=prompt,
            expected_keywords=expected_keywords,
            max_tokens=max_tokens,
            cwd=cwd,
        )
        tokens: int = task_result.get("tokens", 0)
        score: float = task_result.get("score", 0.0)
        status: str = task_result.get("status", "ok")

        total_tokens += tokens
        total_score += score

        short_prompt = prompt[:38] + "\u2026" if len(prompt) > 38 else prompt
        score_pct = f"{score * 100:.0f}%"
        status_color = "green" if status == "ok" else "red"
        lines.append(
            f"  {idx:>3}  {short_prompt:<40}  [{status_color}]{status:<8}[/{status_color}]  {tokens:>7}  {score_pct:>6}"
        )

    avg_score = total_score / len(tasks) if tasks else 0.0
    lines += [
        sep,
        f"  [bold]Total:[/bold]  {len(tasks)} tasks  |  {total_tokens:,} tokens  |  avg score {avg_score * 100:.1f}%",
    ]
    return "\n".join(lines)


def _make_task_runner():
    """Return a callable that runs a single benchmark task.

    Tries to use the Harbor backend; falls back to a keyword-match stub.

    Returns:
        Callable(prompt, expected_keywords, max_tokens, cwd) -> dict.
    """
    try:
        from bog_agents_harbor.backend import run_task_stub

        return run_task_stub
    except Exception:
        logger.debug("Harbor backend not available; using stub runner", exc_info=True)

    def _stub_run(
        *,
        prompt: str,  # noqa: ARG001
        expected_keywords: list[str],  # noqa: ARG001
        max_tokens: int,  # noqa: ARG001
        cwd: Path,  # noqa: ARG001
    ) -> dict:
        """Stub runner: marks task as skipped (Harbor backend not available).

        Args:
            prompt: Task prompt text.
            expected_keywords: Keywords expected in the response.
            max_tokens: Token budget for the task.
            cwd: Working directory.

        Returns:
            Dict with 'status', 'tokens', 'score'.
        """
        return {"status": "skip", "tokens": 0, "score": 0.0}

    return _stub_run


def show_recent_results(*, cwd: Path, limit: int = 5) -> str:  # noqa: ARG001
    """Show recent benchmark run results from trajectory files.

    Scans ``~/.bog-agents/trajectories/`` for recent runs.
    Shows date, session, tasks, avg_score, and total_tokens.

    Args:
        cwd: Working directory (unused; included for interface consistency).
        limit: Maximum number of recent results to display.

    Returns:
        Rich-formatted table of recent benchmark results.
    """
    try:
        from bog_agents_harbor.reporter import (
            find_trajectories,
            load_trajectory,
        )
    except ImportError:
        return (
            "[yellow]Harbor package not installed.[/yellow] "
            "Install it to view trajectory results:\n"
            "  [cyan]pip install bog-agents-harbor[/cyan]"
        )

    traj_paths = find_trajectories(_TRAJECTORIES_DIR, limit=limit)
    if not traj_paths:
        return (
            "No trajectory files found in [cyan]~/.bog-agents/trajectories/[/cyan].\n"
            "Run a benchmark first: [cyan]/benchmark run <suite>[/cyan]"
        )

    header = (
        f"  {'Date':<20}  {'Session':<16}  {'Steps':>5}  {'Score':>7}  {'Tokens':>9}"
    )
    sep = "  " + "\u2500" * 66
    lines: list[str] = [
        "[bold]Recent Benchmark Results[/bold]",
        "",
        header,
        sep,
    ]

    for path in traj_paths:
        try:
            report = load_trajectory(path)
        except Exception:
            logger.debug("Skipping invalid trajectory %s", path, exc_info=True)
            continue

        mtime = path.stat().st_mtime
        date_str = datetime.fromtimestamp(mtime, tz=None).strftime("%Y-%m-%d %H:%M")  # noqa: DTZ006
        short_session = (
            report.session_id[:14] + "\u2026"
            if len(report.session_id) > 14
            else report.session_id
        )
        score_str = (
            f"{report.reward * 100:.1f}%" if report.reward is not None else "  n/a"
        )
        tokens_str = (
            f"{report.total_tokens:,}" if report.total_tokens is not None else "  n/a"
        )

        lines.append(
            f"  {date_str:<20}  [cyan]{short_session:<16}[/cyan]  {report.total_steps:>5}  {score_str:>7}  {tokens_str:>9}"
        )

    return "\n".join(lines)


def format_benchmark_help() -> str:
    """Return usage help for /benchmark command.

    Returns:
        Rich markup usage text.
    """
    return """\
[bold]Benchmark commands[/bold]

  [cyan]/benchmark list[/cyan]                     List available benchmark suites
  [cyan]/benchmark run <suite>[/cyan]              Run a benchmark suite (up to 5 tasks)
  [cyan]/benchmark run <suite> --max 10[/cyan]     Run up to 10 tasks
  [cyan]/benchmark results[/cyan]                  Show recent benchmark results

[bold]Suite location[/bold]
  User suites:    [dim]~/.bog-agents/benchmarks/*.yaml[/dim]
  Built-in suites: provided by the [dim]bog-agents-harbor[/dim] package

[bold]Suite YAML format[/bold]
  [dim]name: my-suite[/dim]
  [dim]description: Tests coding ability[/dim]
  [dim]tasks:[/dim]
  [dim]  - prompt: Write a hello world in Python[/dim]
  [dim]    expected_keywords: [print, Hello][/dim]
  [dim]    max_tokens: 500[/dim]"""
