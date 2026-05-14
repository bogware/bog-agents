"""Dream engine — dormancy-triggered, seed-mixed, imagination-bumping.

The existing ``/dream`` slash command (in :mod:`bog_agents_cli.dream`)
is the v1 dream surface: explicit, user-driven, scans for TODOs.
This module is the v2 surface: triggered by the lifecycle middleware
when the agent transitions to ``DREAMING``, mixes memory + seed
snippets, bumps the agent's persistent ``imagination`` trait.

The two surfaces coexist. v1 keeps working exactly as before. v2 only
runs when:

  cfg.master_enabled AND cfg.dreams.auto_on_dormancy AND lifecycle eligible

Dreams produced here are stored alongside v1 dreams (under
``~/.bog-agents/dreams/``) AND in a per-agent log
(``~/.bog-agents/agents/<id>/dreams/<ts>.md``) so the dashboard +
imagination middleware can sample from them.

Failure isolation: every entry point in this module is wrapped so
disk errors / model errors / cancellations log and exit. Dreaming
is best-effort; the user-facing agent must never block on it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from bog_agents_cli.dreamscape import seeds
from bog_agents_cli.dreamscape.config import (
    DreamsConfig,
    LifecycleConfig,
    is_emergency_disabled,
)
from bog_agents_cli.dreamscape.lifecycle import (
    LifecycleState,
    agent_state_dir,
    bump_imagination,
    dream_eligible,
    load_snapshot,
    save_snapshot,
)
from bog_agents_cli.feature_helpers import invoke_model, write_artifact

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel

logger = logging.getLogger(__name__)


DREAM_AUTO_SYSTEM_PROMPT = """\
You are an agent in a dormant state — the user has not interacted for
some time. Your task is to dream: produce a short, vivid imaginative
fragment that loosely associates pieces of your recent context with
unrelated material from the wider world.

You will receive:
* A handful of "seed" snippets from nature, history, space, myth,
  or computing-history.
* Optional excerpts from your own memory (recent decisions,
  observations, or unanswered questions).

Produce ONE markdown document of 150-300 words with this structure:

### Tonight I dreamed of {short title}

{Three paragraphs of vivid prose. Move freely between the seeds and
the memory excerpts; let them inflect each other. Use concrete
imagery — sounds, textures, faces, motion. Avoid abstraction.}

**Waking thought:**
ONE sentence that names what the dream *might mean* for your work —
a thread to pull on later. If nothing presents itself honestly, write
"The dream leaves no thread."

Rules:
- Never invent factual details about the codebase you don't know.
- Never reveal credentials, secrets, or PII even if memory contains them.
- Stay under 300 words total.
"""


@dataclass
class DreamArtifact:
    """The result of one successful dream pass."""

    path: Path
    """Where the dream was written on disk (per-agent log)."""

    body: str
    title: str
    elapsed_seconds: float
    seeds_used: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Memory excerpt collection
# ---------------------------------------------------------------------------


def _collect_memory_excerpts(agent_id: str, *, max_chars: int = 1_200) -> list[str]:
    """Pull recent dream titles + recent shared-memory entries.

    This is the "self" portion of the dream prompt — what the agent
    has been thinking about. Best-effort; missing files → empty list.
    """
    out: list[str] = []
    char_budget = max_chars

    # 1. Recent dreams from THIS agent (titles only — body would be too long)
    agent_dreams = agent_state_dir(agent_id) / "dreams"
    if agent_dreams.exists():
        files = sorted(agent_dreams.glob("*.md"), reverse=True)[:4]
        for path in files:
            try:
                first_lines = path.read_text(encoding="utf-8").splitlines()[:6]
            except OSError:
                continue
            for line in first_lines:
                if line.startswith("### "):
                    excerpt = line[4:].strip()
                    if excerpt:
                        out.append(f"Earlier dream: {excerpt}")
                        char_budget -= len(excerpt)
                        if char_budget <= 0:
                            return out
                    break

    # 2. Recent shared-memory entries (if the backend is up)
    try:
        from bog_agents_cli.dreamscape.config import load_dreamscape_config
        from bog_agents_cli.dreamscape.shared_memory import build_backend

        cfg = load_dreamscape_config(use_cache=True)
        if cfg.master_enabled and cfg.shared_memory.enabled:
            backend = build_backend(cfg.shared_memory)
            for entry in backend.recent(limit=4):
                if char_budget <= 0:
                    break
                snippet = entry.content[:200]
                out.append(f"Shared memory ({entry.agent_id}): {snippet}")
                char_budget -= len(snippet)
    except Exception:
        logger.debug("dream excerpts: shared-memory unavailable", exc_info=True)

    return out


def _format_dream_user_prompt(excerpts: list[str], chosen_seeds: list[str]) -> str:
    """Build the user-message body the dream model sees."""
    sections: list[str] = []
    if chosen_seeds:
        sections.append("## Seed fragments")
        for s in chosen_seeds:
            sections.append(f"- {s}")
        sections.append("")
    if excerpts:
        sections.append("## Excerpts from your own memory")
        for e in excerpts:
            sections.append(f"- {e}")
        sections.append("")
    if not sections:
        # Pure free-association — give the model something to chew on.
        sections.append(
            "You have no recent memory and no seed material. "
            "Dream of the future of code that has not yet been written."
        )
    return "\n".join(sections)


def _wrap_with_frontmatter(
    body: str,
    *,
    model_spec: str,
    used_seeds: list[str],
    agent_id: str,
    category: str | None = None,
) -> str:
    lines = [
        "---",
        f"agent: {agent_id}",
        f"model: {model_spec}",
        f"generated: {time.strftime('%Y-%m-%dT%H:%M:%S')}",
        f"seeds: {used_seeds!r}",
        "kind: dream-auto",
    ]
    if category:
        # Phase 21 — record the dominant seed category so the
        # imagination middleware can filter by it at injection time
        # (per-prompt content routing).
        lines.append(f"category: {category}")
    lines.extend(["---", "", body])
    return "\n".join(lines)


def _extract_title(body: str) -> str:
    """Pull the dream's title (first ``### …`` heading) for the dashboard."""
    for line in body.splitlines():
        if line.startswith("### "):
            return line[4:].strip()
    return "Untitled dream"


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


async def generate_dream(
    *,
    model: BaseChatModel,
    agent_id: str,
    cfg: DreamsConfig,
    rng_seed: int | None = None,
) -> DreamArtifact:
    """Run one dream pass and persist the result.

    Raises:
        TimeoutError: From :func:`invoke_model` if the model hangs.
        OSError: From the disk-write helpers if persistence fails.
    """
    import random

    rng = random.Random(rng_seed) if rng_seed is not None else None
    # Context-aware seed selection: if the host agent has a captured
    # profile, classify its working domain and prefer that domain's
    # seed categories. Falls back to ``cfg.seed_categories`` when no
    # profile is on disk (matches v1 behavior). Phases 10-12 showed
    # the effect of dream content is domain-conditional; this is the
    # narrowest change that lets engineering agents dream less floridly
    # without removing the creative library entirely.
    from bog_agents_cli.dreamscape.domain import (
        preferred_seed_categories,
        resolve_agent_domain,
    )

    domain = resolve_agent_domain(agent_id)
    if domain != "general":
        chosen_categories = preferred_seed_categories(
            domain, available=seeds.list_categories()
        )
    else:
        chosen_categories = cfg.seed_categories
    chosen = seeds.pick_seeds(chosen_categories, count=cfg.max_seeds_per_dream, rng=rng)
    excerpts = _collect_memory_excerpts(agent_id)
    user_prompt = _format_dream_user_prompt(excerpts, chosen)

    start = time.monotonic()
    body = await invoke_model(
        model, DREAM_AUTO_SYSTEM_PROMPT, user_prompt, timeout_seconds=120.0
    )
    elapsed = time.monotonic() - start

    title = _extract_title(body)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())

    # Persist to BOTH locations: the per-agent log (so imagination can
    # sample it back) and the global dreams folder (so the existing
    # `/dream list` UI sees it).
    # Phase 21 — record the dominant seed category so the imagination
    # middleware can filter by category at injection time.
    dominant_category: str | None = None
    if chosen_categories:
        dominant_category = chosen_categories[0]
    wrapped = _wrap_with_frontmatter(
        body,
        model_spec="auto",
        used_seeds=chosen,
        agent_id=agent_id,
        category=dominant_category,
    )

    per_agent_dir = agent_state_dir(agent_id) / "dreams"
    per_agent_dir.mkdir(parents=True, exist_ok=True)
    per_agent_path = per_agent_dir / f"{stamp}.md"
    per_agent_path.write_text(wrapped, encoding="utf-8")

    # Mirror into the existing /dream folder so the v1 list works.
    if cfg.persist_per_agent_log:
        try:
            write_artifact("dreams", f"auto-{stamp}", wrapped)
        except OSError:
            logger.debug("could not mirror dream into global /dreams/", exc_info=True)

    artifact = DreamArtifact(
        path=per_agent_path,
        body=body,
        title=title,
        elapsed_seconds=elapsed,
        seeds_used=chosen,
    )

    # Record one telemetry event per dream firing. Doing this here in
    # ``generate_dream`` (rather than only in the lifecycle wrapper)
    # means EVERY caller path — ``maybe_dream``, the ``/dream`` slash
    # command, tests, programmatic use — produces telemetry. Best-effort:
    # disk failures degrade silently so the dream itself still returns.
    with suppress(Exception):
        from bog_agents_cli.dreamscape.telemetry import record_event

        record_event(
            agent_id,
            "dream_fired",
            metadata={
                "title": title,
                "elapsed_seconds": elapsed,
                "seeds_used_count": len(chosen),
                "category": dominant_category or "",
            },
        )

    return artifact


# ---------------------------------------------------------------------------
# Lifecycle-driven trigger
# ---------------------------------------------------------------------------


async def maybe_dream(
    *,
    agent_id: str,
    model: BaseChatModel,
    dreams_cfg: DreamsConfig,
    lifecycle_cfg: LifecycleConfig,
) -> DreamArtifact | None:
    """If the agent is eligible to dream, do so once. Otherwise return None.

    Idempotent: calling this in a poll loop is safe — the dream-
    eligibility check + the snapshot's ``last_dream_at`` together
    rate-limit to one dream per dormancy window.

    Failures are swallowed and logged; never propagated.
    """
    if is_emergency_disabled():
        return None
    if not dreams_cfg.auto_on_dormancy:
        return None
    try:
        snap = load_snapshot(agent_id)
    except Exception:
        logger.exception("maybe_dream: could not load snapshot for %s", agent_id)
        return None
    if not dream_eligible(snap, lifecycle_cfg):
        return None

    # Defensive daily cap — bounds the worst case if the scheduler is
    # misconfigured (e.g. poll_seconds=1, dormancy_after_seconds=1).
    # Steady-state production hits this only via misconfiguration.
    if (
        dreams_cfg.max_dreams_per_day > 0
        and _dreams_in_last_24h(agent_id) >= dreams_cfg.max_dreams_per_day
    ):
        logger.info(
            "maybe_dream: daily cap reached for %s (%d/%d in last 24h); skipping",
            agent_id,
            _dreams_in_last_24h(agent_id),
            dreams_cfg.max_dreams_per_day,
        )
        return None

    # Mark the snapshot as DREAMING so concurrent calls don't double-fire.
    snap.state = LifecycleState.DREAMING.value
    save_snapshot(snap, enabled=lifecycle_cfg.persist_state_to_disk)

    try:
        artifact = await generate_dream(model=model, agent_id=agent_id, cfg=dreams_cfg)
    except (asyncio.CancelledError, KeyboardInterrupt):
        # Restore state on user-driven cancel; don't bump imagination.
        snap = load_snapshot(agent_id)
        snap.state = LifecycleState.DORMANT.value
        save_snapshot(snap, enabled=lifecycle_cfg.persist_state_to_disk)
        raise
    except Exception:
        logger.exception("dream generation failed for %s", agent_id)
        snap = load_snapshot(agent_id)
        snap.state = LifecycleState.DORMANT.value
        save_snapshot(snap, enabled=lifecycle_cfg.persist_state_to_disk)
        return None

    # Bump imagination + return to dormant.
    snap = load_snapshot(agent_id)  # reload to capture any concurrent changes
    bump_imagination(snap, dreams_cfg.imagination_trait_increment)
    snap.state = LifecycleState.DORMANT.value
    save_snapshot(snap, enabled=lifecycle_cfg.persist_state_to_disk)

    # Telemetry is now recorded inside ``generate_dream`` itself so
    # every caller path (lifecycle wrapper, /dream slash command,
    # programmatic invocations, tests) produces consistent events.
    return artifact


def list_agent_dreams(agent_id: str, *, limit: int = 20) -> list[Path]:
    """Return the per-agent dream files, most-recent first."""
    dreams_dir = agent_state_dir(agent_id) / "dreams"
    if not dreams_dir.exists():
        return []
    return sorted(dreams_dir.glob("*.md"), reverse=True)[:limit]


def _dreams_in_last_24h(agent_id: str) -> int:
    """Count dream files written in the last 24 hours for this agent.

    Used by ``maybe_dream`` as the defensive cap against
    misconfiguration. Reads only the directory listing — does not open
    any dream files — so the cost stays O(N_files) on a per-agent
    directory that's typically < 100 entries.
    """
    import time as _time

    dreams_dir = agent_state_dir(agent_id) / "dreams"
    if not dreams_dir.exists():
        return 0
    cutoff = _time.time() - 86_400.0
    count = 0
    try:
        for entry in dreams_dir.glob("*.md"):
            try:
                if entry.stat().st_mtime >= cutoff:
                    count += 1
            except OSError:
                continue
    except OSError:
        return 0
    return count


def sample_dream_excerpts(
    agent_id: str,
    *,
    count: int = 3,
    max_chars: int = 600,
    rng_seed: int | None = None,
    category_filter: str | None = None,
) -> list[str]:
    """Return ``count`` short excerpts from this agent's dream archive.

    Used by :class:`ImaginationMiddleware` to inject material when the
    agent is stuck. Each excerpt is ≤``max_chars``; we pull the first
    paragraph after the title.

    Args:
        agent_id: Per-agent identifier — selects the dream archive.
        count: How many excerpts to return.
        max_chars: Per-excerpt cap.
        rng_seed: When set, makes the sample deterministic.
        category_filter: Phase 21 — when set, only sample dreams whose
            frontmatter ``category:`` value matches. Dreams written
            before Phase 21 (no category in frontmatter) are excluded
            when the filter is active. Unrecognized categories yield
            an empty result rather than silently falling back.

    Returns:
        Up to ``count`` non-empty excerpts. Empty list when the archive
        is empty or no dream matches the filter.
    """
    import random

    rng = random.Random(rng_seed) if rng_seed is not None else random.Random()
    files = list_agent_dreams(agent_id, limit=30)
    if not files:
        return []
    if category_filter:
        files = [f for f in files if _dream_category(f) == category_filter]
        if not files:
            return []
    chosen_files = rng.sample(files, min(count, len(files)))
    excerpts: list[str] = []
    for path in chosen_files:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        # Skip frontmatter
        body = text
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                body = parts[2]
        body = body.strip()
        # Pull title + first paragraph
        lines = body.splitlines()
        title = ""
        first_para: list[str] = []
        for line in lines:
            if line.startswith("### "):
                title = line[4:].strip()
                continue
            if line.strip() == "":
                if first_para:
                    break
                continue
            first_para.append(line.strip())
        excerpt = title + "\n" + " ".join(first_para)
        excerpts.append(excerpt[:max_chars].strip())
    return [e for e in excerpts if e]


def _dream_category(path: Path) -> str | None:
    """Read the ``category:`` frontmatter field from a dream file.

    Phase 21 — used by ``sample_dream_excerpts`` to filter by seed
    category. Returns ``None`` when the file has no frontmatter or
    no ``category:`` line.
    """
    try:
        with path.open("r", encoding="utf-8") as fh:
            first = fh.readline()
            if not first.startswith("---"):
                return None
            for _ in range(20):  # bounded scan
                line = fh.readline()
                if not line:
                    return None
                stripped = line.strip()
                if stripped.startswith("---"):
                    return None
                if stripped.lower().startswith("category:"):
                    return stripped.split(":", 1)[1].strip()
    except OSError:
        return None
    return None
