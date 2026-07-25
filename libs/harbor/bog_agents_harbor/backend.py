"""Harbor sandbox backend for executing commands in Harbor environments.

Design note — async-only
------------------------
HarborSandbox is intentionally async-only. All file and shell operations
communicate with a remote Harbor environment over an async transport, so
blocking sync wrappers would deadlock or stall the event loop.

For scripting or testing outside an async context, use the module-level
`run_sync()` helper::

    from bog_agents_harbor.backend import run_sync, HarborSandbox
    result = run_sync(sandbox.aexecute("ls"))

Do NOT call `run_sync()` from inside async code — use ``await`` directly.
"""

import asyncio
import base64
import json
import shlex
from typing import Any

from bog_agents.backends.protocol import (
    EditResult,
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
    WriteResult,
)
from bog_agents.backends.sandbox import BaseSandbox
from harbor.environments.base import BaseEnvironment

_SYNC_NOT_SUPPORTED = (
    "HarborSandbox is async-only. Use aexecute(), aread(), etc. "
    "For sync contexts, wrap with asyncio.run(sandbox.aexecute(cmd))."
)


def run_sync(coro: object) -> object:
    """Run an async coroutine synchronously.

    Convenience helper for testing and scripting contexts where an event loop
    is not already running. Do NOT use this inside async code — use ``await``
    directly.

    Args:
        coro: An awaitable (coroutine) returned by any ``HarborSandbox.a*`` method.

    Returns:
        The result of the coroutine.

    Example:
        ```python
        from bog_agents_harbor.backend import run_sync, HarborSandbox

        sandbox = HarborSandbox(environment)
        result = run_sync(sandbox.aexecute("echo hello"))
        ```
    """
    return asyncio.get_event_loop().run_until_complete(coro)  # type: ignore[arg-type]

# Shell exit codes used by the aedit command script
_EXIT_NOT_FOUND = 1
_EXIT_MULTIPLE_MATCHES = 2
_EXIT_FILE_MISSING = 3
_EXIT_DECODE_FAILED = 4

DEFAULT_COMMAND_TIMEOUT_SEC = 300
"""Default per-command timeout (5 minutes) to prevent stuck command hangs."""


_COMMAND_PREVIEW_CHAR_LIMIT = 200
"""Maximum chars included in timeout error command previews."""


class HarborSandbox(BaseSandbox):
    """A sandbox implementation using shell commands, on top of `BaseSandbox`.

    SAT-1 (v4): the structured listing/read/search surface (`als`, `aread_file`,
    `agrep`, `aglob`, `adelete`) is inherited from `BaseSandbox`, which derives
    every one from `aexecute()`. HarborSandbox only supplies the async
    command-execution primitive (`aexecute`) plus its own exec-based
    `awrite`/`aedit` (Harbor environments expose no native file-transfer API, so
    the `upload_files`/`download_files`-based defaults do not apply). Previously
    this class overrode the *deprecated* `als_info`/`agrep_raw`/`aglob_info`
    names, so the SDK's `als`/`agrep`/`aglob` fell through to a raising stub and
    every eval run had broken ls/grep/glob tools.

    Async-only: sync entry points (`execute`, `upload_files`, ...) raise. The
    edit operation requires python3 for JSON parsing; other operations use only
    standard shell utilities.
    """

    def __init__(self, environment: BaseEnvironment) -> None:
        """Initialize HarborSandbox with the given environment."""
        self.environment = environment

    async def aexecute(
        self,
        command: str,
        *,
        timeout: int | None = None,  # noqa: ASYNC109  # Timeout parameter is forwarded to environment exec, not used as asyncio timeout
    ) -> ExecuteResponse:
        """Execute a bash command in the task environment.

        Args:
            command: Shell command string to execute.
            timeout: Maximum time in seconds to wait for the command to complete.

                If None, uses the environment's default timeout.
        """
        timeout_sec = timeout if timeout is not None else DEFAULT_COMMAND_TIMEOUT_SEC
        try:
            if timeout_sec > 0:
                result = await asyncio.wait_for(
                    self.environment.exec(command),
                    timeout=timeout_sec,
                )
            else:
                result = await self.environment.exec(command)
        except TimeoutError:
            return ExecuteResponse(
                output=f"ERROR: Command timed out after {timeout_sec} seconds.\n"
                f"Command: {command[:_COMMAND_PREVIEW_CHAR_LIMIT]}"
                f"{'...' if len(command) > _COMMAND_PREVIEW_CHAR_LIMIT else ''}\n\n"
                f"SUGGESTION: This command is taking too long. Consider:\n"
                f"- Breaking it into smaller steps\n"
                f"- Using a shorter timeout with the timeout_sec parameter\n"
                f"- For package installs: use --no-install-recommends ...\n"
                f"- For long builds: run in background with nohup ...",
                exit_code=124,
                truncated=False,
            )

        # These errors appear in harbor environments when running bash commands
        # in non-interactive/non-TTY contexts. They're harmless artifacts.
        # Filter them from both stdout and stderr, then collect them to show in stderr.
        error_messages = [
            "bash: cannot set terminal process group (-1): Inappropriate ioctl for device",
            "bash: cannot set terminal process group (1): Inappropriate ioctl for device",
            "bash: no job control in this shell",
            "bash: initialize_job_control: no job control in background: Bad file descriptor",
        ]

        stdout = result.stdout or ""
        stderr = result.stderr or ""

        # Collect the bash messages if they appear (to move to stderr)
        bash_messages = []
        for error_msg in error_messages:
            if error_msg in stdout:
                bash_messages.append(error_msg)
                stdout = stdout.replace(error_msg, "")
            if error_msg in stderr:
                stderr = stderr.replace(error_msg, "")

        stdout = stdout.strip()
        stderr = stderr.strip()

        # Add bash messages to stderr
        if bash_messages:
            bash_msg_text = "\n".join(bash_messages)
            stderr = f"{bash_msg_text}\n{stderr}".strip() if stderr else bash_msg_text

        # Only append stderr label if there's actual stderr content
        if stderr:
            output = stdout + "\n\n stderr: " + stderr if stdout else "\n stderr: " + stderr
        else:
            output = stdout
        return ExecuteResponse(
            output=output,
            exit_code=result.return_code,
        )

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        """Execute a bash command in the task environment."""
        raise NotImplementedError(_SYNC_NOT_SUPPORTED)

    @property
    def id(self) -> str:
        """Unique identifier for the sandbox backend."""
        return self.environment.session_id

    async def awrite(
        self,
        file_path: str,
        content: str,
    ) -> WriteResult:
        """Create a new file using shell commands."""
        # Encode content as base64 to avoid escaping issues
        content_b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
        safe_path = shlex.quote(file_path)

        # Use heredoc to pass content via stdin to avoid ARG_MAX limits on large files.
        # ARG_MAX limits the total size of command-line arguments.
        # Heredocs bypass this by passing data through stdin rather than as arguments.
        cmd = f"""
if [ -e {safe_path} ]; then
    echo "Error: File '"{safe_path}"' already exists" >&2
    exit 1
fi
parent_dir=$(dirname {safe_path})
mkdir -p "$parent_dir" 2>/dev/null
if ! base64 -d > {safe_path} <<'__BOG_AGENTS_EOF__'
{content_b64}
__BOG_AGENTS_EOF__
then
    echo "Error: Failed to decode content for file '"{safe_path}"' " >&2
    exit 1
fi
"""
        result = await self.aexecute(cmd)

        if result.exit_code != 0 or "Error:" in result.output:
            error_msg = result.output.strip() or f"Failed to write file '{file_path}'"
            return WriteResult(error=error_msg)

        return WriteResult(path=file_path, files_update=None)

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
        *,
        base_content: dict[str, Any] | None = None,  # noqa: ARG002  # accepted for BaseSandbox parity; the sandbox is the source of truth
    ) -> EditResult:
        """Edit a file by replacing string occurrences using shell commands."""
        # Create JSON payload with old and new strings, then base64 encode
        payload = json.dumps({"old": old_string, "new": new_string})
        payload_b64 = base64.b64encode(payload.encode("utf-8")).decode("ascii")
        safe_path = shlex.quote(file_path)
        replace_all_str = "true" if replace_all else "false"

        # Use heredoc to pass old/new strings via stdin to avoid ARG_MAX limits.
        # ARG_MAX limits the total size of command-line arguments.
        # Format: base64-encoded JSON with {{"old": str, "new": str}}.
        # The heredoc feeds into the brace group { ... } which reads and processes stdin.
        cmd = f"""
if [ ! -f {safe_path} ]; then
    exit 3
fi

{{
    # Read entire heredoc content using cat (read only gets first line)
    payload_b64=$(cat)
    if [ -z "$payload_b64" ]; then
        echo "Error: No payload received for edit operation" >&2
        exit 4
    fi

    # Decode base64 payload
    payload=$(echo "$payload_b64" | base64 -d) || {{
        echo "Error: Failed to decode payload" >&2
        exit 4
    }}

    # Extract old and new strings from JSON using python3
    old=$(echo "$payload" | python3 -c "import sys, json; print(json.load(sys.stdin)['old'], end='')") || {{
        echo "Error: Failed to parse JSON payload" >&2
        exit 4
    }}
    new=$(echo "$payload" | python3 -c "import sys, json; print(json.load(sys.stdin)['new'], end='')") || {{
        echo "Error: Failed to parse JSON payload" >&2
        exit 4
    }}

    # Count occurrences using grep -F (fixed strings)
    count=$(grep -o -F "$old" {safe_path} | wc -l)

    if [ "$count" -eq 0 ]; then
        exit 1
    elif [ "$count" -gt 1 ] && [ "{replace_all_str}" = "false" ]; then
        exit 2
    fi

    # Use perl for reliable string replacement (handles special chars).
    # Note: \\Q...\\E escapes the search pattern. The replacement string is not
    # escaped, so Perl special sequences (\\U, $1, etc.) in new will be interpreted.
    if [ "{replace_all_str}" = "true" ]; then
        perl -i -pe 's/\\Q'"$old"'\\E/'"$new"'/g' {safe_path}
    else
        perl -i -pe 's/\\Q'"$old"'\\E/'"$new"'/' {safe_path}
    fi

    echo "$count"
}} <<'__BOG_AGENTS_EOF__'
{payload_b64}
__BOG_AGENTS_EOF__
"""
        result = await self.aexecute(cmd)

        exit_code = result.exit_code
        output = result.output.strip()

        if exit_code == _EXIT_NOT_FOUND:
            return EditResult(error=f"Error: String not found in file: '{old_string}'")
        if exit_code == _EXIT_MULTIPLE_MATCHES:
            return EditResult(
                error=f"Error: String '{old_string}' appears multiple times. Use replace_all=True to replace all occurrences."
            )
        if exit_code == _EXIT_FILE_MISSING:
            return EditResult(error=f"Error: File '{file_path}' not found")
        if exit_code == _EXIT_DECODE_FAILED:
            return EditResult(error=f"Error: Failed to decode edit payload: {output}")
        if exit_code != 0:
            return EditResult(
                error=f"Error editing file (exit code {exit_code}): {output or 'Unknown error'}"
            )

        try:
            count = int(output.split("\n")[0])
        except (ValueError, IndexError):
            count = 1

        return EditResult(path=file_path, files_update=None, occurrences=count)

    # -- required by BaseSandbox (Harbor has no native file-transfer API) ------

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """Not supported: Harbor environments expose no native upload API.

        `BaseSandbox` calls this only from its default `awrite`/`aedit`, both of
        which HarborSandbox overrides with exec-based implementations, so this is
        never reached on the normal path.
        """
        raise NotImplementedError(_SYNC_NOT_SUPPORTED)

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Not supported: Harbor reads go through `aread_file` / `aexecute`."""
        raise NotImplementedError(_SYNC_NOT_SUPPORTED)
