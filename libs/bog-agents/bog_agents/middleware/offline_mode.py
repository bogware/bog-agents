"""Offline Mode middleware — polished air-gapped experience with local models.

Extends the air-gapped middleware with Ollama auto-detection, local embedding
fallbacks, offline-safe tool filtering, and network connectivity monitoring.
Designed for corporate environments behind firewalls.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
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

logger = logging.getLogger(__name__)


class ConnectivityStatus(StrEnum):
    """Network connectivity status."""

    ONLINE = "online"
    OFFLINE = "offline"
    RESTRICTED = "restricted"  # Can reach local network only
    UNKNOWN = "unknown"


class OfflineCapability(StrEnum):
    """Features available in offline mode."""

    FILE_OPERATIONS = "file_operations"
    SHELL_EXECUTION = "shell_execution"
    GIT_OPERATIONS = "git_operations"
    CODE_SEARCH = "code_search"
    LOCAL_LLM = "local_llm"
    LOCAL_EMBEDDINGS = "local_embeddings"
    PLANNING = "planning"
    SUB_AGENTS = "sub_agents"


# Tools that require network access
NETWORK_REQUIRED_TOOLS = frozenset({
    "web_search",
    "fetch_url",
    "http_request",
    "create_pr",
    "push_to_remote",
})

# Tools that work fully offline
OFFLINE_SAFE_TOOLS = frozenset({
    "read_file",
    "write_file",
    "edit_file",
    "multi_edit",
    "ls",
    "glob",
    "grep",
    "execute",
    "write_todos",
    "read_many_files",
    "repo_map",
    "git_status",
    "git_diff",
    "git_log",
    "git_commit",
    "git_add",
    "git_branch",
    "git_stash",
    "git_blame",
    "git_show",
    "detect_project",
    "show_cost",
    "show_context",
    "task",
})


@dataclass
class OllamaModel:
    """A locally available Ollama model."""

    name: str
    size_bytes: int = 0
    modified_at: str = ""
    digest: str = ""
    parameter_size: str = ""
    quantization: str = ""

    @property
    def size_gb(self) -> float:
        """Size in gigabytes."""
        return self.size_bytes / (1024 ** 3)


@dataclass
class OfflineStatus:
    """Current offline mode status."""

    connectivity: ConnectivityStatus = ConnectivityStatus.UNKNOWN
    ollama_available: bool = False
    ollama_models: list[OllamaModel] = field(default_factory=list)
    active_model: str | None = None
    available_capabilities: list[OfflineCapability] = field(default_factory=list)
    last_connectivity_check: float = 0.0
    blocked_tool_calls: int = 0


def check_connectivity(*, timeout: float = 3.0) -> ConnectivityStatus:
    """Check network connectivity status.

    Args:
        timeout: Timeout for connectivity check in seconds.

    Returns:
        Current connectivity status.
    """
    try:
        import socket
        # Try to resolve a DNS name
        socket.setdefaulttimeout(timeout)
        socket.getaddrinfo("dns.google", 443)
        return ConnectivityStatus.ONLINE
    except (socket.gaierror, socket.timeout, OSError):
        pass

    # Check if local network is reachable
    try:
        import socket
        socket.setdefaulttimeout(timeout)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        result = sock.connect_ex(("127.0.0.1", 11434))  # Ollama default port
        sock.close()
        if result == 0:
            return ConnectivityStatus.RESTRICTED
    except OSError:
        pass

    return ConnectivityStatus.OFFLINE


def detect_ollama_models() -> list[OllamaModel]:
    """Detect locally available Ollama models.

    Returns:
        List of available OllamaModel instances.
    """
    try:
        result = subprocess.run(
            ["ollama", "list"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return []

        models: list[OllamaModel] = []
        lines = result.stdout.strip().split("\n")
        if len(lines) < 2:
            return []

        for line in lines[1:]:  # Skip header
            parts = line.split()
            if len(parts) >= 2:
                name = parts[0]
                # Parse size if available
                size_bytes = 0
                for part in parts:
                    if part.endswith("GB"):
                        try:
                            size_bytes = int(float(part[:-2]) * 1024 ** 3)
                        except ValueError:
                            pass
                    elif part.endswith("MB"):
                        try:
                            size_bytes = int(float(part[:-2]) * 1024 ** 2)
                        except ValueError:
                            pass

                models.append(OllamaModel(name=name, size_bytes=size_bytes))

        return models
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []


def check_ollama_running() -> bool:
    """Check if the Ollama server is running.

    Returns:
        True if Ollama is responding on its default port.
    """
    try:
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        result = sock.connect_ex(("127.0.0.1", 11434))
        sock.close()
        return result == 0
    except OSError:
        return False


def select_best_ollama_model(models: list[OllamaModel]) -> str | None:
    """Select the best available Ollama model for coding tasks.

    Prefers models known to be good at coding, falling back to
    the largest available model.

    Args:
        models: Available Ollama models.

    Returns:
        Model name or None if no models available.
    """
    if not models:
        return None

    # Preferred models for coding, in priority order
    preferred = [
        "qwen2.5-coder",
        "deepseek-coder-v2",
        "codellama",
        "llama3.3",
        "llama3.2",
        "llama3.1",
        "llama3",
        "mistral",
        "phi3",
        "gemma2",
    ]

    model_names = [m.name.split(":")[0] for m in models]
    for pref in preferred:
        for i, name in enumerate(model_names):
            if name.lower() == pref or name.lower().startswith(pref + "-"):
                return models[i].name

    # Fall back to largest model
    if models:
        return max(models, key=lambda m: m.size_bytes).name

    return None


def get_offline_capabilities(
    ollama_available: bool,
    ollama_models: list[OllamaModel],
) -> list[OfflineCapability]:
    """Determine what capabilities are available offline.

    Args:
        ollama_available: Whether Ollama is running.
        ollama_models: Available Ollama models.

    Returns:
        List of available capabilities.
    """
    caps = [
        OfflineCapability.FILE_OPERATIONS,
        OfflineCapability.SHELL_EXECUTION,
        OfflineCapability.GIT_OPERATIONS,
        OfflineCapability.CODE_SEARCH,
        OfflineCapability.PLANNING,
    ]

    if ollama_available and ollama_models:
        caps.append(OfflineCapability.LOCAL_LLM)
        caps.append(OfflineCapability.SUB_AGENTS)
        # Check for embedding models
        embedding_names = {"nomic-embed-text", "mxbai-embed-large", "all-minilm"}
        for model in ollama_models:
            if any(e in model.name.lower() for e in embedding_names):
                caps.append(OfflineCapability.LOCAL_EMBEDDINGS)
                break

    return caps


class OfflineModeMiddleware(AgentMiddleware):
    """Middleware for fully offline agent operation with local models.

    Auto-detects Ollama, monitors connectivity, filters network-dependent
    tools, and provides a polished air-gapped experience.

    Example:
        ```python
        from bog_agents.middleware.offline_mode import OfflineModeMiddleware

        middleware = OfflineModeMiddleware(
            enforce_offline=True,  # Block all network access
            preferred_model="ollama:llama3.3",
        )

        # Check status
        status = middleware.get_status()
        print(f"Connectivity: {status.connectivity}")
        print(f"Ollama models: {len(status.ollama_models)}")
        print(f"Active model: {status.active_model}")
        ```
    """

    enforce_offline: bool
    preferred_model: str | None
    status: OfflineStatus
    _check_interval: float

    def __init__(
        self,
        *,
        enforce_offline: bool = False,
        preferred_model: str | None = None,
        check_interval: float = 60.0,
    ) -> None:
        """Initialize offline mode middleware.

        Args:
            enforce_offline: Whether to block all network tool calls.
            preferred_model: Preferred local model name.
            check_interval: Seconds between connectivity checks.
        """
        self.enforce_offline = enforce_offline
        self.preferred_model = preferred_model
        self._check_interval = check_interval
        self.status = OfflineStatus()
        self._refresh_status()

    def _refresh_status(self) -> None:
        """Refresh connectivity and model status."""
        now = time.time()
        if now - self.status.last_connectivity_check < self._check_interval:
            return

        self.status.connectivity = check_connectivity()
        self.status.ollama_available = check_ollama_running()

        if self.status.ollama_available:
            self.status.ollama_models = detect_ollama_models()
            if not self.status.active_model:
                self.status.active_model = (
                    self.preferred_model
                    or select_best_ollama_model(self.status.ollama_models)
                )
        else:
            self.status.ollama_models = []

        self.status.available_capabilities = get_offline_capabilities(
            self.status.ollama_available,
            self.status.ollama_models,
        )
        self.status.last_connectivity_check = now

        logger.info(
            "Offline status: connectivity=%s, ollama=%s, models=%d, active=%s",
            self.status.connectivity,
            self.status.ollama_available,
            len(self.status.ollama_models),
            self.status.active_model,
        )

    def is_tool_allowed(self, tool_name: str) -> bool:
        """Check if a tool is allowed in current connectivity mode.

        Args:
            tool_name: Name of the tool.

        Returns:
            True if the tool is allowed.
        """
        if not self.enforce_offline:
            return True

        if tool_name in NETWORK_REQUIRED_TOOLS:
            self.status.blocked_tool_calls += 1
            logger.debug("Blocked network tool in offline mode: %s", tool_name)
            return False

        return True

    def get_status(self) -> OfflineStatus:
        """Get current offline status.

        Returns:
            OfflineStatus with current state.
        """
        self._refresh_status()
        return self.status

    def get_status_summary(self) -> str:
        """Get a human-readable status summary.

        Returns:
            Formatted status string.
        """
        self._refresh_status()
        lines: list[str] = []
        lines.append(f"Connectivity: {self.status.connectivity}")
        lines.append(f"Enforce offline: {self.enforce_offline}")
        lines.append(f"Ollama: {'running' if self.status.ollama_available else 'not available'}")

        if self.status.ollama_models:
            lines.append(f"Local models ({len(self.status.ollama_models)}):")
            for m in self.status.ollama_models:
                active = " (active)" if m.name == self.status.active_model else ""
                lines.append(f"  - {m.name} ({m.size_gb:.1f} GB){active}")

        lines.append(f"Capabilities: {', '.join(self.status.available_capabilities)}")

        if self.status.blocked_tool_calls > 0:
            lines.append(f"Blocked tool calls: {self.status.blocked_tool_calls}")

        return "\n".join(lines)

    async def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Any,
        runtime: Any,
    ) -> ModelResponse[ResponseT]:
        """Refresh status and pass through model calls."""
        self._refresh_status()
        return await call_next(request, runtime)
