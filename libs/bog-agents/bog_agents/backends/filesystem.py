"""`FilesystemBackend`: Read and write files directly from the filesystem."""

import base64
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import warnings
from datetime import datetime
from pathlib import Path

import anyio

from bog_agents.backends.protocol import (
    DEFAULT_GREP_TIMEOUT,
    BackendProtocol,
    DeleteResult,
    EditResult,
    FileData,
    FileDownloadResponse,
    FileInfo,
    FileUploadResponse,
    GlobResult,
    GrepMatch,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)
from bog_agents.backends.utils import (
    EMPTY_CONTENT_WARNING,
    MAX_BINARY_BYTES,
    MAX_VIDEO_INPUT_BYTES,
    _get_backend_read_file_type,
    check_empty_content,
    compile_grep_include_glob,
    compile_recursive_glob,
    file_data_to_string,
    format_content_with_line_numbers,
    perform_string_replacement,
)

logger = logging.getLogger(__name__)

DEFAULT_GLOB_TIMEOUT = 5
"""Wall-clock budget in seconds for a single `glob` walk.

Kept below the filesystem middleware's `GLOB_TIMEOUT` (20s) so the backend
returns partial results before that outer net abandons the call; the ordering is
guarded by `test_glob_backend_budget_below_middleware_deadline`.
"""


class FilesystemBackend(BackendProtocol):
    """Backend that reads and writes files directly from the filesystem.

    Files are accessed using their actual filesystem paths. Relative paths are
    resolved relative to the current working directory. Content is read/written
    as plain text, and metadata (timestamps) are derived from filesystem stats.

    !!! warning "Security Warning"

        This backend grants agents direct filesystem read/write access. Use with
        caution and only in appropriate environments.

        **Appropriate use cases:**

        - Local development CLIs (coding assistants, development tools)
        - CI/CD pipelines (see security considerations below)

        **Inappropriate use cases:**

        - Web servers or HTTP APIs - use `StateBackend`, `StoreBackend`, or
            `SandboxBackend` instead

        **Security risks:**

        - Agents can read any accessible file, including secrets (API keys,
            credentials, `.env` files)
        - Combined with network tools, secrets may be exfiltrated via SSRF attacks
        - File modifications are permanent and irreversible

        **Recommended safeguards:**

        1. Enable Human-in-the-Loop (HITL) middleware to review sensitive operations
        2. Exclude secrets from accessible filesystem paths (especially in CI/CD)
        3. For production environments, prefer `StateBackend`, `StoreBackend` or `SandboxBackend`

        In general, we expect this backend to be used with Human-in-the-Loop (HITL)
        middleware, or within a properly sandboxed environment if you need to run
        untrusted workloads.

        !!! note

            `virtual_mode=True` (the default) is primarily for virtual path semantics (for
            example with `CompositeBackend`). It also provides path-based guardrails by
            blocking traversal (`..`, `~`) and absolute paths outside `root_dir`, but it
            does not provide sandboxing or process isolation. `virtual_mode=False` provides
            no security even with `root_dir` set and is deprecated.
    """

    def __init__(
        self,
        root_dir: str | Path | None = None,
        virtual_mode: bool | None = None,
        max_file_size_mb: int = 10,
    ) -> None:
        """Initialize filesystem backend.

        Args:
            root_dir: Optional root directory for file operations.

                Defaults to the current working directory.

                - When `virtual_mode=True` (default): Acts as a virtual root for filesystem operations.
                - When `virtual_mode=False`: Only affects relative path resolution. Deprecated.

            virtual_mode: Enable virtual path mode.

                **Primary use case:** stable, backend-independent path semantics when
                used with `CompositeBackend`, which strips route prefixes and forwards
                normalized paths to the routed backend.

                When `True` (default), all paths are treated as virtual paths anchored to
                `root_dir`. Path traversal (`..`, `~`) is blocked and all resolved paths
                are verified to remain within `root_dir`.

                When `False`, absolute paths are used as-is and relative paths are
                resolved under `root_dir`. This provides no security against an agent
                choosing paths outside `root_dir`.

                - Absolute paths (e.g., `/etc/passwd`) bypass `root_dir` entirely
                - Relative paths with `..` can escape `root_dir`
                - Agents have unrestricted filesystem access

            max_file_size_mb: Maximum file size in megabytes for operations like
                grep's Python fallback search.

                Files exceeding this limit are skipped during search. Defaults to 10 MB.
        """
        self.cwd = Path(root_dir).resolve() if root_dir else Path.cwd()
        if virtual_mode is None:
            virtual_mode = True
        elif virtual_mode is False:
            warnings.warn(
                "FilesystemBackend virtual_mode=False is deprecated and will be removed in a future "
                "release. The default is now virtual_mode=True (secure by default). "
                "Passing virtual_mode=False disables path-based guardrails: absolute paths and '..' "
                "can bypass root_dir. See the API reference for details.",
                DeprecationWarning,
                stacklevel=2,
            )
        self.virtual_mode = virtual_mode
        self.max_file_size_bytes = max_file_size_mb * 1024 * 1024

    def _atomic_write(self, resolved_path: Path, content: str | bytes, *, mode: int = 0o644) -> None:
        """Write `content` to `resolved_path` crash-safely via a sibling temp file.

        Writes to a uniquely-named sibling temp file opened with
        `O_NOFOLLOW|O_CREAT|O_EXCL` (so the write can never traverse a symlink and
        never clobber a pre-existing temp), fsyncs the data to disk, then
        `os.replace`s the temp file onto the destination. Because `os.replace` is
        atomic on a single filesystem, an interrupt / `ENOSPC` after the old
        `O_TRUNC` truncate can no longer corrupt or empty the original: either the
        replace completes (new content) or it doesn't (original untouched).

        The destination is checked for being a symlink before the replace so the
        O_NOFOLLOW protection of the previous in-place writers is preserved: we
        never silently overwrite a symlink target.

        Args:
            resolved_path: Absolute destination path (already security-resolved).
            content: Text (`str`) or binary (`bytes`) payload to write.
            mode: File mode for the newly created temp file. Defaults to `0o644`.

        Raises:
            OSError: If the destination is a symlink (preserving O_NOFOLLOW
                semantics), or on any underlying open/write/replace failure.
            UnicodeEncodeError: If `content` is `str` and cannot be UTF-8 encoded.
        """
        # Preserve the O_NOFOLLOW symlink protection of the in-place writers:
        # refuse to replace a symlink (which os.replace would otherwise clobber).
        # os.path.islink inspects the link itself, not its target.
        if hasattr(os, "O_NOFOLLOW") and os.path.islink(resolved_path):
            msg = f"Refusing to write through symlink: {resolved_path}"
            raise OSError(msg)

        # Sibling temp file: same directory guarantees same filesystem so
        # os.replace is a true atomic rename (not a cross-device copy).
        tmp_path = resolved_path.with_name(f".{resolved_path.name}.{os.getpid()}.tmp")

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW

        is_bytes = isinstance(content, bytes)
        try:
            fd = os.open(tmp_path, flags, mode)
            try:
                with os.fdopen(fd, "wb" if is_bytes else "w", encoding=None if is_bytes else "utf-8") as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
            except BaseException:
                # fdopen took ownership of fd; the with-block closed it. Best-effort
                # cleanup of the partial temp so it doesn't linger or block a retry.
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
            os.replace(tmp_path, resolved_path)
        except BaseException:
            # Replace (or a pre-replace failure) left the temp behind; remove it.
            # The original destination is guaranteed untouched at this point.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _resolve_path(self, key: str) -> Path:
        r"""Resolve a file path with security checks.

        When `virtual_mode=True`, treat incoming paths as virtual absolute paths under
        `self.cwd`, disallow traversal (`..`, `~`) and ensure resolved path stays within
        root.

        When `virtual_mode=False`, preserve legacy behavior: absolute paths are allowed
        as-is; relative paths resolve under cwd.

        On Windows specifically, POSIX-style absolute paths (e.g. ``/foo.txt``)
        emitted by LLMs are rewritten to be cwd-relative so they don't silently
        land at the current drive root. Drive-letter absolute paths (``C:\\foo``)
        are still honored. This addresses a class of bugs where local models
        (notably Llama 3.1, Gemma 4) emit POSIX paths in tool args on Windows
        hosts and writes end up at ``C:\\foo.txt`` instead of the intended
        location under cwd.

        Args:
            key: File path (absolute, relative, or virtual when `virtual_mode=True`).

        Returns:
            Resolved absolute `Path` object.

        Raises:
            ValueError: If path traversal is attempted in `virtual_mode` or if the
                resolved path escapes the root directory.
        """
        if self.virtual_mode:
            vpath = key if key.startswith("/") else "/" + key
            # P1-2: split on path separators so a legitimate filename
            # containing ``..`` (e.g. "version..backup") doesn't trip the
            # check. Substring matching also rejected ``foo..bar`` which
            # was a false positive. The real security check below
            # (``relative_to(self.cwd)``) is what actually keeps reads
            # inside the root — this part-based test is the
            # ``virtual_mode`` UX-friendly fast path.
            vparts = vpath.replace("\\", "/").split("/")
            if ".." in vparts or vpath.startswith("~"):
                # Keep the exact prefix ``"Path traversal not allowed"`` so
                # existing pytest.raises(match=...) assertions continue to
                # work; append the actionable hint after the colon.
                msg = (
                    "Path traversal not allowed in virtual_mode "
                    "(.. or ~). Set BOG_AGENTS_FS_UNSANDBOXED=1 to "
                    "disable this check, or invoke bog-agents from a "
                    "parent directory that contains the paths you need."
                )
                raise ValueError(msg)
            full = (self.cwd / vpath.lstrip("/")).resolve()
            try:
                full.relative_to(self.cwd)
            except ValueError:
                msg = (
                    f"Path: {full} is outside the agent's root directory: "
                    f"{self.cwd}. The filesystem sandbox only permits paths "
                    f"under root_dir. Options:\n"
                    f"  1. Set env var BOG_AGENTS_FS_UNSANDBOXED=1 to "
                    f"disable the sandbox (cross-repo / system-wide access).\n"
                    f"  2. Restart bog-agents from a parent directory that "
                    f"contains both root and the target path.\n"
                    f"  3. Use a path relative to {self.cwd}."
                )
                raise ValueError(msg) from None
            return full

        # Windows safety net: a POSIX-shaped path that starts with `/` or
        # `\` but has no drive letter (e.g. "/foo/bar") would otherwise be
        # treated as drive-rooted by pathlib — `(E:/cwd) / "/foo/bar"`
        # resolves to `E:\foo\bar` (drive root), silently mis-routing
        # writes. Local LLMs (Llama 3.1, Gemma 4) emit paths in this shape
        # all the time. Treat them as cwd-relative instead. A drive-letter
        # path (`C:\foo`, `D:/data`) is still honoured as truly absolute.
        if sys.platform == "win32" and (key.startswith(("/", "\\"))) and not re.match(r"^[\\/][a-zA-Z]:", key):
            stripped = key.lstrip("/\\")
            if stripped:
                logger.debug(
                    "Rewriting drive-rooted path '%s' to cwd-relative '%s' on Windows",
                    key,
                    stripped,
                )
                return (self.cwd / stripped).resolve()

        path = Path(key)
        if path.is_absolute():
            return path
        return (self.cwd / path).resolve()

    def _physical_base(self, key: str) -> Path:
        r"""Physical path for `key` with its FINAL component left unresolved.

        `_resolve_path` fully resolves the path — following a symlink at the final
        component — which is exactly what the sandbox-escape check needs, but it
        also defeats `O_NOFOLLOW` and `Path.is_symlink()` at the leaf (they end up
        inspecting the symlink's *target*, not the link). Operations that must not
        traverse a symlinked final component (`write`, `delete`) call
        `_resolve_path` first for validation, then act on this leaf-unresolved
        path so a symlinked leaf is refused (write) or unlinked as a link
        (delete) instead of being followed into its target.

        Ancestor directories are still followed by the OS; a symlinked ancestor
        that escapes the sandbox is already rejected by the preceding
        `_resolve_path` call, so only the final component needs guarding here.

        Args:
            key: The caller-supplied path, mapped the same way as `_resolve_path`.

        Returns:
            The physical `Path` whose parent may contain symlinks (validated
                in-sandbox) but whose final component is not symlink-resolved.
        """
        if self.virtual_mode:
            vpath = key if key.startswith("/") else "/" + key
            return self.cwd / vpath.lstrip("/")
        if sys.platform == "win32" and key.startswith(("/", "\\")) and not re.match(r"^[\\/][a-zA-Z]:", key):
            stripped = key.lstrip("/\\")
            if stripped:
                return self.cwd / stripped
        path = Path(key)
        if path.is_absolute():
            return path
        return self.cwd / path

    def _to_virtual_path(self, path: Path) -> str:
        """Convert a filesystem path to a virtual path relative to cwd.

        Args:
            path: Filesystem path to convert.

        Returns:
            Forward-slash relative path string prefixed with `/`.

        Raises:
            ValueError: If path is outside cwd.
            OSError: If path cannot be resolved (broken symlink, permission denied).
        """
        return "/" + path.resolve().relative_to(self.cwd).as_posix()

    def ls(self, path: str) -> LsResult:  # Complex virtual_mode logic
        """List files and directories in the specified directory (non-recursive).

        Args:
            path: Absolute directory path to list files from.

        Returns:
            `LsResult` whose `entries` hold the files and directories directly in
                the directory. Directories have a trailing `/` in their path and
                `is_dir=True`. A missing path (or a path that is not a directory)
                yields an empty entry list rather than an error, matching the
                behavior the filesystem middleware and `CompositeBackend` rely on.
        """
        dir_path = self._resolve_path(path)
        if not dir_path.exists() or not dir_path.is_dir():
            return LsResult(entries=[])

        results: list[FileInfo] = []

        # Convert cwd to string for comparison
        cwd_str = str(self.cwd)
        if not cwd_str.endswith("/"):
            cwd_str += "/"

        # List only direct children (non-recursive)
        try:
            for child_path in dir_path.iterdir():
                try:
                    is_file = child_path.is_file()
                    is_dir = child_path.is_dir()
                except OSError:
                    continue

                abs_path = str(child_path)

                if not self.virtual_mode:
                    # Non-virtual mode: use absolute paths
                    if is_file:
                        try:
                            st = child_path.stat()
                            results.append(
                                {
                                    "path": abs_path,
                                    "is_dir": False,
                                    "size": int(st.st_size),
                                    "modified_at": datetime.fromtimestamp(st.st_mtime).isoformat(),  # noqa: DTZ006  # Local filesystem timestamps don't need timezone
                                }
                            )
                        except OSError:
                            results.append({"path": abs_path, "is_dir": False})
                    elif is_dir:
                        try:
                            st = child_path.stat()
                            results.append(
                                {
                                    "path": abs_path + "/",
                                    "is_dir": True,
                                    "size": 0,
                                    "modified_at": datetime.fromtimestamp(st.st_mtime).isoformat(),  # noqa: DTZ006  # Local filesystem timestamps don't need timezone
                                }
                            )
                        except OSError:
                            results.append({"path": abs_path + "/", "is_dir": True})
                else:
                    # Virtual mode: strip cwd prefix using Path for cross-platform support
                    try:
                        virt_path = self._to_virtual_path(child_path)
                    except ValueError:
                        logger.debug("Skipping path outside root: %s", child_path)
                        continue
                    except OSError:
                        logger.warning("Could not resolve path: %s", child_path, exc_info=True)
                        continue

                    if is_file:
                        try:
                            st = child_path.stat()
                            results.append(
                                {
                                    "path": virt_path,
                                    "is_dir": False,
                                    "size": int(st.st_size),
                                    "modified_at": datetime.fromtimestamp(st.st_mtime).isoformat(),  # noqa: DTZ006  # Local filesystem timestamps don't need timezone
                                }
                            )
                        except OSError:
                            results.append({"path": virt_path, "is_dir": False})
                    elif is_dir:
                        try:
                            st = child_path.stat()
                            results.append(
                                {
                                    "path": virt_path + "/",
                                    "is_dir": True,
                                    "size": 0,
                                    "modified_at": datetime.fromtimestamp(st.st_mtime).isoformat(),  # noqa: DTZ006  # Local filesystem timestamps don't need timezone
                                }
                            )
                        except OSError:
                            results.append({"path": virt_path + "/", "is_dir": True})
        except (OSError, PermissionError):
            pass

        # Keep deterministic order by path
        results.sort(key=lambda x: x.get("path", ""))
        return LsResult(entries=results)

    def ls_info(self, path: str) -> list[FileInfo]:
        """List files and directories in a directory (legacy shape of `ls`).

        Args:
            path: Absolute directory path to list files from.

        Returns:
            List of `FileInfo` dicts. Empty when the path does not exist or is
                not a directory.
        """
        return self.ls(path).entries or []

    async def als_info(self, path: str) -> list[FileInfo]:
        """Async version of `ls_info`.

        Args:
            path: Absolute directory path to list files from.

        Returns:
            List of `FileInfo` dicts. Empty when the path does not exist or is
                not a directory.
        """
        result = await self.als(path)
        return result.entries or []

    def _read_binary(self, resolved_path: Path, file_path: str, file_type: str) -> ReadResult:
        """Read a non-text file as base64-encoded `FileData`.

        Args:
            resolved_path: Security-resolved absolute path to the file.
            file_path: The caller-supplied path, used only in error messages.
            file_type: Classification from `_get_backend_read_file_type`.

        Returns:
            `ReadResult` carrying base64 `FileData`, or an error when the file
                exceeds the binary/video size cap.
        """
        max_bytes = MAX_VIDEO_INPUT_BYTES if file_type == "video" else MAX_BINARY_BYTES
        try:
            file_size = resolved_path.stat().st_size
        except OSError as e:
            return ReadResult(error=f"Error reading file '{file_path}': {e}")
        if file_size > max_bytes:
            return ReadResult(error=f"Error: File '{file_path}' is {file_size} bytes, which exceeds the maximum readable size of {max_bytes} bytes.")

        try:
            fd = os.open(resolved_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as f:
                raw = f.read()
        except OSError as e:
            return ReadResult(error=f"Error reading file '{file_path}': {e}")

        encoded = base64.standard_b64encode(raw).decode("ascii")
        return ReadResult(file_data=FileData(content=encoded, encoding="base64"))

    def read_file(
        self,
        file_path: str,
        offset: int = 0,
        limit: int = 2000,
    ) -> ReadResult:
        """Read raw file data for a line window.

        Text files are decoded as UTF-8 and sliced to the requested window.
        Non-text files (images, audio, video, PDFs, and other known binary
        containers, per `_get_backend_read_file_type`) are returned whole as
        base64 — `offset` / `limit` do not apply to them, since line slicing a
        binary payload would corrupt it.

        Args:
            file_path: Absolute or relative file path.
            offset: Line offset to start reading from (0-indexed).
            limit: Maximum number of lines to read.

        Returns:
            `ReadResult` carrying the sliced `FileData`, or an error message.
        """
        resolved_path = self._resolve_path(file_path)

        if not resolved_path.exists() or not resolved_path.is_file():
            return ReadResult(error=f"Error: File '{file_path}' not found")

        file_type = _get_backend_read_file_type(file_path)
        if file_type != "text":
            return self._read_binary(resolved_path, file_path, file_type)

        # Guard against reading an unbounded file fully into memory. The whole
        # file is buffered before slicing by offset/limit, so a multi-GB log would
        # OOM the process. Short-circuit on stat size (mirrors the grep fallback's
        # max_file_size_bytes skip) and suggest a tighter range or grep.
        try:
            file_size = resolved_path.stat().st_size
        except OSError as e:
            return ReadResult(error=f"Error reading file '{file_path}': {e}")
        if file_size > self.max_file_size_bytes:
            return ReadResult(
                error=(
                    f"Error: File '{file_path}' is {file_size} bytes, which exceeds the "
                    f"maximum readable size of {self.max_file_size_bytes} bytes. Use grep "
                    f"to search within it, or read a smaller portion with a tighter offset/limit range."
                )
            )

        try:
            # Open with O_NOFOLLOW where available to avoid symlink traversal
            fd = os.open(resolved_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "r", encoding="utf-8") as f:
                content = f.read()
        except (OSError, UnicodeDecodeError) as e:
            return ReadResult(error=f"Error reading file '{file_path}': {e}")

        if check_empty_content(content):
            return ReadResult(file_data=FileData(content="", encoding="utf-8"))

        # `splitlines(keepends=True)` preserves whether the final line has a
        # terminator, so the window round-trips the file's trailing-newline state.
        lines = content.splitlines(keepends=True)
        start_idx = offset
        end_idx = min(start_idx + limit, len(lines))

        if start_idx >= len(lines):
            return ReadResult(error=f"Error: Line offset {offset} exceeds file length ({len(lines)} lines)")

        return ReadResult(file_data=FileData(content="".join(lines[start_idx:end_idx]), encoding="utf-8"))

    def read(
        self,
        file_path: str,
        offset: int = 0,
        limit: int = 2000,
    ) -> str:
        """Read file content with line numbers (rendered form of `read_file`).

        Args:
            file_path: Absolute or relative file path.
            offset: Line offset to start reading from (0-indexed).
            limit: Maximum number of lines to read.

        Returns:
            Formatted file content with line numbers, or error message. Binary
                files are refused here rather than rendered: base64 is meaningless
                as numbered text and would flood the caller's context. Use
                `read_file` or `download_files` for binary content.
        """
        result = self.read_file(file_path, offset, limit)
        if result.error is not None:
            return result.error
        if result.file_data is None:
            return EMPTY_CONTENT_WARNING
        if result.file_data.get("encoding") == "base64":
            return f"Error: File '{file_path}' is binary. Use `read_file` for base64 content, or `download_files` for raw bytes."

        content = file_data_to_string(result.file_data)
        empty_msg = check_empty_content(content)
        if empty_msg:
            return empty_msg
        return format_content_with_line_numbers(content, start_line=offset + 1)

    def write(
        self,
        file_path: str,
        content: str,
    ) -> WriteResult:
        """Write content to a file, creating it or overwriting it if it already exists.

        Overwrite (rather than error-on-exists) is the upstream deepagents contract, and every
        other backend in this package follows it. Diverging here would make a `CompositeBackend`
        that routes a `FilesystemBackend` default alongside a `StateBackend` behave differently
        for the same `write` call depending only on which route the path landed in.

        Args:
            file_path: Path where the file will be written.
            content: Text content to write to the file.

        Returns:
            `WriteResult` with path on success, or an error message if the write fails.
                External storage sets `files_update=None`.
        """
        # _resolve_path validates the sandbox (and rejects a symlink that escapes
        # root). The actual open targets the leaf-unresolved path so O_NOFOLLOW
        # refuses a write THROUGH a symlink at the final component — _resolve_path
        # would have followed it to its target, silently defeating O_NOFOLLOW.
        self._resolve_path(file_path)
        nofollow_path = self._physical_base(file_path)

        try:
            # Create parent directories if needed
            nofollow_path.parent.mkdir(parents=True, exist_ok=True)

            # Prefer O_NOFOLLOW to avoid writing through symlinks
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(nofollow_path, flags, 0o644)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)

            return WriteResult(path=file_path, files_update=None)
        except (OSError, UnicodeEncodeError) as e:
            return WriteResult(error=f"Error writing file '{file_path}': {e}")

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        """Edit a file by replacing string occurrences.

        Args:
            file_path: Path to the file to edit.
            old_string: The text to search for and replace.
            new_string: The replacement text.
            replace_all: If `True`, replace all occurrences. If `False` (default),
                replace only if exactly one occurrence exists.

        Returns:
            `EditResult` with path and occurrence count on success, or error
                message if file not found or replacement fails. External storage sets
                `files_update=None`.
        """
        resolved_path = self._resolve_path(file_path)

        if not resolved_path.exists() or not resolved_path.is_file():
            return EditResult(error=f"Error: File '{file_path}' not found")

        try:
            # Read securely
            fd = os.open(resolved_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "r", encoding="utf-8") as f:
                content = f.read()

            result = perform_string_replacement(content, old_string, new_string, replace_all)

            if isinstance(result, str):
                return EditResult(error=result)

            new_content, occurrences = result

            # Write crash-safely: a sibling temp file + fsync + atomic os.replace so
            # an interrupt/ENOSPC mid-write can no longer truncate-then-lose the
            # original (the old O_WRONLY|O_TRUNC path destroyed it). O_NOFOLLOW
            # symlink protection is preserved inside the helper.
            self._atomic_write(resolved_path, new_content)

            return EditResult(path=file_path, files_update=None, occurrences=int(occurrences))
        except (OSError, UnicodeDecodeError, UnicodeEncodeError) as e:
            return EditResult(error=f"Error editing file '{file_path}': {e}")

    def _deleted_paths_under(self, resolved_path: Path, file_path: str) -> list[str]:
        """Enumerate the caller-visible paths a recursive delete of `resolved_path` removes.

        Args:
            resolved_path: Security-resolved absolute path about to be deleted.
            file_path: The caller-supplied path (reported as-is for a plain file).

        Returns:
            Every file path that will disappear, in the backend's own path shape
                (virtual paths in `virtual_mode`, absolute paths otherwise). A
                tree that cannot be walked reports just `file_path`.
        """
        if resolved_path.is_symlink() or not resolved_path.is_dir():
            return [file_path]

        paths: list[str] = []
        try:
            for child in resolved_path.rglob("*"):
                try:
                    if not child.is_file():
                        continue
                except OSError:
                    continue
                if self.virtual_mode:
                    try:
                        paths.append(self._to_virtual_path(child))
                    except (OSError, ValueError):
                        continue
                else:
                    paths.append(str(child))
        except OSError:
            return [file_path]
        paths.sort()
        return paths

    def delete(self, file_path: str) -> DeleteResult:
        """Delete a file or directory from the filesystem.

        Files are unlinked. Directories are removed recursively along with all of
        their contents. Symlinks are removed as links and never followed into
        their target, so deleting a symlink to a directory removes only the link.

        The path is routed through `_resolve_path`, so in `virtual_mode` a delete
        cannot escape `root_dir`.

        Args:
            file_path: Path to the file or directory to delete.

        Returns:
            `DeleteResult` with the deleted path and the paths removed on success,
                or an error if the path is unreachable, does not exist, or removal
                fails. `files_update` is always `None`: this backend persists to
                disk rather than to graph state.
        """
        try:
            # Validate the sandbox (rejects a symlink whose target escapes root).
            self._resolve_path(file_path)
        except (ValueError, OSError) as e:
            return DeleteResult(error=f"Error deleting '{file_path}': {e}")

        # Operate on the leaf-unresolved path so a symlinked final component is
        # removed as a *link* rather than followed into (and destroying) its
        # target — _resolve_path would have resolved it to the target directory.
        target = self._physical_base(file_path)

        try:
            if not target.exists() and not target.is_symlink():
                return DeleteResult(error=f"Error: '{file_path}' not found")

            deleted_paths = self._deleted_paths_under(target, file_path)

            if target.is_symlink() or not target.is_dir():
                target.unlink()
            else:
                shutil.rmtree(target)
        except OSError as e:
            return DeleteResult(error=f"Error deleting '{file_path}': {e}")

        return DeleteResult(path=file_path, files_update=None, deleted_paths=deleted_paths)

    async def adelete(self, file_path: str) -> DeleteResult:
        """Async version of `delete`.

        Args:
            file_path: Path to the file or directory to delete.

        Returns:
            `DeleteResult` with the deleted path and the paths removed on success,
                or an error on failure.
        """
        return await anyio.to_thread.run_sync(self.delete, file_path)  # ty: ignore[unresolved-attribute]

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> GrepResult:
        """Search for a literal text pattern in files.

        Uses ripgrep if available, falling back to a bounded Python search.

        Args:
            pattern: Literal string to search for (NOT regex).
            path: Directory or file path to search in. Defaults to current directory.
            glob: Optional glob pattern to filter which files to search.

        Returns:
            `GrepResult` with matches. `truncated` is `True` when the Python
                fallback hit its wall-clock budget, leaving `matches` valid but
                incomplete.
        """
        # Resolve base path
        try:
            base_full = self._resolve_path(path or ".")
        except ValueError:
            return GrepResult(matches=[])

        if not base_full.exists():
            return GrepResult(matches=[])

        # Try ripgrep first (with -F flag for literal search)
        results = self._ripgrep_search(pattern, base_full, glob)
        truncated = False
        if results is None:
            results, truncated = self._python_search(pattern, base_full, glob)

        matches: list[GrepMatch] = []
        for fpath, items in results.items():
            for line_num, line_text in items:
                matches.append({"path": fpath, "line": int(line_num), "text": line_text})
        return GrepResult(matches=matches, truncated=truncated)

    def grep_raw(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> list[GrepMatch] | str:
        """Search for a literal text pattern in files (legacy shape of `grep`).

        Args:
            pattern: Literal string to search for (NOT regex).
            path: Directory or file path to search in. Defaults to current directory.
            glob: Optional glob pattern to filter which files to search.

        Returns:
            List of `GrepMatch` dicts on success, or the error message.
        """
        result = self.grep(pattern, path, glob)
        if result.error is not None:
            return result.error
        return result.matches or []

    def _ripgrep_search(
        self, pattern: str, base_full: Path, include_glob: str | None
    ) -> dict[str, list[tuple[int, str]]] | None:  # Split except clauses for logging
        """Search using ripgrep with fixed-string (literal) mode.

        Args:
            pattern: Literal string to search for (unescaped).
            base_full: Resolved base path to search in.
            include_glob: Optional glob pattern to filter files.

        Returns:
            Dict mapping file paths to list of `(line_number, line_text)` tuples.
                Returns `None` if ripgrep is unavailable or times out.
        """
        cmd = ["rg", "--json", "-F"]  # -F enables fixed-string (literal) mode
        if include_glob:
            cmd.extend(["--glob", include_glob])
        cmd.extend(["--", pattern, str(base_full)])

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=DEFAULT_GREP_TIMEOUT,
                check=False,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, PermissionError):
            return None

        results: dict[str, list[tuple[int, str]]] = {}
        for line in proc.stdout.splitlines():
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if data.get("type") != "match":
                continue
            pdata = data.get("data", {})
            ftext = pdata.get("path", {}).get("text")
            if not ftext:
                continue
            p = Path(ftext)
            if self.virtual_mode:
                try:
                    virt = self._to_virtual_path(p)
                except ValueError:
                    logger.debug("Skipping grep result outside root: %s", p)
                    continue
                except OSError:
                    logger.warning("Could not resolve grep result path: %s", p, exc_info=True)
                    continue
            else:
                virt = str(p)
            ln = pdata.get("line_number")
            lt = pdata.get("lines", {}).get("text", "").rstrip("\n")
            if ln is None:
                continue
            results.setdefault(virt, []).append((int(ln), lt))

        return results

    def _python_search(
        self,
        pattern: str,
        base_full: Path,
        include_glob: str | None,
        *,
        timeout: int = DEFAULT_GREP_TIMEOUT,
    ) -> tuple[dict[str, list[tuple[int, str]]], bool]:
        """Fallback search using Python when ripgrep is unavailable.

        Recursively searches files, respecting the `max_file_size_bytes` limit and
        a wall-clock budget so a huge or slow tree cannot hang the caller.

        Args:
            pattern: Literal string to search for (substring match, not regex).
            base_full: Resolved base path to search in.
            include_glob: Optional glob pattern to filter files by name.
            timeout: Maximum wall-clock seconds before the walk is abandoned.

        Returns:
            A `(results, truncated)` tuple. `results` maps file paths to a list of
                `(line_number, line_text)` tuples for every match found before the
                walk stopped. `truncated` is `True` when `timeout` elapsed, leaving
                `results` valid but incomplete.
        """
        deadline = time.monotonic() + timeout
        # `compile_grep_include_glob` gives ripgrep-like include semantics (a
        # separator-free pattern such as `*.py` matches the basename at any depth)
        # and normalizes Windows backslash separators before matching. A bare
        # `globmatch(str(rel_path), ...)` did neither, so `*.py` matched nothing
        # nested and nothing at all on Windows.
        glob_matcher = compile_grep_include_glob(include_glob) if include_glob else None

        results: dict[str, list[tuple[int, str]]] = {}
        root = base_full if base_full.is_dir() else base_full.parent

        for fp in root.rglob("*"):
            if time.monotonic() > deadline:
                logger.warning("Grep timed out after %ss with %d matching file(s); returning partial results", timeout, len(results))
                return results, True
            try:
                if not fp.is_file():
                    continue
            except (PermissionError, OSError):
                continue
            if glob_matcher is not None and not glob_matcher(str(fp.relative_to(root))):
                continue
            try:
                if fp.stat().st_size > self.max_file_size_bytes:
                    continue
            except OSError:
                continue
            try:
                content = fp.read_text(encoding="utf-8")
            except (UnicodeDecodeError, PermissionError, OSError):
                continue
            for line_num, line in enumerate(content.splitlines(), 1):
                if pattern in line:
                    if self.virtual_mode:
                        try:
                            virt_path = self._to_virtual_path(fp)
                        except ValueError:
                            logger.debug("Skipping grep result outside root: %s", fp)
                            continue
                        except OSError:
                            logger.warning("Could not resolve grep result path: %s", fp, exc_info=True)
                            continue
                    else:
                        virt_path = str(fp)
                    results.setdefault(virt_path, []).append((line_num, line))

        return results, False

    def glob(self, pattern: str, path: str | None = "/") -> GlobResult:  # Complex virtual_mode logic
        """Find files matching a glob pattern.

        Args:
            pattern: Glob pattern to match files against (e.g., `'*.py'`, `'**/*.txt'`).
            path: Base directory to search from. `None` or `'/'` means the backend
                root.

        Returns:
            `GlobResult` whose `matches` are `FileInfo` dicts for matching files,
                sorted by path. `truncated` is `True` when the walk exceeded its
                wall-clock budget and `matches` is therefore partial.

        Raises:
            ValueError: If the pattern attempts traversal in `virtual_mode`.
        """
        # Strip any anchor from the pattern. Models often emit absolute-looking
        # patterns like `/foo/**/*.py` or `E:/proj/**/*.tsx`; treat them all as
        # relative-from-search-root.
        if pattern.startswith(("/", "\\")):
            pattern = pattern.lstrip("/\\")
        elif len(pattern) >= 2 and pattern[1] == ":" and pattern[0].isalpha():  # noqa: PLR2004 — `2` = "<drive-letter>:" prefix length
            # Windows drive-letter prefix: strip it AND any following sep.
            pattern = pattern[2:].lstrip("/\\")

        if self.virtual_mode and ".." in Path(pattern).parts:
            msg = "Path traversal not allowed in glob pattern"
            raise ValueError(msg)

        search_path = self.cwd if path is None or path == "/" else self._resolve_path(path)
        if not search_path.exists() or not search_path.is_dir():
            return GlobResult(matches=[])

        # Walk every entry and apply the pattern ourselves rather than calling
        # `rglob(pattern)`: the latter only yields matches, so a sparse search over
        # a huge tree traverses everything without ever checking the deadline.
        matches_pattern = compile_recursive_glob(pattern)
        deadline = time.monotonic() + DEFAULT_GLOB_TIMEOUT
        truncated = False

        results: list[FileInfo] = []
        try:
            for matched_path in search_path.rglob("*"):
                if time.monotonic() > deadline:
                    logger.warning("Glob timed out after %ss with %d match(es); returning partial results", DEFAULT_GLOB_TIMEOUT, len(results))
                    truncated = True
                    break
                try:
                    rel_path = str(matched_path.relative_to(search_path))
                except ValueError:
                    continue
                if not matches_pattern(rel_path):
                    continue
                try:
                    is_file = matched_path.is_file()
                except (PermissionError, OSError):
                    continue
                if not is_file:
                    continue
                if self.virtual_mode:
                    try:
                        matched_path.resolve().relative_to(self.cwd)
                    except ValueError:
                        continue
                abs_path = str(matched_path)
                if not self.virtual_mode:
                    try:
                        st = matched_path.stat()
                        results.append(
                            {
                                "path": abs_path,
                                "is_dir": False,
                                "size": int(st.st_size),
                                "modified_at": datetime.fromtimestamp(st.st_mtime).isoformat(),  # noqa: DTZ006  # Local filesystem timestamps don't need timezone
                            }
                        )
                    except OSError:
                        results.append({"path": abs_path, "is_dir": False})
                else:
                    # Virtual mode: use Path for cross-platform support
                    try:
                        virt = self._to_virtual_path(matched_path)
                    except ValueError:
                        logger.debug("Skipping glob result outside root: %s", matched_path)
                        continue
                    except OSError:
                        logger.warning("Could not resolve glob result path: %s", matched_path, exc_info=True)
                        continue
                    try:
                        st = matched_path.stat()
                        results.append(
                            {
                                "path": virt,
                                "is_dir": False,
                                "size": int(st.st_size),
                                "modified_at": datetime.fromtimestamp(st.st_mtime).isoformat(),  # noqa: DTZ006  # Local filesystem timestamps don't need timezone
                            }
                        )
                    except OSError:
                        results.append({"path": virt, "is_dir": False})
        except OSError:
            # `rglob` raised mid-iteration (entry unlinked during the walk, NFS
            # drop, permission flip). Keep whatever was collected and flag it as
            # incomplete rather than presenting a partial list as authoritative.
            logger.warning("Glob aborted partway with %d match(es)", len(results), exc_info=True)
            truncated = True

        results.sort(key=lambda x: x.get("path", ""))
        return GlobResult(matches=results, truncated=truncated)

    def glob_info(self, pattern: str, path: str = "/") -> list[FileInfo]:
        """Find files matching a glob pattern (legacy shape of `glob`).

        Args:
            pattern: Glob pattern to match files against (e.g., `'*.py'`, `'**/*.txt'`).
            path: Base directory to search from. Defaults to root (`/`).

        Returns:
            List of `FileInfo` dicts for matching files, sorted by path.
        """
        return self.glob(pattern, path).matches or []

    async def aglob_info(self, pattern: str, path: str | None = "/") -> list[FileInfo]:
        """Async version of `glob_info`.

        Args:
            pattern: Glob pattern to match files against.
            path: Base directory to search from. Defaults to root (`/`).

        Returns:
            List of `FileInfo` dicts for matching files, sorted by path.
        """
        result = await self.aglob(pattern, path)
        return result.matches or []

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """Upload multiple files to the filesystem.

        Args:
            files: List of (path, content) tuples where content is bytes.

        Returns:
            List of FileUploadResponse objects, one per input file.
            Response order matches input order.
        """
        responses: list[FileUploadResponse] = []
        for path, content in files:
            try:
                resolved_path = self._resolve_path(path)

                # Create parent directories if needed
                resolved_path.parent.mkdir(parents=True, exist_ok=True)

                # Crash-safe write: sibling temp file + fsync + atomic os.replace so a
                # mid-write interrupt/ENOSPC can't truncate-then-lose an existing file
                # being overwritten. O_NOFOLLOW symlink protection is preserved.
                self._atomic_write(resolved_path, content)

                responses.append(FileUploadResponse(path=path, error=None))
            except FileNotFoundError:
                responses.append(FileUploadResponse(path=path, error="file_not_found"))
            except PermissionError:
                responses.append(FileUploadResponse(path=path, error="permission_denied"))
            except (ValueError, OSError) as e:
                # ValueError from _resolve_path for path traversal, OSError for other file errors
                if isinstance(e, ValueError) or "invalid" in str(e).lower():
                    responses.append(FileUploadResponse(path=path, error="invalid_path"))
                else:
                    # Generic error fallback
                    responses.append(FileUploadResponse(path=path, error="invalid_path"))

        return responses

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Download multiple files from the filesystem.

        Args:
            paths: List of file paths to download.

        Returns:
            List of FileDownloadResponse objects, one per input path.
        """
        responses: list[FileDownloadResponse] = []
        for path in paths:
            try:
                resolved_path = self._resolve_path(path)
                if resolved_path.is_dir():
                    responses.append(FileDownloadResponse(path=path, content=None, error="is_directory"))
                    continue
                # Guard against buffering an unbounded file fully into memory
                # (mirrors read()'s stat-based short-circuit). Reuse the existing
                # invalid_path code since FileOperationError has no size-specific literal.
                if resolved_path.stat().st_size > self.max_file_size_bytes:
                    responses.append(FileDownloadResponse(path=path, content=None, error="invalid_path"))
                    continue
                # Use flags to optionally prevent symlink following if
                # supported by the OS
                fd = os.open(resolved_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(fd, "rb") as f:
                    content = f.read()
                responses.append(FileDownloadResponse(path=path, content=content, error=None))
            except FileNotFoundError:
                responses.append(FileDownloadResponse(path=path, content=None, error="file_not_found"))
            except PermissionError:
                responses.append(FileDownloadResponse(path=path, content=None, error="permission_denied"))
            except IsADirectoryError:
                responses.append(FileDownloadResponse(path=path, content=None, error="is_directory"))
            except ValueError:
                responses.append(FileDownloadResponse(path=path, content=None, error="invalid_path"))
            except OSError:
                # O_NOFOLLOW on a symlink (ELOOP), FIFO/device (ENXIO), EIO, etc.
                # raise plain OSError; catch them so one bad file doesn't abort
                # the whole batch (preserves the partial-success contract).
                responses.append(FileDownloadResponse(path=path, content=None, error="invalid_path"))
        return responses
