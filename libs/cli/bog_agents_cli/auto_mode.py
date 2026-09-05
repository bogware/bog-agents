"""Auto-mode: smart approval engine between paranoid and permissive.

Priority: always_ask > auto_mode > auto_approve > default (ask all).

Settings cascade: built-in defaults → ~/.bog-agents/settings.json [auto_mode]
                  → <project>/.bog-agents/settings.json [auto_mode]
"""

from __future__ import annotations

import json
import logging
import re
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from bog_agents.exec_risk import command_has_exec_risk
from bog_agents.middleware.expert_engine.types import Fact

from bog_agents_cli.bash_hygiene import analyze_bash_hygiene
from bog_agents_cli.git_ops import GitOpType, classify_git_command

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Decision types
# ---------------------------------------------------------------------------


class AutoDecision(Enum):
    """Whether a tool call should be auto-approved or shown to the user."""

    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"
    """ROADMAP #49: matched a persistent per-project never-allow entry; rejected without asking."""


@dataclass(frozen=True)
class RuleVerdict:
    """Result of evaluating a tool call against the rule engine."""

    decision: AutoDecision
    reason: str
    rule_source: (
        str  # "safe_tools", "risky_tools", "allow_list", "ask_list", "haiku", "default"
    )


# ---------------------------------------------------------------------------
# Built-in default patterns
# ---------------------------------------------------------------------------

# Shell commands that always trigger ASK
_DEFAULT_SHELL_ASK_PATTERNS: tuple[str, ...] = (
    # Deletions
    r"\brm\s",
    r"\brm$",
    r"\brmdir\b",
    r"\bdel\b",
    r"\brd\b",
    # Git destructive
    r"git\s+push\s+.*--force",
    r"git\s+push\s+-f\b",
    r"git\s+reset\s+--hard",
    r"git\s+clean\s+",
    r"git\s+checkout\s+\.",
    r"git\s+rebase\b",
    # Database
    r"\bDROP\s+TABLE\b",
    r"\bTRUNCATE\b",
    r"\bdropdb\b",
    # Network writes
    r"\bcurl\b.+(-X\s*(POST|PUT|DELETE|PATCH)|--data|-d\s)",
    r"\bwget\b.+--post",
    # Process kill
    r"\bkill\b",
    r"\bkillall\b",
    r"\bpkill\b",
    # Move with path separators (potentially destructive overwrite)
    r"\bmv\b.+[/\\]",
    # Overwrite redirects — single > but NOT >> (append)
    r"(?<![>])>(?!>)\s*\S",
    # Container engine cleanup
    r"\bdocker\s+(rm|rmi|prune|system\s+prune)\b",
    r"\bpodman\s+(rm|rmi|prune|system\s+prune)\b",
    # Raw disk writes
    r"\bdd\b.*\bof=",
    # Privileged destructive operations
    r"\bsudo\s+.*\b(rm|mv|dd|chmod\s+0|chown|mkfs|fdisk|parted)\b",
    # Cloud storage deletion
    r"\baws\s+s3\s+rm\b",
    r"\bgsutil\s+rm\b",
    r"\baz\s+storage\s+blob\s+delete\b",
    # Package removal (can break environments)
    r"\bpip\s+uninstall\b",
    r"\bnpm\s+uninstall\b",
    r"\buv\s+remove\b",
)

# Shell commands that are always safe to auto-approve
_DEFAULT_SHELL_ALLOW_PATTERNS: tuple[str, ...] = (
    # File reading
    r"^cat\b",
    r"^head\b",
    r"^tail\b",
    r"^grep\b",
    r"^rg\b",
    r"^find\b",
    r"^ls\b",
    r"^dir\b",
    r"^wc\b",
    r"^diff\b",
    r"^sed\b.+-n\b",  # sed read-only with -n
    # Git read-only
    r"^git\s+(status|log|diff|show|branch|tag|remote|ls-files|describe|shortlog|cat-file|for-each-ref)\b",
    # Test runs (not install)
    r"^npm\s+(test|run\s+test|run\s+typecheck|run\s+lint|run\s+check)\b",
    r"^(pytest|python\s+-m\s+pytest|uv\s+run\s+.*pytest)\b",
    r"^cargo\s+test\b",
    r"^go\s+test\b",
    r"^vitest\b",
    r"^jest\b",
    # Type checking / linting
    r"^tsc\b",
    r"^mypy\b",
    r"^ruff\s+(check|format\s+--check)\b",
    r"^ty\s+check\b",
    r"^pyright\b",
    # Path utilities
    r"^pwd\b",
    r"^which\b",
    r"^echo\b",
    r"^uname\b",
    r"^hostname\b",
)

# Tool names always safe to auto-approve
_SAFE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "read_file",
        "read_many_files",
        "glob",
        "grep",
        "list_directory",
        "get_file_info",
        "search_files",
        "git_status",
        "git_log",
        "git_diff",
        "git_show",
    }
)

# Tool names that always trigger ask
_RISKY_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "delete_file",
        "remove_directory",
        "git_push",
        "git_reset",
    }
)

# Patterns that suggest an ambiguous prompt needing pre-flight Q&A
_AMBIGUITY_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        r"\b(everything|all files?|all code|all of it)\b",
        "scope is very broad — which specific files or components?",
    ),
    (
        r"\b(some|a few|various|certain)\b",
        "vague quantity — be specific about what exactly",
    ),
    (
        r"\b(fix it|fix this|fix that)\b",
        "unclear what 'it' refers to — which bug, file, or feature?",
    ),
    (
        r"\b(make it better|improve|optimize|clean up)\b",
        "open-ended goal — what specific improvement is needed?",
    ),
    (
        r"\b(refactor|restructure|reorganize)\b",
        "broad change — which files/modules and what target structure?",
    ),
    (
        r"\b(then|after that|and also|and then|finally)\b",
        "multi-step task — please confirm the steps in order",
    ),
    (
        r"\b(deploy|publish|release|push to prod)\b",
        "deployment action — confirm the target environment",
    ),
    (
        r"\b(delete|remove|drop|wipe|clear)\b",
        "destructive action — confirm exactly what to delete",
    ),
)


# ---------------------------------------------------------------------------
# Settings model
# ---------------------------------------------------------------------------


RiskJudge = Callable[[str], Awaitable[str]]
"""An injected risk judge: takes the evaluator prompt, returns the model text (v6 CLI-9)."""


@dataclass
class HaikuEvalConfig:
    """Configuration for the Haiku risk evaluator."""

    enabled: bool = True
    model: str = "claude-haiku-4-5-20251001"
    fallback_model: str = "claude-haiku-4-5"
    for_shell_commands: bool = True
    for_destructive_ops: bool = True

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> HaikuEvalConfig:
        """Construct from a plain dict (e.g. from JSON settings)."""
        return cls(
            enabled=bool(d.get("enabled", True)),
            model=str(d.get("model", "claude-haiku-4-5-20251001")),
            fallback_model=str(d.get("fallback_model", "claude-haiku-4-5")),
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
    # ROADMAP #49: persistent per-project never-allow entries. Each entry is a
    # bare tool name (`"web_fetch"`), or `"<tool>: <regex>"` matched against
    # the shell command (execute/shell) or the JSON of the tool arguments.
    never_allow: list[str] = field(default_factory=list)
    # v6 #47: consecutive risky verdicts from the review model before auto
    # mode pauses itself and every call asks a human (Codex's circuit breaker).
    breaker_threshold: int = 3

    def merge_dict(self, d: dict[str, Any]) -> AutoModeSettings:
        """Return new settings with this dict's values overlaid.

        Args:
            d: Dict of overrides (from a settings.json ``auto_mode`` section).

        Returns:
            New AutoModeSettings with the overlaid values.
        """
        haiku_raw = d.get("haiku_eval", {})

        def _coerce_str_list(key: str, fallback: list[str]) -> list[str]:
            val = d.get(key, fallback)
            if not isinstance(val, list):
                logger.warning(
                    "auto_mode setting '%s' must be a list, got %s — ignoring",
                    key,
                    type(val).__name__,
                )
                return fallback
            return [str(item) for item in val]

        return AutoModeSettings(
            enabled=bool(d.get("enabled", self.enabled)),
            extra_shell_ask_patterns=_coerce_str_list(
                "shell_ask_patterns", self.extra_shell_ask_patterns
            ),
            extra_shell_allow_patterns=_coerce_str_list(
                "shell_allow_patterns", self.extra_shell_allow_patterns
            ),
            extra_safe_tools=_coerce_str_list("safe_tools", self.extra_safe_tools),
            extra_risky_tools=_coerce_str_list("risky_tools", self.extra_risky_tools),
            haiku_eval=HaikuEvalConfig.from_dict(haiku_raw)
            if haiku_raw
            else self.haiku_eval,
            preflight_clarification=bool(
                d.get("preflight_clarification", self.preflight_clarification)
            ),
            breaker_threshold=max(
                1, int(d.get("breaker_threshold", self.breaker_threshold))
            ),
            never_allow=_coerce_str_list("never_allow", self.never_allow),
        )


# ---------------------------------------------------------------------------
# ROADMAP #49: persistent never-allow
# ---------------------------------------------------------------------------


def compile_never_allow(
    entries: list[str],
) -> list[tuple[str, str | None, re.Pattern[str] | None]]:
    """Parse never-allow entries into `(raw, tool, pattern)`; a bare tool name has no pattern."""
    compiled: list[tuple[str, str | None, re.Pattern[str] | None]] = []
    for raw in entries:
        text = str(raw).strip()
        if not text:
            continue
        tool, sep, pattern = text.partition(":")
        if not sep:
            compiled.append((text, text.strip().lower(), None))
            continue
        try:
            rx = re.compile(pattern.strip(), re.IGNORECASE)
        except re.error:
            logger.warning("never_allow: invalid regex in %r; entry ignored", text)
            continue
        compiled.append((text, tool.strip().lower() or None, rx))
    return compiled


def never_allow_match(
    compiled: list[tuple[str, str | None, re.Pattern[str] | None]],
    tool_name: str,
    tool_args: dict[str, Any],
) -> str | None:
    """Return the matching never-allow entry for a tool call, or `None`."""
    lowered = tool_name.lower()
    shell_like = lowered in {"execute", "shell", "bash", "run_command"}
    haystack: str | None = None
    for raw, tool, rx in compiled:
        if tool not in (None, lowered) and not (
            tool in {"execute", "shell"} and shell_like
        ):
            continue
        if rx is None:
            if tool == lowered:
                return raw
            continue
        if haystack is None:
            command = tool_args.get("command") if isinstance(tool_args, dict) else None
            haystack = (
                str(command)
                if shell_like and command is not None
                else json.dumps(tool_args, sort_keys=True, default=str)
            )
        if rx.search(haystack):
            return raw
    return None


def never_allow_entry_for(tool_name: str, tool_args: dict[str, Any]) -> str:
    """Build the never-allow entry that would block exactly this call again."""
    lowered = tool_name.lower()
    if lowered in {"execute", "shell", "bash", "run_command"}:
        command = str((tool_args or {}).get("command") or "").strip()
        if command:
            return f"execute: ^{re.escape(command)}$"
    return lowered


def project_never_allow(project_root: Path | str | None) -> list[str]:
    """The effective never-allow list for a project (user + project settings.json)."""
    return list(
        load_auto_mode_settings(
            Path(project_root) if project_root else None
        ).never_allow
    )


def record_never_allow(project_root: Path | str, entry: str) -> Path:
    """Append `entry` to `<project>/.bog-agents/settings.json` `auto_mode.never_allow` (atomic, idempotent)."""
    from bog_agents_cli.io_utils import atomic_write_text

    path = Path(project_root) / ".bog-agents" / "settings.json"
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError):
            logger.warning(
                "never_allow: could not parse %s; rewriting the auto_mode section only",
                path,
            )
    section = data.get("auto_mode")
    if not isinstance(section, dict):
        section = {}
    current = (
        [str(e) for e in section.get("never_allow", []) if isinstance(e, str)]
        if isinstance(section.get("never_allow"), list)
        else []
    )
    if entry not in current:
        current.append(entry)
    section["never_allow"] = current
    data["auto_mode"] = section
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(data, indent=2))
    return path


def denied_indexes(
    action_requests: list[dict[str, Any]], project_root: Path | str | None
) -> dict[int, str]:
    """Map action-request indexes to the never-allow entry that denies them (any approval mode)."""
    compiled = compile_never_allow(project_never_allow(project_root))
    if not compiled:
        return {}
    out: dict[int, str] = {}
    for index, req in enumerate(action_requests):
        args = req.get("args", {}) if isinstance(req.get("args"), dict) else {}
        hit = never_allow_match(compiled, str(req.get("name", "")), args)
        if hit is not None:
            out[index] = hit
    return out


def approval_timeout_seconds() -> float | None:
    """`approvals.timeout_seconds` from the manifest (`None` = wait forever)."""
    try:
        from bog_agents_cli.config_manifest import resolve_option

        value = resolve_option("approvals.timeout_seconds")
    except Exception:  # a config problem never blocks approvals
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return float(value)
    return None


def load_auto_mode_settings(
    project_root: Path | None = None, *, user_home: Path | None = None
) -> AutoModeSettings:
    """Load auto-mode settings from the cascade.

    Cascade order (later overrides earlier):
    1. Built-in defaults
    2. ``~/.bog-agents/settings.json`` [auto_mode section]
    3. ``<project>/.bog-agents/settings.json`` [auto_mode section]

    Args:
        project_root: Project root directory. If None, only user-global
            settings are loaded.
        user_home: Override for the home directory (tests); production callers
            leave None to use `Path.home()`.

    Returns:
        Merged AutoModeSettings.
    """
    from bog_agents_cli._settings_cascade import load_layered_section

    return load_layered_section(
        section="auto_mode",
        initial=AutoModeSettings(),
        merge=lambda current, override: current.merge_dict(override),
        project_root=project_root,
        user_home=user_home,
    )


# Kept for back-compat with tests that monkey-patched the old internal
# helper. New code uses ``load_layered_section`` directly.
_SETTINGS_MAX_BYTES = 1 * 1024 * 1024  # 1 MB — guard against absurdly large files


def _apply_settings_file(base: AutoModeSettings, path: Path) -> AutoModeSettings:
    """Legacy single-file applier kept for test compatibility.

    Production callers go through :func:`load_auto_mode_settings`, which
    now delegates to ``_settings_cascade.load_layered_section``. This
    shim remains so the existing
    ``TestApplySettingsFile`` test class keeps exercising the same
    file-level behaviour (oversized cap, malformed JSON, missing
    section).
    """
    if not path.is_file():
        return base
    try:
        raw = path.read_bytes()
        if len(raw) > _SETTINGS_MAX_BYTES:
            logger.warning(
                "Auto-mode settings file %s is too large (%d bytes, max %d) — skipping",
                path,
                len(raw),
                _SETTINGS_MAX_BYTES,
            )
            return base
        data = json.loads(raw.decode("utf-8"))
        section = data.get("auto_mode", {})
        if isinstance(section, dict) and section:
            return base.merge_dict(section)
    except (OSError, json.JSONDecodeError, TypeError, KeyError) as exc:
        logger.warning("Failed to parse auto-mode settings from %s: %s", path, exc)
    return base


# ---------------------------------------------------------------------------
# Rule engine
# ---------------------------------------------------------------------------


class AutoModeRuleEngine:
    """Evaluate tool calls against configured rules to decide allow vs ask."""

    def __init__(self, settings: AutoModeSettings) -> None:
        self._settings = settings
        all_ask = list(_DEFAULT_SHELL_ASK_PATTERNS) + settings.extra_shell_ask_patterns
        all_allow = (
            list(_DEFAULT_SHELL_ALLOW_PATTERNS) + settings.extra_shell_allow_patterns
        )
        self._ask_re = [re.compile(p, re.IGNORECASE) for p in all_ask]
        self._allow_re = [re.compile(p, re.IGNORECASE) for p in all_allow]
        self._safe_tools = _SAFE_TOOL_NAMES | frozenset(settings.extra_safe_tools)
        self._risky_tools = _RISKY_TOOL_NAMES | frozenset(settings.extra_risky_tools)
        self._never_allow = compile_never_allow(settings.never_allow)

    def evaluate(self, tool_name: str, tool_args: dict[str, Any]) -> RuleVerdict:
        """Return a verdict for a single tool call.

        Args:
            tool_name: Name of the tool being called.
            tool_args: Arguments dict for the tool.

        Returns:
            RuleVerdict with decision, reason, and rule_source.
        """
        denied = never_allow_match(self._never_allow, tool_name, tool_args)
        if denied is not None:
            return RuleVerdict(
                AutoDecision.DENY, f"never allowed: {denied}", "never_allow"
            )

        if tool_name in self._safe_tools:
            return RuleVerdict(
                AutoDecision.ALLOW, f"safe tool: {tool_name}", "safe_tools"
            )

        if tool_name in self._risky_tools:
            return RuleVerdict(
                AutoDecision.ASK, f"risky tool: {tool_name}", "risky_tools"
            )

        if tool_name in ("execute", "run_command", "shell", "bash"):
            cmd = str(tool_args.get("command", tool_args.get("cmd", "")))
            return self._eval_shell(cmd)

        # File writes — allow by default (non-destructive; Haiku handles edge cases)
        if tool_name in ("write_file", "edit_file", "create_file", "multi_edit_file"):
            return RuleVerdict(AutoDecision.ALLOW, "file write/edit", "default")

        # Unknown tool — allow (Haiku escalation handled by caller)
        return RuleVerdict(AutoDecision.ALLOW, "no matching rule", "default")

    def _eval_shell(self, cmd: str) -> RuleVerdict:
        # Ask-list checked FIRST — destructive patterns take priority over allow patterns.
        # This ensures e.g. `echo foo > file.txt` is caught by the redirect ask-rule
        # even though `echo` is also in the allow-list.
        for rx in self._ask_re:
            if rx.search(cmd):
                return RuleVerdict(
                    AutoDecision.ASK, f"ask: {rx.pattern[:50]}", "ask_list"
                )
        # Consolidated git classifier closes gaps the ask-list misses (force-push
        # spelled `-ff`, `branch -D`, `stash drop`, ...). Must run before the
        # allow-list, whose broad patterns like `git\s+branch\b` would otherwise
        # swallow destructive subcommands.
        git_op = classify_git_command(cmd)
        if git_op is GitOpType.DESTRUCTIVE:
            return RuleVerdict(
                AutoDecision.ASK, "git: destructive git operation", "git_ops"
            )
        # Exec-risk veto: commands that look read-only but can run attacker code
        # (`git -c core.pager=…`, `sort --compress-program=…`, `tar --to-command=…`,
        # `ssh -o ProxyCommand=…`). This is the deterministic Tier-1 #2 floor —
        # it must run before the allow-list, whose broad patterns (`git\s+log`)
        # would otherwise auto-approve the stealth-exec form. Fails toward
        # prompting, never toward silent execution.
        if command_has_exec_risk(cmd):
            return RuleVerdict(
                AutoDecision.ASK,
                "exec risk: command can execute code via a helper option",
                "exec_risk",
            )
        # Allow-list (fast path for known-safe commands with no destructive pattern)
        for rx in self._allow_re:
            if rx.search(cmd):
                return RuleVerdict(
                    AutoDecision.ALLOW, f"allow: {rx.pattern[:50]}", "allow_list"
                )
        # Known read-only git ops not explicitly allow-listed are safe to approve.
        if git_op is GitOpType.READ_ONLY:
            return RuleVerdict(AutoDecision.ALLOW, "git read-only", "git_ops")
        # Bash-hygiene gate: flag hang-prone / blocking commands (long sleeps,
        # infinite loops, interactive tools, network calls without timeouts).
        # Explicitly allow-listed commands (e.g. `tail -f`) win over this gate.
        hygiene = analyze_bash_hygiene(cmd)
        if hygiene:
            message = "; ".join(finding.message for finding in hygiene[:2])
            return RuleVerdict(
                AutoDecision.ASK, f"bash hygiene: {message}", "bash_hygiene"
            )
        # Falls through to Haiku (caller decides)
        return RuleVerdict(
            AutoDecision.ALLOW,
            "no shell pattern matched — may escalate to haiku",
            "default",
        )


# ---------------------------------------------------------------------------
# Haiku risk evaluator
# ---------------------------------------------------------------------------


def _risk_prompt(tool_name: str, tool_args: dict[str, Any]) -> str:
    """Build the JSON-only risk prompt shared by every judge backend."""
    cmd_repr = _format_tool_repr(tool_name, tool_args)
    return (
        "You are a security evaluator for an AI coding assistant. "
        "Respond with a JSON object ONLY — no prose.\n\n"
        f"Tool call: {cmd_repr}\n\n"
        "Is this operation risky? Consider risky: deleting files, force-pushing git, "
        "dropping databases, killing processes, overwriting production data, "
        "destructive system changes. Consider safe: reading files, running tests, "
        "type-checking, git status/log/diff, creating new files.\n\n"
        '{"risky": true/false, "reason": "one sentence"}'
    )


def _parse_risk_verdict(text: str) -> tuple[bool, str]:
    """Parse a judge reply into `(is_risky, reason)`; anything unparseable is risky.

    Matches the outermost brace pair first (a reason may contain braces), then
    falls back to a minimal leading object when the model appended prose.
    """
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return True, "risk judge: inconclusive — treating as risky"
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        inner = re.search(r"\{[^{}]*\}", text)
        if inner is None:
            return True, "risk judge: malformed JSON — treating as risky"
        try:
            data = json.loads(inner.group(0))
        except json.JSONDecodeError:
            return True, "risk judge: malformed JSON — treating as risky"
    if not isinstance(data, dict):
        return True, "risk judge: malformed JSON — treating as risky"
    return bool(data.get("risky", False)), str(data.get("reason", "risk judge"))


def _message_text(response: Any) -> str:  # noqa: ANN401 — LangChain message or plain text
    """Return the text of a chat-model reply (string or content blocks)."""
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "".join(parts)
    return str(content)


def default_judge_spec(
    active_provider: str | None, active_model: str | None, configured: str | None = None
) -> str | None:
    """Pick the `provider:model` that reviews uncertain tool calls (v6 CLI-9).

    - An explicit `provider:model` in `haiku_eval.model` always wins.
    - Anthropic (or an unknown provider) returns `None`: keep the legacy
      Anthropic-SDK Haiku path.
    - OpenAI gets its cheap tier; every other provider (Ollama, Bedrock,
      Google, …) reviews with the active model itself — a real reviewer beats
      failing closed to a prompt on every unmatched command.

    Args:
        active_provider: The session's model provider.
        active_model: The session's model name.
        configured: `haiku_eval.model` from settings.

    Returns:
        A `provider:model` spec, or `None` for the legacy path.
    """
    if configured and ":" in configured:
        return configured
    provider = (active_provider or "").strip().lower()
    if not provider or provider == "anthropic":
        return None
    if provider == "openai":
        return "openai:gpt-5.4-mini"
    if active_model:
        return f"{provider}:{active_model}"
    return None


_JUDGE_CACHE: dict[str, RiskJudge] = {}


def _anthropic_sdk_judge(model: str, fallback_model: str) -> RiskJudge | None:
    """Wrap the legacy Anthropic-SDK Haiku call as a `RiskJudge` (None when the SDK is absent)."""
    try:
        import anthropic  # type: ignore[import-untyped]
    except ImportError:
        return None

    async def _judge(prompt: str) -> str:
        client = anthropic.AsyncAnthropic()
        models_to_try = [model] + (
            [fallback_model] if fallback_model and fallback_model != model else []
        )
        last_exc: Exception | None = None
        for attempt_model in models_to_try:
            try:
                msg = await client.messages.create(
                    model=attempt_model,
                    max_tokens=400,
                    messages=[{"role": "user", "content": prompt}],
                )
                return str(msg.content[0].text)
            except anthropic.NotFoundError as exc:
                last_exc = exc
                continue
        raise RuntimeError(
            f"no judge model found ({', '.join(models_to_try)})"
        ) from last_exc

    return _judge


def resolve_risk_judge(
    settings: AutoModeSettings | None = None,
) -> tuple[RiskJudge | None, str]:
    """Build (and cache) the provider-agnostic risk judge for this session.

    Args:
        settings: Auto-mode settings (for the configured judge model).

    Returns:
        `(judge, description)`. A `None` judge means the caller should use the
        legacy Anthropic-SDK path in `haiku_risk_eval`; the description names
        the model in use for status lines.
    """
    cfg = (settings or AutoModeSettings()).haiku_eval
    try:
        from bog_agents_cli.config import settings as cli_settings

        provider = getattr(cli_settings, "model_provider", None)
        model_name = getattr(cli_settings, "model_name", None)
    except Exception:
        provider = model_name = None
    spec = default_judge_spec(provider, model_name, cfg.model)
    if spec is None:
        # Anthropic (or unknown) provider: wrap the SDK path as a judge so the
        # batched review (#47) works for every provider through one interface.
        sdk_judge = _anthropic_sdk_judge(cfg.model, cfg.fallback_model)
        if sdk_judge is None:
            return None, "unavailable (anthropic package not installed)"
        return sdk_judge, f"Anthropic SDK ({cfg.model})"
    cached = _JUDGE_CACHE.get(spec)
    if cached is not None:
        return cached, spec
    try:
        from bog_agents_cli.config import create_model

        chat_model = create_model(spec).model
    except Exception as exc:
        logger.warning(
            "risk judge: could not build %s (%s); unmatched commands will ask",
            spec,
            exc,
        )
        return None, f"unavailable ({spec}: {exc.__class__.__name__})"

    async def _judge(prompt: str) -> str:
        return _message_text(await chat_model.ainvoke(prompt))

    _JUDGE_CACHE[spec] = _judge
    return _judge, spec


async def haiku_risk_eval(
    tool_name: str,
    tool_args: dict[str, Any],
    *,
    model: str = "claude-haiku-4-5-20251001",
    fallback_model: str = "claude-haiku-4-5",
    invoke: RiskJudge | None = None,
) -> tuple[bool, str]:
    """Ask a review model whether a tool call is risky.

    Only called when the rule engine's verdict is ``default`` (no pattern
    matched). Returns (is_risky, reason).

    Retries with ``fallback_model`` when the primary model is not found (e.g.
    after a version-dated snapshot is retired by the API).

    Args:
        tool_name: Name of the tool.
        tool_args: Tool arguments.
        model: Model to use for evaluation.
        fallback_model: Model to retry with if ``model`` returns a not-found
            error.
        invoke: Provider-agnostic judge built by `resolve_risk_judge` (v6
            CLI-9). When given, it replaces the Anthropic-SDK path entirely, so
            OpenAI / Ollama / Bedrock-only installs get a real reviewer instead
            of failing closed to a prompt on every unmatched command.

    Returns:
        Tuple of (is_risky, reason_string).
    """
    if not isinstance(tool_args, dict):
        tool_args = {}
    if invoke is not None:
        try:
            text = await invoke(_risk_prompt(tool_name, tool_args))
        except Exception as exc:
            logger.warning("risk judge error (treating as risky for safety): %s", exc)
            return (
                True,
                f"risk judge unavailable — treating as risky ({exc.__class__.__name__})",
            )
        return _parse_risk_verdict(str(text))
    try:
        import anthropic  # type: ignore[import-untyped]
    except ImportError:
        # Fail CLOSED for gating: this evaluator only runs on the `default`
        # (no-pattern-matched) verdict, i.e. the caller is about to auto-approve
        # unless we say risky. If the risk classifier cannot run at all, the
        # command must fall through to a human — never be silently approved on a
        # non-Anthropic install (T1-4). Contrast the API-error paths below, which
        # already fail closed by returning risky=True.
        return True, "haiku eval: anthropic package unavailable — treating as risky"

    prompt = _risk_prompt(tool_name, tool_args)
    models_to_try = [model]
    if fallback_model and fallback_model != model:
        models_to_try.append(fallback_model)
    client = anthropic.AsyncAnthropic()
    for attempt_model in models_to_try:
        try:
            msg = await client.messages.create(
                model=attempt_model,
                max_tokens=80,
                messages=[{"role": "user", "content": prompt}],
            )
            return _parse_risk_verdict(msg.content[0].text)
        except anthropic.NotFoundError:
            if attempt_model != models_to_try[-1]:
                logger.warning(
                    "haiku_risk_eval: model %r not found, retrying with fallback %r",
                    attempt_model,
                    models_to_try[-1],
                )
                continue
            logger.warning(
                "haiku_risk_eval: fallback model %r also not found — treating as risky",
                attempt_model,
            )
            return (
                True,
                f"haiku eval: model not found ({attempt_model}) — treating as risky",
            )
        except Exception as exc:
            logger.warning(
                "haiku_risk_eval error (treating as risky for safety): %s", exc
            )
            return (
                True,
                f"haiku eval: API unavailable — treating as risky ({exc.__class__.__name__})",
            )
    return True, "haiku eval: inconclusive — treating as risky"


def _format_tool_repr(tool_name: str, tool_args: dict[str, Any]) -> str:
    if tool_name in ("execute", "run_command", "shell", "bash"):
        return f"shell: {tool_args.get('command', tool_args.get('cmd', ''))}"
    if "path" in tool_args or "file_path" in tool_args:
        path = tool_args.get("path") or tool_args.get("file_path", "")
        return f"{tool_name}({path})"
    # ``default=str`` so a SecretStr (or any other non-JSON-serializable value
    # surfaced via VarBundle.substitute) renders as its redacted ``str()``
    # form (``"***"`` for secrets) instead of crashing the rule engine.
    return f"{tool_name}({json.dumps(tool_args, default=str)[:120]})"


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
    fallback_model: str = "claude-haiku-4-5",
) -> list[str]:
    """Use Haiku to generate clarifying questions for an ambiguous prompt.

    Retries with ``fallback_model`` when the primary model is not found (e.g.
    after a version-dated snapshot is retired by the API).

    Args:
        prompt: The user's prompt to check.
        model: Model to use.
        fallback_model: Model to retry with if ``model`` returns a not-found
            error.

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
        'If clear and specific: {"questions": []}\n'
        'If unclear, up to 3 questions: {"questions": ["q1", "q2"]}\n'
        "JSON only."
    )
    models_to_try = [model]
    if fallback_model and fallback_model != model:
        models_to_try.append(fallback_model)
    client = anthropic.AsyncAnthropic()
    for attempt_model in models_to_try:
        try:
            msg = await client.messages.create(
                model=attempt_model,
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
            return []
        except anthropic.NotFoundError:
            if attempt_model != models_to_try[-1]:
                logger.warning(
                    "haiku_preflight_check: model %r not found, retrying with fallback %r",
                    attempt_model,
                    models_to_try[-1],
                )
                continue
            logger.debug(
                "haiku_preflight_check: fallback model %r also not found", attempt_model
            )
        except Exception as exc:
            logger.debug("haiku_preflight_check error: %s", exc)
    return []


# ---------------------------------------------------------------------------
# Governed Auto Mode (ROADMAP #47): batched review, approval ledger, breaker
# ---------------------------------------------------------------------------

RISK_LEVELS: tuple[str, ...] = ("low", "medium", "high", "critical")
"""Risk ladder the batched review grades with; `high`/`critical` ask a human."""


@dataclass(frozen=True)
class RiskAssessment:
    """One entry of a batched review."""

    index: int
    risk: str
    reason: str

    @property
    def risky(self) -> bool:
        """True when the call must fall through to a human."""
        return self.risk in ("high", "critical")


def _batch_risk_prompt(items: list[tuple[int, str, dict[str, Any]]], goal: str) -> str:
    """Build the one-call review prompt for every pending tool call in a turn.

    The user's stated outcome is included so the reviewer grades calls in
    context ("delete the build dir" is expected when the goal is a clean
    rebuild, alarming when the goal is a typo fix).
    """
    lines = [
        "You are a security reviewer for an AI coding assistant. Grade EVERY pending tool call.",
        "Respond with a JSON object ONLY — no prose:",
        '{"assessments": [{"index": <int>, "risk": "low|medium|high|critical", "reason": "one sentence"}, ...]}',
        "",
        "Risk ladder: low = read-only or reversible in the working tree; medium = ordinary edits, tests, builds;",
        "high = deletes data, force-pushes, changes system/global state, touches secrets or CI/CD;",
        "critical = destructive or irreversible outside the working tree, or clearly unrelated to the goal.",
        "",
        f"User's stated goal: {goal.strip()[:600] or '(not stated)'}",
        "",
        "Pending tool calls:",
    ]
    lines.extend(
        f"  [{index}] {_format_tool_repr(name, args)}" for index, name, args in items
    )
    return "\n".join(lines)


def _parse_batch_assessments(text: str, indices: list[int]) -> list[RiskAssessment]:
    """Parse the reviewer reply; any index it failed to grade is treated as critical."""
    graded: dict[int, RiskAssessment] = {}
    m = re.search(r"\{.*\}", text.strip(), re.DOTALL)
    data: Any = None
    if m:
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            data = None
    entries = data.get("assessments") if isinstance(data, dict) else None
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            try:
                idx = int(entry.get("index"))
            except (TypeError, ValueError):
                continue
            risk = str(entry.get("risk", "")).strip().lower()
            if risk not in RISK_LEVELS:
                risk = "critical"
            graded[idx] = RiskAssessment(
                idx, risk, str(entry.get("reason", "")).strip() or "no reason given"
            )
    return [
        graded.get(
            i,
            RiskAssessment(
                i, "critical", "reviewer did not grade this call — treating as risky"
            ),
        )
        for i in indices
    ]


async def batch_risk_eval(
    items: list[tuple[int, str, dict[str, Any]]],
    *,
    goal: str,
    invoke: RiskJudge,
) -> list[RiskAssessment]:
    """Grade every pending tool call with ONE review-model call (v6 #47).

    Replaces the per-call Haiku round-trips: one structured verdict per turn,
    graded against the user's stated goal. Fails closed — a judge error or an
    unparseable reply grades every call `critical`.

    Args:
        items: `(index, tool_name, tool_args)` for each call the rule engine
            left undecided.
        goal: The user's latest prompt (their stated outcome).
        invoke: The session's risk judge.

    Returns:
        One `RiskAssessment` per item, in input order.
    """
    indices = [index for index, _name, _args in items]
    if not items:
        return []
    try:
        text = await invoke(_batch_risk_prompt(items, goal))
    except Exception as exc:
        logger.warning(
            "batched risk review failed (treating every call as risky): %s", exc
        )
        return [
            RiskAssessment(
                i, "critical", f"review model unavailable ({exc.__class__.__name__})"
            )
            for i in indices
        ]
    return _parse_batch_assessments(str(text), indices)


@dataclass(frozen=True)
class ApprovalDecision:
    """One auto-mode decision, kept so `/auto why` and `/why` can explain it."""

    tool: str
    call: str
    decision: str
    """`auto-approved`, `ask`, or `paused` (circuit breaker open)."""
    rule_source: str
    """`ask_list`, `git_ops`, `exec_risk`, `bash_hygiene`, `allow_list`, `review_model`, `breaker`, …"""
    reason: str
    risk: str = ""
    judge: str = ""

    def render(self) -> str:
        """One-line, human-readable form."""
        tail = f" [{self.risk}]" if self.risk else ""
        via = f" via {self.judge}" if self.judge else ""
        return f"{self.decision:<13} {self.call}  ← {self.rule_source}{tail}: {self.reason}{via}"


class ApprovalLedger:
    """Session ring buffer of auto-mode decisions (`/auto why [n]`)."""

    def __init__(self, maxlen: int = 200) -> None:
        self._entries: deque[ApprovalDecision] = deque(maxlen=maxlen)

    def record(self, decision: ApprovalDecision) -> None:
        """Append a decision."""
        self._entries.append(decision)

    def recent(self, n: int = 5) -> list[ApprovalDecision]:
        """Return the last `n` decisions, newest last."""
        return list(self._entries)[-max(1, n) :]

    def __len__(self) -> int:
        """Number of decisions kept."""
        return len(self._entries)

    def clear(self) -> None:
        """Forget everything (tests, `/clear`)."""
        self._entries.clear()

    def render(self, n: int = 5) -> str:
        """Render the last `n` decisions for the TUI."""
        rows = self.recent(n)
        if not rows:
            return "No auto-mode decisions recorded this session yet."
        return "\n".join(
            [
                "[bold]Recent auto-mode decisions[/bold] (newest last)",
                *(f"  {d.render()}" for d in rows),
            ]
        )


class AutoModeBreaker:
    """Pause auto mode after `threshold` consecutive risky verdicts (v6 #47).

    A reviewer that keeps flagging calls means the plan drifted from the goal;
    rather than nagging with dialog after dialog, auto mode hands the session
    back to the human until `/auto on` re-arms it.
    """

    def __init__(self, threshold: int = 3) -> None:
        self.threshold = max(1, threshold)
        self.consecutive_risky = 0
        self.tripped = False
        self.notified = False

    def record(self, risky: bool) -> bool:
        """Count a verdict; return True the moment the breaker trips."""
        if self.tripped:
            return False
        if not risky:
            self.consecutive_risky = 0
            return False
        self.consecutive_risky += 1
        if self.consecutive_risky >= self.threshold:
            self.tripped = True
            return True
        return False

    def reset(self) -> None:
        """Re-arm (called by `/auto on`)."""
        self.consecutive_risky = 0
        self.tripped = False
        self.notified = False

    def status(self) -> str:
        """Human-readable state for `/auto status`."""
        if self.tripped:
            return f"paused — {self.threshold} consecutive risky verdicts; /auto on re-arms"
        return f"armed ({self.consecutive_risky}/{self.threshold} consecutive risky verdicts)"


_APPROVAL_LEDGER = ApprovalLedger()
_BREAKER = AutoModeBreaker()


def get_approval_ledger() -> ApprovalLedger:
    """Return the session-wide approval ledger."""
    return _APPROVAL_LEDGER


def get_auto_mode_breaker(threshold: int | None = None) -> AutoModeBreaker:
    """Return the session-wide circuit breaker, updating its threshold when given."""
    if threshold is not None:
        _BREAKER.threshold = max(1, threshold)
    return _BREAKER


def record_approval_decisions(
    decisions: list[ApprovalDecision], working_dir: Path | str | None = None
) -> None:
    """Persist decisions to the ledger and assert them as Expert Mode facts.

    The fact (`approval_decision{tool, decision, rule_source, risk, reason}`)
    lands in the client-side expert engine's working memory, so `/why
    approval_decision tool=execute` and YAML rules that react to approvals
    both work. Fact assertion is best-effort: a missing or failing engine
    never blocks a turn.
    """
    for decision in decisions:
        _APPROVAL_LEDGER.record(decision)
    if working_dir is None:
        return
    try:
        from bog_agents_cli.expert_controller import get_controller

        engine = get_controller(working_dir).middleware.engine
        for decision in decisions:
            engine.assert_fact(
                Fact(
                    fact_type="approval_decision",
                    data={
                        "tool": decision.tool,
                        "call": decision.call,
                        "decision": decision.decision,
                        "rule_source": decision.rule_source,
                        "risk": decision.risk,
                        "reason": decision.reason,
                    },
                )
            )
    except Exception as exc:  # the ledger is the source of truth; facts are a bonus
        logger.debug("approval_decision fact not asserted: %s", exc)


def render_auto_mode_status(
    auto_on: bool, project_root: Path | str | None = None
) -> str:
    """Render `/auto status`: on/off, the review model in use, breaker state, ledger size."""
    _judge, judge_desc = resolve_risk_judge(
        load_auto_mode_settings(Path(project_root) if project_root else None)
    )
    return (
        f"auto mode is currently {'ON' if auto_on else 'OFF'}\n"
        f"  review model: {judge_desc}\n"
        f"  circuit breaker: {get_auto_mode_breaker().status()}\n"
        f"  decisions this session: {len(get_approval_ledger())} (/auto why [n])"
    )
