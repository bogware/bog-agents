"""Consolidated git-command classifier.

Answers "is this git invocation read-only, mutating, or destructive?" in one
place so every approval layer classifies identically. The regexes were
previously fragmented across `auto_mode._DEFAULT_SHELL_ASK_PATTERNS`,
`auto_mode._DEFAULT_SHELL_ALLOW_PATTERNS`, the SDK's `exec_risk.py`, and
`local_shell._DANGEROUS_PATTERNS` — a force-push spelled `git push -ff`
slipped through all of them.

Pure static analysis (no shell execution), so it is deterministic and safe to
call on any command string, including chained commands (`cd x && git ...`).

Severity semantics for callers:
  - `READ_ONLY` — safe to auto-approve (status/log/diff/...).
  - `MUTATING` — normal write operations (add/commit/push/merge/...).
  - `DESTRUCTIVE` — history/worktree rewrites that should always ask
    (force-push, `reset --hard`, `clean -f`, `branch -D`, `stash drop`, ...).
"""

from __future__ import annotations

import re
import shlex
from enum import StrEnum

__all__ = ["GitOpType", "classify_git_command"]


class GitOpType(StrEnum):
    """How risky a git invocation is, from an approval standpoint."""

    READ_ONLY = "read_only"
    MUTATING = "mutating"
    DESTRUCTIVE = "destructive"


_SEVERITY: dict[GitOpType, int] = {
    GitOpType.READ_ONLY: 0,
    GitOpType.MUTATING: 1,
    GitOpType.DESTRUCTIVE: 2,
}

_GIT_WORD = re.compile(r"\bgit(?:\.exe)?\b", re.IGNORECASE)
_SEGMENT_SPLIT = re.compile(r"\s*(?:&&|\|\||;|\||>|>>|<)\s*")
_ENV_PREFIX = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PREFIX_WORDS = frozenset({"sudo", "nohup", "env", "command", "time", "timeout"})
_GIT_BIN = frozenset({"git", "git.exe"})

# Subcommands that never touch the worktree or history.
_READONLY_OPS = frozenset(
    {
        "status",
        "log",
        "diff",
        "show",
        "blame",
        "grep",
        "ls-files",
        "ls-tree",
        "cat-file",
        "describe",
        "shortlog",
        "for-each-ref",
        "rev-parse",
        "count-objects",
        "help",
        "version",
        "whatchanged",
    }
)


def classify_git_command(command: str) -> GitOpType | None:
    """Classify a shell command by its git invocations.

    Args:
        command: The raw shell command string.

    Returns:
        The riskiest git operation in `command`, or `None` when the command
        contains no git invocation.
    """
    if not _GIT_WORD.search(command):
        return None
    verdict: GitOpType | None = None
    for segment in _SEGMENT_SPLIT.split(command):
        segment_type = _classify_segment(segment.strip())
        if segment_type is not None and _SEVERITY[segment_type] > _SEVERITY.get(
            verdict, -1
        ):
            verdict = segment_type
    return verdict


def _tokens(segment: str) -> list[str]:
    """Tokenize a segment; fall back to whitespace splitting on quote errors."""
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.split()


def _classify_segment(segment: str) -> GitOpType | None:
    """Classify a single chained segment that may start with a git command."""
    tokens = _tokens(segment)
    while tokens and (
        _ENV_PREFIX.match(tokens[0]) or tokens[0].lower() in _PREFIX_WORDS
    ):
        tokens = tokens[1:]
    if not tokens or tokens[0].lower() not in _GIT_BIN:
        return None
    if len(tokens) == 1:
        return GitOpType.MUTATING
    return _classify_subcommand(tokens[1].lower(), tokens[2:])


def _classify_subcommand(sub: str, rest: list[str]) -> GitOpType:
    """Classify a git subcommand plus its argument tokens."""
    if sub in _READONLY_OPS:
        return GitOpType.READ_ONLY
    if sub == "branch":
        if _has_flag(rest, {"-d", "-D", "--delete", "--force"}):
            return GitOpType.DESTRUCTIVE
        if _has_flag(rest, {"-m", "-M", "-c", "-C", "--move", "--copy"}):
            return GitOpType.MUTATING
        if rest and not rest[0].startswith("-"):
            return GitOpType.MUTATING  # `git branch <name>` creates a branch
        return GitOpType.READ_ONLY
    if sub == "tag":
        if _has_flag(rest, {"-d", "-D", "--delete"}):
            return GitOpType.DESTRUCTIVE
        if _has_flag(rest, {"-a", "-s", "-m", "--annotate", "--sign"}):
            return GitOpType.MUTATING
        if rest and not rest[0].startswith("-"):
            return GitOpType.MUTATING  # `git tag <name>` creates a tag
        return GitOpType.READ_ONLY
    if sub == "remote":
        if rest and rest[0] in {
            "add",
            "rename",
            "remove",
            "set-url",
            "prune",
            "update",
        }:
            return GitOpType.MUTATING
        return GitOpType.READ_ONLY
    if sub == "config":
        if _has_flag(rest, {"--unset", "--unset-all", "--add", "--set"}):
            return GitOpType.MUTATING
        # `git config <key> <value>` sets a value; `<key>` alone reads it.
        positional = [t for t in rest if not t.startswith("-")]
        if len(positional) >= 2:
            return GitOpType.MUTATING
        return GitOpType.READ_ONLY
    if sub == "stash":
        if rest and rest[0] in {"drop", "clear"}:
            return GitOpType.DESTRUCTIVE
        if rest and rest[0] == "list":
            return GitOpType.READ_ONLY
        return GitOpType.MUTATING
    if sub == "checkout":
        # `--force` discards local edits wherever it appears, not just first.
        if rest and (rest[0] in {".", "--"} or _has_flag(rest, {"-f", "--force"})):
            return GitOpType.DESTRUCTIVE
        if rest and rest[0] in {"-b", "-B", "--orphan"}:
            return GitOpType.MUTATING
        # `git checkout <path>` restores a path from the index, overwriting
        # local edits to it — treat any positional target as risky.
        if rest and not rest[0].startswith("-"):
            return GitOpType.MUTATING
        return GitOpType.MUTATING
    if sub == "switch":
        if rest and (rest[0] in {"-f", "--force"} or rest[0].startswith("--detach")):
            return (
                GitOpType.MUTATING
                if rest[0].startswith("--detach")
                else GitOpType.DESTRUCTIVE
            )
        if rest and rest[0] == "-c":
            return GitOpType.MUTATING
        return GitOpType.MUTATING
    if sub == "restore":
        if rest and rest[0] in {"--staged", "--stash"}:
            return GitOpType.MUTATING
        return GitOpType.DESTRUCTIVE
    if sub == "reset":
        if _has_flag(rest, {"--hard"}):
            return GitOpType.DESTRUCTIVE
        return GitOpType.MUTATING
    if sub == "clean":
        if _has_flag(rest, {"-n", "--dry-run"}):
            return GitOpType.READ_ONLY
        return GitOpType.DESTRUCTIVE
    if sub == "push":
        if any(_is_force_push(token) or _is_push_delete(token) for token in rest):
            return GitOpType.DESTRUCTIVE
        return GitOpType.MUTATING
    if sub == "filter-branch":
        return GitOpType.DESTRUCTIVE
    if sub == "reflog":
        if rest and rest[0] in {"expire", "delete"}:
            return GitOpType.DESTRUCTIVE
        return GitOpType.READ_ONLY
    if sub == "submodule":
        if _has_flag(rest, {"-f", "--force"}) or "foreach" in rest:
            return GitOpType.DESTRUCTIVE
        return GitOpType.MUTATING
    if sub in {"merge", "rebase", "cherry-pick", "revert"}:
        if rest and rest[0] in {"--abort", "--quit"}:
            return GitOpType.DESTRUCTIVE
        return GitOpType.MUTATING
    if sub in {"gc", "prune", "repack", "archive", "bundle", "clone", "init"}:
        return GitOpType.MUTATING
    if sub == "worktree":
        if rest and rest[0] in {"add", "remove", "prune", "move", "lock", "unlock"}:
            return GitOpType.MUTATING
        return GitOpType.READ_ONLY
    # Anything else with a recognized git subcommand: conservative default.
    return GitOpType.MUTATING


def _has_flag(tokens: list[str], flags: set[str]) -> bool:
    """True when any token supplies one of `flags`.

    Long (`--`) flags match exactly or as `--flag=value`. Short flags match
    membership in a single-dash cluster, so `-D`, `-Df` and `-rD` are all
    detected -- prefix matching alone missed the trailing forms. Matching is
    case-sensitive because `-d` and `-D` mean different things to git.
    """
    for token in tokens:
        if not token.startswith("-"):
            continue
        is_long = token.startswith("--")
        for flag in flags:
            if token == flag:
                return True
            if flag.startswith("--"):
                if is_long and token.startswith(f"{flag}="):
                    return True
            elif not is_long and len(flag) == 2 and flag[1] in token[1:]:
                return True
    return False


def _is_force_push(token: str) -> bool:
    """True when a push argument forces the update.

    Covers the long forms, single-dash clusters (`-f`, but also `-uf` / `-fq`,
    which git accepts and which used to slip past every approval layer), and
    the `+refspec` syntax that forces an update without any flag at all.
    """
    if token.startswith("--"):
        return token.startswith("--force")
    if token.startswith("-"):
        return "f" in token[1:]
    return token.startswith("+")


def _is_push_delete(token: str) -> bool:
    """True when a push argument deletes a remote ref.

    `git push --delete origin foo` and `git push origin :foo` both remove a
    branch for everyone, which is as destructive as a force-push.
    """
    if token.startswith("--"):
        return token == "--delete"
    if token.startswith("-"):
        return "d" in token[1:]
    return token.startswith(":")
