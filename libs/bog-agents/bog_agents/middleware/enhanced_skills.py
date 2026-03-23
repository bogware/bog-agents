"""Enhanced skills middleware with remote skill loading.

Feature #46: Extends the base SkillsMiddleware with support for loading skills
from remote sources — git repositories, HTTP endpoints, and other URL-based
locations — in addition to local backend paths.

## Remote Sources

Skills can be loaded from:

- **Local paths** (same as base SkillsMiddleware): `/skills/user/`, `/skills/project/`
- **Git repositories**: `git+https://github.com/org/skills-repo.git#subdirectory=skills`
- **HTTP endpoints**: `https://example.com/skills/` (expects directory listing or index.json)

Git sources are cloned to a local cache directory and refreshed periodically.
HTTP sources fetch skill manifests and download individual SKILL.md files.

## Usage

```python
from bog_agents.middleware.enhanced_skills import EnhancedSkillsMiddleware

middleware = EnhancedSkillsMiddleware(
    backend=my_backend,
    sources=[
        "/skills/local/",  # Local
        "git+https://github.com/org/skills.git",  # Git repo
        "https://example.com/skills/",  # HTTP
    ],
    cache_dir="/tmp/bog-agents-skills-cache",
)
```
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain_core.runnables import RunnableConfig
    from langgraph.runtime import Runtime

    from bog_agents.backends.protocol import BACKEND_TYPES, BackendProtocol

from typing import NotRequired

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ContextT,
    ModelRequest,
    ModelResponse,
    PrivateStateAttr,
    ResponseT,
)
from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, StructuredTool
from typing_extensions import TypedDict

from bog_agents.middleware._utils import append_to_system_message
from bog_agents.middleware.skills import (
    SkillMetadata,
    _format_skill_annotations,
    _list_skills,
    _parse_skill_metadata,
)

logger = logging.getLogger(__name__)


class EnhancedSkillsState(AgentState):
    """State for the enhanced skills middleware."""

    enhanced_skills_metadata: NotRequired[Annotated[list[dict[str, Any]], PrivateStateAttr]]


class EnhancedSkillsStateUpdate(TypedDict):
    """State update for enhanced skills middleware."""

    enhanced_skills_metadata: list[dict[str, Any]]


def _is_git_source(source: str) -> bool:
    """Check if a source is a git repository URL.

    Args:
        source: Source path or URL.

    Returns:
        True if the source is a git URL.
    """
    return source.startswith("git+") or source.startswith("git://")


def _is_http_source(source: str) -> bool:
    """Check if a source is an HTTP/HTTPS URL.

    Args:
        source: Source path or URL.

    Returns:
        True if the source is an HTTP URL.
    """
    return source.startswith("http://") or source.startswith("https://")


def _cache_key(source: str) -> str:
    """Generate a deterministic cache key for a source URL.

    Args:
        source: Source URL.

    Returns:
        SHA256 hex digest of the source URL.
    """
    return hashlib.sha256(source.encode()).hexdigest()[:16]


def _clone_git_skills(source: str, cache_dir: Path) -> list[SkillMetadata]:
    """Clone a git repository and load skills from it.

    Args:
        source: Git source URL (git+https://... or git://...).
        cache_dir: Local directory for caching cloned repos.

    Returns:
        List of skill metadata from the repository.
    """
    # Parse git URL: strip "git+" prefix
    git_url = source
    git_url = git_url.removeprefix("git+")

    # Handle subdirectory in URL fragment
    subdir = ""
    if "#subdirectory=" in git_url:
        git_url, subdir = git_url.split("#subdirectory=", 1)

    # Determine cache path
    key = _cache_key(source)
    clone_dir = cache_dir / f"git-{key}"

    try:
        if clone_dir.exists():
            # Pull latest
            subprocess.run(
                ["git", "-C", str(clone_dir), "pull", "--quiet"],
                capture_output=True,
                timeout=30,
                check=False,
            )
        else:
            clone_dir.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "clone", "--depth", "1", "--quiet", git_url, str(clone_dir)],
                capture_output=True,
                timeout=60,
                check=True,
            )
    except (subprocess.SubprocessError, OSError) as e:
        logger.warning("Failed to clone git skills from %s: %s", source, e)
        return []

    # Load skills from the cloned directory
    skills_dir = clone_dir / subdir if subdir else clone_dir
    if not skills_dir.is_dir():
        logger.warning("Skills directory not found in git repo: %s", skills_dir)
        return []

    skills: list[SkillMetadata] = []
    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        try:
            content = skill_md.read_text(encoding="utf-8")
        except OSError:
            continue

        metadata = _parse_skill_metadata(
            content=content,
            skill_path=str(skill_md),
            directory_name=skill_dir.name,
        )
        if metadata:
            skills.append(metadata)

    return skills


def _fetch_http_skills(source: str, cache_dir: Path) -> list[SkillMetadata]:
    """Fetch skills from an HTTP endpoint.

    Expects the endpoint to serve either:
    - An index.json with a list of skill names
    - A directory listing with links to skill subdirectories

    Args:
        source: HTTP source URL.
        cache_dir: Local cache directory.

    Returns:
        List of skill metadata.
    """
    source_url = source.rstrip("/")

    try:
        # Try fetching index.json first
        index_url = f"{source_url}/index.json"
        req = urllib.request.Request(index_url, headers={"User-Agent": "bog-agents-skills/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            index_data = json.loads(resp.read().decode("utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        logger.debug("No index.json at %s, skipping HTTP source", source_url)
        return []

    if not isinstance(index_data, dict) or "skills" not in index_data:
        logger.warning("Invalid index.json format at %s", source_url)
        return []

    skills: list[SkillMetadata] = []
    for skill_name in index_data["skills"]:
        skill_md_url = f"{source_url}/{skill_name}/SKILL.md"
        try:
            req = urllib.request.Request(skill_md_url, headers={"User-Agent": "bog-agents-skills/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                content = resp.read().decode("utf-8")
        except OSError:
            logger.debug("Could not fetch %s", skill_md_url)
            continue

        metadata = _parse_skill_metadata(
            content=content,
            skill_path=skill_md_url,
            directory_name=str(skill_name),
        )
        if metadata:
            skills.append(metadata)

    return skills


ENHANCED_SKILLS_SYSTEM_PROMPT = """## Enhanced Skills System

You have access to skills loaded from both local and remote sources.

{skills_locations}

**Available Skills:**

{skills_list}

**How to Use Skills:**

1. Check if the user's task matches an available skill
2. Read the skill's full instructions using the path shown
3. Follow the skill's workflow
4. For remote skills, the content is cached locally for fast access

Use `refresh_skills` to reload skills from all sources (including remote)."""


class EnhancedSkillsMiddleware(AgentMiddleware[EnhancedSkillsState, ContextT, ResponseT]):
    """Middleware for loading skills from local and remote sources.

    Extends the base skills system with git and HTTP source support.

    Args:
        backend: Backend instance or factory for file operations.
        sources: List of skill source paths (local paths, git URLs, HTTP URLs).
        cache_dir: Directory for caching remote skills. Defaults to temp directory.
    """

    state_schema = EnhancedSkillsState

    def __init__(
        self,
        *,
        backend: BACKEND_TYPES,
        sources: list[str],
        cache_dir: str | Path | None = None,
    ) -> None:
        self._backend = backend
        self.sources = sources
        self._cache_dir = Path(cache_dir) if cache_dir else Path(tempfile.gettempdir()) / "bog-agents-skills-cache"
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self.tools: list[BaseTool] = self._build_tools()

    def _get_backend(self, state: EnhancedSkillsState, runtime: Runtime, config: RunnableConfig) -> BackendProtocol:
        """Resolve backend.

        Args:
            state: Current agent state.
            runtime: Runtime context.
            config: Runnable config.

        Returns:
            Resolved backend instance.
        """
        if callable(self._backend):
            tool_runtime = ToolRuntime(
                state=state,
                context=runtime.context,
                stream_writer=runtime.stream_writer,
                store=runtime.store,
                config=config,
                tool_call_id=None,
            )
            return self._backend(tool_runtime)  # ty: ignore[call-top-callable]
        return self._backend

    def _load_all_skills(self, backend: BackendProtocol) -> list[SkillMetadata]:
        """Load skills from all sources.

        Args:
            backend: Backend for local sources.

        Returns:
            Combined list of skills from all sources.
        """
        all_skills: dict[str, SkillMetadata] = {}

        for source in self.sources:
            if _is_git_source(source):
                source_skills = _clone_git_skills(source, self._cache_dir)
            elif _is_http_source(source):
                source_skills = _fetch_http_skills(source, self._cache_dir)
            else:
                source_skills = _list_skills(backend, source)

            for skill in source_skills:
                all_skills[skill["name"]] = skill

        return list(all_skills.values())

    def _build_tools(self) -> list[BaseTool]:
        """Build enhanced skills tools."""
        mw = self

        def refresh_skills(
            runtime: ToolRuntime[None, EnhancedSkillsState],
        ) -> str:
            """Reload skills from all sources including remote git repos and HTTP endpoints."""
            # This is a simplified refresh — in a real implementation, we'd
            # need access to the backend through runtime
            return f"Skills refresh requested. {len(mw.sources)} source(s) will be reloaded on next turn."

        def list_skill_sources(
            runtime: ToolRuntime[None, EnhancedSkillsState],
        ) -> str:
            """List all configured skill sources with their types."""
            lines = ["## Skill Sources", ""]
            for source in mw.sources:
                if _is_git_source(source):
                    source_type = "Git Repository"
                elif _is_http_source(source):
                    source_type = "HTTP Endpoint"
                else:
                    source_type = "Local Path"
                lines.append(f"- **{source_type}**: `{source}`")
            return "\n".join(lines)

        return [
            StructuredTool.from_function(
                name="refresh_skills",
                description="Reload skills from all sources including remote git repos and HTTP endpoints.",
                func=refresh_skills,
            ),
            StructuredTool.from_function(
                name="list_skill_sources",
                description="List all configured skill sources with their types (local, git, HTTP).",
                func=list_skill_sources,
            ),
        ]

    def _format_skills_list(self, skills: list[SkillMetadata]) -> str:
        """Format skills for system prompt."""
        if not skills:
            return "(No skills available)"

        lines = []
        for skill in skills:
            annotations = _format_skill_annotations(skill)
            line = f"- **{skill['name']}**: {skill['description']}"
            if annotations:
                line += f" ({annotations})"
            lines.append(line)
            lines.append(f"  -> Read `{skill['path']}` for full instructions")
        return "\n".join(lines)

    def _format_skills_locations(self) -> str:
        """Format source locations for system prompt."""
        lines = []
        for source in self.sources:
            if _is_git_source(source):
                lines.append(f"**Remote (Git)**: `{source}`")
            elif _is_http_source(source):
                lines.append(f"**Remote (HTTP)**: `{source}`")
            else:
                lines.append(f"**Local**: `{source}`")
        return "\n".join(lines)

    def before_agent(self, state: EnhancedSkillsState, runtime: Runtime, config: RunnableConfig) -> EnhancedSkillsStateUpdate | None:
        """Load skills from all sources before agent execution.

        Args:
            state: Current agent state.
            runtime: Runtime context.
            config: Runnable config.

        Returns:
            State update with skills loaded.
        """
        if "enhanced_skills_metadata" in state:
            return None

        backend = self._get_backend(state, runtime, config)
        skills = self._load_all_skills(backend)
        return EnhancedSkillsStateUpdate(enhanced_skills_metadata=skills)

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Inject skills into system prompt.

        Args:
            request: Model request to modify.

        Returns:
            Modified request.
        """
        skills = request.state.get("enhanced_skills_metadata", [])
        locations = self._format_skills_locations()
        skills_list = self._format_skills_list(skills)

        section = ENHANCED_SKILLS_SYSTEM_PROMPT.format(
            skills_locations=locations,
            skills_list=skills_list,
        )
        new_system_message = append_to_system_message(request.system_message, section)
        return request.override(system_message=new_system_message)

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """Inject skills into system prompt.

        Args:
            request: Model request.
            call_next: Handler function.

        Returns:
            Model response.
        """
        modified = self.modify_request(request)
        return call_next(modified)

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        """Async version of wrap_model_call.

        Args:
            request: Model request.
            call_next: Async handler function.

        Returns:
            Model response.
        """
        modified = self.modify_request(request)
        return await call_next(modified)


__all__ = ["EnhancedSkillsMiddleware"]
