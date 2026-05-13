"""Read-only dashboard surfaces for the dreamscape feature set.

* ``/agent-state`` — lifecycle + imagination + recent dreams + recent
  shared-memory posts. Always safe to run; reads disk only.
* ``/repo`` — a quick "what is this checkout?" summary. Branch, dirty
  files, top-edited files this week, clone command. Also pure-read.
* ``/dreamscape`` — show/edit/init the dreamscape config.
* ``/laws`` — audit a sample against current Laws+Constitution; render
  starter files.

All of these load the dreamscape config to read state but never
*write* runtime state — they're observability tools, not action
tools. Each handler is wrapped in try/except so a missing dependency
doesn't crash the CLI.
"""

from __future__ import annotations

import logging
import os
import subprocess  # noqa: S404 — controlled git/clone introspection only
import time
from pathlib import Path

from bog_agents_cli.dreamscape.config import (
    DreamscapeConfig,
    dreamscape_config_path,
    load_dreamscape_config,
    save_dreamscape_config,
)
from bog_agents_cli.dreamscape.dream_engine import list_agent_dreams
from bog_agents_cli.dreamscape.laws import (
    DEFAULT_CONSTITUTION_TEMPLATE,
    DEFAULT_LAWS_TEMPLATE,
    audit_text,
    write_default_templates,
)
from bog_agents_cli.dreamscape.lifecycle import (
    compute_state,
    load_snapshot,
)
from bog_agents_cli.dreamscape.shared_memory import (
    NoopSharedMemory,
    build_backend,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# /agent-state
# ---------------------------------------------------------------------------


def render_agent_state(
    agent_id: str = "default", *, cfg: DreamscapeConfig | None = None
) -> str:
    """Render a Rich-markup overview of the current agent's state."""
    config = cfg or load_dreamscape_config()
    try:
        snap = load_snapshot(agent_id)
    except Exception:
        logger.exception("/agent-state: snapshot load failed")
        return "[red]Could not load agent state — see logs for detail.[/red]"
    state = compute_state(snap, config.lifecycle)

    lines: list[str] = [
        f"[bold]Agent state — {agent_id}[/bold]",
        "",
        f"  Lifecycle: [cyan]{state.value}[/cyan]",
        f"  Imagination: [magenta]{snap.imagination:.2f}[/magenta]",
        f"  Total dreams: {snap.total_dreams}",
        f"  Consecutive tool failures: {snap.consecutive_tool_failures}",
    ]
    if snap.last_activity_at:
        age = max(0, time.time() - snap.last_activity_at)
        lines.append(f"  Last activity: {_pretty_age(age)} ago")
    if snap.last_dream_at:
        age = max(0, time.time() - snap.last_dream_at)
        lines.append(f"  Last dream: {_pretty_age(age)} ago")
    if snap.imagination_injections:
        ratio = snap.imagination_injections_helped / max(1, snap.imagination_injections)
        lines.append(
            f"  Imagination injections: {snap.imagination_injections} "
            f"(helped {snap.imagination_injections_helped}, "
            f"success rate {ratio:.0%})"
        )

    lines.append("")
    lines.append("[bold]Config[/bold]")
    lines.append(f"  master_enabled: {config.master_enabled}")
    lines.append("  features active: " + _active_feature_list(config))

    dreams = list_agent_dreams(agent_id, limit=5)
    if dreams:
        lines.append("")
        lines.append("[bold]Recent dreams[/bold]")
        for path in dreams:
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(path.stat().st_mtime))
            title = _read_dream_title(path)
            lines.append(f"  [bold]{title}[/bold]  [dim]{ts}[/dim]")
    elif config.master_enabled and config.dreams.auto_on_dormancy:
        lines.append("")
        lines.append(
            "[dim]No dreams yet — agent has not been dormant long enough.[/dim]"
        )

    if config.master_enabled and config.shared_memory.enabled:
        try:
            backend = build_backend(config.shared_memory)
            if not isinstance(backend, NoopSharedMemory):
                recent = backend.recent(limit=5)
                if recent:
                    lines.append("")
                    lines.append("[bold]Recent shared-memory entries[/bold]")
                    for entry in recent:
                        excerpt = entry.content[:120]
                        lines.append(f"  ({entry.agent_id}) {excerpt}")
        except Exception:
            logger.exception("/agent-state: shared-memory read failed")

    return "\n".join(lines)


def _pretty_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds / 60)}m"
    if seconds < 86400:
        return f"{int(seconds / 3600)}h"
    return f"{int(seconds / 86400)}d"


def _active_feature_list(cfg: DreamscapeConfig) -> str:
    if not cfg.master_enabled:
        return "[dim]none (master_enabled = false)[/dim]"
    active: list[str] = []
    if cfg.lifecycle.enabled:
        active.append("lifecycle")
    if cfg.laws.enabled:
        active.append("laws")
    if cfg.shared_memory.enabled:
        active.append("shared-memory")
    if cfg.dreams.auto_on_dormancy:
        active.append("dreams-auto")
    if cfg.imagination.enabled:
        active.append("imagination")
    if not active:
        return "[dim]none[/dim]"
    return ", ".join(active)


def _read_dream_title(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return path.name
    for line in text.splitlines():
        if line.startswith("### "):
            return line[4:].strip()
    return path.name


# ---------------------------------------------------------------------------
# /repo
# ---------------------------------------------------------------------------


def render_repo_overview(cwd: Path | None = None) -> str:
    """A quick "what is this checkout?" summary."""
    work_dir = str(cwd) if cwd is not None else None

    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], work_dir) or "(no branch)"
    head = _git(["rev-parse", "HEAD"], work_dir) or "(no HEAD)"
    head_short = head[:12]
    origin = _git(["remote", "get-url", "origin"], work_dir)

    status_raw = _git(["status", "--porcelain"], work_dir)
    modified = []
    untracked = []
    if status_raw:
        for line in status_raw.splitlines():
            if len(line) < 4:
                continue
            if line[:2].startswith("??"):
                untracked.append(line[3:].strip())
            else:
                modified.append(line[3:].strip())

    # Top-edited files in the last 14 days.
    top_files_raw = _git(
        [
            "log",
            "--since=14.days.ago",
            "--pretty=format:",
            "--name-only",
        ],
        work_dir,
    )
    top_files: dict[str, int] = {}
    if top_files_raw:
        for line in top_files_raw.splitlines():
            name = line.strip()
            if not name:
                continue
            top_files[name] = top_files.get(name, 0) + 1
    top = sorted(top_files.items(), key=lambda x: -x[1])[:10]

    lines = [
        "[bold]Repository overview[/bold]",
        "",
        f"  Branch:   [cyan]{branch}[/cyan]",
        f"  HEAD:     [dim]{head_short}[/dim]",
    ]
    if origin:
        lines.append(f"  Origin:   {origin}")
    lines.append("")
    if modified:
        lines.append(f"[bold]Modified ({len(modified)})[/bold]")
        for m in modified[:15]:
            lines.append(f"  M {m}")
        if len(modified) > 15:
            lines.append(f"  [dim]…and {len(modified) - 15} more[/dim]")
    if untracked:
        lines.append("")
        lines.append(f"[bold]Untracked ({len(untracked)})[/bold]")
        for u in untracked[:8]:
            lines.append(f"  ? {u}")

    if top:
        lines.append("")
        lines.append("[bold]Top-edited files (last 14 days)[/bold]")
        for name, count in top:
            lines.append(f"  {count:>3}× {name}")

    if origin:
        lines.append("")
        lines.append(f"[bold]Clone elsewhere[/bold]\n  [cyan]git clone {origin}[/cyan]")
    return "\n".join(lines)


def _git(args: list[str], cwd: str | None) -> str:
    """Best-effort git, like ``feature_helpers._git`` but local copy."""
    try:
        result = subprocess.run(  # noqa: S603 — controlled argv
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env={
                **os.environ,
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_OPTIONAL_LOCKS": "0",
            },
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


# ---------------------------------------------------------------------------
# /dreamscape
# ---------------------------------------------------------------------------


def render_dreamscape_status() -> str:
    """Render the full dreamscape config as a markdown table."""
    cfg = load_dreamscape_config()
    path = dreamscape_config_path()
    exists = path.exists()
    lines = [
        f"[bold]Dreamscape configuration[/bold] — [cyan]{path}[/cyan] "
        f"({'present' if exists else '[dim]missing — defaults in effect[/dim]'})",
        "",
        f"  Master switch: {'[green]ON[/green]' if cfg.master_enabled else '[dim]OFF[/dim]'}",
        "",
    ]
    sections = (
        (
            "Lifecycle",
            cfg.lifecycle.enabled,
            f"dormancy_after={cfg.lifecycle.dormancy_after_seconds}s",
        ),
        (
            "Laws",
            cfg.laws.enabled,
            f"reject_on_violation={cfg.laws.reject_on_violation}",
        ),
        (
            "Shared memory",
            cfg.shared_memory.enabled,
            f"backend={cfg.shared_memory.backend}",
        ),
        (
            "Dreams auto",
            cfg.dreams.auto_on_dormancy,
            f"model={cfg.dreams.model or 'active'}",
        ),
        (
            "Imagination",
            cfg.imagination.enabled,
            f"trigger@{cfg.imagination.trigger_after_failures}fails",
        ),
        ("Dashboard", cfg.dashboard.enabled, "always safe"),
    )
    for name, on, detail in sections:
        flag = "[green]ON[/green]" if on else "[dim]off[/dim]"
        lines.append(f"  {name:<18} {flag}  [dim]{detail}[/dim]")
    lines.append("")
    lines.append(
        "[dim]Edit ~/.bog-agents/dreamscape.toml to change. "
        "Set BOG_AGENTS_DREAMSCAPE_DISABLE=1 to kill everything at runtime.[/dim]"
    )
    return "\n".join(lines)


def init_dreamscape_config(*, overwrite: bool = False) -> Path:
    """Write a starter dreamscape config (still master-OFF)."""
    cfg = load_dreamscape_config(use_cache=False)
    path = dreamscape_config_path()
    if path.exists() and not overwrite:
        msg = f"{path} already exists — pass overwrite=True to replace it"
        raise FileExistsError(msg)
    save_dreamscape_config(cfg, path=path)
    return path


# ---------------------------------------------------------------------------
# /laws
# ---------------------------------------------------------------------------


def render_laws_audit(sample: str) -> str:
    """Render an audit of ``sample`` against the configured Laws+Constitution."""
    cfg = load_dreamscape_config()
    result = audit_text(sample, cfg.laws)
    lines = [
        "[bold]Laws audit[/bold]",
        "",
        f"  Laws on file:         {result.laws_found}",
        f"  Constitution on file: {result.constitution_found}",
        "",
    ]
    if not result.laws_found and not result.constitution_found:
        lines.append(
            "[dim]No rules configured. Run [bold]/laws init[/bold] to write "
            "starter files.[/dim]"
        )
        return "\n".join(lines)
    if result.violations:
        lines.append("[bold]Sample triggers:[/bold]")
        for v in result.violations:
            lines.append(f"  [red]✗[/red] {v}")
    else:
        lines.append("[green]Sample triggers no configured rules.[/green]")
    return "\n".join(lines)


def init_laws_templates(*, overwrite: bool = False) -> list[Path]:
    """Write starter Laws + Constitution files. Returns the paths."""
    cfg = load_dreamscape_config()
    return write_default_templates(cfg.laws, overwrite=overwrite)


# Re-exports — keep the templates importable without depending on the
# laws module for callers that only want the wizard text.
__all__ = [
    "DEFAULT_CONSTITUTION_TEMPLATE",
    "DEFAULT_LAWS_TEMPLATE",
    "init_dreamscape_config",
    "init_laws_templates",
    "render_agent_state",
    "render_dreamscape_status",
    "render_laws_audit",
    "render_repo_overview",
]
