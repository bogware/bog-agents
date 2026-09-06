"""Plan review (ROADMAP #69): one review model for butcher manifests, JTBD specs and plan-mode output.

A plan is text; reviewing it means pointing at lines. `parse_plan` numbers the
lines and tags the ones that are steps, headings or butcher slices;
`PlanReview` holds line-addressed comments and the per-slice checkboxes; the
two renderers turn a review into either a *revision prompt* (comments quoted
against the lines they address, so the planner re-plans exactly what was
questioned) or an *execution brief* (the approved plan with deselected slices
marked skipped). Pure logic: the `PlanReviewScreen` widget and the headless
`--plan --auto` runner both build on it, and it unit-tests without a TUI.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+\S")
_STEP_RE = re.compile(r"^\s*(?:\d+[.)]|[-*]\s*\[[ xX]\]|[-*])\s+\S")
_SLICE_RE = re.compile(r"^\s*(?:#+\s*)?(?:slice|step)\s*(?P<num>\d+)\b", re.IGNORECASE)
MAX_COMMENT_CHARS = 2000


@dataclass(frozen=True)
class PlanLine:
    """One numbered line of a plan."""

    number: int
    text: str
    kind: str = "text"  # text | heading | step | slice
    slice_id: str | None = None

    @property
    def selectable(self) -> bool:
        """Whether the line carries a checkbox (a butcher slice)."""
        return self.slice_id is not None


def parse_plan(text: str) -> list[PlanLine]:
    """Number the lines and classify headings, steps and slices."""
    lines: list[PlanLine] = []
    for index, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.rstrip()
        kind = "text"
        slice_id: str | None = None
        if (match := _SLICE_RE.match(stripped)) and (
            _HEADING_RE.match(stripped)
            or stripped.lower().lstrip("# ").startswith("slice")
        ):
            kind, slice_id = "slice", match.group("num")
        elif _HEADING_RE.match(stripped):
            kind = "heading"
        elif _STEP_RE.match(stripped):
            kind = "step"
        lines.append(
            PlanLine(number=index, text=stripped, kind=kind, slice_id=slice_id)
        )
    return lines


@dataclass
class PlanReview:
    """A plan under review: comments by line and the slice checkboxes."""

    source: str
    title: str
    lines: list[PlanLine]
    comments: dict[int, str] = field(default_factory=dict)
    deselected: set[str] = field(default_factory=set)
    kind: str = "plan"  # plan | butcher | jtbd

    @classmethod
    def from_text(
        cls, text: str, *, source: str = "", title: str = "Plan", kind: str = "plan"
    ) -> PlanReview:
        """Parse `text` into a review."""
        return cls(source=source, title=title, lines=parse_plan(text), kind=kind)

    @property
    def text(self) -> str:
        """The plan as text."""
        return "\n".join(line.text for line in self.lines)

    @property
    def slice_ids(self) -> list[str]:
        """Slice ids in order."""
        return [line.slice_id for line in self.lines if line.slice_id is not None]

    def comment(self, number: int, text: str) -> None:
        """Attach (or clear, with empty text) a comment to line `number`.

        Raises:
            IndexError: For a line number outside the plan.
        """
        if not 1 <= number <= len(self.lines):
            msg = f"line {number} is outside the plan (1..{len(self.lines)})"
            raise IndexError(msg)
        cleaned = " ".join(text.split())[:MAX_COMMENT_CHARS]
        if cleaned:
            self.comments[number] = cleaned
        else:
            self.comments.pop(number, None)

    def toggle(self, slice_id: str) -> bool:
        """Flip a slice checkbox; returns whether it is now selected."""
        if slice_id in self.deselected:
            self.deselected.discard(slice_id)
            return True
        self.deselected.add(slice_id)
        return False

    def selected(self, slice_id: str) -> bool:
        """Whether a slice is selected for execution."""
        return slice_id not in self.deselected

    def revision_prompt(self, *, original_request: str = "") -> str:
        """The re-plan prompt: the plan, then every comment quoted against its line."""
        if not self.comments:
            return ""
        quoted = "\n".join(
            f"- line {n} (`{self.lines[n - 1].text.strip()[:120]}`): {c}"
            for n, c in sorted(self.comments.items())
        )
        skipped = (
            f"\nSlices deselected by the reviewer (drop or fold them in): {', '.join(sorted(self.deselected, key=_slice_key))}\n"
            if self.deselected
            else ""
        )
        head = f"Revise this {self.kind} plan. Keep what was not questioned; change exactly what the comments ask for and say what changed.\n"
        request = (
            f"\nOriginal request:\n{original_request.strip()}\n"
            if original_request.strip()
            else ""
        )
        return (
            f"{head}{request}\nCurrent plan (line numbers for reference):\n"
            + "\n".join(f"{line.number:>3}  {line.text}" for line in self.lines)
            + f"\n\nReviewer comments:\n{quoted}\n{skipped}\nReply with the full revised plan in the same format."
        )

    def execution_brief(self) -> str:
        """The approved plan for execution, with deselected slices marked skipped."""
        parts: list[str] = []
        skip_from: str | None = None
        for line in self.lines:
            if line.slice_id is not None:
                skip_from = line.slice_id if line.slice_id in self.deselected else None
                parts.append(
                    f"{line.text}  [SKIPPED by reviewer]" if skip_from else line.text
                )
                continue
            if skip_from is not None and line.kind == "heading":
                skip_from = None
            parts.append(line.text)
        notes = (
            "\n\nReviewer notes to honour while executing:\n"
            + "\n".join(f"- line {n}: {c}" for n, c in sorted(self.comments.items()))
            if self.comments
            else ""
        )
        return (
            "Execute this approved plan. Do not re-plan; follow it step by step and report deviations.\n\n"
            + "\n".join(parts)
            + notes
        )

    def summary(self) -> str:
        """One line for the status bar / chat."""
        total = len(self.slice_ids)
        chosen = total - len(self.deselected)
        bits = [f"{len(self.lines)} lines", f"{len(self.comments)} comment(s)"]
        if total:
            bits.append(f"{chosen}/{total} slices selected")
        return f"{self.title}: " + ", ".join(bits)


def _slice_key(value: str) -> tuple[int, str]:
    return (int(value), "") if value.isdigit() else (10**9, value)


# --------------------------------------------------------------------------- sources
def render_butcher_manifest(manifest: dict[str, Any]) -> str:
    """A butcher `manifest.json` as reviewable text (one `## Slice N` block per slice)."""
    lines = [f"# {manifest.get('title') or manifest.get('job_id', 'butcher job')}", ""]
    prompt = str(manifest.get("prompt", "")).strip()
    if prompt:
        lines += ["Request:", prompt, ""]
    for raw in manifest.get("slices", []) or []:
        if not isinstance(raw, dict):
            continue
        number = raw.get("number", "?")
        lines.append(f"## Slice {number}: {raw.get('title', '')}".rstrip(": "))
        status = str(raw.get("status", "pending"))
        if status != "pending":
            lines.append(f"status: {status}")
        files = raw.get("files") or []
        if files:
            lines.append("files: " + ", ".join(str(f) for f in files))
        check = raw.get("acceptance_check")
        if check:
            lines.append(f"acceptance: {check}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def load_review(
    kind: str, ref: str, *, project_root: Path, fallback_text: str = ""
) -> PlanReview:
    """Build a review from `butcher <job-id>`, `jtbd <id>`, `file <path>` or `last` (the caller's text).

    Raises:
        FileNotFoundError: When the referenced artifact does not exist.
        ValueError: For an unknown kind.
    """
    root = Path(project_root)
    if kind == "butcher":
        path = root / ".bog-agents" / "butcher" / ref / "manifest.json"
        if not path.is_file():
            msg = f"no butcher job {ref!r} ({path})"
            raise FileNotFoundError(msg)
        manifest = json.loads(path.read_text(encoding="utf-8"))
        return PlanReview.from_text(
            render_butcher_manifest(manifest),
            source=str(path),
            title=str(manifest.get("title") or ref),
            kind="butcher",
        )
    if kind == "jtbd":
        path = root / ".bog-agents" / "jtbd" / ref / "job-spec.md"
        if not path.is_file():
            msg = f"no JTBD spec {ref!r} ({path})"
            raise FileNotFoundError(msg)
        return PlanReview.from_text(
            path.read_text(encoding="utf-8"),
            source=str(path),
            title=f"JTBD {ref}",
            kind="jtbd",
        )
    if kind == "file":
        path = Path(ref) if Path(ref).is_absolute() else root / ref
        if not path.is_file():
            msg = f"no plan file {ref!r}"
            raise FileNotFoundError(msg)
        return PlanReview.from_text(
            path.read_text(encoding="utf-8"),
            source=str(path),
            title=path.name,
            kind="plan",
        )
    if kind == "last":
        if not fallback_text.strip():
            msg = "no plan text to review yet — ask the agent for a plan first"
            raise FileNotFoundError(msg)
        return PlanReview.from_text(
            fallback_text, source="last assistant message", title="Plan", kind="plan"
        )
    msg = f"unknown plan source {kind!r}; use butcher <job-id>, jtbd <id>, file <path> or last"
    raise ValueError(msg)


def apply_slice_selection(manifest_path: Path, deselected: set[str]) -> int:
    """Mark deselected slices `skipped` in a butcher manifest (in place); returns how many changed."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    changed = 0
    for raw in manifest.get("slices", []) or []:
        if not isinstance(raw, dict):
            continue
        number = str(raw.get("number", ""))
        if number in deselected and raw.get("status") != "skipped":
            raw["status"] = "skipped"
            changed += 1
        elif number not in deselected and raw.get("status") == "skipped":
            raw["status"] = "pending"
            changed += 1
    if changed:
        tmp = manifest_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        tmp.replace(manifest_path)
    return changed


@dataclass(frozen=True)
class PlanReviewResult:
    """What the reviewer decided."""

    action: str  # approve | revise | cancel
    review: PlanReview

    @property
    def prompt(self) -> str:
        """The prompt to send for the chosen action (empty for cancel)."""
        if self.action == "approve":
            return self.review.execution_brief()
        if self.action == "revise":
            return self.review.revision_prompt()
        return ""


__all__ = [
    "MAX_COMMENT_CHARS",
    "PlanLine",
    "PlanReview",
    "PlanReviewResult",
    "apply_slice_selection",
    "load_review",
    "parse_plan",
    "render_butcher_manifest",
]
