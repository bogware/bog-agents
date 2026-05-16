"""Stand-alone controller for the ``/sidecar`` slash command.

Wires :mod:`bog_agents_cli.sidecar` into the running TUI: resolves the
user's configured model, builds the read-only tool list, snapshots the
parent's recent messages into a summary, and runs the sidecar query in
a worker thread so the TUI event loop stays responsive.

All user-facing logic lives here so the TUI handler in ``app.py`` is a
thin (~6-line) dispatcher and the feature is testable without the
Textual app.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from bog_agents_cli.sidecar import (
    SidecarResult,
    build_readonly_tools,
    run_sidecar_query,
    summarize_parent_context,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


class SidecarController:
    """Facade for the ``/sidecar`` slash command.

    Args:
        working_dir: Project root used by the read-only filesystem tools.
        model_factory: Zero-arg callable that returns a fresh
            ``BaseChatModel`` for sidecar use. We accept a factory
            (instead of a model instance) so each sidecar query gets a
            fresh client — preventing the parent's mid-flight state
            (mid-request, paused tool call, partial stream) from
            leaking into the sidecar.
        web_search: Toggle the ``web_search`` tool in sidecar's allowlist.
        max_iterations: Hard cap on the sidecar agent's tool-call loop.
    """

    def __init__(
        self,
        *,
        working_dir: Path,
        model_factory: Any,  # noqa: ANN401 — Callable[[], BaseChatModel]
        web_search: bool = True,
        max_iterations: int = 8,
    ) -> None:
        self._working_dir = working_dir
        self._model_factory = model_factory
        self._web_search = web_search
        self._max_iterations = max_iterations

    def run(
        self,
        question: str,
        *,
        parent_messages: Sequence[Any] | None = None,
        context_override: str | None = None,
    ) -> SidecarResult:
        """Execute a sidecar query end-to-end.

        Args:
            question: The user's question (the slash arg).
            parent_messages: Optional list of LangChain messages from
                the parent thread. When supplied (and
                ``context_override`` is not), the controller
                summarises the last few turns as background. The
                summary is read-only — the sidecar never mutates these.
            context_override: When set, this string is the literal
                context block passed to the sidecar — bypasses the
                automatic summariser. Used by tests and by users who
                want to paste their own context.

        Returns:
            A :class:`SidecarResult` whose ``quote_for_parent()`` is
            the TUI-ready quoted block.
        """
        question = (question or "").strip()
        if not question:
            return SidecarResult(
                ok=False,
                error="empty question — pass `/sidecar <your question>`",
            )

        context_summary = (
            context_override
            if context_override is not None
            else summarize_parent_context(parent_messages or ())
        )

        try:
            model = self._model_factory()
        except Exception as exc:
            logger.exception("sidecar: failed to build model")
            return SidecarResult(
                ok=False, error=f"could not build sidecar model: {exc}"
            )

        tools = build_readonly_tools(
            working_dir=self._working_dir,
            web_search=self._web_search,
        )

        return run_sidecar_query(
            question=question,
            model=model,
            tools=tools,
            context_summary=context_summary,
            max_iterations=self._max_iterations,
        )


# ---------------------------------------------------------------------------
# Per-cwd singleton (mirrors expert_controller.get_controller for symmetry)
# ---------------------------------------------------------------------------


_CONTROLLERS: dict[Path, SidecarController] = {}


def get_controller(
    working_dir: Path | str,
    *,
    model_factory: Any | None = None,  # noqa: ANN401
    web_search: bool = True,
) -> SidecarController:
    """Return a per-cwd cached :class:`SidecarController`.

    Args:
        working_dir: Project root.
        model_factory: Required on first call per cwd; ignored on
            subsequent calls (the cached instance keeps its original
            factory so reset_controllers() is the only way to swap).
        web_search: Toggle the ``web_search`` tool. Only honored on
            first call per cwd.

    Raises:
        RuntimeError: When the first call for a given cwd is made
            without supplying ``model_factory``.
    """
    key = Path(working_dir).resolve()
    if key not in _CONTROLLERS:
        if model_factory is None:
            msg = (
                "SidecarController has not been built for this cwd yet — "
                "the first call must pass model_factory=..."
            )
            raise RuntimeError(msg)
        _CONTROLLERS[key] = SidecarController(
            working_dir=key,
            model_factory=model_factory,
            web_search=web_search,
        )
    return _CONTROLLERS[key]


def reset_controllers() -> None:
    """Drop every cached controller. Test-only helper."""
    _CONTROLLERS.clear()


# ---------------------------------------------------------------------------
# Top-level dispatcher
# ---------------------------------------------------------------------------


def dispatch(
    command_text: str,
    *,
    working_dir: Path | str,
    model_factory: Any,  # noqa: ANN401
    parent_messages: Sequence[Any] | None = None,
    web_search: bool = True,
) -> str:
    """Run a ``/sidecar <question>`` command, returning the quoted reply.

    Args:
        command_text: Raw slash input, e.g. ``"/sidecar how does X work?"``.
        working_dir: Project root.
        model_factory: Zero-arg callable returning a fresh chat model.
        parent_messages: Optional parent message history for the
            background summariser.
        web_search: Toggle web_search tool.

    Returns:
        Markdown-quoted block ready to drop into the parent transcript.
    """
    text = command_text.strip()
    if text.startswith("/sidecar"):
        text = text[len("/sidecar") :].strip()
    controller = get_controller(
        working_dir,
        model_factory=model_factory,
        web_search=web_search,
    )
    result = controller.run(text, parent_messages=parent_messages)
    return result.quote_for_parent()
