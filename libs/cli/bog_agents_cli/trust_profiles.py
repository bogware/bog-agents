"""Trust profiles and `--restricted` (ROADMAP #48).

A trust profile is the policy half of a CLI profile: which permission mode the
session runs in, whether that mode may be lowered from the keyboard, the
sandbox level and egress allowlist, and the web domains the agent may fetch.
It lives under `custom_settings.trust` of a `profiles.json` entry, so no
schema change; `restricted_profile()` is the built-in preset behind
`bog-agents --restricted`: no shell, no git tools, no daemon hand-offs, no
approval bypass, and every outbound fetch blocked unless a domain allowlist
says otherwise. Pure logic — `create_cli_agent` and the App consult it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PERMISSION_MODES = ("default", "accept-edits", "plan", "bypass", "paranoid")
"""Modes `_apply_permission_mode` understands, from most to least prompting-heavy is not the order — see `mode_rank`."""

RESTRICTED_TOOL_NAMES: frozenset[str] = frozenset(
    {
        # shells
        "execute",
        "powershell",
        # raw network (fetch_url is kept only with a domain allow-list)
        "http_request",
        "web_search",
        "api_request",
        "web_fetch",
        # daemon hand-offs
        "schedule",
        "subscribe",
        "unsubscribe",
        "list_subscriptions",
        # git / PR management (spawns git and gh)
        "create_pr",
        "list_prs",
        "pr_comments",
        "add_pr_comment",
        "auto_pr_description",
        "resolve_conflicts",
        "git_bisect_start",
        "git_bisect_step",
        "git_bisect_reset",
        # preview servers and browser automation
        "start_preview",
        "stop_preview",
        "stop_all_preview_servers",
        # plugin / skill installation (clones and runs code)
        "install_plugin",
        "uninstall_plugin",
        "toggle_plugin",
        "list_plugins",
        "publish_skill",
        "import_claude_skills",
        "list_claude_skills",
        "sync_mcp_with_claude",
        "create_skill",
        "list_skills",
        # test / coverage / benchmark runners
        "run_coverage",
        "run_benchmark",
        "coverage_gaps",
        "audit_dependencies",
        "generate_test_skeleton",
        # code intelligence that shells out
        "analyze_imports",
        "codebase_health",
        "generate_changelog",
        "generate_infra",
        "migration_plan",
        "onboard",
        "replay_actions",
        # screen, clipboard and image pipelines
        "analyze_image",
        "generate_diagram",
        "paste_clipboard_image",
        "screenshot_to_code",
        "copy_to_clipboard",
        # local model probes (spawn ollama & co.)
        "check_local_models",
        "compare_costs",
        "list_models",
        "recommend_model",
    }
)
"""Tools a restricted session never registers: anything that spawns a process or opens a raw socket.

`tests/unit_tests/test_trust_profiles.py` builds a restricted agent and checks that no
surviving tool comes from a module that uses `subprocess` / raw network I/O, so a new
process-spawning tool has to be listed here before it can ship.
"""

_LOWERING = {"bypass": 3, "accept-edits": 2, "default": 1, "plan": 0, "paranoid": 0}


@dataclass(frozen=True)
class TrustProfile:
    """The policy a session runs under."""

    name: str = "default"
    permission_mode: str = "default"
    lock_mode: bool = False
    restricted: bool = False
    sandbox_level: str = ""
    egress_allowlist: tuple[str, ...] = ()
    allowed_domains: tuple[str, ...] = ()
    blocked_domains: tuple[str, ...] = ()
    excluded_tools: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def strips_shell(self) -> bool:
        """Whether shell / git tools are removed."""
        return self.restricted or "execute" in self.excluded_tools

    def tool_excluded(self, name: str) -> bool:
        """Whether a tool of this name must not be registered (`fetch_url` needs an allow-list when restricted)."""
        if name in self.excluded_tools:
            return True
        if not self.restricted:
            return False
        return name in RESTRICTED_TOOL_NAMES or (
            name == "fetch_url" and not self.allowed_domains
        )

    def describe(self) -> list[str]:
        """Rows for `/permissions`."""
        rows = [
            f"Trust profile: {self.name}" + (" (restricted)" if self.restricted else "")
        ]
        rows.append(
            f"  permission mode: {self.permission_mode}"
            + (" — locked, cannot be lowered" if self.lock_mode else "")
        )
        if self.restricted:
            rows.append(
                "  restricted: no shell, git/PR, raw HTTP, search, daemon, plugin, preview or other process-spawning tools; bypass refused; fetches only to allow-listed domains"
            )
        if self.sandbox_level:
            rows.append(
                f"  sandbox: {self.sandbox_level}"
                + (
                    f" (egress: {', '.join(self.egress_allowlist)})"
                    if self.egress_allowlist
                    else ""
                )
            )
        if self.allowed_domains:
            rows.append(f"  fetch allowed: {', '.join(self.allowed_domains)}")
        if self.blocked_domains:
            rows.append(f"  fetch blocked: {', '.join(self.blocked_domains)}")
        if self.excluded_tools:
            rows.append(f"  tools excluded: {', '.join(self.excluded_tools)}")
        rows.extend(f"  {note}" for note in self.notes)
        return rows


def restricted_profile(*, allowed_domains: tuple[str, ...] = ()) -> TrustProfile:
    """The `--restricted` preset."""
    return TrustProfile(
        name="restricted",
        permission_mode="default",
        lock_mode=True,
        restricted=True,
        allowed_domains=allowed_domains,
        notes=("reads and writes stay inside the working directory (virtual paths)",),
    )


def _strings(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(v.strip() for v in value.split(",") if v.strip())
    if isinstance(value, (list, tuple)):
        return tuple(str(v).strip() for v in value if str(v).strip())
    return ()


def trust_profile_from_settings(
    name: str, custom_settings: dict[str, Any] | None
) -> TrustProfile | None:
    """Read `custom_settings["trust"]` of a profile; `None` when the profile carries no policy.

    Raises:
        ValueError: For an unknown `permission_mode`.
    """
    raw = (custom_settings or {}).get("trust")
    if not isinstance(raw, dict):
        return None
    mode = str(raw.get("permission_mode", "default") or "default")
    if mode not in PERMISSION_MODES:
        msg = f"profile {name!r}: unknown permission_mode {mode!r}; use one of {', '.join(PERMISSION_MODES)}"
        raise ValueError(msg)
    restricted = bool(raw.get("restricted", False))
    return TrustProfile(
        name=name,
        permission_mode=mode,
        lock_mode=bool(raw.get("lock_mode", restricted)),
        restricted=restricted,
        sandbox_level=str(raw.get("sandbox_level", "") or ""),
        egress_allowlist=_strings(raw.get("egress_allowlist")),
        allowed_domains=_strings(raw.get("allowed_domains")),
        blocked_domains=_strings(raw.get("blocked_domains")),
        excluded_tools=_strings(raw.get("excluded_tools")),
    )


def load_trust_profile(config_dir: Path, name: str) -> TrustProfile | None:
    """The trust profile of the named CLI profile, or `None`."""
    from bog_agents_cli.profiles import load_profiles

    profile = load_profiles(config_dir).get(name)
    if profile is None:
        return None
    return trust_profile_from_settings(profile.name, profile.custom_settings)


def resolve_trust_profile(
    *, restricted: bool, profile_name: str = "", config_dir: Path | None = None
) -> TrustProfile:
    """What a session runs under: `--restricted` wins, then the named profile's policy, then the default."""
    if restricted:
        extra: tuple[str, ...] = ()
        if profile_name and config_dir is not None:
            named = load_trust_profile(config_dir, profile_name)
            if named is not None:
                extra = named.allowed_domains
        return restricted_profile(allowed_domains=extra)
    if profile_name and config_dir is not None:
        named = load_trust_profile(config_dir, profile_name)
        if named is not None:
            return named
    return TrustProfile()


def mode_change_refusal(profile: TrustProfile, mode: str) -> str | None:
    """Why `mode` may not be applied under `profile`, or `None` when it may."""
    if profile.restricted and mode in ("bypass", "accept-edits"):
        return f"{mode} is refused in a restricted session"
    if profile.lock_mode and _LOWERING.get(mode, 1) > _LOWERING.get(
        profile.permission_mode, 1
    ):
        return f"trust profile {profile.name!r} locks the permission mode at {profile.permission_mode}"
    return None


__all__ = [
    "PERMISSION_MODES",
    "RESTRICTED_TOOL_NAMES",
    "TrustProfile",
    "load_trust_profile",
    "mode_change_refusal",
    "resolve_trust_profile",
    "restricted_profile",
    "trust_profile_from_settings",
]
