"""OS-level sandboxing for local command execution.

Feature #2: OS-level sandboxing — provides bubblewrap (Linux) and
seatbelt (macOS) based sandboxing for the LocalShellBackend.

This prevents the agent from accessing files outside the working directory,
making network requests, or performing other dangerous operations when
running in local (non-remote) mode.
"""

from bog_agents.sandbox.local_sandbox import (
    LocalSandbox,
    SandboxLevel,
    create_local_sandbox,
    get_platform_sandbox_support,
    wrap_command_with_sandbox,
)

__all__ = [
    "LocalSandbox",
    "SandboxLevel",
    "create_local_sandbox",
    "get_platform_sandbox_support",
    "wrap_command_with_sandbox",
]
