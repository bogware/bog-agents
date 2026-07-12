"""Controller for the `/skills trust` subcommand family.

Kept as a standalone, pure-logic module (no Textual imports) so the trust CLI
surface is testable without spinning up the TUI. The app handler stays thin: it
mounts the user echo, calls `handle_skills_command`, and mounts the returned
message.

Grammar (everything after `/skills trust`):

* `/skills trust`                  → list trusted directories
* `/skills trust list`             → list trusted directories
* `/skills trust <path>`           → trust a (symlinked) skill directory
* `/skills trust revoke <path>`    → revoke trust for a directory
* `/skills trust clear`            → revoke trust for every directory
"""

from __future__ import annotations

from pathlib import Path

from bog_agents_cli import skill_trust


def _strip_quotes(value: str) -> str:
    """Strip a single matching pair of surrounding quotes, if present.

    A plain whitespace split (used instead of `shlex`, which mangles Windows
    backslash paths) leaves quote characters attached, so a user who quoted a
    path with spaces would otherwise get literal quotes in the stored key.

    Args:
        value: Raw path argument.

    Returns:
        The value with one surrounding pair of `"` or `'` removed.
    """
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def handle_skills_command(
    command: str, *, store_path: Path | None = None
) -> str | None:
    """Handle the `trust` subcommand family of `/skills`.

    Args:
        command: The full slash command text (e.g. `/skills trust list`).
        store_path: Optional trust store path (tests inject a temp path).

    Returns:
        A result message to display, or None when `command` is not a `trust`
            subcommand (the caller should fall through to the default `/skills`
            summary).
    """
    # Whitespace split (NOT shlex): shlex treats backslashes as escapes and
    # would corrupt Windows paths like `C:\skills\foo`. Path arguments are
    # reconstructed by joining the remaining tokens with a single space, with
    # surrounding quotes stripped.
    tokens = command.split()
    # tokens[0] is `/skills`; the trust family lives under the `trust` verb.
    if len(tokens) < 2 or tokens[1] != "trust":
        return None
    return _run_trust(tokens[2:], store_path=store_path)


def _run_trust(rest: list[str], *, store_path: Path | None) -> str:
    """Dispatch the arguments after `/skills trust`.

    Args:
        rest: Tokens following the `trust` verb.
        store_path: Optional trust store path.

    Returns:
        A human-readable result message.
    """
    if not rest or rest[0] == "list":
        return _format_list(store_path=store_path)
    action = rest[0]
    if action == "clear":
        return _do_clear(store_path=store_path)
    if action == "revoke":
        target = _strip_quotes(" ".join(rest[1:]))
        if not target:
            return "Usage: /skills trust revoke <path>"
        return _do_revoke(target, store_path=store_path)
    # Anything else is treated as a path to trust.
    target = _strip_quotes(" ".join(rest))
    return _do_trust(target, store_path=store_path)


def _format_list(*, store_path: Path | None) -> str:
    """Render the trusted-directory list.

    Args:
        store_path: Optional trust store path.

    Returns:
        A message listing trusted directories, an unreadable-store error, or a
            "nothing trusted" note.
    """
    try:
        entries = skill_trust.list_trusted_skill_dir_entries(
            store_path=store_path, strict=True
        )
    except (OSError, ValueError) as exc:
        return (
            "Could not read the skill trust store (it may be corrupt or unreadable):\n"
            f"  {type(exc).__name__}: {exc}\n"
            "Run `/skills trust clear` to reset it."
        )
    if not entries:
        return "No skill directories are trusted. Symlinked skill directories are refused by default.\nTrust one with `/skills trust <path>`."
    lines = ["Trusted skill directories:"]
    for path, trusted_at in entries:
        lines.append(
            f"  - {path}" + (f"  (trusted {trusted_at})" if trusted_at else "")
        )
    lines.append("")
    lines.append(
        "Revoke with `/skills trust revoke <path>` or clear all with `/skills trust clear`."
    )
    return "\n".join(lines)


def _do_trust(target: str, *, store_path: Path | None) -> str:
    """Trust a skill directory by its real resolved path.

    Args:
        target: User-supplied path (symlink or real directory).
        store_path: Optional trust store path.

    Returns:
        A success or error message.
    """
    try:
        resolved = Path(target).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        return f"Could not resolve `{target}`: {type(exc).__name__}: {exc}"
    if not resolved.is_dir():
        return f"Not a directory: {resolved}\nTrust the skill directory itself (the folder containing SKILL.md)."
    if not (resolved / "SKILL.md").exists():
        return (
            f"No SKILL.md found in {resolved}.\n"
            "Trust must point at a skill directory (the folder that contains SKILL.md)."
        )
    if skill_trust.trust_skill_dir(resolved, store_path=store_path):
        return (
            f"Trusted skill directory:\n  {resolved}\n\n"
            "A symlink resolving to this exact directory will now be loaded. If the symlink is later "
            "repointed, the directory is swapped for a symlink, or its SKILL.md changes, it is refused "
            "again until re-trusted."
        )
    return f"Failed to persist trust for {resolved} (the store may be unreadable). Nothing was changed."


def _do_revoke(target: str, *, store_path: Path | None) -> str:
    """Revoke trust for a directory.

    Args:
        target: Path to revoke (real or symlink form).
        store_path: Optional trust store path.

    Returns:
        A message describing the outcome.
    """
    result = skill_trust.revoke_skill_dir_trust(target, store_path=store_path)
    if result is skill_trust.RevokeResult.REMOVED:
        return f"Revoked trust for {Path(target).expanduser()}."
    if result is skill_trust.RevokeResult.NOT_FOUND:
        return f"No trust entry matched {Path(target).expanduser()}. Nothing was changed.\nRun `/skills trust list` to see trusted directories."
    return (
        "Could not update the trust store (it may be unreadable). Nothing was changed."
    )


def _do_clear(*, store_path: Path | None) -> str:
    """Clear every trusted directory.

    Args:
        store_path: Optional trust store path.

    Returns:
        A success or error message.
    """
    if skill_trust.clear_trusted_skill_dirs(store_path=store_path):
        return "Cleared all trusted skill directories. Symlinked skill directories are refused again by default."
    return (
        "Failed to clear the trust store (it may be unreadable). Nothing was changed."
    )
