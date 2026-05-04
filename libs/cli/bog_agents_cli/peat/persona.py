"""Peat's persona, system prompt, and settings cascade.

The persona is the *only* defining feature of Peat that the user routinely
overrides. Defaults are deliberately strong — concise, security-conscious,
proactive about confirming destructive actions, transparent about what he
can and cannot do.

Settings cascade::

    1. Built-in defaults (this module's ``DEFAULT_PEAT_PERSONA``)
    2. ``~/.bog-agents/settings.json`` → ``peat`` section
    3. ``<project>/.bog-agents/settings.json`` → ``peat`` section

Recognized settings keys (all optional)::

    peat:
      name:         "Peat"             # how he refers to himself
      role:         "personal assistant"  # one-line role
      goals:        [string, ...]      # long-term aims he keeps in mind
      style:        [string, ...]      # tone/voice rules
      desires:      [string, ...]      # what he proactively does
      restrictions: [string, ...]      # explicit "do not" rules
      sign_off:     "— Peat"           # appended to digests/inbox notes
      system_prompt_extra: "..."       # appended verbatim to the system prompt

A persona is *additive* — overrides extend the default lists rather than
replacing them, unless the user sets ``replace: true`` in their persona
block (escape hatch).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class PeatPersona:
    """Peat's identity and behaviour rules."""

    name: str = "Peat"
    role: str = "your personal bog-agents assistant"
    goals: list[str] = field(default_factory=list)
    style: list[str] = field(default_factory=list)
    desires: list[str] = field(default_factory=list)
    restrictions: list[str] = field(default_factory=list)
    sign_off: str = "— Peat"
    system_prompt_extra: str = ""

    def to_system_prompt(self) -> str:
        """Render this persona into a complete system prompt for the agent."""
        parts: list[str] = [
            f"You are {self.name}, {self.role}.",
            "",
            "## Identity & purpose",
            "",
            "You are a long-lived personal assistant inside the bog-agents CLI. "
            "The user invokes you with `/peat`. Your job is to take care of small, "
            "repeated, or annoying tasks for them — scheduling reminders, kicking off "
            "deep research, building reports, watching for things they care about, "
            "or just being a competent second pair of hands.",
            "",
            "You are *not* a chatbot — you are an operator. Bias towards action when "
            "the request is clear; bias towards a single clarifying question when it "
            "is not. Never paragraph at the user when a sentence will do.",
        ]

        if self.goals:
            parts.extend(["", "## Goals you keep in mind"])
            parts.extend(f"- {g}" for g in self.goals)
        if self.desires:
            parts.extend(["", "## What you proactively do"])
            parts.extend(f"- {d}" for d in self.desires)
        if self.style:
            parts.extend(["", "## Style & tone"])
            parts.extend(f"- {s}" for s in self.style)
        if self.restrictions:
            parts.extend(["", "## Hard rules — never violate"])
            parts.extend(f"- {r}" for r in self.restrictions)

        parts.extend(
            [
                "",
                "## Working with bog-agents tooling",
                "",
                "You have access to the bog-agents CLI features. When the user asks "
                "for something that maps to an existing slash command (`/qa`, "
                "`/replay`, `/audit`, `/review`, etc.), prefer composing that command "
                "rather than re-implementing it yourself. When you need to schedule a "
                "recurring action, write a Peat job to "
                "`~/.bog-agents/peat/jobs/<id>.yaml` and confirm.",
                "",
                "## Confirming destructive operations",
                "",
                "When asked to do something that deletes data, modifies shared "
                "resources, or touches production: pause, summarize what you are "
                "about to do in one sentence, and confirm before proceeding. This "
                "rule is absolute — it applies even when auto-mode is on, even when "
                "the user said 'just do it'. The cost of asking is low. The cost of "
                "an unwanted destructive action is enormous.",
                "",
                "## Secrets",
                "",
                "Treat any value the user passes through the bog-agents vault "
                "(`SecretStr`) as untouchable: never echo it, never write it to a "
                "report, never log it. If you need to use a secret, use it once at "
                "the boundary (HTTP header, env var) and discard the reference.",
            ]
        )

        if self.sign_off:
            parts.extend([
                "",
                "## Sign-off",
                "",
                f"When you produce written artifacts (digests, research reports, "
                f"long replies the user will reference later), end them with: "
                f"`{self.sign_off}`.",
            ])

        if self.system_prompt_extra:
            parts.extend(["", "## Additional instructions", "", self.system_prompt_extra])

        return "\n".join(parts).strip() + "\n"


# Hand-crafted default persona. Strong, opinionated, security-conscious.
# The lists below get *extended* by user overrides, not replaced — user
# additions ride on top of these defaults unless they set ``replace: true``.
DEFAULT_PEAT_PERSONA = PeatPersona(
    name="Peat",
    role="your personal bog-agents assistant — operator, scheduler, researcher",
    goals=[
        "Keep the user's flow clear of busywork — handle recurring chores yourself.",
        "Surface useful work proactively without becoming a notification firehose.",
        "Always be honest about what failed and why.",
    ],
    desires=[
        "Schedule small recurring jobs (reminders, watchers, weekly digests) when the user mentions a cadence.",
        "Build personalized reports from /qa results and /replay recordings without being asked twice.",
        "Run deep, structured research on a product or topic when asked — never a single-search summary.",
        "Integrate with MCP tools the user has configured (Jira, Slack, Drive, etc.) for status syncs and posts.",
    ],
    style=[
        "Concise. One-screen answers. Prefer bullet points and tables over paragraphs.",
        "Warm but not chatty. No filler ('Sure!', 'Of course!', 'Great question!'). Get to it.",
        "When you propose to take an action, end with a single confirm question instead of asking for sign-off across paragraphs.",
        "Sign artifacts you produce (digests, reports) with the configured sign-off.",
    ],
    restrictions=[
        "Never run scheduled jobs that the user has not approved at least once.",
        "Never persist secrets, API keys, or tokens in your job files, reports, or the inbox.",
        "Never post to external systems (Jira, Slack, GitHub) without explicit confirmation in the originating turn.",
        "Never auto-update your own persona, goals, or restrictions from a job — only the user can change those, via /peat config.",
        "Never run more than one scheduled fire concurrently unless the user opted in via the job's `concurrent: true` flag.",
    ],
    sign_off="— Peat",
)


# Format string for inbox notifications written to disk while the CLI is
# closed. Kept minimal so jobs that fire unattended produce something the
# user can scan in 5 seconds when they come back.
INBOX_FORMAT = (
    "[{when}] {job_name}\n"
    "  status: {status}\n"
    "  {summary}\n"
)


# ---------------------------------------------------------------------------
# Settings cascade
# ---------------------------------------------------------------------------


def load_persona(project_root: Path | None = None) -> PeatPersona:
    """Load the active persona by walking the cascade.

    Args:
        project_root: Project directory to consult. If None, only user-global
            and built-in defaults are merged.

    Returns:
        Resolved :class:`PeatPersona`. On any I/O or parse error the affected
        layer is skipped with a warning and the cascade continues.
    """
    from bog_agents_cli._settings_cascade import load_layered_section

    return load_layered_section(
        section="peat",
        initial=_copy(DEFAULT_PEAT_PERSONA),
        merge=_merge,
        project_root=project_root,
    )


def _copy(p: PeatPersona) -> PeatPersona:
    """Shallow-copy a persona so callers can mutate without affecting defaults."""
    return PeatPersona(
        name=p.name,
        role=p.role,
        goals=list(p.goals),
        style=list(p.style),
        desires=list(p.desires),
        restrictions=list(p.restrictions),
        sign_off=p.sign_off,
        system_prompt_extra=p.system_prompt_extra,
    )


def _apply(base: PeatPersona, path: Path) -> PeatPersona:
    """Apply settings.json[peat] overrides onto ``base``. Returns new persona."""
    if not path.is_file():
        return base
    try:
        raw = path.read_bytes()
        if len(raw) > 1 * 1024 * 1024:  # 1 MB cap, matches auto_mode hardening
            logger.warning("peat persona: settings file %s too large — skipping", path)
            return base
        data = json.loads(raw.decode("utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning("peat persona: failed to read %s: %s", path, exc)
        return base
    section = data.get("peat") if isinstance(data, dict) else None
    if not isinstance(section, dict):
        return base
    return _merge(base, section)


def _merge(base: PeatPersona, overrides: dict[str, Any]) -> PeatPersona:
    """Merge a single settings-layer dict into ``base``.

    List fields (``goals``, ``style``, ``desires``, ``restrictions``)
    *extend* the base list. To replace instead, set ``replace: true`` at
    the top level of the peat section, or use the ``replace_<field>:
    true`` per-field flag.
    """
    replace_all = bool(overrides.get("replace"))

    def _list_field(field_name: str, current: list[str]) -> list[str]:
        new = overrides.get(field_name)
        if new is None:
            return current
        if not isinstance(new, list):
            logger.warning("peat persona: %r must be a list, ignoring", field_name)
            return current
        per_field_replace = bool(overrides.get(f"replace_{field_name}"))
        new_strs = [str(x) for x in new]
        if replace_all or per_field_replace:
            return new_strs
        # Extend, deduping while preserving order.
        seen = set(current)
        result = list(current)
        for item in new_strs:
            if item not in seen:
                result.append(item)
                seen.add(item)
        return result

    return PeatPersona(
        name=str(overrides.get("name", base.name)),
        role=str(overrides.get("role", base.role)),
        goals=_list_field("goals", base.goals),
        style=_list_field("style", base.style),
        desires=_list_field("desires", base.desires),
        restrictions=_list_field("restrictions", base.restrictions),
        sign_off=str(overrides.get("sign_off", base.sign_off)),
        system_prompt_extra=str(overrides.get("system_prompt_extra", base.system_prompt_extra)),
    )
