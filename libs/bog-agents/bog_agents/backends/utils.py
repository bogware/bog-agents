"""Shared utility functions for memory backend implementations.

This module contains both user-facing string formatters and structured
helpers used by backends and the composite router. Structured helpers
enable composition without fragile string parsing.
"""

import functools
import os
import re
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any, Final, Literal, overload

import wcmatch.glob as wcglob

from bog_agents._api.deprecation import warn_deprecated
from bog_agents.backends.protocol import FileData, FileFormat, FileInfo as _FileInfo, GrepMatch as _GrepMatch, ReadResult

_IS_WINDOWS = sys.platform == "win32"

EMPTY_CONTENT_WARNING = "System reminder: File exists but has empty contents"
MAX_LINE_LENGTH = 5000
LINE_NUMBER_WIDTH = 6
TOOL_RESULT_TOKEN_LIMIT = 20000  # Same threshold as eviction
TRUNCATION_GUIDANCE = "... [results truncated, try being more specific with your parameters]"

MAX_BINARY_BYTES: Final = 500 * 1024
"""Maximum size of a binary file a backend will base64-encode into a read result."""

MAX_VIDEO_INPUT_BYTES: Final = 1024 * 1024 * 1024
"""Maximum raw video payload size accepted by `read_file` frame extraction."""

# Re-export protocol types for backwards compatibility
FileInfo = _FileInfo
GrepMatch = _GrepMatch

FileType = Literal["text", "image", "audio", "video", "file"]
"""Classification of a file by extension."""

_EXTENSION_TO_FILE_TYPE: dict[str, FileType] = {
    # Images (https://ai.google.dev/gemini-api/docs/image-understanding)
    ".png": "image",
    ".jpeg": "image",
    ".jpg": "image",
    ".webp": "image",
    ".gif": "image",
    ".heic": "image",
    ".heif": "image",
    # Video (https://ai.google.dev/gemini-api/docs/video-understanding)
    ".mp4": "video",
    ".mpeg": "video",
    ".mov": "video",
    ".avi": "video",
    ".flv": "video",
    ".mpg": "video",
    ".webm": "video",
    ".wmv": "video",
    ".3gpp": "video",
    # Audio (https://ai.google.dev/gemini-api/docs/audio)
    ".wav": "audio",
    ".mp3": "audio",
    ".aiff": "audio",
    ".aac": "audio",
    ".ogg": "audio",
    ".flac": "audio",
    # Files
    ".pdf": "file",
    ".ppt": "file",
    ".pptx": "file",
}
"""Extension-to-type mapping for non-text files.

Optional features may layer on additional classifications at the use site. For
example, `read_file` treats `.mkv` as video only when the optional video
dependencies are installed.

Derived from Google's multimodal API supported formats:

- Images: https://ai.google.dev/gemini-api/docs/image-understanding
- Video: https://ai.google.dev/gemini-api/docs/video-understanding
- Audio: https://ai.google.dev/gemini-api/docs/audio
"""

_VIDEO_EXTRA_EXTENSIONS: frozenset[str] = frozenset({".mkv"})
"""Video container extensions handled outside the Google-derived multimodal map.

These are intentionally absent from `_EXTENSION_TO_FILE_TYPE`, so a `read_file`
without the optional `[video]` extra returns them as a generic file block rather
than a native video block. Backends must still read them as binary — never
text-decode them — and `read_file` layers frame extraction on top only when the
`[video]` dependencies are installed.
"""


def _get_file_type(path: str) -> FileType:
    """Classify a file by its extension.

    Args:
        path: File path to classify.

    Returns:
        One of `"text"`, `"image"`, `"audio"`, `"video"`, or `"file"`. Defaults
            to `"text"` for unrecognized extensions.
    """
    return _EXTENSION_TO_FILE_TYPE.get(PurePosixPath(to_posix_path(path)).suffix.lower(), "text")


def _get_backend_read_file_type(path: str) -> FileType:
    """Classify a file for backend reads, forcing known video containers to binary.

    Backends decide binary-vs-text on `_get_file_type(...) != "text"`. Extensions
    in `_VIDEO_EXTRA_EXTENSIONS` are absent from `_EXTENSION_TO_FILE_TYPE`, so
    `_get_file_type` alone would treat them as text and corrupt the bytes (a raw
    UTF-8 decode of a video, or line-slicing a base64 blob). Classify them as
    `"video"` here so the binary read path runs on every backend.

    Args:
        path: File path to classify.

    Returns:
        `"video"` for `_VIDEO_EXTRA_EXTENSIONS`; otherwise the shared
            `_get_file_type` classification.
    """
    if PurePosixPath(to_posix_path(path)).suffix.lower() in _VIDEO_EXTRA_EXTENSIONS:
        return "video"
    return _get_file_type(path)


# ---------------------------------------------------------------------------
# Glob compilation
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=256)
def compile_grep_include_glob(pattern: str) -> Callable[[str], bool]:
    """Compile a grep include-glob into a matcher with ripgrep-like semantics.

    Provides one shared include-glob behavior for every backend so the same
    `grep(..., glob=...)` call closely mirrors ripgrep for common include
    patterns, whether or not ripgrep is installed:

    - Patterns without a `/` match the basename at any depth. Example: `*.py`
        matches `src/app/main.py`.
    - Patterns containing a `/` match the path relative to the grep search root,
        with `**` support. Example: `src/**/*.py` matches `src/app/main.py`.
    - A leading `/` anchors the pattern to the search root; it narrows the match
        rather than widening it. Example: `/*.py` matches `top.py` but not
        `src/app/main.py`.

    Exclusion/negation patterns (a leading `!`) are not supported: the `!` is
    treated literally rather than inverting the match, so results for such
    patterns can diverge from `rg --glob '!...'`.

    Args:
        pattern: Glob include pattern.

    Returns:
        Predicate accepting a search-root-relative POSIX path; returns True when
            the path is included by `pattern`.
    """
    flags = wcglob.BRACE | wcglob.GLOBSTAR
    # A leading `/` anchors to the search root: strip it so it matches against
    # the (slash-less) relative path, but decide anchoring from the original
    # pattern so `/*.py` stays root-anchored instead of collapsing to a
    # basename-at-any-depth match.
    anchored = "/" in pattern
    compiled = wcglob.compile(pattern.lstrip("/"), flags=flags)

    if anchored:

        def matcher(rel_path: str) -> bool:
            return bool(compiled.match(to_posix_path(rel_path)))
    else:

        def matcher(rel_path: str) -> bool:
            return bool(compiled.match(PurePosixPath(to_posix_path(rel_path)).name))

    return matcher


@functools.lru_cache(maxsize=256)
def compile_recursive_glob(pattern: str) -> Callable[[str], bool]:
    r"""Compile a `glob` pattern into a per-entry matcher for a recursive walk.

    `Path.rglob(pattern)` is equivalent to `Path.glob("**/" + pattern)`, so the
    pattern matches at any depth (e.g. `*.py` matches `src/app/main.py`). Prefix
    the pattern with `**/` and compile it with globstar support so a matcher can
    be applied to each visited entry while walking the tree, letting the caller
    enforce a deadline on every entry instead of only on matched paths.

    Depth (`GLOBSTAR`) and dotfile matching (`DOTMATCH`) mirror `Path.rglob`:
    `DOTMATCH` is required because `wcmatch` excludes dotfiles by default whereas
    stdlib `rglob` includes them. Brace expansion (`BRACE`) is an intentional
    *divergence* from `rglob` — `{a,b}.py` expands here but `Path.rglob` treats
    the braces literally — chosen so `glob` matches the include-glob semantics of
    `compile_grep_include_glob`.

    The matcher normalizes backslashes before matching, so a Windows-native
    relative path (`src\\app\\main.py`) still matches `**/*.py`.

    Args:
        pattern: Glob pattern (a leading `/` is stripped).

    Returns:
        Predicate accepting a search-root-relative path; returns True when the
            path matches `pattern` under recursive-glob semantics.
    """
    flags = wcglob.BRACE | wcglob.GLOBSTAR | wcglob.DOTMATCH
    compiled = wcglob.compile("**/" + pattern.lstrip("/"), flags=flags)

    def matcher(rel_path: str) -> bool:
        return bool(compiled.match(to_posix_path(rel_path)))

    return matcher


def to_posix_path(path: str) -> str:
    r"""Normalize backslash separators to forward slashes for `PurePosixPath` use.

    Backends running on Windows return OS-native paths using backslashes.
    `PurePosixPath` treats backslashes as literal filename characters, so
    `PurePosixPath(r"C:\a\b").name` yields the full string instead of `"b"`.
    Normalize before constructing a `PurePosixPath`.

    This is best-effort: a POSIX directory literally named with a backslash will
    also be rewritten. That trade-off is accepted because such filenames are
    vanishingly rare in practice and the alternative (gating on `os.sep`) fails
    when a Windows-style path is handed to a non-Windows process.

    Args:
        path: Path string that may use backslash separators.

    Returns:
        The same path with every backslash replaced by `/`. Inputs that already
            use forward slashes are returned unchanged.
    """
    return path.replace("\\", "/")


# Characters that mark a glob path component as a wildcard segment for the
# purposes of `_glob_anchor`. Keep in sync with the wcmatch flags used by the
# filesystem middleware (`BRACE | GLOBSTAR`).
_GLOB_WILDCARD_CHARS = frozenset("*?[{")


def _glob_anchor(pattern: str) -> str:
    """Return the longest leading directory of `pattern` with no wildcards.

    For `/secrets/**` returns `/secrets`; for `/a/*/b` returns `/a`; for a
    pattern with a wildcard at or near the root (`/**/secrets`, `/*/foo`) falls
    back to `/`. The root fallback causes overlap checks to match any subtree —
    conservative over-gating, since we cannot statically pin down where the rule
    could resolve. Callers wanting precise gating should anchor the rule's
    leading components.

    Args:
        pattern: A glob pattern.

    Returns:
        The longest wildcard-free leading directory, or `/` if none.
    """
    parts = PurePosixPath(to_posix_path(pattern)).parts
    safe: list[str] = []
    for part in parts:
        if any(c in _GLOB_WILDCARD_CHARS for c in part):
            break
        safe.append(part)
    if not safe:
        return "/"
    return str(PurePosixPath(*safe))


def _paths_overlap(call_path: str, rule_anchor: str) -> bool:
    """Return True if the subtree at `call_path` intersects the subtree at `rule_anchor`.

    Two subtrees overlap when one is a (component-wise) prefix of the other, or
    they're equal. Comparison runs on `PurePosixPath` components, so `/secret`
    does not overlap `/secrets`. The root `/` overlaps everything.

    Args:
        call_path: Normalized path of the call's search root.
        rule_anchor: Anchor (wildcard-free prefix) of a rule's pattern.

    Returns:
        True if the two subtrees intersect.
    """
    a = PurePosixPath(call_path)
    b = PurePosixPath(rule_anchor)
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


# ---------------------------------------------------------------------------
# FileData v1 and v2 conversion
# ---------------------------------------------------------------------------


def _normalize_content(file_data: FileData | dict[str, Any]) -> str:
    r"""Normalize `file_data` content to a plain string.

    Single backwards-compatibility conversion point for the legacy (v1)
    `list[str]` file format. v2 stores `content` as a plain `str`; persisted
    checkpoints and pickled state in the wild still carry a list of lines.

    Args:
        file_data: `FileData` dict with a `content` key.

    Returns:
        Content as a single string.
    """
    content = file_data.get("content", "")
    if isinstance(content, list):
        warn_deprecated(
            since="0.10.0",
            removal="1.0.0",
            message=(
                "`FileData` with `list[str]` content (the v1 format) is deprecated and will be removed in bog-agents==1.0.0. "
                "Store `content` as a plain `str` with an `encoding` field instead."
            ),
            package="bog-agents",
            stacklevel=3,
        )
        return "\n".join(content)
    return content


def _to_legacy_file_data(file_data: FileData | dict[str, Any]) -> dict[str, Any]:
    r"""Convert a `FileData` dict to the legacy (v1) storage format.

    The v1 format stores content as `list[str]` (lines split on `\n`) and omits
    the `encoding` field. Use this when `file_format="v1"` on a backend, to
    preserve compatibility with consumers that expect `list[str]` content.

    Args:
        file_data: `FileData` with `content: str` (and, usually, `encoding`).

    Returns:
        Dict with `content` as `list[str]`, plus whichever of `created_at` /
            `modified_at` were present. No `encoding` key.
    """
    content = file_data.get("content", "")
    if isinstance(content, list):
        # Already v1 — pass the lines through rather than splitting a joined copy.
        lines = list(content)
    else:
        lines = content.split("\n")

    result: dict[str, Any] = {"content": lines}
    if "created_at" in file_data:
        result["created_at"] = file_data["created_at"]
    if "modified_at" in file_data:
        result["modified_at"] = file_data["modified_at"]
    return result


def file_data_to_string(file_data: FileData | dict[str, Any]) -> str:
    """Convert `FileData` to plain string content.

    Accepts both the v2 (`content: str`) and legacy v1 (`content: list[str]`)
    shapes.

    Args:
        file_data: `FileData` dict with a `content` key.

    Returns:
        Content as a single string.
    """
    return _normalize_content(file_data)


def create_file_data(
    content: str,
    created_at: str | None = None,
    *,
    encoding: str = "utf-8",
    file_format: FileFormat = "v2",
) -> dict[str, Any]:
    r"""Create a `FileData` object with timestamps.

    Args:
        content: File content as a string (plain text, or base64-encoded binary
            when `encoding="base64"`).
        created_at: Optional creation timestamp (ISO format).
        encoding: Content encoding — `"utf-8"` for text, `"base64"` for binary.
            Only representable in the v2 format.
        file_format: Storage format to emit. Defaults to `"v2"` (`content` as a
            plain `str` plus an `encoding` field). `"v1"` emits the legacy shape
            (`content` as `list[str]`, no `encoding`) for callers that must keep
            writing checkpoints readable by pre-0.10 consumers.

    Returns:
        `FileData` dict with content and timestamps.

    Raises:
        ValueError: If `file_format="v1"` is combined with a non-`"utf-8"`
            encoding. v1 has no `encoding` field, so the encoding would be
            silently dropped and the content later decoded as text.
    """
    now = datetime.now(UTC).isoformat()

    if file_format == "v1":
        if encoding != "utf-8":
            msg = f"file_format='v1' cannot represent encoding={encoding!r} (v1 has no `encoding` field). Use file_format='v2' for non-utf-8 content."
            raise ValueError(msg)
        lines = content.split("\n") if isinstance(content, str) else content
        return {
            "content": lines,
            "created_at": created_at or now,
            "modified_at": now,
        }

    return {
        "content": content,
        "encoding": encoding,
        "created_at": created_at or now,
        "modified_at": now,
    }


def update_file_data(file_data: FileData | dict[str, Any], content: str) -> dict[str, Any]:
    """Update `FileData` with new content, preserving creation timestamp and format.

    The storage format of `file_data` is preserved: a v1 dict (list content, no
    `encoding`) updates to v1, a v2 dict updates to v2. This keeps a backend's
    on-disk / in-state representation stable across edits regardless of which
    format it was originally written in.

    Args:
        file_data: Existing `FileData` dict.
        content: New content as a string.

    Returns:
        Updated `FileData` dict in the same format as the input.
    """
    now = datetime.now(UTC).isoformat()
    created_at = file_data.get("created_at") or now
    is_legacy = isinstance(file_data.get("content"), list)

    if is_legacy:
        lines = content.split("\n") if isinstance(content, str) else content
        return {
            "content": lines,
            "created_at": created_at,
            "modified_at": now,
        }

    return {
        "content": content,
        "encoding": file_data.get("encoding", "utf-8"),
        "created_at": created_at,
        "modified_at": now,
    }


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def sanitize_tool_call_id(tool_call_id: str) -> str:
    r"""Sanitize tool_call_id to prevent path traversal and separator issues.

    Replaces dangerous characters (., /, \) with underscores.
    """
    return tool_call_id.replace(".", "_").replace("/", "_").replace("\\", "_")


def format_content_with_line_numbers(
    content: str | list[str],
    start_line: int = 1,
) -> str:
    """Format file content with line numbers (cat -n style).

    Chunks lines longer than MAX_LINE_LENGTH with continuation markers (e.g., 5.1, 5.2).

    Args:
        content: File content as string or list of lines
        start_line: Starting line number (default: 1)

    Returns:
        Formatted content with line numbers and continuation markers
    """
    if isinstance(content, str):
        lines = content.split("\n")
        if lines and lines[-1] == "":
            lines = lines[:-1]
    else:
        lines = content

    result_lines = []
    for i, line in enumerate(lines):
        line_num = i + start_line

        if len(line) <= MAX_LINE_LENGTH:
            result_lines.append(f"{line_num:{LINE_NUMBER_WIDTH}d}\t{line}")
        else:
            # Split long line into chunks with continuation markers
            num_chunks = (len(line) + MAX_LINE_LENGTH - 1) // MAX_LINE_LENGTH
            for chunk_idx in range(num_chunks):
                start = chunk_idx * MAX_LINE_LENGTH
                end = min(start + MAX_LINE_LENGTH, len(line))
                chunk = line[start:end]
                if chunk_idx == 0:
                    # First chunk: use normal line number
                    result_lines.append(f"{line_num:{LINE_NUMBER_WIDTH}d}\t{chunk}")
                else:
                    # Continuation chunks: use decimal notation (e.g., 5.1, 5.2)
                    continuation_marker = f"{line_num}.{chunk_idx}"
                    result_lines.append(f"{continuation_marker:>{LINE_NUMBER_WIDTH}}\t{chunk}")

    return "\n".join(result_lines)


def check_empty_content(content: str) -> str | None:
    """Check if content is empty and return warning message.

    Args:
        content: Content to check

    Returns:
        Warning message if empty, None otherwise
    """
    if not content or content.strip() == "":
        return EMPTY_CONTENT_WARNING
    return None


def slice_read_response(
    file_data: FileData | dict[str, Any],
    offset: int,
    limit: int,
) -> str | ReadResult:
    """Slice file data to the requested line range without formatting.

    Returns raw text for the requested window. Line-number formatting is applied
    downstream (by `format_read_response` or the middleware layer).

    Args:
        file_data: `FileData` dict.
        offset: Line offset (0-indexed).
        limit: Maximum number of lines.

    Returns:
        Raw sliced content string on success, or a `ReadResult` with `error` set
            when the offset exceeds the file length.
    """
    content = file_data_to_string(file_data)

    if not content or content.strip() == "":
        return content

    # `splitlines(keepends=True)` retains each line's terminator, including the
    # absence of one on the final line. Joining with `""` therefore round-trips
    # the trailing-newline state of the file faithfully — required so `edit()`
    # can report EOF-newline mismatches accurately. It also splits on CR / CRLF,
    # so line indexing matches the LF-normalized form without first rewriting the
    # whole (potentially huge) string.
    lines = content.splitlines(keepends=True)
    start_idx = offset
    end_idx = min(start_idx + limit, len(lines))

    if start_idx >= len(lines):
        return ReadResult(error=f"Line offset {offset} exceeds file length ({len(lines)} lines)")

    # Normalize line endings to LF, but only across the requested window.
    # State/Store backends may carry CRLF or CR content as written; downstream
    # tooling (edit match, grep, format) assumes LF.
    return "".join(lines[start_idx:end_idx]).replace("\r\n", "\n").replace("\r", "\n")


def format_read_response(
    file_data: FileData | dict[str, Any],
    offset: int,
    limit: int,
) -> str:
    """Format file data for read response with line numbers.

    Args:
        file_data: FileData dict
        offset: Line offset (0-indexed)
        limit: Maximum number of lines

    Returns:
        Formatted content or error message
    """
    content = file_data_to_string(file_data)
    empty_msg = check_empty_content(content)
    if empty_msg:
        return empty_msg

    lines = content.splitlines()
    start_idx = offset
    end_idx = min(start_idx + limit, len(lines))

    if start_idx >= len(lines):
        return f"Error: Line offset {offset} exceeds file length ({len(lines)} lines)"

    selected_lines = lines[start_idx:end_idx]
    return format_content_with_line_numbers(selected_lines, start_line=start_idx + 1)


def perform_string_replacement(
    content: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> tuple[str, int] | str:
    """Perform string replacement with occurrence validation.

    Args:
        content: Original content
        old_string: String to replace
        new_string: Replacement string
        replace_all: Whether to replace all occurrences

    Returns:
        Tuple of (new_content, occurrences) on success, or error message string
    """
    occurrences = content.count(old_string)

    if occurrences == 0:
        return f"Error: String not found in file: '{old_string}'"

    if occurrences > 1 and not replace_all:
        return (
            f"Error: String '{old_string}' appears {occurrences} times in file. "
            f"Use replace_all=True to replace all instances, or provide a more specific string with surrounding context."
        )

    new_content = content.replace(old_string, new_string)
    return new_content, occurrences


@overload
def truncate_if_too_long(result: list[str]) -> list[str]: ...


@overload
def truncate_if_too_long(result: str) -> str: ...


def truncate_if_too_long(result: list[str] | str) -> list[str] | str:
    """Truncate list or string result if it exceeds token limit (rough estimate: 4 chars/token)."""
    if isinstance(result, list):
        total_chars = sum(len(item) for item in result)
        if total_chars > TOOL_RESULT_TOKEN_LIMIT * 4:
            return result[: len(result) * TOOL_RESULT_TOKEN_LIMIT * 4 // total_chars] + [TRUNCATION_GUIDANCE]  # noqa: RUF005  # Concatenation preferred for clarity
        return result
    # string
    if len(result) > TOOL_RESULT_TOKEN_LIMIT * 4:
        return result[: TOOL_RESULT_TOKEN_LIMIT * 4] + "\n" + TRUNCATION_GUIDANCE
    return result


# ---------------------------------------------------------------------------
# Path handling
# ---------------------------------------------------------------------------


def validate_path(path: str, *, allowed_prefixes: Sequence[str] | None = None) -> str:
    r"""Validate and normalize file path for security.

    Ensures paths are safe to use by preventing directory traversal attacks
    and enforcing consistent formatting. All paths are normalized to use
    forward slashes and start with a leading slash.

    This function is designed for virtual filesystem paths and rejects
    Windows absolute paths (e.g., `C:/...`, `F:/...`) to maintain consistency
    and prevent path format ambiguity.

    Args:
        path: The path to validate and normalize.
        allowed_prefixes: Optional list of allowed path prefixes.

            If provided, the normalized path must start with one of
            these prefixes.

    Returns:
        Normalized canonical path starting with `/` and using forward slashes.

    Raises:
        ValueError: If path contains traversal sequences (`..` or `~`), is a
            Windows absolute path (e.g., `C:/...`), or does not start with an
            allowed prefix when `allowed_prefixes` is specified.

    Example:
        ```python
        validate_path("foo/bar")  # Returns: "/foo/bar"
        validate_path("/./foo//bar")  # Returns: "/foo/bar"
        validate_path("../etc/passwd")  # Raises ValueError
        validate_path(r"C:\\Users\\file.txt")  # Raises ValueError
        validate_path("/data/file.txt", allowed_prefixes=["/data/"])  # OK
        validate_path("/etc/file.txt", allowed_prefixes=["/data/"])  # Raises ValueError
        ```
    """
    # Check for traversal as a path component (not substring) to avoid
    # false-positive rejection of legitimate filenames like "foo..bar.txt"
    parts = PurePosixPath(to_posix_path(path)).parts
    if ".." in parts or path.startswith("~"):
        msg = f"Path traversal not allowed: {path}"
        raise ValueError(msg)

    if _IS_WINDOWS:
        # On Windows, accept native absolute paths (C:\Users\..., D:/data/...)
        # and normalize them while preserving the drive letter.
        normalized = os.path.normpath(path)
        normalized = to_posix_path(normalized)
        # Defense-in-depth: verify normpath didn't produce traversal
        if ".." in normalized.split("/"):
            msg = f"Path traversal detected after normalization: {path} -> {normalized}"
            raise ValueError(msg)
        # For non-drive-letter paths, prepend "/" to match POSIX virtual-path
        # semantics (parity with the non-Windows branch below).
        if not normalized.startswith("/") and not re.match(r"^[a-zA-Z]:", normalized):
            normalized = f"/{normalized}"
        if allowed_prefixes is not None and not any(normalized.startswith(prefix) for prefix in allowed_prefixes):
            msg = f"Path must start with one of {allowed_prefixes}: {path}"
            raise ValueError(msg)
        return normalized

    # Non-Windows: reject Windows absolute paths to prevent format ambiguity
    if re.match(r"^[a-zA-Z]:", path):
        msg = f"Windows absolute paths are not supported: {path}. Please use virtual paths starting with / (e.g., /workspace/file.txt)"
        raise ValueError(msg)

    normalized = os.path.normpath(path)
    normalized = to_posix_path(normalized)

    if not normalized.startswith("/"):
        normalized = f"/{normalized}"

    # Defense-in-depth: verify normpath didn't produce traversal
    if ".." in normalized.split("/"):
        msg = f"Path traversal detected after normalization: {path} -> {normalized}"
        raise ValueError(msg)

    if allowed_prefixes is not None and not any(normalized.startswith(prefix) for prefix in allowed_prefixes):
        msg = f"Path must start with one of {allowed_prefixes}: {path}"
        raise ValueError(msg)

    return normalized


def _normalize_path(path: str | None) -> str:
    """Normalize a path to canonical form.

    Converts path to absolute form starting with /, removes trailing slashes
    (except for root), and validates that the path is not empty.

    Args:
        path: Path to normalize (None defaults to "/")

    Returns:
        Normalized path starting with / (without trailing slash unless it's root)

    Raises:
        ValueError: If path is invalid (empty string after strip)

    Example:
        _normalize_path(None) -> "/"
        _normalize_path("/dir/") -> "/dir"
        _normalize_path("dir") -> "/dir"
        _normalize_path("/") -> "/"
    """
    path = path or "/"
    if not path or path.strip() == "":
        msg = "Path cannot be empty"
        raise ValueError(msg)

    normalized = path if path.startswith("/") else "/" + path

    # Only root should have trailing slash
    if normalized != "/" and normalized.endswith("/"):
        normalized = normalized.rstrip("/")

    return normalized


def _filter_files_by_path(files: dict[str, Any], normalized_path: str) -> dict[str, Any]:
    """Filter files dict by normalized path, handling exact file matches and directory prefixes.

    Expects a normalized path from _normalize_path (no trailing slash except root).

    Args:
        files: Dictionary mapping file paths to file data
        normalized_path: Normalized path from _normalize_path (e.g., "/", "/dir", "/dir/file")

    Returns:
        Filtered dictionary of files matching the path

    Example:
        files = {"/dir/file": {...}, "/dir/other": {...}}
        _filter_files_by_path(files, "/dir/file")  # Returns {"/dir/file": {...}}
        _filter_files_by_path(files, "/dir")       # Returns both files
    """
    # Check if path matches an exact file
    if normalized_path in files:
        return {normalized_path: files[normalized_path]}

    # Otherwise treat as directory prefix
    if normalized_path == "/":
        # Root directory - match all files starting with /
        return {fp: fd for fp, fd in files.items() if fp.startswith("/")}
    # Non-root directory - add trailing slash for prefix matching
    dir_prefix = normalized_path + "/"
    return {fp: fd for fp, fd in files.items() if fp.startswith(dir_prefix)}


def _relative_to_root(file_path: str, normalized_path: str) -> str:
    """Return `file_path` relative to a normalized grep/glob search root.

    Args:
        file_path: Absolute file path (e.g. `/src/app/main.py`).
        normalized_path: Normalized search root from `_normalize_path`.

    Returns:
        POSIX path relative to the search root (e.g. `src/app/main.py`). When
            `file_path` equals the search root (an exact-file search), returns
            just the basename.
    """
    if normalized_path == "/":
        return file_path[1:]
    if file_path == normalized_path:
        return file_path.rsplit("/", maxsplit=1)[-1]
    return file_path[len(normalized_path) + 1 :]


# ---------------------------------------------------------------------------
# In-memory glob / grep
# ---------------------------------------------------------------------------


def _glob_search_files(
    files: dict[str, Any],
    pattern: str,
    path: str | None = "/",
) -> str:
    r"""Search files dict for paths matching glob pattern.

    Args:
        files: Dictionary of file paths to FileData.
        pattern: Glob pattern (e.g., "*.py", "**/*.ts").
        path: Base path to search from. `None` defaults to root.

    Returns:
        Newline-separated file paths, sorted by modification time (most recent first).
        Returns "No files found" if no matches.

    Example:
        ```python
        files = {"/src/main.py": FileData(...), "/test.py": FileData(...)}
        _glob_search_files(files, "*.py", "/")
        # Returns: "/test.py\n/src/main.py" (sorted by modified_at)
        ```
    """
    try:
        normalized_path = _normalize_path(path)
    except ValueError:
        return "No files found"

    filtered = _filter_files_by_path(files, normalized_path)

    # Respect standard glob semantics:
    # - Patterns without path separators (e.g., "*.py") match only in the current
    #   directory (non-recursive) relative to `path`.
    # - Use "**" explicitly for recursive matching.
    # Strip leading "/" from pattern since matching is done against relative paths.
    effective_pattern = pattern.lstrip("/")

    matches = []
    for file_path, file_data in filtered.items():
        relative = _relative_to_root(file_path, normalized_path)

        if wcglob.globmatch(relative, effective_pattern, flags=wcglob.BRACE | wcglob.GLOBSTAR):
            # `modified_at` is optional on FileData; backends that never stamp it
            # (and hand-built fixtures) must not blow up the sort.
            matches.append((file_path, file_data.get("modified_at") or ""))

    matches.sort(key=lambda x: x[1], reverse=True)

    if not matches:
        return "No files found"

    return "\n".join(fp for fp, _ in matches)


def _format_grep_results(
    results: dict[str, list[tuple[int, str]]],
    output_mode: Literal["files_with_matches", "content", "count"],
) -> str:
    """Format grep search results based on output mode.

    Args:
        results: Dictionary mapping file paths to list of (line_num, line_content) tuples
        output_mode: Output format - "files_with_matches", "content", or "count"

    Returns:
        Formatted string output
    """
    if output_mode == "files_with_matches":
        return "\n".join(sorted(results.keys()))
    if output_mode == "count":
        lines = []
        for file_path in sorted(results.keys()):
            count = len(results[file_path])
            lines.append(f"{file_path}: {count}")
        return "\n".join(lines)
    lines = []
    for file_path in sorted(results.keys()):
        lines.append(f"{file_path}:")
        for line_num, line in results[file_path]:
            lines.append(f"  {line_num}: {line}")
    return "\n".join(lines)


def _grep_search_files(
    files: dict[str, Any],
    pattern: str,
    path: str | None = None,
    glob: str | None = None,
    output_mode: Literal["files_with_matches", "content", "count"] = "files_with_matches",
) -> str:
    r"""Search file contents for regex pattern.

    Args:
        files: Dictionary of file paths to FileData.
        pattern: Regex pattern to search for.
        path: Base path to search from.
        glob: Optional glob pattern to filter files (e.g., "*.py").
        output_mode: Output format - "files_with_matches", "content", or "count".

    Returns:
        Formatted search results. Returns "No matches found" if no results.

    Example:
        ```python
        files = {"/file.py": FileData(content="import os\\nprint('hi')", ...)}
        _grep_search_files(files, "import", "/")
        # Returns: "/file.py" (with output_mode="files_with_matches")
        ```
    """
    try:
        regex = re.compile(pattern)
    except re.error as e:
        return f"Invalid regex pattern: {e}"

    try:
        normalized_path = _normalize_path(path)
    except ValueError:
        return "No matches found"

    filtered = _filter_files_by_path(files, normalized_path)

    if glob:
        matcher = compile_grep_include_glob(glob)
        filtered = {fp: fd for fp, fd in filtered.items() if matcher(_relative_to_root(fp, normalized_path))}

    results: dict[str, list[tuple[int, str]]] = {}
    for file_path, file_data in filtered.items():
        # Split explicitly: v2 stores `content` as a `str`, so iterating the raw
        # value would walk one character at a time.
        for line_num, line in enumerate(_normalize_content(file_data).split("\n"), 1):
            if regex.search(line):
                if file_path not in results:
                    results[file_path] = []
                results[file_path].append((line_num, line))

    if not results:
        return "No matches found"
    return _format_grep_results(results, output_mode)


# -------- Structured helpers for composition --------


def grep_matches_from_files(
    files: dict[str, Any],
    pattern: str,
    path: str | None = None,
    glob: str | None = None,
) -> list[GrepMatch] | str:
    """Return structured grep matches from an in-memory files mapping.

    Performs literal text search (not regex).

    Returns a list of GrepMatch on success, or a string for invalid inputs.
    We deliberately do not raise here to keep backends non-throwing in tool
    contexts and preserve user-facing error messages.

    Args:
        files: Dictionary of file paths to FileData.
        pattern: Literal substring to search for.
        path: Base path to search from. `None` defaults to root.
        glob: Optional include-glob filtering which files are searched.

    Returns:
        List of `GrepMatch` dicts, or an error string.
    """
    try:
        normalized_path = _normalize_path(path)
    except ValueError:
        return []

    filtered = _filter_files_by_path(files, normalized_path)

    if glob:
        matcher = compile_grep_include_glob(glob)
        filtered = {fp: fd for fp, fd in filtered.items() if matcher(_relative_to_root(fp, normalized_path))}

    matches: list[GrepMatch] = []
    for file_path, file_data in filtered.items():
        # Split explicitly: v2 stores `content` as a `str`, so iterating the raw
        # value would walk one character at a time.
        for line_num, line in enumerate(_normalize_content(file_data).split("\n"), 1):
            if pattern in line:  # Simple substring search for literal matching
                matches.append({"path": file_path, "line": int(line_num), "text": line})
    return matches


def build_grep_results_dict(matches: list[GrepMatch]) -> dict[str, list[tuple[int, str]]]:
    """Group structured matches into the legacy dict form used by formatters."""
    grouped: dict[str, list[tuple[int, str]]] = {}
    for m in matches:
        grouped.setdefault(m["path"], []).append((m["line"], m["text"]))
    return grouped


def format_grep_matches(
    matches: list[GrepMatch],
    output_mode: Literal["files_with_matches", "content", "count"],
) -> str:
    """Format structured grep matches using existing formatting logic."""
    if not matches:
        return "No matches found"
    return _format_grep_results(build_grep_results_dict(matches), output_mode)


# ---------------------------------------------------------------------------
# Regex-vs-literal hinting
# ---------------------------------------------------------------------------


_REGEX_SIGNAL_RE = re.compile(
    r"\|"  # alternation
    r"|\.\*"  # `.*` wildcard
    r"|\.\+"  # `.+` wildcard
    r"|\\[.wWdDsSbB(){}\[\]|+*?^$]"  # escaped regex metacharacters / classes
)
"""Strong signals that a pattern was written as a regex rather than literal text.

Deliberately conservative: bare `.`, `(`, `)`, `[`, `]`, `?`, `^`, `$` are
omitted because they appear routinely in literal code searches (e.g.
`self.tools`, `def __init__(self):`, `arr[0]`), which would cause false hints.
"""


def _looks_like_regex(pattern: str) -> bool:
    """Heuristically detect regex syntax in a pattern meant for literal grep.

    Args:
        pattern: The pattern to inspect.

    Returns:
        True when the pattern carries a strong regex signal.
    """
    return bool(_REGEX_SIGNAL_RE.search(pattern))


def regex_literal_hint(pattern: str) -> str | None:
    """Return a hint when a pattern looks like an (unsupported) regex.

    `grep` matches literal text, so regex metacharacters are searched verbatim
    and silently miss. Callers gate this on a no-match result; the function
    itself only inspects the pattern.

    Args:
        pattern: The literal grep pattern to inspect for regex signals.

    Returns:
        A one-line hint steering the caller toward literal search, or `None`
            when the pattern has no regex signals.
    """
    if not _looks_like_regex(pattern):
        return None
    return (
        "Note: grep matches literal text, not regex, so characters like "
        "`|`, `.*`, and `\\.` are searched verbatim. Search for the literal "
        "text you need instead; for `|` alternation, run a separate search "
        "per alternative."
    )
