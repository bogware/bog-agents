"""Pipeline system — chain prompts, skills, and slash commands with cron scheduling.

A pipeline is a YAML file in ``~/.bog-agents/pipelines/`` that defines an
ordered sequence of steps.  Each step can be:

* ``prompt``  — a named prompt from the prompt library (supports variables)
* ``message`` — a literal text message sent to the agent
* ``slash``   — a slash command (e.g. ``/review``, ``/skills``)

Steps may reference outputs from previous steps via ``{{step.<n>.output}}``.
Top-level pipeline variables can be declared and are filled in at run-time.

Pipeline files use ``.yaml`` or ``.yml`` extensions and live under
``~/.bog-agents/pipelines/``.

Example ``my-pipeline.yaml``::

    name: security-review
    description: "Run a security review and commit a report"
    variables:
      - module
      - branch
    schedule: "0 9 * * 1"        # Every Monday at 09:00 (cron expression)
    steps:
      - id: review
        type: prompt
        name: security-review    # from prompt library
        variables:
          module: "{{module}}"
          focus_areas: "auth, sql, xss"

      - id: commit
        type: slash
        command: "/git commit -m 'Security review for {{module}}'"
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_PIPELINES_DIR = Path.home() / ".bog-agents" / "pipelines"
_VAR_RE_STR = r"\{\{(\w[\w.]*)\}\}"

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class PipelineStep:
    """A single step in a pipeline."""

    id: str
    type: str  # "prompt" | "message" | "slash"
    name: str = ""  # used when type == "prompt"
    variables: dict[str, str] = field(
        default_factory=dict
    )  # used when type == "prompt"
    text: str = ""  # used when type == "message"
    command: str = ""  # used when type == "slash"
    description: str = ""


@dataclass
class Pipeline:
    """A complete pipeline definition."""

    name: str
    steps: list[PipelineStep]
    description: str = ""
    variables: list[str] = field(default_factory=list)
    schedule: str = ""  # cron expression, e.g. "0 9 * * 1"
    source_path: Path | None = None

    def render_step_text(  # noqa: PLR6301
        self, step: PipelineStep, values: dict[str, str]
    ) -> str:
        """Resolve variable placeholders in a step's text fields.

        Args:
            step: The step to render.
            values: Current variable mapping (includes previous step outputs).

        Returns:
            Rendered text ready for the agent.

        Raises:
            ValueError: If the step type is unknown or the referenced prompt is not found.
        """
        import re

        from bog_agents_cli.vars_store import (
            resolve_vars,  # deferred to avoid circular import
        )

        def _sub(m: re.Match) -> str:
            key = m.group(1)
            return values.get(key, m.group(0))

        if step.type == "message":
            return resolve_vars(re.sub(_VAR_RE_STR, _sub, step.text))
        if step.type == "slash":
            return resolve_vars(re.sub(_VAR_RE_STR, _sub, step.command))
        if step.type == "prompt":
            from bog_agents_cli.prompt_library import get_prompt

            entry = get_prompt(step.name)
            if entry is None:
                msg = f"Prompt '{step.name}' not found in library"
                raise ValueError(msg)
            resolved_vars = {
                k: re.sub(_VAR_RE_STR, _sub, v) for k, v in step.variables.items()
            }
            return entry.render(
                resolved_vars
            )  # render() calls resolve_vars() internally
        msg = f"Unknown step type: {step.type!r}"
        raise ValueError(msg)

    def missing_variables(self, provided: dict[str, str]) -> list[str]:
        """Return top-level variable names not yet in *provided*.

        Args:
            provided: Partially-filled variable mapping.

        Returns:
            List of variable names still needed.
        """
        return [v for v in self.variables if v not in provided]


# ---------------------------------------------------------------------------
# YAML loading / saving
# ---------------------------------------------------------------------------


def _pipelines_dir() -> Path:
    _PIPELINES_DIR.mkdir(parents=True, exist_ok=True)
    return _PIPELINES_DIR


def load_pipeline(path: Path) -> Pipeline:
    """Load a :class:`Pipeline` from a YAML file.

    Args:
        path: Path to the YAML pipeline file.

    Returns:
        Parsed :class:`Pipeline`.

    Raises:
        ValueError: If the file is invalid or missing required fields.
    """
    import yaml  # pyyaml — already a CLI dependency

    try:
        with path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except Exception as exc:
        msg = f"Could not parse pipeline file {path}: {exc}"
        raise ValueError(msg) from exc

    if not isinstance(data, dict):
        msg = f"Pipeline file {path} must be a YAML mapping"
        raise ValueError(msg)  # noqa: TRY004

    name = data.get("name") or path.stem
    raw_steps = data.get("steps", [])
    if not isinstance(raw_steps, list):
        msg = "Pipeline 'steps' must be a list"
        raise ValueError(msg)  # noqa: TRY004

    steps: list[PipelineStep] = []
    for i, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            msg = f"Step {i} must be a mapping"
            raise ValueError(msg)  # noqa: TRY004
        step_type = raw.get("type", "message")
        steps.append(
            PipelineStep(
                id=str(raw.get("id", f"step-{i}")),
                type=step_type,
                name=str(raw.get("name", "")),
                variables={k: str(v) for k, v in raw.get("variables", {}).items()},
                text=str(raw.get("text", "")),
                command=str(raw.get("command", "")),
                description=str(raw.get("description", "")),
            )
        )

    return Pipeline(
        name=str(name),
        steps=steps,
        description=str(data.get("description", "")),
        variables=list(data.get("variables", [])),
        schedule=str(data.get("schedule", "")),
        source_path=path,
    )


def list_pipelines() -> list[Pipeline]:
    """Return all pipelines found in the pipelines directory.

    Returns:
        Sorted list of :class:`Pipeline` objects.  Parsing errors are logged
        and the offending file is skipped.
    """
    directory = _pipelines_dir()
    pipelines: list[Pipeline] = []
    for path in sorted(directory.glob("*.y*ml")):
        try:
            pipelines.append(load_pipeline(path))
        except ValueError:
            logger.warning("Skipping invalid pipeline file: %s", path, exc_info=True)
    return pipelines


def save_pipeline(pipeline: Pipeline, *, name: str | None = None) -> Path:
    """Persist *pipeline* to the pipelines directory.

    Args:
        pipeline: The pipeline to save.
        name: Override file name (without extension).

    Returns:
        Path to the saved file.
    """
    import yaml

    file_name = (name or pipeline.name).replace(" ", "-") + ".yaml"
    dest = _pipelines_dir() / file_name

    data: dict[str, Any] = {
        "name": pipeline.name,
        "description": pipeline.description,
    }
    if pipeline.variables:
        data["variables"] = pipeline.variables
    if pipeline.schedule:
        data["schedule"] = pipeline.schedule
    data["steps"] = [
        {
            k: v
            for k, v in {
                "id": step.id,
                "type": step.type,
                "name": step.name or None,
                "variables": step.variables or None,
                "text": step.text or None,
                "command": step.command or None,
                "description": step.description or None,
            }.items()
            if v is not None and v != {}
        }
        for step in pipeline.steps
    ]

    with dest.open("w") as fh:
        yaml.dump(data, fh, sort_keys=False, allow_unicode=True)

    logger.info("Saved pipeline '%s' to %s", pipeline.name, dest)
    return dest


def delete_pipeline(name: str) -> bool:
    """Delete a pipeline file by pipeline name or file stem.

    Args:
        name: Pipeline name or YAML file stem.

    Returns:
        ``True`` if deleted, ``False`` if not found.
    """
    directory = _pipelines_dir()
    for path in directory.glob("*.y*ml"):
        if path.stem == name or path.stem == name.replace(" ", "-"):
            path.unlink()
            logger.info("Deleted pipeline: %s", path)
            return True
    return False


# ---------------------------------------------------------------------------
# Pipeline executor
# ---------------------------------------------------------------------------


class PipelineResult:
    """Result of running a pipeline."""

    def __init__(self) -> None:
        self.step_outputs: dict[str, str] = {}
        self.errors: list[str] = []
        self.completed_steps: int = 0

    @property
    def success(self) -> bool:
        """True if all steps completed without errors."""
        return not self.errors


async def execute_pipeline(
    pipeline: Pipeline,
    variable_values: dict[str, str],
    on_step: Callable[[int, str, str], Any] | None = None,
) -> PipelineResult:
    """Execute all steps in *pipeline* in order.

    Args:
        pipeline: The pipeline to run.
        variable_values: Top-level variable values (pre-filled).
        on_step: Optional async callback called with
            ``(step_index, step_id, rendered_text)`` before each step runs.
            If provided, the caller is responsible for sending *rendered_text*
            to the agent.

    Returns:
        :class:`PipelineResult` with per-step outputs.
    """
    result = PipelineResult()
    context: dict[str, str] = dict(variable_values)

    for i, step in enumerate(pipeline.steps):
        try:
            rendered = pipeline.render_step_text(step, context)
        except (KeyError, ValueError) as exc:
            error = f"Step {step.id}: {exc}"
            logger.exception("Pipeline step render failed: %s", error)
            result.errors.append(error)
            break

        if on_step is not None:
            try:
                await on_step(i, step.id, rendered)
            except Exception as exc:
                error = f"Step {step.id} callback error: {exc}"
                logger.exception("Pipeline on_step callback error: %s", error)
                result.errors.append(error)
                break

        result.step_outputs[step.id] = rendered
        # Expose step output for later steps via {{step.<id>.output}}
        context[f"step.{step.id}.output"] = rendered
        context[f"step.{i}.output"] = rendered
        result.completed_steps += 1

    return result


# ---------------------------------------------------------------------------
# Cron scheduler
# ---------------------------------------------------------------------------


class PipelineScheduler:
    """Background thread that runs scheduled pipelines at their cron times.

    Usage::

        scheduler = PipelineScheduler(run_callback)
        scheduler.start()
        scheduler.reload()  # call whenever pipelines change
        scheduler.stop()
    """

    def __init__(
        self,
        run_callback: Callable[[Pipeline, dict[str, str]], Any],
        check_interval: float = 30.0,
    ) -> None:
        """Initialize the scheduler.

        Args:
            run_callback: Called with ``(pipeline, {})`` when a pipeline's
                cron expression fires.  The callback is responsible for
                collecting any required variables before execution.
            check_interval: How often (seconds) the scheduler checks for
                due pipelines.  Defaults to 30 s (cron granularity is 1 min).
        """
        self._run_callback = run_callback
        self._check_interval = check_interval
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._pipelines: list[Pipeline] = []
        self._last_fired: dict[str, float] = {}  # pipeline name → last fire timestamp
        self._lock = threading.Lock()

    def start(self) -> None:
        """Start the scheduler background thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="pipeline-scheduler",
            daemon=True,
        )
        self._thread.start()
        logger.info("Pipeline scheduler started (interval=%ss)", self._check_interval)

    def stop(self) -> None:
        """Signal the scheduler to stop and wait for it to exit."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("Pipeline scheduler stopped")

    def reload(self) -> None:
        """Reload the pipeline list from disk."""
        with self._lock:
            self._pipelines = [p for p in list_pipelines() if p.schedule]
        logger.debug(
            "Scheduler reloaded: %d scheduled pipelines",
            len(self._pipelines),
        )

    def _loop(self) -> None:
        self.reload()
        while not self._stop_event.wait(self._check_interval):
            self._tick()

    def _tick(self) -> None:
        now = time.time()
        with self._lock:
            pipelines = list(self._pipelines)

        for pipeline in pipelines:
            if not pipeline.schedule:
                continue
            try:
                if self._is_due(pipeline, now):
                    logger.info("Firing scheduled pipeline: %s", pipeline.name)
                    self._last_fired[pipeline.name] = now
                    try:
                        self._run_callback(pipeline, {})
                    except Exception:
                        logger.exception(
                            "Error running scheduled pipeline: %s", pipeline.name
                        )
            except Exception:
                logger.exception("Scheduler error checking pipeline: %s", pipeline.name)

    def _is_due(self, pipeline: Pipeline, now: float) -> bool:
        """Check if *pipeline* is due to fire at *now*.

        Uses ``croniter`` to compute the most recent scheduled time and
        compares it against when we last fired.

        Args:
            pipeline: Pipeline with a cron expression.
            now: Current epoch timestamp.

        Returns:
            ``True`` if the pipeline should fire.
        """
        try:
            from croniter import croniter
        except ImportError:
            logger.warning("croniter not installed; scheduled pipelines disabled")
            return False

        try:
            cron = croniter(pipeline.schedule, now - self._check_interval)
            next_time = cron.get_next(float)
        except Exception:
            logger.warning(
                "Invalid cron expression for '%s': %s", pipeline.name, pipeline.schedule
            )
            return False

        last = self._last_fired.get(pipeline.name, 0.0)
        return next_time <= now and next_time > last


# Module-level scheduler instance (lazy initialised by the CLI app)
_scheduler: PipelineScheduler | None = None


def get_scheduler(
    run_callback: Callable[[Pipeline, dict[str, str]], Any] | None = None,
) -> PipelineScheduler:
    """Return the module-level scheduler, creating it if needed.

    Args:
        run_callback: Required on first call to provide the pipeline trigger.

    Returns:
        The shared :class:`PipelineScheduler` instance.

    Raises:
        RuntimeError: If called before initialisation (no *run_callback*).
    """
    global _scheduler  # noqa: PLW0603
    if _scheduler is None:
        if run_callback is None:
            msg = "Pipeline scheduler not yet initialised; provide run_callback"
            raise RuntimeError(msg)
        _scheduler = PipelineScheduler(run_callback)
        _scheduler.start()
    return _scheduler
