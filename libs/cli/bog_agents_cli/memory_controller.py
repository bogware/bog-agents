"""`/memory` (ROADMAP #75): rebuild the agent-recorded memories into a reviewed candidate, then apply or discard.

Verbs: `rebuild [--global] [--threads N] [--dedup] [--steer "…"]`, `show`
(the pending diff), `apply`, `discard`, `status`. The transcript source and
the model call are injected so the body unit-tests without a store or a
provider; the App wiring supplies the checkpointer's recent threads and the
active model (local-model friendly: `--dedup` needs no model at all).
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from bog_agents_cli.memory_rebuild import (
    Candidate,
    apply_candidate,
    discard_candidate,
    pending_candidate,
    rebuild,
    rebuild_dir,
)

logger = logging.getLogger(__name__)

USAGE = (
    'Usage: /memory rebuild [--global] [--threads N] [--dedup] [--steer "…"] | /memory show | '
    "/memory apply | /memory discard | /memory status"
)
DEFAULT_THREADS = 10
MAX_THREADS = 50
TranscriptLoader = Callable[[int], Awaitable[list[tuple[str, str]]]]
"""`(limit) -> [(thread id, transcript text), …]`, newest first."""


def project_memory_target(project_root: Path) -> Path:
    """The project store the `remember` tool appends to (`AGENTS.md` at the root)."""
    return Path(project_root) / "AGENTS.md"


def global_memory_target() -> Path:
    """`~/.bog-agents/memory.md`."""
    from bog_agents_cli.auto_memory import _GLOBAL_MEMORY

    return _GLOBAL_MEMORY


def _message_text(message: object) -> str:
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(p.get("text", "")) if isinstance(p, dict) else str(p) for p in content
        )
    return ""


def transcript_text(messages: list[object], *, max_chars: int = 6000) -> str:
    """Human / AI turns of a thread as `role: text` lines, bounded from the end."""
    lines: list[str] = []
    for message in messages:
        kind = getattr(message, "type", None)
        if kind not in {"human", "ai"}:
            continue
        text = " ".join(_message_text(message).split())
        if text:
            lines.append(f"{kind}: {text[:1200]}")
    joined = "\n".join(lines)
    return joined[-max_chars:] if len(joined) > max_chars else joined


async def load_recent_transcripts(limit: int) -> list[tuple[str, str]]:
    """Recent threads from the checkpointer as `(thread id, transcript)` pairs (best-effort)."""
    try:
        from bog_agents_cli.sessions import get_thread_checkpoint_payload, list_threads

        threads = await list_threads(limit=limit)
    except Exception:
        return []
    out: list[tuple[str, str]] = []
    for info in threads:
        checkpoint = info.get("latest_checkpoint_id")
        if not checkpoint:
            continue
        try:
            payload = await get_thread_checkpoint_payload(
                info["thread_id"], str(checkpoint)
            )
        except Exception:
            logger.debug(
                "transcript load failed for %s", info["thread_id"], exc_info=True
            )
            continue
        if payload is None:
            continue
        text = transcript_text(list(payload.get("messages", [])))
        if text:
            out.append((info["thread_id"], text))
    return out


def parse_rebuild_args(tokens: list[str]) -> dict[str, Any]:
    """`{global, threads, dedup, steer}` from the `/memory rebuild` tail.

    Raises:
        ValueError: On a non-numeric or out-of-range `--threads`.
    """
    opts: dict[str, Any] = {
        "global": False,
        "threads": DEFAULT_THREADS,
        "dedup": False,
        "steer": "",
    }
    rest = list(tokens)
    while rest:
        token = rest.pop(0)
        if token == "--global":
            opts["global"] = True
        elif token == "--dedup":
            opts["dedup"] = True
        elif token == "--threads":
            try:
                opts["threads"] = int(rest.pop(0))
            except (IndexError, ValueError) as exc:
                msg = "--threads needs a whole number"
                raise ValueError(msg) from exc
            if not 0 <= opts["threads"] <= MAX_THREADS:
                msg = f"--threads must be between 0 and {MAX_THREADS}"
                raise ValueError(msg)
        elif token == "--steer":
            opts["steer"] = rest.pop(0) if rest else ""
        else:
            opts["steer"] = (opts["steer"] + " " + token).strip()
    return opts


def model_invoke(app: Any) -> Callable[[str], str]:  # noqa: ANN401 - the App
    """A sync `invoke` on the App's active model (the rebuild runs it off the event loop)."""
    from bog_agents_cli.config import create_model, settings

    spec = getattr(app, "_model_override", None) or settings.model_name
    model = create_model(
        spec, profile_overrides=getattr(app, "_profile_override", None)
    ).model

    def _invoke(prompt: str) -> str:
        return _message_text(model.invoke(prompt))

    return _invoke


async def run_memory_from_app(app: Any, command: str) -> str:  # noqa: ANN401 - the App
    """`/memory` with the App's model (skipped for `--dedup`) and the checkpointer's transcripts."""
    from bog_agents_cli.findings_controller import project_root

    invoke = None if " --dedup" in f"{command} " else model_invoke(app)
    return await run_memory_command(
        command,
        project_root(app),
        invoke=invoke,
        load_transcripts=load_recent_transcripts,
    )


def describe_candidate(candidate: Candidate) -> str:
    """Report + diff (bounded) for the terminal."""
    text = candidate.report.summary()
    if not candidate.changed:
        return f"{text}\n\nNo changes — the store is already consolidated; nothing to apply."
    diff = candidate.diff
    if len(diff) > 6000:
        diff = (
            diff[:6000]
            + f"\n… ({len(candidate.diff) - 6000} more characters in {candidate.diff_path})"
        )
    return f"{text}\n\nCandidate written to {candidate.path}\n\n{diff}\n\n/memory apply swaps it in (backup kept); /memory discard drops it."


async def run_memory_command(
    command: str,
    project_root: Path,
    *,
    invoke: Callable[[str], str] | None = None,
    load_transcripts: TranscriptLoader | None = None,
) -> str:
    """Body of `/memory`."""
    try:
        tokens = shlex.split(command.strip())[1:]
    except ValueError as exc:
        return f"Could not parse arguments: {exc}\n{USAGE}"
    verb = tokens[0].lower() if tokens else "status"
    rest = tokens[1:]
    if verb in {"help", "-h", "--help"}:
        return USAGE
    if verb == "status":
        pending = pending_candidate(project_root)
        if pending is None:
            return f"No memory rebuild candidate pending (project store: {project_memory_target(project_root)}; global: {global_memory_target()}).\n{USAGE}"
        return f"Candidate pending for {pending[1]}: {pending[0]} — /memory show, /memory apply, /memory discard."
    if verb == "rebuild":
        try:
            opts = parse_rebuild_args(rest)
        except ValueError as exc:
            return f"{exc}\n{USAGE}"
        target = (
            global_memory_target()
            if opts["global"]
            else project_memory_target(project_root)
        )
        transcripts: list[tuple[str, str]] = []
        if opts["threads"] and load_transcripts is not None:
            transcripts = await load_transcripts(opts["threads"])
        model_invoke = None if opts["dedup"] else invoke
        candidate = await asyncio.to_thread(
            rebuild,
            target,
            project_root=project_root,
            transcripts=transcripts,
            invoke=model_invoke,
            steer=opts["steer"],
        )
        return describe_candidate(candidate)
    if verb == "show":
        pending = pending_candidate(project_root)
        if pending is None:
            return "No candidate pending; run /memory rebuild first."
        diff_path = rebuild_dir(project_root) / "candidate.diff"
        diff = diff_path.read_text(encoding="utf-8") if diff_path.is_file() else ""
        return f"Candidate for {pending[1]}:\n\n{diff or '(no differences)'}"
    if verb == "apply":
        try:
            backup = apply_candidate(project_root)
        except FileNotFoundError:
            return "No candidate pending; run /memory rebuild first."
        return f"Applied the rebuilt memory (backup at {backup}). It loads on the next session."
    if verb == "discard":
        return (
            "Candidate discarded."
            if discard_candidate(project_root)
            else "No candidate pending."
        )
    return f"Unknown verb {verb!r}.\n{USAGE}"


__all__ = [
    "USAGE",
    "describe_candidate",
    "global_memory_target",
    "load_recent_transcripts",
    "model_invoke",
    "parse_rebuild_args",
    "project_memory_target",
    "run_memory_command",
    "run_memory_from_app",
    "transcript_text",
]
