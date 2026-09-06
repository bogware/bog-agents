"""Self-review memo, dispositions and review markers (ROADMAP #67).

A review that re-reads an unchanged diff is wasted money, and a review whose
findings the human already ruled on is noise. This module keeps the two
small records that fix both, under `.bog-agents/self-review/`:

- `<branch>.json` — the memo: which diff (sha256 of the exact text the agent
  would review), against which base, at which effort, when. `--since-last`
  skips when the fingerprint and effort match.
- `dispositions.jsonl` — `/resolve <id> addressed|wontfix|incorrect [note]`
  appends one line per ruling; `lessons_block` turns the `incorrect` and
  `wontfix` rulings into a short "do not repeat" block the next review prompt
  carries, so the loop learns without a model in the middle.

`marker_comment` is the HTML comment a headless run prints (and the post-PR
review carries) so CI can dedupe by fingerprint. Everything here is pure
logic over a repo directory; the git calls go through `hardened_git_env` and
`NO_EXTERNAL_DIFF` like every other internal diff.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess  # noqa: S404 - fixed git argv under hardened_git_env
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from bog_agents.git_env import NO_EXTERNAL_DIFF, hardened_git_env

MEMO_DIRNAME = "self-review"
DISPOSITIONS_FILE = "dispositions.jsonl"
DISPOSITIONS = ("addressed", "wontfix", "incorrect")
EFFORT_LEVELS = ("default", "high")
_MARKER_RE = re.compile(r"<!--\s*bog-review:([0-9a-f]{12})\s*-->")
_GIT_TIMEOUT_S = 30


@dataclass
class SelfReviewMemo:
    """What was reviewed last time on a branch."""

    branch: str
    scope: str
    base: str
    diff_sha: str
    effort: str = "default"
    reviewed_at: float = field(default_factory=time.time)
    verdict: str = ""

    def to_dict(self) -> dict[str, Any]:
        """JSON form."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SelfReviewMemo:
        """Parse a JSON form (unknown keys ignored, missing ones defaulted)."""
        return cls(
            branch=str(data.get("branch", "")),
            scope=str(data.get("scope", "working")),
            base=str(data.get("base", "")),
            diff_sha=str(data.get("diff_sha", "")),
            effort=str(data.get("effort", "default")),
            reviewed_at=float(data.get("reviewed_at", 0.0) or 0.0),
            verdict=str(data.get("verdict", "")),
        )


@dataclass
class Disposition:
    """A human ruling on one finding."""

    finding_id: str
    disposition: str
    note: str = ""
    branch: str = ""
    recorded_at: float = field(default_factory=time.time)


# --------------------------------------------------------------------------- paths


def memo_dir(project_root: str | Path) -> Path:
    """`<project>/.bog-agents/self-review/`."""
    return Path(project_root) / ".bog-agents" / MEMO_DIRNAME


def _safe_name(branch: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", branch.strip() or "detached")[:120]


def memo_path(project_root: str | Path, branch: str) -> Path:
    """Path of the memo for `branch`."""
    return memo_dir(project_root) / f"{_safe_name(branch)}.json"


# --------------------------------------------------------------------------- git


def _git(repo_dir: str | Path, *args: str) -> str:
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv
            ["git", *args],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=_GIT_TIMEOUT_S,
            env=hardened_git_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout if result.returncode == 0 else ""


def current_branch(repo_dir: str | Path) -> str:
    """The checked-out branch name (`detached` when there is none)."""
    return _git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD").strip() or "detached"


def review_diff_text(
    repo_dir: str | Path, *, scope: str = "working", ref: str = ""
) -> str:
    """The exact text a review of `scope` covers — the same commands `/self-review` tells the agent to run."""
    if scope == "staged":
        return _git(repo_dir, "diff", *NO_EXTERNAL_DIFF, "--cached")
    if scope == "branch":
        return _git(repo_dir, "diff", *NO_EXTERNAL_DIFF, f"{ref or 'main'}...HEAD")
    if scope == "commit":
        return _git(repo_dir, "show", *NO_EXTERNAL_DIFF, ref or "HEAD")
    tracked = _git(repo_dir, "diff", *NO_EXTERNAL_DIFF, "HEAD")
    # The memo and dispositions live under .bog-agents/; they must not move the fingerprint.
    untracked = _git(
        repo_dir,
        "ls-files",
        "--others",
        "--exclude-standard",
        "--",
        ".",
        ":(exclude).bog-agents",
    )
    return tracked + ("\n# untracked:\n" + untracked if untracked.strip() else "")


def diff_fingerprint(diff_text: str) -> str:
    """`sha256` of the review text, or `""` for an empty diff."""
    text = diff_text.strip()
    return (
        hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
        if text
        else ""
    )


# --------------------------------------------------------------------------- memo


def load_memo(project_root: str | Path, branch: str) -> SelfReviewMemo | None:
    """The last memo for `branch`, or `None`."""
    path = memo_path(project_root, branch)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return SelfReviewMemo.from_dict(data) if isinstance(data, dict) else None


def save_memo(project_root: str | Path, memo: SelfReviewMemo) -> Path:
    """Write the memo atomically; returns its path."""
    path = memo_path(project_root, memo.branch)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(memo.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    tmp.replace(path)
    return path


def should_skip(memo: SelfReviewMemo | None, *, diff_sha: str, effort: str) -> bool:
    """`--since-last`: skip when the same diff was reviewed at the same (or higher) effort."""
    if memo is None or not diff_sha or memo.diff_sha != diff_sha:
        return False
    rank = {"default": 0, "high": 1}
    memo_rank = rank.get(memo.effort, 1 if memo.effort.startswith("custom:") else 0)
    wanted_rank = rank.get(effort, 1 if effort.startswith("custom:") else 0)
    return memo_rank >= wanted_rank and (
        not effort.startswith("custom:") or memo.effort == effort
    )


# --------------------------------------------------------------------------- markers + effort


def marker_comment(diff_sha: str) -> str:
    """The HTML marker a headless review prints so CI can dedupe on the fingerprint."""
    return f"<!-- bog-review:{(diff_sha or '0' * 12)[:12]} -->"


def parse_marker(text: str) -> str | None:
    """The 12-hex fingerprint inside a marker, or `None`."""
    match = _MARKER_RE.search(text or "")
    return match.group(1) if match else None


def normalize_effort(raw: str) -> str:
    """`default` | `high` | `custom:<rule>`; anything else is an error.

    Raises:
        ValueError: For any other spelling.
    """
    value = (raw or "default").strip()
    if value in EFFORT_LEVELS:
        return value
    if value.lower().startswith("custom:") and value[len("custom:") :].strip():
        return "custom:" + value[len("custom:") :].strip().strip("\"'")
    msg = f'unknown effort {raw!r}; use default, high or custom:"<rule>"'
    raise ValueError(msg)


def effort_rule(effort: str) -> str:
    """Prompt lines for an effort level (empty for `default`)."""
    if effort == "high":
        return (
            "Effort: HIGH. Read every changed file in full, trace each call site of changed "
            "public functions, and run the project's tests and linters yourself before the verdict. "
            "Treat any untested behaviour change as a blocker."
        )
    if effort.startswith("custom:"):
        return f"Effort rule from the user: {effort[len('custom:') :]}"
    return ""


# --------------------------------------------------------------------------- dispositions


def dispositions_path(project_root: str | Path) -> Path:
    """`.bog-agents/self-review/dispositions.jsonl`."""
    return memo_dir(project_root) / DISPOSITIONS_FILE


def record_disposition(
    project_root: str | Path,
    finding_id: str,
    disposition: str,
    *,
    note: str = "",
    branch: str = "",
) -> Disposition:
    """Append a ruling; `disposition` must be one of `DISPOSITIONS`.

    Raises:
        ValueError: For an unknown disposition.
    """
    if disposition not in DISPOSITIONS:
        msg = (
            f"disposition must be one of {', '.join(DISPOSITIONS)}, not {disposition!r}"
        )
        raise ValueError(msg)
    record = Disposition(
        finding_id=finding_id.strip(),
        disposition=disposition,
        note=note.strip(),
        branch=branch,
    )
    path = dispositions_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(asdict(record), sort_keys=True) + "\n")
    return record


def load_dispositions(project_root: str | Path) -> list[Disposition]:
    """Every ruling recorded for the project, oldest first."""
    path = dispositions_path(project_root)
    out: list[Disposition] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if (
            isinstance(data, dict)
            and data.get("finding_id")
            and data.get("disposition") in DISPOSITIONS
        ):
            out.append(
                Disposition(
                    finding_id=str(data["finding_id"]),
                    disposition=str(data["disposition"]),
                    note=str(data.get("note", "")),
                    branch=str(data.get("branch", "")),
                    recorded_at=float(data.get("recorded_at", 0.0) or 0.0),
                )
            )
    return out


def lessons_block(dispositions: list[Disposition], *, limit: int = 12) -> str:
    """Prompt block built from `incorrect` / `wontfix` rulings (newest first), empty when there are none."""
    relevant = [d for d in dispositions if d.disposition in ("incorrect", "wontfix")][
        -limit:
    ]
    if not relevant:
        return ""
    lines = ["## Rulings from previous reviews (do not repeat these)", ""]
    for d in reversed(relevant):
        label = "false positive" if d.disposition == "incorrect" else "accepted as-is"
        note = f" — {d.note}" if d.note else ""
        lines.append(f"- {d.finding_id}: {label}{note}")
    return "\n".join(lines)


__all__ = [
    "DISPOSITIONS",
    "EFFORT_LEVELS",
    "Disposition",
    "SelfReviewMemo",
    "current_branch",
    "diff_fingerprint",
    "dispositions_path",
    "effort_rule",
    "lessons_block",
    "load_dispositions",
    "load_memo",
    "marker_comment",
    "memo_dir",
    "memo_path",
    "normalize_effort",
    "parse_marker",
    "record_disposition",
    "review_diff_text",
    "save_memo",
    "should_skip",
]
