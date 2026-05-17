"""Stand-alone controller for the ``/expert``, ``/why``, ``/prove`` commands.

Backs the ``/expert``, ``/expert trace``, ``/why``, and ``/prove`` slash
commands. All user-facing logic lives here so the TUI handlers in ``app.py`` are
trivially thin (4-6 lines each) and the feature is testable without the
Textual app. One :class:`ExpertController` instance is created per
working directory and cached in :data:`_CONTROLLERS`.

The controller owns one :class:`ExpertRulesMiddleware` instance, so facts
asserted by tool calls and facts asserted by the user via ``/expert assert``
share the same working memory.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Callable
from pathlib import Path
from typing import Any

from bog_agents.middleware.expert_engine import (
    AuthoringProposal,
    Fact,
    Pattern,
    Predicate,
    PredicateOp,
    build_proposal as build_authoring_proposal,
    lint as lint_rules,
    menu_text as wizard_menu_text,
    render_proposal as render_authoring_proposal,
    render_report as render_lint_report,
    run_wizard as run_wizard_step,
    save_proposal as save_authoring_proposal,
)
from bog_agents.middleware.expert_engine.backward import render_tree
from bog_agents.middleware.expert_rules import ExpertRulesMiddleware

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

    Args:
        working_dir: Project root (rules are loaded from
            ``<working_dir>/.bog-agents/expert_rules/``).
        middleware: Optional preconstructed middleware. Tests use this
            to inject programmatic rules.
        model_factory: Zero-arg callable returning a fresh chat model.
            Required for ``/expert write`` (LLM-driven rule authoring,
            REVIEW.md T-11 v2 #4). When None, the write flow refuses
            cleanly with an actionable error.
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
        # ``/expert write save [name]`` can commit it. Cleared on save
        # or on a new ``/expert write <intent>`` call.
        self._pending_proposal: AuthoringProposal | None = None
        # Optional async callback fired by ``/expert watch`` whenever
        # the scheduled proposer completes one run. The TUI handler
        # registers this via :meth:`set_watch_summary_callback` so the
        # user sees a Textual notification when a proposal lands,
        # without having to /expert watch (status) every few minutes.
        self._on_watch_summary: Any | None = None

    # ------------------------------------------------------------------
    # Used by app.py to register the middleware with create_agent
    # ------------------------------------------------------------------

    @property
    def middleware(self) -> ExpertRulesMiddleware:
        """The underlying middleware (exposed for ``create_agent`` registration)."""
        return self._middleware

    # ------------------------------------------------------------------
    # Command surface (each returns formatted text)
    # ------------------------------------------------------------------

    def status(self) -> str:
        """Human-readable status — used by ``/expert`` with no subcommand."""
        rule_count = len(self._middleware.engine.rules)
        counters = self._middleware.counters
        state = "ON" if self._middleware.enabled else "OFF"
        rules_dir = self._working_dir / ".bog-agents" / "expert_rules"
        lines = [
            f"Expert mode: {state}",
            f"Rules loaded: {rule_count} (from {rules_dir})",
            f"Denials: {counters['denials']}  "
            f"Modifications: {counters['modifications']}  "
            f"Approvals: {counters['approvals']}",
        ]
        if rule_count > 0:
            lines.append("")
            lines.append("Loaded rules (in declaration order):")
            for rule in self._middleware.engine.rules:
                src = Path(rule.source_file).name if rule.source_file else "<programmatic>"
                desc = f" — {rule.description}" if rule.description else ""
                lines.append(
                    f"  {rule.name}  [salience={rule.salience}]  ({src}){desc}"
                )
        else:
            lines.append("")
            lines.append(f"Create a rule file in {rules_dir} to get started.")
            lines.append("See /expert example for a template.")
        return "\n".join(lines)

    def set_enabled(self, on: bool) -> str:
        """Toggle the engine on/off."""
        self._middleware.set_enabled(on)
        return f"Expert mode: {'ON' if on else 'OFF'}"

    def reload(self) -> str:
        """Force a rule reload from disk."""
        count, err = self._middleware.reload()
        if err:
            return f"Reload completed with errors:\n  {err}\n\n(kept the previous {count} rules live)"
        return f"Reloaded {count} rule(s) from disk."

    def list_rules(self) -> str:
        """List every loaded rule's name + summary."""
        rules = self._middleware.engine.rules
        if not rules:
            return "No rules loaded. Drop a YAML file into .bog-agents/expert_rules/."
        lines = [f"{len(rules)} rule(s) loaded:"]
        for rule in rules:
            src = Path(rule.source_file).name if rule.source_file else "<programmatic>"
            desc = f" — {rule.description}" if rule.description else ""
            lines.append(
                f"  • {rule.name}  [salience={rule.salience}, once={rule.once}]  ({src}){desc}"
            )
        return "\n".join(lines)

    def show_rule(self, name: str) -> str:
        """Show one rule's source file path + summary."""
        if not name:
            return "Usage: /expert show <rule-name>"
        match = next(
            (r for r in self._middleware.engine.rules if r.name == name),
            None,
        )
        if match is None:
            return f"Rule '{name}' not found. Use /expert list."
        lines = [
            f"Rule: {match.name}",
            f"Source: {match.source_file or '<programmatic>'}",
            f"Salience: {match.salience}  Once: {match.once}",
        ]
        if match.description:
            lines.append(f"Description: {match.description}")
        lines.append("")
        lines.append(f"When ({len(match.when)} pattern(s)):")
        for pat in match.when:
            preds = ", ".join(
                f"{p.field}.{p.op.value}={p.value!r}" for p in pat.predicates
            ) or "(no predicates)"
            neg = " NOT" if pat.negated else ""
            bind = f" $bind={pat.bind}" if pat.bind else ""
            lines.append(f"  -{neg} {pat.fact_type}({preds}){bind}")
        lines.append("")
        lines.append(f"Then ({len(match.then)} action(s)):")
        for act in match.then:
            params = ", ".join(f"{k}={v!r}" for k, v in act.params.items())
            lines.append(f"  - {act.kind.value}({params})")
        return "\n".join(lines)

    def trace(self, limit: int = 50) -> str:
        """Render the last engine run trace (up to *limit* entries)."""
        entries = self._middleware.last_trace()
        if not entries:
            return "No trace available — no tool call has run through the engine yet."
        lines = [f"Last engine trace ({len(entries)} entries):"]
        for e in entries[-limit:]:
            stamp = f"[{e['kind']}]"
            rule = f" {e['rule']}" if e["rule"] else ""
            detail = f" — {e['detail']}" if e["detail"] else ""
            lines.append(f"  {stamp}{rule}{detail}")
        return "\n".join(lines)

    def explain(self, fact_type: str, **fields: Any) -> str:
        """``/why <fact_type> [k=v ...]`` — render the proof tree."""
        if not fact_type:
            return "Usage: /why <fact_type> [field=value ...]"
        chainer = self._middleware.make_backward_chainer()
        pattern = _pattern_from_kv(fact_type, fields)
        tree = chainer.why(pattern)
        return render_tree(tree)

    def prove(self, fact_type: str, **fields: Any) -> str:
        """``/prove <fact_type> [k=v ...]`` — render the proof tree."""
        if not fact_type:
            return "Usage: /prove <fact_type> [field=value ...]"
        chainer = self._middleware.make_backward_chainer()
        pattern = _pattern_from_kv(fact_type, fields)
        tree = chainer.prove(pattern)
        return render_tree(tree)

    def assert_fact(self, fact_type: str, **fields: Any) -> str:
        """Inject a fact into working memory (debug / demo).

        Used by ``/expert assert <fact_type> k=v ...`` so users can drive
        the engine without an actual tool call.
        """
        fact = self._middleware.engine.assert_fact(
            Fact(fact_type=fact_type, data=dict(fields))
        )
        return f"Asserted {fact_type}#{fact.id}: {dict(fields)}"

    def run(self) -> str:
        """Run the engine to a fixed point against the current memory."""
        result = self._middleware.engine.run()
        lines = [
            f"Engine ran {result.iterations} iteration(s)"
            f"{' (truncated)' if result.truncated else ''}.",
            f"Activations fired: {len(result.activations)}",
            f"Denied: {result.denied}",
        ]
        if result.deny_reasons:
            lines.append(f"Deny reasons: {result.deny_reasons}")
        return "\n".join(lines)

    @staticmethod
    def example() -> str:
        """Print a starter rule YAML."""
        return _EXAMPLE_RULE

    def lint(self) -> str:
        """Run the rulebook linter and render findings as text."""
        report = lint_rules(self._middleware.engine.rules)
        return render_lint_report(report)

    def write(self, intent: str) -> str:
        """``/expert write <intent>`` — LLM-driven rule authoring (T-11 v2 #4).

        Generates YAML implementing *intent*, validates + lints it,
        replays against the session's recent tool_call history so the
        user sees what the rule would have done. Stashes the proposal
        on the controller; the user follows up with
        ``/expert write save [filename]`` to commit it.
        """
        if not intent.strip():
            return (
                "Usage: /expert write <your policy in plain English>\n"
                "Example: /expert write block force-push to main"
            )
        if self._model_factory is None:
            return (
                "Cannot author rules: no model factory configured. "
                "The CLI normally supplies one — this controller was "
                "constructed without model_factory= (test / programmatic "
                "use). Set model_factory= on ExpertController."
            )
        try:
            model = self._model_factory()
        except Exception as exc:
            return f"Could not build authoring model: {exc}"

        history = self._middleware.tool_call_history
        proposal = build_authoring_proposal(intent, model=model, history=history)
        self._pending_proposal = proposal
        return render_authoring_proposal(proposal)

    def write_save(self, filename: str = "") -> str:
        """``/expert write save [filename]`` — commit the pending proposal.

        Writes the stashed proposal to disk under
        ``<cwd>/.bog-agents/expert_rules/`` and triggers a reload so
        the rule is live in the same session.
        """
        if self._pending_proposal is None:
            return (
                "No pending proposal — run /expert write <intent> first."
            )
        if not self._pending_proposal.ok_to_save:
            return (
                "Pending proposal has errors; fix them and rerun "
                "/expert write <intent>."
            )
        rules_dir = self._working_dir / ".bog-agents" / "expert_rules"
        try:
            written = save_authoring_proposal(
                self._pending_proposal,
                rules_dir=rules_dir,
                filename=filename or None,
            )
        except ValueError as exc:
            return f"Save failed: {exc}"
        # Clear the stash and hot-reload so the new rule is live.
        self._pending_proposal = None
        count, err = self._middleware.reload()
        line = f"Saved {written} ({count} rule(s) now active)"
        if err:
            line += f"\nReload reported: {err}"
        return line

    def discard_proposal(self) -> str:
        """``/expert write cancel`` — drop the pending proposal without saving."""
        if self._pending_proposal is None:
            return "No pending proposal to discard."
        self._pending_proposal = None
        return "Discarded pending proposal."

    # ------------------------------------------------------------------
    # Dreamscape → proposals (Wave E)
    # ------------------------------------------------------------------

    def _proposals_dir(self) -> Path:
        return self._working_dir / ".bog-agents" / "expert_rules" / "proposals"

    def _rules_dir(self) -> Path:
        return self._working_dir / ".bog-agents" / "expert_rules"

    def propose_from_dreamscape(
        self,
        agent_id: str = "default",
        *,
        auto_activate: bool = False,
    ) -> str:
        """``/expert propose [agent] [--apply]`` — mine dreams + tool history → propose rules.

        Args:
            agent_id: Dreamscape agent id.
            auto_activate: When True, write the proposed rule straight
                to the active rules directory and hot-reload the engine
                so it fires on the next tool call. Use only when the
                user has explicitly opted in via ``--apply``. Default
                False keeps the safer staged-then-approve pattern.
        """
        if self._model_factory is None:
            return (
                "Cannot propose rules: no model factory configured. "
                "Pass model_factory= to ExpertController."
            )
        try:
            model = self._model_factory()
        except Exception as exc:
            return f"Could not build proposer model: {exc}"

        from bog_agents_cli.dreamscape.rule_proposer import (
            propose_rules as _propose,
        )

        run = _propose(
            agent_id=agent_id or "default",
            model=model,
            tool_history=self._middleware.tool_call_history,
            existing_rules=[r.name for r in self._middleware.engine.rules],
            proposals_dir=self._proposals_dir(),
            rules_dir=self._rules_dir(),
            save=True,
            auto_activate=auto_activate,
        )
        if run.error and run.proposal is None:
            return f"Propose failed: {run.error}"
        if run.skipped:
            return (
                "Dreamscape proposer found no patterns worth codifying as rules. "
                f"({run.error or 'evidence not actionable'}) — try again after more activity."
            )
        if run.saved_path is None and run.proposal is not None:
            # The model produced something the lint or parse rejected.
            yaml_preview = run.proposal.yaml[:400]
            return (
                "Propose generated a rule that failed validation:\n"
                f"  {run.error}\n\n"
                "Model output (first 400 chars):\n"
                f"{yaml_preview}"
            )
        if run.active:
            count, err = self._middleware.reload()
            lines = [
                f"⚡ Auto-activated rule: {run.saved_path.name}",
                f"  → wrote to {run.saved_path}",
                f"  → {count} rule(s) now active",
            ]
            if err:
                lines.append(f"  → reload warning: {err}")
            lines.append(
                f"  → revert by removing {run.saved_path.name} and running /expert reload"
            )
            return "\n".join(lines)
        return (
            f"Saved proposal: {run.saved_path.name}\n"
            f"  → review with /expert proposals\n"
            f"  → approve with /expert proposals approve {run.saved_path.name}\n"
            f"  → or skip staging next time with: /expert propose --apply"
        )

    def list_proposals(self) -> str:
        """``/expert proposals`` — list the YAML proposals awaiting review."""
        from bog_agents_cli.dreamscape.rule_proposer import (
            render_proposals_list,
        )

        return render_proposals_list(self._proposals_dir())

    def approve_proposal_file(self, name: str) -> str:
        """``/expert proposals approve <name>`` — promote a proposal to active rules."""
        if not name:
            return (
                "Usage: /expert proposals approve <filename>"
            )
        from bog_agents_cli.dreamscape.rule_proposer import approve_proposal

        try:
            target = approve_proposal(
                proposals_dir=self._proposals_dir(),
                rules_dir=self._rules_dir(),
                name=name,
            )
        except ValueError as exc:
            return f"Approve failed: {exc}"
        count, err = self._middleware.reload()
        line = f"Approved {target.name} → {target} ({count} rule(s) active)"
        if err:
            line += f"\nReload reported: {err}"
        return line

    def wizard(self, args: str) -> str:
        """``/expert wizard [<category> [intent]]`` — guided rule-author flow.

        With no args, prints the category menu. With a category and an
        intent, runs the wizard step (category framing + intent → LLM
        → AuthoringProposal) and stashes the result on
        :attr:`_pending_proposal` so the user can ``/expert write save``
        it just like a normal ``/expert write`` proposal.
        """
        args = args.strip()
        if not args:
            return wizard_menu_text()
        head, _, rest = args.partition(" ")
        category_key = head.lower()
        intent = rest.strip()
        # No model needed for the menu / empty-intent help paths.
        if not intent:
            run = run_wizard_step(
                category_key=category_key,
                intent="",
                model=_NullModel(),  # never called when intent is empty
            )
            return run.error or wizard_menu_text()
        if self._model_factory is None:
            return (
                "Cannot run wizard: no model factory configured. "
                "Pass model_factory= to ExpertController."
            )
        try:
            model = self._model_factory()
        except Exception as exc:
            return f"Could not build wizard model: {exc}"
        history = self._middleware.tool_call_history
        run = run_wizard_step(
            category_key=category_key,
            intent=intent,
            model=model,
            history=history,
        )
        if run.error:
            return run.error
        if run.proposal is None:
            return f"Wizard returned no proposal for category {category_key!r}."
        self._pending_proposal = run.proposal
        lines = [
            f"== Wizard ({run.category.title if run.category else category_key}) ==",
            f"Intent: {run.proposal.intent[:200]}",
            "",
            render_authoring_proposal(run.proposal),
        ]
        return "\n".join(lines)

    def discard_proposal_file(self, name: str) -> str:
        """``/expert proposals discard <name>`` — delete a pending proposal."""
        if not name:
            return "Usage: /expert proposals discard <filename>"
        from bog_agents_cli.dreamscape.rule_proposer import (
            discard_proposal as _discard,
        )

        try:
            target = _discard(proposals_dir=self._proposals_dir(), name=name)
        except ValueError as exc:
            return f"Discard failed: {exc}"
        return f"Discarded proposal {target.name}"

    def dry_run(self, fact_type: str, **fields: Any) -> str:
        """Assert a fact, run the engine, then retract — show what would happen.

        Unlike ``/expert assert`` + ``/expert run``, the asserted fact is
        rolled back at the end so working memory remains untouched. The
        ``denials`` / ``modifications`` / ``approvals`` counters are NOT
        bumped (we restore them to their pre-call values). This is the
        right command for "would my rule fire against this kind of call?"
        without polluting later traces.
        """
        if not fact_type:
            return "Usage: /expert dry-run <fact_type> [field=value ...]"
        engine = self._middleware.engine
        before_counters = dict(self._middleware.counters)
        asserted = engine.assert_fact(Fact(fact_type=fact_type, data=dict(fields)))
        try:
            result = engine.run()
        finally:
            engine.retract(asserted.id)
            # Restore counters so dry-run doesn't pollute the session view.
            self._middleware._denials = before_counters["denials"]
            self._middleware._modifications = before_counters["modifications"]
            self._middleware._approvals = before_counters["approvals"]
        lines = [
            f"Dry-run: asserted {fact_type}#{asserted.id} {dict(fields)!r}",
            f"  Iterations: {result.iterations}"
            f"{' (truncated)' if result.truncated else ''}",
            f"  Activations fired: {len(result.activations)}"
            + (
                f" — {', '.join(a.rule.name for a in result.activations)}"
                if result.activations
                else ""
            ),
            f"  Denied: {result.denied}",
        ]
        if result.deny_reasons:
            lines.append(f"  Deny reasons: {result.deny_reasons}")
        if result.actions.modifications:
            lines.append(f"  Modifications: {result.actions.modifications}")
        if result.actions.approvals_required:
            lines.append(f"  Approvals required: {result.actions.approvals_required}")
        lines.append("(fact retracted; counters restored)")
        return "\n".join(lines)

    def memory_stats(self) -> str:
        """Show working-memory contents (by fact type)."""
        stats = self._middleware.engine.memory.stats()
        if not stats:
            return "Working memory is empty."
        lines = ["Working memory:"]
        for ft, n in sorted(stats.items()):
            lines.append(f"  {ft}: {n} fact(s)")
        return "\n".join(lines)

    def clear_memory(self) -> str:
        """Wipe working memory. Counters and rules stay."""
        self._middleware.engine.memory.clear()
        return "Cleared working memory."

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
            return self._dispatch_write(rest)
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
            return self._dispatch_watch(rest)
        if sub == "proposals":
            return self._dispatch_proposals(rest)
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

    def _dispatch_write(self, rest: str) -> str:
        """Handle the ``write`` sub-tree: ``write <intent>``, ``write save [name]``, ``write cancel``."""
        rest = rest.strip()
        if not rest:
            return self.write("")
        head, _, tail = rest.partition(" ")
        head = head.lower()
        if head == "save":
            return self.write_save(tail.strip())
        if head in ("cancel", "discard"):
            return self.discard_proposal()
        return self.write(rest)

    def _dispatch_watch(self, rest: str) -> str:
        """Handle ``watch``, ``watch start [interval] [--apply]``, ``watch stop``."""
        from bog_agents_cli import expert_watch

        rest = rest.strip()
        if not rest or rest.lower() == "status":
            return expert_watch.status(self._working_dir)
        head, _, tail = rest.partition(" ")
        head = head.lower()
        if head == "stop":
            return self._dispatch_watch_stop()
        if head == "start":
            return self._dispatch_watch_start(tail)
        return (
            "Usage: /expert watch [status | start [interval-seconds] [--apply] | stop]"
        )

    def set_watch_summary_callback(self, fn: Any | None) -> None:  # noqa: ANN401 — Optional[Callable[[str], Awaitable[None]]]
        """Register an async callback fired after every watcher run.

        Used by the TUI's expert handler to surface a Textual
        notification when ``/expert watch`` produces a new proposal.
        Pass ``None`` to clear.
        """
        self._on_watch_summary = fn

    def _dispatch_watch_start(self, rest: str) -> str:
        from bog_agents_cli import expert_watch

        tokens = rest.split()
        auto = False
        interval = None
        for tok in tokens:
            if tok in ("--apply", "--auto", "--activate"):
                auto = True
            else:
                try:
                    interval = float(tok)
                except ValueError:
                    return f"Invalid interval-seconds: {tok!r}"
        if interval is None:
            interval = expert_watch._DEFAULT_INTERVAL_SECONDS
        _started, message = expert_watch.start(
            working_dir=self._working_dir,
            propose=self.propose_from_dreamscape,
            interval_seconds=interval,
            auto_activate=auto,
            on_summary=self._on_watch_summary,
        )
        return message

    def _dispatch_watch_stop(self) -> str:
        import asyncio

        from bog_agents_cli import expert_watch

        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            return "No running event loop — can't stop watcher cleanly."
        # The stop() coroutine is short; we run it to completion here
        # because the slash dispatcher is synchronous from the
        # controller's point of view.
        coro = expert_watch.stop(self._working_dir)
        if loop.is_running():
            # Stop is called from app.py via to_thread, so the loop
            # is running. Use run_coroutine_threadsafe.
            fut = asyncio.run_coroutine_threadsafe(coro, loop)
            stopped, message = fut.result(timeout=5)
        else:
            stopped, message = loop.run_until_complete(coro)
        _ = stopped
        return message

    def _dispatch_proposals(self, rest: str) -> str:
        """Handle ``proposals``, ``proposals approve <name>``, ``proposals discard <name>``."""
        rest = rest.strip()
        if not rest:
            return self.list_proposals()
        head, _, tail = rest.partition(" ")
        head = head.lower()
        if head == "approve":
            return self.approve_proposal_file(tail.strip())
        if head in ("discard", "delete", "reject"):
            return self.discard_proposal_file(tail.strip())
        return (
            "Usage: /expert proposals [approve <name> | discard <name>]"
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
# Placeholders + parsing helpers
# ---------------------------------------------------------------------------


class _NullModel:
    """Placeholder for the wizard's "no-intent → help" path.

    ``run_wizard`` only calls ``model.invoke`` when there's an intent
    to author; the no-intent help branch returns formatted text without
    going to the LLM. We hand this stub in so the signature stays
    consistent and tests don't have to mock a real model just to print
    the category help.
    """

    def invoke(self, _messages: list) -> Any:  # noqa: ANN401, PLR6301
        msg = "_NullModel.invoke should never be called"
        raise AssertionError(msg)


def _split_subcommand(text: str) -> tuple[str, str]:
    """Split ``"on rest of args"`` into ``("on", "rest of args")``."""
    text = text.strip()
    if not text:
        return ("", "")
    parts = text.split(None, 1)
    if len(parts) == 1:
        return (parts[0].lower(), "")
    return (parts[0].lower(), parts[1])


def _parse_pattern_args(text: str) -> tuple[str, dict[str, Any]]:
    """Parse ``"fact_type k1=v1 k2=v2"`` into ``("fact_type", {k1: v1, k2: v2})``.

    Values that look like JSON literals (``true``, ``false``, ``null``,
    numbers, or quoted strings) are decoded via :func:`json.loads`; anything
    else stays a string. ``shlex`` handles quoted multi-word values.
    """
    if not text.strip():
        return ("", {})
    try:
        tokens = shlex.split(text)
    except ValueError:
        return (text.strip(), {})
    if not tokens:
        return ("", {})
    fact_type = tokens[0]
    fields: dict[str, Any] = {}
    for tok in tokens[1:]:
        if "=" not in tok:
            continue
        key, _, raw = tok.partition("=")
        fields[key] = _coerce_value(raw)
    return (fact_type, fields)


def _coerce_value(raw: str) -> Any:  # noqa: ANN401 — CLI values are intentionally untyped
    """Best-effort JSON-ish coercion of a CLI value."""
    if not raw:
        return ""
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


def _pattern_from_kv(fact_type: str, fields: dict[str, Any]) -> Pattern:
    """Build an equality :class:`Pattern` from keyword-arg fields."""
    preds = tuple(Predicate(field=k, op=PredicateOp.EQ, value=v) for k, v in fields.items())
    return Pattern(fact_type=fact_type, predicates=preds)


# ---------------------------------------------------------------------------
# Built-in starter rule (printed by ``/expert example``)
# ---------------------------------------------------------------------------


_EXAMPLE_RULE = """# Example rule — save to .bog-agents/expert_rules/example.yaml,
# then run /expert reload.

- name: block_force_push_to_main
  description: Block force-pushes to main/master.
  salience: 100
  when:
    - tool_call:
        name: shell_execute
        command:
          matches: 'git push.*--force.*(main|master)'
  then:
    - deny: "Force-push to main is prohibited by policy."
    - audit_log:
        event: prod_force_push_blocked

- name: budget_brake
  description: Brake on session spend > $5.
  salience: 90
  when:
    - session:
        cost_usd:
          gt: 5.0
  then:
    - require_approval:
        gate: "Cost exceeded $5.00 — continue?"
        risk: high
"""


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


# Re-exported for type-checkers and downstream users:
__all__ = [
    "ExpertController",
    "dispatch",
    "get_controller",
    "reset_controllers",
]


# Silence "unused" import lint for Callable in the type hints above
_ = Callable
