"""`/squad` — multi-persona dialogue review.

A *squad* is a configured set of personas, each with a system prompt
that biases their voice (security focus, performance focus, clarity
focus, etc.). When the user runs ``/squad review`` against a file or
code block, each persona gives a focused review; a final synthesiser
pass collates the findings into a single ranked report.

Configuration lives at ``~/.bog-agents/squad.toml``. ``/squad init``
seeds a sensible default; ``/squad list`` shows the active roster.
Personas are stored in a flat TOML so they're trivially editable.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import tomli_w

from bog_agents_cli.feature_helpers import (
    collect_transcript,
    feature_state_dir,
    invoke_model,
    resolve_active_model_spec,
)

logger = logging.getLogger(__name__)


_SQUAD_CONFIG_NAME = "squad.toml"
# Personas use capitalised keys so the on-disk and in-memory views
# match — load_squad keeps the raw TOML key as the display name, and
# users reading squad.toml expect "Alice / Bob / Carol", not lowercase.
_DEFAULT_SQUAD: dict[str, str] = {
    "Alice": (
        "You are Alice, a senior security engineer. Review the code or "
        "design from a security and data-handling perspective. Flag: "
        "input handling, authn/authz gaps, secret exposure, injection "
        "paths, audit-log absence, error-message leakage. Concrete "
        "examples only. Skip topics outside security."
    ),
    "Bob": (
        "You are Bob, a performance and reliability engineer. Review "
        "from the angle of cost-to-run and failure modes. Flag: hot "
        "loops, N+1 queries, allocation patterns, blocking calls in "
        "async paths, retry storms, missing timeouts. Concrete only. "
        "Skip topics outside perf / reliability."
    ),
    "Carol": (
        "You are Carol, a code-clarity reviewer. Review from a future-"
        "maintainer's point of view. Flag: unclear naming, comments "
        "that lie, leaky abstractions, magic numbers, dead code, "
        "missing docstrings on public API. Skip stylistic nitpicks."
    ),
}


SQUAD_USER_TEMPLATE = """\
Review target:

{target}

{transcript_block}

Produce ONE response in this format:

**Headline** — One sentence: what's the most important thing the user
should know from YOUR persona's perspective?

**Findings** — Numbered list. Each item is: the issue, the file/line
or code excerpt if known, and your recommended fix in <=2 sentences.

**No-issue verdict** — If you genuinely have nothing in scope to flag,
say so in one sentence and stop. Do NOT pad. Other personas will
cover their domains.

Stay strictly inside your persona's scope.
"""


SQUAD_SYNTHESIS_PROMPT = """\
You are the squad moderator. You have just received reviews from N
personas, each with a different scope. Render a consolidated report.

Sections:

## Ranked findings
Numbered list, sorted by severity. Each entry: a one-line summary, the
persona who flagged it (in parentheses), and a 1-2 sentence
recommended fix. If two personas flagged related issues, merge them
into a single entry.

## Cross-cutting risks
Bullet list of issues that span multiple personas' scopes (e.g. a
performance fix that has security implications). Skip the section if
there are none.

## What looked clean
ONE sentence naming aspects every persona left alone — useful as a
positive signal. If reviews were too sparse to tell, say so.

Hard rules:
- Never invent findings not in the persona reports.
- Be ruthless about severity ranking.
- Total response under ~500 words.
"""


@dataclass
class SquadPersona:
    """One configured persona."""

    name: str
    system_prompt: str


@dataclass
class SquadReview:
    """One persona's review of the target."""

    persona: str
    body: str
    elapsed_seconds: float
    error: str = ""

    @property
    def ok(self) -> bool:
        """True when the persona produced a non-empty body without error."""
        return not self.error and bool(self.body)


@dataclass
class SquadResult:
    """Aggregated output of a /squad review."""

    target_excerpt: str
    reviews: list[SquadReview] = field(default_factory=list)
    synthesis: str = ""
    elapsed_seconds: float = 0.0

    def render(self) -> str:
        """Render the full result as Rich-markup ready for the chat surface."""
        lines = [
            f"[bold]Squad review — {len(self.reviews)} personas "
            f"({self.elapsed_seconds:.1f}s)[/bold]\n",
            f"**Target:**\n> {self.target_excerpt}\n",
            "---\n",
        ]
        for review in self.reviews:
            if review.ok:
                lines.append(
                    f"### {review.persona} "
                    f"[dim]({review.elapsed_seconds:.1f}s)[/dim]\n\n"
                    f"{review.body}\n"
                )
            else:
                lines.append(
                    f"### {review.persona} [red](failed)[/red]\n\n{review.error}\n"
                )
        if self.synthesis:
            lines.append("---\n")
            lines.append("## Moderator's synthesis\n")
            lines.append(self.synthesis)
        return "\n".join(lines)


def squad_config_path() -> Path:
    """Return ``~/.bog-agents/squad.toml`` (created on demand)."""
    return feature_state_dir() / _SQUAD_CONFIG_NAME


def load_squad(path: Path | None = None) -> list[SquadPersona]:
    """Load squad personas from disk, falling back to the built-in defaults.

    Returns the default Alice/Bob/Carol trio when the file doesn't
    exist or is malformed — never raises.
    """
    target = path or squad_config_path()
    if not target.exists():
        return _default_personas()
    try:
        data = tomllib.loads(target.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError):
        logger.warning("Failed to parse squad.toml; using defaults", exc_info=True)
        return _default_personas()
    personas_section = data.get("personas")
    if not isinstance(personas_section, dict):
        return _default_personas()
    out: list[SquadPersona] = []
    for name, spec in personas_section.items():
        if isinstance(spec, str) and spec.strip():
            out.append(SquadPersona(name=str(name), system_prompt=spec.strip()))
        elif isinstance(spec, dict):
            prompt = spec.get("system_prompt") or spec.get("prompt")
            if isinstance(prompt, str) and prompt.strip():
                out.append(SquadPersona(name=str(name), system_prompt=prompt.strip()))
    return out or _default_personas()


def _default_personas() -> list[SquadPersona]:
    """Return the built-in three-persona squad (Alice/Bob/Carol)."""
    return [
        SquadPersona(name=name, system_prompt=prompt)
        for name, prompt in _DEFAULT_SQUAD.items()
    ]


def write_default_squad(path: Path | None = None, *, overwrite: bool = False) -> Path:
    """Write the default Alice/Bob/Carol squad to ``squad.toml``.

    Args:
        path: Override target path.
        overwrite: When False (default), fails if the file already exists.

    Raises:
        FileExistsError: When the file exists and ``overwrite`` is False.
    """
    target = path or squad_config_path()
    if target.exists() and not overwrite:
        msg = f"{target} already exists — pass overwrite=True to replace it"
        raise FileExistsError(msg)
    payload = {
        "personas": {
            name: {"system_prompt": prompt} for name, prompt in _DEFAULT_SQUAD.items()
        }
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    with tmp.open("wb") as fh:
        tomli_w.dump(payload, fh)
    tmp.replace(target)
    return target


def resolve_target(app: object, raw_arg: str) -> str:
    """Resolve what the squad should review.

    Accepted forms:
      * ``""`` (just ``/squad review``) → the most recent assistant
        message in the conversation.
      * a file path → the file's contents.
      * any other string → treated as a literal code snippet / prompt.

    Returns:
        The text body to embed in the user-message prompt.

    Raises:
        ValueError: When the target can't be resolved (e.g. empty session).
    """
    cleaned = raw_arg.strip()
    if cleaned and cleaned not in {"review", "last"}:
        # Strip a leading ``review `` if present.
        if cleaned.startswith("review "):
            cleaned = cleaned[len("review ") :].strip()
        path = Path(cleaned)
        if path.exists() and path.is_file():
            try:
                return f"File: {path}\n\n```\n{path.read_text(encoding='utf-8')}\n```"
            except OSError as exc:
                msg = f"could not read {path}: {exc}"
                raise ValueError(msg) from exc
        # Otherwise treat as literal text.
        return cleaned

    transcript = collect_transcript(app, max_entries=20, max_chars=8_000)
    target = next(
        (entry.text for entry in reversed(transcript) if entry.role == "assistant"),
        "",
    )
    if not target.strip():
        msg = (
            "no review target — pass a file path, code snippet, or run "
            "after the agent has spoken so the last assistant message "
            "can be reviewed"
        )
        raise ValueError(msg)
    return target


async def run_squad(app: object, raw_arg: str) -> SquadResult:
    """End-to-end ``/squad review`` flow used by the app handler.

    Raises:
        RuntimeError: When no active model spec can be resolved.
            ``ValueError`` may also propagate from :func:`resolve_target`
            when no review target can be found.
    """
    from bog_agents_cli.config import create_model_with_fallback

    target = resolve_target(app, raw_arg)

    personas = load_squad()
    spec = resolve_active_model_spec(app)
    if not spec:
        msg = "no active model — run /model first or set a default"
        raise RuntimeError(msg)
    profile = getattr(app, "_profile_override", None)
    model_result = create_model_with_fallback(spec, profile_overrides=profile)
    model = model_result.model

    transcript = collect_transcript(app, max_entries=8, max_chars=3_000)
    transcript_block = ""
    if transcript:
        from bog_agents_cli.feature_helpers import transcript_to_markdown

        transcript_block = (
            "Recent conversation for context:\n\n"
            + transcript_to_markdown(transcript[-4:])
        )

    user_prompt = SQUAD_USER_TEMPLATE.format(
        target=target, transcript_block=transcript_block
    )

    start = time.monotonic()

    async def call_persona(p: SquadPersona) -> SquadReview:
        review_start = time.monotonic()
        try:
            body = await invoke_model(
                model, p.system_prompt, user_prompt, timeout_seconds=60.0
            )
            return SquadReview(
                persona=p.name,
                body=body,
                elapsed_seconds=time.monotonic() - review_start,
            )
        except Exception as exc:
            logger.warning("/squad persona %s failed", p.name, exc_info=True)
            return SquadReview(
                persona=p.name,
                body="",
                elapsed_seconds=time.monotonic() - review_start,
                error=str(exc),
            )

    reviews = await asyncio.gather(*(call_persona(p) for p in personas))
    ok_reviews = [r for r in reviews if r.ok]

    synthesis = ""
    if len(ok_reviews) >= 2:
        synth_body = "\n\n".join(f"### {r.persona}\n\n{r.body}" for r in ok_reviews)
        with contextlib.suppress(Exception):
            synthesis = await invoke_model(
                model,
                SQUAD_SYNTHESIS_PROMPT,
                f"Target:\n{target}\n\nReviews:\n\n{synth_body}",
                timeout_seconds=60.0,
            )

    excerpt = target.strip().replace("\n", " ")
    if len(excerpt) > 240:
        excerpt = excerpt[:239] + "…"

    return SquadResult(
        target_excerpt=excerpt,
        reviews=list(reviews),
        synthesis=synthesis,
        elapsed_seconds=time.monotonic() - start,
    )


# --------------------------------------------------------------------------- #
# App handler glue                                                            #
# --------------------------------------------------------------------------- #


async def handle_squad_subcommand(app: object, raw_arg: str) -> None:
    """Dispatch ``/squad <sub>`` subcommands.

    Subcommands:
        review [target]   Run the multi-persona review (default action).
        list              List configured personas.
        init              Write the default squad.toml if absent.
    """
    from bog_agents_cli.widgets.messages import AppMessage, ErrorMessage

    arg = raw_arg.strip()
    head, _, rest = arg.partition(" ")
    head = head.lower()
    rest = rest.strip()

    if head == "list":
        personas = load_squad()
        lines = [
            f"[bold]{len(personas)} configured personas[/bold] "
            f"([cyan]{squad_config_path()}[/cyan])\n"
        ]
        for p in personas:
            lines.append(f"### {p.name}")
            lines.append(p.system_prompt.split("\n")[0])
            lines.append("")
        await app._mount_message(AppMessage("\n".join(lines)))  # type: ignore[attr-defined]
        return

    if head == "init":
        try:
            written = write_default_squad()
        except FileExistsError:
            await app._mount_message(  # type: ignore[attr-defined]
                AppMessage(
                    f"[yellow]squad.toml already exists at "
                    f"{squad_config_path()} — leaving it alone.[/yellow]"
                )
            )
            return
        await app._mount_message(  # type: ignore[attr-defined]
            AppMessage(
                f"[bold]Default squad written to[/bold] [cyan]{written}[/cyan]\n"
                "Edit the file to customise personas, then run /squad review."
            )
        )
        return

    # Busy-guarding moved to the app (v6 CLI-3): the review runs inside a
    # TurnManager-tracked session, during which `_agent_running` is True.
    review_arg = arg
    if head == "review":
        review_arg = rest

    await app._set_spinner("Squad review")  # type: ignore[attr-defined]
    try:
        result = await run_squad(app, review_arg)
    except ValueError as exc:
        await app._set_spinner("")  # type: ignore[attr-defined]
        await app._mount_message(ErrorMessage(f"/squad: {exc}"))  # type: ignore[attr-defined]
        return
    finally:
        await app._set_spinner("")  # type: ignore[attr-defined]

    await app._mount_message(AppMessage(result.render()))  # type: ignore[attr-defined]
