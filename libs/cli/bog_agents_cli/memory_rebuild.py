"""Memory rebuild (ROADMAP #75): consolidate the agent-recorded memories into a reviewed candidate.

The auto-memory store (`## Agent-Recorded Memories` in `AGENTS.md` or
`~/.bog-agents/memory.md`) grows by appends; nothing ever merges duplicates
or retires a fact that a later session contradicted. `rebuild()` reads the
current entries plus recent transcripts, runs a steerable consolidation
(dedup, contradiction resolution, provenance kept per entry) through an
injected `invoke` — or a pure, deterministic dedup when no model is given —
and writes a *candidate* under `.bog-agents/memory.rebuild/` with a diff and a
report. Nothing touches the live file until `apply_candidate()` swaps it in
(backup kept). Pure logic; unit-tests without a model.
"""

from __future__ import annotations

import difflib
import json
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from bog_agents_cli.auto_memory import _PROVENANCE, _SECTION

REBUILD_RELATIVE = Path(".bog-agents") / "memory.rebuild"
CANDIDATE_NAME = "memory.md"
REPORT_NAME = "report.json"
DIFF_NAME = "candidate.diff"
MAX_TRANSCRIPT_CHARS = 6000
MAX_ENTRIES = 400
_ENTRY_RE = re.compile(
    r"^\s*-\s*\((?P<category>[a-z0-9_-]+)\)\s*(?P<text>.+?)\s*$", re.IGNORECASE
)
_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class MemoryEntry:
    """One `- (category) fact` line."""

    text: str
    category: str = "note"
    sources: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        """Normalised text used for dedup."""
        return _WS.sub(" ", self.text.strip().rstrip(".")).lower()

    def render(self) -> str:
        """The store line."""
        return f"- ({self.category}) {self.text}"


def parse_entries(markdown: str) -> tuple[str, list[MemoryEntry], str]:
    """Split a memory file into `(text before the managed section, entries, text after)`.

    Files without the managed section yield `(whole text, [], "")`.
    """
    if _SECTION not in markdown:
        return markdown, [], ""
    start = markdown.index(_SECTION)
    body_start = start + len(_SECTION)
    next_heading = markdown.find("\n## ", body_start)
    body = (
        markdown[body_start:]
        if next_heading == -1
        else markdown[body_start:next_heading]
    )
    after = "" if next_heading == -1 else markdown[next_heading:]
    entries = [
        MemoryEntry(text=m.group("text"), category=m.group("category").lower())
        for line in body.splitlines()
        if (m := _ENTRY_RE.match(line))
    ]
    return markdown[:start], entries, after


def render_section(entries: Sequence[MemoryEntry]) -> str:
    """The managed section for `entries`."""
    lines = [_SECTION, _PROVENANCE, ""]
    lines.extend(e.render() for e in entries)
    return "\n".join(lines).rstrip() + "\n"


def compose(before: str, entries: Sequence[MemoryEntry], after: str) -> str:
    """Rebuild a memory file around a new managed section."""
    head = before.rstrip()
    section = render_section(entries)
    parts = [head, section] if head else [section]
    text = "\n\n".join(p for p in parts if p)
    if after:
        text = text.rstrip() + "\n" + after
    return text if text.endswith("\n") else text + "\n"


def dedup_entries(
    entries: Sequence[MemoryEntry],
) -> tuple[list[MemoryEntry], list[str]]:
    """Deterministic consolidation: drop exact / whitespace-only duplicates, keep first seen; returns `(kept, notes)`."""
    seen: dict[str, MemoryEntry] = {}
    notes: list[str] = []
    for entry in entries:
        key = entry.key
        if key in seen:
            notes.append(f"dropped duplicate: {entry.render()}")
            continue
        seen[key] = entry
    return list(seen.values()), notes


@dataclass
class RebuildReport:
    """What the rebuild did, for review."""

    mode: str
    steer: str
    before_count: int
    after_count: int
    transcripts: int
    notes: list[str] = field(default_factory=list)
    entries: list[dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready mapping."""
        return asdict(self)

    def summary(self) -> str:
        """One paragraph for the terminal."""
        line = f"Memory rebuild ({self.mode}): {self.before_count} → {self.after_count} entries from {self.transcripts} transcript(s)"
        if self.steer:
            line += f", steered by: {self.steer!r}"
        if self.notes:
            line += "\n" + "\n".join(f"  - {n}" for n in self.notes[:20])
            if len(self.notes) > 20:
                line += f"\n  … {len(self.notes) - 20} more"
        return line


CONSOLIDATE_PROMPT = """You maintain an engineering agent's long-term memory: short durable facts about a codebase or a user's preferences.
Rewrite the memory so it is smaller and truer, using the transcripts as newer evidence:
- merge entries that say the same thing (keep the more specific wording);
- when two entries contradict, keep the one the transcripts support (or the newer one) and drop the other;
- drop entries the transcripts show to be obsolete; keep everything still true even if unmentioned;
- add a new entry only for a durable fact stated plainly in a transcript (not tasks, not one-off details);
- keep each entry one sentence, category one of convention / decision / gotcha / fix-pattern / note.
{steer}
Reply with JSON only: {{"entries": [{{"text": "...", "category": "...", "sources": ["entry:3", "thread:abc"]}}, ...], "notes": ["what changed and why", ...]}}
`sources` cites the entry numbers below and/or thread ids that justify the entry.

## Current entries
{entries}

## Recent transcripts
{transcripts}
"""


def _strip_fences(text: str) -> str:
    match = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
    return (match.group(1) if match else text).strip()


def consolidate(
    entries: Sequence[MemoryEntry],
    transcripts: Sequence[tuple[str, str]],
    *,
    invoke: Callable[[str], str] | None,
    steer: str = "",
) -> tuple[list[MemoryEntry], RebuildReport]:
    """Consolidate `entries` (with `transcripts` as evidence) into a new list plus a report.

    With `invoke=None`, or when the model's reply is unusable, the result is the
    deterministic dedup (`mode="dedup"`), never nothing.
    """
    deduped, notes = dedup_entries(entries)
    if invoke is None:
        return deduped, RebuildReport(
            mode="dedup",
            steer=steer,
            before_count=len(entries),
            after_count=len(deduped),
            transcripts=len(transcripts),
            notes=notes,
        )
    numbered = (
        "\n".join(f"{i + 1}. ({e.category}) {e.text}" for i, e in enumerate(entries))
        or "(none)"
    )
    excerpts = (
        "\n\n".join(
            f"### thread:{tid}\n{body[:MAX_TRANSCRIPT_CHARS]}"
            for tid, body in transcripts
        )
        or "(none)"
    )
    prompt = CONSOLIDATE_PROMPT.format(
        steer=f"Operator steering: {steer}" if steer else "",
        entries=numbered,
        transcripts=excerpts,
    )
    try:
        raw = invoke(prompt)
        data = json.loads(_strip_fences(raw))
        items = data["entries"] if isinstance(data, dict) else data
        if not isinstance(items, list):
            items = None
    except Exception as exc:
        items = None
        reason = str(exc)
    else:
        reason = "entries is not a list"
    if items is None:
        notes.append(
            f"model consolidation unusable ({reason}); kept the deterministic dedup"
        )
        return deduped, RebuildReport(
            mode="dedup",
            steer=steer,
            before_count=len(entries),
            after_count=len(deduped),
            transcripts=len(transcripts),
            notes=notes,
        )
    rebuilt: list[MemoryEntry] = []
    for item in items[:MAX_ENTRIES]:
        if not isinstance(item, dict):
            continue
        text = _WS.sub(" ", str(item.get("text", ""))).strip()
        if not text:
            continue
        category = str(item.get("category", "note")).strip().lower() or "note"
        sources = item.get("sources", [])
        rebuilt.append(
            MemoryEntry(
                text=text,
                category=category,
                sources=tuple(str(s) for s in sources)
                if isinstance(sources, list)
                else (),
            )
        )
    rebuilt, dup_notes = dedup_entries(rebuilt)
    model_notes = data.get("notes", []) if isinstance(data, dict) else []
    all_notes = [str(n) for n in model_notes if isinstance(n, str)] + dup_notes
    report = RebuildReport(
        mode="model",
        steer=steer,
        before_count=len(entries),
        after_count=len(rebuilt),
        transcripts=len(transcripts),
        notes=all_notes,
        entries=[
            {"text": e.text, "category": e.category, "sources": list(e.sources)}
            for e in rebuilt
        ],
    )
    return rebuilt, report


def rebuild_dir(project_root: str | Path) -> Path:
    """`<root>/.bog-agents/memory.rebuild`."""
    return Path(project_root) / REBUILD_RELATIVE


def render_diff(current: str, candidate: str, *, name: str = "memory.md") -> str:
    """Unified diff current → candidate."""
    return "".join(
        difflib.unified_diff(
            current.splitlines(keepends=True),
            candidate.splitlines(keepends=True),
            fromfile=f"a/{name}",
            tofile=f"b/{name}",
        )
    )


@dataclass
class Candidate:
    """A written candidate: where it is and what changed."""

    target: Path
    path: Path
    diff_path: Path
    report_path: Path
    diff: str
    report: RebuildReport

    @property
    def changed(self) -> bool:
        """Whether the candidate differs from the live file."""
        return bool(self.diff.strip())


def rebuild(
    target: Path,
    *,
    project_root: str | Path,
    transcripts: Sequence[tuple[str, str]] = (),
    invoke: Callable[[str], str] | None = None,
    steer: str = "",
) -> Candidate:
    """Read `target`, consolidate, and write the candidate + diff + report under the rebuild dir."""
    current = target.read_text(encoding="utf-8") if target.is_file() else ""
    before, entries, after = parse_entries(current)
    rebuilt, report = consolidate(entries, transcripts, invoke=invoke, steer=steer)
    candidate_text = compose(before, rebuilt, after)
    directory = rebuild_dir(project_root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / CANDIDATE_NAME
    path.write_text(candidate_text, encoding="utf-8")
    diff = render_diff(current, candidate_text, name=target.name)
    diff_path = directory / DIFF_NAME
    diff_path.write_text(diff, encoding="utf-8")
    report_path = directory / REPORT_NAME
    payload = {**report.to_dict(), "target": str(target)}
    report_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return Candidate(
        target=target,
        path=path,
        diff_path=diff_path,
        report_path=report_path,
        diff=diff,
        report=report,
    )


def pending_candidate(project_root: str | Path) -> tuple[Path, Path] | None:
    """`(candidate path, target)` when a candidate is waiting for approval."""
    directory = rebuild_dir(project_root)
    path, report_path = directory / CANDIDATE_NAME, directory / REPORT_NAME
    if not path.is_file() or not report_path.is_file():
        return None
    try:
        target = Path(json.loads(report_path.read_text(encoding="utf-8"))["target"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return path, target


def apply_candidate(project_root: str | Path) -> Path:
    """Swap the pending candidate into its target (backup kept beside the candidate); returns the backup path.

    Raises:
        FileNotFoundError: When no candidate is pending.
    """
    pending = pending_candidate(project_root)
    if pending is None:
        msg = "no memory rebuild candidate is pending"
        raise FileNotFoundError(msg)
    path, target = pending
    directory = rebuild_dir(project_root)
    backup = directory / f"backup-{time.strftime('%Y%m%d-%H%M%S')}.md"
    if target.is_file():
        backup.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
    else:
        backup.write_text("", encoding="utf-8")
    from bog_agents_cli.io_utils import atomic_write_text

    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target, path.read_text(encoding="utf-8"), encoding="utf-8")
    path.unlink(missing_ok=True)
    return backup


def discard_candidate(project_root: str | Path) -> bool:
    """Delete the pending candidate; `True` when one existed."""
    pending = pending_candidate(project_root)
    if pending is None:
        return False
    pending[0].unlink(missing_ok=True)
    return True


__all__ = [
    "CANDIDATE_NAME",
    "CONSOLIDATE_PROMPT",
    "Candidate",
    "MemoryEntry",
    "RebuildReport",
    "apply_candidate",
    "compose",
    "consolidate",
    "dedup_entries",
    "discard_candidate",
    "parse_entries",
    "pending_candidate",
    "rebuild",
    "rebuild_dir",
    "render_diff",
    "render_section",
]
