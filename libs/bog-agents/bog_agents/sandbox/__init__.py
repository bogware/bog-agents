"""OS-level sandboxing for local command execution.

Feature #2: OS-level sandboxing — provides bubblewrap (Linux) and
seatbelt (macOS) based sandboxing for the LocalShellBackend.

This prevents the agent from accessing files outside the working directory,
making network requests, or performing other dangerous operations when
running in local (non-remote) mode.
"""

from bog_agents.sandbox.egress_proxy import (
    SANDBOX_EGRESS_PROXY_ENV,
    AllowlistEgressProxy,
    egress_env_for,
    host_allowed,
    parse_connect_target,
)
from bog_agents.sandbox.local_sandbox import (
    LocalSandbox,
    SandboxLevel,
    create_local_sandbox,
    get_platform_sandbox_support,
    sandbox_launcher_available,
    wrap_command_with_sandbox,
)

__all__ = [
    "SANDBOX_EGRESS_PROXY_ENV",
    "AllowlistEgressProxy",
    "LocalSandbox",
    "SandboxLevel",
    "create_local_sandbox",
    "egress_env_for",
    "get_platform_sandbox_support",
    "host_allowed",
    "parse_connect_target",
    "sandbox_launcher_available",
    "wrap_command_with_sandbox",
]
