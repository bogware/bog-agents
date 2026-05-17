"""Slash-command controller for /orchestrate (Wave G).

Mirrors :mod:`bog_agents_cli.sidecar_controller`: per-cwd cached
controller, model-factory pattern (fresh client per run so the
parent's in-flight state can't leak into the orchestrator's planner),
thin dispatcher the TUI handler calls.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from bog_agents_cli.orchestrator import (
    OrchestrationResult,
    render_result,
    run_orchestration,
)

logger = logging.getLogger(__name__)


class OrchestratorController:
    """Facade for ``/orchestrate``.

    Args:
        working_dir: Project root used by the read-only tools each
            subtask gets.
        model_factory: Zero-arg callable returning a fresh chat model.
            Mandatory for ``/orchestrate`` — the planner needs an LLM.
        max_iterations_per_subtask: Hard cap on each subtask's
            model→tool loop. Default 12.
    """

    def __init__(
        self,
        *,
        working_dir: Path,
        model_factory: Any,  # noqa: ANN401 — Callable[[], BaseChatModel]
        max_iterations_per_subtask: int = 12,
    ) -> None:
        self._working_dir = working_dir
        self._model_factory = model_factory
        self._max_iterations = max_iterations_per_subtask

    def run(self, goal: str) -> OrchestrationResult:
        """Execute one orchestration end-to-end."""
        goal = (goal or "").strip()
        if not goal:
            return OrchestrationResult(
                goal="",
                error="empty goal — pass `/orchestrate <your goal>`",
            )
        try:
            model = self._model_factory()
        except Exception as exc:
            logger.exception("orchestrator: failed to build model")
            return OrchestrationResult(
                goal=goal,
                error=f"could not build orchestrator model: {exc}",
            )
        return run_orchestration(
            goal=goal,
            model=model,
            working_dir=self._working_dir,
            max_iterations_per_subtask=self._max_iterations,
        )


_CONTROLLERS: dict[Path, OrchestratorController] = {}


def get_controller(
    working_dir: Path | str,
    *,
    model_factory: Any | None = None,  # noqa: ANN401
) -> OrchestratorController:
    """Return a per-cwd cached :class:`OrchestratorController`.

    Raises:
        RuntimeError: When the first call for this cwd is made
            without ``model_factory``.
    """
    key = Path(working_dir).resolve()
    if key not in _CONTROLLERS:
        if model_factory is None:
            msg = (
                "OrchestratorController has not been built for this cwd yet — "
                "the first call must pass model_factory=..."
            )
            raise RuntimeError(msg)
        _CONTROLLERS[key] = OrchestratorController(
            working_dir=key, model_factory=model_factory
        )
    return _CONTROLLERS[key]


def reset_controllers() -> None:
    """Drop every cached controller. Test-only."""
    _CONTROLLERS.clear()


def dispatch(
    command_text: str,
    *,
    working_dir: Path | str,
    model_factory: Any,  # noqa: ANN401
) -> str:
    """Top-level dispatcher for ``/orchestrate <goal>``."""
    text = command_text.strip()
    if text.startswith("/orchestrate"):
        text = text[len("/orchestrate") :].strip()
    controller = get_controller(working_dir, model_factory=model_factory)
    result = controller.run(text)
    return render_result(result)
