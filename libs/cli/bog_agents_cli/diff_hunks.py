"""Unified-diff hunks (ROADMAP #66): parse, render, and revert one hunk in place.

Pure text logic behind `/changes revert <n> <hunk>`: a hunk's *new* side is
located in the current file text (exact contiguous match, nearest to where
the diff said it was) and replaced by its *old* side. No git required, so it
works on untracked files and inside worktrees alike.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HUNK_RE = re.compile(
    r"^@@ -(?P<os>\d+)(?:,(?P<ol>\d+))? \+(?P<ns>\d+)(?:,(?P<nl>\d+))? @@(?P<rest>.*)$"
)


@dataclass
class Hunk:
    """One `@@` block of a unified diff.

    Attributes:
        old_start: 1-based line where the hunk starts in the old text.
        old_len: Old-side line count.
        new_start: 1-based line where the hunk starts in the new text.
        new_len: New-side line count.
        lines: Body lines with their leading ` `, `-` or `+` marker.
        header: The raw `@@ … @@` line.
    """

    old_start: int
    old_len: int
    new_start: int
    new_len: int
    lines: list[str] = field(default_factory=list)
    header: str = ""

    @property
    def added(self) -> int:
        """Added line count."""
        return sum(1 for line in self.lines if line.startswith("+"))

    @property
    def removed(self) -> int:
        """Removed line count."""
        return sum(1 for line in self.lines if line.startswith("-"))


def parse_hunks(diff: str) -> list[Hunk]:
    """Parse every hunk from a single-file unified diff (headers are skipped)."""
    hunks: list[Hunk] = []
    current: Hunk | None = None
    for raw in diff.replace("\r\n", "\n").split("\n"):
        match = _HUNK_RE.match(raw)
        if match:
            current = Hunk(
                old_start=int(match.group("os")),
                old_len=int(match.group("ol") or 1),
                new_start=int(match.group("ns")),
                new_len=int(match.group("nl") or 1),
                header=raw,
            )
            hunks.append(current)
            continue
        if current is None:
            continue
        if raw.startswith(("+++", "---")) and not current.lines:
            continue
        if raw.startswith(("+", "-", " ")):
            current.lines.append(raw)
        elif not raw and current.lines:
            # difflib emits no trailing marker for an empty context line inside a hunk
            current.lines.append(" ")
        elif raw.startswith("\\"):
            continue  # "\ No newline at end of file"
    return hunks


def new_side(hunk: Hunk) -> list[str]:
    """Lines as they read in the new text (context + additions)."""
    return [line[1:] for line in hunk.lines if line.startswith((" ", "+"))]


def old_side(hunk: Hunk) -> list[str]:
    """Lines as they read in the old text (context + removals)."""
    return [line[1:] for line in hunk.lines if line.startswith((" ", "-"))]


def _find_block(haystack: list[str], needle: list[str], *, near: int) -> int | None:
    if not needle:
        return None
    candidates = [
        i
        for i in range(len(haystack) - len(needle) + 1)
        if haystack[i : i + len(needle)] == needle
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda i: abs(i - near))


def revert_hunk(current: str, hunk: Hunk) -> str | None:
    """Undo one hunk in `current`; `None` when its new side is no longer present.

    Args:
        current: The file's current text.
        hunk: The hunk to revert.

    Returns:
        The reverted text (trailing newline preserved), or `None`.
    """
    trailing = current.endswith("\n")
    lines = current.split("\n")
    if trailing:
        lines = lines[:-1]
    target = new_side(hunk)
    replacement = old_side(hunk)
    if not target:
        # pure addition with no context: cannot locate safely
        return None
    index = _find_block(lines, target, near=max(0, hunk.new_start - 1))
    if index is None:
        return None
    out = lines[:index] + replacement + lines[index + len(target) :]
    return "\n".join(out) + ("\n" if trailing else "")


def render_hunk(hunk: Hunk) -> str:
    """The hunk as diff text (header + body)."""
    return "\n".join([hunk.header, *hunk.lines])
