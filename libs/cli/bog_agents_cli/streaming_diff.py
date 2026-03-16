"""Streaming diff preview for file edits.

Feature #46: Streaming diff — show file edits in real-time with
unified diff format before they are applied.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass


@dataclass
class DiffChunk:
    """A chunk of a diff with metadata."""

    file_path: str
    """File being modified."""

    old_content: str
    """Original content."""

    new_content: str
    """New content after edit."""

    operation: str = "edit"
    """Operation type: 'edit', 'create', 'delete'."""


def generate_unified_diff(chunk: DiffChunk) -> str:
    """Generate a unified diff string for a file change.

    Args:
        chunk: The diff chunk to format.

    Returns:
        Unified diff string.
    """
    old_lines = chunk.old_content.splitlines(keepends=True)
    new_lines = chunk.new_content.splitlines(keepends=True)

    diff = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=f"a/{chunk.file_path}",
        tofile=f"b/{chunk.file_path}",
        lineterm="",
    )
    return "".join(diff)


def format_diff_for_display(chunk: DiffChunk) -> str:
    """Format a diff for terminal display with markers.

    Args:
        chunk: The diff chunk to format.

    Returns:
        Formatted diff string with +/- markers.
    """
    if chunk.operation == "create":
        lines = chunk.new_content.splitlines()
        header = f"--- /dev/null\n+++ b/{chunk.file_path}\n"
        body = "\n".join(f"+{line}" for line in lines)
        return header + body

    if chunk.operation == "delete":
        lines = chunk.old_content.splitlines()
        header = f"--- a/{chunk.file_path}\n+++ /dev/null\n"
        body = "\n".join(f"-{line}" for line in lines)
        return header + body

    return generate_unified_diff(chunk)


def compute_edit_stats(chunk: DiffChunk) -> dict[str, int]:
    """Compute statistics about a diff.

    Args:
        chunk: The diff chunk to analyze.

    Returns:
        Dict with 'additions', 'deletions', 'unchanged' counts.
    """
    old_lines = set(chunk.old_content.splitlines())
    new_lines = set(chunk.new_content.splitlines())

    additions = len(new_lines - old_lines)
    deletions = len(old_lines - new_lines)
    unchanged = len(old_lines & new_lines)

    return {
        "additions": additions,
        "deletions": deletions,
        "unchanged": unchanged,
    }


def format_edit_summary(chunks: list[DiffChunk]) -> str:
    """Format a summary of multiple file edits.

    Args:
        chunks: List of diff chunks.

    Returns:
        Summary string.
    """
    if not chunks:
        return "No changes."

    total_add = 0
    total_del = 0
    lines = [f"## Changes ({len(chunks)} file{'s' if len(chunks) > 1 else ''})\n"]

    for chunk in chunks:
        stats = compute_edit_stats(chunk)
        total_add += stats["additions"]
        total_del += stats["deletions"]
        lines.append(
            f"  {chunk.file_path}: +{stats['additions']} -{stats['deletions']}"
        )

    lines.append(f"\nTotal: +{total_add} -{total_del}")
    return "\n".join(lines)
