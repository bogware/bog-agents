"""Repo-config acknowledgement gate (ROADMAP #49).

`bog_agents.git_env.scan_repo_config` says what a cloned repo's `.git/config`
would execute; every internal git call is already hardened against it, but
`/diff`, `/review` and `/pr` still read the repo on the user's behalf, so they
stay blocked until the user has seen the findings once per config fingerprint
(`/permissions trust-git-config`). The store mirrors `mcp_trust.py`:
`~/.bog-agents/repo_trust.json` keyed by resolved repo path.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from bog_agents.git_env import (
    format_findings,
    repo_config_fingerprint,
    scan_repo_config,
)

logger = logging.getLogger(__name__)

STORE_NAME = "repo_trust.json"


def store_path() -> Path:
    """`~/.bog-agents/repo_trust.json`."""
    from bog_agents_cli._env_vars import bog_agents_home

    return bog_agents_home() / STORE_NAME


def _key(repo_dir: str | Path) -> str:
    return str(Path(repo_dir).resolve()).replace("\\", "/").lower()


def _load(path: Path) -> dict[str, dict[str, str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"repos": {}}
    if not isinstance(data, dict) or not isinstance(data.get("repos"), dict):
        return {"repos": {}}
    return data


def is_repo_config_acknowledged(
    repo_dir: str | Path, *, path: Path | None = None
) -> bool:
    """Whether the repo's *current* config fingerprint was acknowledged."""
    fingerprint = repo_config_fingerprint(repo_dir)
    if not fingerprint:
        return True
    return _load(path or store_path())["repos"].get(_key(repo_dir)) == fingerprint


def acknowledge_repo_config(repo_dir: str | Path, *, path: Path | None = None) -> str:
    """Record the current fingerprint as acknowledged; returns it (`""` when no config)."""
    from bog_agents_cli.io_utils import atomic_write_text

    fingerprint = repo_config_fingerprint(repo_dir)
    if not fingerprint:
        return ""
    target = path or store_path()
    data = _load(target)
    data["repos"][_key(repo_dir)] = fingerprint
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target, json.dumps(data, indent=2))
    return fingerprint


def repo_config_gate(repo_dir: str | Path, *, path: Path | None = None) -> str | None:
    """Message that blocks a review surface, or `None` when the repo config is clean or acknowledged."""
    try:
        findings = scan_repo_config(repo_dir)
    except Exception:  # never wedge a command on a scan error
        logger.debug("repo config scan failed", exc_info=True)
        return None
    if not findings or is_repo_config_acknowledged(repo_dir, path=path):
        return None
    return (
        "This repository's own .git/config names programs git would run "
        f"({len(findings)} entr{'y' if len(findings) == 1 else 'ies'}):\n"
        f"{format_findings(findings)}\n"
        "bog runs every internal git command with those keys neutralised, but review the list, then "
        "`/permissions trust-git-config` to unblock /diff, /review and /pr for this config."
    )
