"""`/handoff` — compile a human-to-human context-transfer document.

Generates a handoff brief written in the *next* developer's voice. The
brief synthesises the current session's conversation, recent git
activity, and modified files into a short document that the next person
on the keyboard can read in 60 seconds to pick up where this session
left off.

Output lands in ``~/.bog-agents/handoffs/<timestamp>-<branch>.md`` and
is also rendered into the chat surface for review.

This module is invoked by ``BogAgentsApp._handle_handoff_command`` —
keep the public entry point pure-async so it's trivial to test without a
running TUI.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from bog_agents_cli.feature_helpers import (
    GitContext,
    TranscriptEntry,
    collect_git_context,
    collect_transcript,
    invoke_model,
    resolve_active_model_spec,
    transcript_to_markdown,
    write_artifact,
)

logger = logging.getLogger(__name__)


HANDOFF_SYSTEM_PROMPT = """\
You are the developer about to take over a project from a teammate. They
just finished a coding session and you need to read their handoff doc
before you sit down at the keyboard.

Write the handoff doc in YOUR voice — first-person, past-tense
("I noticed", "I tried", "I'm not sure whether") — as if you're
narrating what you've already absorbed from a session you observed.

Structure (use these exact markdown headings):

## Where I left off
A one-paragraph summary of what was being attempted.

## What I tried
Concrete bullet points naming files, commands, or approaches that were
attempted, with a one-line outcome each.

## What I'm uncertain about
Open questions the next dev should resolve before going further.

## Suggested next step
ONE specific action to take next — a file to open, a test to run, a
question to answer.

## State on disk
A short paragraph naming the current branch, any uncommitted changes,
and whether anything is mid-rebase / mid-merge.

Rules:
- Do NOT invent details. If the session data doesn't tell you something,
  say "the session log does not say".
- Keep the whole document under ~500 words.
- Do NOT include preamble or sign-off — start with the first heading.
- Write naturally; avoid bureaucratic language.
"""


@dataclass(frozen=True)
class HandoffResult:
    """Outcome of a single handoff generation."""

    path: Path
    """Where the handoff doc was written on disk."""

    content: str
    """The rendered markdown body (same content as on disk)."""

    elapsed_seconds: float
    """Wall-clock time for the LLM call, useful for logs and UI."""


def render_session_for_handoff(
    transcript: list[TranscriptEntry],
    git: GitContext,
    *,
    author_voice: str = "",
) -> str:
    """Render the session into a single prompt body the model can read.

    The model gets git context first (most concrete), then transcript
    (more verbose but lower-signal). The author hint, if any, is given
    as a voice cue.
    """
    parts: list[str] = []
    if author_voice:
        parts.append(
            f"AUTHOR HINT: Write in the voice of {author_voice}. Use their "
            "name only if it feels natural; do not over-personalise."
        )
    parts.append("## Git context")
    parts.append(f"- Branch: `{git.branch or '(detached)'}`")
    if git.head_sha:
        parts.append(f"- HEAD: `{git.head_sha}`")
    if git.modified_files:
        parts.append(f"- Modified files ({len(git.modified_files)}):")
        for path in git.modified_files[:20]:
            parts.append(f"  - {path}")
        if len(git.modified_files) > 20:
            parts.append(f"  - ...and {len(git.modified_files) - 20} more")
    if git.untracked_files:
        parts.append(f"- Untracked files ({len(git.untracked_files)}):")
        for path in git.untracked_files[:10]:
            parts.append(f"  - {path}")
    if git.recent_commits:
        parts.append("- Recent commits:")
        for c in git.recent_commits[:8]:
            parts.append(f"  - {c}")
    if git.diff_summary:
        parts.append("- Diff stat:")
        parts.append("```")
        parts.append(git.diff_summary)
        parts.append("```")

    parts.append("")
    parts.append("## Conversation transcript")
    parts.append(transcript_to_markdown(transcript))
    return "\n".join(parts)


async def generate_handoff(
    *,
    model: object,
    transcript: list[TranscriptEntry],
    git: GitContext,
    author_voice: str = "",
) -> str:
    """Invoke the model with the rendered session and return the handoff doc.

    Args:
        model: A ``BaseChatModel`` instance.
        transcript: Conversation entries to summarise.
        git: Git context snapshot.
        author_voice: Optional name to use as a voice cue.

    Returns:
        The handoff document body (markdown), ready to display and write.
        Propagates :class:`TimeoutError` from :func:`invoke_model` when
        the LLM call exceeds its budget.
    """
    body = render_session_for_handoff(transcript, git, author_voice=author_voice)
    return await invoke_model(model, HANDOFF_SYSTEM_PROMPT, body)  # type: ignore[arg-type]


async def run_handoff(app: object, *, author_voice: str = "") -> HandoffResult:
    """End-to-end ``/handoff`` flow used by the app handler.

    Gathers transcript + git context, resolves the active model, invokes
    it, persists the artifact, and returns a :class:`HandoffResult`.

    Args:
        app: The running ``BogAgentsApp`` instance.
        author_voice: Optional voice hint (typically a name).

    Returns:
        A :class:`HandoffResult` with the path and body.

    Raises:
        RuntimeError: If no model spec can be resolved.
    """
    from bog_agents_cli.config import create_model_with_fallback

    spec = resolve_active_model_spec(app)
    if not spec:
        msg = "no active model — run /model first or set a default"
        raise RuntimeError(msg)

    transcript = collect_transcript(app)
    git = collect_git_context(Path(getattr(app, "_cwd", Path.cwd())))
    profile = getattr(app, "_profile_override", None)
    model_result = create_model_with_fallback(spec, profile_overrides=profile)

    start = time.monotonic()
    body = await generate_handoff(
        model=model_result.model,
        transcript=transcript,
        git=git,
        author_voice=author_voice,
    )
    elapsed = time.monotonic() - start

    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    branch_slug = (git.branch or "head").replace("/", "_").replace(" ", "-")[:40]
    filename = f"{stamp}-{branch_slug}.md"
    path = write_artifact("handoffs", filename, _wrap_with_frontmatter(body, git, spec))
    return HandoffResult(path=path, content=body, elapsed_seconds=elapsed)


def _wrap_with_frontmatter(body: str, git: GitContext, model_spec: str) -> str:
    """Prepend a small YAML-ish frontmatter so artifacts are self-describing."""
    lines = [
        "---",
        f"branch: {git.branch or '(detached)'}",
        f"head: {git.head_sha or '(none)'}",
        f"model: {model_spec}",
        f"generated: {time.strftime('%Y-%m-%dT%H:%M:%S')}",
        "kind: handoff",
        "---",
        "",
        body,
    ]
    return "\n".join(lines)
