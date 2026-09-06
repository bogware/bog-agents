"""Workspace trust: one fingerprint over everything a cloned repo can use to steer the agent (ROADMAP #48).

`mcp_trust.py` already records trust for a project's `.mcp.json` and hook
scripts, each at its own fingerprint. This module adds the umbrella: a single
fingerprint over the repo-controlled instruction and policy files
(`.bog-agents/**` config, `AGENTS.md`, `CLAUDE.md`, `.claude/**`,
`.cursor/**`, `.agents/**`, `.mcp.json`, `.github/workflows/**`) stored in
the same config file under `workspace_trust`. `/permissions trust-workspace`
records it and, in the same step, trusts the project's hooks and MCP servers
at their current fingerprints — so one acknowledgement covers the whole
first-open decision, and any later change to those files shows up as
"changed since you trusted it".
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from bog_agents_cli.mcp_trust import (
    _load_config,
    _save_config,
    compute_config_fingerprint,
)

logger = logging.getLogger(__name__)

WORKSPACE_TRUST_KEY = "workspace_trust"
TRUSTED_GLOBS: tuple[str, ...] = (
    "AGENTS.md",
    "CLAUDE.md",
    ".mcp.json",
    ".bog-agents/*.md",
    ".bog-agents/*.toml",
    ".bog-agents/*.json",
    ".bog-agents/rules/**/*",
    ".bog-agents/expert_rules/**/*",
    ".bog-agents/hooks/**/*",
    ".bog-agents/skills/**/*",
    ".bog-agents/agents/**/*",
    ".bog-agents/workflows/**/*",
    ".claude/**/*",
    ".cursor/**/*",
    ".agents/**/*",
    ".github/workflows/*",
)
"""Repo-controlled files that can change what the agent does; symlinks and directories are skipped."""
_MAX_FILES = 2000


def workspace_files(project_root: Path) -> list[Path]:
    """The files the fingerprint covers (sorted, capped so a giant tree cannot stall start-up)."""
    root = Path(project_root)
    found: set[Path] = set()
    for pattern in TRUSTED_GLOBS:
        for path in root.glob(pattern):
            if path.is_file() and not path.is_symlink():
                found.add(path)
            if len(found) >= _MAX_FILES:
                break
    return sorted(found)


def workspace_fingerprint(project_root: Path) -> str:
    """`sha256:<hex>` over the covered files (stable while none of them changes)."""
    return compute_config_fingerprint(workspace_files(project_root))


def _key(project_root: Path) -> str:
    return str(Path(project_root).resolve())


def trusted_fingerprint(
    project_root: Path, *, config_path: Path | None = None
) -> str | None:
    """The fingerprint the user trusted for this project, or `None`."""
    data = (
        _load_config(config_path) if config_path is not None else _load_config_default()
    )
    projects = data.get(WORKSPACE_TRUST_KEY, {}).get("projects", {})
    value = projects.get(_key(project_root)) if isinstance(projects, dict) else None
    return str(value) if value else None


def _load_config_default() -> dict[str, Any]:
    from bog_agents_cli.mcp_trust import _DEFAULT_CONFIG_PATH

    return _load_config(_DEFAULT_CONFIG_PATH)


def is_workspace_trusted(
    project_root: Path, *, config_path: Path | None = None
) -> bool:
    """Whether the project is trusted at its *current* fingerprint."""
    return trusted_fingerprint(
        project_root, config_path=config_path
    ) == workspace_fingerprint(project_root)


def trust_workspace(project_root: Path, *, config_path: Path | None = None) -> str:
    """Record trust at the current fingerprint and trust the hooks and MCP servers with it; returns the fingerprint."""
    from bog_agents_cli.mcp_trust import (
        _DEFAULT_CONFIG_PATH,
        trust_project_hooks,
        trust_project_mcp,
    )

    path = config_path or _DEFAULT_CONFIG_PATH
    fingerprint = workspace_fingerprint(project_root)
    data = _load_config(path)
    section = data.setdefault(WORKSPACE_TRUST_KEY, {})
    projects = section.setdefault("projects", {})
    projects[_key(project_root)] = fingerprint
    _save_config(data, path)
    root = Path(project_root)
    try:
        from bog_agents_cli.project_hooks import hooks_fingerprint

        trust_project_hooks(_key(root), hooks_fingerprint(root), config_path=path)
    except (
        Exception
    ):  # hooks trust is best effort; the workspace record is the contract
        logger.debug(
            "Could not trust project hooks alongside the workspace", exc_info=True
        )
    mcp_file = root / ".mcp.json"
    if mcp_file.is_file():
        try:
            trust_project_mcp(
                _key(root), compute_config_fingerprint([mcp_file]), config_path=path
            )
        except Exception:
            logger.debug(
                "Could not trust project MCP alongside the workspace", exc_info=True
            )
    return fingerprint


def revoke_workspace_trust(
    project_root: Path, *, config_path: Path | None = None
) -> bool:
    """Forget the workspace record (hooks / MCP trust stay as they are); `True` when something was removed."""
    from bog_agents_cli.mcp_trust import _DEFAULT_CONFIG_PATH

    path = config_path or _DEFAULT_CONFIG_PATH
    data = _load_config(path)
    projects = data.get(WORKSPACE_TRUST_KEY, {}).get("projects", {})
    if not isinstance(projects, dict) or _key(project_root) not in projects:
        return False
    del projects[_key(project_root)]
    _save_config(data, path)
    return True


def workspace_status(project_root: Path, *, config_path: Path | None = None) -> str:
    """One line for `/permissions`: trusted / changed since trusted / never trusted."""
    current = workspace_fingerprint(project_root)
    recorded = trusted_fingerprint(project_root, config_path=config_path)
    count = len(workspace_files(project_root))
    if recorded is None:
        return f"Workspace trust: never acknowledged ({count} repo-controlled file(s)); /permissions trust-workspace records it"
    if recorded == current:
        return f"Workspace trust: trusted ({count} file(s), {current[7:19]})"
    return f"Workspace trust: CHANGED since you trusted it ({count} file(s) now {current[7:19]}); re-run /permissions trust-workspace after reviewing"


__all__ = [
    "TRUSTED_GLOBS",
    "is_workspace_trusted",
    "revoke_workspace_trust",
    "trust_workspace",
    "trusted_fingerprint",
    "workspace_files",
    "workspace_fingerprint",
    "workspace_status",
]
