"""LangSmith sandbox backend.

`LangSmithSandbox` is a `BaseSandbox` whose file transfers go through the
LangSmith SDK rather than through shell commands: `write` posts content in the
HTTP body (so a large file cannot hit ARG_MAX or a provider's `execute`
request-body limit) and `read_file` fetches bytes directly (so a large file is
never piped through the shell's stdout).

The `langsmith` package is imported lazily inside each method. A module-level
import would make `import bog_agents.backends.langsmith` pull the SDK — and its
transitive HTTP stack — on every process start.
"""

from __future__ import annotations

import base64
import logging
from typing import TYPE_CHECKING

import anyio

from bog_agents.backends.protocol import (
    ExecuteResponse,
    FileData,
    FileDownloadResponse,
    FileUploadResponse,
    ReadResult,
    WriteResult,
)
from bog_agents.backends.sandbox import (
    MAX_BINARY_BYTES,
    MAX_OUTPUT_BYTES,
    TRUNCATION_MSG,
    BaseSandbox,
)
from bog_agents.backends.utils import _get_backend_read_file_type

if TYPE_CHECKING:
    from langsmith.sandbox import Sandbox

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 30 * 60
"""Default `execute` timeout: long enough for a build/test run to finish."""


def _binary_read_result(file_path: str, raw: bytes) -> ReadResult:
    """Build the binary `ReadResult` returned by `LangSmithSandbox.read_file`.

    Mirrors the `error` / `encoding="base64"` shapes that `BaseSandbox`'s
    server-side read script produces, including the `File '<path>': ` prefix.

    Args:
        file_path: Path that was read, used in the error message.
        raw: Raw file bytes.

    Returns:
        `ReadResult` with base64 `file_data`, or an error when the file exceeds
            `MAX_BINARY_BYTES`.
    """
    if len(raw) > MAX_BINARY_BYTES:
        return ReadResult(error=f"File '{file_path}': Binary file exceeds maximum preview size of {MAX_BINARY_BYTES} bytes")
    return ReadResult(file_data=FileData(content=base64.b64encode(raw).decode("ascii"), encoding="base64"))


def _cap_text(content: str) -> str:
    """Cap rendered text at `MAX_OUTPUT_BYTES`, appending `TRUNCATION_MSG`.

    Keeps the SDK read path from reintroducing the transport-size symptom that
    the server-side read script guards against.

    Args:
        content: The page of text about to be returned.

    Returns:
        `content` unchanged, or a truncated copy with `TRUNCATION_MSG` appended.
    """
    encoded = content.encode("utf-8")
    effective_limit = MAX_OUTPUT_BYTES - len(TRUNCATION_MSG.encode("utf-8"))
    if len(encoded) <= effective_limit:
        return content
    return encoded[:effective_limit].decode("utf-8", errors="ignore") + TRUNCATION_MSG


class LangSmithSandbox(BaseSandbox):
    r"""LangSmith sandbox conforming to `SandboxBackendProtocol`.

    Example:
        ```python
        from langsmith.sandbox import Sandbox

        from bog_agents.backends.langsmith import LangSmithSandbox

        backend = LangSmithSandbox(Sandbox.create(name="my-sandbox"))
        backend.write("/workspace/main.py", "print('hi')\\n")
        ```
    """

    enable_capture_offload = True
    """LangSmith images ship a POSIX shell and coreutils compatible with the
    capture wrapper, so capture-at-source offload for `execute` is safe here.
    """

    def __init__(self, sandbox: Sandbox) -> None:
        """Wrap an existing LangSmith sandbox.

        Args:
            sandbox: LangSmith `Sandbox` instance to wrap.
        """
        self._sandbox = sandbox
        self._default_timeout: int = _DEFAULT_TIMEOUT_SECONDS

    @property
    def id(self) -> str:
        """Return the LangSmith sandbox name."""
        return self._sandbox.name

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        """Execute a shell command inside the sandbox.

        Args:
            command: Shell command string to execute.
            timeout: Maximum time in seconds to wait for the command. If `None`,
                uses the backend's default. A value of `0` disables the command
                timeout.

        Returns:
            `ExecuteResponse` with combined output, exit code, and truncation flag.
        """
        effective_timeout = timeout if timeout is not None else self._default_timeout
        result = self._sandbox.run(command, timeout=effective_timeout)

        output = result.stdout or ""
        if result.stderr:
            output = output + "\n" + result.stderr if output else result.stderr

        return ExecuteResponse(output=output, exit_code=result.exit_code, truncated=False)

    # -- write ----------------------------------------------------------------

    def write(self, file_path: str, content: str) -> WriteResult:
        """Write content via the LangSmith SDK, bypassing ARG_MAX entirely.

        `BaseSandbox.write` routes content through `upload_files`; this override
        calls the SDK's native `write`, which sends the bytes in the HTTP body,
        while preserving the parent-directory creation of the base class.

        Args:
            file_path: Destination path inside the sandbox.
            content: UTF-8 text content to write.

        Returns:
            `WriteResult` with the written path, or an error message.
        """
        from langsmith.sandbox import SandboxClientError

        preflight_error = self._write_preflight(file_path)
        if preflight_error is not None:
            return preflight_error

        try:
            self._sandbox.write(file_path, content.encode("utf-8"))
        except SandboxClientError as e:
            return WriteResult(error=f"Failed to write file '{file_path}': {e}")
        # External storage: nothing to thread back into LangGraph state.
        return WriteResult(path=file_path, files_update=None)

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        """Async version of `write`.

        The LangSmith SDK is synchronous, so the call runs in a worker thread
        rather than through `BaseSandbox.awrite` (which would take the
        `upload_files` path and lose this override's transport).

        Args:
            file_path: Destination path inside the sandbox.
            content: UTF-8 text content to write.

        Returns:
            `WriteResult` with the written path, or an error message.
        """
        return await anyio.to_thread.run_sync(self.write, file_path, content)  # ty: ignore[unresolved-attribute]

    # -- read -----------------------------------------------------------------

    def read_file(
        self,
        file_path: str,
        offset: int = 0,
        limit: int = 2000,
    ) -> ReadResult:
        r"""Read file content via the LangSmith SDK.

        `BaseSandbox.read_file` pipes content through `execute`, which can hang
        or exceed transport limits on a large file. This override fetches the
        bytes directly and reproduces the base class's pagination semantics
        locally:

        - Files routed as binary by extension — or that fail a UTF-8 decode —
            come back base64-encoded, capped at `MAX_BINARY_BYTES`.
        - Text is normalized for universal newlines (`\r\n` and bare `\r` both
            collapse to `\n`), paginated by `offset` / `limit`, and capped at
            `MAX_OUTPUT_BYTES` with `TRUNCATION_MSG` appended on overflow.

        The newline normalization is load-bearing: without it a CRLF file
        round-trips with stray `\r`, and the `old_string` the model sends back
        never matches on `edit`.

        Args:
            file_path: Absolute path to the file to read.
            offset: Number of leading text lines to skip.
            limit: Maximum number of text lines to return.

        Returns:
            `ReadResult` with `file_data` on success, or `error` on failure.
        """
        from langsmith.sandbox import ResourceNotFoundError, SandboxClientError

        try:
            raw = self._sandbox.read(file_path)
        except ResourceNotFoundError:
            return ReadResult(error=f"File '{file_path}': file_not_found")
        except SandboxClientError as e:
            logger.warning("LangSmith read failed for %s: %s", file_path, e)
            return ReadResult(error=f"File '{file_path}': {type(e).__name__}: {e}")

        if not raw:
            # Empty content renders as the standard empty-file reminder upstack.
            return ReadResult(file_data=FileData(content="", encoding="utf-8"))

        # Route by extension first, mirroring the server-side read script:
        # anything not classified as text goes straight to base64, with no decode
        # attempt.
        if _get_backend_read_file_type(file_path) != "text":
            return _binary_read_result(file_path, raw)

        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            # A text-by-extension file holding non-UTF-8 bytes: fall back to
            # base64 rather than guessing an encoding. Logged so a corrupted file
            # or a mis-named extension is observable rather than silently
            # reshaped.
            logger.info("Text-extension file %s contained invalid UTF-8; returning as base64", file_path)
            return _binary_read_result(file_path, raw)

        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = normalized.split("\n")
        if lines and lines[-1] == "":
            lines.pop()

        offset = int(offset)
        limit = int(limit)

        if not lines or offset >= len(lines):
            return ReadResult(error=f"File '{file_path}': Line offset {offset} exceeds file length ({len(lines)} lines)")

        content = "\n".join(lines[offset : offset + limit])
        return ReadResult(file_data=FileData(content=_cap_text(content), encoding="utf-8"))

    async def aread_file(
        self,
        file_path: str,
        offset: int = 0,
        limit: int = 2000,
    ) -> ReadResult:
        """Async version of `read_file`.

        The LangSmith SDK is synchronous, so the call runs in a worker thread
        rather than through `BaseSandbox.aread_file` (which would take the
        server-side-script path and lose this override's transport).

        Args:
            file_path: Absolute path to the file to read.
            offset: Number of leading text lines to skip.
            limit: Maximum number of text lines to return.

        Returns:
            `ReadResult` with `file_data` on success, or `error` on failure.
        """
        return await anyio.to_thread.run_sync(self.read_file, file_path, offset, limit)  # ty: ignore[unresolved-attribute]

    # -- bulk transfer --------------------------------------------------------

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Download multiple files from the LangSmith sandbox.

        Supports partial success: one failure does not abort the others.

        Args:
            paths: File paths to download.

        Returns:
            One `FileDownloadResponse` per input path, in input order.
        """
        from langsmith.sandbox import ResourceNotFoundError, SandboxClientError

        responses: list[FileDownloadResponse] = []
        for path in paths:
            if not path.startswith("/"):
                responses.append(FileDownloadResponse(path=path, content=None, error="invalid_path"))
                continue
            try:
                content = self._sandbox.read(path)
            except ResourceNotFoundError:
                responses.append(FileDownloadResponse(path=path, content=None, error="file_not_found"))
            except SandboxClientError as e:
                error = "is_directory" if "is a directory" in str(e).lower() else "file_not_found"
                responses.append(FileDownloadResponse(path=path, content=None, error=error))
            else:
                responses.append(FileDownloadResponse(path=path, content=content, error=None))
        return responses

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """Upload multiple files to the LangSmith sandbox.

        Supports partial success: one failure does not abort the others.

        Args:
            files: `(path, content)` tuples to upload.

        Returns:
            One `FileUploadResponse` per input file, in input order.
        """
        from langsmith.sandbox import SandboxClientError

        responses: list[FileUploadResponse] = []
        for path, content in files:
            if not path.startswith("/"):
                responses.append(FileUploadResponse(path=path, error="invalid_path"))
                continue
            try:
                self._sandbox.write(path, content)
            except SandboxClientError as e:
                logger.debug("Failed to upload %s: %s", path, e)
                responses.append(FileUploadResponse(path=path, error="permission_denied"))
            else:
                responses.append(FileUploadResponse(path=path, error=None))
        return responses
