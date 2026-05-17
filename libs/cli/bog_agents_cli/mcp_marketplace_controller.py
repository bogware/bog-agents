"""``/mcp`` slash-command controller (Gap 5).

Sits between :mod:`bog_agents_cli.mcp_marketplace` (the catalog +
install logic) and the TUI handler in ``app.py``. Pure-text in, pure-
text out — no Textual imports — so the slash command stays testable
without spinning up the TUI.
"""

from __future__ import annotations

import logging
import shlex

from bog_agents_cli.mcp_marketplace import (
    CredentialPrompt,
    find_entry,
    install,
    render_entry_detail,
    render_install_outcome,
    render_marketplace_listing,
    search_entries,
    uninstall,
)

logger = logging.getLogger(__name__)


def handle(command: str, *, prompt: CredentialPrompt | None = None) -> str:
    """Dispatch one ``/mcp …`` invocation and return user-facing text.

    Args:
        command: Raw slash command (with or without the leading ``/mcp``).
        prompt: Credential prompter. The TUI handler supplies a Textual
            prompt; tests pass a stub. When ``None``, install requests
            with required env vars return the "missing required"
            outcome rather than blocking.
    """
    text = command.strip()
    if text.startswith("/mcp"):
        text = text[len("/mcp"):].strip()
    if not text:
        return _help_text()
    try:
        tokens = shlex.split(text)
    except ValueError as exc:
        return f"Could not parse /mcp arguments: {exc}"
    head = tokens[0].lower()
    rest = tokens[1:]
    if head in ("marketplace", "list", "ls"):
        query = " ".join(rest)
        return render_marketplace_listing(search_entries(query))
    if head == "search":
        if not rest:
            return "Usage: /mcp search <query>"
        return render_marketplace_listing(search_entries(" ".join(rest)))
    if head == "show":
        if not rest:
            return "Usage: /mcp show <name>"
        entry = find_entry(rest[0])
        if entry is None:
            return f"No marketplace entry named {rest[0]!r}."
        return render_entry_detail(entry)
    if head == "install":
        return _install(rest, prompt=prompt)
    if head == "uninstall":
        if not rest:
            return "Usage: /mcp uninstall <server_name>"
        ok = uninstall(rest[0])
        if ok:
            return f"Removed MCP server `{rest[0]}` from the user config."
        return f"No MCP server named `{rest[0]}` in the user config."
    return _help_text()


def _help_text() -> str:
    return (
        "/mcp — model-context-protocol server marketplace\n\n"
        "  /mcp marketplace [query]       — list / search the catalog\n"
        "  /mcp show <name>               — show details for one entry\n"
        "  /mcp install <name> [opts]     — install into ~/.bog-agents/.mcp.json\n"
        "      --as <id>                  — register under a different name\n"
        "      --overwrite                — replace existing entry\n"
        "      KEY=value …                — inline env values (skip the prompt)\n"
        "  /mcp uninstall <id>            — remove from the user config\n"
    )


def _install(
    args: list[str], *, prompt: CredentialPrompt | None
) -> str:
    if not args:
        return "Usage: /mcp install <name> [--as <id>] [--overwrite] [KEY=value …]"
    name = args[0]
    install_as: str | None = None
    overwrite = False
    env_overrides: dict[str, str] = {}
    rest = list(args[1:])
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == "--as":
            if i + 1 >= len(rest):
                return "Missing value after --as."
            install_as = rest[i + 1]
            i += 2
            continue
        if tok in ("--overwrite", "--force"):
            overwrite = True
            i += 1
            continue
        if "=" in tok:
            key, _, value = tok.partition("=")
            env_overrides[key.strip()] = value
            i += 1
            continue
        return f"Unrecognised /mcp install argument: {tok!r}"
    try:
        result = install(
            name,
            prompt=prompt,
            install_as=install_as,
            overwrite=overwrite,
            env_overrides=env_overrides,
        )
    except ValueError as exc:
        return f"/mcp install failed: {exc}"
    except RuntimeError as exc:
        return f"/mcp install failed: {exc}"
    return render_install_outcome(result)


__all__ = ["handle"]
