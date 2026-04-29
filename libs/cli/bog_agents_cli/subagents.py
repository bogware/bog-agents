"""Subagent loader for CLI.

Loads custom subagent definitions from the filesystem. Subagents are defined
as markdown files with YAML frontmatter in the agents/ directory.

Directory structure:
    .bog-agents/agents/{agent_name}/AGENTS.md

Example file (researcher/AGENTS.md):
    ---
    name: researcher
    description: Research topics on the web before writing content
    model: anthropic:claude-haiku-4-5-20251001
    ---

    You are a research assistant with access to web search.

    ## Your Process
    1. Search for relevant information
    2. Summarize findings clearly
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, TypedDict

import yaml

if TYPE_CHECKING:
    from pathlib import Path


class SubagentMetadata(TypedDict):
    """Metadata for a custom subagent loaded from filesystem."""

    name: str
    """Unique identifier for the subagent, used with the task tool."""

    description: str
    """What this subagent does. Main agent uses this to decide when to delegate."""

    system_prompt: str
    """Instructions for the subagent (body of the markdown file)."""

    model: str | None
    """Optional model override in 'provider:model-name' format."""

    source: str
    """Where this subagent was loaded from ('user' or 'project')."""

    path: str
    """Absolute path to the subagent definition file."""


def _parse_subagent_file(file_path: Path) -> SubagentMetadata | None:
    """Parse a subagent markdown file with YAML frontmatter.

    The file must have YAML frontmatter (delimited by ---) containing at minimum
    'name' and 'description' fields. The body of the file becomes the system_prompt.

    Args:
        file_path: Path to the markdown file.

    Returns:
        SubagentMetadata if parsing succeeds, None otherwise.
    """
    try:
        content = file_path.read_text(encoding="utf-8")
    except OSError:
        return None

    # Extract YAML frontmatter (--- delimited)
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", content, re.DOTALL)
    if not match:
        return None

    try:
        frontmatter = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None

    # Validate frontmatter structure and required fields
    if not isinstance(frontmatter, dict):
        return None

    name = frontmatter.get("name")
    description = frontmatter.get("description")
    model = frontmatter.get("model")

    # Validate types: name and description must be non-empty strings
    # model is optional but must be string if present
    name_valid = isinstance(name, str) and name
    description_valid = isinstance(description, str) and description
    model_valid = model is None or isinstance(model, str)

    if not (name_valid and description_valid and model_valid):
        return None

    return {
        "name": name,
        "description": description,
        "system_prompt": match.group(2).strip(),
        "model": model,
        "source": "",  # Set by caller
        "path": str(file_path),
    }


def _load_subagents_from_dir(
    agents_dir: Path, source: str
) -> dict[str, SubagentMetadata]:
    """Load subagents from a directory.

    Expects structure: agents_dir/{subagent_name}/AGENTS.md

    Args:
        agents_dir: Directory containing subagent folders.
        source: Source identifier ('user' or 'project').

    Returns:
        Dict mapping subagent name to metadata.
    """
    subagents: dict[str, SubagentMetadata] = {}

    if not agents_dir.exists() or not agents_dir.is_dir():
        return subagents

    for folder in agents_dir.iterdir():
        if not folder.is_dir():
            continue

        # Look for {folder_name}/AGENTS.md
        subagent_file = folder / "AGENTS.md"
        if not subagent_file.exists():
            continue

        subagent = _parse_subagent_file(subagent_file)
        if subagent:
            subagent["source"] = source
            subagents[subagent["name"]] = subagent

    return subagents


_PROJECT_TYPE_INDICATORS: tuple[tuple[str, str], ...] = (
    ("pyproject.toml", "python"),
    ("setup.py", "python"),
    ("package.json", "node"),
    ("Cargo.toml", "rust"),
    ("go.mod", "go"),
)


def detect_project_language(project_root: Path | None) -> str | None:
    """Detect a project's primary language from indicator files.

    Used to surface the matching set of bundled subagents (Python, Node,
    Rust, Go) without the user authoring AGENTS.md by hand.

    Args:
        project_root: Project root to inspect. None disables detection.

    Returns:
        One of `'python'`, `'node'`, `'rust'`, `'go'`, or None when no
        indicator is found.
    """
    if project_root is None:
        return None
    for indicator, language in _PROJECT_TYPE_INDICATORS:
        if (project_root / indicator).is_file():
            return language
    return None


def get_bundled_agents_dir(language: str) -> Path | None:
    """Return the bundled-agents directory for *language*, if any.

    Args:
        language: Language label returned by `detect_project_language`.

    Returns:
        Path to the bundled subagents directory, or None when the
        language has no bundled defaults.
    """
    bundled_root = Path(__file__).parent / "bundled_agents" / language
    return bundled_root if bundled_root.is_dir() else None


def list_subagents(
    *,
    user_agents_dir: Path | None = None,
    project_agents_dir: Path | None = None,
    project_root: Path | None = None,
    include_bundled: bool = True,
) -> list[SubagentMetadata]:
    """List subagents from user and/or project directories.

    Precedence (low → high): bundled defaults < user agents < project agents.
    A subagent name defined at a higher precedence level overrides any
    same-named entry at a lower level — so users can shadow a bundled
    `code-reviewer` simply by writing their own AGENTS.md with the same
    `name:` field.

    Args:
        user_agents_dir: Path to user-level agents directory.
        project_agents_dir: Path to project-level agents directory.
        project_root: Project root used to detect language for the
            bundled-agents library. None disables bundled loading.
        include_bundled: When True (default), seed the listing with the
            bundled defaults that match the project language. Set False
            to get the legacy "only what's on disk" behavior.

    Returns:
        List of subagent metadata, with later sources overriding earlier
        ones on name conflict.
    """
    all_subagents: dict[str, SubagentMetadata] = {}

    # Bundled defaults (lowest priority): seed the project with sensible
    # subagents (code-reviewer, test-author, refactorer/react-ink-artist
    # depending on language) on first use.
    if include_bundled:
        language = detect_project_language(project_root)
        if language:
            bundled_dir = get_bundled_agents_dir(language)
            if bundled_dir is not None:
                all_subagents.update(_load_subagents_from_dir(bundled_dir, "bundled"))

    # User subagents
    if user_agents_dir is not None:
        all_subagents.update(_load_subagents_from_dir(user_agents_dir, "user"))

    # Project subagents (highest priority — overrides bundled + user)
    if project_agents_dir is not None:
        all_subagents.update(_load_subagents_from_dir(project_agents_dir, "project"))

    return list(all_subagents.values())
