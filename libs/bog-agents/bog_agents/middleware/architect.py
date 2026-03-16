"""Middleware for Architect/Editor dual-model workflow.

Feature #16: Architect/Editor split — uses a strong reasoning model to plan
changes and a fast model to execute the edits.

Feature #41: Multi-provider agent teams — enables cross-provider orchestration
where different models handle different responsibilities.
"""

from __future__ import annotations

import logging

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ResponseT,
)
from langchain.tools import ToolRuntime
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import BaseTool, StructuredTool
from typing_extensions import TypedDict

from bog_agents._models import resolve_model

logger = logging.getLogger(__name__)


class ArchitectState(TypedDict):
    """State for the architect middleware."""


class ArchitectMiddleware(AgentMiddleware[ArchitectState, ContextT, ResponseT]):
    """Middleware that implements an Architect/Editor dual-model workflow.

    The architect model (typically a stronger reasoning model) analyzes
    the task and generates a plan. The editor model (typically faster)
    then executes the actual code changes following the plan.

    This also enables multi-provider agent teams where different providers
    handle different responsibilities in the same session.

    Args:
        architect_model: Model for planning and reasoning.
        editor_model: Model for executing edits. If None, uses the main agent model.
        reviewer_model: Optional model for code review.
        enable_review: Whether to auto-review changes after edits.
    """

    state_schema = ArchitectState

    def __init__(
        self,
        *,
        architect_model: str | BaseChatModel | None = None,
        editor_model: str | BaseChatModel | None = None,
        reviewer_model: str | BaseChatModel | None = None,
        enable_review: bool = False,
    ) -> None:
        self._architect_model = resolve_model(architect_model) if architect_model else None
        self._editor_model = resolve_model(editor_model) if editor_model else None
        self._reviewer_model = resolve_model(reviewer_model) if reviewer_model else None
        self._enable_review = enable_review
        self.tools = self._build_tools()

    def _build_tools(self) -> list[BaseTool]:
        """Build architect/editor tools."""
        middleware = self
        tools: list[BaseTool] = []

        if self._architect_model:

            def architect_plan(
                runtime: ToolRuntime[None, ArchitectState],
                task: str = "",
                context: str = "",
            ) -> str:
                """Ask the architect model to analyze a task and create an implementation plan.

                Args:
                    task: Description of what needs to be done.
                    context: Relevant code or context.
                """
                if not middleware._architect_model:
                    return "Error: No architect model configured."

                messages = [
                    SystemMessage(
                        content=(
                            "You are a software architect. Analyze the task and create a detailed, "
                            "step-by-step implementation plan. Focus on:\n"
                            "1. What files need to be changed\n"
                            "2. What specific changes to make in each file\n"
                            "3. The order of changes\n"
                            "4. Any edge cases or considerations\n\n"
                            "Be precise and actionable. The plan will be executed by another model."
                        )
                    ),
                    HumanMessage(content=f"Task: {task}\n\nContext:\n{context}" if context else f"Task: {task}"),
                ]
                response = middleware._architect_model.invoke(messages)
                return str(response.content)

            tools.append(
                StructuredTool.from_function(
                    name="architect_plan",
                    description=(
                        "Ask the architect model (stronger reasoning) to analyze a task and "
                        "create a detailed implementation plan. Use this for complex tasks "
                        "that benefit from careful planning before coding."
                    ),
                    func=architect_plan,
                )
            )

        if self._reviewer_model:

            def code_review(
                runtime: ToolRuntime[None, ArchitectState],
                code: str = "",
                context: str = "",
            ) -> str:
                """Ask the reviewer model to review code changes.

                Args:
                    code: The code or diff to review.
                    context: Additional context about the changes.
                """
                if not middleware._reviewer_model:
                    return "Error: No reviewer model configured."

                messages = [
                    SystemMessage(
                        content=(
                            "You are a code reviewer. Review the provided code changes for:\n"
                            "1. Correctness and logic errors\n"
                            "2. Security vulnerabilities\n"
                            "3. Performance issues\n"
                            "4. Style and best practices\n"
                            "5. Missing edge cases or tests\n\n"
                            "Be constructive and specific. Point to exact lines when possible."
                        )
                    ),
                    HumanMessage(content=f"Changes:\n{code}\n\nContext:\n{context}" if context else f"Changes:\n{code}"),
                ]
                response = middleware._reviewer_model.invoke(messages)
                return str(response.content)

            tools.append(
                StructuredTool.from_function(
                    name="code_review",
                    description=("Ask the reviewer model to review code changes for correctness, security, performance, and best practices."),
                    func=code_review,
                )
            )

        def consult_model(
            runtime: ToolRuntime[None, ArchitectState],
            model_spec: str = "",
            prompt: str = "",
            system_prompt: str = "You are a helpful assistant.",
        ) -> str:
            """Consult a specific model for a one-off question.

            Args:
                model_spec: Model specification in provider:model format.
                prompt: The question or task.
                system_prompt: System instructions for the model.
            """
            try:
                model = resolve_model(model_spec)
            except Exception as e:
                return f"Error resolving model '{model_spec}': {e}"

            messages = [
                SystemMessage(content=system_prompt),
                HumanMessage(content=prompt),
            ]
            try:
                response = model.invoke(messages)
                return str(response.content)
            except Exception as e:
                return f"Error from model '{model_spec}': {e}"

        tools.append(
            StructuredTool.from_function(
                name="consult_model",
                description=(
                    "Consult a specific model for a one-off question. "
                    "Use provider:model format (e.g., 'openai:gpt-5', 'anthropic:claude-sonnet-4-6'). "
                    "Useful for getting a second opinion or leveraging model-specific strengths."
                ),
                func=consult_model,
            )
        )

        return tools
