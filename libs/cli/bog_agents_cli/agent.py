"""Agent management and creation for the CLI."""

from __future__ import annotations

import logging
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bog_agents import create_agent
from bog_agents.backends import CompositeBackend, LocalShellBackend
from bog_agents.backends.filesystem import FilesystemBackend
from bog_agents.middleware import MemoryMiddleware, SkillsMiddleware

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from bog_agents.backends.sandbox import SandboxBackendProtocol
    from bog_agents.middleware.subagents import CompiledSubAgent, SubAgent
    from langchain.agents.middleware import InterruptOnConfig
    from langchain.agents.middleware.types import AgentState
    from langchain.messages import ToolCall
    from langchain.tools import BaseTool
    from langchain_core.language_models import BaseChatModel
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.pregel import Pregel
    from langgraph.runtime import Runtime

    from bog_agents_cli.mcp_tools import MCPServerInfo
    from bog_agents_cli.output import OutputFormat

from bog_agents_cli.config import (
    COLORS,
    config,
    console,
    get_default_coding_instructions,
    get_glyphs,
    settings,
)
from bog_agents_cli.configurable_model import ConfigurableModelMiddleware
from bog_agents_cli.integrations.sandbox_factory import get_default_working_dir
from bog_agents_cli.local_context import LocalContextMiddleware, _ExecutableBackend
from bog_agents_cli.project_utils import ProjectContext, get_server_project_context
from bog_agents_cli.subagents import list_subagents
from bog_agents_cli.unicode_security import (
    check_url_safety,
    detect_dangerous_unicode,
    format_warning_detail,
    render_with_unicode_markers,
    strip_dangerous_unicode,
    summarize_issues,
)

logger = logging.getLogger(__name__)


def _resolve_thinking_config() -> tuple[bool, int]:
    """Determine the initial state of the extended-thinking middleware.

    Resolution order (highest priority first):

    1. ``BOG_AGENTS_THINKING`` env var (``1``/``true``/``yes`` = on,
       anything else = off) — handy for one-off sessions without
       editing config.toml.
    2. ``[models.providers.<provider>].params.thinking_enabled`` in
       ``~/.bog-agents/config.toml`` — per-provider opt-in.
    3. Default ``False``.

    The budget follows the same hierarchy via
    ``BOG_AGENTS_THINKING_BUDGET`` / ``thinking_budget_tokens``, with
    a default of 8 000 tokens.

    Returns:
        ``(enabled, budget_tokens)``.
    """
    env_enable = os.environ.get("BOG_AGENTS_THINKING", "").strip().lower()
    env_budget_raw = os.environ.get("BOG_AGENTS_THINKING_BUDGET", "").strip()

    enabled = env_enable in ("1", "true", "yes", "on")
    budget = 8_000
    if env_budget_raw:
        try:
            budget = max(1_000, int(env_budget_raw))
        except ValueError:
            logger.warning(
                "Invalid BOG_AGENTS_THINKING_BUDGET=%r; ignoring", env_budget_raw
            )

    if env_enable:
        # Env var wins — no need to consult config.toml.
        return enabled, budget

    # Fall back to config.toml. Read only the active provider's params
    # to avoid leaking unrelated providers' settings into the session.
    provider = (settings.model_provider or "").strip()
    if not provider:
        return enabled, budget
    try:
        from bog_agents_cli.model_config import ModelConfig

        cfg = ModelConfig.load()
    except (OSError, ValueError):
        return enabled, budget
    provider_cfg = cfg.providers.get(provider) or {}
    params = provider_cfg.get("params") if isinstance(provider_cfg, dict) else {}
    if not isinstance(params, dict):
        return enabled, budget

    cfg_enable = params.get("thinking_enabled")
    if isinstance(cfg_enable, bool):
        enabled = cfg_enable
    cfg_budget = params.get("thinking_budget_tokens")
    if isinstance(cfg_budget, int) and cfg_budget >= 1_000:
        budget = cfg_budget
    return enabled, budget


def _build_propose_on_dream_callback(
    cfg: Any,  # noqa: ANN401 — DreamscapeConfig
) -> Any | None:  # noqa: ANN401 — returns Callable[[str, str], Awaitable[None]]
    """K5: build the dream-completion → ``/expert propose`` callback.

    Returns a coroutine function suitable for ``DreamScheduler``'s
    ``on_dream_complete`` slot. Each successful dream triggers one
    proposer pass on the controller for the current working
    directory; the proposer either stages a YAML proposal under
    ``.bog-agents/expert_rules/proposals/`` (the default) or
    auto-applies one when the rules engine is otherwise configured to
    accept it. Failures are caught and logged — a misbehaving
    proposer must never destabilize the dream scheduler.

    Args:
        cfg: The active ``DreamscapeConfig`` (typed Any to avoid an
            eager import in non-dreamscape paths). Only used here to
            keep the signature aligned with the caller.
    """
    _ = cfg  # currently unused; future tuning can read agent_id from cfg

    async def on_dream_complete(agent_id: str, dream_title: str) -> None:
        # Imports deferred so the callback factory stays cheap on the
        # non-dreamscape path.
        try:
            from bog_agents_cli.config import create_model, settings
        except Exception:
            logger.debug(
                "propose-on-dream: settings unavailable; skipping (agent=%s)",
                agent_id,
            )
            return

        try:
            from bog_agents_cli.expert_controller import get_controller
        except Exception:
            logger.debug("propose-on-dream: expert_controller import failed; skipping")
            return

        spec = (settings.model_name or "").strip()
        if not spec:
            logger.debug(
                "propose-on-dream: no active model configured (agent=%s)",
                agent_id,
            )
            return

        def model_factory() -> Any:  # noqa: ANN401 — BaseChatModel
            return create_model(spec).model

        cwd = Path.cwd()
        controller = get_controller(cwd, model_factory=model_factory)
        # Run synchronously on a worker thread — propose_from_dreamscape
        # is CPU/LLM bound and blocking; we don't want to stall the
        # scheduler's asyncio loop.
        import asyncio as _asyncio

        try:
            result = await _asyncio.to_thread(
                controller.propose_from_dreamscape,
                agent_id,
                auto_activate=False,
            )
        except Exception:
            logger.exception(
                "propose-on-dream: proposer raised (agent=%s, dream=%r)",
                agent_id,
                dream_title,
            )
            return
        logger.info(
            "propose-on-dream: proposer finished (agent=%s, dream=%r): %s",
            agent_id,
            dream_title,
            result.splitlines()[0] if result else "<no output>",
        )

    return on_dream_complete


def _build_dream_scheduler_factory(
    *,
    cfg: Any,  # noqa: ANN401 — DreamscapeConfig; typed Any to avoid eager import
    agent_id: str,
) -> Callable[[], Any] | None:
    """Build a zero-arg factory that starts a DreamScheduler.

    Returns ``None`` when prerequisites aren't met — typically because
    no dream model can be resolved. The factory closure resolves the
    model lazily (only when the lifecycle middleware first fires)
    so we don't pay the model-construction cost during agent build.

    Args:
        cfg: A ``DreamscapeConfig`` (typed Any to avoid the cost of
            an import that runs even when this feature is off).
        agent_id: Stable identifier passed to the scheduler.

    Returns:
        A nullary callable, or ``None`` when scheduling can't be set up.
    """

    def factory() -> Any:  # noqa: ANN401 — returns a DreamScheduler, import deferred

        # Imports deferred — these pull in langchain providers we
        # don't want to load when dreamscape is disabled.
        from bog_agents_cli.config import create_model

        spec = (cfg.dreams.model or "").strip()
        if not spec:
            # Inherit the active model — read from CLI settings.
            from bog_agents_cli.config import settings as _settings

            provider = (_settings.model_provider or "").strip()
            model_name = (_settings.model_name or "").strip()
            spec = f"{provider}:{model_name}" if provider and model_name else model_name

        if not spec:
            logger.info("dreamscape: dream scheduler not started — no model resolved")
            return None

        try:
            result = create_model(spec)
        except Exception:
            logger.warning(
                "dreamscape: dream scheduler model creation failed (%s)",
                spec,
                exc_info=True,
            )
            return None

        from bog_agents_cli.dreamscape.scheduler import ensure_scheduler

        # K5: when ``propose_rules_on_complete`` is on, install a
        # callback that fires the expert proposer once per dream.
        # Replaces the timer-driven ``/expert watch`` for users who
        # want the proposer paced by actual dream activity instead of
        # wall-clock polls.
        on_complete = (
            _build_propose_on_dream_callback(cfg)
            if cfg.dreams.propose_rules_on_complete
            else None
        )

        scheduler = ensure_scheduler(
            agent_id=agent_id,
            model=result.model,
            dreams_cfg=cfg.dreams,
            lifecycle_cfg=cfg.lifecycle,
            on_dream_complete=on_complete,
        )
        scheduler.start()
        return scheduler

    return factory


# Phase 19 — module-level strong refs for fire-and-forget LLM-classifier
# tasks. Without this set, the GC can reap the task before it
# completes, dropping the classification + cache write.
_LLM_CLASSIFIER_TASKS: set[Any] = set()


def _schedule_llm_domain_classification(agent_id: str, system_prompt: str) -> None:
    """Phase 19 — fire-and-forget LLM classification for the long tail.

    The keyword classifier returned ``"general"`` for this agent's
    system prompt — either the prompt is intentionally broad or its
    vocabulary doesn't intersect the keyword dictionaries. Phase 19
    builds an LLM-based fallback that runs once per agent build,
    caches the result to disk, and is consulted by
    :func:`resolve_agent_domain` on subsequent dream cycles.

    The classification runs as a background asyncio task using a
    cheap dream-model spec when one is configured. Failure here is
    silent — the agent uses the keyword "general" classification
    until the cache is populated.
    """
    try:
        import asyncio

        from bog_agents_cli.dreamscape import load_dreamscape_config
        from bog_agents_cli.dreamscape.domain import (
            _save_cached_llm_domain,
            classify_agent_domain_llm_async,
            llm_cache_path,
        )

        # Skip if already cached — avoids paying for repeat agent builds.
        if llm_cache_path(agent_id).exists():
            return

        ds_cfg = load_dreamscape_config()
        spec = (ds_cfg.dreams.model or "").strip()
        if not spec:
            from bog_agents_cli.config import settings as _settings

            provider = (_settings.model_provider or "").strip()
            model_name = (_settings.model_name or "").strip()
            spec = f"{provider}:{model_name}" if provider and model_name else model_name
        if not spec:
            return

        from bog_agents_cli.config import create_model

        result = create_model(spec)

        async def _do() -> None:
            domain = await classify_agent_domain_llm_async(system_prompt, result.model)
            if domain != "general":
                _save_cached_llm_domain(agent_id, domain)
                logger.info(
                    "dreamscape: llm classifier cached %s for agent=%s",
                    domain,
                    agent_id,
                )

        try:
            loop = asyncio.get_running_loop()
            # Fire-and-forget by design. The task may be GC'd before
            # completion if the event loop tears down; that's fine —
            # this is a best-effort enhancement, not a correctness
            # path. Stored in a module-level set so the GC doesn't
            # reap it during normal operation.
            _LLM_CLASSIFIER_TASKS.add(loop.create_task(_do()))
        except RuntimeError:
            # No running event loop — the agent build is synchronous.
            # Skip; the next async call from the agent will leave the
            # keyword classification in place. (Real users always
            # build agents inside an event loop.)
            logger.debug(
                "dreamscape: llm classifier deferred — no event loop at build time"
            )
    except Exception:
        logger.debug("dreamscape: llm classifier scheduling failed", exc_info=True)


def _attach_dreamscape_middleware(
    middleware_list: list[Any],
    *,
    cfg: Any,  # noqa: ANN401 — DreamscapeConfig is deferred-imported; typing leaks here
    agent_id: str,
    system_prompt: str | None = None,
) -> None:
    """Append every enabled dreamscape middleware to ``middleware_list``.

    Caller has already verified ``cfg.any_active`` is True. Each
    sub-middleware is gated on its individual ``enabled`` flag so the
    master switch can be on while specific features stay off. All
    imports are deferred to keep CLI cold-start fast.

    Args:
        middleware_list: The accumulating list of agent middleware.
            New middlewares are appended in place.
        cfg: A ``DreamscapeConfig`` (typed as ``Any`` here to avoid
            an import that runs even when the feature is off).
        agent_id: Per-agent identifier so on-disk state files end up
            in the right directory.
        system_prompt: The agent's resolved system prompt. When
            provided, captured via :func:`capture_agent_profile` so the
            dream engine can classify the agent's working domain.
    """
    safe_id = agent_id or "default"

    # Capture the agent's system prompt so the dream engine can
    # classify the agent's working domain (engineering / creative /
    # research / general) and steer seed selection accordingly. Phases
    # 10-12 showed the effect of injected dreams is domain-conditional;
    # this hook is the input to context-aware dreaming. Best-effort —
    # disk failure here must not block agent creation.
    if system_prompt:
        try:
            from bog_agents_cli.dreamscape.domain import (
                capture_agent_profile,
                classify_agent_domain,
            )

            capture_agent_profile(safe_id, system_prompt)
            # Phase 19 — if the keyword classifier falls back to
            # "general", schedule a background LLM classification.
            # The result is cached to disk and consulted on next dream
            # cycle. Best-effort: failure here logs and continues.
            if classify_agent_domain(system_prompt) == "general":
                _schedule_llm_domain_classification(safe_id, system_prompt)
        except Exception:
            logger.debug("dreamscape: agent-profile capture failed", exc_info=True)

    # Persist the resolved runtime config so the dashboard (/agent-state,
    # /dreamscape status) shows what's actually active instead of what
    # the canonical TOML says. Best-effort — disk failure logs and
    # continues. Fixes the Phase-1 staleness bug where the dashboard
    # reported ``master_enabled: False`` whenever the runtime was
    # driven entirely by env-var overrides.
    try:
        from bog_agents_cli.dreamscape import write_active_runtime_config

        write_active_runtime_config(cfg)
    except Exception:
        logger.debug("dreamscape: failed to persist active config", exc_info=True)

    if cfg.lifecycle.enabled:
        try:
            from bog_agents_cli.dreamscape.lifecycle import LifecycleMiddleware

            # When dream auto-on-dormancy is on, give the lifecycle
            # middleware a factory that lazy-starts a DreamScheduler
            # the first time it sees a real async model call. The
            # factory closure captures the dream-model resolution +
            # configs so the middleware itself stays decoupled from
            # langchain/model loading.
            dream_factory = (
                _build_dream_scheduler_factory(cfg=cfg, agent_id=safe_id)
                if cfg.dreams.auto_on_dormancy
                else None
            )
            middleware_list.append(
                LifecycleMiddleware(
                    agent_id=safe_id,
                    cfg=cfg.lifecycle,
                    dream_scheduler_factory=dream_factory,
                )
            )
            logger.info(
                "dreamscape: lifecycle middleware attached "
                "(agent=%s, dream_scheduler=%s)",
                safe_id,
                "yes" if dream_factory else "no",
            )
        except Exception:
            logger.warning(
                "dreamscape: lifecycle middleware failed to attach", exc_info=True
            )

    if cfg.laws.enabled:
        try:
            from bog_agents_cli.dreamscape.laws import LawsMiddleware
            from bog_agents_cli.dreamscape.violations import make_violation_recorder

            middleware_list.append(
                LawsMiddleware(
                    cfg=cfg.laws,
                    violation_recorder=make_violation_recorder(safe_id),
                )
            )
            logger.info(
                "dreamscape: laws middleware attached (reject_on_violation=%s)",
                cfg.laws.reject_on_violation,
            )
        except Exception:
            logger.warning(
                "dreamscape: laws middleware failed to attach", exc_info=True
            )

    if cfg.shared_memory.enabled:
        try:
            from bog_agents_cli.dreamscape.shared_memory import (
                SharedMemoryMiddleware,
            )

            middleware_list.append(
                SharedMemoryMiddleware(agent_id=safe_id, cfg=cfg.shared_memory)
            )
            logger.info(
                "dreamscape: shared-memory middleware attached (backend=%s)",
                cfg.shared_memory.backend,
            )
        except Exception:
            logger.warning(
                "dreamscape: shared-memory middleware failed to attach", exc_info=True
            )

    if cfg.imagination.enabled:
        try:
            from dataclasses import replace as dc_replace

            from bog_agents_cli.dreamscape.domain import (
                recommended_injection_style,
                resolve_agent_domain,
            )
            from bog_agents_cli.dreamscape.imagination import ImaginationMiddleware

            # Context-aware injection style: engineering / research /
            # general agents get the neutral wrapper (Phase 10 + 12
            # showed the "Fragment from your dreams" framing is penalized
            # on technical-debugging prompts); creative agents get the
            # original dreams wrapper (Phase 11 showed it's a feature
            # there, 6/7 treatment wins).
            #
            # Phase 17 — the per-prompt routing mechanism is shipped
            # (use_prompt_routing in ImaginationConfig) but is NOT
            # enabled by default. Phase 17's N=45 validation showed
            # that forcing the dreams wrapper on engineering agents
            # via decision-pattern detection did not reliably improve
            # outcomes (legacy-deletion 87% with neutral dropped to
            # 67% with prompt-routed dreams; retry-under-load 60%
            # dropped to 33%). The mechanism is preserved as a knob
            # for future experiments and for power-users who can A/B
            # test it on their workloads. The default keeps the
            # agent-level routing only.
            domain = resolve_agent_domain(safe_id)
            preferred_style = recommended_injection_style(domain)
            effective_cfg = dc_replace(cfg.imagination, injection_style=preferred_style)
            middleware_list.append(
                ImaginationMiddleware(agent_id=safe_id, cfg=effective_cfg)
            )
            logger.info(
                "dreamscape: imagination middleware attached "
                "(trigger@%d failures, threshold=%.1f, domain=%s, style=%s)",
                cfg.imagination.trigger_after_failures,
                cfg.imagination.min_imagination_trait,
                domain,
                preferred_style,
            )
        except Exception:
            logger.warning(
                "dreamscape: imagination middleware failed to attach", exc_info=True
            )


DEFAULT_AGENT_NAME = "agent"
"""The default agent name used when no `-a` flag is provided."""

REQUIRE_COMPACT_TOOL_APPROVAL: bool = True
"""When `True`, `compact_conversation` requires HITL approval like other gated tools."""

_RESERVED_AGENT_HOME_DIRS = frozenset(
    {
        "daemon",  # bog-agents-daemon state (token, runs/, daemon.pid)
        "logs",
        "pipelines",  # CLI pipeline definitions, not an agent
        "plugins",
        "skills",
    }
)
"""Directories under `~/.bog-agents` reserved for global CLI state, not agents."""


def _iter_listed_agent_dirs(agents_dir: Path) -> list[Path]:
    """Return agent directories that should appear in `bog-agents list`.

    The user-level `.bog-agents` directory also contains shared CLI state
    such as logs and plugin installs. Filter those reserved directories
    so `list` only shows real agent workspaces.

    Args:
        agents_dir: Base `~/.bog-agents` directory.

    Returns:
        Sorted list of agent directories.
    """
    return [
        agent_path
        for agent_path in sorted(agents_dir.iterdir())
        if (agent_path.is_dir() and agent_path.name not in _RESERVED_AGENT_HOME_DIRS)
    ]


def list_agents(*, output_format: OutputFormat = "text") -> None:
    """List all available agents.

    Args:
        output_format: Output format — `'text'` (Rich) or `'json'`.
    """
    agents_dir = settings.user_agents_dir

    if not agents_dir.exists() or not any(agents_dir.iterdir()):
        if output_format == "json":
            from bog_agents_cli.output import write_json

            write_json("list", [])
            return
        console.print("[yellow]No agents found.[/yellow]")
        console.print(
            "[dim]Agents will be created in ~/.bog-agents/ "
            "when you first use them.[/dim]",
            style=COLORS["dim"],
        )
        return

    agent_dirs = _iter_listed_agent_dirs(agents_dir)

    if not agent_dirs:
        if output_format == "json":
            from bog_agents_cli.output import write_json

            write_json("list", [])
            return
        console.print("[yellow]No agents found.[/yellow]")
        console.print(
            "[dim]Agents will be created in ~/.bog-agents/ "
            "when you first use them.[/dim]",
            style=COLORS["dim"],
        )
        return

    if output_format == "json":
        from bog_agents_cli.output import write_json

        agents = []
        for agent_path in agent_dirs:
            agent_name = agent_path.name
            agents.append(
                {
                    "name": agent_name,
                    "path": str(agent_path),
                    "has_agents_md": (agent_path / "AGENTS.md").exists(),
                    "is_default": agent_name == DEFAULT_AGENT_NAME,
                }
            )
        write_json("list", agents)
        return

    console.print("\n[bold]Available Agents:[/bold]\n", style=COLORS["primary"])

    for agent_path in agent_dirs:
        agent_name = agent_path.name
        agent_md = agent_path / "AGENTS.md"
        is_default = agent_name == DEFAULT_AGENT_NAME
        default_label = " [dim](default)[/dim]" if is_default else ""

        bullet = get_glyphs().bullet
        if agent_md.exists():
            console.print(
                f"  {bullet} [bold]{agent_name}[/bold]{default_label}",
                style=COLORS["primary"],
            )
            console.print(f"    {agent_path}", style=COLORS["dim"])
        else:
            console.print(
                f"  {bullet} [bold]{agent_name}[/bold]{default_label}"
                " [dim](incomplete)[/dim]",
                style=COLORS["tool"],
            )
            console.print(f"    {agent_path}", style=COLORS["dim"])

    console.print()


def reset_agent(
    agent_name: str,
    source_agent: str | None = None,
    *,
    output_format: OutputFormat = "text",
) -> None:
    """Reset an agent to default or copy from another agent.

    Args:
        agent_name: Name of the agent to reset.
        source_agent: Copy AGENTS.md from this agent instead of default.
        output_format: Output format — `'text'` (Rich) or `'json'`.
    """
    agents_dir = settings.user_agents_dir
    agent_dir = agents_dir / agent_name

    if source_agent:
        source_dir = agents_dir / source_agent
        source_md = source_dir / "AGENTS.md"

        if not source_md.exists():
            console.print(
                f"[bold red]Error:[/bold red] Source agent '{source_agent}' not found "
                "or has no AGENTS.md"
            )
            return

        source_content = source_md.read_text(encoding="utf-8")
        action_desc = f"contents of agent '{source_agent}'"
    else:
        source_content = get_default_coding_instructions()
        action_desc = "default"

    if agent_dir.exists():
        shutil.rmtree(agent_dir)
        if output_format != "json":
            console.print(
                f"Removed existing agent directory: {agent_dir}", style=COLORS["tool"]
            )

    agent_dir.mkdir(parents=True, exist_ok=True)
    agent_md = agent_dir / "AGENTS.md"
    agent_md.write_text(source_content, encoding="utf-8")

    if output_format == "json":
        from bog_agents_cli.output import write_json

        write_json(
            "reset",
            {
                "agent": agent_name,
                "reset_to": source_agent or "default",
                "path": str(agent_dir),
            },
        )
        return

    console.print(
        f"{get_glyphs().checkmark} Agent '{agent_name}' reset to {action_desc}",
        style=COLORS["primary"],
    )
    console.print(f"Location: {agent_dir}\n", style=COLORS["dim"])


def get_system_prompt(
    assistant_id: str,
    sandbox_type: str | None = None,
    *,
    interactive: bool = True,
    cwd: str | Path | None = None,
) -> str:
    """Get the base system prompt for the agent.

    Loads the base system prompt template from `system_prompt.md` and
    interpolates dynamic sections (model identity, working directory,
    skills path, execution mode).

    Args:
        assistant_id: The agent identifier for path references
        sandbox_type: Type of sandbox provider
            (`'daytona'`, `'langsmith'`, `'modal'`, `'runloop'`).

            If `None`, agent is operating in local mode.
        interactive: When `False`, the prompt is tailored for headless
            non-interactive execution (no human in the loop).
        cwd: Override the working directory shown in the prompt.

    Returns:
        The system prompt string

    Example:
        ```txt
        You are running as model {MODEL} (provider: {PROVIDER}).

        Your context window is {CONTEXT_WINDOW} tokens.

        ... {CONDITIONAL SECTIONS} ...
        ```
    """
    template = (Path(__file__).parent / "system_prompt.md").read_text(encoding="utf-8")

    skills_path = f"~/.bog-agents/{assistant_id}/skills"

    if interactive:
        mode_description = "an interactive CLI on the user's computer"
        interactive_preamble = (
            "The user sends you messages and you respond with text and tool "
            "calls. Your tools run on the user's machine. The user can see "
            "your responses and tool outputs in real time, so keep them "
            "informed — but don't over-explain."
        )
        ambiguity_guidance = (
            "- If the request is ambiguous, ask questions before acting.\n"
            "- If asked how to approach something, explain first, then act."
        )
    else:
        mode_description = (
            "non-interactive (headless) mode — there is no human operator "
            "monitoring your output in real time"
        )
        interactive_preamble = (
            "You received a single task and must complete it fully and "
            "autonomously. There is no human available to answer follow-up "
            "questions, so do NOT ask for clarification — make reasonable "
            "assumptions and proceed."
        )
        ambiguity_guidance = (
            "- Do NOT ask clarifying questions — there is no human to answer "
            "them. Make reasonable assumptions and proceed.\n"
            "- If you encounter ambiguity, choose the most reasonable "
            "interpretation and note your assumption briefly.\n"
            "- Always use non-interactive command variants — no human is "
            "available to respond to prompts. Examples: `npm init -y` not "
            "`npm init`, `apt-get install -y` not `apt-get install`, "
            "`yes |` or `--no-input`/`--non-interactive` flags where "
            "available. Never run commands that block waiting for stdin."
        )

    # Build model identity section
    model_identity_section = ""
    if settings.model_name:
        model_identity_section = (
            f"### Model Identity\n\nYou are running as model `{settings.model_name}`"
        )
        if settings.model_provider:
            model_identity_section += f" (provider: {settings.model_provider})"
        model_identity_section += ".\n"
        if settings.model_context_limit:
            model_identity_section += (
                f"Your context window is {settings.model_context_limit:,} tokens.\n"
            )
        model_identity_section += "\n"

    # Build working directory section (local vs sandbox)
    if sandbox_type:
        working_dir = get_default_working_dir(sandbox_type)
        working_dir_section = (
            f"### Current Working Directory\n\n"
            f"You are operating in a **remote Linux sandbox** at `{working_dir}`.\n\n"
            f"All code execution and file operations happen in this sandbox "
            f"environment.\n\n"
            f"**Important:**\n"
            f"- The CLI is running locally on the user's machine, but you execute "
            f"code remotely\n"
            f"- Use `{working_dir}` as your working directory for all operations\n\n"
        )
    else:
        if cwd is not None:
            resolved_cwd = Path(cwd)
        else:
            try:
                resolved_cwd = Path.cwd()
            except OSError:
                logger.warning(
                    "Could not determine working directory for system prompt",
                    exc_info=True,
                )
                resolved_cwd = Path()
        cwd = resolved_cwd
        working_dir_section = (
            f"### Current Working Directory\n\n"
            f"The filesystem backend is currently operating in: `{cwd}`\n\n"
            f"### File System and Paths\n\n"
            f"**IMPORTANT - Path Handling:**\n"
            f"- All file paths must be absolute paths (e.g., `{cwd}/file.txt`)\n"
            f"- Use the working directory to construct absolute paths\n"
            f"- Example: To create a file in your working directory, "
            f"use `{cwd}/research_project/file.md`\n"
            f"- Never use relative paths - always construct full absolute paths\n\n"
        )

    result = (
        template.replace("{mode_description}", mode_description)
        .replace("{interactive_preamble}", interactive_preamble)
        .replace("{ambiguity_guidance}", ambiguity_guidance)
        .replace("{model_identity_section}", model_identity_section)
        .replace("{working_dir_section}", working_dir_section)
        .replace("{skills_path}", skills_path)
    )

    # Detect unreplaced placeholders (defense-in-depth for template typos)
    unreplaced = re.findall(r"\{[a-z_]+\}", result)
    if unreplaced:
        logger.warning("System prompt contains unreplaced placeholders: %s", unreplaced)

    # Append project + global memory if present
    from bog_agents_cli.project_memory import load_project_memory

    memory_block = load_project_memory(cwd=cwd)
    if memory_block:
        result = result + memory_block

    return result


def _format_write_file_description(
    tool_call: ToolCall, _state: AgentState[Any], _runtime: Runtime[Any]
) -> str:
    """Format write_file tool call for approval prompt.

    Returns:
        Formatted description string for the write_file tool call.
    """
    args = tool_call["args"]
    file_path = args.get("file_path", "unknown")
    content = args.get("content", "")

    action = "Overwrite" if Path(file_path).exists() else "Create"
    line_count = len(content.splitlines())

    return f"File: {file_path}\nAction: {action} file\nLines: {line_count}"


def _format_edit_file_description(
    tool_call: ToolCall, _state: AgentState[Any], _runtime: Runtime[Any]
) -> str:
    """Format edit_file tool call for approval prompt.

    Returns:
        Formatted description string for the edit_file tool call.
    """
    args = tool_call["args"]
    file_path = args.get("file_path", "unknown")
    replace_all = bool(args.get("replace_all", False))

    scope = "all occurrences" if replace_all else "single occurrence"
    return f"File: {file_path}\nAction: Replace text ({scope})"


def _format_web_search_description(
    tool_call: ToolCall, _state: AgentState[Any], _runtime: Runtime[Any]
) -> str:
    """Format web_search tool call for approval prompt.

    Returns:
        Formatted description string for the web_search tool call.
    """
    args = tool_call["args"]
    query = args.get("query", "unknown")
    max_results = args.get("max_results", 5)

    return (
        f"Query: {query}\nMax results: {max_results}\n\n"
        f"{get_glyphs().warning}  This will use Tavily API credits"
    )


def _format_fetch_url_description(
    tool_call: ToolCall, _state: AgentState[Any], _runtime: Runtime[Any]
) -> str:
    """Format fetch_url tool call for approval prompt.

    Returns:
        Formatted description string for the fetch_url tool call.
    """
    args = tool_call["args"]
    url = str(args.get("url", "unknown"))
    display_url = strip_dangerous_unicode(url)
    timeout = args.get("timeout", 30)
    safety = check_url_safety(url)

    warning_lines: list[str] = []
    if not safety.safe:
        detail = format_warning_detail(safety.warnings)
        warning_lines.append(f"{get_glyphs().warning}  URL warning: {detail}")
    if safety.decoded_domain:
        warning_lines.append(
            f"{get_glyphs().warning}  Decoded domain: {safety.decoded_domain}"
        )

    warning_block = "\n".join(warning_lines)
    if warning_block:
        warning_block = f"\n{warning_block}"

    return (
        f"URL: {display_url}\nTimeout: {timeout}s\n\n"
        f"{get_glyphs().warning}  Will fetch and convert web content to markdown"
        f"{warning_block}"
    )


def _format_task_description(
    tool_call: ToolCall, _state: AgentState[Any], _runtime: Runtime[Any]
) -> str:
    """Format task (subagent) tool call for approval prompt.

    The task tool signature is: task(description: str, subagent_type: str)
    The description contains all instructions that will be sent to the subagent.

    Returns:
        Formatted description string for the task tool call.
    """
    args = tool_call["args"]
    description = args.get("description", "unknown")
    subagent_type = args.get("subagent_type", "unknown")

    # Truncate description if too long for display
    description_preview = description
    if len(description) > 500:  # Subagent description length threshold
        description_preview = description[:500] + "..."

    glyphs = get_glyphs()
    separator = glyphs.box_horizontal * 40
    warning_msg = "Subagent will have access to file operations and shell commands"
    return (
        f"Subagent Type: {subagent_type}\n\n"
        f"Task Instructions:\n"
        f"{separator}\n"
        f"{description_preview}\n"
        f"{separator}\n\n"
        f"{glyphs.warning}  {warning_msg}"
    )


def _format_execute_description(
    tool_call: ToolCall, _state: AgentState[Any], _runtime: Runtime[Any]
) -> str:
    """Format execute tool call for approval prompt.

    Returns:
        Formatted description string for the execute tool call.
    """
    args = tool_call["args"]
    command_raw = str(args.get("command", "N/A"))
    command = strip_dangerous_unicode(command_raw)
    project_context = get_server_project_context()
    effective_cwd = (
        str(project_context.user_cwd)
        if project_context is not None
        else str(Path.cwd())
    )
    lines = [f"Execute Command: {command}", f"Working Directory: {effective_cwd}"]

    issues = detect_dangerous_unicode(command_raw)
    if issues:
        summary = summarize_issues(issues)
        lines.append(f"{get_glyphs().warning}  Hidden Unicode detected: {summary}")
        raw_marked = render_with_unicode_markers(command_raw)
        if len(raw_marked) > 220:  # UI display truncation threshold
            raw_marked = raw_marked[:220] + "..."
        lines.append(f"Raw: {raw_marked}")

    return "\n".join(lines)


def _format_git_commit_description(
    tool_call: ToolCall, _state: AgentState[Any], _runtime: Runtime[Any]
) -> str:
    """Format a git_commit tool call for the approval prompt.

    Returns:
        Formatted description string for the git_commit tool call.
    """
    args = tool_call["args"]
    message = strip_dangerous_unicode(str(args.get("message", "")))
    files = args.get("files")
    lines = [f"Git commit: {message}"]
    if files:
        joined = ", ".join(strip_dangerous_unicode(str(f)) for f in files)
        lines.append(f"Staging first: {joined}")
    else:
        lines.append("Stages and commits all current changes.")
    return "\n".join(lines)


def _format_git_add_description(
    tool_call: ToolCall, _state: AgentState[Any], _runtime: Runtime[Any]
) -> str:
    """Format a git_add tool call for the approval prompt.

    Returns:
        Formatted description string for the git_add tool call.
    """
    paths = tool_call["args"].get("paths") or []
    joined = ", ".join(strip_dangerous_unicode(str(p)) for p in paths)
    return f"Stage files for commit: {joined}" if joined else "Stage files for commit."


def _format_git_branch_description(
    tool_call: ToolCall, _state: AgentState[Any], _runtime: Runtime[Any]
) -> str:
    """Format a git_branch tool call for the approval prompt.

    Only reached when a branch name is supplied (see the `when` predicate); a
    bare `git_branch` call just lists branches and is not gated.

    Returns:
        Formatted description string for the git_branch tool call.
    """
    args = tool_call["args"]
    name = strip_dangerous_unicode(str(args.get("name", "")))
    if args.get("checkout"):
        return f"Create and switch to branch: {name}"
    return f"Create branch: {name}"


def _format_git_stash_description(
    tool_call: ToolCall, _state: AgentState[Any], _runtime: Runtime[Any]
) -> str:
    """Format a git_stash tool call for the approval prompt.

    Only reached for mutating actions (push/pop/drop) — list/show are not gated.

    Returns:
        Formatted description string for the git_stash tool call.
    """
    action = str(tool_call["args"].get("action", "list"))
    if action == "drop":
        return (
            f"{get_glyphs().warning}  git stash drop — permanently discards a "
            "stash entry (cannot be undone)."
        )
    return f"git stash {action}"


def _add_interrupt_on() -> dict[str, InterruptOnConfig]:
    """Configure human-in-the-loop interrupt settings for all gated tools.

    Every tool that can have side effects or access external resources
    (shell execution, file writes/edits, web search, URL fetch, task
    delegation, and the mutating git tools) is gated behind an approval prompt
    unless auto-approve is enabled. The git tools are arg-conditional via a
    `when` predicate so read-only paths (`git_branch` listing, `git_stash
    list`/`show`) are never gated (CLI-CORE-2 / v4).

    Returns:
        Dictionary mapping tool names to their interrupt configuration.
    """
    execute_interrupt_config: InterruptOnConfig = {
        "allowed_decisions": ["approve", "reject"],
        "description": _format_execute_description,  # type: ignore[typeddict-item]  # Callable description narrower than TypedDict expects
    }

    write_file_interrupt_config: InterruptOnConfig = {
        "allowed_decisions": ["approve", "reject"],
        "description": _format_write_file_description,  # type: ignore[typeddict-item]  # Callable description narrower than TypedDict expects
    }

    edit_file_interrupt_config: InterruptOnConfig = {
        "allowed_decisions": ["approve", "reject"],
        "description": _format_edit_file_description,  # type: ignore[typeddict-item]  # Callable description narrower than TypedDict expects
    }

    web_search_interrupt_config: InterruptOnConfig = {
        "allowed_decisions": ["approve", "reject"],
        "description": _format_web_search_description,  # type: ignore[typeddict-item]  # Callable description narrower than TypedDict expects
    }

    fetch_url_interrupt_config: InterruptOnConfig = {
        "allowed_decisions": ["approve", "reject"],
        "description": _format_fetch_url_description,  # type: ignore[typeddict-item]  # Callable description narrower than TypedDict expects
    }

    task_interrupt_config: InterruptOnConfig = {
        "allowed_decisions": ["approve", "reject"],
        "description": _format_task_description,  # type: ignore[typeddict-item]  # Callable description narrower than TypedDict expects
    }

    interrupt_map: dict[str, InterruptOnConfig] = {
        "execute": execute_interrupt_config,
        "write_file": write_file_interrupt_config,
        "edit_file": edit_file_interrupt_config,
        "web_search": web_search_interrupt_config,
        "fetch_url": fetch_url_interrupt_config,
        "task": task_interrupt_config,
    }

    # Mutating git tools (default-on via GitToolsMiddleware) must be gated too —
    # git_commit/git_add always mutate; git_branch mutates only when creating or
    # switching a branch (a name is supplied); git_stash mutates on push/pop/drop
    # (drop is destructive). The `when` predicates keep read-only calls
    # (branch listing, `git stash list`/`show`) un-prompted (CLI-CORE-2 / v4).
    interrupt_map["git_commit"] = {
        "allowed_decisions": ["approve", "reject"],
        "description": _format_git_commit_description,  # type: ignore[typeddict-item]  # Callable description narrower than TypedDict expects
    }
    interrupt_map["git_add"] = {
        "allowed_decisions": ["approve", "reject"],
        "description": _format_git_add_description,  # type: ignore[typeddict-item]  # Callable description narrower than TypedDict expects
    }
    interrupt_map["git_branch"] = {
        "allowed_decisions": ["approve", "reject"],
        "description": _format_git_branch_description,  # type: ignore[typeddict-item]  # Callable description narrower than TypedDict expects
        "when": lambda req: req.tool_call["args"].get("name") is not None,
    }
    interrupt_map["git_stash"] = {
        "allowed_decisions": ["approve", "reject"],
        "description": _format_git_stash_description,  # type: ignore[typeddict-item]  # Callable description narrower than TypedDict expects
        "when": lambda req: (
            req.tool_call["args"].get("action", "list") in {"push", "pop", "drop"}
        ),
    }

    if REQUIRE_COMPACT_TOOL_APPROVAL:
        interrupt_map["compact_conversation"] = {
            "allowed_decisions": ["approve", "reject"],
            "description": (
                "Summarizes older messages into a shorter summary "
                "using an LLM call, then replaces them in context. "
                "Recent messages are kept as-is. Full history is "
                "written to backend storage for agent retrieval."
            ),
        }

    return interrupt_map


def create_cli_agent(
    model: str | BaseChatModel,
    assistant_id: str,
    *,
    tools: Sequence[BaseTool | Callable | dict[str, Any]] | None = None,
    sandbox: SandboxBackendProtocol | None = None,
    sandbox_type: str | None = None,
    system_prompt: str | None = None,
    interactive: bool = True,
    auto_approve: bool = False,
    enable_memory: bool = True,
    enable_skills: bool = True,
    enable_shell: bool = True,
    enable_git_tools: bool = True,
    enable_repo_map: bool = False,
    enable_checkpointing: bool = True,
    enable_cost_tracking: bool = True,
    enable_plan_mode: bool = True,
    effort_level: str = "high",
    budget_usd: float = 0.0,
    auto_lint: bool = False,
    auto_test: bool = False,
    profile: str = "",
    checkpointer: BaseCheckpointSaver | None = None,
    mcp_server_info: list[MCPServerInfo] | None = None,
    cwd: str | Path | None = None,
    project_context: ProjectContext | None = None,
) -> tuple[Pregel, CompositeBackend]:
    """Create a CLI-configured agent with flexible options.

    This is the main entry point for creating a bog-agents CLI agent, usable
    both internally and from external code (e.g., benchmarking frameworks).

    Args:
        model: LLM model to use (e.g., `'anthropic:claude-sonnet-4-6'`)
        assistant_id: Agent identifier for memory/state storage
        tools: Additional tools to provide to agent
        sandbox: Optional sandbox backend for remote execution
            (e.g., `ModalBackend`).

            If `None`, uses local filesystem + shell.
        sandbox_type: Type of sandbox provider
            (`'daytona'`, `'langsmith'`, `'modal'`, `'runloop'`).
            Used for system prompt generation.
        system_prompt: Override the default system prompt.

            If `None`, generates one based on `sandbox_type`, `assistant_id`,
            and `interactive`.
        interactive: When `False`, the auto-generated system prompt is
            tailored for headless non-interactive execution. Ignored when
            `system_prompt` is provided explicitly.
        auto_approve: If `True`, no tools trigger human-in-the-loop
            interrupts — all calls (shell execution, file writes/edits,
            web search, URL fetch) run automatically.

            If `False`, tools pause for user confirmation via the approval menu.
            See `_add_interrupt_on` for the full list of gated tools.
        enable_memory: Enable `MemoryMiddleware` for persistent memory
        enable_skills: Enable `SkillsMiddleware` for custom agent skills
        enable_shell: Enable shell execution via `LocalShellBackend`
            (only in local mode). When enabled, the `execute` tool is available.
        checkpointer: Optional checkpointer for session persistence.
            When `None`, the graph is compiled without a checkpointer.
        mcp_server_info: MCP server metadata to surface in the system prompt.
        enable_git_tools: Enable built-in git workflow tools.
        enable_repo_map: Enable repository structural map middleware.
        enable_checkpointing: Enable git-based checkpointing before file changes.
        enable_cost_tracking: Enable token/cost tracking middleware.
        enable_plan_mode: Enable read-only plan mode toggle.
        effort_level: AI effort level ('low', 'medium', 'high', 'max').
        budget_usd: Maximum budget in USD (0 = unlimited).
        auto_lint: Auto-run linter after file edits.
        auto_test: Auto-run tests after file edits.
        profile: Configuration profile name to apply.
        cwd: Override the working directory for the agent's filesystem backend
            and system prompt.
        project_context: Explicit project path context for project-sensitive
            behavior such as project `AGENTS.md` files, skills, subagents, and
            MCP trust.

    Returns:
        2-tuple of `(agent_graph, backend)`

            - `agent_graph`: Configured LangGraph Pregel instance ready
                for execution
            - `composite_backend`: `CompositeBackend` for file operations
    """
    # Preserve the original model spec string (used to price street-sweeper
    # savings) before `model` is resolved to a BaseChatModel instance below.
    model_spec_str = model if isinstance(model, str) else ""
    if isinstance(model, str):
        from bog_agents_cli.config import create_model as _create_model

        model = _create_model(model).model

    tools = list(tools or [])
    effective_cwd = (
        Path(cwd)
        if cwd is not None
        else (project_context.user_cwd if project_context is not None else None)
    )

    # User-defined proxy tools — shell-command templates registered via
    # ``/proxy add``. They live in ``~/.bog-agents/proxies.toml`` and are
    # materialised as LangChain StructuredTools at build time. Failure
    # to load any one tool is logged but never blocks agent creation.
    try:
        from bog_agents_cli.proxy_tools import build_proxy_tools

        proxy_tools = build_proxy_tools(cwd=effective_cwd or Path.cwd())
        if proxy_tools:
            tools.extend(proxy_tools)
            logger.info(
                "Loaded %d proxy tools from ~/.bog-agents/proxies.toml",
                len(proxy_tools),
            )
    except Exception:
        logger.warning("Failed to build proxy tools; skipping", exc_info=True)

    # Agent-written auto-memories (#13): a `remember` tool so the agent can
    # proactively persist durable facts (conventions/gotchas/fix-patterns) to
    # the AGENTS.md / ~/.bog-agents/memory.md cascade, auto-recalled next
    # session. Only useful when memory loading is on.
    if enable_memory:
        try:
            from bog_agents_cli.auto_memory import auto_memory_tools

            tools.extend(auto_memory_tools(working_dir=effective_cwd or Path.cwd()))
        except Exception:
            logger.warning("Failed to build auto-memory tool; skipping", exc_info=True)

    # Setup agent directory for persistent memory (if enabled)
    if enable_memory or enable_skills:
        agent_dir = settings.ensure_agent_dir(assistant_id)
        agent_md = agent_dir / "AGENTS.md"
        if not agent_md.exists():
            # Create empty file for user customizations
            # Base instructions are loaded fresh from get_system_prompt()
            agent_md.touch()

    # Skills directories (if enabled)
    skills_dir = None
    user_agent_skills_dir = None
    project_skills_dir = None
    project_agent_skills_dir = None
    if enable_skills:
        # Honor the skill-trust store when SkillsMiddleware lists directories:
        # an explicitly-trusted symlinked skill dir is loaded, everything else
        # is still refused (fail-closed default). Idempotent.
        from bog_agents_cli.skill_trust import install_symlink_trust_hook

        install_symlink_trust_hook()
        skills_dir = settings.ensure_user_skills_dir(assistant_id)
        user_agent_skills_dir = settings.get_user_agent_skills_dir()
        project_skills_dir = (
            project_context.project_skills_dir()
            if project_context is not None
            else settings.get_project_skills_dir()
        )
        project_agent_skills_dir = (
            project_context.project_agent_skills_dir()
            if project_context is not None
            else settings.get_project_agent_skills_dir()
        )

    # Load custom subagents from filesystem
    custom_subagents: list[SubAgent | CompiledSubAgent] = []
    user_agents_dir = settings.get_user_agents_dir(assistant_id)
    project_agents_dir = (
        project_context.project_agents_dir()
        if project_context is not None
        else settings.get_project_agents_dir()
    )

    # Bundled-agents seeding: if the project is Python/Node/Rust/Go and
    # the user hasn't authored their own subagents, this pulls in
    # code-reviewer, test-author, and language-specific specialists from
    # the package's bundled_agents/ tree. User and project subagents
    # override on name conflict.
    #
    # ``BOG_AGENTS_DISABLE_SUBAGENTS=1`` skips subagent loading entirely.
    # The parent agent then has no ``task`` tool and cannot invoke
    # subagents — every step runs at the parent level. This is the
    # workaround for a reliable deadlock where a subagent's
    # HumanInTheLoop interrupt fires inside the parent's pregel
    # invocation, the user approves, and the subagent's resumed model
    # call stalls indefinitely (no httpx request ever leaves the
    # process — it's stuck in the wrap_model_call middleware chain
    # after a HITL resume). Until that root cause is fixed in the
    # framework, this env var lets ``/review`` and similar
    # subagent-heavy commands complete by running the work in the
    # parent agent's context.
    subagents_disabled = os.environ.get(
        "BOG_AGENTS_DISABLE_SUBAGENTS", ""
    ).strip().lower() in ("1", "true", "yes")
    if subagents_disabled:
        logger.warning(
            "BOG_AGENTS_DISABLE_SUBAGENTS=1 — skipping all subagent loading. "
            "Parent agent has no `task` tool; subagent-routed commands "
            "(/review, etc.) will execute directly in the parent context."
        )
    project_root_for_bundled = effective_cwd if effective_cwd is not None else None
    subagent_iter = (
        []
        if subagents_disabled
        else list_subagents(
            user_agents_dir=user_agents_dir,
            project_agents_dir=project_agents_dir,
            project_root=project_root_for_bundled,
        )
    )
    for subagent_meta in subagent_iter:
        subagent: SubAgent = {
            "name": subagent_meta["name"],
            "description": subagent_meta["description"],
            "system_prompt": subagent_meta["system_prompt"],
        }
        if subagent_meta["model"]:
            subagent["model"] = subagent_meta["model"]
        custom_subagents.append(subagent)

    # Build middleware stack based on enabled features
    agent_middleware = []
    agent_middleware.append(ConfigurableModelMiddleware())

    # Auto-enable tool-call parser for Ollama models. Many local models emit
    # tool calls as text (Mistral [TOOL_CALLS], Hermes <tool_call>, fenced
    # JSON) instead of using OpenAI's structured tool_calls field; the parser
    # recovers them so the agent loop can proceed. No-op for cloud providers.
    if (settings.model_provider or "").lower() == "ollama":
        from bog_agents.middleware import ToolCallParserMiddleware

        agent_middleware.append(ToolCallParserMiddleware())

    # Add ask_user middleware (must be early so its tool is available).
    # Skip in non-interactive mode: there is no user to answer, and a stray
    # `ask_user` call mid-run produces a malformed HITL interrupt that the
    # CLI rejects, which derails the agent without recourse. Headless agents
    # should make a best-effort decision and proceed instead.
    if interactive:
        from bog_agents_cli.ask_user import AskUserMiddleware

        agent_middleware.append(AskUserMiddleware())

    # Add memory middleware
    if enable_memory:
        memory_sources = [str(settings.get_user_agent_md_path(assistant_id))]
        if project_context is not None:
            # Walk home → project → ancestor dirs → cwd so the deepest
            # AGENTS.md (closest to the user's actual cwd) is loaded last
            # and gets the most attention from the model.
            hierarchical_paths = project_context.hierarchical_agent_md_paths()
        else:
            hierarchical_paths = list(settings.get_project_agent_md_path())
        memory_sources.extend(str(p) for p in hierarchical_paths)

        agent_middleware.append(
            MemoryMiddleware(
                # virtual_mode=False: memory sources are CLI-controlled absolute paths
                # spanning multiple roots (user home + project), not agent-supplied input.
                backend=FilesystemBackend(virtual_mode=False),
                sources=memory_sources,
            )
        )
        # A `memory_search` tool over the same memory files (Tier-2 #8): lets the
        # agent search its memory for relevant notes instead of relying only on
        # the whole cascade being in context.
        try:
            from bog_agents.tools import memory_search_tool_bundle

            tools.extend(memory_search_tool_bundle(memory_sources))
        except Exception:
            logger.debug("Could not build memory_search tool", exc_info=True)

    # Add skills middleware
    if enable_skills:
        from bog_agents_cli.extensibility import get_extension_skill_dirs

        # Lowest to highest precedence:
        # built-in -> extensions -> user .bog-agents -> user .agents
        # -> project .bog-agents -> project .agents
        extension_config_dir = (
            settings.user_agents_dir
            if isinstance(getattr(settings, "user_agents_dir", None), Path)
            else (
                user_agents_dir.parent
                if user_agents_dir.name == "agents"
                else user_agents_dir
            )
        )
        sources = [str(settings.get_built_in_skills_dir())]
        sources.extend(
            str(path) for path in get_extension_skill_dirs(extension_config_dir)
        )
        sources.extend([str(skills_dir), str(user_agent_skills_dir)])
        if project_skills_dir:
            sources.append(str(project_skills_dir))
        if project_agent_skills_dir:
            sources.append(str(project_agent_skills_dir))

        # Hierarchical skill layering: walk from project root → cwd so a
        # subdirectory can override skills from a shallower .bog-agents/
        # skills directory. The deepest layer loads last, so SkillsMiddleware
        # honours its "last source wins on name conflict" rule.
        if project_context is not None:
            seen_skill_dirs = {Path(s).resolve() for s in sources if Path(s).exists()}
            for hier_dir in project_context.hierarchical_skill_dirs():
                resolved = hier_dir.resolve()
                if resolved in seen_skill_dirs:
                    continue
                seen_skill_dirs.add(resolved)
                sources.append(str(hier_dir))

        agent_middleware.append(
            SkillsMiddleware(
                # virtual_mode=False: skill sources are CLI-controlled absolute paths
                # spanning multiple roots (built-in, extensions, user, project).
                backend=FilesystemBackend(virtual_mode=False),
                sources=sources,
            )
        )

    # CONDITIONAL SETUP: Local vs Remote Sandbox
    if sandbox is None:
        # ========== LOCAL MODE ==========
        root_dir = effective_cwd if effective_cwd is not None else Path.cwd()

        # 0.8.0+ default: filesystem virtual_mode = True. Tools are
        # confined to ``root_dir``; absolute paths outside root and
        # ``..`` traversal are blocked. The old unrestricted behaviour
        # is opt-in via ``BOG_AGENTS_FS_UNSANDBOXED=1`` (cross-repo
        # refactors, reading ``~/.aws/credentials`` for an explicit IaC
        # task, working from a subdir of the repo root).
        #
        # Previously this env var was honoured ONLY in the no-shell
        # branch — shell-mode users had no escape hatch and would hit
        # ``ValueError: Path:X outside root directory`` the moment the
        # agent reached above its cwd. Now it gates both branches.
        unsandboxed = os.environ.get(
            "BOG_AGENTS_FS_UNSANDBOXED", ""
        ).strip().lower() in ("1", "true", "yes")
        if unsandboxed:
            logger.warning(
                "BOG_AGENTS_FS_UNSANDBOXED is set — agent filesystem "
                "tools may read/write outside %s. Use only when you "
                "intentionally want cross-repo or system-wide file "
                "access.",
                root_dir,
            )

        if enable_shell:
            # Create environment for shell commands
            # Restore user's original LANGSMITH_PROJECT so their code traces separately
            shell_env = os.environ.copy()
            if settings.user_langchain_project:
                shell_env["LANGSMITH_PROJECT"] = settings.user_langchain_project

            # Optional OS-level sandbox (#22): when `.bog-agents/sandbox.toml`
            # declares `local_sandbox = "..."`, confine shell commands with
            # bubblewrap/seatbelt and enforce the network allowlist. Opt-in and
            # a safe no-op where no launcher exists (unless require_sandbox).
            local_sandbox = None
            require_sandbox = False
            try:
                from bog_agents.sandbox_config import load_sandbox_config

                sbx_cfg = load_sandbox_config(root_dir)
                if sbx_cfg is not None and sbx_cfg.local_sandbox:
                    local_sandbox = sbx_cfg.build_local_sandbox(root_dir)
                    require_sandbox = sbx_cfg.require_sandbox
            except Exception:
                logger.debug("Could not load local sandbox config", exc_info=True)

            # Auto-background-on-timeout (Tier-1 #1): when
            # BOG_AGENTS_SHELL_AUTO_BACKGROUND_AFTER is a positive number, a
            # foreground command that overruns it is moved to the background
            # instead of killed. Off by default (`off`/`none`/0/unset).
            auto_background_after: float | None = None
            raw_abg = os.environ.get("BOG_AGENTS_SHELL_AUTO_BACKGROUND_AFTER", "").strip().lower()
            if raw_abg and raw_abg not in ("off", "none", "0"):
                try:
                    abg_val = float(raw_abg)
                    auto_background_after = abg_val if abg_val > 0 else None
                except ValueError:
                    logger.debug("Ignoring non-numeric BOG_AGENTS_SHELL_AUTO_BACKGROUND_AFTER=%r", raw_abg)

            # Use LocalShellBackend for filesystem + shell execution.
            # The SDK's FilesystemMiddleware exposes per-command timeout
            # on the execute tool natively. Honour the same
            # ``virtual_mode`` toggle as the no-shell branch.
            backend = LocalShellBackend(
                root_dir=root_dir,
                inherit_env=True,
                env=shell_env,
                virtual_mode=not unsandboxed,
                sandbox=local_sandbox,
                require_sandbox=require_sandbox,
                auto_background_after=auto_background_after,
            )
            # When auto-background is on, give the agent tools to read/wait/kill
            # a detached command (Tier-1 #1).
            if auto_background_after is not None:
                from bog_agents.tools import background_shell_tools_bundle

                tools.extend(background_shell_tools_bundle(backend))
        else:
            # No shell access - use plain FilesystemBackend with the
            # same virtual_mode policy as the shell branch.
            backend = FilesystemBackend(
                root_dir=root_dir,
                virtual_mode=not unsandboxed,
            )
    else:
        # ========== REMOTE SANDBOX MODE ==========
        backend = sandbox  # Remote sandbox (ModalBackend, etc.)
        # Note: Shell middleware not used in sandbox mode
        # File operations and execute tool are provided by the sandbox backend

    # Local context middleware (git info, directory tree, etc.)
    # Uses backend.execute() so it works in both local shell and remote sandbox modes.
    # Only enabled when the backend supports shell execution.
    if isinstance(backend, _ExecutableBackend):
        agent_middleware.append(
            LocalContextMiddleware(backend=backend, mcp_server_info=mcp_server_info)
        )

        # Keep-working Stop gates (Tier-1 #3): BOG_AGENTS_STOP_GATE_CHECKS is a
        # semicolon-separated list of commands that must pass before the agent
        # may end a turn (e.g. "uv run pytest -q; uv run ruff check .").
        stop_checks_raw = os.environ.get("BOG_AGENTS_STOP_GATE_CHECKS", "").strip()
        if stop_checks_raw:
            from bog_agents.middleware.stop_gate import (
                StopGateMiddleware,
                command_stop_check,
            )

            commands = [c.strip() for c in stop_checks_raw.split(";") if c.strip()]
            if commands:
                agent_middleware.append(
                    StopGateMiddleware([command_stop_check(backend, cmd) for cmd in commands])
                )

    # Get or use custom system prompt
    if system_prompt is None:
        system_prompt = get_system_prompt(
            assistant_id=assistant_id,
            sandbox_type=sandbox_type,
            interactive=interactive,
            cwd=effective_cwd,
        )

    # Configure interrupt_on based on auto_approve setting
    interrupt_on: dict[str, bool | InterruptOnConfig] | None = None
    if auto_approve:
        # No interrupts for ordinary tools — but the self-modification guard
        # (#24) still forces approval for a shell command that appears to write
        # an authority file, even unattended. Writes to authority files via the
        # file tools are gated by the interrupt-mode permission rules below,
        # which the SDK merges into interrupt_on regardless of this setting.
        from bog_agents_cli.self_protection import command_targets_authority_file

        interrupt_on = {
            "execute": {
                "allowed_decisions": ["approve", "reject"],
                "description": _format_execute_description,  # type: ignore[typeddict-item]  # Callable description narrower than TypedDict expects
                "when": lambda req: command_targets_authority_file(
                    str(req.tool_call["args"].get("command", ""))
                ),
            }
        }
    else:
        # Full HITL for destructive operations
        interrupt_on = _add_interrupt_on()  # type: ignore[assignment]  # InterruptOnConfig is compatible at runtime

    # Set up composite backend with routing
    # For local FilesystemBackend, route large tool results to a temp directory to avoid
    # polluting the working directory. For sandbox backends, no special routing is needed.
    if sandbox is None:
        # Local mode: Route large results to a unique temp directory
        large_results_backend = FilesystemBackend(
            root_dir=tempfile.mkdtemp(prefix="bog_agents_large_results_"),
            virtual_mode=True,
        )
        conversation_history_backend = FilesystemBackend(
            root_dir=tempfile.mkdtemp(prefix="bog_agents_conversation_history_"),
            virtual_mode=True,
        )
        composite_backend = CompositeBackend(
            default=backend,
            routes={
                "/large_tool_results/": large_results_backend,
                "/conversation_history/": conversation_history_backend,
            },
        )
    else:
        # Sandbox mode: No special routing needed
        composite_backend = CompositeBackend(
            default=backend,
            routes={},
        )

    from bog_agents.middleware.summarization import create_summarization_tool_middleware

    agent_middleware.append(
        create_summarization_tool_middleware(model, composite_backend)
    )

    # Apply profile overrides (if specified)
    if profile:
        from bog_agents_cli.profiles import load_profiles

        profiles = load_profiles(settings.user_agents_dir)
        if profile in profiles:
            p = profiles[profile]
            if p.effort_level:
                effort_level = p.effort_level
            if p.auto_approve is not None:
                auto_approve = p.auto_approve
            if p.enable_git_tools is not None:
                enable_git_tools = p.enable_git_tools
            if p.enable_repo_map is not None:
                enable_repo_map = p.enable_repo_map
            if p.auto_lint is not None:
                auto_lint = p.auto_lint
            if p.auto_test is not None:
                auto_test = p.auto_test

    # Git tools middleware (#15, #43)
    if enable_git_tools and sandbox is None:
        from bog_agents.middleware.git_tools import GitToolsMiddleware

        working_dir = effective_cwd or Path.cwd()
        agent_middleware.append(GitToolsMiddleware(working_dir=working_dir))

    # Repository map middleware (#13)
    if enable_repo_map and sandbox is None:
        from bog_agents.middleware.repo_map import RepoMapMiddleware

        working_dir = effective_cwd or Path.cwd()
        agent_middleware.append(RepoMapMiddleware(working_dir=working_dir))

    # Checkpointing middleware (#3, #5, #39, #43)
    if enable_checkpointing and sandbox is None:
        from bog_agents.middleware.checkpointing import CheckpointingMiddleware

        working_dir = effective_cwd or Path.cwd()
        agent_middleware.append(CheckpointingMiddleware(working_dir=working_dir))

    # Cost tracking middleware (#8, #34, #36, #47)
    if enable_cost_tracking:
        from bog_agents.middleware.cost_tracker import CostTrackerMiddleware

        agent_middleware.append(
            CostTrackerMiddleware(
                effort_level=effort_level,
                budget_usd=budget_usd if budget_usd > 0 else None,
            )
        )

    # Plan mode middleware (#38)
    if enable_plan_mode:
        from bog_agents.middleware.plan_mode import PlanModeMiddleware

        agent_middleware.append(PlanModeMiddleware())

    # Auto quality middleware (#11, #12, #44)
    if auto_lint or auto_test:
        from bog_agents.middleware.auto_quality import AutoQualityMiddleware

        working_dir = effective_cwd or Path.cwd()
        agent_middleware.append(
            AutoQualityMiddleware(
                working_dir=working_dir,
                auto_lint=auto_lint,
                auto_test=auto_test,
            )
        )

    # Extended-thinking middleware — defaults to disabled but always
    # attached so `/think on` works mid-session without a restart. When
    # the user sets ``thinking_enabled = true`` in the provider's
    # ``params`` block (or exports ``BOG_AGENTS_THINKING=1``), it
    # auto-enables for thinking-capable models. The middleware itself
    # is a no-op for models that don't support native thinking — it
    # simply forwards the request unchanged when ``enabled=False``.
    try:
        from bog_agents.middleware.thinking import ThinkingMiddleware

        thinking_enabled_default, thinking_budget = _resolve_thinking_config()
        agent_middleware.append(
            ThinkingMiddleware(
                enabled=thinking_enabled_default,
                budget_tokens=thinking_budget,
            )
        )
        if thinking_enabled_default:
            logger.info("ThinkingMiddleware auto-enabled (budget=%d)", thinking_budget)
    except ImportError:
        logger.debug("ThinkingMiddleware not importable; skipping")

    # Dreamscape middleware stack — entirely opt-in.
    #
    # When `~/.bog-agents/dreamscape.toml` is missing or has
    # `enabled = false`, NONE of these middlewares attach and the
    # agent behaves bit-for-bit identical to a build without the
    # dreamscape package. Per-feature toggles inside the master
    # switch give finer control: someone can enable lifecycle
    # tracking (for the dashboard) without enabling laws enforcement
    # or imagination injection.
    #
    # Wrapped in a try/except so any import/config error here can
    # never block agent creation — observability features must not
    # be load-bearing.
    try:
        from bog_agents_cli.dreamscape import load_dreamscape_config

        dreamscape_cfg = load_dreamscape_config()
        if dreamscape_cfg.any_active:
            _attach_dreamscape_middleware(
                agent_middleware,
                cfg=dreamscape_cfg,
                agent_id=assistant_id,
                system_prompt=system_prompt,
            )
    except Exception:
        logger.warning("dreamscape middleware setup failed; skipping", exc_info=True)

    # Worktree isolation middleware (Feature #1)
    if sandbox is None and enable_git_tools:
        from bog_agents.middleware.worktree import WorktreeMiddleware

        working_dir = effective_cwd or Path.cwd()
        agent_middleware.append(WorktreeMiddleware(working_dir=working_dir))

    # Wave V removed MultiAgentOrchestratorMiddleware (it was a STUB
    # whose import is no longer available). Multi-agent orchestration
    # is handled today by /orchestrate + the sub-agent system, not by
    # a dedicated middleware.

    # Smart context middleware (Features #13-18)
    from bog_agents.middleware.smart_context import SmartContextMiddleware

    working_dir = effective_cwd or Path.cwd()
    agent_middleware.append(SmartContextMiddleware(working_dir=working_dir))

    # Conversation branching middleware (Features #14, #16)
    from bog_agents.middleware.conversation_branch import ConversationBranchMiddleware

    agent_middleware.append(ConversationBranchMiddleware(working_dir=working_dir))

    # Image input middleware (Features #19-23)
    from bog_agents.middleware.image_input import ImageInputMiddleware

    agent_middleware.append(ImageInputMiddleware(working_dir=working_dir))

    # Browser agent middleware (Features #24-27)
    from bog_agents.middleware.browser_agent import BrowserAgentMiddleware

    agent_middleware.append(BrowserAgentMiddleware(working_dir=working_dir))

    # PR management middleware (Features #28-34)
    if sandbox is None:
        from bog_agents.middleware.pr_management import PRManagementMiddleware

        agent_middleware.append(PRManagementMiddleware(working_dir=working_dir))

    # Test generation middleware (Features #35-38, 40)
    from bog_agents.middleware.test_generation import TestGenerationMiddleware

    agent_middleware.append(TestGenerationMiddleware(working_dir=working_dir))

    # Enterprise middleware (Features #51-57)
    from bog_agents.middleware.enterprise import EnterpriseMiddleware

    agent_middleware.append(EnterpriseMiddleware(working_dir=working_dir))

    # Multi-model middleware (Features #58, #72, #73)
    from bog_agents.middleware.multi_model import MultiModelMiddleware

    agent_middleware.append(MultiModelMiddleware())

    # Code intelligence middleware (Features #59-75)
    from bog_agents.middleware.code_intelligence import CodeIntelligenceMiddleware

    agent_middleware.append(CodeIntelligenceMiddleware(working_dir=working_dir))

    # Plugin system middleware (Features #7-12)
    from bog_agents.middleware.plugin_system import PluginSystemMiddleware

    agent_middleware.append(PluginSystemMiddleware())

    # Notifications middleware (Features #42-47, 49)
    from bog_agents.middleware.notifications import NotificationsMiddleware

    agent_middleware.append(NotificationsMiddleware())

    # Goal tools — a durable objective + acceptance-criteria rubric that survive
    # across turns and are re-injected into the system prompt every model call.
    # Carries no configuration and is safe to include unconditionally: with no
    # goal set, get_goal/get_rubric report empty and the injected prompt is just
    # the static guidance. The CLI's /goal and /rubric commands seed the goal
    # channels (see goal_controller.state_seed); the agent's update_goal tool
    # records progress back into the same checkpointed state.
    from bog_agents.middleware import GoalToolsMiddleware

    agent_middleware.append(GoalToolsMiddleware())

    # Bedrock resilience — only attached when a Bedrock model is in use, to
    # keep the middleware list lean for everyone else. NOTE: by this point
    # `model` has already been resolved from a `provider:model` string to a
    # `BaseChatModel` instance (above), so an `isinstance(model, str)` check
    # is always False on the live path — detect Bedrock from the resolved
    # model instead. (Previously the string check meant NEITHER Bedrock
    # middleware ever attached on the live server path.)
    from bog_agents_cli.bedrock_resilience import is_bedrock_chat_model

    model_is_bedrock = (
        isinstance(model, str) and model.startswith(("bedrock:", "bedrock_converse:"))
    ) or (not isinstance(model, str) and is_bedrock_chat_model(model))
    if model_is_bedrock:
        from bog_agents_cli.bedrock_refresh import BedrockRefreshMiddleware
        from bog_agents_cli.bedrock_resilience import BedrockResilienceMiddleware

        bedrock_interactive = sys.stdin.isatty()
        # Order is load-bearing: earlier in the list == OUTER wrapper.
        # Resilience (outer) categorizes any failure and falls back to a
        # hittable model / emits a friendly diagnosis. Refresh (inner) gets
        # first crack at an expired-SSO failure (runs `aws sso login` and
        # retries) before resilience would otherwise surface it. See
        # docs/providers/bedrock.md, bog_agents_cli.bedrock_resilience, and
        # bog_agents_cli.bedrock_refresh.
        agent_middleware.append(
            BedrockResilienceMiddleware(interactive=bedrock_interactive)
        )
        agent_middleware.append(
            BedrockRefreshMiddleware(interactive=bedrock_interactive)
        )

    # Street sweeper (opt-in) — attach the per-cwd singleton instance, disabled
    # by default. `/sweep on` flips it live without a rebuild. Pointing it at the
    # live composite backend enables offload + the recall_swept tool. Wrapped so
    # an attach failure can never block agent creation.
    try:
        from bog_agents_cli.sweep_controller import get_sweep_controller

        sweeper = get_sweep_controller(effective_cwd or Path.cwd()).middleware
        sweeper.set_backend(composite_backend)
        sweeper.set_pricing(model_spec_str)
        agent_middleware.append(sweeper)
    except Exception:
        logger.debug("street sweeper attach failed; skipping", exc_info=True)

    # Create the agent
    # Self-modification guard (#24): gate writes to the agent's own authority
    # files (Expert rules, dreamscape laws, hooks, .mcp.json) behind human
    # approval. These interrupt-mode rules are merged into interrupt_on by
    # create_agent even under --auto-approve, so the guard can't be bypassed.
    from bog_agents_cli.self_protection import authority_file_permissions

    agent = create_agent(
        model=model,
        system_prompt=system_prompt,
        tools=tools,
        backend=composite_backend,
        middleware=agent_middleware,
        interrupt_on=interrupt_on,
        permissions=authority_file_permissions(),
        checkpointer=checkpointer,
        subagents=custom_subagents or None,
    ).with_config(config)
    return agent, composite_backend
