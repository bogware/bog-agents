"""`/tokens middleware` — the CLI agent's fixed per-turn cost, attributed (ROADMAP #54).

The TUI's agent lives in a separate LangGraph server process, so the audit
rebuilds the same stack locally through `create_cli_agent` around the SDK's
`RecordingChatModel` (no provider call, no checkpointer) and runs one probe
turn. `bog_agents.token_audit` does the measuring and the per-middleware
attribution; this module only supplies the CLI's build recipe and the widget
plumbing. Also the headless twin: `bog-agents command "tokens middleware"`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bog_agents.token_audit import audit_agent

if TYPE_CHECKING:
    from bog_agents.token_audit import TokenAudit
    from langchain_core.language_models import BaseChatModel

LEAN_PROFILE = "lean"


def audit_cli_agent(
    *,
    harness_profile: str | None,
    cwd: str | Path,
    assistant_id: str = "agent",
    auto_approve: bool = True,
    effort_level: str = "high",
    profile: str = "",
    enable_plan_mode: bool = True,
    method: str = "auto",
    restricted: bool = False,
) -> TokenAudit:
    """Build the CLI agent the way the TUI does and audit one turn of it.

    Args:
        harness_profile: SDK harness profile key (`"lean"` for `--mini`) or `None` for the default.
        cwd: Working directory the agent is built for (rules, skills and memory resolve from it).
        assistant_id: Agent identifier.
        auto_approve: Whether the HITL layer is disabled (changes the stack, so mirror the session).
        effort_level: Effort level the session runs at.
        profile: Configuration profile name (`create_cli_agent(profile=...)`).
        enable_plan_mode: Whether plan mode middleware is attached.
        method: Token counting method (see `bog_agents.token_audit.count_tokens`).
        restricted: Build under the `--restricted` trust profile (ROADMAP #48).

    Returns:
        The audit.
    """
    from bog_agents_cli.agent import create_cli_agent

    def _build(model: BaseChatModel) -> Any:  # noqa: ANN401 - compiled graph tuple
        return create_cli_agent(
            model=model,
            assistant_id=assistant_id,
            auto_approve=auto_approve,
            enable_plan_mode=enable_plan_mode,
            effort_level=effort_level,
            profile=profile,
            cwd=cwd,
            harness_profile=harness_profile,
            restricted=restricted,
        )

    return audit_agent(_build, method=method)


def render_cli_audit(audit: TokenAudit, *, harness_profile: str | None) -> str:
    """The report with a one-line header naming the profile that was measured."""
    label = harness_profile or "default"
    hint = (
        "switch with --mini"
        if not harness_profile
        else "the default profile costs more; drop --mini to compare"
    )
    return f"Profile: {label} ({hint})\n{audit.render()}"


async def run_middleware_audit(app: Any) -> None:  # noqa: ANN401 - the App
    """Body of `/tokens middleware`: measure in a worker thread, then mount the report."""
    from bog_agents_cli.widgets.messages import AppMessage, ErrorMessage

    harness_profile = getattr(app, "_harness_profile", None)
    await app._mount_message(
        AppMessage("Measuring the harness with a recording model (no provider call)...")
    )
    try:
        audit = await asyncio.to_thread(
            audit_cli_agent,
            harness_profile=harness_profile,
            cwd=getattr(app, "_cwd", None) or Path.cwd(),
            assistant_id=getattr(app, "_assistant_id", "agent") or "agent",
            auto_approve=bool(getattr(app, "_auto_approve", False)),
            effort_level=getattr(app, "_effort_level", "high") or "high",
            profile=getattr(app, "_active_profile_name", "") or "",
            enable_plan_mode=bool(getattr(app, "_plan_mode_enabled", True)),
        )
    except Exception as exc:  # a broken audit must not take the TUI down
        await app._mount_message(ErrorMessage(f"/tokens middleware failed: {exc}"))
        return
    await app._mount_message(
        AppMessage(render_cli_audit(audit, harness_profile=harness_profile))
    )
