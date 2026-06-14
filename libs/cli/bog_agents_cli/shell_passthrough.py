"""Shell pass-through that the agent can see (`!command` in the TUI).

Typing ``!<command>`` in the TUI runs the command in the session's working
directory and shows the output. Historically that output was shown only to the
*user* — the agent never knew the command ran. This module formats a record of
each pass-through run (command + output + exit code) so the CLI can inject it
into the agent's conversation thread (via ``aupdate_state``). The next time the
agent runs, it sees what you ran in the shared session and its output, and can
use it as context — no copy-pasting required.

Pure formatting lives here so it's unit-testable; the thread injection is a thin
wrapper in ``app.BogAgentsApp._record_shell_run_for_agent``.
"""

from __future__ import annotations

# Cap injected output so a chatty command can't blow up the agent's context.
# Keep the head and tail (errors usually surface at the end).
_MAX_OUTPUT_CHARS = 8000


def format_shell_context(
    command: str,
    output: str,
    returncode: int | None,
    *,
    max_chars: int = _MAX_OUTPUT_CHARS,
) -> str:
    """Render a shell pass-through run as a message for the agent's thread.

    Args:
        command: The command the user ran (without the leading ``!``).
        output: Combined stdout/stderr the command produced.
        returncode: Process exit code, or None if unknown.
        max_chars: Cap on injected output length (head+tail kept beyond it).

    Returns:
        A first-person message telling the agent the user ran this in the
        shared session, with the (possibly truncated) output.
    """
    out = (output or "").strip() or "(no output)"
    if len(out) > max_chars:
        half = max_chars // 2
        out = f"{out[:half]}\n...[{len(out) - max_chars} chars truncated]...\n{out[-half:]}"
    status = f"exit code {returncode}" if returncode is not None else "completed"
    return (
        f"[shell pass-through] I ran a command in our shared session ({status}). "
        "You did not run this — I did — but you can see it and use the output as "
        "context:\n"
        f"```console\n$ {command}\n{out}\n```"
    )
