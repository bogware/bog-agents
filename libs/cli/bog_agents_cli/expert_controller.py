"""Stand-alone controller for the ``/expert``, ``/why``, ``/prove`` commands.

Backs the ``/expert``, ``/expert trace``, ``/why``, and ``/prove`` slash
commands. All user-facing logic lives in the ``expert`` sub-package
(``expert/status.py``, ``expert/write.py``, ``expert/wizard.py``,
``expert/propose.py``, ``expert/watch.py``) so this controller stays a
thin façade. One :class:`ExpertController` instance is created per
working directory and cached in :data:`_CONTROLLERS`.

The controller owns one :class:`ExpertRulesMiddleware` instance, so facts
asserted by tool calls and facts asserted by the user via ``/expert assert``
share the same working memory.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from bog_agents.middleware.expert_rules import ExpertRulesMiddleware

from bog_agents_cli.expert import (
    propose as _propose_mod,
    status as _status_mod,
    watch as _watch_mod,
    wizard as _wizard_mod,
    write as _write_mod,
)
from bog_agents_cli.expert._helpers import _parse_pattern_args, _split_subcommand

if TYPE_CHECKING:
    from bog_agents.middleware.expert_engine import AuthoringProposal

# ---------------------------------------------------------------------------
# Singleton registry
# ---------------------------------------------------------------------------

_CONTROLLERS: dict[Path, ExpertController] = {}


def get_controller(
    working_dir: Path | str,
    *,
    model_factory: Any | None = None,  # noqa: ANN401 — Callable[[], BaseChatModel]
) -> ExpertController:
    """Return the (per-cwd) singleton controller.

    Args:
        working_dir: Project root. Different roots get independent
            controllers — useful when the CLI hops between repos via
            ``/cd``.
        model_factory: Optional zero-arg callable returning a fresh
            chat model. Required for ``/expert write`` (LLM authoring).
            Honored only on first call per cwd; later calls return the
            cached instance unchanged. Use :func:`reset_controllers`
            to swap.
    """
    key = Path(working_dir).resolve()
    if key not in _CONTROLLERS:
        _CONTROLLERS[key] = ExpertController(
            working_dir=key, model_factory=model_factory
        )
    return _CONTROLLERS[key]


def reset_controllers() -> None:
    """Drop every cached controller. Test-only helper."""
    _CONTROLLERS.clear()


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


class ExpertController:
    """Slash-command-facing facade around :class:`ExpertRulesMiddleware`.

    Every public method is a 1-line delegator into one of the
    ``expert/*.py`` sub-modules. This file owns construction +
    delegation only.

    Args:
        working_dir: Project root (rules are loaded from
            ``<working_dir>/.bog-agents/expert_rules/``).
        middleware: Optional preconstructed middleware. Tests use this
            to inject programmatic rules.
        model_factory: Zero-arg callable returning a fresh chat model.
            Required for ``/expert write`` (LLM-driven rule authoring).
            When None, the write flow refuses cleanly with an
            actionable error.
    """

    def __init__(
        self,
        *,
        working_dir: Path,
        middleware: ExpertRulesMiddleware | None = None,
        model_factory: Any | None = None,  # noqa: ANN401 — Callable[[], BaseChatModel]
    ) -> None:
        self._working_dir = working_dir
        self._middleware = middleware or ExpertRulesMiddleware(
            working_dir=working_dir,
            enabled=False,  # start disabled — explicit opt-in via /expert on
        )
        self._model_factory = model_factory
        # ``/expert write`` stashes the most recent proposal here so
        # ``/expert write save [name]`` can commit it.
        self._pending_proposal: AuthoringProposal | None = None
        # Optional async callback fired by ``/expert watch`` whenever
        # the scheduled proposer completes one run. The TUI handler
        # registers this via :meth:`set_watch_summary_callback`.
        self._on_watch_summary: Any | None = None

    # ------------------------------------------------------------------
    # Used by app.py to register the middleware with create_agent
    # ------------------------------------------------------------------

    @property
    def middleware(self) -> ExpertRulesMiddleware:
        """The underlying middleware (exposed for ``create_agent`` registration)."""
        return self._middleware

    # -- status / introspection / dry-run (expert.status) ---------------

    def status(self) -> str:
        return _status_mod.status(self)

    def set_enabled(self, on: bool) -> str:
        return _status_mod.set_enabled(self, on)

    def reload(self) -> str:
        return _status_mod.reload(self)

    def list_rules(self) -> str:
        return _status_mod.list_rules(self)

    def show_rule(self, name: str) -> str:
        return _status_mod.show_rule(self, name)

    def trace(self, limit: int = 50) -> str:
        return _status_mod.trace(self, limit=limit)

    def explain(self, fact_type: str, **fields: Any) -> str:
        return _status_mod.explain(self, fact_type, **fields)

    def prove(self, fact_type: str, **fields: Any) -> str:
        return _status_mod.prove(self, fact_type, **fields)

    def assert_fact(self, fact_type: str, **fields: Any) -> str:
        return _status_mod.assert_fact(self, fact_type, **fields)

    def run(self) -> str:
        return _status_mod.run(self)

    @staticmethod
    def example() -> str:
        return _status_mod.example()

    def lint(self) -> str:
        return _status_mod.lint(self)

    def dry_run(self, fact_type: str, **fields: Any) -> str:
        return _status_mod.dry_run(self, fact_type, **fields)

    def memory_stats(self) -> str:
        return _status_mod.memory_stats(self)

    def clear_memory(self) -> str:
        return _status_mod.clear_memory(self)

    # ------------------------------------------------------------------
    # /expert write (expert.write)
    # ------------------------------------------------------------------

    def write(self, intent: str) -> str:
        return _write_mod.write(self, intent)

    def write_save(self, filename: str = "") -> str:
        return _write_mod.write_save(self, filename)

    def discard_proposal(self) -> str:
        return _write_mod.discard_proposal(self)

    # ------------------------------------------------------------------
    # /expert wizard (expert.wizard)
    # ------------------------------------------------------------------

    def wizard(self, args: str) -> str:
        return _wizard_mod.wizard(self, args)

    # ------------------------------------------------------------------
    # /expert propose + /expert proposals (expert.propose)
    # ------------------------------------------------------------------

    def propose_from_dreamscape(
        self,
        agent_id: str = "default",
        *,
        auto_activate: bool = False,
    ) -> str:
        return _propose_mod.propose_from_dreamscape(
            self, agent_id, auto_activate=auto_activate
        )

    def list_proposals(self) -> str:
        return _propose_mod.list_proposals(self)

    def approve_proposal_file(self, name: str) -> str:
        return _propose_mod.approve_proposal_file(self, name)

    def discard_proposal_file(self, name: str) -> str:
        return _propose_mod.discard_proposal_file(self, name)

    # ------------------------------------------------------------------
    # /expert watch (expert.watch)
    # ------------------------------------------------------------------

    def set_watch_summary_callback(self, fn: Any | None) -> None:  # noqa: ANN401 — Optional[Callable[[str], Awaitable[None]]]
        _watch_mod.set_watch_summary_callback(self, fn)

    def resume_watcher_if_persisted(self) -> tuple[bool, str]:
        return _watch_mod.resume_watcher_if_persisted(self)

    # Underscore-prefixed dispatch hooks are kept as 1-line delegators
    # because existing tests poke them directly (e.g. test_expert_watch).
    def _dispatch_write(self, rest: str) -> str:
        return _write_mod.dispatch_write(self, rest)

    def _dispatch_watch(self, rest: str) -> str:
        return _watch_mod.dispatch_watch(self, rest)

    def _dispatch_watch_start(self, rest: str) -> str:
        return _watch_mod.dispatch_watch_start(self, rest)

    def _dispatch_watch_stop(self) -> str:
        return _watch_mod.dispatch_watch_stop(self)

    def _dispatch_proposals(self, rest: str) -> str:
        return _propose_mod.dispatch_proposals(self, rest)

    # ------------------------------------------------------------------
    # Slash-command dispatcher (one entry point per command surface)
    # ------------------------------------------------------------------

    def handle_expert(self, args: str) -> str:
        """Dispatch ``/expert [subcommand …]``."""
        sub, rest = _split_subcommand(args)
        if not sub:
            return self.status()
        if sub == "on":
            return self.set_enabled(True)
        if sub == "off":
            return self.set_enabled(False)
        if sub == "reload":
            return self.reload()
        if sub in ("list", "rules"):
            return self.list_rules()
        if sub == "show":
            return self.show_rule(rest.strip())
        if sub == "trace":
            try:
                limit = int(rest.strip()) if rest.strip() else 50
            except ValueError:
                limit = 50
            return self.trace(limit=limit)
        if sub == "memory":
            return self.memory_stats()
        if sub == "clear":
            return self.clear_memory()
        if sub == "assert":
            ft, fields = _parse_pattern_args(rest)
            if not ft:
                return "Usage: /expert assert <fact_type> [field=value ...]"
            return self.assert_fact(ft, **fields)
        if sub == "run":
            return self.run()
        if sub == "example":
            return self.example()
        if sub == "lint":
            return self.lint()
        if sub in ("dry-run", "dryrun"):
            ft, fields = _parse_pattern_args(rest)
            return self.dry_run(ft, **fields)
        if sub == "write":
            return _write_mod.dispatch_write(self, rest)
        if sub == "propose":
            tokens = rest.strip().split()
            auto = False
            agent_tokens = []
            for tok in tokens:
                if tok in ("--apply", "--auto", "--activate"):
                    auto = True
                else:
                    agent_tokens.append(tok)
            agent = " ".join(agent_tokens).strip() or "default"
            return self.propose_from_dreamscape(agent, auto_activate=auto)
        if sub == "wizard":
            return self.wizard(rest)
        if sub == "watch":
            return _watch_mod.dispatch_watch(self, rest)
        if sub == "proposals":
            return _propose_mod.dispatch_proposals(self, rest)
        if sub == "status":
            return self.status()
        return (
            f"Unknown /expert subcommand: '{sub}'.\n\n"
            "Try one of:\n"
            "  /expert                              — show status\n"
            "  /expert on|off                       — toggle the engine\n"
            "  /expert list                         — list loaded rules\n"
            "  /expert show <name>                  — show a rule\n"
            "  /expert lint                         — static analysis of the rulebook\n"
            "  /expert trace [N]                    — last run trace\n"
            "  /expert memory                       — working-memory contents\n"
            "  /expert clear                        — wipe working memory\n"
            "  /expert assert <fact_type> k=v ...    — inject a fact\n"
            "  /expert dry-run <fact_type> k=v ...   — simulate without persisting\n"
            "  /expert write <intent>               — LLM generates a rule from your description\n"
            "  /expert write save [filename]        — commit the most recent /expert write proposal\n"
            "  /expert write cancel                 — discard the pending proposal\n"
            "  /expert propose [agent] [--apply]    — mine dreams + history → propose (or apply) rules\n"
            "  /expert proposals                    — list pending proposals\n"
            "  /expert proposals approve <name>     — promote a proposal to active rules\n"
            "  /expert proposals discard <name>     — delete a proposal\n"
            "  /expert run                          — run engine to fixed point\n"
            "  /expert reload                       — reload rules from disk\n"
            "  /expert wizard                       — show the guided setup menu\n"
            "  /expert wizard <category> <intent>   — build a rule via the wizard\n"
            "  /expert watch                        — show watcher status\n"
            "  /expert watch start [N] [--apply]    — start the scheduled proposer\n"
            "  /expert watch stop                   — stop the scheduled proposer\n"
            "  /expert example                      — print a starter rule"
        )

    def handle_why(self, args: str) -> str:
        """Dispatch ``/why <fact_type> [field=value ...]``."""
        ft, fields = _parse_pattern_args(args)
        return self.explain(ft, **fields)

    def handle_prove(self, args: str) -> str:
        """Dispatch ``/prove <fact_type> [field=value ...]``."""
        ft, fields = _parse_pattern_args(args)
        return self.prove(ft, **fields)


# ---------------------------------------------------------------------------
# Callable convenience (used by app.py handlers)
# ---------------------------------------------------------------------------


def dispatch(command_text: str, working_dir: Path | str) -> str:
    """Top-level dispatcher for the three slash commands.

    Args:
        command_text: Raw input including leading slash, e.g.
            ``"/expert on"`` or ``"/why tool_call name=shell"``.
        working_dir: Project root.

    Returns:
        Plain text to render in the TUI.
    """
    controller = get_controller(working_dir)
    text = command_text.strip()
    if text.startswith("/expert"):
        return controller.handle_expert(text[len("/expert"):].strip())
    if text.startswith("/why"):
        return controller.handle_why(text[len("/why"):].strip())
    if text.startswith("/prove"):
        return controller.handle_prove(text[len("/prove"):].strip())
    return f"Unknown expert command: {text}"


__all__ = [
    "ExpertController",
    "dispatch",
    "get_controller",
    "reset_controllers",
]
