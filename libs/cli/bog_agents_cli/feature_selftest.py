"""Static self-test of the advertised slash-command surface (v6 Wave 0).

REVIEW v4 §4.1 asked for a doctor mode that proves advertised surfaces exist;
three cycles later `/think`, `/worktrees` and `/checkpoint load` were still
dead while `/help` promised them. This is the static half of that check. For
every registered slash command it verifies that

* the handler method exists on the App,
* every subcommand the spec advertises is mentioned in the handler (or in the
  `bog_agents_cli` modules the handler delegates to), and
* the handler does not depend on `self._middleware` — an attribute that is
  never assigned because the agent runs in the LangGraph server process,
  which was the shared root cause of v6 CLI-1.

It runs from `bog-agents --doctor-features` and from a unit test, so the
command surface cannot drift again without CI noticing. Pure and import-light:
it only reads source text via `inspect`.
"""

from __future__ import annotations

import inspect
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

DEAD_MIDDLEWARE_LOOKUP = 'getattr(self, "_middleware"'
"""Source fragment that marks a handler as depending on in-process middleware."""

_DELEGATE_IMPORT_RE = re.compile(r"from bog_agents_cli(?:\.(?P<mod>[\w.]+))? import")
_SELF_DELEGATE_RE = re.compile(r"self\.(?P<method>_(?:handle|dispatch)_\w+)\(")


@dataclass(frozen=True, slots=True)
class CommandAudit:
    """Result of auditing one slash command.

    Attributes:
        name: The slash command, e.g. `/think`.
        handler: The `BogAgentsApp` method name the registry dispatches to.
        problems: Human-readable findings; empty when the command is honest.
    """

    name: str
    handler: str
    problems: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """True when no problem was found."""
        return not self.problems


def _delegate_sources(handler_source: str) -> str:
    """Return the concatenated source of every `bog_agents_cli` module a handler imports."""
    import importlib

    chunks: list[str] = []
    for match in _DELEGATE_IMPORT_RE.finditer(handler_source):
        mod = match.group("mod")
        if not mod:
            continue
        try:
            module = importlib.import_module(f"bog_agents_cli.{mod}")
            chunks.append(inspect.getsource(module))
        except Exception as exc:
            # A delegate that cannot be imported simply adds no source.
            logger.debug("feature self-test: could not read delegate %s: %s", mod, exc)
            continue
    return "\n".join(chunks)


def _same_class_delegate_sources(
    app_cls: type, handler_source: str, *, seen: set[str]
) -> str:
    """Return the source of `self._handle_*` / `self._dispatch_*` methods a handler calls (one hop)."""
    chunks: list[str] = []
    for match in _SELF_DELEGATE_RE.finditer(handler_source):
        name = match.group("method")
        if name in seen:
            continue
        seen.add(name)
        target = getattr(app_cls, name, None)
        if target is None:
            continue
        try:
            chunks.append(inspect.getsource(target))
        except (OSError, TypeError):
            continue
    return "\n".join(chunks)


def _is_placeholder(token: str) -> bool:
    """`<objective>`, `[tag]`, `[from]..[to]` describe an argument, not a subcommand word."""
    return token.startswith(("<", "["))


def audit_command_surface(app_cls: type | None = None) -> list[CommandAudit]:
    """Audit every registered slash command against the App class.

    Args:
        app_cls: The class whose handler methods to inspect; defaults to
            `BogAgentsApp` (imported lazily so this module stays cheap).

    Returns:
        One `CommandAudit` per registered command, in registry order.
    """
    from bog_agents_cli.commands import COMMANDS

    if app_cls is None:
        from bog_agents_cli.app import BogAgentsApp

        app_cls = BogAgentsApp

    audits: list[CommandAudit] = []
    for command in COMMANDS:
        spec = command.spec
        handler_name = command.handler_method
        problems: list[str] = []
        handler = getattr(app_cls, handler_name, None)
        source = ""
        if handler is None:
            problems.append(f"handler {handler_name} is missing on {app_cls.__name__}")
        else:
            try:
                source = inspect.getsource(handler)
            except (OSError, TypeError):
                source = ""
        if source and DEAD_MIDDLEWARE_LOOKUP in source:
            problems.append(
                "handler looks for in-process middleware on self._middleware, which is never assigned (v6 CLI-1)"
            )
        if source and spec.subcommands:
            same_class = _same_class_delegate_sources(
                app_cls, source, seen={handler_name}
            )
            haystack = source + same_class + _delegate_sources(source + same_class)
            for sub_name, _description in spec.subcommands:
                token = sub_name.strip().split()[0] if sub_name.strip() else ""
                if token and not _is_placeholder(token) and token not in haystack:
                    problems.append(
                        f"advertised subcommand {sub_name!r} is not implemented by the handler"
                    )
        audits.append(
            CommandAudit(name=spec.name, handler=handler_name, problems=tuple(problems))
        )
    return audits


def render_audit(audits: list[CommandAudit]) -> str:
    """Render an audit as a one-page report."""
    bad = [a for a in audits if not a.ok]
    lines = [
        f"Slash-command surface: {len(audits)} commands audited, {len(bad)} with problems."
    ]
    for audit in bad:
        lines.append(f"  {audit.name}  ({audit.handler})")
        lines.extend(f"    - {problem}" for problem in audit.problems)
    if not bad:
        lines.append(
            "  Every advertised command has a handler, implements its subcommands, and depends on no dead middleware lookup."
        )
    return "\n".join(lines)


__all__ = [
    "DEAD_MIDDLEWARE_LOOKUP",
    "CommandAudit",
    "audit_command_surface",
    "render_audit",
]
