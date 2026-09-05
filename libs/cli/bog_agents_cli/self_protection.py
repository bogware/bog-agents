"""Self-modification guard: the agent may not silently rewrite its own authority.

#24 (CVE-2026-25725 class): the files that define the agent's own permissions and
safety policy — Expert Mode rulebooks, the dreamscape laws/constitution, project
lifecycle hooks, and the MCP server manifest — are gated behind a human-in-the-
loop approval whenever a file tool (write_file / edit_file / multi_edit_file /
delete) targets them, **even in auto-approve mode**. The gate is enforced with
`FilesystemPermissionsMiddleware` `interrupt`-mode rules, which the SDK merges
into `interrupt_on` regardless of the CLI's `--auto-approve`, so it holds below
the tool layer and cannot be auto-approved away.

The CLI's file tools run in a virtual filesystem rooted at the project, so the
agent cannot reach the home-level trust stores (`~/.bog-agents/config.toml`, the
skill-trust store, `.env`) through file tools at all. The one remaining vector to
those is the shell (`execute`), which is Turing-complete; `command_targets_authority_file`
is a best-effort screen used to force an approval prompt for shell commands that
appear to write an authority file when the agent is otherwise unattended.
"""

from __future__ import annotations

from bog_agents.middleware.permissions import FilesystemPermission

# Project-relative (virtual) authority paths. The file backend roots the virtual
# filesystem at the project, so these `/`-anchored globs are cross-platform and
# match the paths the file tools actually see.
_PROJECT_AUTHORITY_PATTERNS: tuple[str, ...] = (
    "/.bog-agents/expert_rules/**",  # Expert Mode rulebooks (deny/allow tool calls)
    "/.bog-agents/laws.md",  # dreamscape hard-reject laws
    "/.bog-agents/constitution.md",  # dreamscape soft / log-only rules
    "/.bog-agents/hooks/**",  # project lifecycle hooks (execute on events)
    "/.mcp.json",  # project MCP server manifest
    # ROADMAP #48: the rest of what a checkout can use to steer this or the
    # next agent — CLI policy, other agents' instruction trees, CI and editor
    # automation that runs on open.
    "/.bog-agents/settings.json",
    "/.bog-agents/sandbox.toml",
    "/.claude/**",
    "/.cursor/**",
    "/.agents/**",
    "/.github/workflows/**",
    "/.vscode/**",
    "/.idea/**",
)

# Never written through the file tools, in any mode: git runs these without
# asking, and no agent task legitimately edits them in place.
_PROJECT_DENY_PATTERNS: tuple[str, ...] = (
    "/.git/hooks/**",
    "/.git/config",
)

# Under `--restricted` the interrupt tier for automation files becomes a deny
# tier: a restricted session has no shell, so the only way it could run code
# is by editing something that runs later.
_RESTRICTED_DENY_PATTERNS: tuple[str, ...] = (
    "/.bog-agents/expert_rules/**",
    "/.bog-agents/hooks/**",
    "/.bog-agents/settings.json",
    "/.bog-agents/sandbox.toml",
    "/.mcp.json",
    "/.github/workflows/**",
    "/.vscode/**",
    "/.idea/**",
)


def authority_file_permissions(
    *, restricted: bool = False
) -> list[FilesystemPermission]:
    """Return the rules that gate writes to authority files (deny tier first; first match wins).

    Args:
        restricted: Under `--restricted` (ROADMAP #48) the automation files move
            from `interrupt` to `deny`.

    Returns:
        A `deny` rule for the paths no session may write, then the `interrupt`
        rule whose `write` operations on the project's authority paths resolve
        to human approval, never a silent write.
    """
    deny = list(_PROJECT_DENY_PATTERNS)
    if restricted:
        deny.extend(_RESTRICTED_DENY_PATTERNS)
    return [
        FilesystemPermission(operations=["write"], paths=deny, mode="deny"),
        FilesystemPermission(
            operations=["write"],
            paths=list(_PROJECT_AUTHORITY_PATTERNS),
            mode="interrupt",
        ),
    ]


# Best-effort shell screen (defense-in-depth for the `execute` tool, which the
# virtual filesystem does not confine).
_AUTHORITY_NAME_MARKERS: tuple[str, ...] = (
    "expert_rules",
    "laws.md",
    "constitution.md",
    "skill_trust.json",
    "config.toml",
    "butcher.toml",
    "operator.toml",
    "dreamscape.toml",
    ".mcp.json",
)
_WRITE_INDICATORS: tuple[str, ...] = (
    ">",
    ">>",
    "tee ",
    "dd ",
    "sed -i",
    "truncate",
    "cp ",
    "mv ",
    "install ",
)


def command_targets_authority_file(command: str) -> bool:
    """Best-effort: does a shell command appear to WRITE an authority file?

    A Turing-complete shell cannot be screened exactly; this flags the common,
    legible cases (redirects, `tee`, `sed -i`, `cp`/`mv` into a `.bog-agents`
    path or an authority filename) so an unattended run still surfaces an
    approval prompt. A false positive costs one extra prompt, never a silent
    bypass — so this errs toward prompting.

    Args:
        command: The shell command the `execute` tool was asked to run.

    Returns:
        True when the command looks like it writes an authority file.
    """
    if not command:
        return False
    touches_authority = ".bog-agents" in command or any(
        marker in command for marker in _AUTHORITY_NAME_MARKERS
    )
    if not touches_authority:
        return False
    return any(indicator in command for indicator in _WRITE_INDICATORS)
