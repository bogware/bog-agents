"""Multi-model and local model CLI interface.

Feature #58: Multi-model consensus.
Feature #72: Local model support (Ollama, llama.cpp).
Feature #73: Cost optimizer — route to cheapest capable model.
"""

from __future__ import annotations

import logging
import subprocess  # noqa: S404
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class LocalModelInfo:
    """Information about a locally available model."""

    name: str
    size: str = ""
    modified: str = ""
    provider: str = "ollama"


def detect_local_models() -> list[LocalModelInfo]:
    """Detect locally available models (Ollama, llama.cpp).

    Returns:
        List of detected local models.
    """
    models: list[LocalModelInfo] = []

    # Check Ollama
    try:
        result = subprocess.run(
            ["ollama", "list"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n")[1:]:  # Skip header
                parts = line.split()
                if parts:
                    models.append(
                        LocalModelInfo(
                            name=parts[0],
                            size=parts[2] if len(parts) > 2 else "",
                            modified=parts[3] if len(parts) > 3 else "",
                            provider="ollama",
                        )
                    )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    return models


def format_model_list(
    models: list[LocalModelInfo],
    cloud_models: list[dict[str, str]] | None = None,
) -> str:
    """Format model list for display.

    Args:
        models: Local models.
        cloud_models: Optional cloud model info.

    Returns:
        Formatted string.
    """
    lines = []

    if models:
        lines.append("Local Models:")
        for m in models:
            lines.append(f"  {m.provider}:{m.name} ({m.size})")
    else:
        lines.append("No local models detected. Install Ollama: https://ollama.com")

    if cloud_models:
        lines.append("\nCloud Models:")
        for m in cloud_models:
            lines.append(
                f"  {m.get('provider', '')}:{m.get('name', '')} — {m.get('tier', '')}"
            )

    return "\n".join(lines)


def parse_model_route_command(text: str) -> dict[str, str]:
    """Parse a /model-route command.

    Subcommands:
    - /model-route auto — enable auto-routing
    - /model-route local — prefer local models
    - /model-route cheap — prefer cheapest models
    - /model-route quality — prefer highest quality

    Args:
        text: Command text.

    Returns:
        Parsed command dict.
    """
    parts = text.strip().split(maxsplit=1)
    strategy = parts[0] if parts else "auto"
    return {"strategy": strategy}


def recommend_model_for_task(task: str, local_available: bool = False) -> str:
    """Recommend a model for a given task.

    Args:
        task: Task description.
        local_available: Whether local models are available.

    Returns:
        Model recommendation.
    """
    task_lower = task.lower()

    # Complex tasks need frontier models
    complex_keywords = [
        "architect",
        "refactor entire",
        "security audit",
        "performance optimize",
        "design system",
    ]
    if any(kw in task_lower for kw in complex_keywords):
        return "anthropic:claude-opus-4-6"

    # Simple tasks can use cheap/local models
    simple_keywords = ["format", "rename", "add comment", "fix typo", "lint"]
    if any(kw in task_lower for kw in simple_keywords):
        if local_available:
            return "ollama:llama3"
        return "anthropic:claude-haiku-4-5"

    # Default: standard tier
    return "anthropic:claude-sonnet-4-6"
