"""Repo-committed `.prompt.md` files as auto-registered slash commands (#14).

Teams want shareable, version-controlled workflow recipes that show up as
`/`-commands — Claude Code's `.claude/commands/*.md`, Copilot's `.prompt.md`,
Windsurf workflows. This loader discovers `*.prompt.md` files under
`.bog-agents/prompts/` (project) and `~/.bog-agents/prompts/` (user), parses
optional YAML frontmatter + a Markdown body, and exposes them as prompt-backed
slash commands. When invoked, the body (with `$ARGUMENTS` substituted) is sent
to the agent.

Format::

    ---
    description: Triage a bug report
    argument-hint: "[issue number]"
    ---
    Investigate issue $ARGUMENTS: reproduce it, find the root cause, and propose
    a minimal fix with a regression test.

The command name is the file stem (`triage.prompt.md` -> `/triage`). Project
definitions override user ones on a name clash. Pure logic — no TUI deps — so
it's testable and reusable by the headless path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_ARGUMENTS_TOKEN = "$ARGUMENTS"
_PROMPT_SUFFIX = ".prompt.md"


@dataclass(frozen=True)
class PromptCommand:
    """A slash command defined by a repo/user `.prompt.md` file.

    Attributes:
        name: Slash command including the leading slash (e.g. ``/triage``).
        description: One-line description for help/autocomplete.
        template: The Markdown body sent to the agent (pre-substitution).
        argument_hint: Optional usage hint shown in help.
        source: Absolute path of the defining file.
        scope: ``project`` or ``user``.
    """

    name: str
    description: str
    template: str
    argument_hint: str = ""
    source: str = ""
    scope: str = "project"


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split optional ``---`` YAML frontmatter from the Markdown body.

    Returns:
        A ``(metadata, body)`` tuple. Metadata is ``{}`` when no frontmatter or
        when it fails to parse (the body is then the whole file).
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    # Find the closing fence after the first line.
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            fm_block = "\n".join(lines[1:idx])
            body = "\n".join(lines[idx + 1 :]).lstrip("\n")
            try:
                import yaml

                data = yaml.safe_load(fm_block) or {}
            except Exception:  # malformed frontmatter -> treat whole file as body
                logger.debug("prompt-command frontmatter parse failed", exc_info=True)
                return {}, text
            if not isinstance(data, dict):
                return {}, text
            return {str(k): str(v) for k, v in data.items()}, body
    return {}, text


def _load_prompt_file(path: Path, scope: str) -> PromptCommand | None:
    """Parse a single ``*.prompt.md`` file into a :class:`PromptCommand`."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        logger.debug("could not read prompt command %s", path, exc_info=True)
        return None
    meta, body = _parse_frontmatter(text)
    body = body.strip()
    if not body:
        return None
    stem = path.name[: -len(_PROMPT_SUFFIX)] if path.name.endswith(_PROMPT_SUFFIX) else path.stem
    name = "/" + stem.strip().lstrip("/")
    description = meta.get("description") or f"Custom prompt command from {path.name}"
    return PromptCommand(
        name=name,
        description=description.strip(),
        template=body,
        argument_hint=meta.get("argument-hint", meta.get("argument_hint", "")).strip(),
        source=str(path),
        scope=scope,
    )


def _scan_dir(directory: Path, scope: str) -> dict[str, PromptCommand]:
    out: dict[str, PromptCommand] = {}
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob(f"*{_PROMPT_SUFFIX}")):
        cmd = _load_prompt_file(path, scope)
        if cmd is not None:
            out[cmd.name] = cmd
    return out


def discover_prompt_commands(
    cwd: str | Path | None = None, *, include_user: bool = True
) -> dict[str, PromptCommand]:
    """Discover prompt-backed slash commands from project + user dirs.

    Args:
        cwd: Project root (defaults to the current directory). Its
            ``.bog-agents/prompts/`` is scanned.
        include_user: Also scan ``~/.bog-agents/prompts/``.

    Returns:
        Mapping of slash-command name -> :class:`PromptCommand`. Project
        definitions override user ones on a name clash.
    """
    commands: dict[str, PromptCommand] = {}
    if include_user:
        commands.update(_scan_dir(Path.home() / ".bog-agents" / "prompts", "user"))
    project_root = Path(cwd) if cwd else Path.cwd()
    commands.update(_scan_dir(project_root / ".bog-agents" / "prompts", "project"))
    return commands


def render_prompt_command(cmd: PromptCommand, args: str) -> str:
    """Render a prompt-command body for sending to the agent.

    ``$ARGUMENTS`` is replaced with ``args``. If the template has no
    ``$ARGUMENTS`` token and args were supplied, they are appended so a bare
    command like ``/triage 42`` still passes the argument through.

    Args:
        cmd: The command to render.
        args: The raw argument string after the command name.

    Returns:
        The final prompt text.
    """
    args = args.strip()
    if _ARGUMENTS_TOKEN in cmd.template:
        return cmd.template.replace(_ARGUMENTS_TOKEN, args)
    if args:
        return f"{cmd.template}\n\n{args}"
    return cmd.template
