"""Runloop sandbox implementation."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from runloop_api_client.sdk import Devbox

from bog_agents.backends.protocol import (
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
)
from bog_agents.backends.sandbox import BaseSandbox

logger = logging.getLogger(__name__)


class RunloopSandbox(BaseSandbox):
    """Sandbox backend that operates on a Runloop devbox."""

    def __init__(
        self,
        *,
        devbox: Devbox,
    ) -> None:
        """Create a sandbox backend connected to an existing Runloop devbox."""
        self._devbox = devbox
        self._devbox_id = devbox.id
        self._default_timeout = 30 * 60

    @property
    def id(self) -> str:
        """Return the devbox id."""
        return self._devbox_id

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        """Execute a shell command inside the devbox.

        Args:
            command: Shell command string to execute.
            timeout: Maximum time in seconds to wait for this command.

                If None, uses the backend's default timeout.

        Returns:
            ExecuteResponse containing output, exit code, and truncation flag.
        """
        effective_timeout = timeout if timeout is not None else self._default_timeout
        result = self._devbox.cmd.exec(command, timeout=effective_timeout)

        output = result.stdout() if result.stdout() is not None else ""
        stderr = result.stderr() if result.stderr() is not None else ""
        if stderr:
            output += "\n" + stderr if output else stderr

        return ExecuteResponse(
            output=output,
            exit_code=result.exit_code,
            truncated=False,
        )

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Download files from the devbox.

        Per the BackendProtocol contract, exceptions raised by the underlying
        Runloop SDK are caught and converted to standardized error codes so a
        single failed file in a batch does not abort the others.
        """
        responses: list[FileDownloadResponse] = []
        for path in paths:
            try:
                content = self._devbox.file.download(path=path)
                responses.append(
                    FileDownloadResponse(path=path, content=content, error=None)
                )
            except FileNotFoundError:
                responses.append(FileDownloadResponse(path=path, content=None, error="file_not_found"))
            except PermissionError:
                responses.append(FileDownloadResponse(path=path, content=None, error="permission_denied"))
            except IsADirectoryError:
                responses.append(FileDownloadResponse(path=path, content=None, error="is_directory"))
            except (OSError, ValueError):
                responses.append(FileDownloadResponse(path=path, content=None, error="invalid_path"))
            except Exception:  # noqa: BLE001  # SDK can raise arbitrary HTTP/API errors; map to file_not_found
                logger.exception("Runloop download failed for path: %s", path)
                responses.append(FileDownloadResponse(path=path, content=None, error="file_not_found"))
        return responses

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """Upload files into the devbox.

        Per the BackendProtocol contract, exceptions raised by the underlying
        Runloop SDK are caught and converted to standardized error codes so a
        single failed file in a batch does not abort the others.
        """
        responses: list[FileUploadResponse] = []
        for path, content in files:
            try:
                self._devbox.file.upload(path=path, file=content)
                responses.append(FileUploadResponse(path=path, error=None))
            except FileNotFoundError:
                responses.append(FileUploadResponse(path=path, error="file_not_found"))
            except PermissionError:
                responses.append(FileUploadResponse(path=path, error="permission_denied"))
            except (OSError, ValueError):
                responses.append(FileUploadResponse(path=path, error="invalid_path"))
            except Exception:  # noqa: BLE001  # SDK can raise arbitrary HTTP/API errors; map to invalid_path
                logger.exception("Runloop upload failed for path: %s", path)
                responses.append(FileUploadResponse(path=path, error="invalid_path"))
        return responses
