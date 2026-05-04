"""Self-improving skill flywheel.

Reads recent session transcripts, asks an LLM to surface durable
patterns (the kind of "every time the user does X, I should …" insight
that's worth turning into a skill), and writes the proposals to
``~/.bog-agents/skills/proposed/`` for the user to review and accept.

The intent is *not* to auto-write to the live skills directory — the
user always reviews. That's why proposals land under a ``proposed/``
subdirectory: ``/teach accept <id>`` then promotes one to the active
skill set.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel

logger = logging.getLogger(__name__)


_PROPOSAL_SYSTEM_PROMPT = """\
You are reviewing a coding-agent transcript to find durable, reusable
skills the user would want available next time.

Output a JSON array of proposals. Each proposal has:
{
  "id": "<kebab-case-name>",
  "description": "<one sentence of what the skill does>",
  "trigger": "<short phrase describing when to apply this skill>",
  "instructions": "<3-10 lines of imperative guidance for the agent>"
}

Rules:
- Maximum 5 proposals. Quality over quantity.
- Each proposal must be GENERAL — applicable beyond the exact session.
- Skip proposals that are project-specific to the point of being
  uninteresting outside this repo.
- Output ONLY the JSON array, no commentary.
"""


@dataclass(frozen=True, slots=True)
class SkillProposal:
    """One proposed skill discovered in a transcript."""

    id: str
    description: str
    trigger: str
    instructions: str

    def to_skill_md(self) -> str:
        """Render the proposal as a SKILL.md file body."""
        return (
            f"---\n"
            f"name: {self.id}\n"
            f"description: {self.description}\n"
            f"trigger: {self.trigger}\n"
            f"---\n\n"
            f"# {self.id}\n\n"
            f"{self.description}\n\n"
            f"## When to apply\n\n"
            f"{self.trigger}\n\n"
            f"## Instructions\n\n"
            f"{self.instructions}\n"
        )


_PROPOSAL_DIR_NAME = "proposed"
_VALID_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def proposed_dir(*, skills_dir: Path | None = None) -> Path:
    """Return the directory where proposals live (creating it if needed)."""
    base = (
        skills_dir
        if skills_dir is not None
        else (Path.home() / ".bog-agents" / "skills")
    )
    target = base / _PROPOSAL_DIR_NAME
    target.mkdir(parents=True, exist_ok=True)
    return target


def list_proposals(*, skills_dir: Path | None = None) -> list[Path]:
    """Return every pending proposal file, sorted by name."""
    target = proposed_dir(skills_dir=skills_dir)
    return sorted(p for p in target.iterdir() if p.suffix.lower() == ".md")


def write_proposal(
    proposal: SkillProposal,
    *,
    skills_dir: Path | None = None,
    overwrite: bool = False,
) -> Path:
    """Persist one proposal under ``skills_dir/proposed/<id>.md``.

    Raises:
        ValueError: If the proposal id is not a safe identifier.
        FileExistsError: If a proposal with the same id already exists
            and ``overwrite`` is False.
    """
    if not _VALID_ID_RE.match(proposal.id):
        msg = f"invalid proposal id: {proposal.id!r}"
        raise ValueError(msg)
    target_dir = proposed_dir(skills_dir=skills_dir)
    target = target_dir / f"{proposal.id}.md"
    if target.exists() and not overwrite:
        msg = f"{target} already exists; pass overwrite=True"
        raise FileExistsError(msg)
    target.write_text(proposal.to_skill_md(), encoding="utf-8")
    return target


def accept_proposal(
    proposal_id: str,
    *,
    skills_dir: Path | None = None,
) -> Path:
    """Promote a proposal into the live ``~/.bog-agents/skills/`` directory.

    Returns the destination path. The matching proposal file under
    ``proposed/`` is removed once it has been promoted.

    Raises:
        FileNotFoundError: If no matching proposal exists.
    """
    base = (
        skills_dir
        if skills_dir is not None
        else (Path.home() / ".bog-agents" / "skills")
    )
    src = base / _PROPOSAL_DIR_NAME / f"{proposal_id}.md"
    if not src.is_file():
        msg = f"no proposal '{proposal_id}' under {base}"
        raise FileNotFoundError(msg)
    base.mkdir(parents=True, exist_ok=True)
    dst = base / f"{proposal_id}.md"
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    src.unlink()
    return dst


def reject_proposal(
    proposal_id: str,
    *,
    skills_dir: Path | None = None,
) -> bool:
    """Delete a pending proposal. Returns True if anything was removed."""
    base = (
        skills_dir
        if skills_dir is not None
        else (Path.home() / ".bog-agents" / "skills")
    )
    src = base / _PROPOSAL_DIR_NAME / f"{proposal_id}.md"
    if not src.is_file():
        return False
    src.unlink()
    return True


async def propose_skills_from_transcript(
    transcript: str,
    model: BaseChatModel,
) -> list[SkillProposal]:
    """Ask ``model`` to extract candidate skills from a transcript.

    Returns an empty list when the model output cannot be parsed — the
    flywheel intentionally fails closed rather than auto-writing
    garbage to the skills directory.
    """
    import json

    from langchain_core.messages import HumanMessage, SystemMessage

    if not transcript or not transcript.strip():
        return []

    response = await model.ainvoke(
        [
            SystemMessage(content=_PROPOSAL_SYSTEM_PROMPT),
            HumanMessage(content=transcript),
        ]
    )
    text = getattr(response, "content", "") or ""
    if isinstance(text, list):
        parts: list[str] = []
        for part in text:
            if isinstance(part, dict) and part.get("type") == "text":
                value = part.get("text")
                if isinstance(value, str):
                    parts.append(value)
            elif isinstance(part, str):
                parts.append(part)
        text = "".join(parts)

    raw = str(text).strip()
    # Tolerate a leading ```json ... ``` fence the model often adds.
    fence = re.search(r"\[.*\]", raw, re.DOTALL)
    if fence is None:
        return []
    try:
        items = json.loads(fence.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(items, list):
        return []

    proposals: list[SkillProposal] = []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        pid_raw = str(entry.get("id", "")).strip().lower()
        # Coerce to safe id by replacing whitespace and stray separators.
        pid = re.sub(r"\s+", "-", pid_raw)
        pid = re.sub(r"[^a-z0-9_-]", "", pid)[:64]
        if not pid or not _VALID_ID_RE.match(pid):
            continue
        description = str(entry.get("description", "")).strip()
        trigger = str(entry.get("trigger", "")).strip()
        instructions = str(entry.get("instructions", "")).strip()
        if not description or not instructions:
            continue
        proposals.append(
            SkillProposal(
                id=pid,
                description=description,
                trigger=trigger or "(unspecified)",
                instructions=instructions,
            )
        )
    return proposals[:5]


__all__ = [
    "SkillProposal",
    "accept_proposal",
    "list_proposals",
    "propose_skills_from_transcript",
    "proposed_dir",
    "reject_proposal",
    "write_proposal",
]
