"""Variable subsystem for record/replay and QA plans.

A *VarBundle* is a dict of named slots — strings, secrets, or constrained
enums — declared in a YAML file. Substitution uses ``${name}`` syntax.
Secrets are never serialized; they live only in the in-memory
:class:`bog_agents_cli.vault.SessionVault`.

Typical lifecycle::

    bundle = VarBundle.from_dict(
        {
            "jira_ticket": {"type": "string", "default": "JIRA-134"},
            "api_key": {"type": "secret"},
        }
    )
    # Some values come from CLI: --var jira_ticket=JIRA-200
    bundle.set("jira_ticket", "JIRA-200")
    # Resolve any missing values (prompts user via supplied callback for
    # non-secret values; secrets go through prompt_secret + vault).
    await bundle.resolve(prompt=ask_user, prompt_secret=ask_user_secret)
    # Render templates anywhere the bundle is in scope:
    rendered = bundle.substitute("Open PR for ${repo_id}")

This module is provider-agnostic — it doesn't know about Textual, agents,
or anything else. The CLI app provides the prompt callbacks.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from bog_agents_cli.vault import SecretStr, SessionVault, get_default_vault

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Var spec / bundle
# ---------------------------------------------------------------------------

VarType = str  # "string" | "secret" | "enum" | "int" | "bool"

# Pattern for ${var_name} substitution. Names accept letters, digits, and _.
# We deliberately don't support ${a.b} or ${a:-default} — keep it boring.
_VAR_PATTERN = re.compile(r"\$\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


class VarError(ValueError):
    """Raised for malformed var specs or substitution failures."""


@dataclass
class VarSpec:
    """Declaration of a single variable slot."""

    name: str
    type: VarType = "string"
    default: Any = None
    description: str = ""
    choices: list[str] = field(default_factory=list)
    required: bool = True

    def __post_init__(self) -> None:
        if not self.name or not _VAR_PATTERN.fullmatch("${" + self.name + "}"):
            msg = f"invalid var name: {self.name!r}"
            raise VarError(msg)
        if self.type not in ("string", "secret", "enum", "int", "bool"):
            msg = f"unknown var type: {self.type!r}"
            raise VarError(msg)
        if self.type == "enum" and not self.choices:
            msg = f"enum var {self.name!r} requires non-empty 'choices'"
            raise VarError(msg)
        if self.default is not None and self.type == "secret":
            msg = f"secret var {self.name!r} must not declare a default value"
            raise VarError(msg)

    @classmethod
    def from_dict(cls, name: str, d: dict[str, Any]) -> VarSpec:
        return cls(
            name=name,
            type=str(d.get("type", "string")),
            default=d.get("default"),
            description=str(d.get("description", "")),
            choices=list(d.get("choices", [])),
            required=bool(d.get("required", True)),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.type}
        if self.default is not None:
            out["default"] = self.default
        if self.description:
            out["description"] = self.description
        if self.choices:
            out["choices"] = list(self.choices)
        if not self.required:
            out["required"] = False
        return out

    def coerce(self, raw: str) -> Any:
        """Coerce a raw string answer into the spec's declared type."""
        if self.type in ("string", "secret"):
            return raw
        if self.type == "int":
            try:
                return int(raw)
            except ValueError as exc:
                msg = f"var {self.name!r} expected an integer, got {raw!r}"
                raise VarError(msg) from exc
        if self.type == "bool":
            low = raw.strip().lower()
            if low in ("1", "true", "yes", "y", "on"):
                return True
            if low in ("0", "false", "no", "n", "off"):
                return False
            msg = f"var {self.name!r} expected a boolean, got {raw!r}"
            raise VarError(msg)
        if self.type == "enum":
            if raw not in self.choices:
                msg = f"var {self.name!r} must be one of {self.choices}, got {raw!r}"
                raise VarError(msg)
            return raw
        msg = f"unknown var type: {self.type!r}"
        raise VarError(msg)


# Callback signatures for runtime prompting. Two flavours so secret values
# can route through a different UI (typically a masked input).
NormalPromptFn = Callable[[VarSpec], Awaitable[str]]
SecretPromptFn = Callable[[VarSpec], Awaitable[str]]


@dataclass
class VarBundle:
    """A collection of variable specs plus current resolved values.

    Plain-typed values (string/int/bool/enum) are stored in ``values``.
    Secrets are *not* stored here — they go to the supplied
    :class:`SessionVault`. ``substitute`` consults both.
    """

    specs: dict[str, VarSpec] = field(default_factory=dict)
    values: dict[str, Any] = field(default_factory=dict)
    vault: SessionVault = field(default_factory=get_default_vault)

    @classmethod
    def from_dict(
        cls, raw: dict[str, Any] | None, *, vault: SessionVault | None = None
    ) -> VarBundle:
        """Build a bundle from a YAML/JSON dict.

        ``raw`` maps var name → spec dict (or empty/None for an empty bundle).
        """
        specs: dict[str, VarSpec] = {}
        for name, spec_raw in (raw or {}).items():
            if not isinstance(spec_raw, dict):
                msg = f"var {name!r}: spec must be a dict"
                raise VarError(msg)
            specs[name] = VarSpec.from_dict(name, spec_raw)
        return cls(specs=specs, vault=vault or get_default_vault())

    def to_dict(self) -> dict[str, Any]:
        """Serialize spec definitions only — never values, never secrets."""
        return {name: spec.to_dict() for name, spec in self.specs.items()}

    def declare(self, spec: VarSpec) -> None:
        """Register a new var spec. Idempotent if name already declared."""
        self.specs[spec.name] = spec

    def set(self, name: str, value: Any) -> None:
        """Set the resolved value for ``name``.

        For secret-typed vars the value is written to the vault, not the
        plain ``values`` dict, so it's never accidentally serialized.

        Args:
            name: Var name (must already be declared).
            value: Raw value. Strings are coerced via the spec.
        """
        spec = self.specs.get(name)
        if spec is None:
            msg = f"unknown var: {name!r} (declare it first)"
            raise VarError(msg)
        if spec.type == "secret":
            if not isinstance(value, (str, SecretStr)):
                msg = f"secret var {name!r} requires a string value"
                raise VarError(msg)
            self.vault.put(name, value)
            return
        coerced = spec.coerce(value) if isinstance(value, str) else value
        self.values[name] = coerced

    def get(self, name: str) -> Any:
        """Return the resolved value for ``name``, or None if unresolved.

        For secret vars the returned wrapper is a ``SecretStr``; coerce it
        explicitly via ``.get_secret_value()`` when handing to a tool.
        """
        spec = self.specs.get(name)
        if spec is None:
            return None
        if spec.type == "secret":
            return self.vault.get(name)
        if name in self.values:
            return self.values[name]
        return spec.default

    def is_resolved(self, name: str) -> bool:
        """Return True if ``name`` has a value from the user OR a non-None default."""
        spec = self.specs.get(name)
        if spec is None:
            return False
        if spec.type == "secret":
            return self.vault.has(name)
        if name in self.values:
            return True
        return spec.default is not None or not spec.required

    def missing(self) -> list[VarSpec]:
        """Return specs for required vars that have no value or default."""
        out: list[VarSpec] = []
        for name, spec in self.specs.items():
            if not spec.required:
                continue
            if not self.is_resolved(name):
                out.append(spec)
        return out

    async def resolve(
        self,
        *,
        prompt: NormalPromptFn,
        prompt_secret: SecretPromptFn | None = None,
        cli_overrides: dict[str, str] | None = None,
    ) -> None:
        """Fill in any missing values by applying overrides then prompting.

        Args:
            prompt: Async callback invoked for each missing non-secret var.
                Receives the :class:`VarSpec` and must return the raw user
                answer as a string.
            prompt_secret: Async callback for secret vars. Defaults to
                ``prompt`` if omitted, but the caller is strongly encouraged
                to use a masked input.
            cli_overrides: Map of var name → raw value (e.g. from
                ``--var name=value`` CLI args). Applied before prompting.
        """
        ps = prompt_secret or prompt

        # Apply CLI overrides first.
        for name, raw in (cli_overrides or {}).items():
            if name in self.specs:
                self.set(name, raw)
            else:
                logger.warning("ignoring --var %s: not declared in bundle", name)

        # Prompt for anything still missing.
        for spec in list(self.missing()):
            cb = ps if spec.type == "secret" else prompt
            answer = (await cb(spec)).strip()
            if not answer and spec.default is not None:
                # User accepted the default by entering nothing.
                self.set(spec.name, spec.default)
            elif not answer and not spec.required:
                continue
            else:
                self.set(spec.name, answer)

    def substitute(self, template: str | dict[str, Any] | list[Any]) -> Any:
        """Recursively replace ``${var_name}`` placeholders in ``template``.

        For secret vars, the substituted value is the cleartext (so HTTP
        headers, env dicts, etc. work). Avoid logging the result.

        Args:
            template: A string with ``${name}`` placeholders, or a nested
                dict/list of such strings.

        Returns:
            The same shape as ``template`` with placeholders replaced.

        Raises:
            VarError: If a referenced var is not declared.
        """
        if isinstance(template, str):
            return self._sub_string(template)
        if isinstance(template, dict):
            return {k: self.substitute(v) for k, v in template.items()}
        if isinstance(template, list):
            return [self.substitute(v) for v in template]
        return template

    def _sub_string(self, s: str) -> str:
        def _replace(m: re.Match[str]) -> str:
            name = m.group(1)
            if name not in self.specs:
                msg = f"reference to undeclared var: ${{{name}}}"
                raise VarError(msg)
            value = self.get(name)
            if value is None:
                msg = f"var ${{{name}}} is unresolved (call resolve() first)"
                raise VarError(msg)
            if isinstance(value, SecretStr):
                return value.get_secret_value()
            return str(value)

        return _VAR_PATTERN.sub(_replace, s)


# ---------------------------------------------------------------------------
# Auto-variabilizer — extract probable variables from recorded text
# ---------------------------------------------------------------------------

# Each pattern produces a (name, regex) pair. The first capture group of the
# regex is what gets replaced by ${name}. Names are deliberately generic
# (``jira_ticket``, ``repo_url``) — users edit them after recording.
# Each pattern is ordered most-specific first so a Jira ticket inside a URL
# is detected as a ticket, not as part of the URL. The first capture group
# is what gets replaced.
_AUTO_VAR_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Jira-style ticket: 2+ uppercase letters, hyphen, digits.
    ("jira_ticket", re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")),
    # GitHub repo: only when context makes it unambiguous — preceded by
    # ``github.com/``, ``git@github.com:``, or suffixed with ``.git``. Plain
    # ``a/b`` slugs are far too easy to false-match against file paths.
    (
        "github_repo",
        re.compile(
            r"(?:github\.com[/:]|\bgit@github\.com:)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?=\.git\b|\b)"
        ),
    ),
    # Plain URL.
    ("url", re.compile(r"\b(https?://[^\s<>\"']+)")),
    # UUID.
    (
        "uuid",
        re.compile(
            r"\b([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\b"
        ),
    ),
)


@dataclass
class _Hit:
    name: str
    matched: str
    occurrences: int


def auto_variabilize(text: str) -> tuple[str, dict[str, str]]:
    """Replace known patterns in ``text`` with ``${name}`` placeholders.

    The first occurrence of each distinct match becomes the canonical value
    for that var name; subsequent occurrences of the same literal are
    replaced with the same placeholder. Different literals matched by the
    same pattern get suffixed names (e.g. ``jira_ticket``,
    ``jira_ticket_2``).

    Args:
        text: Raw text to scan. Typically a recorded user prompt.

    Returns:
        Tuple of ``(rewritten_text, vars_map)`` where ``vars_map`` maps
        var name → original value (suitable for use as ``default`` in a
        :class:`VarSpec`).
    """
    rewritten = text
    vars_map: dict[str, str] = {}
    seen_literals: dict[str, str] = {}  # literal value → assigned var name

    for base_name, pattern in _AUTO_VAR_PATTERNS:
        idx = 0
        # Walk matches left-to-right and replace.
        while True:
            m = pattern.search(rewritten)
            if not m:
                break
            literal = m.group(1)
            if literal in seen_literals:
                var_name = seen_literals[literal]
            else:
                idx += 1
                var_name = (
                    base_name
                    if idx == 1 and base_name not in vars_map
                    else f"{base_name}_{idx}"
                )
                # Bump idx until we find a free name.
                while var_name in vars_map:
                    idx += 1
                    var_name = f"{base_name}_{idx}"
                vars_map[var_name] = literal
                seen_literals[literal] = var_name
            placeholder = "${" + var_name + "}"
            # Replace only this span so we don't keep re-matching.
            rewritten = rewritten[: m.start(1)] + placeholder + rewritten[m.end(1) :]
    return rewritten, vars_map
