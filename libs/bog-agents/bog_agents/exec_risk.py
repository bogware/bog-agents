"""Static exec-risk analysis for shell commands (Tier-1 #2).

Some commands *look* read-only — `git log`, `sort`, `tar -t` — yet can execute
attacker-controlled code through a flag or a repo-config value. Grok Build calls
these an "exec-risk floor": even an otherwise auto-approvable command is forced
to prompt when it carries one of these vectors. This module is the pure analyzer
behind that behaviour for bog; `SafeToolsMiddleware` uses it to *veto*
auto-approval so the call falls through to human-in-the-loop.

It is a heuristic escalation (it fails toward prompting, never toward silent
execution), so imperfect shell parsing is acceptable: the command is split on
the usual operators, leading wrappers/env-prefixes are peeled, and each segment
is matched against known stealth-execution vectors.

Detected vectors (the canonical ones):
  * `git -c <exec-key>=…` / `--config-env` / `-c alias.x=!…` / `--upload-pack` /
    `--receive-pack` / `--git-dir=` / `--work-tree=` — config-driven hook/command
    execution or retargeting git at an attacker-controlled repo.
  * `sort --compress-program=PROG` (incl. abbreviations) — spawns PROG per file.
  * `tar --to-command=` / `--use-compress-program=` — runs a program per member.
  * `rsync -e` / `--rsh=` and `ssh -o ProxyCommand=` / `LocalCommand=` — run a
    shell/command under a copy/connect that looks benign.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Leading tokens that don't change what ultimately runs — peel them so
# `env git -c … log` and `timeout 5 sort --compress-program=…` still match.
_WRAPPERS = frozenset({"timeout", "nice", "ionice", "chrt", "stdbuf", "env", "nohup", "setsid", "time"})
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# git config keys whose value is executed (or which retarget git at another repo
# whose config then executes). Matched case-insensitively against `-c key=`.
_GIT_EXEC_KEY = re.compile(
    r"""-c\s+(
        core\.(fsmonitor|sshcommand|pager|editor|hookspath) |
        (diff|merge)\.[^=\s]*\.(command|textconv|external|driver) |
        filter\.[^=\s]*\.(clean|smudge|process) |
        alias\.[^=\s]*\s*=\s*['"]?! |
        uploadpack\.packobjectshook |
        sequence\.editor
    )""",
    re.IGNORECASE | re.VERBOSE,
)
_GIT_OTHER = re.compile(
    r"""(--config-env\b|--upload-pack\b|--receive-pack\b|--git-dir[=\s]|--work-tree[=\s]|-c\s+alias\.[^=\s]*\s*=\s*['"]?!)""",
    re.IGNORECASE,
)
# `--compress-program` and any unambiguous long-option abbreviation (`--co…`),
# since sort has no other `--co*` option.
_SORT_COMPRESS = re.compile(r"\bsort\b[^|;&]*\s--co(m(p(r(e(s(s(-p(r(o(g(r(a(m)?)?)?)?)?)?)?)?)?)?)?)?)?[=\s]", re.IGNORECASE)
_TAR_EXEC = re.compile(r"\btar\b[^|;&]*\s(--to-command[=\s]|--use-compress-program[=\s]|-I[=\s])", re.IGNORECASE)
_RSYNC_EXEC = re.compile(r"\brsync\b[^|;&]*\s(-e[=\s]|--rsh[=\s])", re.IGNORECASE)
_SSH_EXEC = re.compile(r"\bssh\b[^|;&]*\s-o\s*[\"']?(proxycommand|localcommand)\s*=", re.IGNORECASE)

_SEGMENT_SPLIT = re.compile(r"&&|\|\||;|\||\n")


@dataclass(frozen=True)
class ExecRisk:
    """One stealth code-execution vector found in a command.

    Attributes:
        vector: Short stable identifier (e.g. ``"git-config-exec"``).
        description: Human-readable explanation for an approval prompt.
    """

    vector: str
    description: str


def _peel(segment: str) -> str:
    """Strip leading env-var assignments and wrapper commands from a segment.

    Handles wrapper arguments too, so `timeout 5 git …`, `nice -n 10 sort …`
    and `env FOO=bar tar …` all reduce to the real command.
    """
    tokens = segment.split()
    i, n = 0, len(tokens)
    while i < n:
        tok = tokens[i]
        if _ENV_ASSIGN.match(tok):
            i += 1
            continue
        if tok in _WRAPPERS:
            i += 1
            # Consume this wrapper's own arguments (numbers and -flags) so the
            # wrapped command becomes the first token.
            while i < n and (tokens[i].startswith("-") or tokens[i].lstrip("-").isdigit()):
                i += 1
            continue
        break
    return " ".join(tokens[i:])


def analyze_exec_risk(command: str) -> list[ExecRisk]:
    """Find stealth code-execution vectors in a (possibly compound) command.

    Args:
        command: The shell command string to analyze.

    Returns:
        A list of `ExecRisk` findings (empty when none) — deduplicated by vector.
        A non-empty result means "do not auto-approve; prompt a human."
    """
    if not command or not isinstance(command, str):
        return []
    found: dict[str, ExecRisk] = {}
    for raw_segment in _SEGMENT_SPLIT.split(command):
        segment = _peel(raw_segment.strip())
        if not segment:
            continue
        _match_segment(segment, found)
    return list(found.values())


def _match_segment(segment: str, found: dict[str, ExecRisk]) -> None:
    """Match a single peeled segment against every vector, recording findings."""
    is_git = re.match(r"git\b", segment) is not None
    if is_git and (_GIT_EXEC_KEY.search(segment) or _GIT_OTHER.search(segment)):
        found["git-config-exec"] = ExecRisk(
            "git-config-exec",
            "git invoked with a config/flag that can execute a program "
            "(e.g. -c core.fsmonitor=…, alias.x=!…, --upload-pack, --git-dir/--work-tree retargeting).",
        )
    if _SORT_COMPRESS.search(segment):
        found["sort-compress-program"] = ExecRisk(
            "sort-compress-program",
            "sort --compress-program runs an arbitrary program for each temp file.",
        )
    if _TAR_EXEC.search(segment):
        found["tar-command-exec"] = ExecRisk(
            "tar-command-exec",
            "tar --to-command/--use-compress-program/-I runs an arbitrary program.",
        )
    if _RSYNC_EXEC.search(segment):
        found["rsync-remote-shell"] = ExecRisk(
            "rsync-remote-shell",
            "rsync -e/--rsh runs an arbitrary command as the remote shell.",
        )
    if _SSH_EXEC.search(segment):
        found["ssh-proxy-command"] = ExecRisk(
            "ssh-proxy-command",
            "ssh ProxyCommand/LocalCommand runs an arbitrary command on connect.",
        )


def command_has_exec_risk(command: str) -> bool:
    """Convenience predicate: True when `command` carries any exec-risk vector."""
    return bool(analyze_exec_risk(command))


__all__ = ["ExecRisk", "analyze_exec_risk", "command_has_exec_risk"]
