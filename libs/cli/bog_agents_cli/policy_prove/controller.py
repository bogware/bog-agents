"""``/prove-invariant`` slash-command controller (Q1).

Wraps :mod:`bog_agents_cli.policy_prove.invariant` +
:mod:`bog_agents_cli.policy_prove.prover` for the TUI. Pure text in,
pure text out — the actual proof runs on a worker thread from
``app.py``.

Input grammar
-------------

The slash command accepts either:

1. **Inline YAML** — everything after the command name is treated as
   a YAML document::

       /prove-invariant
       name: no_force_push
       precondition:
         fact_type: tool_call
         predicates: [{field: name, op: eq, value: shell_execute}]
       forbidden:
         fact_type: tool_call
         predicates: [{field: command, op: matches, value: 'git push.*--force'}]

2. **File reference** — when the first non-blank token is a path,
   load the invariant from that file::

       /prove-invariant invariants/no-force-push.yaml

3. **Sub-command** — ``/prove-invariant list`` enumerates the bundled
   example invariants under ``invariants/`` (when present).
"""

from __future__ import annotations

import logging
from pathlib import Path

from bog_agents_cli.expert_controller import get_controller as _expert_controller
from bog_agents_cli.policy_prove.invariant import (
    Invariant,
    InvariantParseError,
    load_invariant_from_yaml,
)
from bog_agents_cli.policy_prove.prover import (
    InvariantProof,
    ProofVerdict,
    prove,
)

logger = logging.getLogger(__name__)


def dispatch(command_text: str, working_dir: Path | str) -> str:
    """Top-level entry point — called from the TUI handler."""
    text = command_text.strip()
    if text.startswith("/prove-invariant"):
        text = text[len("/prove-invariant") :].strip()
    if not text:
        return _help_text()
    head = text.split(None, 1)[0].lower()
    if head in ("help", "?"):
        return _help_text()
    if head in ("list", "ls"):
        return _list_examples(Path(working_dir))

    # Wave V removed the --z3 flag — strip it silently from older
    # invocations so we don't break user muscle memory, but ignore
    # it; the heuristic prover is the only backend now.
    body = text
    for flag in ("--z3", "-z3"):
        if f" {flag}" in f" {body} ":
            body = body.replace(flag, "", 1).strip()
    try:
        invariant = _load_invariant_from_input(body, Path(working_dir))
    except InvariantParseError as exc:
        return f"Could not parse invariant: {exc}"

    try:
        rules = list(_expert_controller(working_dir).middleware.engine.rules)
    except Exception as exc:
        logger.exception("Could not load rules for /prove-invariant")
        return f"Could not load active rules: {exc}"

    proof = prove(invariant, rules)
    return render_proof(proof)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


_VERDICT_ICON: dict[ProofVerdict, str] = {
    ProofVerdict.HOLDS: "✓",
    ProofVerdict.COUNTEREXAMPLE: "✗",
    ProofVerdict.INCONCLUSIVE: "?",
}


def render_proof(proof: InvariantProof) -> str:
    """Format an :class:`InvariantProof` for the TUI."""
    icon = _VERDICT_ICON[proof.verdict]
    lines = [
        f"== Invariant {proof.invariant.name} ==",
        f"  {proof.invariant.description or '(no description)'}",
        "",
        f"Verdict: {icon} {proof.verdict.value.upper()}",
        proof.rationale,
    ]
    if proof.guards:
        lines.append("")
        lines.append("Guard rules that block the forbidden pattern:")
        for name in proof.guards:
            lines.append(f"  - {name}")
    if proof.counterexample:
        lines.append("")
        lines.append("Counterexample:")
        for c_line in proof.counterexample.splitlines():
            lines.append(f"  {c_line}")
    if proof.notes:
        lines.append("")
        lines.append("Notes:")
        for note in proof.notes:
            lines.append(f"  · {note}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_invariant_from_input(body: str, working_dir: Path) -> Invariant:
    """Try the input as a file path first, then as inline YAML."""
    body = body.strip()
    if not body:
        msg = "Empty invariant body. Provide YAML inline or a file path."
        raise InvariantParseError(msg)
    candidate = (
        (working_dir / body).resolve() if not Path(body).is_absolute() else Path(body)
    )
    if candidate.is_file() and candidate.suffix in (".yaml", ".yml"):
        return load_invariant_from_yaml(candidate)
    # Treat as YAML body. If it parses, we accept it.
    return load_invariant_from_yaml(body)


def _list_examples(working_dir: Path) -> str:
    """List ``<cwd>/invariants/*.yaml`` files."""
    invariants_dir = working_dir / "invariants"
    if not invariants_dir.is_dir():
        return (
            "No invariants/ directory under this project.\n"
            "Create one and drop YAML files in to share invariants "
            "with the team."
        )
    files = sorted(invariants_dir.glob("*.yaml")) + sorted(invariants_dir.glob("*.yml"))
    if not files:
        return f"No invariant files found under {invariants_dir}."
    lines = [f"{len(files)} invariant file(s) under {invariants_dir.name}/:", ""]
    for path in files:
        try:
            invariant = load_invariant_from_yaml(path)
            lines.append(f"  {path.name:<40}  {invariant.header()}")
        except InvariantParseError as exc:
            lines.append(f"  {path.name:<40}  [parse error: {exc}]")
    lines.append("")
    lines.append("Run:  /prove-invariant invariants/<file>.yaml")
    return "\n".join(lines)


def _help_text() -> str:
    return (
        "/prove-invariant — formally prove a safety invariant against\n"
        "                  the loaded expert rules.\n\n"
        "Usage:\n"
        "  /prove-invariant <path-to-yaml>         — prove an invariant from a file\n"
        "  /prove-invariant\\n<inline yaml>          — inline (newline + YAML body)\n"
        "  /prove-invariant list                   — list invariants/*.yaml in cwd\n"
        "  /prove-invariant help                   — this message\n\n"
        "Flags:\n"
        "Invariant YAML shape:\n"
        "  name: <slug>\n"
        "  description: <one line>\n"
        "  precondition:\n"
        "    fact_type: <type>\n"
        "    predicates: [{field: ..., op: ..., value: ...}, ...]\n"
        "  forbidden:\n"
        "    fact_type: <type>\n"
        "    predicates: [...]\n"
    )


__all__ = [
    "dispatch",
    "render_proof",
]
