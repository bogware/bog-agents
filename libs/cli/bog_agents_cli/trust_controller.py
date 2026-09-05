"""Glue between `/permissions`, the permission-mode keys and the trust modules (ROADMAP #48).

Pure functions the App calls off-thread: the verb handlers behind
`/permissions trust-git-config | trust-workspace | revoke-workspace`, the trust
rows of the `/permissions` report, and the one question the mode keys ask
before changing the permission mode — "does the trust profile allow this?".
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def run_permissions_verb(verb: str, cwd: str) -> str | None:
    """Run a `/permissions <verb>`; `None` when `verb` is not one of ours."""
    root = Path(cwd)
    if verb == "trust-git-config":
        from bog_agents_cli.repo_trust import acknowledge_repo_config

        fingerprint = acknowledge_repo_config(cwd)
        return f"Acknowledged this repo's git config ({fingerprint[:19] or 'no config'}); /diff, /review and /pr are unblocked until it changes."
    if verb == "trust-workspace":
        from bog_agents_cli.workspace_trust import trust_workspace, workspace_files

        fingerprint = trust_workspace(root)
        count = len(workspace_files(root))
        return (
            f"Trusted this workspace at {fingerprint[7:19]} ({count} repo-controlled file(s)); "
            "project hooks and MCP servers are trusted at their current fingerprints too. "
            "Any change to those files shows up here as 'CHANGED since you trusted it'."
        )
    if verb == "revoke-workspace":
        from bog_agents_cli.workspace_trust import revoke_workspace_trust

        if revoke_workspace_trust(root):
            return "Workspace trust revoked (hook and MCP trust are unchanged; use /hooks and /mcp to revoke those)."
        return "This workspace was not trusted."
    return None


def _web_policy_row(
    profile_allowed: tuple[str, ...], profile_blocked: tuple[str, ...]
) -> str:
    from bog_agents_cli.config_manifest import resolve_option
    from bog_agents_cli.web_policy import WebPolicy, policy_from_strings

    policy = policy_from_strings(
        resolve_option("web.allowed_domains"), resolve_option("web.blocked_domains")
    ).merged(
        WebPolicy(allowed_domains=profile_allowed, blocked_domains=profile_blocked)
    )
    if not policy.active:
        return "Web domains: any public host (set web.allowed_domains / web.blocked_domains to narrow)"
    parts = []
    if policy.allowed_domains:
        parts.append("allowed " + ", ".join(policy.allowed_domains))
    if policy.blocked_domains:
        parts.append("blocked " + ", ".join(policy.blocked_domains))
    return "Web domains: " + "; ".join(parts)


def trust_rows(cwd: str, restricted: bool, profile_name: str | None) -> list[str]:
    """The trust section of the `/permissions` report."""
    from bog_agents_cli.config import settings
    from bog_agents_cli.trust_profiles import resolve_trust_profile
    from bog_agents_cli.workspace_trust import workspace_status

    rows: list[str] = []
    allowed: tuple[str, ...] = ()
    blocked: tuple[str, ...] = ()
    try:
        profile = resolve_trust_profile(
            restricted=restricted,
            profile_name=profile_name or "",
            config_dir=settings.user_agents_dir,
        )
        allowed, blocked = profile.allowed_domains, profile.blocked_domains
        rows.extend(profile.describe())
    except Exception as exc:
        rows.append(f"Trust profile: unreadable ({exc})")
    try:
        rows.append(_web_policy_row(allowed, blocked))
    except Exception:
        logger.debug("Could not describe the web policy", exc_info=True)
    try:
        rows.append(workspace_status(Path(cwd)))
    except Exception as exc:
        rows.append(f"Workspace trust: unavailable ({exc})")
    return rows


def mode_refusal(
    mode: str, *, restricted: bool, profile_name: str | None
) -> str | None:
    """Why the trust profile refuses switching to `mode`, or `None` when it may proceed."""
    from bog_agents_cli.config import settings
    from bog_agents_cli.trust_profiles import mode_change_refusal, resolve_trust_profile

    try:
        profile = resolve_trust_profile(
            restricted=restricted,
            profile_name=profile_name or "",
            config_dir=settings.user_agents_dir,
        )
    except Exception:
        logger.warning(
            "Trust profile unreadable; permission mode change not gated", exc_info=True
        )
        return None
    refusal = mode_change_refusal(profile, mode)
    return refusal[0].upper() + refusal[1:] if refusal else None


__all__ = ["mode_refusal", "run_permissions_verb", "trust_rows"]
