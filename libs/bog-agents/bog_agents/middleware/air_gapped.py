"""On-premise / air-gapped deployment middleware.

Feature #23: Local model management and data flow policies for air-gapped
environments.

## Egress enforcement (best-effort)

In addition to injecting policy instructions into the system prompt, this
middleware installs a `wrap_tool_call` / `awrap_tool_call` egress gate that
intercepts a *known* set of egress vectors before they execute:

- Tools whose name contains `web_fetch`, `fetch_url`, or `http_request`.
- Shell / execute tools whose command string looks networked
  (`curl`, `wget`, `nc`/`netcat`, `ssh`, `scp`, `telnet`, or a bare
  `http(s)://` URL).

For each intercepted call it extracts the target host and consults
`AirGapStore.check_allowed`, **denying** the call (returning an error
`ToolMessage`) whenever the policy does not allow it. The gate fails CLOSED:
when the target host cannot be determined for a recognised egress tool, the
call is denied rather than passed through.

This is **best-effort defense-in-depth over KNOWN egress vectors, not a hard
guarantee**. Egress vectors not on the lists above (custom tools, indirect
network access, DNS side channels, etc.) are not covered, so the gate must be
paired with a real network sandbox for adversarial isolation.

## Tools

- `register_local_model`: Register a local model endpoint
- `set_data_policy`: Configure data flow policies
- `check_data_flow`: Check if an external request is allowed
- `air_gap_status`: View current air-gap configuration status
- `clear_air_gap`: Reset all air-gap configuration

## Usage

```python
from bog_agents.middleware.air_gapped import AirGappedMiddleware

middleware = AirGappedMiddleware()
```
"""

from __future__ import annotations

import logging
import re
import shlex
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated, Any
from urllib.parse import urlsplit

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)
from langchain.tools import ToolRuntime
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
from typing_extensions import TypedDict

from bog_agents.middleware._utils import append_to_system_message

logger = logging.getLogger(__name__)


@dataclass
class LocalModel:
    """A locally deployed model endpoint.

    Attributes:
        name: Model name.
        endpoint: Local endpoint URL.
        model_type: Type of model (llm, embedding, reranker).
        is_available: Whether the model is currently available.
    """

    name: str
    endpoint: str
    model_type: str = "llm"
    is_available: bool = True


@dataclass
class DataPolicy:
    """Data flow policy for air-gapped environments.

    Attributes:
        allow_external: Whether external network access is allowed.
        allowed_domains: List of domains that are explicitly allowed.
        blocked_patterns: List of patterns to block in outgoing data.
        audit_external: Whether to audit all external access attempts.
    """

    allow_external: bool = False
    allowed_domains: list[str] = field(default_factory=list)
    blocked_patterns: list[str] = field(default_factory=list)
    audit_external: bool = True


@dataclass
class AirGapStore:
    """In-memory store for air-gap configuration.

    Attributes:
        models: Registered local models keyed by name.
        policy: Active data flow policy.
        external_attempts: Log of external access attempts.
    """

    models: dict[str, LocalModel] = field(default_factory=dict)
    policy: DataPolicy = field(default_factory=DataPolicy)
    external_attempts: list[dict[str, str]] = field(default_factory=list)

    def register_model(
        self,
        name: str,
        endpoint: str,
        model_type: str = "llm",
        is_available: bool = True,
    ) -> LocalModel:
        """Register a local model endpoint.

        Args:
            name: Model name.
            endpoint: Local endpoint URL.
            model_type: Type of model.
            is_available: Whether the model is available.

        Returns:
            The registered local model.
        """
        model = LocalModel(
            name=name,
            endpoint=endpoint,
            model_type=model_type,
            is_available=is_available,
        )
        self.models[name] = model
        return model

    def set_policy(
        self,
        allow_external: bool | None = None,
        allowed_domains: list[str] | None = None,
        blocked_patterns: list[str] | None = None,
        audit_external: bool | None = None,
    ) -> DataPolicy:
        """Update the data flow policy.

        Args:
            allow_external: Whether to allow external access.
            allowed_domains: Allowed domain list.
            blocked_patterns: Blocked data patterns.
            audit_external: Whether to audit external attempts.

        Returns:
            Updated data policy.
        """
        if allow_external is not None:
            self.policy.allow_external = allow_external
        if allowed_domains is not None:
            self.policy.allowed_domains = allowed_domains
        if blocked_patterns is not None:
            self.policy.blocked_patterns = blocked_patterns
        if audit_external is not None:
            self.policy.audit_external = audit_external
        return self.policy

    def check_allowed(self, domain: str, data: str = "") -> tuple[bool, str]:
        """Check if an external request is allowed by policy.

        Args:
            domain: Target domain.
            data: Outgoing data to check against blocked patterns.

        Returns:
            Tuple of (is_allowed, reason).
        """
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime())

        if not self.policy.allow_external:
            reason = "External access is disabled"
            if self.policy.audit_external:
                self.external_attempts.append({"domain": domain, "allowed": "false", "reason": reason, "timestamp": timestamp})
            return False, reason

        if self.policy.allowed_domains and domain not in self.policy.allowed_domains:
            reason = f"Domain '{domain}' is not in the allowed list"
            if self.policy.audit_external:
                self.external_attempts.append({"domain": domain, "allowed": "false", "reason": reason, "timestamp": timestamp})
            return False, reason

        for pattern in self.policy.blocked_patterns:
            if pattern.lower() in data.lower():
                reason = f"Data contains blocked pattern: '{pattern}'"
                if self.policy.audit_external:
                    self.external_attempts.append({"domain": domain, "allowed": "false", "reason": reason, "timestamp": timestamp})
                return False, reason

        if self.policy.audit_external:
            self.external_attempts.append({"domain": domain, "allowed": "true", "reason": "Passed all checks", "timestamp": timestamp})
        return True, "Allowed"

    def format_status(self) -> str:
        """Format the current air-gap status for display.

        Returns:
            Formatted status string.
        """
        lines = [
            "## Air-Gap Deployment Status",
            "",
            "### Data Policy",
            f"  External Access: {'ALLOWED' if self.policy.allow_external else 'BLOCKED'}",
            f"  Audit External:  {'ON' if self.policy.audit_external else 'OFF'}",
            f"  Allowed Domains: {', '.join(self.policy.allowed_domains) if self.policy.allowed_domains else 'None'}",
            f"  Blocked Patterns: {', '.join(self.policy.blocked_patterns) if self.policy.blocked_patterns else 'None'}",
            "",
            f"### Local Models ({len(self.models)})",
        ]
        if self.models:
            for model in self.models.values():
                status = "AVAILABLE" if model.is_available else "UNAVAILABLE"
                lines.append(f"  - {model.name} ({model.model_type}): {model.endpoint} [{status}]")
        else:
            lines.append("  No local models registered.")

        lines.append("")
        lines.append(f"### External Access Attempts: {len(self.external_attempts)}")
        for attempt in self.external_attempts[-5:]:
            lines.append(f"  - {attempt['domain']}: {attempt['allowed']} ({attempt['reason']}) at {attempt['timestamp']}")

        return "\n".join(lines)


AIR_GAPPED_SYSTEM_PROMPT = """## Air-Gapped Deployment Tools

You have access to tools for managing on-premise / air-gapped deployments.

**Available Tools:**
- `register_local_model`: Register local model endpoints
- `set_data_policy`: Configure data flow restrictions
- `check_data_flow`: Verify if external requests are allowed
- `air_gap_status`: View current deployment configuration
- `clear_air_gap`: Reset air-gap settings

**Guidelines:**
- Always check data flow policies before making external requests
- Prefer local models over external ones in air-gapped mode
- Audit logs track all external access attempts for compliance"""


AIR_GAPPED_PINNED_SYSTEM_PROMPT = """## Air-Gapped Deployment (operator-enforced)

You are operating under an operator-pinned data-flow policy. External network
egress is governed by that policy and you CANNOT change it — `set_data_policy`
and `clear_air_gap` are operator-only controls and are not available to you.

**Available Tools:**
- `register_local_model`: Register local model endpoints
- `check_data_flow`: Verify whether an external request is allowed by the policy
- `air_gap_status`: View the current deployment configuration and audit log

Prefer local models; every external attempt is audited for compliance."""


# Tool-name substrings that identify a direct egress (network-fetch) tool.
_EGRESS_TOOL_NAME_MARKERS: tuple[str, ...] = ("web_fetch", "fetch_url", "http_request")

# Tool-name substrings that identify a shell / command-execution tool whose
# argument string must be inspected for networked commands.
_SHELL_TOOL_NAME_MARKERS: tuple[str, ...] = ("shell", "execute", "bash", "run_command", "command")

# Networked command names that, when present in a shell command, indicate egress.
_NETWORK_COMMANDS: frozenset[str] = frozenset({"curl", "wget", "nc", "ncat", "netcat", "ssh", "scp", "sftp", "telnet"})

# Common tool-call argument keys that may carry a URL or host.
_URL_ARG_KEYS: tuple[str, ...] = ("url", "uri", "endpoint", "address", "host", "target", "link")

# Common tool-call argument keys that may carry a shell command string.
_COMMAND_ARG_KEYS: tuple[str, ...] = ("command", "cmd", "script", "input", "code")

_URL_RE = re.compile(r"\bhttps?://[^\s'\"<>|]+", re.IGNORECASE)


def _host_from_url(value: str) -> str | None:
    """Extract the host from a URL-like string.

    Args:
        value: A candidate URL (with or without a scheme).

    Returns:
        The lower-cased hostname, or None if no host could be parsed.
    """
    candidate = value.strip()
    if not candidate:
        return None
    if "://" not in candidate:
        candidate = f"//{candidate}"
    try:
        host = urlsplit(candidate).hostname
    except ValueError:
        return None
    return host.lower() if host else None


def _looks_networked_command(command: str) -> bool:
    """Return whether a shell command string appears to perform network egress.

    Args:
        command: The raw command string.

    Returns:
        True if the command invokes a known network tool or embeds a URL.
    """
    if _URL_RE.search(command):
        return True
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = command.split()
    for token in tokens:
        # Strip any leading path (e.g. /usr/bin/curl -> curl).
        base = token.rsplit("/", 1)[-1].lower()
        if base in _NETWORK_COMMANDS:
            return True
    return False


def _host_from_command(command: str) -> str | None:
    """Best-effort extraction of the target host from a networked command.

    Args:
        command: The raw command string.

    Returns:
        The target host if one can be determined, else None.
    """
    url_match = _URL_RE.search(command)
    if url_match:
        host = _host_from_url(url_match.group(0))
        if host:
            return host
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = command.split()
    # For ssh/scp/sftp/telnet/nc, the host is typically the first non-flag,
    # non-command token; e.g. `ssh user@example.com`, `nc example.com 443`.
    seen_command = False
    for token in tokens:
        base = token.rsplit("/", 1)[-1].lower()
        if not seen_command:
            if base in _NETWORK_COMMANDS:
                seen_command = True
            continue
        if token.startswith("-"):
            continue
        candidate = token.split("@", 1)[-1]  # drop user@ prefix
        host = _host_from_url(candidate)
        if host:
            return host
    return None


class AirGappedState(TypedDict):
    """State for air-gapped middleware."""


class AirGappedMiddleware(AgentMiddleware[AirGappedState, ContextT, ResponseT]):
    """Middleware for on-premise and air-gapped deployment management.

    Provides tools for registering local models, configuring data flow
    policies, and auditing external access attempts.
    """

    state_schema = AirGappedState

    def __init__(self, *, policy: DataPolicy | None = None) -> None:
        """Initialize the air-gap middleware.

        Args:
            policy: An operator-owned data-flow policy. When provided, the
                middleware runs in operator-pinned mode: the policy governs
                egress and the model-callable `set_data_policy` / `clear_air_gap`
                tools are withheld, so a jailbroken model cannot lift its own
                restrictions (MW-SAFE-1). When omitted, the legacy self-service
                behavior applies (default fail-closed policy, but the model can
                mutate it) — intended only for cooperative experimentation.
        """
        self.store = AirGapStore()
        self._operator_pinned = policy is not None
        if policy is not None:
            self.store.policy = policy
        self.tools: list[BaseTool] = self._build_tools()

    def _build_tools(self) -> list[BaseTool]:
        """Build air-gapped deployment tools.

        In operator-pinned mode the policy-mutation tools (`set_data_policy`,
        `clear_air_gap`) are withheld from the model.
        """
        mw = self

        def register_local_model(
            runtime: ToolRuntime[None, AirGappedState],
            name: Annotated[str, "Model name"],
            endpoint: Annotated[str, "Local endpoint URL"],
            model_type: Annotated[str, "Model type: llm, embedding, reranker"] = "llm",
            is_available: Annotated[str, "Whether model is available (true/false)"] = "true",
        ) -> str:
            """Register a local model endpoint for air-gapped operation."""
            available = is_available.lower() in ("true", "yes", "1", "on")
            model = mw.store.register_model(
                name=name,
                endpoint=endpoint,
                model_type=model_type,
                is_available=available,
            )
            status = "AVAILABLE" if model.is_available else "UNAVAILABLE"
            return f"Registered local model '{model.name}' ({model.model_type}) at {model.endpoint} [{status}]. Total models: {len(mw.store.models)}"

        def set_data_policy(
            runtime: ToolRuntime[None, AirGappedState],
            allow_external: Annotated[str, "Allow external access (true/false)"] = "",
            allowed_domains: Annotated[str, "Comma-separated allowed domains"] = "",
            blocked_patterns: Annotated[str, "Comma-separated blocked data patterns"] = "",
            audit_external: Annotated[str, "Audit external attempts (true/false)"] = "",
        ) -> str:
            """Configure data flow policies for air-gapped operation."""
            ext = None
            if allow_external:
                ext = allow_external.lower() in ("true", "yes", "1", "on")
            domains = [d.strip() for d in allowed_domains.split(",") if d.strip()] if allowed_domains else None
            patterns = [p.strip() for p in blocked_patterns.split(",") if p.strip()] if blocked_patterns else None
            audit = None
            if audit_external:
                audit = audit_external.lower() in ("true", "yes", "1", "on")
            policy = mw.store.set_policy(
                allow_external=ext,
                allowed_domains=domains,
                blocked_patterns=patterns,
                audit_external=audit,
            )
            return f"Data policy updated: External={'ALLOWED' if policy.allow_external else 'BLOCKED'} | Domains={len(policy.allowed_domains)} | Patterns={len(policy.blocked_patterns)} | Audit={'ON' if policy.audit_external else 'OFF'}"

        def check_data_flow(
            runtime: ToolRuntime[None, AirGappedState],
            domain: Annotated[str, "Target domain to check"],
            data: Annotated[str, "Outgoing data to check against blocked patterns"] = "",
        ) -> str:
            """Check if an external request is allowed by the current data policy."""
            allowed, reason = mw.store.check_allowed(domain=domain, data=data)
            status = "ALLOWED" if allowed else "BLOCKED"
            return f"Data flow check: {domain} -> {status} ({reason})"

        def air_gap_status(
            runtime: ToolRuntime[None, AirGappedState],
        ) -> str:
            """View current air-gap configuration and deployment status."""
            return mw.store.format_status()

        def clear_air_gap(
            runtime: ToolRuntime[None, AirGappedState],
        ) -> str:
            """Reset all air-gap configuration."""
            models = len(mw.store.models)
            attempts = len(mw.store.external_attempts)
            mw.store = AirGapStore()
            return f"Cleared air-gap config: {models} models, {attempts} audit entries."

        read_and_register_tools = [
            StructuredTool.from_function(
                name="register_local_model", description="Register a local model endpoint for air-gapped operation.", func=register_local_model
            ),
            StructuredTool.from_function(
                name="check_data_flow", description="Check if an external request is allowed by the data policy.", func=check_data_flow
            ),
            StructuredTool.from_function(
                name="air_gap_status", description="View current air-gap deployment configuration and audit log.", func=air_gap_status
            ),
        ]

        if self._operator_pinned:
            # The model cannot mutate an operator-pinned policy.
            return read_and_register_tools

        return [
            *read_and_register_tools,
            StructuredTool.from_function(
                name="set_data_policy", description="Configure data flow restrictions and audit settings.", func=set_data_policy
            ),
            StructuredTool.from_function(name="clear_air_gap", description="Reset all air-gap configuration and audit data.", func=clear_air_gap),
        ]

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Inject air-gapped deployment instructions.

        Args:
            request: Model request to modify.

        Returns:
            Modified request.
        """
        prompt = AIR_GAPPED_PINNED_SYSTEM_PROMPT if self._operator_pinned else AIR_GAPPED_SYSTEM_PROMPT
        return request.override(system_message=append_to_system_message(request.system_message, prompt))

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """Inject air-gapped deployment instructions.

        Args:
            request: Model request.
            call_next: Handler.

        Returns:
            Model response.
        """
        return call_next(self.modify_request(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        """Async version.

        Args:
            request: Model request.
            call_next: Async handler.

        Returns:
            Model response.
        """
        return await call_next(self.modify_request(request))

    def _evaluate_egress(self, request: ToolCallRequest) -> tuple[bool, str, str]:
        """Decide whether a tool call is a recognised egress and, if so, allowed.

        This inspects only KNOWN egress vectors (see the module docstring). For
        recognised vectors the gate fails CLOSED: if the target host cannot be
        determined, the call is denied.

        Args:
            request: The incoming tool-call request.

        Returns:
            A tuple `(is_egress, allowed, reason)`. When `is_egress` is False the
            other fields are unset and the caller must pass the call through.
        """
        tool_call = request.tool_call or {}
        name = str(tool_call.get("name", "")).lower()
        args = tool_call.get("args") or {}
        if not isinstance(args, dict):
            args = {}

        # Direct network-fetch tools (web_fetch / fetch_url / http_request).
        if any(marker in name for marker in _EGRESS_TOOL_NAME_MARKERS):
            host = self._host_from_args(args)
            if host is None:
                return True, False, "Air-gap egress blocked: could not determine target host for network tool (fail-closed)."
            allowed, reason = self.store.check_allowed(domain=host, data=self._data_blob(args))
            return True, allowed, f"Air-gap egress to '{host}': {reason}"

        # Shell / execute tools carrying a networked command string.
        if any(marker in name for marker in _SHELL_TOOL_NAME_MARKERS):
            command = self._command_from_args(args)
            if command and _looks_networked_command(command):
                host = _host_from_command(command)
                if host is None:
                    return True, False, "Air-gap egress blocked: networked command with no resolvable host (fail-closed)."
                allowed, reason = self.store.check_allowed(domain=host, data=command)
                return True, allowed, f"Air-gap egress to '{host}': {reason}"

        return False, True, ""

    @staticmethod
    def _host_from_args(args: dict[str, Any]) -> str | None:
        """Extract a target host from common URL-bearing argument keys.

        Args:
            args: The tool-call argument mapping.

        Returns:
            The target host, or None if none could be determined.
        """
        for key in _URL_ARG_KEYS:
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                host = _host_from_url(value)
                if host:
                    return host
        return None

    @staticmethod
    def _command_from_args(args: dict[str, Any]) -> str:
        """Extract a shell command string from common command-bearing keys.

        Args:
            args: The tool-call argument mapping.

        Returns:
            The command string, or an empty string if none was found.
        """
        for key in _COMMAND_ARG_KEYS:
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return ""

    @staticmethod
    def _data_blob(args: dict[str, Any]) -> str:
        """Concatenate string argument values for blocked-pattern scanning.

        Args:
            args: The tool-call argument mapping.

        Returns:
            A single string joining all string-valued arguments.
        """
        return " ".join(str(v) for v in args.values() if isinstance(v, str))

    def _make_deny_message(self, request: ToolCallRequest, reason: str) -> ToolMessage:
        """Build the egress-denied `ToolMessage` for a blocked call.

        Args:
            request: The denied tool-call request.
            reason: Human-readable denial reason.

        Returns:
            A `ToolMessage` with `status="error"` carrying the denial reason.
        """
        tool_call = request.tool_call or {}
        return ToolMessage(
            content=reason,
            tool_call_id=str(tool_call.get("id", "")),
            name=str(tool_call.get("name", "")),
            status="error",
        )

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Any],
    ) -> ToolMessage | Any:
        """Deny disallowed egress tool calls; otherwise pass through.

        Best-effort: only KNOWN egress vectors are intercepted (see the module
        docstring). Recognised egress fails CLOSED when not policy-allowed.

        Args:
            request: The incoming tool-call request.
            handler: The downstream tool-call handler.

        Returns:
            An egress-denied `ToolMessage` for blocked calls, else the handler's
            result.
        """
        is_egress, allowed, reason = self._evaluate_egress(request)
        if is_egress and not allowed:
            return self._make_deny_message(request, reason)
        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Any]],
    ) -> ToolMessage | Any:
        """Async version of `wrap_tool_call`.

        Args:
            request: The incoming tool-call request.
            handler: The downstream async tool-call handler.

        Returns:
            An egress-denied `ToolMessage` for blocked calls, else the handler's
            result.
        """
        is_egress, allowed, reason = self._evaluate_egress(request)
        if is_egress and not allowed:
            return self._make_deny_message(request, reason)
        return await handler(request)


__all__ = ["AirGapStore", "AirGappedMiddleware", "DataPolicy", "LocalModel"]
