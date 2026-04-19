"""Interactive builder helpers for skills, prompts, and pipelines."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_VAR_RE = re.compile(r"\{\{([A-Z_][A-Z0-9_]*)\}\}")
_PROMPT_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def extract_variables(text: str) -> list[str]:
    """Find all ``{{VAR_NAME}}`` patterns in text.

    Matches uppercase variable names enclosed in double curly braces.

    Args:
        text: The text to search for variable placeholders.

    Returns:
        Sorted, deduplicated list of variable names found in the text.
    """
    return sorted(set(_VAR_RE.findall(text)))


def build_skill_template(
    name: str,
    description: str,
    body: str,
    *,
    variables: list[str],
) -> str:
    """Build SKILL.md content for a new skill.

    Args:
        name: Skill name, must match ``^[a-z][a-z0-9-]{0,62}$``.
        description: Short description of what the skill does.
        body: The body content (instructions) for the skill.
        variables: List of variable names to document.

    Returns:
        Full SKILL.md content string.

    Raises:
        ValueError: If `name` does not match the required pattern.
    """
    if not _NAME_RE.match(name):
        msg = (
            f"Invalid skill name {name!r}. "
            "Must match ^[a-z][a-z0-9-]{0,62}$ (lowercase letters, digits, hyphens)."
        )
        raise ValueError(msg)

    lines = [
        f"# {name}",
        "",
        description,
        "",
        "## Usage",
        "",
        body,
    ]

    if variables:
        lines.extend(
            [
                "",
                "## Variables",
                "",
            ]
        )
        for var in variables:
            lines.append(f"- {{{{{var}}}}}: description")

    return "\n".join(lines) + "\n"


def build_prompt_entry(name: str, description: str, body: str) -> dict[str, Any]:
    """Build a prompt library entry dict.

    Args:
        name: Prompt name (alphanumeric, underscores, hyphens).
        description: Short description of what the prompt does.
        body: The prompt body text (may include ``{{VARIABLE}}`` placeholders).

    Returns:
        Dict suitable for the prompt library with keys ``name``, ``description``,
        ``body``, and ``variables``.

    Raises:
        ValueError: If `name` is empty or contains invalid characters.
    """
    if not name or not _PROMPT_NAME_RE.match(name):
        msg = (
            f"Invalid prompt name {name!r}. "
            "Must be non-empty and contain only alphanumeric characters, underscores, or hyphens."
        )
        raise ValueError(msg)

    return {
        "name": name,
        "description": description,
        "body": body,
        "variables": extract_variables(body),
    }


def build_pipeline_yaml(
    name: str,
    description: str,
    steps: list[dict[str, str]],
    *,
    schedule: str = "",
) -> str:
    """Build a pipeline YAML string.

    Args:
        name: Pipeline name.
        description: Short description of the pipeline.
        steps: List of step dicts, each with keys ``label``, ``type``,
            and ``content``. ``type`` must be one of ``"prompt"``,
            ``"skill"``, or ``"slash"``.
        schedule: Optional cron schedule string; omitted from output when empty.

    Returns:
        YAML string for the pipeline definition.
    """
    lines = [
        f"name: {name}",
        f"description: {description}",
    ]
    if schedule:
        lines.append(f"schedule: {schedule}")
    lines.append("steps:")

    for step in steps:
        label = step.get("label", "Step")
        step_type = step.get("type", "prompt")
        content = step.get("content", "")
        # Indent content lines for block scalar
        indented = (
            "\n".join(f"        {line}" for line in content.splitlines())
            if content
            else "        "
        )
        lines.extend(
            [
                f"  - label: {label}",
                f"    type: {step_type}",
                "    content: |",
                indented,
            ]
        )

    return "\n".join(lines) + "\n"


def save_skill(name: str, content: str, *, user_skills_dir: Path) -> Path:
    """Save skill content to ``user_skills_dir/name/SKILL.md``.

    Args:
        name: Skill directory name.
        content: Full SKILL.md content to write.
        user_skills_dir: Parent directory for user skills.

    Returns:
        Path to the written SKILL.md file.

    Raises:
        FileExistsError: If the skill directory already exists and SKILL.md is non-empty.
    """
    skill_dir = user_skills_dir / name
    skill_md = skill_dir / "SKILL.md"

    if skill_dir.exists() and skill_md.exists() and skill_md.stat().st_size > 0:
        msg = f"Skill {name!r} already exists at {skill_md}. Delete the directory or use a different name."
        raise FileExistsError(msg)

    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md.write_text(content, encoding="utf-8")
    return skill_md


def save_prompt(entry: dict[str, Any], *, library_path: Path) -> None:
    """Add or update a prompt entry in the TOML prompt library.

    Loads the existing library (or starts with an empty dict), adds/updates
    the entry under ``[prompts.{name}]``, and writes back atomically.

    Args:
        entry: Prompt dict with keys ``name``, ``description``, ``body``,
            and ``variables``.
        library_path: Path to the ``prompt_library.toml`` file.
    """
    from bog_agents_cli.io_utils import atomic_write_text

    name = entry["name"]

    # Load existing data
    raw: dict[str, Any] = {}
    if library_path.exists():
        try:
            with library_path.open("rb") as fh:
                raw = tomllib.load(fh)
        except Exception:
            raw = {}

    prompts = raw.setdefault("prompts", {})
    prompts[name] = {
        "description": entry.get("description", ""),
        "body": entry["body"],
        "variables": entry.get("variables", []),
    }

    # Serialise back to TOML text using tomli_w
    import tomli_w  # type: ignore[import-untyped]

    toml_text = tomli_w.dumps(raw)
    atomic_write_text(library_path, toml_text)


def save_pipeline(name: str, yaml_content: str, *, pipelines_dir: Path) -> Path:
    """Write a pipeline YAML file.

    Args:
        name: Pipeline name (used as the filename stem).
        yaml_content: Full YAML string to write.
        pipelines_dir: Directory to write pipelines into.

    Returns:
        Path to the written YAML file.

    Raises:
        FileExistsError: If a pipeline file with this name already exists.
    """
    pipelines_dir.mkdir(parents=True, exist_ok=True)
    pipeline_path = pipelines_dir / f"{name}.yaml"
    if pipeline_path.exists():
        msg = f"Pipeline {name!r} already exists at {pipeline_path}. Delete the file or use a different name."
        raise FileExistsError(msg)
    pipeline_path.write_text(yaml_content, encoding="utf-8")
    return pipeline_path


def format_build_help() -> str:
    """Return Rich markup usage string for the /build command.

    Returns:
        Formatted help text with Rich markup.
    """
    return (
        "[bold]/build[/bold] — Interactive wizard for creating skills, prompts, and pipelines\n\n"
        "[bold]Usage:[/bold]\n"
        "  [cyan]/build skill[/cyan] [dim]<name> [description][/dim]   — Scaffold a new skill with AI-generated body\n"
        "  [cyan]/build prompt[/cyan] [dim]<name> [description][/dim]  — Create a saved prompt with variable support\n"
        "  [cyan]/build pipeline[/cyan] [dim]<name> [--schedule 'cron'][/dim]  — Design a multi-step automation pipeline\n"
        "  [cyan]/build save[/cyan]                          — Save the last agent-generated content\n\n"
        "[bold]Examples:[/bold]\n"
        "  [dim]/build skill web-research Search the web and summarize results[/dim]\n"
        "  [dim]/build prompt code-review Structured code review for {{LANGUAGE}} files[/dim]\n"
        "  [dim]/build pipeline daily-standup --schedule '0 9 * * 1-5'[/dim]\n"
        "  [dim]/build save[/dim]  ← after reviewing the agent's output\n\n"
        "[bold]Workflow:[/bold]\n"
        "  1. Run [cyan]/build skill <name>[/cyan] — the agent writes the skill body\n"
        "  2. Review the agent's output\n"
        "  3. Run [cyan]/build save[/cyan] to persist it to [dim]~/.bog-agents/skills/[/dim]"
    )


def preview_skill(
    name: str,
    description: str,
    body: str,
    variables: list[str],
) -> str:
    """Return a formatted preview string for a skill.

    Args:
        name: Skill name.
        description: Skill description.
        body: Skill body content.
        variables: List of variable names.

    Returns:
        Formatted preview string showing how the skill will look.
    """
    lines = [
        "─" * 40,
        f"SKILL PREVIEW: {name}",
        "─" * 40,
        f"Description: {description}",
        "",
        "Body:",
        body,
    ]
    if variables:
        lines.extend(
            [
                "",
                f"Variables: {', '.join(variables)}",
            ]
        )
    lines.append("─" * 40)
    return "\n".join(lines)
