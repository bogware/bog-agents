"""Agent Teams middleware for multi-session orchestration.
Multiple agents working on shared projects with task assignment,
member management, and collaborative artifact tracking.

⚠ **STUB — NOT FOR PRODUCTION USE.**

This middleware is a scaffold that demonstrates the shape of a real
implementation. Its tools accept calls and return placeholder structures
so an agent can be wired against the surface, but the underlying logic
is not implemented — for example, ``fetch_quote`` returns ``price=0.0``
with a note instructing the caller to populate real data. Models that
call these tools will receive plausible-looking but **incorrect**
results.

This module ships at "Development Status :: 4 - Beta" deliberately;
see REVIEW.md P0-A for the broader plan (extract to a separate
``bog-agents-finance``-style package once the implementations are real,
or remove from the headline middleware list if they will not be).
Do not enable in any flow whose output is consumed by a downstream
system, customer-facing surface, or compliance-relevant artifact.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)
from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, StructuredTool
from typing_extensions import TypedDict

from bog_agents.middleware._utils import append_to_system_message

logger = logging.getLogger(__name__)


@dataclass
class TeamMember:
    """A member of an agent team."""

    name: str
    role: str
    capabilities: list[str] = field(default_factory=list)
    status: str = "active"


@dataclass
class TeamTask:
    """A task assigned within a shared project."""

    task_id: str
    title: str
    assigned_to: str
    status: str = "pending"
    result: str = ""
    created_at: str = ""


@dataclass
class SharedProject:
    """A shared project with team members and tasks."""

    project_id: str
    name: str
    description: str
    members: list[TeamMember] = field(default_factory=list)
    tasks: list[TeamTask] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)


@dataclass
class TeamStore:
    """In-memory store for team projects."""

    projects: dict[str, SharedProject] = field(default_factory=dict)
    _next_project_id: int = 1
    _next_task_id: int = 1


SYSTEM_PROMPT = """You have access to agent team orchestration tools. You can:
- Create shared projects for collaborative financial work
- Add team members with specific roles and capabilities
- Assign tasks to team members and track progress
- View project status with member and task details
Use these tools to coordinate multi-agent workflows for financial advisory tasks."""


class AgentTeamsState(TypedDict):
    """State for the agent teams middleware."""


class AgentTeamsMiddleware(AgentMiddleware[AgentTeamsState, ContextT, ResponseT]):
    """Middleware for multi-agent team orchestration on shared projects."""

    state_schema = AgentTeamsState

    def __init__(self) -> None:
        self.store = TeamStore()
        self.tools: list[BaseTool] = self._build_tools()

    def _build_tools(self) -> list[BaseTool]:
        mw = self

        def create_project(
            runtime: ToolRuntime[None, AgentTeamsState],
            name: Annotated[str, "Name of the project"],
            description: Annotated[str, "Description of the project goals"],
        ) -> str:
            """Create a new shared project for team collaboration."""
            pid = f"proj-{mw.store._next_project_id}"
            mw.store._next_project_id += 1
            project = SharedProject(
                project_id=pid,
                name=name,
                description=description,
            )
            mw.store.projects[pid] = project
            logger.info("Created project %s: %s", pid, name)
            return f"Created project '{name}' (ID: {pid})"

        def add_team_member(
            runtime: ToolRuntime[None, AgentTeamsState],
            project_id: Annotated[str, "ID of the project to add the member to"],
            name: Annotated[str, "Name of the team member"],
            role: Annotated[str, "Role of the team member"],
            capabilities: Annotated[str, "Comma-separated list of capabilities"],
        ) -> str:
            """Add a team member to a shared project."""
            project = mw.store.projects.get(project_id)
            if not project:
                return f"Error: Project '{project_id}' not found."
            caps = [c.strip() for c in capabilities.split(",") if c.strip()]
            member = TeamMember(name=name, role=role, capabilities=caps)
            project.members.append(member)
            logger.info("Added member %s to project %s", name, project_id)
            return f"Added '{name}' ({role}) to project '{project.name}'"

        def assign_task(
            runtime: ToolRuntime[None, AgentTeamsState],
            project_id: Annotated[str, "ID of the project"],
            title: Annotated[str, "Title of the task"],
            assigned_to: Annotated[str, "Name of the team member to assign the task to"],
        ) -> str:
            """Assign a task to a team member in a project."""
            project = mw.store.projects.get(project_id)
            if not project:
                return f"Error: Project '{project_id}' not found."
            member_names = [m.name for m in project.members]
            if assigned_to not in member_names:
                return f"Error: Member '{assigned_to}' not found in project."
            tid = f"task-{mw.store._next_task_id}"
            mw.store._next_task_id += 1
            task = TeamTask(
                task_id=tid,
                title=title,
                assigned_to=assigned_to,
                created_at=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime()),
            )
            project.tasks.append(task)
            logger.info("Assigned task %s to %s in project %s", tid, assigned_to, project_id)
            return f"Assigned task '{title}' (ID: {tid}) to {assigned_to}"

        def project_status(
            runtime: ToolRuntime[None, AgentTeamsState],
            project_id: Annotated[str, "ID of the project to get status for"],
        ) -> str:
            """Get the status of a shared project including members and tasks."""
            project = mw.store.projects.get(project_id)
            if not project:
                return f"Error: Project '{project_id}' not found."
            lines = [
                f"Project: {project.name} ({project.project_id})",
                f"Description: {project.description}",
                f"Members ({len(project.members)}):",
            ]
            for m in project.members:
                lines.append(f"  - {m.name} ({m.role}) [{m.status}]")
            lines.append(f"Tasks ({len(project.tasks)}):")
            for t in project.tasks:
                lines.append(f"  - [{t.status}] {t.title} -> {t.assigned_to}")
            return "\n".join(lines)

        def clear_projects(
            runtime: ToolRuntime[None, AgentTeamsState],
        ) -> str:
            """Clear all projects and reset the team store."""
            count = len(mw.store.projects)
            mw.store = TeamStore()
            logger.info("Cleared %d projects", count)
            return f"Cleared {count} project(s)."

        return [
            StructuredTool.from_function(create_project, name="create_project"),
            StructuredTool.from_function(add_team_member, name="add_team_member"),
            StructuredTool.from_function(assign_task, name="assign_task"),
            StructuredTool.from_function(project_status, name="project_status"),
            StructuredTool.from_function(clear_projects, name="clear_projects"),
        ]

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Append the agent teams system prompt to the request."""
        return request.override(system_message=append_to_system_message(request.system_message, SYSTEM_PROMPT))

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """Synchronously wrap the model call with team orchestration context."""
        return call_next(self.modify_request(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        """Asynchronously wrap the model call with team orchestration context."""
        return await call_next(self.modify_request(request))


__all__ = ["AgentTeamsMiddleware", "SharedProject", "TeamMember", "TeamStore", "TeamTask"]
