"""Multi-Agent Dashboard for the TUI.

Provides a split-pane dashboard view showing parallel agent progress,
costs, tool usage, and outputs in real time. Designed for Textual.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

_MAX_OUTPUT_LINES = 100


@dataclass
class AgentPanelState:
    """State for a single agent in the dashboard."""

    agent_id: str
    name: str
    status: str = "idle"
    prompt: str = ""
    current_action: str = ""
    tool_calls: int = 0
    errors: int = 0
    tokens_used: int = 0
    cost_usd: float = 0.0
    started_at: float | None = None
    completed_at: float | None = None
    output_lines: list[str] = field(default_factory=list)
    progress_pct: float = 0.0

    @property
    def duration_seconds(self) -> float:
        """Duration in seconds."""
        if self.started_at is None:
            return 0.0
        end = self.completed_at or time.time()
        return end - self.started_at

    @property
    def status_icon(self) -> str:
        """Status indicator character."""
        icons = {
            "idle": " ",
            "running": ">",
            "completed": "+",
            "failed": "!",
            "cancelled": "x",
        }
        return icons.get(self.status, "?")

    def add_output(self, line: str) -> None:
        """Add an output line, keeping last ``_MAX_OUTPUT_LINES``.

        Args:
            line: Output line to add.
        """
        self.output_lines.append(line)
        if len(self.output_lines) > _MAX_OUTPUT_LINES:
            self.output_lines = self.output_lines[-_MAX_OUTPUT_LINES:]


@dataclass
class DashboardState:
    """Aggregate state for the multi-agent dashboard."""

    agents: dict[str, AgentPanelState] = field(default_factory=dict)
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    session_start: float = field(default_factory=time.time)

    def add_agent(
        self, agent_id: str, name: str, prompt: str = ""
    ) -> AgentPanelState:
        """Register a new agent in the dashboard.

        Args:
            agent_id: Unique agent identifier.
            name: Human-readable name.
            prompt: Initial prompt.

        Returns:
            The new ``AgentPanelState``.
        """
        state = AgentPanelState(
            agent_id=agent_id,
            name=name,
            prompt=prompt,
        )
        self.agents[agent_id] = state
        return state

    def remove_agent(self, agent_id: str) -> None:
        """Remove an agent from the dashboard.

        Args:
            agent_id: Agent to remove.
        """
        self.agents.pop(agent_id, None)

    def get_agent(self, agent_id: str) -> AgentPanelState | None:
        """Get an agent's panel state.

        Args:
            agent_id: Agent identifier.

        Returns:
            ``AgentPanelState`` or ``None``.
        """
        return self.agents.get(agent_id)

    @property
    def running_count(self) -> int:
        """Number of currently running agents."""
        return sum(
            1 for a in self.agents.values() if a.status == "running"
        )

    @property
    def completed_count(self) -> int:
        """Number of completed agents."""
        return sum(
            1 for a in self.agents.values() if a.status == "completed"
        )

    def update_totals(self) -> None:
        """Recalculate total cost and tokens from all agents."""
        self.total_cost_usd = sum(a.cost_usd for a in self.agents.values())
        self.total_tokens = sum(a.tokens_used for a in self.agents.values())

    def format_summary(self) -> str:
        """Format a text summary of all agents.

        Returns:
            Formatted summary string.
        """
        self.update_totals()
        elapsed = time.time() - self.session_start
        lines: list[str] = [
            (
                f"Dashboard: {len(self.agents)} agents | "
                f"{self.running_count} running | "
                f"{self.completed_count} done | "
                f"${self.total_cost_usd:.4f} | "
                f"{elapsed:.0f}s"
            ),
            "-" * 70,
        ]

        for agent in sorted(
            self.agents.values(), key=lambda a: a.agent_id
        ):
            icon = agent.status_icon
            dur = (
                f"{agent.duration_seconds:.0f}s"
                if agent.started_at
                else "--"
            )
            action = (
                agent.current_action[:40]
                if agent.current_action
                else agent.status
            )
            lines.append(
                f"  [{icon}] {agent.name:<20} {action:<40} "
                f"{agent.tool_calls}tc {dur}"
            )

        return "\n".join(lines)

    def format_agent_detail(self, agent_id: str) -> str:
        """Format detailed view for a single agent.

        Args:
            agent_id: Agent to show details for.

        Returns:
            Formatted detail string.
        """
        agent = self.agents.get(agent_id)
        if agent is None:
            return f"Agent {agent_id} not found."

        lines: list[str] = [
            f"Agent: {agent.name} ({agent.agent_id})",
            f"Status: {agent.status}",
            f"Duration: {agent.duration_seconds:.1f}s",
            f"Tool calls: {agent.tool_calls} ({agent.errors} errors)",
            f"Tokens: {agent.tokens_used:,}",
            f"Cost: ${agent.cost_usd:.4f}",
        ]

        if agent.prompt:
            lines.append(f"\nPrompt: {agent.prompt[:200]}")

        if agent.output_lines:
            lines.append("\nRecent output:")
            lines.extend(
                f"  {line}" for line in agent.output_lines[-20:]
            )

        return "\n".join(lines)


def create_dashboard_layout(state: DashboardState) -> str:
    """Create a text-based dashboard layout.

    For use in terminals without Textual, or as a fallback.

    Args:
        state: Current dashboard state.

    Returns:
        Formatted dashboard text.
    """
    state.update_totals()
    width = 80
    elapsed = time.time() - state.session_start

    lines: list[str] = [
        "=" * width,
        " BOG AGENTS DASHBOARD".center(width),
        "=" * width,
        "",
        (
            f" Agents: {len(state.agents)} | "
            f"Running: {state.running_count} | "
            f"Done: {state.completed_count} | "
            f"Cost: ${state.total_cost_usd:.4f} | "
            f"Time: {elapsed:.0f}s"
        ),
        "-" * width,
        "",
    ]

    agents = sorted(state.agents.values(), key=lambda a: a.agent_id)
    for agent in agents:
        icon = agent.status_icon
        dur = (
            f"{agent.duration_seconds:.0f}s" if agent.started_at else "--"
        )
        lines.extend((
            f" [{icon}] {agent.name} ({agent.agent_id})",
            f"     Status: {agent.status} | Duration: {dur}",
            (
                f"     Tools: {agent.tool_calls} | "
                f"Errors: {agent.errors} | "
                f"Tokens: {agent.tokens_used:,}"
            ),
        ))

        if agent.current_action:
            lines.append(f"     Action: {agent.current_action[:60]}")

        if agent.output_lines:
            last_line = agent.output_lines[-1][:60]
            lines.append(f"     Last: {last_line}")

        lines.append("")

    if not agents:
        lines.extend((" No agents running.", ""))

    lines.append("=" * width)
    return "\n".join(lines)


class DashboardScreen:
    """Live-updating dashboard screen controller.

    Manages a polling timer that refreshes the dashboard display at a
    configurable interval. Designed to integrate with the Textual app
    via a simple callback pattern.

    Example:
        ```python
        screen = DashboardScreen(
            state_builder=build_state,
            on_render=lambda text: mount_message(text),
            interval=2.0,
        )
        screen.start()
        # ... later
        screen.stop()
        ```
    """

    def __init__(
        self,
        *,
        state_builder: Callable[[], DashboardState] | None = None,
        on_render: Callable[[str], object] | None = None,
        interval: float = 2.0,
    ) -> None:
        """Initialize the dashboard screen.

        Args:
            state_builder: Callable that returns a ``DashboardState``.
            on_render: Callback(text) to display the rendered dashboard.
            interval: Refresh interval in seconds.
        """
        self._state_builder = state_builder
        self._on_render = on_render
        self._interval = interval
        self._running = False
        self._timer: object | None = None

    @property
    def is_running(self) -> bool:
        """Whether the dashboard is actively refreshing."""
        return self._running

    def render_once(self) -> str:
        """Build state and render the dashboard once.

        Returns:
            Formatted dashboard text.
        """
        state = (
            self._state_builder()
            if self._state_builder
            else DashboardState()
        )
        return create_dashboard_layout(state)

    def start(
        self,
        set_interval_fn: Callable[..., object] | None = None,
    ) -> str:
        """Start live refresh and return the initial render.

        Args:
            set_interval_fn: Textual's ``set_interval(seconds, callback)``.
                If provided, automatic refresh is enabled.

        Returns:
            Initial dashboard render.
        """
        self._running = True
        if set_interval_fn is not None:
            self._timer = set_interval_fn(self._interval, self._tick)
        return self.render_once()

    def stop(self) -> None:
        """Stop live refresh."""
        self._running = False
        if self._timer is not None:
            if hasattr(self._timer, "stop"):
                self._timer.stop()  # type: ignore[union-attr]
            self._timer = None

    def _tick(self) -> None:
        """Timer callback to refresh the dashboard."""
        if not self._running:
            return
        text = self.render_once()
        if self._on_render:
            self._on_render(text)
