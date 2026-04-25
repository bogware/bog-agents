"""Team Shared Config & Context for developer collaboration.

Manages a project-level team configuration committed to git so all team
members automatically receive shared settings, context, and prompts when
they pull the repository.

File layout::

    .bog-agents/
    └── team/
        ├── config.json          # Machine-readable team config (this module)
        ├── context/             # Shared context injected into every session
        │   └── *.md
        ├── prompts/             # Named shared prompt library
        │   └── *.md
        └── skills/              # Shared skills available to all members

User-level identity (NOT committed)::

    ~/.bog-agents/
    └── identity.json            # Who the current user is

Usage::

    from bog_agents_cli.team_config import load_team_config, save_team_config

    cfg = load_team_config(Path("/my/project"))
    context_text = get_shared_context_text(cfg, Path("/my/project"))
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

_VALID_ROLES = frozenset({"owner", "admin", "member", "readonly"})
_TEAM_DIR_NAME = ".bog-agents/team"


@dataclass
class TeamMemberRecord:
    """A human team member with an email identity."""

    name: str
    email: str
    role: str = "member"  # owner | admin | member | readonly
    joined_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        """Normalize role to a valid value."""
        if self.role not in _VALID_ROLES:
            self.role = "member"


@dataclass
class TeamSettings:
    """Project-wide default settings for all team members."""

    shared_context_auto_inject: bool = True  # inject team context automatically
    require_approval: bool = False  # always run in plan mode
    model: str = ""  # override default model for team


@dataclass
class TeamContextConfig:
    """Which context files are auto-injected."""

    always_include: list[str] = field(default_factory=list)  # relative to team dir
    description: str = ""


@dataclass
class TeamSharedConfig:
    """Root team configuration stored in .bog-agents/team/config.json."""

    name: str = ""
    description: str = ""
    members: list[TeamMemberRecord] = field(default_factory=list)
    settings: TeamSettings = field(default_factory=TeamSettings)
    context: TeamContextConfig = field(default_factory=TeamContextConfig)
    prompts: dict[str, str] = field(default_factory=dict)  # name → text
    vars: dict[str, str] = field(default_factory=dict)  # key → default value
    mcp_servers: dict[str, dict] = field(default_factory=dict)  # name → server def
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


@dataclass
class UserIdentity:
    """Current user's identity stored in ~/.bog-agents/identity.json."""

    name: str = ""
    email: str = ""
    role: str = "member"


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def get_team_dir(project_root: Path) -> Path:
    """Return the team config directory for a project."""
    return project_root / _TEAM_DIR_NAME


def get_team_config_path(project_root: Path) -> Path:
    """Return the team config file path."""
    return get_team_dir(project_root) / "config.json"


def load_team_config(project_root: Path) -> TeamSharedConfig | None:
    """Load team config from disk.

    Args:
        project_root: Project root directory.

    Returns:
        Loaded config or None if no team config exists.
    """
    path = get_team_config_path(project_root)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    settings_data = payload.get("settings", {})
    context_data = payload.get("context", {})

    members = [
        TeamMemberRecord(
            name=str(m.get("name", "")),
            email=str(m.get("email", "")),
            role=str(m.get("role", "member")),
            joined_at=float(m.get("joined_at", time.time())),
        )
        for m in payload.get("members", [])
        if isinstance(m, dict) and m.get("email")
    ]

    return TeamSharedConfig(
        name=str(payload.get("name", "")),
        description=str(payload.get("description", "")),
        members=members,
        settings=TeamSettings(
            shared_context_auto_inject=bool(
                settings_data.get("shared_context_auto_inject", True)
            ),
            require_approval=bool(settings_data.get("require_approval", False)),
            model=str(settings_data.get("model", "")),
        ),
        context=TeamContextConfig(
            always_include=list(context_data.get("always_include", [])),
            description=str(context_data.get("description", "")),
        ),
        prompts={str(k): str(v) for k, v in payload.get("prompts", {}).items()},
        vars={str(k): str(v) for k, v in payload.get("vars", {}).items()},
        mcp_servers={str(k): v for k, v in payload.get("mcp_servers", {}).items()},
        created_at=float(payload.get("created_at", time.time())),
        updated_at=float(payload.get("updated_at", time.time())),
    )


def save_team_config(config: TeamSharedConfig, project_root: Path) -> None:
    """Persist team config to disk.

    Args:
        config: Team configuration to save.
        project_root: Project root directory.
    """
    path = get_team_config_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)

    config.updated_at = time.time()

    payload: dict = {
        "name": config.name,
        "description": config.description,
        "created_at": config.created_at,
        "updated_at": config.updated_at,
        "members": [asdict(m) for m in config.members],
        "settings": asdict(config.settings),
        "context": asdict(config.context),
        "prompts": config.prompts,
        "vars": config.vars,
        "mcp_servers": config.mcp_servers,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# User identity
# ---------------------------------------------------------------------------


def get_user_identity_path() -> Path:
    """Return the path to the user identity file."""
    return Path.home() / ".bog-agents" / "identity.json"


def load_user_identity() -> UserIdentity:
    """Load the current user's identity.

    Returns:
        UserIdentity, empty if not configured.
    """
    path = get_user_identity_path()
    if not path.exists():
        return UserIdentity()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return UserIdentity(
            name=str(data.get("name", "")),
            email=str(data.get("email", "")),
            role=str(data.get("role", "member")),
        )
    except (json.JSONDecodeError, OSError):
        return UserIdentity()


def save_user_identity(identity: UserIdentity) -> None:
    """Save the current user's identity.

    Args:
        identity: User identity to persist.
    """
    path = get_user_identity_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"name": identity.name, "email": identity.email, "role": identity.role},
            indent=2,
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Context injection
# ---------------------------------------------------------------------------


def get_shared_context_text(config: TeamSharedConfig, project_root: Path) -> str:
    """Return the auto-injected team context as a single string.

    Reads all files in ``context.always_include`` from the team directory.

    Args:
        config: Loaded team configuration.
        project_root: Project root directory.

    Returns:
        Combined context text, empty string if nothing to inject.
    """
    if not config.settings.shared_context_auto_inject:
        return ""

    team_dir = get_team_dir(project_root)
    parts: list[str] = []

    for rel_path in config.context.always_include:
        full_path = team_dir / rel_path
        if not full_path.exists():
            # Also check context/ subdirectory
            alt = team_dir / "context" / rel_path
            if alt.exists():
                full_path = alt
            else:
                continue
        try:
            content = full_path.read_text(encoding="utf-8", errors="replace").strip()
            if content:
                parts.append(f"### Team Context: {full_path.name}\n\n{content}")
        except OSError:
            continue

    return "\n\n---\n\n".join(parts)


def get_named_prompt(
    config: TeamSharedConfig, name: str, project_root: Path
) -> str | None:
    """Return a named shared prompt by looking in config and prompt files.

    Args:
        config: Loaded team configuration.
        name: Prompt name.
        project_root: Project root directory.

    Returns:
        Prompt text, or None if not found.
    """
    # Check inline prompts dict
    if name in config.prompts:
        return config.prompts[name]

    # Check prompts directory
    prompts_dir = get_team_dir(project_root) / "prompts"
    for suffix in (".md", ".txt", ""):
        path = prompts_dir / f"{name}{suffix}"
        if path.is_file():
            try:
                return path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue

    return None


# ---------------------------------------------------------------------------
# Member management
# ---------------------------------------------------------------------------


def add_member(
    config: TeamSharedConfig, name: str, email: str, role: str = "member"
) -> TeamMemberRecord:
    """Add or update a team member.

    Args:
        config: Team config to modify (in-place).
        name: Display name.
        email: Email address (used as unique key).
        role: Role string.

    Returns:
        The added or updated member record.
    """
    if role not in _VALID_ROLES:
        role = "member"

    for member in config.members:
        if member.email.lower() == email.lower():
            member.name = name or member.name
            member.role = role
            return member

    record = TeamMemberRecord(name=name, email=email, role=role)
    config.members.append(record)
    config.members.sort(key=lambda m: m.email.lower())
    return record


def remove_member(config: TeamSharedConfig, email_or_name: str) -> bool:
    """Remove a team member by email or name.

    Args:
        config: Team config to modify (in-place).
        email_or_name: Email or display name of member to remove.

    Returns:
        True if a member was removed.
    """
    key = email_or_name.strip().lower()
    before = len(config.members)
    config.members = [
        m for m in config.members if m.email.lower() != key and m.name.lower() != key
    ]
    return len(config.members) < before


# ---------------------------------------------------------------------------
# Setup guide
# ---------------------------------------------------------------------------


_SETUP_GUIDE = """\
Team Shared Config & Context — Setup Guide
==========================================

bog-agents supports sharing configuration, context, and prompts across your
entire developer team through files committed to your repository.

QUICK START
-----------

1. Initialize team config (run once per project):

       /team init "My Team"

   This creates:
       .bog-agents/team/config.json        ← team settings & member roster
       .bog-agents/team/context/            ← shared context files
       .bog-agents/team/prompts/            ← shared prompt library
       .bog-agents/team/skills/             ← shared skills

2. Set up your identity (each developer does this once):

       /team whoami set "Alice Smith" alice@company.com

3. Invite collaborators (one entry per person):

       /team invite alice@company.com owner "Alice Smith"
       /team invite bob@company.com member "Bob Jones"

4. Commit the config to share with your team:

       git add .bog-agents/team/
       git commit -m "chore: add bog-agents team config"
       git push

5. Team members just pull and get the shared config automatically:

       git pull

SHARED CONTEXT
--------------

Files in .bog-agents/team/context/ are auto-injected into every session.
This is perfect for:
  • Architecture overview           → context/architecture.md
  • Coding standards                → context/standards.md
  • API reference or key data URLs  → context/api-reference.md

Add a context file:

    /team context add context/architecture.md

Or create the file manually and add it:

    echo "# Architecture\n..." > .bog-agents/team/context/architecture.md
    /team context add context/architecture.md

SHARED PROMPTS
--------------

Named prompts that any team member can invoke:

    /team prompt add code-review "Review this code for security, performance, and style..."
    /team prompt show code-review

Then use it:

    /team prompt run code-review

SHARED VARIABLES
----------------

Set shared non-secret variables (don't put secrets here — use /vars for secrets):

    /team var set STAGING_URL https://staging.example.com
    /team var set API_DOCS_URL https://docs.example.com/api

ROLES
-----

  owner    — Can change team settings, invite/remove members
  admin    — Can add context/prompts, manage vars
  member   — Standard team access (default)
  readonly — Read team config but cannot modify it

TROUBLESHOOTING
---------------

  • Run `/team status` to verify the current config is loaded
  • Run `/team members` to see who's on the team
  • Team config not loading? Check .bog-agents/team/config.json exists
  • Context not injecting? Check settings.shared_context_auto_inject is true

For more help, see: https://bog-agents.dev/docs/team-config
"""


def format_setup_guide() -> str:
    """Return the team setup guide text."""
    return _SETUP_GUIDE


def format_team_status(
    config: TeamSharedConfig, project_root: Path, identity: UserIdentity
) -> str:
    """Return a formatted status summary of the team config.

    Args:
        config: Loaded team configuration.
        project_root: Project root directory.
        identity: Current user identity.

    Returns:
        Multi-line status string.
    """
    team_dir = get_team_dir(project_root)
    lines: list[str] = [
        f"Team: {config.name or '(unnamed)'}",
        f"Config: {get_team_config_path(project_root)}",
        "",
    ]

    if config.description:
        lines.append(f"Description: {config.description}")
        lines.append("")

    # Current user
    if identity.email:
        me = next(
            (m for m in config.members if m.email.lower() == identity.email.lower()),
            None,
        )
        if me:
            lines.append(f"You: {me.name} <{me.email}> ({me.role})")
        else:
            lines.append(f"You: {identity.name} <{identity.email}> (not yet invited)")
    else:
        lines.append("Your identity: not set — run /team whoami set <name> <email>")
    lines.append("")

    # Members
    if config.members:
        lines.append(f"Members ({len(config.members)}):")
        for m in config.members:
            lines.append(f"  • {m.name} <{m.email}> [{m.role}]")
    else:
        lines.append("Members: none — run /team invite <email> to add the first member")
    lines.append("")

    # Settings
    lines.append("Settings:")
    lines.append(
        f"  shared_context_auto_inject: {config.settings.shared_context_auto_inject}"
    )
    lines.append(f"  require_approval: {config.settings.require_approval}")
    if config.settings.model:
        lines.append(f"  model: {config.settings.model}")
    lines.append("")

    # Context
    context_files = config.context.always_include
    if context_files:
        lines.append(f"Auto-inject context ({len(context_files)} files):")
        for f in context_files:
            full = team_dir / f
            exists = (
                "✓"
                if full.exists() or (team_dir / "context" / f).exists()
                else "✗ missing"
            )
            lines.append(f"  [{exists}] {f}")
    else:
        lines.append("Context: none — run /team context add <file> to share context")
    lines.append("")

    # Prompts
    prompt_count = len(config.prompts)
    prompt_files = (
        list((team_dir / "prompts").glob("*.md"))
        if (team_dir / "prompts").is_dir()
        else []
    )
    total_prompts = prompt_count + len(prompt_files)
    if total_prompts:
        lines.append(f"Shared prompts: {total_prompts}")
        for name in list(config.prompts)[:5]:
            lines.append(f"  • {name}")
        for pf in prompt_files[:5]:
            lines.append(f"  • {pf.stem} (file)")
    else:
        lines.append("Prompts: none — run /team prompt add <name> <text>")

    return "\n".join(lines)


def init_team_directory(project_root: Path, team_name: str) -> list[Path]:
    """Create the team directory structure with template files.

    Args:
        project_root: Project root directory.
        team_name: Name for the team.

    Returns:
        List of created file paths.
    """
    team_dir = get_team_dir(project_root)
    created: list[Path] = []

    for subdir in ("context", "prompts", "skills"):
        (team_dir / subdir).mkdir(parents=True, exist_ok=True)

    # Template context file
    ctx_file = team_dir / "context" / "team-overview.md"
    if not ctx_file.exists():
        ctx_file.write_text(
            f"# {team_name} — Team Overview\n\n"
            "Add your team's shared context here. This file is auto-injected\n"
            "into every bog-agents session for all team members.\n\n"
            "## Project Description\n\n"
            "[Describe the project here]\n\n"
            "## Architecture Notes\n\n"
            "[Key architectural decisions and patterns]\n\n"
            "## Coding Standards\n\n"
            "[Link to style guides, linting rules, etc.]\n",
            encoding="utf-8",
        )
        created.append(ctx_file)

    # Template prompt file
    prompt_file = team_dir / "prompts" / "code-review.md"
    if not prompt_file.exists():
        prompt_file.write_text(
            "Review the provided code for:\n\n"
            "1. Security vulnerabilities (OWASP Top 10)\n"
            "2. Performance issues (N+1 queries, unnecessary allocations)\n"
            "3. Code style compliance with team standards\n"
            "4. Test coverage gaps\n"
            "5. Documentation completeness\n\n"
            "Provide specific, actionable feedback with line references.\n",
            encoding="utf-8",
        )
        created.append(prompt_file)

    return created
