"""Shared helpers for slash-command features that talk to a model directly.

These features (``/handoff``, ``/release-train``, ``/imagine``, ``/devil``,
``/squad``, ``/dream``) all need the same plumbing:

* resolve the active model spec (CLI override → settings → config default),
* construct a ``BaseChatModel`` via :func:`create_model_with_fallback`,
* invoke it with a ``SystemMessage`` + ``HumanMessage`` pair,
* extract a clean string from the (possibly multimodal) response.

We also gather context (recent git activity, the conversation transcript)
in one place so each feature reads from a consistent shape.

Nothing in this module is feature-specific — keep it pure helpers.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import subprocess  # noqa: S404 — only used for read-only git introspection
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Model invocation
# ---------------------------------------------------------------------------


async def invoke_model(
    model: BaseChatModel,
    system_prompt: str,
    user_prompt: str,
    *,
    timeout_seconds: float = 90.0,
) -> str:
    """Call ``model`` with one system + one user message; return clean text.

    The response payload is normalised: a multimodal block list is
    flattened to its text parts, leading/trailing whitespace is
    stripped, and accidental ```` ``` `` fences are pulled off when
    they wrap the entire body. Anything else passes through untouched.

    Args:
        model: A LangChain ``BaseChatModel`` (anything ``ainvoke``-able).
        system_prompt: The system prompt to install.
        user_prompt: The user-message body.
        timeout_seconds: Hard wall-clock cap. The model providers all
            have their own timeouts but this layer adds a uniform
            outer bound so a wedged call cannot freeze the TUI.

    Returns:
        Cleaned text body. Empty string on a fully-empty response.

    Raises:
        TimeoutError: If the call exceeds ``timeout_seconds``.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    try:
        response = await asyncio.wait_for(
            model.ainvoke(
                [
                    SystemMessage(content=system_prompt),
                    HumanMessage(content=user_prompt),
                ],
            ),
            timeout=timeout_seconds,
        )
    except TimeoutError as exc:
        msg = f"model call exceeded {timeout_seconds:.0f}s budget"
        raise TimeoutError(msg) from exc

    return _normalise_response(response)


def _normalise_response(response: object) -> str:
    """Pull a clean string out of a LangChain response object."""
    raw = response.content if hasattr(response, "content") else str(response)
    if isinstance(raw, list):
        parts: list[str] = []
        for part in raw:
            if isinstance(part, dict) and part.get("type") == "text":
                value = part.get("text")
                if isinstance(value, str):
                    parts.append(value)
            elif isinstance(part, str):
                parts.append(part)
        text = "".join(parts)
    else:
        text = str(raw)
    text = text.strip()
    # Strip a whole-body markdown fence if the model wrapped the answer.
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if len(lines) >= 2:
            # Drop opening fence (with optional language tag) and trailing fence.
            text = "\n".join(lines[1:-1]).strip()
    return text


def resolve_active_model_spec(app: object) -> str:
    """Return the model spec the user would naturally consider 'active'.

    Order of precedence:

    1. ``app._model_override`` (set when the user did ``/model <spec>``
       this session).
    2. ``settings.model_name`` resolved against ``settings.model_provider``
       (the default at process start).
    3. Empty string when nothing is configured.
    """
    from bog_agents_cli.config import settings

    override = getattr(app, "_model_override", "") or ""
    if override:
        return override
    name = getattr(settings, "model_name", "") or ""
    provider = getattr(settings, "model_provider", "") or ""
    if name and provider:
        return f"{provider}:{name}"
    return name


# ---------------------------------------------------------------------------
# Conversation transcript
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TranscriptEntry:
    """One conversational exchange captured for downstream summarisation."""

    role: str
    """``user``, ``assistant``, or ``system``."""

    text: str
    """The displayed body. Tool-output blobs are pre-collapsed by the caller."""


def collect_transcript(
    app: object, *, max_entries: int = 60, max_chars: int = 24_000
) -> list[TranscriptEntry]:
    """Pull the current conversation into a flat list of role/text entries.

    This is a *best-effort* extractor — the CLI persists more state than
    we expose here, but a clean user-and-assistant trail is enough for
    summarisers like ``/handoff`` and ``/devil``.

    Args:
        app: The running ``BogAgentsApp`` instance.
        max_entries: Hard cap on entries returned (most-recent kept).
        max_chars: Total character budget across all entries. Earlier
            entries are dropped until the budget is met.

    Returns:
        A list of :class:`TranscriptEntry`, oldest first.
    """
    from bog_agents_cli.widgets.messages import (
        AppMessage,
        ErrorMessage,
        UserMessage,
    )

    out: list[TranscriptEntry] = []
    try:
        # The chat container holds rendered Message widgets in order.
        container = getattr(app, "_chat_container", None)
        if container is None:
            return out
        children = list(container.children)
    except Exception:
        logger.debug("collect_transcript: failed to read chat container", exc_info=True)
        return out

    for widget in children:
        if isinstance(widget, UserMessage):
            role = "user"
        elif isinstance(widget, AppMessage):
            role = "assistant"
        elif isinstance(widget, ErrorMessage):
            role = "system"
        else:
            continue
        text = _widget_text(widget)
        if text:
            out.append(TranscriptEntry(role=role, text=text))

    # Trim by entry count first, then by character budget (right-aligned).
    if len(out) > max_entries:
        out = out[-max_entries:]
    total = sum(len(e.text) for e in out)
    while total > max_chars and len(out) > 1:
        dropped = out.pop(0)
        total -= len(dropped.text)
    return out


def _widget_text(widget: object) -> str:
    """Extract the displayed text from a chat message widget."""
    # Most chat widgets store the text on `_content` or via `.renderable`.
    for attr in ("_content", "content", "_text", "text"):
        val = getattr(widget, attr, None)
        if isinstance(val, str) and val.strip():
            return val.strip()
    try:
        rendered = str(widget.renderable) if hasattr(widget, "renderable") else ""
        return rendered.strip()
    except Exception:
        return ""


def transcript_to_markdown(entries: list[TranscriptEntry]) -> str:
    """Render entries as a compact markdown transcript for prompt embedding."""
    if not entries:
        return "(no prior conversation)"
    out: list[str] = []
    for e in entries:
        out.append(f"**{e.role}:** {e.text}")
    return "\n\n".join(out)


# ---------------------------------------------------------------------------
# Git context
# ---------------------------------------------------------------------------


@dataclass
class GitContext:
    """Snapshot of recent git activity in the working tree."""

    branch: str = ""
    head_sha: str = ""
    is_dirty: bool = False
    modified_files: list[str] = field(default_factory=list)
    untracked_files: list[str] = field(default_factory=list)
    recent_commits: list[str] = field(default_factory=list)
    """One line per commit in the form ``<short-sha> <subject>``."""

    diff_summary: str = ""
    """``git diff --stat`` output, truncated."""


def collect_git_context(
    cwd: Path | None = None,
    *,
    commit_limit: int = 12,
    diff_char_limit: int = 4_000,
) -> GitContext:
    """Best-effort git introspection. Never raises; returns an empty context on failure.

    Reads:
      * ``git branch --show-current``
      * ``git rev-parse HEAD``
      * ``git status --porcelain`` (modified + untracked)
      * ``git log -n <limit> --oneline``
      * ``git diff --stat HEAD`` (capped at ``diff_char_limit``)

    Args:
        cwd: Working directory. Defaults to :func:`Path.cwd`.
        commit_limit: Max commits to include in ``recent_commits``.
        diff_char_limit: Max chars retained from the diff stat.

    Returns:
        A :class:`GitContext` — fields are empty strings/lists on failure.
    """
    work_dir = str(cwd) if cwd is not None else None
    ctx = GitContext()

    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], work_dir)
    if branch:
        ctx.branch = branch
    head = _git(["rev-parse", "HEAD"], work_dir)
    if head:
        ctx.head_sha = head[:12]

    status = _git(["status", "--porcelain"], work_dir)
    if status:
        for line in status.splitlines():
            if len(line) < 4:
                continue
            code = line[:2]
            path = line[3:].strip()
            if code.startswith("??"):
                ctx.untracked_files.append(path)
            else:
                ctx.modified_files.append(path)
        ctx.is_dirty = bool(ctx.modified_files or ctx.untracked_files)

    commits = _git(["log", f"-n{commit_limit}", "--oneline", "--no-decorate"], work_dir)
    if commits:
        ctx.recent_commits = [
            line.strip() for line in commits.splitlines() if line.strip()
        ]

    diff = _git(["diff", "--stat", "HEAD"], work_dir)
    if diff:
        if len(diff) > diff_char_limit:
            diff = diff[: diff_char_limit - 1] + "…"
        ctx.diff_summary = diff
    return ctx


def collect_git_log_between(
    from_ref: str,
    to_ref: str,
    *,
    cwd: Path | None = None,
    commit_limit: int = 200,
) -> list[str]:
    """Return ``git log <from>..<to>`` as oneline entries.

    Empty list on failure or when ``from_ref`` doesn't exist.
    """
    work_dir = str(cwd) if cwd is not None else None
    raw = _git(
        [
            "log",
            f"-n{commit_limit}",
            "--oneline",
            "--no-decorate",
            f"{from_ref}..{to_ref}",
        ],
        work_dir,
    )
    if not raw:
        return []
    return [line.strip() for line in raw.splitlines() if line.strip()]


def previous_tag(current_tag: str, *, cwd: Path | None = None) -> str:
    """Return the tag immediately before ``current_tag``, or empty string.

    Used by ``/release-train`` to default the lower bound of a range
    when the user only provides the new tag.
    """
    work_dir = str(cwd) if cwd is not None else None
    # ``git describe --tags --abbrev=0 <current>^`` returns the latest
    # tag reachable from the parent of the named tag — exactly the
    # "previous release" semantics we want.
    raw = _git(
        ["describe", "--tags", "--abbrev=0", f"{current_tag}^"],
        work_dir,
    )
    return raw or ""


def latest_tag(*, cwd: Path | None = None) -> str:
    """Return the most recent tag in the repository, or empty string."""
    work_dir = str(cwd) if cwd is not None else None
    raw = _git(["describe", "--tags", "--abbrev=0"], work_dir)
    return raw or ""


def _git(args: list[str], cwd: str | None) -> str:
    """Run a git command and return its stdout, or empty on any failure.

    Hardened: never raises, never blocks longer than 5s, never touches
    stderr (which can include credential prompts on misconfigured repos).
    """
    try:
        result = subprocess.run(  # noqa: S603 — controlled argv from this module
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env={
                **os.environ,
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_OPTIONAL_LOCKS": "0",
            },
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


# ---------------------------------------------------------------------------
# Disk persistence
# ---------------------------------------------------------------------------


def feature_state_dir() -> Path:
    """Return ``~/.bog-agents/`` (created if missing)."""
    path = Path.home() / ".bog-agents"
    with contextlib.suppress(OSError):
        path.mkdir(parents=True, exist_ok=True)
    return path


def write_artifact(
    subdir: str, filename: str, content: str, *, suffix: str = ".md"
) -> Path:
    """Write ``content`` to ``~/.bog-agents/<subdir>/<filename><suffix>``.

    Atomic-write via tmp+rename so a crash mid-write doesn't leave a
    half-written file. Returns the resolved final path. ``OSError``
    propagates when the target directory is unwritable.
    """
    target_dir = feature_state_dir() / subdir
    target_dir.mkdir(parents=True, exist_ok=True)
    if not filename.endswith(suffix):
        filename = filename + suffix
    final = target_dir / filename
    tmp = final.with_suffix(final.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(final)
    return final
