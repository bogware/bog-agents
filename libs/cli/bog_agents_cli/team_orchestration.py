"""Team orchestration helpers for named worker groups and shared memory."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass(slots=True)
class TeamMember:
    """One configured team member."""

    name: str
    role: str = "worker"


@dataclass(slots=True)
class TeamMessage:
    """Shared note or coordination message for a team."""

    body: str
    sender: str = "supervisor"
    created_at: float = field(default_factory=time.time)


@dataclass(slots=True)
class TeamProfile:
    """Persisted team configuration and memory."""

    name: str
    summary: str = ""
    members: list[TeamMember] = field(default_factory=list)
    messages: list[TeamMessage] = field(default_factory=list)
    updated_at: float = field(default_factory=time.time)


@dataclass(slots=True)
class TeamRegistry:
    """Workspace-local registry of named teams."""

    active_team: str = ""
    teams: list[TeamProfile] = field(default_factory=list)


def get_team_registry_path(working_dir: Path | None = None) -> Path:
    """Return the workspace-local team registry path."""
    directory = working_dir or Path.cwd()
    return directory / ".bog-agents" / "teams.json"


def load_team_registry(working_dir: Path | None = None) -> TeamRegistry:
    """Load the workspace-local team registry from disk."""
    path = get_team_registry_path(working_dir)
    if not path.exists():
        return TeamRegistry()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return TeamRegistry()
    if not isinstance(payload, dict):
        return TeamRegistry()

    teams_data = payload.get("teams")
    teams: list[TeamProfile] = []
    if isinstance(teams_data, list):
        for item in teams_data:
            if not isinstance(item, dict):
                continue
            members_data = item.get("members")
            members = [
                TeamMember(
                    name=str(member.get("name", "")).strip(),
                    role=str(member.get("role", "worker")).strip() or "worker",
                )
                for member in members_data
                if isinstance(members_data, list)
                for member in [member]
                if isinstance(member, dict) and str(member.get("name", "")).strip()
            ]
            messages_data = item.get("messages")
            messages = [
                TeamMessage(
                    body=str(message.get("body", "")),
                    sender=str(message.get("sender", "supervisor")) or "supervisor",
                    created_at=float(message.get("created_at", time.time())),
                )
                for message in messages_data
                if isinstance(messages_data, list)
                for message in [message]
                if isinstance(message, dict) and str(message.get("body", "")).strip()
            ]
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            teams.append(
                TeamProfile(
                    name=name,
                    summary=str(item.get("summary", "")).strip(),
                    members=members,
                    messages=messages[-24:],
                    updated_at=float(item.get("updated_at", time.time())),
                )
            )
    return TeamRegistry(
        active_team=str(payload.get("active_team", "")).strip(),
        teams=teams,
    )


def save_team_registry(registry: TeamRegistry, working_dir: Path | None = None) -> None:
    """Persist the team registry to disk."""
    path = get_team_registry_path(working_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "active_team": registry.active_team,
        "teams": [asdict(team) for team in registry.teams],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def find_team(registry: TeamRegistry, name: str) -> TeamProfile | None:
    """Return a team profile by name, case-insensitively."""
    lowered = name.strip().lower()
    if not lowered:
        return None
    for team in registry.teams:
        if team.name.lower() == lowered:
            return team
    return None


def ensure_team(registry: TeamRegistry, name: str) -> TeamProfile:
    """Return an existing team or create a new one."""
    cleaned = name.strip()
    existing = find_team(registry, cleaned)
    if existing is not None:
        return existing
    team = TeamProfile(name=cleaned, updated_at=time.time())
    registry.teams.append(team)
    registry.teams.sort(key=lambda item: item.name.lower())
    return team


def set_active_team(registry: TeamRegistry, name: str | None) -> None:
    """Set or clear the active team."""
    registry.active_team = (
        name.strip() if isinstance(name, str) and name.strip() else ""
    )


def add_team_member(
    registry: TeamRegistry, team_name: str, member_name: str, role: str
) -> TeamProfile:
    """Add or update a member on a team."""
    team = ensure_team(registry, team_name)
    cleaned_member = member_name.strip()
    cleaned_role = role.strip() or "worker"
    for member in team.members:
        if member.name.lower() == cleaned_member.lower():
            member.role = cleaned_role
            team.updated_at = time.time()
            return team
    team.members.append(TeamMember(name=cleaned_member, role=cleaned_role))
    team.members.sort(key=lambda item: item.name.lower())
    team.updated_at = time.time()
    return team


def remove_team_member(
    registry: TeamRegistry, team_name: str, member_name: str
) -> bool:
    """Remove a member from a team."""
    team = find_team(registry, team_name)
    if team is None:
        return False
    before = len(team.members)
    team.members = [
        member
        for member in team.members
        if member.name.lower() != member_name.strip().lower()
    ]
    changed = len(team.members) != before
    if changed:
        team.updated_at = time.time()
    return changed


def append_team_message(
    registry: TeamRegistry,
    team_name: str,
    body: str,
    *,
    sender: str = "supervisor",
) -> TeamProfile:
    """Append a shared message to a team."""
    team = ensure_team(registry, team_name)
    team.messages.append(
        TeamMessage(body=body.strip(), sender=sender.strip() or "supervisor")
    )
    team.messages = team.messages[-24:]
    team.updated_at = time.time()
    return team


def set_team_summary(
    registry: TeamRegistry, team_name: str, summary: str
) -> TeamProfile:
    """Set the persistent shared summary for a team."""
    team = ensure_team(registry, team_name)
    team.summary = summary.strip()
    team.updated_at = time.time()
    return team


def build_team_brief(team: TeamProfile) -> str:
    """Build a compact shared-memory brief for task prompts."""
    lines = [f"Team: {team.name}"]
    if team.summary:
        lines.append(f"Shared summary: {team.summary}")
    if team.members:
        member_text = ", ".join(
            f"{member.name} ({member.role})" for member in team.members[:8]
        )
        lines.append(f"Members: {member_text}")
    if team.messages:
        lines.append("Recent coordination:")
        for message in team.messages[-3:]:
            lines.append(f"- {message.sender}: {message.body}")
    return "\n".join(lines)


def summarize_team_activity(team: TeamProfile, task_summaries: list[str]) -> str:
    """Generate a compact shared summary from task output and coordination notes."""
    parts: list[str] = []
    if team.summary:
        parts.append(team.summary)
    recent_notes = [
        message.body for message in team.messages[-3:] if message.body.strip()
    ]
    parts.extend(recent_notes)
    parts.extend(summary.strip() for summary in task_summaries if summary.strip())
    if not parts:
        return f"{team.name} is ready for coordination."
    combined = " | ".join(parts)
    if len(combined) > 480:
        return combined[:477].rstrip() + "..."
    return combined


def format_team_profile(
    team: TeamProfile,
    *,
    active: bool = False,
    local_tasks: int = 0,
    remote_tasks: int = 0,
    inbox_count: int = 0,
) -> str:
    """Format one team profile for status output."""
    prefix = "* " if active else "- "
    lines = [
        (
            f"{prefix}{team.name} | members={len(team.members)} | "
            f"local={local_tasks} | remote={remote_tasks} | inbox={inbox_count}"
        )
    ]
    if team.summary:
        lines.append(f"  summary: {team.summary}")
    if team.members:
        member_text = ", ".join(
            f"{member.name} ({member.role})" for member in team.members[:6]
        )
        lines.append(f"  members: {member_text}")
    if team.messages:
        last = team.messages[-1]
        lines.append(f"  last note: {last.sender}: {last.body}")
    return "\n".join(lines)
