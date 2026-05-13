"""Two-tier behavioural rules: Laws (hard) and Constitution (soft).

* **Laws** live in ``.bog-agents/laws.md`` (project-level) or
  ``~/.bog-agents/laws.md`` (user-level). The middleware reads both
  and concatenates project laws on top of user laws. Each non-empty,
  non-comment line is one Law.
* **Constitution** lives in ``.bog-agents/constitution.md`` (project)
  and ``~/.bog-agents/constitution.md`` (user). Same shape.

Laws are pre-pended to the agent's system prompt at every model call,
under a ``## Hard rules`` heading. The middleware also checks the
model's *output* for explicit Law violations and, when
``reject_on_violation=True``, replaces the offending response with a
short refusal.

Constitution lines are appended under ``## Preferences`` — they shape
the agent without blocking it. Violations are logged into the
lifecycle store (so the user can audit "agent did X, which violates
Constitution rule N") but never refused.

Both files are MISSING by default; this is intentional. Without files
on disk the middleware loads as a passthrough — no laws, no system-
prompt mutation, no behaviour change.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
)

from bog_agents_cli.dreamscape.config import LawsConfig, is_emergency_disabled

logger = logging.getLogger(__name__)


# Markers inserted into the system prompt. We keep them deterministic
# so a downstream middleware (or the user) can grep for them.
_LAW_SECTION_HEADER = "## Hard rules — Laws (must follow)"
_CONSTITUTION_SECTION_HEADER = "## Preferences — Constitution (try to follow)"
_LAW_VIOLATION_REFUSAL = (
    "I cannot do that — it would violate one of the configured Laws "
    "for this agent. (Triggered: {laws})"
)


@dataclass
class Rule:
    """One parsed rule from a Laws or Constitution file."""

    text: str
    source_path: Path
    """Where the rule came from — useful for ``/laws audit`` output."""

    line_number: int

    def __post_init__(self) -> None:
        # Pre-compile a forgiving phrase match. We won't catch every
        # Law violation (no NLU), but for simple "never X" / "must Y"
        # constructions we can do better than nothing.
        self._key_phrases: list[str] = _extract_key_phrases(self.text)

    @property
    def key_phrases(self) -> list[str]:
        return self._key_phrases


@dataclass
class RuleSet:
    """A collection of rules drawn from one or more files."""

    laws: list[Rule] = field(default_factory=list)
    constitution: list[Rule] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.laws and not self.constitution


_BACKTICK_RE = re.compile(r"`([^`]{2,80})`")
_DANGEROUS_TOKEN_RE = re.compile(
    r"\b(rm\s+-rf|chmod\s+\d+|sudo\s+\w+|drop\s+table|truncate\s+\w+|"
    r"force[-\s]push|--no-verify|--force(?:-with-lease)?)\b",
    re.IGNORECASE,
)


def _extract_key_phrases(line: str) -> list[str]:
    r"""Pull keywords out of "never X" / "must Y" / "do not Z" patterns.

    Returns lower-cased substrings whose presence in agent output
    *suggests* a violation. The match is intentionally loose — we
    want false positives we can review, not false negatives that
    let dangerous output through.

    Three categories are extracted from each rule line:

    1. Anything inside backticks (commands, paths, code fragments) —
       these are usually the load-bearing part of a rule like
       ``Never run `rm -rf /``.
    2. Known dangerous tokens (``rm -rf``, ``--force-push``, etc.)
       harvested via a small allowlist of well-known patterns.
    3. The phrase following a "never" / "must not" / "do not" /
       "avoid" / "forbidden" verb, trimmed to its first content
       words for paraphrase tolerance.

    Comparison is normalised on both sides via
    :func:`_normalize_for_match` so an agent's plain ``rm -rf /``
    matches a rule's backtick-formatted ``\`rm -rf /\```.
    """
    raw = line.strip().lstrip("-*•").strip()
    text = _normalize_for_match(raw)

    phrases: list[str] = []
    seen: set[str] = set()

    def _add(candidate: str) -> None:
        candidate = candidate.strip().rstrip(".,;:")
        candidate = _normalize_for_match(candidate)
        if len(candidate) >= 3 and candidate not in seen:
            phrases.append(candidate)
            seen.add(candidate)

    # (1) Backtick-quoted spans from the original line, then normalised.
    for m in _BACKTICK_RE.finditer(raw):
        _add(m.group(1))

    # (2) Dangerous tokens regardless of formatting.
    for m in _DANGEROUS_TOKEN_RE.finditer(text):
        _add(m.group(1))

    # (3) "Never X" / "must not Y" / "do not Z" tails.
    verb_patterns = (
        r"\bnever\s+(.{3,80})",
        r"\bdo not\s+(.{3,80})",
        r"\bdon'?t\s+(.{3,80})",
        r"\bmust not\s+(.{3,80})",
        r"\bforbidden(?:\s*[:]\s*)(.{3,80})",
        r"\bavoid\s+(.{3,80})",
    )
    for pat in verb_patterns:
        for m in re.finditer(pat, text):
            tail = m.group(1).strip().rstrip(".,;:")
            if len(tail) >= 3:
                _add(tail)
                # First few words as a paraphrase-tolerant variant.
                words = tail.split()
                if len(words) >= 2:
                    _add(" ".join(words[:4]))
    return phrases


def _normalize_for_match(text: str) -> str:
    """Lower-case + strip decorative chars (backticks, quotes) for matching."""
    cleaned = text.lower().strip().lstrip("-*•").strip()
    return cleaned.replace("`", "").replace('"', "").replace("'", "")


def _read_rule_file(path: Path) -> list[Rule]:
    """Read a markdown rules file. Empty list when missing/unreadable.

    Format:
      * one rule per non-empty, non-comment line
      * leading ``-`` / ``*`` / ``•`` bullet markers are stripped
      * lines starting with ``#`` are treated as headings (skipped)
      * blank lines + ``//`` comments are skipped
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    out: list[Rule] = []
    for lineno, line in enumerate(raw.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        if stripped.startswith("//"):
            continue
        # Strip bullets but keep the body.
        body = stripped.lstrip("-*•").strip()
        if not body:
            continue
        out.append(Rule(text=body, source_path=path, line_number=lineno))
    return out


def _candidate_paths(spec: str, *, project_root: Path) -> list[Path]:
    """Yield candidate paths in priority order (project first, user fallback)."""
    user_path = Path(spec).expanduser()
    paths: list[Path] = []
    # Project-level override: same filename but inside the cwd's
    # ``.bog-agents`` directory.
    project_path = project_root / ".bog-agents" / user_path.name
    if project_path != user_path:
        paths.append(project_path)
    paths.append(user_path)
    return paths


def load_rules(cfg: LawsConfig, project_root: Path | None = None) -> RuleSet:
    """Load Laws + Constitution from disk. Returns an empty RuleSet on miss."""
    root = project_root or Path.cwd()
    rs = RuleSet()
    for path in _candidate_paths(cfg.laws_path, project_root=root):
        rs.laws.extend(_read_rule_file(path))
    for path in _candidate_paths(cfg.constitution_path, project_root=root):
        rs.constitution.extend(_read_rule_file(path))
    return rs


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


# No durable LangGraph state — rules are loaded fresh from disk on
# every call. Skip the TypedDict shape and use the un-parameterised
# AgentMiddleware base class to keep typing aligned with the rest of
# the CLI middleware stack.


class LawsMiddleware(AgentMiddleware):
    """Apply Laws + Constitution to every model call.

    With ``cfg.enabled=False`` or the emergency disable env var set,
    every hook is a passthrough. With files absent, the middleware
    is a passthrough even when enabled — it just has nothing to do.

    Args:
        cfg: Laws config (paths + enforcement flags).
        project_root: Where to look for project-level overrides. Falls
            back to ``Path.cwd()`` at call time.
        violation_recorder: Optional callable invoked when a
            Constitution violation is detected. Used to wire the
            lifecycle log without taking a hard dependency on it.
    """

    def __init__(
        self,
        *,
        cfg: LawsConfig | None = None,
        project_root: Path | None = None,
        violation_recorder: Callable[[str, list[str]], None] | None = None,
    ) -> None:
        self._cfg = cfg or LawsConfig()
        self._project_root = project_root
        self._violation_recorder = violation_recorder
        self._tools: list[Any] = []

    @property
    def tools(self) -> list[Any]:
        return self._tools

    @property
    def active(self) -> bool:
        return self._cfg.enabled and not is_emergency_disabled()

    # ------------------------------------------------------------------
    # Hooks
    # ------------------------------------------------------------------

    def wrap_model_call(  # type: ignore[override]
        self,
        request: ModelRequest,
        call_next: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        if not self.active:
            return call_next(request)
        rule_set = self._safe_load_rules()
        if rule_set.is_empty():
            return call_next(request)
        try:
            request = self._inject_rules(request, rule_set)
        except Exception:
            logger.exception("LawsMiddleware: failed to inject rules; passing through")
            return call_next(request)
        response = call_next(request)
        return self._check_response(response, rule_set)

    async def awrap_model_call(  # type: ignore[override]
        self,
        request: ModelRequest,
        call_next: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        if not self.active:
            return await call_next(request)
        rule_set = self._safe_load_rules()
        if rule_set.is_empty():
            return await call_next(request)
        try:
            request = self._inject_rules(request, rule_set)
        except Exception:
            logger.exception("LawsMiddleware: failed to inject rules; passing through")
            return await call_next(request)
        response = await call_next(request)
        return self._check_response(response, rule_set)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _safe_load_rules(self) -> RuleSet:
        try:
            return load_rules(self._cfg, project_root=self._project_root)
        except Exception:
            logger.exception("LawsMiddleware: failed to load rules")
            return RuleSet()

    @staticmethod
    def _format_rules_block(header: str, rules: list[Rule]) -> str:
        if not rules:
            return ""
        lines = [header, ""]
        for rule in rules:
            lines.append(f"- {rule.text}")
        lines.append("")
        return "\n".join(lines)

    def _inject_rules(self, request: ModelRequest, rule_set: RuleSet) -> ModelRequest:
        """Append a rules block to the model request's system prompt."""
        from bog_agents.middleware._utils import append_to_system_message

        blocks: list[str] = []
        if rule_set.laws:
            blocks.append(self._format_rules_block(_LAW_SECTION_HEADER, rule_set.laws))
        if rule_set.constitution:
            blocks.append(
                self._format_rules_block(
                    _CONSTITUTION_SECTION_HEADER, rule_set.constitution
                )
            )
        if not blocks:
            return request
        addendum = "\n\n".join(blocks)
        try:
            return append_to_system_message(request, addendum)  # type: ignore[return-value]
        except Exception:
            logger.exception("LawsMiddleware: append_to_system_message failed")
            return request

    def _check_response(
        self, response: ModelResponse, rule_set: RuleSet
    ) -> ModelResponse:
        text = _response_text(response)
        if not text:
            return response

        # Hard Laws check
        violations = _violation_phrases(text, rule_set.laws)
        if violations:
            logger.warning(
                "LawsMiddleware: detected Law violation phrases: %s", violations
            )
            if self._cfg.reject_on_violation:
                return _replace_response_content(
                    response, _LAW_VIOLATION_REFUSAL.format(laws=", ".join(violations))
                )

        # Soft Constitution check
        if self._cfg.log_constitution_violations:
            soft_violations = _violation_phrases(text, rule_set.constitution)
            if soft_violations:
                logger.info(
                    "LawsMiddleware: Constitution violations (log-only): %s",
                    soft_violations,
                )
                if self._violation_recorder is not None:
                    with suppress(Exception):
                        self._violation_recorder("constitution", soft_violations)
        return response


# ---------------------------------------------------------------------------
# Response inspection helpers
# ---------------------------------------------------------------------------


def _response_text(response: Any) -> str:
    """Best-effort string extraction from a model response."""
    content = getattr(response, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                value = part.get("text")
                if isinstance(value, str):
                    parts.append(value)
            elif isinstance(part, str):
                parts.append(part)
        return "".join(parts)
    return str(content) if content is not None else ""


def _violation_phrases(text: str, rules: list[Rule]) -> list[str]:
    """Return the set of phrases that appear in ``text`` and match a rule."""
    if not text or not rules:
        return []
    haystack = _normalize_for_match(text)
    matched: list[str] = []
    for rule in rules:
        for phrase in rule.key_phrases:
            if phrase in haystack:
                matched.append(phrase)
    return matched


def _replace_response_content(response: Any, new_content: str) -> Any:
    """Build a new response object with ``content`` replaced."""
    # ModelResponse is a pydantic model (langchain) — use model_copy if
    # available; fall back to mutating.
    try:
        if hasattr(response, "model_copy"):
            return response.model_copy(update={"content": new_content})
    except Exception:
        pass
    try:
        response.content = new_content  # type: ignore[attr-defined]
    except Exception:
        pass
    return response


# ---------------------------------------------------------------------------
# Audit + presets
# ---------------------------------------------------------------------------


@dataclass
class AuditResult:
    """Result of a dry-run audit (used by ``/laws audit`` slash command)."""

    laws_found: int
    constitution_found: int
    sample_text: str
    violations: list[str]
    matched_rules: list[Rule]

    def summary(self) -> str:
        if self.laws_found == 0 and self.constitution_found == 0:
            return (
                "No rules configured. Drop a ``.bog-agents/laws.md`` or "
                "``~/.bog-agents/laws.md`` to start."
            )
        parts: list[str] = [
            f"Laws: {self.laws_found}; Constitution: {self.constitution_found}.",
        ]
        if self.violations:
            parts.append(
                f"Sample triggers {len(self.violations)} rule(s): "
                f"{', '.join(self.violations)}."
            )
        else:
            parts.append("Sample text triggers no configured rules.")
        return " ".join(parts)


def audit_text(
    sample: str, cfg: LawsConfig, project_root: Path | None = None
) -> AuditResult:
    """Dry-run the rules against ``sample`` — used by ``/laws audit``."""
    rule_set = load_rules(cfg, project_root=project_root)
    all_rules = rule_set.laws + rule_set.constitution
    matched_phrases: list[str] = []
    matched_rules: list[Rule] = []
    haystack = _normalize_for_match(sample)
    for rule in all_rules:
        for phrase in rule.key_phrases:
            if phrase in haystack:
                matched_phrases.append(phrase)
                matched_rules.append(rule)
                break
    return AuditResult(
        laws_found=len(rule_set.laws),
        constitution_found=len(rule_set.constitution),
        sample_text=sample,
        violations=matched_phrases,
        matched_rules=matched_rules,
    )


DEFAULT_LAWS_TEMPLATE = """\
# Hard Laws — the bog-agents agent MUST follow these.
#
# Lines starting with `#` are comments and ignored. Bullets (`-` / `*`)
# are stripped. Each non-empty line is one Law. Keep them short and
# imperative — "Never do X", "Must Y", "Forbidden: Z".

- Never run `rm -rf /` or any unbounded recursive delete.
- Never exfiltrate, log, or echo API keys, tokens, or session cookies.
- Never silently rewrite git history (force-push, amend published commits).
- Never disable or bypass safety middlewares without explicit user consent.
- Never commit secret material to version control.
"""

DEFAULT_CONSTITUTION_TEMPLATE = """\
# Constitution — preferences the agent should TRY to follow.
#
# Soft rules. The agent may deviate when warranted but each deviation
# is logged so you can audit later.

- Prefer small, focused pull requests over giant ones.
- Add tests when introducing new behavior, even if not requested.
- Avoid touching files outside the scope of the user's request.
- Ask before destructive operations even when permission is granted.
- Use clear, plain-language naming over clever abbreviations.
"""


def write_default_templates(
    cfg: LawsConfig, *, project_root: Path | None = None, overwrite: bool = False
) -> list[Path]:
    """Write starter Laws + Constitution files. Returns the paths created."""
    root = project_root or Path.cwd()
    written: list[Path] = []
    for spec, body in (
        (cfg.laws_path, DEFAULT_LAWS_TEMPLATE),
        (cfg.constitution_path, DEFAULT_CONSTITUTION_TEMPLATE),
    ):
        # Project-first, then user
        target = root / ".bog-agents" / Path(spec).expanduser().name
        if target.exists() and not overwrite:
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
            written.append(target)
        except OSError as exc:
            logger.warning("could not write %s: %s", target, exc)
    return written
