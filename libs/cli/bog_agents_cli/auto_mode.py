"""Auto-mode: smart approval engine between paranoid and permissive.

Priority: always_ask > auto_mode > auto_approve > default (ask all).

Settings cascade: built-in defaults → ~/.bog-agents/settings.json [auto_mode]
                  → <project>/.bog-agents/settings.json [auto_mode]
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Decision types
# ---------------------------------------------------------------------------

class AutoDecision(Enum):
    ALLOW = "allow"
    ASK = "ask"


@dataclass(frozen=True)
class RuleVerdict:
    decision: AutoDecision
    reason: str
    rule_source: str  # "safe_tools", "risky_tools", "allow_list", "ask_list", "haiku", "default"


# ---------------------------------------------------------------------------
# Built-in default patterns
# ---------------------------------------------------------------------------

# Shell commands that always trigger ASK
_DEFAULT_SHELL_ASK_PATTERNS: tuple[str, ...] = (
    # Deletions
    r"\brm\s", r"\brm$", r"\brmdir\b", r"\bdel\b", r"\brd\b",
    # Git destructive
    r"git\s+push\s+.*--force", r"git\s+push\s+-f\b",
    r"git\s+reset\s+--hard", r"git\s+clean\s+",
    r"git\s+checkout\s+\.", r"git\s+rebase\b",
    # Database
    r"\bDROP\s+TABLE\b", r"\bTRUNCATE\b", r"\bdropdb\b",
    # Network writes
    r"\bcurl\b.+(-X\s*(POST|PUT|DELETE|PATCH)|--data|-d\s)",
    r"\bwget\b.+--post",
    # Process kill
    r"\bkill\b", r"\bkillall\b", r"\bpkill\b",
    # Move with path separators (potentially destructive overwrite)
    r"\bmv\b.+[/\\]",
    # Overwrite redirects
    r">\s*[^>]",  # single > redirect (overwrite)
)

# Shell commands that are always safe to auto-approve
_DEFAULT_SHELL_ALLOW_PATTERNS: tuple[str, ...] = (
    # File reading
    r"^cat\b", r"^head\b", r"^tail\b", r"^grep\b", r"^rg\b",
    r"^find\b", r"^ls\b", r"^dir\b", r"^wc\b", r"^diff\b",
    r"^sed\b.+-n\b",  # sed read-only with -n
    # Git read-only
    r"^git\s+(status|log|diff|show|branch|tag|remote|ls-files|describe|shortlog|cat-file|for-each-ref)\b",
    # Test runs (not install)
    r"^npm\s+(test|run\s+test|run\s+typecheck|run\s+lint|run\s+check)\b",
    r"^(pytest|python\s+-m\s+pytest|uv\s+run\s+.*pytest)\b",
    r"^cargo\s+test\b", r"^go\s+test\b",
    r"^vitest\b", r"^jest\b",
    # Type checking / linting
    r"^tsc\b", r"^mypy\b", r"^ruff\s+(check|format\s+--check)\b",
    r"^ty\s+check\b", r"^pyright\b",
    # Path utilities
    r"^pwd\b", r"^which\b", r"^echo\b",
    r"^uname\b", r"^hostname\b",
)

# Tool names always safe to auto-approve
_SAFE_TOOL_NAMES: frozenset[str] = frozenset({
    "read_file", "read_many_files", "glob", "grep", "list_directory",
    "get_file_info", "search_files",
    "git_status", "git_log", "git_diff", "git_show",
})

# Tool names that always trigger ask
_RISKY_TOOL_NAMES: frozenset[str] = frozenset({
    "delete_file", "remove_directory",
    "git_push", "git_reset",
})

# Patterns that suggest an ambiguous prompt needing pre-flight Q&A
_AMBIGUITY_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\b(everything|all files?|all code|all of it)\b", "scope is very broad — which specific files or components?"),
    (r"\b(some|a few|various|certain)\b", "vague quantity — be specific about what exactly"),
    (r"\b(fix it|fix this|fix that)\b", "unclear what 'it' refers to — which bug, file, or feature?"),
    (r"\b(make it better|improve|optimize|clean up)\b", "open-ended goal — what specific improvement is needed?"),
    (r"\b(refactor|restructure|reorganize)\b", "broad change — which files/modules and what target structure?"),
    (r"\b(then|after that|and also|and then|finally)\b", "multi-step task — please confirm the steps in order"),
    (r"\b(deploy|publish|release|push to prod)\b", "deployment action — confirm the target environment"),
    (r"\b(delete|remove|drop|wipe|clear)\b", "destructive action — confirm exactly what to delete"),
)


# ---------------------------------------------------------------------------
# Settings model
# ---------------------------------------------------------------------------

@dataclass
class HaikuEvalConfig:
    """Configuration for the Haiku risk evaluator."""
    enabled: bool = True
    model: str = "claude-haiku-4-5-20251001"
    for_shell_commands: bool = True
    for_destructive_ops: bool = True

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> HaikuEvalConfig:
        return cls(
            enabled=bool(d.get("enabled", True)),
            model=str(d.get("model", "claude-haiku-4-5-20251001")),
            for_shell_commands=bool(d.get("for_shell_commands", True)),
            for_destructive_ops=bool(d.get("for_destructive_ops", True)),
        )


@dataclass
class AutoModeSettings:
    """Full auto-mode configuration (merged from cascade)."""
    enabled: bool = False
    # Extra patterns (merged with defaults, not replacing them)
    extra_shell_ask_patterns: list[str] = field(default_factory=list)
    extra_shell_allow_patterns: list[str] = field(default_factory=list)
    extra_safe_tools: list[str] = field(default_factory=list)
    extra_risky_tools: list[str] = field(default_factory=list)
    haiku_eval: HaikuEvalConfig = field(default_factory=HaikuEvalConfig)
    preflight_clarification: bool = True

    def merge_dict(self, d: dict[str, Any]) -> AutoModeSettings:
        """Return new settings with this dict's values overlaid.

        Args:
            d: Dict of overrides (from a settings.json ``auto_mode`` section).

        Returns:
            New AutoModeSettings with the overlaid values.
        """
        haiku_raw = d.get("haiku_eval", {})
        return AutoModeSettings(
            enabled=bool(d.get("enabled", self.enabled)),
            extra_shell_ask_patterns=list(d.get("shell_ask_patterns", self.extra_shell_ask_patterns)),
            extra_shell_allow_patterns=list(d.get("shell_allow_patterns", self.extra_shell_allow_patterns)),
            extra_safe_tools=list(d.get("safe_tools", self.extra_safe_tools)),
            extra_risky_tools=list(d.get("risky_tools", self.extra_risky_tools)),
            haiku_eval=HaikuEvalConfig.from_dict(haiku_raw) if haiku_raw else self.haiku_eval,
            preflight_clarification=bool(d.get("preflight_clarification", self.preflight_clarification)),
        )


def load_auto_mode_settings(project_root: Path | None = None) -> AutoModeSettings:
    """Load auto-mode settings from the cascade.

    Cascade order (later overrides earlier):
    1. Built-in defaults
    2. ``~/.bog-agents/settings.json`` [auto_mode section]
    3. ``<project>/.bog-agents/settings.json`` [auto_mode section]

    Args:
        project_root: Project root directory. If None, only user-global
            settings are loaded.

    Returns:
        Merged AutoModeSettings.
    """
    settings = AutoModeSettings()
    settings = _apply_settings_file(settings, Path.home() / ".bog-agents" / "settings.json")
    if project_root is not None:
        settings = _apply_settings_file(settings, project_root / ".bog-agents" / "settings.json")
    return settings


def _apply_settings_file(base: AutoModeSettings, path: Path) -> AutoModeSettings:
    if not path.is_file():
        return base
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        section = data.get("auto_mode", {})
        if isinstance(section, dict) and section:
            return base.merge_dict(section)
    except (OSError, json.JSONDecodeError, TypeError, KeyError):
        logger.debug("Failed to parse auto-mode settings from %s", path, exc_info=True)
    return base


# ---------------------------------------------------------------------------
# Rule engine
# ---------------------------------------------------------------------------

class AutoModeRuleEngine:
    """Evaluate tool calls against configured rules to decide allow vs ask."""

    def __init__(self, settings: AutoModeSettings) -> None:
        self._settings = settings
        all_ask = list(_DEFAULT_SHELL_ASK_PATTERNS) + settings.extra_shell_ask_patterns
        all_allow = list(_DEFAULT_SHELL_ALLOW_PATTERNS) + settings.extra_shell_allow_patterns
        self._ask_re = [re.compile(p, re.IGNORECASE) for p in all_ask]
        self._allow_re = [re.compile(p, re.IGNORECASE) for p in all_allow]
        self._safe_tools = _SAFE_TOOL_NAMES | frozenset(settings.extra_safe_tools)
        self._risky_tools = _RISKY_TOOL_NAMES | frozenset(settings.extra_risky_tools)

    def evaluate(self, tool_name: str, tool_args: dict[str, Any]) -> RuleVerdict:
        """Return a verdict for a single tool call.

        Args:
            tool_name: Name of the tool being called.
            tool_args: Arguments dict for the tool.

        Returns:
            RuleVerdict with decision, reason, and rule_source.
        """
        if tool_name in self._safe_tools:
            return RuleVerdict(AutoDecision.ALLOW, f"safe tool: {tool_name}", "safe_tools")

        if tool_name in self._risky_tools:
            return RuleVerdict(AutoDecision.ASK, f"risky tool: {tool_name}", "risky_tools")

        if tool_name in ("execute", "run_command", "shell", "bash"):
            cmd = str(tool_args.get("command", tool_args.get("cmd", "")))
            return self._eval_shell(cmd)

        # File writes — allow by default (non-destructive; Haiku handles edge cases)
        if tool_name in ("write_file", "edit_file", "create_file", "multi_edit_file"):
            return RuleVerdict(AutoDecision.ALLOW, "file write/edit", "default")

        # Unknown tool — allow (Haiku escalation handled by caller)
        return RuleVerdict(AutoDecision.ALLOW, "no matching rule", "default")

    def _eval_shell(self, cmd: str) -> RuleVerdict:
        # Allow-list first (fast path)
        for rx in self._allow_re:
            if rx.search(cmd):
                return RuleVerdict(AutoDecision.ALLOW, f"allow: {rx.pattern[:50]}", "allow_list")
        # Ask-list
        for rx in self._ask_re:
            if rx.search(cmd):
                return RuleVerdict(AutoDecision.ASK, f"ask: {rx.pattern[:50]}", "ask_list")
        # Falls through to Haiku (caller decides)
        return RuleVerdict(AutoDecision.ALLOW, "no shell pattern matched — may escalate to haiku", "default")


# ---------------------------------------------------------------------------
# Haiku risk evaluator
# ---------------------------------------------------------------------------

async def haiku_risk_eval(
    tool_name: str,
    tool_args: dict[str, Any],
    *,
    model: str = "claude-haiku-4-5-20251001",
) -> tuple[bool, str]:
    """Ask Haiku whether a tool call is risky.

    Only called when the rule engine's verdict is ``default`` (no pattern
    matched). Returns (is_risky, reason).

    Args:
        tool_name: Name of the tool.
        tool_args: Tool arguments.
        model: Model to use for evaluation.

    Returns:
        Tuple of (is_risky, reason_string).
    """
    try:
        import anthropic  # type: ignore[import-untyped]
    except ImportError:
        return False, "anthropic not available"

    cmd_repr = _format_tool_repr(tool_name, tool_args)
    prompt = (
        "You are a security evaluator for an AI coding assistant. "
        "Respond with a JSON object ONLY — no prose.\n\n"
        f"Tool call: {cmd_repr}\n\n"
        'Is this operation risky? Consider risky: deleting files, force-pushing git, '
        'dropping databases, killing processes, overwriting production data, '
        'destructive system changes. Consider safe: reading files, running tests, '
        'type-checking, git status/log/diff, creating new files.\n\n'
        '{"risky": true/false, "reason": "one sentence"}'
    )
    try:
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model=model,
            max_tokens=80,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        m = re.search(r"\{[^}]+\}", text, re.DOTALL)
        if m:
            data = json.loads(m.group(0))
            return bool(data.get("risky", False)), str(data.get("reason", "haiku eval"))
    except Exception as exc:
        logger.debug("haiku_risk_eval error: %s", exc)
    return False, "haiku eval: inconclusive"


def _format_tool_repr(tool_name: str, tool_args: dict[str, Any]) -> str:
    if tool_name in ("execute", "run_command", "shell", "bash"):
        return f"shell: {tool_args.get('command', tool_args.get('cmd', ''))}"
    if "path" in tool_args or "file_path" in tool_args:
        path = tool_args.get("path") or tool_args.get("file_path", "")
        return f"{tool_name}({path})"
    return f"{tool_name}({json.dumps(tool_args)[:120]})"


# ---------------------------------------------------------------------------
# Pre-flight ambiguity detection
# ---------------------------------------------------------------------------

def detect_ambiguities(prompt: str) -> list[str]:
    """Return clarifying questions for an ambiguous prompt (heuristic, no API).

    Args:
        prompt: The user's prompt.

    Returns:
        List of clarifying question strings.
    """
    questions: list[str] = []
    seen: set[str] = set()
    for pattern, question in _AMBIGUITY_PATTERNS:
        if re.search(pattern, prompt, re.IGNORECASE) and question not in seen:
            questions.append(question)
            seen.add(question)
    return questions


async def haiku_preflight_check(
    prompt: str,
    *,
    model: str = "claude-haiku-4-5-20251001",
) -> list[str]:
    """Use Haiku to generate clarifying questions for an ambiguous prompt.

    Args:
        prompt: The user's prompt to check.
        model: Model to use.

    Returns:
        List of clarifying questions (empty list if prompt is clear).
    """
    try:
        import anthropic  # type: ignore[import-untyped]
    except ImportError:
        return []

    system = (
        "You are a pre-flight assistant for an AI coding agent. "
        "Identify missing specifics that would prevent completing the task correctly. "
        "Only flag genuinely unclear or risky aspects."
    )
    user_msg = (
        f"User request: {prompt}\n\n"
        "If clear and specific: {\"questions\": []}\n"
        "If unclear, up to 3 questions: {\"questions\": [\"q1\", \"q2\"]}\n"
        "JSON only."
    )
    try:
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model=model,
            max_tokens=200,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = msg.content[0].text.strip()
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            data = json.loads(m.group(0))
            qs = data.get("questions", [])
            if isinstance(qs, list):
                return [str(q) for q in qs if q]
    except Exception as exc:
        logger.debug("haiku_preflight_check error: %s", exc)
    return []
