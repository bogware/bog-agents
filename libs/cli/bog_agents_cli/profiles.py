"""Named configuration profiles for the CLI.

Feature #49: /profile command — named configuration profiles (e.g., "review",
"refactor", "debug") that set model, tools, system prompt, effort in one switch.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class Profile:
    """A named configuration profile."""

    name: str
    """Profile name (e.g., 'review', 'refactor', 'debug')."""

    description: str = ""
    """Human-readable description."""

    model: str | None = None
    """Model override for this profile."""

    effort_level: str | None = None
    """Effort level override (low/medium/high/max)."""

    system_prompt_append: str | None = None
    """Additional system prompt text to append."""

    auto_approve: bool | None = None
    """Override auto-approve setting."""

    enable_git_tools: bool | None = None
    """Whether to enable git tools."""

    enable_repo_map: bool | None = None
    """Whether to enable repo map."""

    auto_lint: bool | None = None
    """Whether to enable auto-lint."""

    auto_test: bool | None = None
    """Whether to enable auto-test."""

    plan_mode: bool | None = None
    """Whether to start in plan mode."""

    safe_tools: list[str] | None = None
    """List of additional safe tool names."""

    custom_settings: dict[str, Any] = field(default_factory=dict)
    """Additional custom settings."""


# Built-in profiles
BUILT_IN_PROFILES: dict[str, Profile] = {
    "review": Profile(
        name="review",
        description="Code review mode — read-only exploration with review focus",
        effort_level="high",
        plan_mode=True,
        enable_repo_map=True,
        enable_git_tools=True,
        system_prompt_append=(
            "\n\n## Code Review Mode\n"
            "You are in code review mode. Focus on:\n"
            "1. Finding bugs, logic errors, and edge cases\n"
            "2. Identifying security vulnerabilities\n"
            "3. Checking for missing tests\n"
            "4. Evaluating code quality and maintainability\n"
            "5. Suggesting improvements\n"
            "Use git_diff to see changes and repo_map for context."
        ),
    ),
    "refactor": Profile(
        name="refactor",
        description="Refactoring mode — careful restructuring with safety checks",
        effort_level="high",
        enable_git_tools=True,
        enable_repo_map=True,
        auto_lint=True,
        auto_test=True,
        system_prompt_append=(
            "\n\n## Refactoring Mode\n"
            "You are in refactoring mode. Focus on:\n"
            "1. Improving code structure without changing behavior\n"
            "2. Running tests after each change to verify correctness\n"
            "3. Making small, incremental changes\n"
            "4. Using git_commit after each successful refactoring step"
        ),
    ),
    "debug": Profile(
        name="debug",
        description="Debugging mode — thorough investigation with execution access",
        effort_level="max",
        enable_git_tools=True,
        auto_approve=False,
        system_prompt_append=(
            "\n\n## Debug Mode\n"
            "You are in debugging mode. Focus on:\n"
            "1. Reproducing the issue first\n"
            "2. Adding diagnostic output to understand the problem\n"
            "3. Forming hypotheses and testing them\n"
            "4. Fixing the root cause, not just symptoms\n"
            "5. Verifying the fix with tests"
        ),
    ),
    "quick": Profile(
        name="quick",
        description="Quick mode — fast responses with minimal overhead",
        effort_level="low",
        auto_approve=True,
        enable_repo_map=False,
    ),
    "careful": Profile(
        name="careful",
        description="Careful mode — thorough analysis with all safety checks",
        effort_level="max",
        auto_approve=False,
        enable_git_tools=True,
        enable_repo_map=True,
        auto_lint=True,
        auto_test=True,
        plan_mode=True,
    ),
}


def load_profiles(config_dir: Path) -> dict[str, Profile]:
    """Load custom profiles from the config directory.

    Args:
        config_dir: Path to the config directory (e.g., ~/.bog-agents/).

    Returns:
        Dict mapping profile names to Profile instances. Built-in profiles
        are included and can be overridden by user-defined ones.
    """
    profiles = dict(BUILT_IN_PROFILES)

    profiles_file = config_dir / "profiles.json"
    if profiles_file.exists():
        try:
            data = json.loads(profiles_file.read_text(encoding="utf-8"))
            for name, profile_data in data.items():
                profiles[name] = Profile(
                    name=name,
                    description=profile_data.get("description", ""),
                    model=profile_data.get("model"),
                    effort_level=profile_data.get("effort_level"),
                    system_prompt_append=profile_data.get("system_prompt_append"),
                    auto_approve=profile_data.get("auto_approve"),
                    enable_git_tools=profile_data.get("enable_git_tools"),
                    enable_repo_map=profile_data.get("enable_repo_map"),
                    auto_lint=profile_data.get("auto_lint"),
                    auto_test=profile_data.get("auto_test"),
                    plan_mode=profile_data.get("plan_mode"),
                    safe_tools=profile_data.get("safe_tools"),
                    custom_settings=profile_data.get("custom_settings", {}),
                )
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load profiles from %s: %s", profiles_file, e)

    return profiles


def list_profiles(config_dir: Path) -> str:
    """List all available profiles.

    Args:
        config_dir: Path to the config directory.

    Returns:
        Formatted string listing all profiles.
    """
    profiles = load_profiles(config_dir)
    lines = ["## Available Profiles\n"]
    for name, profile in sorted(profiles.items()):
        marker = " (built-in)" if name in BUILT_IN_PROFILES else " (custom)"
        lines.append(f"- **{name}**{marker}: {profile.description}")
        if profile.model:
            lines.append(f"  Model: {profile.model}")
        if profile.effort_level:
            lines.append(f"  Effort: {profile.effort_level}")
    return "\n".join(lines)


def save_profile(config_dir: Path, profile: Profile) -> None:
    """Save a custom profile to disk.

    Args:
        config_dir: Path to the config directory.
        profile: Profile to save.
    """
    profiles_file = config_dir / "profiles.json"
    existing: dict[str, Any] = {}

    if profiles_file.exists():
        try:
            existing = json.loads(profiles_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    existing[profile.name] = {
        "description": profile.description,
        "model": profile.model,
        "effort_level": profile.effort_level,
        "system_prompt_append": profile.system_prompt_append,
        "auto_approve": profile.auto_approve,
        "enable_git_tools": profile.enable_git_tools,
        "enable_repo_map": profile.enable_repo_map,
        "auto_lint": profile.auto_lint,
        "auto_test": profile.auto_test,
        "plan_mode": profile.plan_mode,
        "safe_tools": profile.safe_tools,
        "custom_settings": profile.custom_settings,
    }

    config_dir.mkdir(parents=True, exist_ok=True)
    profiles_file.write_text(json.dumps(existing, indent=2))
