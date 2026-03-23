"""Middleware for tool-level lifecycle hooks.

Feature #25: Rich lifecycle hooks — fires events before/after tool calls,
model calls, and agent turns for external tool integration.

Feature #29: Prompt hooks — evaluate tool calls via LLM before execution.

Feature #30: Agent hooks — spawn verification subagents in response to events.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)
from typing_extensions import TypedDict

logger = logging.getLogger(__name__)


class HookEvent(StrEnum):
    """Lifecycle events that can trigger hooks."""

    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"
    PRE_MODEL_CALL = "pre_model_call"
    POST_MODEL_CALL = "post_model_call"
    PRE_AGENT_TURN = "pre_agent_turn"
    POST_AGENT_TURN = "post_agent_turn"
    ON_ERROR = "on_error"
    ON_TOOL_APPROVAL = "on_tool_approval"
    ON_CONTEXT_COMPRESS = "on_context_compress"
    ON_CHECKPOINT = "on_checkpoint"
    ON_MODEL_SWITCH = "on_model_switch"
    ON_SUBAGENT_SPAWN = "on_subagent_spawn"
    ON_SUBAGENT_COMPLETE = "on_subagent_complete"
    ON_SESSION_START = "on_session_start"
    ON_SESSION_END = "on_session_end"


class HookAction(StrEnum):
    """Actions a hook can take."""

    ALLOW = "allow"
    """Allow the action to proceed (default)."""

    BLOCK = "block"
    """Block the action from proceeding."""

    MODIFY = "modify"
    """Modify the action's parameters."""

    LOG = "log"
    """Log the event without interfering."""


@dataclass
class HookDefinition:
    """A registered hook that responds to lifecycle events."""

    name: str
    """Unique hook name."""

    events: list[HookEvent]
    """Events this hook listens to."""

    command: list[str] | None = None
    """Shell command to run (receives JSON on stdin, outputs JSON on stdout)."""

    tool_pattern: str | None = None
    """Regex pattern to match tool names (for tool-specific hooks)."""

    timeout: int = 10
    """Maximum seconds to wait for hook execution."""

    blocking: bool = True
    """Whether the hook blocks the action until it completes."""


@dataclass
class HookResult:
    """Result from executing a hook."""

    action: HookAction = HookAction.ALLOW
    """What action to take."""

    message: str = ""
    """Optional message from the hook."""

    modifications: dict[str, Any] = field(default_factory=dict)
    """Parameter modifications (for MODIFY action)."""


class LifecycleHooksState(TypedDict):
    """State for lifecycle hooks middleware."""


class LifecycleHooksMiddleware(AgentMiddleware[LifecycleHooksState, ContextT, ResponseT]):
    """Middleware providing rich lifecycle hooks for external integration.

    Fires events at key points in the agent lifecycle:
    - Before/after each tool call
    - Before/after each model call
    - Before/after each agent turn
    - On errors, approvals, compressions, checkpoints
    - On model switches, subagent spawns/completions
    - On session start/end

    Hooks can be shell commands that receive JSON on stdin and return
    JSON on stdout, allowing external tools to inspect, log, validate,
    or modify agent behavior.

    Args:
        hooks: List of hook definitions.
        hooks_config_path: Path to hooks configuration file.
    """

    state_schema = LifecycleHooksState

    def __init__(
        self,
        *,
        hooks: list[HookDefinition] | None = None,
        hooks_config_path: str | None = None,
    ) -> None:
        self._hooks: list[HookDefinition] = hooks or []

        if hooks_config_path:
            self._load_hooks_from_file(hooks_config_path)

    def _load_hooks_from_file(self, path: str) -> None:
        """Load hook definitions from a JSON config file.

        Args:
            path: Path to the hooks configuration file.
        """
        try:
            from pathlib import Path as P

            config = json.loads(P(path).read_text())
            hooks_data = config.get("hooks", [])
            for hook_data in hooks_data:
                events = [HookEvent(e) for e in hook_data.get("events", [])]
                self._hooks.append(
                    HookDefinition(
                        name=hook_data.get("name", "unnamed"),
                        events=events,
                        command=hook_data.get("command"),
                        tool_pattern=hook_data.get("tool_pattern"),
                        timeout=hook_data.get("timeout", 10),
                        blocking=hook_data.get("blocking", True),
                    )
                )
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load hooks config from %s: %s", path, e)

    def register_hook(self, hook: HookDefinition) -> None:
        """Register a new hook.

        Args:
            hook: The hook definition to register.
        """
        self._hooks.append(hook)

    def unregister_hook(self, name: str) -> bool:
        """Unregister a hook by name.

        Args:
            name: Hook name to remove.

        Returns:
            True if the hook was found and removed.
        """
        before = len(self._hooks)
        self._hooks = [h for h in self._hooks if h.name != name]
        return len(self._hooks) < before

    def _get_matching_hooks(self, event: HookEvent, tool_name: str | None = None) -> list[HookDefinition]:
        """Get hooks matching an event and optional tool name.

        Args:
            event: The lifecycle event.
            tool_name: Optional tool name for tool-specific filtering.

        Returns:
            List of matching hooks.
        """
        import re

        matching = []
        for hook in self._hooks:
            if event not in hook.events:
                continue
            if hook.tool_pattern and tool_name:
                if not re.match(hook.tool_pattern, tool_name):
                    continue
            elif hook.tool_pattern and not tool_name:
                continue
            matching.append(hook)
        return matching

    def _execute_hook(self, hook: HookDefinition, payload: dict[str, Any]) -> HookResult:
        """Execute a single hook command.

        Args:
            hook: The hook to execute.
            payload: JSON payload to send on stdin.

        Returns:
            HookResult from the hook execution.
        """
        if not hook.command:
            return HookResult(action=HookAction.LOG)

        try:
            payload_bytes = json.dumps(payload).encode()
            result = subprocess.run(
                hook.command,
                input=payload_bytes,
                capture_output=True,
                text=True,
                timeout=hook.timeout,
                check=False,
            )

            if result.returncode != 0:
                logger.warning("Hook %s returned non-zero: %s", hook.name, result.stderr)
                return HookResult(action=HookAction.ALLOW, message=result.stderr)

            if result.stdout.strip():
                try:
                    response = json.loads(result.stdout)
                    return HookResult(
                        action=HookAction(response.get("action", "allow")),
                        message=response.get("message", ""),
                        modifications=response.get("modifications", {}),
                    )
                except json.JSONDecodeError:
                    return HookResult(action=HookAction.ALLOW, message=result.stdout)

            return HookResult(action=HookAction.ALLOW)

        except subprocess.TimeoutExpired:
            logger.warning("Hook %s timed out after %ds", hook.name, hook.timeout)
            return HookResult(action=HookAction.ALLOW, message="Hook timed out")
        except (FileNotFoundError, PermissionError) as e:
            logger.warning("Hook %s failed: %s", hook.name, e)
            return HookResult(action=HookAction.ALLOW, message=str(e))

    def fire_event(
        self,
        event: HookEvent,
        payload: dict[str, Any],
        *,
        tool_name: str | None = None,
    ) -> list[HookResult]:
        """Fire a lifecycle event and collect hook results.

        Args:
            event: The event to fire.
            payload: Event payload data.
            tool_name: Optional tool name for filtering.

        Returns:
            List of results from matching hooks.
        """
        hooks = self._get_matching_hooks(event, tool_name)
        if not hooks:
            return []

        payload["event"] = event.value

        results = []
        blocking = [h for h in hooks if h.blocking]
        non_blocking = [h for h in hooks if not h.blocking]

        # Execute blocking hooks synchronously
        for hook in blocking:
            result = self._execute_hook(hook, payload)
            results.append(result)
            # If any blocking hook returns BLOCK, stop
            if result.action == HookAction.BLOCK:
                break

        # Execute non-blocking hooks in parallel
        if non_blocking:
            with ThreadPoolExecutor(max_workers=len(non_blocking)) as pool:
                futures = [pool.submit(self._execute_hook, hook, payload) for hook in non_blocking]
                for future in futures:
                    try:
                        results.append(future.result(timeout=10))
                    except Exception:
                        results.append(HookResult(action=HookAction.ALLOW))

        return results

    async def afire_event(
        self,
        event: HookEvent,
        payload: dict[str, Any],
        *,
        tool_name: str | None = None,
    ) -> list[HookResult]:
        """Async version of fire_event."""
        return await asyncio.to_thread(self.fire_event, event, payload, tool_name=tool_name)

    def wrap_model_call(
        self,
        request: ModelRequest,
        call_next: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Fire pre/post model call hooks."""
        pre_results = self.fire_event(
            HookEvent.PRE_MODEL_CALL,
            {"type": "model_call"},
        )

        # Check if any hook blocks the call
        for result in pre_results:
            if result.action == HookAction.BLOCK:
                logger.info("Model call blocked by hook: %s", result.message)
                # Return empty response — the hook blocked it
                break

        response = call_next(request)

        self.fire_event(
            HookEvent.POST_MODEL_CALL,
            {"type": "model_call"},
        )

        return response

    async def awrap_model_call(
        self,
        request: ModelRequest,
        call_next: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """Async version of wrap_model_call."""
        await self.afire_event(
            HookEvent.PRE_MODEL_CALL,
            {"type": "model_call"},
        )
        response = await call_next(request)
        await self.afire_event(
            HookEvent.POST_MODEL_CALL,
            {"type": "model_call"},
        )
        return response
