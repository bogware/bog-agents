"""Bash-hygiene analyzer — flag shell commands likely to hang or block.

A pure static heuristic (no shell execution) that answers *"will this command
block the agent?"*. Findings surface as an approval ask rather than a silent
auto-approval, with a concrete mitigation hint.

Deliberately NOT wired into the SDK `LocalShellBackend.execute()`: the
backend's timeout tests (`sleep 30` / `sleep 5` with timeout=1 → exit 124)
prove the timeout mechanism works, and a blocking-sleep guard there would
break them. The guard lives at the CLI/auto-mode policy layer instead.

Commands already wrapped in a `timeout`/`gtimeout` wrapper are treated as
bounded and skipped, since the user has already constrained the runtime.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

__all__ = ["BashHygieneFinding", "HygieneSeverity", "analyze_bash_hygiene"]


class HygieneSeverity(Enum):
    """How much a finding disrupts the agent."""

    INFO = "info"
    WARN = "warn"
    HIGH = "high"


@dataclass(frozen=True)
class BashHygieneFinding:
    """One hang/block risk found in a shell command.

    Attributes:
        severity: Disruption level of the risk.
        message: Human-readable description with a mitigation hint.
        bounded_by_timeout: Whether a `timeout` wrapper suppresses the finding.
    """

    severity: HygieneSeverity
    message: str
    bounded_by_timeout: bool = True


_LONG_SLEEP_SECONDS = 30.0
_TIMEOUT_WRAPPER = re.compile(r"\b(?:g?timeout)\b")


def analyze_bash_hygiene(command: str) -> list[BashHygieneFinding]:
    """Analyze a shell command for hang/block risks.

    Args:
        command: The raw shell command string.

    Returns:
        A list of `BashHygieneFinding`, empty when the command is clean or
        already bounded by a `timeout` wrapper.
    """
    findings: list[BashHygieneFinding] = []
    if not command.strip():
        return findings
    bounded = _TIMEOUT_WRAPPER.search(command) is not None
    for rule in _RULES:
        finding = rule(command)
        if finding is None:
            continue
        if bounded and finding.bounded_by_timeout:
            continue
        findings.append(finding)
    return findings


# ---------------------------------------------------------------------------
# Rules — each returns a finding or None.
# ---------------------------------------------------------------------------


def _sleep_rule(command: str) -> BashHygieneFinding | None:
    m = re.search(r"\bsleep\s+(\d+(?:\.\d+)?)", command)
    if m is None:
        return None
    seconds = float(m.group(1))
    if seconds < _LONG_SLEEP_SECONDS:
        return None
    return BashHygieneFinding(
        HygieneSeverity.WARN,
        f"`sleep {seconds:g}s` blocks the agent for a long time",
    )


def _infinite_loop_rule(command: str) -> BashHygieneFinding | None:
    if not re.search(
        r"\bwhile\s+(?:true|1)\b|\bwhile\s*:|\buntil\s+false\b|for\s*\(+[^)]*;",
        command,
    ):
        return None
    return BashHygieneFinding(
        HygieneSeverity.WARN, "infinite loop — will never exit on its own"
    )


def _yes_rule(command: str) -> BashHygieneFinding | None:
    if not re.search(r"\byes\b", command):
        return None
    if re.search(r"\|\s*head\b", command):
        return None
    return BashHygieneFinding(
        HygieneSeverity.WARN, "`yes` streams forever unless piped to a bounded consumer"
    )


def _tail_follow_rule(command: str) -> BashHygieneFinding | None:
    if not re.search(r"\btail\s+(-[a-zA-Z]*f|--follow)\b", command):
        return None
    return BashHygieneFinding(HygieneSeverity.WARN, "`tail -f` never exits")


def _ping_rule(command: str) -> BashHygieneFinding | None:
    if not re.search(r"\bping\b", command):
        return None
    if re.search(r"-c\s+\d+\b", command) or re.search(r"-n\s+\d+\b", command):
        return None
    return BashHygieneFinding(
        HygieneSeverity.WARN, "`ping` runs forever without a count (-c N)"
    )


def _monitoring_rule(command: str) -> BashHygieneFinding | None:
    if not re.search(r"\b(watch|top|htop)\b", command):
        return None
    return BashHygieneFinding(
        HygieneSeverity.WARN, "`watch`/`top`/`htop` loop indefinitely"
    )


def _interactive_rule(command: str) -> BashHygieneFinding | None:
    if not re.search(r"\b(less|more|vim|vi|nano|emacs|man)\b", command):
        return None
    return BashHygieneFinding(
        HygieneSeverity.HIGH,
        "interactive tool (less/vim/...) blocks waiting for a TTY",
    )


def _read_rule(command: str) -> BashHygieneFinding | None:
    if not re.search(r"\bread\b", command):
        return None
    if re.search(r"\bread\b[^\n]*-t\b", command):
        return None
    return BashHygieneFinding(
        HygieneSeverity.HIGH, "`read` without -t blocks forever on stdin"
    )


def _curl_rule(command: str) -> BashHygieneFinding | None:
    if not re.search(r"\bcurl\b", command):
        return None
    if re.search(r"--max-time|--connect-timeout|-m\b", command):
        return None
    return BashHygieneFinding(
        HygieneSeverity.WARN,
        "`curl` without --max-time can hang on a stalled connection",
    )


def _wget_rule(command: str) -> BashHygieneFinding | None:
    if not re.search(r"\bwget\b", command):
        return None
    if re.search(r"--timeout|--tries\s+\d+|-T\s+\d+", command):
        return None
    return BashHygieneFinding(
        HygieneSeverity.WARN,
        "`wget` without --timeout can hang on a stalled connection",
    )


def _ssh_rule(command: str) -> BashHygieneFinding | None:
    if not re.search(r"\b(?:ssh|scp|sftp)\b", command):
        return None
    if re.search(r"ConnectTimeout|--timeout\b", command):
        return None
    return BashHygieneFinding(
        HygieneSeverity.WARN,
        "`ssh`/`scp`/`sftp` without -o ConnectTimeout can hang on an unreachable host",
    )


def _git_editor_rule(command: str) -> BashHygieneFinding | None:
    if not re.search(r"\bgit\s+(commit|merge|revert)\b", command):
        return None
    if re.search(
        r"-[a-zA-Z]*m\b|--message\b|--no-edit\b|--amend\b|-F\b|--abort\b|"
        r"--no-commit\b|-n\b|--continue\b|--ff-only\b|--squash\b",
        command,
    ):
        return None
    return BashHygieneFinding(
        HygieneSeverity.WARN,
        "git may open an interactive editor — add -m or --no-edit",
    )


_RULES: tuple[Callable[[str], BashHygieneFinding | None], ...] = (
    _sleep_rule,
    _infinite_loop_rule,
    _yes_rule,
    _tail_follow_rule,
    _ping_rule,
    _monitoring_rule,
    _interactive_rule,
    _read_rule,
    _curl_rule,
    _wget_rule,
    _ssh_rule,
    _git_editor_rule,
)
