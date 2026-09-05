"""Proof-ordered diffs (ROADMAP #66): rank changed files by explanatory power.

A reviewer wants the entry point and the public signatures first, the tests
that prove them next, and the lockfile / snapshot churn last (or not at all).
This module is pure: it splits a unified diff into per-file changes, scores
each one from its path and its added lines, and returns them in that order.
The CLI's changes tray, `/diff --ordered` and `render_evidence_markdown` all
use it, so the three surfaces agree on what "first" means.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

_ENTRY_BASENAMES = frozenset(
    {
        "main.py",
        "__main__.py",
        "app.py",
        "cli.py",
        "server.py",
        "index.ts",
        "index.js",
        "index.tsx",
        "main.ts",
        "main.js",
        "main.go",
        "main.rs",
        "lib.rs",
        "mod.rs",
    }
)
_LOCKFILES = frozenset(
    {"uv.lock", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "cargo.lock", "gemfile.lock", "go.sum", "composer.lock"}
)
_SIGNATURE_RE = re.compile(
    r"^\+\s*(async\s+def |def |class |export\s+(default\s+)?(async\s+)?(function|class|const|interface|type)\b"
    r"|public\s+|func |fn |pub\s+fn |interface |type\s+\w+\s*=)"
)
_MIN_BLOCKS_TO_REORDER = 2
_DIFF_HEADER_RE = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+)$", re.MULTILINE)


@dataclass(frozen=True)
class FileChange:
    """One changed file: path, line counts and its own unified diff block."""

    path: str
    added: int = 0
    removed: int = 0
    diff: str = ""

    @property
    def churn(self) -> int:
        """Lines touched."""
        return self.added + self.removed


def _count(diff: str) -> tuple[int, int]:
    added = removed = 0
    for line in diff.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return added, removed


def _path_from_block(block: str) -> str:
    match = _DIFF_HEADER_RE.search(block)
    if match:
        return match.group("b").strip()
    for line in block.splitlines():
        if line.startswith("+++ "):
            candidate = line[4:].split("\t")[0].strip()
            if candidate != "/dev/null":
                return candidate[2:] if candidate.startswith(("a/", "b/")) else candidate
    for line in block.splitlines():
        if line.startswith("--- "):
            candidate = line[4:].split("\t")[0].strip()
            return candidate[2:] if candidate.startswith(("a/", "b/")) else candidate
    return "(unknown)"


def split_unified_diff(diff: str) -> list[FileChange]:
    """Split a multi-file unified diff (git or difflib style) into per-file changes."""
    text = diff.replace("\r\n", "\n")
    if not text.strip():
        return []
    if "diff --git " in text:
        starts = [m.start() for m in re.finditer(r"^diff --git ", text, re.MULTILINE)]
    else:
        starts = [m.start() for m in re.finditer(r"^--- ", text, re.MULTILINE)]
    if not starts:
        added, removed = _count(text)
        return [FileChange(_path_from_block(text), added, removed, text)]
    blocks = [text[s:e] for s, e in zip(starts, [*starts[1:], len(text)], strict=False)]
    out: list[FileChange] = []
    for block in blocks:
        added, removed = _count(block)
        out.append(FileChange(_path_from_block(block), added, removed, block))
    return out


def is_test_path(path: str) -> bool:
    """Whether `path` looks like a test file."""
    p = PurePosixPath(path.replace("\\", "/"))
    name = p.name.lower()
    parts = {part.lower() for part in p.parts[:-1]}
    return (
        bool(parts & {"tests", "test", "__tests__", "spec", "specs"})
        or name.startswith("test_")
        or name.endswith(("_test.py", "_test.go", "_test.rs", ".test.ts", ".test.js", ".test.tsx", ".spec.ts", ".spec.js", ".spec.tsx"))
    )


def is_muted(path: str) -> bool:
    """Lockfiles, snapshots and generated output: shown last and collapsed."""
    p = path.replace("\\", "/").lower()
    name = p.rsplit("/", 1)[-1]
    if name in _LOCKFILES or name.endswith(".lock"):
        return True
    if "__snapshots__" in p or "/snapshots/" in p or name.endswith(".snap"):
        return True
    return bool(
        name.endswith((".min.js", ".min.css", ".pyc", "_pb2.py", "_pb2_grpc.py"))
        or "/dist/" in f"/{p}"
        or "/build/" in f"/{p}"
        or "/node_modules/" in f"/{p}"
        or "/generated/" in f"/{p}"
    )


def score(change: FileChange) -> float:
    """Explanatory-power score: higher shows first."""
    path = change.path.replace("\\", "/")
    name = path.rsplit("/", 1)[-1].lower()
    s = 0.0
    if is_muted(path):
        return -80.0
    if is_test_path(path):
        s -= 40.0
    elif name in _ENTRY_BASENAMES:
        s += 30.0
    elif name == "__init__.py":
        s += 20.0
    if not is_test_path(path):
        signatures = sum(1 for line in change.diff.splitlines() if _SIGNATURE_RE.match(line))
        s += min(30.0, 6.0 * signatures)
    if name.endswith((".md", ".rst", ".txt")):
        s -= 10.0
    elif name.endswith((".toml", ".yaml", ".yml", ".json", ".ini", ".cfg")):
        s -= 5.0
    return s


def rank_changes(changes: list[FileChange]) -> list[FileChange]:
    """Order by score (desc), then churn (desc), then path."""
    return sorted(changes, key=lambda c: (-score(c), -c.churn, c.path))


def reorder_unified_diff(diff: str) -> str:
    """Return `diff` with its per-file blocks in explanatory order (unchanged when unsplittable)."""
    changes = split_unified_diff(diff)
    if len(changes) < _MIN_BLOCKS_TO_REORDER:
        return diff
    return "".join(c.diff if c.diff.endswith("\n") else c.diff + "\n" for c in rank_changes(changes))


def render_ordered_stat(changes: list[FileChange], *, numbered: bool = True) -> str:
    """Render one line per file in explanatory order (`+a/-b`, `[muted]` for collapsed entries)."""
    lines: list[str] = []
    for i, change in enumerate(rank_changes(changes), start=1):
        prefix = f"{i:>2}. " if numbered else "- "
        tag = "  [muted]" if is_muted(change.path) else ("  [test]" if is_test_path(change.path) else "")
        lines.append(f"{prefix}{change.path}  +{change.added}/-{change.removed}{tag}")
    return "\n".join(lines)
