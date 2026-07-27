"""Back-compat shim — the sandbox spec loader moved to the SDK (#27).

`.bog-agents/sandbox.toml` handling now lives in `bog_agents.sandbox_config` so
the daemon and fleet runner consume the same spec the CLI does (it previously
had zero consumers). Import from `bog_agents.sandbox_config` directly in new
code; this module re-exports the public surface for existing importers.
"""

from __future__ import annotations

from bog_agents.sandbox_config import (
    SANDBOX_NETWORK_ALLOWLIST_ENV,
    SandboxConfig,
    SandboxSetup,
    load_sandbox_config,
    resolve_sandbox_setup,
)

__all__ = [
    "SANDBOX_NETWORK_ALLOWLIST_ENV",
    "SandboxConfig",
    "SandboxSetup",
    "load_sandbox_config",
    "resolve_sandbox_setup",
]
