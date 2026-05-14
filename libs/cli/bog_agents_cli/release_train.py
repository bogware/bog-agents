"""`/release-train` — generate release notes, migration guide, and deprecation table.

Given a git tag (or a tag range), this module:

1. Resolves the lower bound — usually the immediately-previous tag.
2. Pulls ``git log <prev>..<new>`` and groups commits by Conventional
   Commit type (feat / fix / chore / refactor / docs / test).
3. Optionally enriches with PR descriptions when ``gh`` is on PATH.
4. Asks the model to produce three artifacts in one pass:
   * user-facing release notes,
   * a migration / upgrade guide for breaking changes,
   * a deprecation table.

The output lands in ``~/.bog-agents/release-notes/<tag>.md`` and is
shown in chat. Everything is best-effort — when ``gh`` is missing or
the tag doesn't exist we degrade to commit-message-only generation.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess  # noqa: S404 — read-only git/gh introspection
import time
from dataclasses import dataclass, field
from pathlib import Path

from bog_agents_cli.feature_helpers import (
    _git,
    collect_git_log_between,
    invoke_model,
    latest_tag,
    previous_tag,
    resolve_active_model_spec,
    write_artifact,
)
from bog_agents_cli.release_train_config import load_release_train_config
from bog_agents_cli.release_train_sources import (
    ResolvedTicket,
    SourceResolution,
    enrich_commits,
)

logger = logging.getLogger(__name__)


RELEASE_TRAIN_SYSTEM_PROMPT = """\
You are a senior release manager writing user-facing release notes for
an open-source library. You will receive:

* the version tag (or tag range) being released,
* a structured commit list grouped by Conventional Commit type,
* optionally, short PR descriptions linked from the commits,
* optionally, indented ``jira:`` / ``halo:`` lines under each commit
  carrying issue/ticket summary, status, type, and fix-version. Use
  these to ground Highlights and Upgrade-guide claims — when a commit
  has a Jira ticket marked as a Bug fix, prefer the ticket summary
  over the raw commit subject because it usually describes the user
  impact more clearly. Never invent ticket data that isn't present.

Produce ONE markdown document with these sections, in order:

# <tag>

> One-sentence headline summary of what users should know.

## Highlights
3 to 6 bullet points calling out the most impactful changes — features
shipped, performance unlocks, breaking changes. Each bullet links the
commit short-sha in backticks.

## Breaking changes
A table (`| Change | Migration |`) listing every `feat!:` / `fix!:` /
`refactor!:` commit. If none, write "_No breaking changes in this release._"

## Deprecations
A table (`| Symbol | Replacement | Removal target |`) listing anything
marked deprecated this cycle. If none, write "_No new deprecations._"

## Upgrade guide
A short step-by-step list, ordered by likelihood the user will need it.
Each step names the file/command to change. Skip this section entirely
if the release contains only minor fixes or chores.

## Full changelog
A list of every commit, grouped by type:
- **Features**
- **Fixes**
- **Refactors**
- **Docs**
- **Tests**
- **Chores**

Each entry is `- {scope}: {subject} ({short-sha})`.

Hard rules:
- Never invent commits or PR descriptions. If a section has no data,
  use the "_No ..._" placeholder.
- Use plain markdown — no HTML, no fenced code unless quoting an actual
  command.
- Keep the headline to one sentence. Total document under ~1200 words.
- Lead with what users care about; ship-day operators read the
  Highlights and Upgrade guide first.
"""


# Conventional Commit prefix → bucket. Order matters — "feat!:" is
# matched before "feat:" so we don't double-bucket.
_TYPE_BUCKETS: tuple[tuple[str, str], ...] = (
    ("feat!", "breaking"),
    ("fix!", "breaking"),
    ("refactor!", "breaking"),
    ("feat", "features"),
    ("fix", "fixes"),
    ("refactor", "refactors"),
    ("docs", "docs"),
    ("test", "tests"),
    ("chore", "chores"),
    ("perf", "performance"),
    ("ci", "ci"),
    ("build", "build"),
    ("style", "style"),
)


@dataclass
class CommitEntry:
    """A single parsed commit."""

    sha: str
    type: str
    """Conventional bucket name (``features``, ``fixes``, etc.; ``other`` if unclassified)."""

    scope: str
    subject: str
    breaking: bool = False
    pr_number: int | None = None
    pr_title: str = ""
    jira_tickets: list[ResolvedTicket] = field(default_factory=list)
    """Jira issues mentioned in subject/PR title, resolved via the configured transport."""

    halo_tickets: list[ResolvedTicket] = field(default_factory=list)
    """Halo tickets mentioned in subject/PR title, resolved via the configured transport."""

    def render(self) -> str:
        """Render as a single bullet for embedding in the prompt."""
        scope = f"({self.scope})" if self.scope else ""
        bang = "!" if self.breaking else ""
        head = f"{self.sha}: {self.type}{scope}{bang}: {self.subject}"
        if self.pr_number:
            head += f"  [PR #{self.pr_number}]"
        if self.pr_title and self.pr_title != self.subject:
            head += f"  — {self.pr_title}"
        for ticket in self.jira_tickets:
            head += f"\n    jira: {ticket.render()}"
        for ticket in self.halo_tickets:
            head += f"\n    halo: {ticket.render()}"
        return head


@dataclass
class ReleaseTrainResult:
    """Outcome of a single release-notes generation."""

    path: Path
    """Where the notes were written."""

    content: str
    """The full rendered markdown document."""

    tag_range: str
    """Human-readable description of the range covered, e.g. ``v0.8.5..v0.8.6``."""

    commits: list[CommitEntry] = field(default_factory=list)
    """Structured commit list, exposed for tests and future tooling."""

    source_resolutions: list[SourceResolution] = field(default_factory=list)
    """One entry per enabled enrichment source describing how it resolved."""

    elapsed_seconds: float = 0.0


_CONVENTIONAL_RE = re.compile(
    r"^([a-zA-Z]+)(\([^)]+\))?(!?):\s*(.+)$",
)
_PR_NUMBER_RE = re.compile(r"#(\d+)")


def parse_commit_line(line: str) -> CommitEntry:
    """Parse a ``--oneline`` git log entry into a structured commit.

    Lines that don't look conventional get bucket ``"other"``.
    """
    line = line.strip()
    if not line:
        return CommitEntry(sha="", type="other", scope="", subject="")
    sha, _, rest = line.partition(" ")
    rest = rest.strip()

    pr_match = _PR_NUMBER_RE.search(rest)
    pr_number = int(pr_match.group(1)) if pr_match else None

    m = _CONVENTIONAL_RE.match(rest)
    if not m:
        return CommitEntry(
            sha=sha, type="other", scope="", subject=rest, pr_number=pr_number
        )
    type_raw = m.group(1).lower()
    scope = (m.group(2) or "").strip("()")
    bang = m.group(3) == "!"
    subject = m.group(4).strip()

    # The bang is captured separately by the regex (``m.group(3)``), so
    # we route on it before falling back to the non-breaking buckets.
    # Otherwise both ``feat!`` and ``feat`` rstrip to ``feat`` and the
    # iteration order in ``_TYPE_BUCKETS`` would mis-bucket plain
    # ``feat`` commits as breaking changes.
    bucket = "other"
    breaking = bang
    if bang and type_raw in {"feat", "fix", "refactor"}:
        bucket = "breaking"
    if not bang or bucket == "other":
        # Plain (non-breaking) buckets, in declaration order.
        for prefix, name in _TYPE_BUCKETS:
            if prefix.endswith("!"):
                continue
            if type_raw == prefix:
                bucket = name
                break

    return CommitEntry(
        sha=sha,
        type=bucket,
        scope=scope,
        subject=subject,
        breaking=breaking,
        pr_number=pr_number,
    )


def enrich_with_pr_titles(
    commits: list[CommitEntry], *, cwd: Path | None = None
) -> None:
    """Fill in ``pr_title`` for each commit whose ``pr_number`` is set.

    Uses ``gh pr view <num> --json title``. Silent no-op when ``gh``
    isn't on PATH or the user isn't authenticated.
    """
    import shutil

    if shutil.which("gh") is None:
        return
    work_dir = str(cwd) if cwd is not None else None
    for c in commits:
        if not c.pr_number or c.pr_title:
            continue
        try:
            result = subprocess.run(  # noqa: S603 — controlled argv
                [
                    "gh",
                    "pr",
                    "view",
                    str(c.pr_number),
                    "--json",
                    "title",
                ],
                cwd=work_dir,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return
        if result.returncode != 0:
            continue
        try:
            data = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            continue
        title = data.get("title")
        if isinstance(title, str):
            c.pr_title = title.strip()


def render_commits_for_prompt(commits: list[CommitEntry], range_label: str) -> str:
    """Render structured commits into a model-friendly prompt body."""
    if not commits:
        return f"Range: {range_label}\n\n(No commits found in this range.)"

    buckets: dict[str, list[CommitEntry]] = {}
    for c in commits:
        buckets.setdefault(c.type, []).append(c)

    lines: list[str] = [f"Range: {range_label}", ""]
    bucket_order = (
        "breaking",
        "features",
        "fixes",
        "refactors",
        "performance",
        "docs",
        "tests",
        "chores",
        "ci",
        "build",
        "style",
        "other",
    )
    for bucket in bucket_order:
        entries = buckets.get(bucket, [])
        if not entries:
            continue
        lines.append(f"### {bucket}")
        for c in entries:
            lines.append(f"- {c.render()}")
        lines.append("")
    return "\n".join(lines)


def resolve_range(spec: str, *, cwd: Path | None = None) -> tuple[str, str, str]:
    """Turn a user-supplied tag spec into ``(from_ref, to_ref, label)``.

    Accepted forms:
      * ``""`` (empty) → defaults to ``<previous_tag>..<latest_tag>``.
      * ``"v0.8.6"`` → resolves previous tag automatically, range is
        ``<prev>..v0.8.6``.
      * ``"v0.8.5..v0.8.6"`` → used verbatim.
      * ``"main"`` / arbitrary ref → previous tag → ref.

    Raises:
        ValueError: When no tags exist or the resolved range is empty.
    """
    spec = spec.strip()
    if not spec:
        to_ref = latest_tag(cwd=cwd)
        if not to_ref:
            msg = "no tags in repository — pass a tag explicitly: /release-train v1.0.0"
            raise ValueError(msg)
        from_ref = previous_tag(to_ref, cwd=cwd)
        if not from_ref:
            # First-ever tag — show everything reachable from the tag.
            return ("", to_ref, f"(initial)..{to_ref}")
        return (from_ref, to_ref, f"{from_ref}..{to_ref}")

    if ".." in spec:
        from_ref, _, to_ref = spec.partition("..")
        from_ref = from_ref.strip()
        to_ref = to_ref.strip()
        if not from_ref or not to_ref:
            msg = f"invalid range {spec!r} — expected <from>..<to>"
            raise ValueError(msg)
        return (from_ref, to_ref, f"{from_ref}..{to_ref}")

    # Single ref form — resolve previous tag as lower bound.
    from_ref = previous_tag(spec, cwd=cwd)
    if not from_ref:
        # No previous tag — show everything reachable.
        return ("", spec, f"(initial)..{spec}")
    return (from_ref, spec, f"{from_ref}..{spec}")


async def generate_release_notes(
    *,
    model: object,
    commits: list[CommitEntry],
    tag_range: str,
) -> str:
    """Invoke the model with structured commits; return the rendered notes."""
    body = render_commits_for_prompt(commits, tag_range)
    return await invoke_model(
        model,  # type: ignore[arg-type]
        RELEASE_TRAIN_SYSTEM_PROMPT,
        body,
        timeout_seconds=120.0,
    )


async def run_release_train(app: object, raw_arg: str) -> ReleaseTrainResult:
    """End-to-end ``/release-train`` flow.

    Args:
        app: The running ``BogAgentsApp`` instance.
        raw_arg: Whatever followed the slash-command name (a tag, a range,
            or empty for "latest").

    Returns:
        A :class:`ReleaseTrainResult` describing the generated document
        and the commit data used to produce it.

    Raises:
        RuntimeError: If no model spec is configured. ``ValueError`` may
            also propagate from :func:`resolve_range` when the range
            cannot be parsed.
    """
    from bog_agents_cli.config import create_model_with_fallback

    cwd = Path(getattr(app, "_cwd", Path.cwd()))
    from_ref, to_ref, label = resolve_range(raw_arg, cwd=cwd)

    if from_ref:
        log_lines = collect_git_log_between(from_ref, to_ref, cwd=cwd)
    else:
        # No lower bound — pull everything reachable from to_ref.
        raw = _git(["log", "--oneline", "--no-decorate", to_ref], str(cwd))
        log_lines = (
            [line.strip() for line in raw.splitlines() if line.strip()] if raw else []
        )

    commits = [parse_commit_line(line) for line in log_lines]
    commits = [c for c in commits if c.sha]
    enrich_with_pr_titles(commits, cwd=cwd)

    rt_config = load_release_train_config()
    source_resolutions: list[SourceResolution] = []
    if rt_config.any_enabled:
        source_resolutions = await enrich_commits(commits, rt_config)

    spec = resolve_active_model_spec(app)
    if not spec:
        msg = "no active model — run /model first or set a default"
        raise RuntimeError(msg)
    profile = getattr(app, "_profile_override", None)
    model_result = create_model_with_fallback(spec, profile_overrides=profile)

    start = time.monotonic()
    body = await generate_release_notes(
        model=model_result.model,
        commits=commits,
        tag_range=label,
    )
    elapsed = time.monotonic() - start

    safe_label = label.replace("..", "_to_").replace("/", "_")
    filename = f"{safe_label}.md"
    path = write_artifact(
        "release-notes",
        filename,
        _wrap_with_frontmatter(body, label, spec, len(commits)),
    )
    return ReleaseTrainResult(
        path=path,
        content=body,
        tag_range=label,
        commits=commits,
        source_resolutions=source_resolutions,
        elapsed_seconds=elapsed,
    )


def _wrap_with_frontmatter(
    body: str, label: str, model_spec: str, commit_count: int
) -> str:
    lines = [
        "---",
        f"range: {label}",
        f"commits: {commit_count}",
        f"model: {model_spec}",
        f"generated: {time.strftime('%Y-%m-%dT%H:%M:%S')}",
        "kind: release-notes",
        "---",
        "",
        body,
    ]
    return "\n".join(lines)
