"""Base sandbox implementation.

`BaseSandbox` implements `SandboxBackendProtocol`.

File listing, grep, glob, and read use shell commands via `execute()`. Write
delegates content transfer to `upload_files()`. Edit uses a server-side
`execute()` script for payloads under `_EDIT_INLINE_MAX_BYTES` and falls back to
uploading old/new strings as temp files with a server-side replace script for
larger ones (ARG_MAX / request-body safety).

Concrete subclasses implement `execute()`, `upload_files()`, `download_files()`,
and the `id` property; every other operation is derived from those.

Command construction and output parsing are split into `_build_*_cmd` /
`_parse_*_output` free functions so the sync and async methods share one
definition of each command — the async overrides are native (`aexecute`), not
thread-pool wrappers.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shlex
from abc import ABC, abstractmethod
from typing import Any, Final

import anyio

from bog_agents.backends.protocol import (
    ASYNC_GREP_TIMEOUT,
    DeleteResult,
    EditResult,
    ExecuteOffloadResult,
    ExecuteResponse,
    FileData,
    FileDownloadResponse,
    FileInfo,
    FileUploadResponse,
    GlobResult,
    GrepMatch,
    GrepResult,
    LsResult,
    ReadResult,
    SandboxBackendProtocol,
    WriteResult,
    execute_accepts_timeout,
)
from bog_agents.backends.utils import MAX_BINARY_BYTES, _get_backend_read_file_type

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_BINARY_BYTES",
    "MAX_OUTPUT_BYTES",
    "TRUNCATION_MSG",
    "BaseSandbox",
]

MAX_OUTPUT_BYTES: Final = 500 * 1024
"""Maximum size of rendered text content returned by `read_file`.

Pages exceeding this cap are truncated and `TRUNCATION_MSG` is appended. Mirrors
the `MAX_OUTPUT_BYTES` literal in `_READ_COMMAND_TEMPLATE` (asserted by
`test_read_constants_match_template`).
"""

TRUNCATION_MSG: Final = (
    "\n\n[Output was truncated due to size limits. "
    "This paginated read result exceeded the sandbox stdout limit. "
    "Continue reading with a larger offset or smaller limit to inspect the rest of the file.]"
)
"""Sentinel appended to `read_file` content when `MAX_OUTPUT_BYTES` is hit."""

_EDIT_INLINE_MAX_BYTES: Final = 50_000
"""Maximum combined byte size of `old_string` + `new_string` for an inline edit.

Payloads above this use `_edit_via_upload` (temp-file upload + server-side
replace) to avoid the size limits some sandbox providers impose on the
`execute()` request body.
"""

_GLOB_COMMAND_TEMPLATE = """python3 -c "
import glob
import os
import json
import base64

# Decode base64-encoded parameters
path = base64.b64decode('{path_b64}').decode('utf-8')
pattern = base64.b64decode('{pattern_b64}').decode('utf-8')

try:
    real_root = os.path.realpath(path)
    os.chdir(path)
    rel_pattern = pattern.lstrip('/')
    if any(seg == '..' for seg in rel_pattern.replace(chr(92), '/').split('/')):
        print(json.dumps({{'error': 'invalid_pattern'}}))
    else:
        matches = sorted(glob.glob(rel_pattern, recursive=True))
        for m in matches:
            candidate = os.path.realpath(m)
            if candidate != real_root and not candidate.startswith(real_root + os.sep):
                continue
            try:
                st = os.stat(candidate)
            except OSError:
                continue
            print(json.dumps({{
                'path': m,
                'size': st.st_size,
                'mtime': st.st_mtime,
                'is_dir': os.path.isdir(candidate),
            }}))
except FileNotFoundError:
    print(json.dumps({{'error': 'path_not_found'}}))
except NotADirectoryError:
    print(json.dumps({{'error': 'not_a_directory'}}))
except PermissionError:
    print(json.dumps({{'error': 'permission_denied'}}))
" 2>&1"""
"""Find files matching a pattern, with metadata.

Parameters are base64-encoded to avoid shell escaping issues. Matches that
resolve (via symlink) outside the search root are dropped, and a `..` in the
pattern is rejected outright.
"""

_GREP_PATH_GLOB_TEMPLATE = """python3 -c "
import glob, os, base64, sys

search_path = base64.b64decode('{path_b64}').decode('utf-8')
glob_pat = base64.b64decode('{glob_b64}').decode('utf-8')
pattern = base64.b64decode('{pattern_b64}').decode('utf-8')

# When the search path is a directory, chdir to it so glob patterns resolve
# relative to it. When it is a single file, search it directly (glob filtering
# is irrelevant for a single-file search).
if os.path.isdir(search_path):
    os.chdir(search_path)
    # A leading `/` would make `glob.glob` treat the pattern as an absolute
    # filesystem path, searching outside the search root. Strip it so anchored
    # globs stay relative to the search root, matching FilesystemBackend
    # semantics where `/` anchors to the root, not the filesystem.
    rel_glob = glob_pat.lstrip('/')
    if any(seg == '..' for seg in rel_glob.replace(chr(92), '/').split('/')):
        sys.stderr.write('glob contains path traversal\\n')
        sys.exit(2)
    real_root = os.path.realpath(search_path)
    rel_files = sorted(glob.glob(rel_glob, recursive=True))
    targets = []
    for rel in rel_files:
        real_open = os.path.realpath(rel)
        if real_open != real_root and not real_open.startswith(real_root + os.sep):
            continue
        display_path = os.path.join(search_path, os.path.relpath(real_open, real_root))
        targets.append((real_open, display_path))
else:
    targets = [(search_path, search_path)]

for open_path, display_path in targets:
    try:
        with open(open_path, 'r', encoding='utf-8', errors='ignore') as fh:
            for i, line in enumerate(fh, 1):
                if pattern in line:
                    # GNU grep -HnFZ always terminates each record with a
                    # newline, even when the matched line has none. Strip the
                    # line's own trailing newline and add an explicit one so
                    # records never concatenate when a file's last line lacks a
                    # final newline.
                    sys.stdout.write(display_path + chr(0) + str(i) + ':' + line.rstrip(chr(10)) + chr(10))
    except OSError:
        pass
" 2>/dev/null"""
r"""Search file contents for a literal string, filtered by a path-relative glob.

Used when the glob contains a `/` (e.g. `src/**/*.py`), because GNU
`grep --include` only matches basenames and would silently return zero results
for such patterns. All three parameters are base64-encoded, so a hostile glob
cannot break out of the shell quoting.

Emits the same `path\0line_num:text` records that `grep -HnFZ` produces, so
`_parse_grep_output` consumes it unchanged. `|| true` is deliberately omitted:
the script exits 0 on a legitimate no-match, so a non-zero exit signals a real
failure that `_parse_grep_output` surfaces as an error rather than an empty
result.
"""

_WRITE_CHECK_TEMPLATE = """python3 -c "
import os, base64

path = base64.b64decode('{path_b64}').decode('utf-8')
os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
" 2>&1"""
"""Preflight for `write`: create the target's parent directories.

Only the (small) base64-encoded path is interpolated — file content is
transferred separately via `upload_files()`, so this command never approaches
ARG_MAX regardless of file size.
"""

_EDIT_COMMAND_TEMPLATE = """python3 -c "
import sys, os, stat as _stat, base64, json

payload = json.loads(base64.b64decode(sys.stdin.read().strip()).decode('utf-8'))
path, old, new = payload['path'], payload['old'], payload['new']
replace_all = payload.get('replace_all', False)

try:
    st = os.stat(path)
    if not _stat.S_ISREG(st.st_mode):
        print(json.dumps({{'error': 'not_a_file'}}))
        sys.exit(0)

    with open(path, 'rb') as f:
        raw = f.read()

    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        print(json.dumps({{'error': 'not_a_text_file'}}))
        sys.exit(0)

    # Match-driven CRLF handling: the read template normalizes CRLF to LF for
    # the model, so old_string arrives LF-only even when the file on disk is
    # CRLF. Try old as sent, then a CRLF variant, then an LF variant. The first
    # match reveals the file's line-ending style in that region; apply the same
    # transform to new so the file's style is preserved.
    old_crlf = old.replace('\\r\\n', '\\n').replace('\\n', '\\r\\n')
    old_lf = old.replace('\\r\\n', '\\n')
    new_crlf = new.replace('\\r\\n', '\\n').replace('\\n', '\\r\\n')
    new_lf = new.replace('\\r\\n', '\\n')
    count = 0
    matched_old, matched_new = old, new
    for cand_old, cand_new in ((old, new), (old_crlf, new_crlf), (old_lf, new_lf)):
        c = text.count(cand_old)
        if c >= 1:
            matched_old, matched_new, count = cand_old, cand_new, c
            break

    if count == 0:
        print(json.dumps({{'error': 'string_not_found'}}))
        sys.exit(0)
    if count > 1 and not replace_all:
        print(json.dumps({{'error': 'multiple_occurrences', 'count': count}}))
        sys.exit(0)

    result = text.replace(matched_old, matched_new) if replace_all else text.replace(matched_old, matched_new, 1)
    with open(path, 'wb') as f:
        f.write(result.encode('utf-8'))

    print(json.dumps({{'count': count}}))
except FileNotFoundError:
    print(json.dumps({{'error': 'file_not_found'}}))
except PermissionError:
    print(json.dumps({{'error': 'permission_denied'}}))
" 2>&1 <<'__BOG_AGENTS_EDIT_EOF__'
{payload_b64}
__BOG_AGENTS_EDIT_EOF__
"""
"""Server-side file edit via `execute()`.

Reads the file, performs the replacement, and writes back — all on the sandbox.
The payload (path, old/new strings, `replace_all`) is base64-encoded JSON fed
through a heredoc on stdin to avoid shell escaping issues.

Output: single-line JSON, `{{"count": N}}` on success or `{{"error": ...}}` on
failure.

The trailing newline after `__BOG_AGENTS_EDIT_EOF__` is load-bearing: some
integrations detect end-of-input on a newline-delimited heredoc feed.
"""

_EDIT_TMPFILE_TEMPLATE = """python3 -c "
import os, stat as _stat, sys, json, base64

old_path = base64.b64decode('{old_path_b64}').decode('utf-8')
new_path = base64.b64decode('{new_path_b64}').decode('utf-8')
target = base64.b64decode('{target_b64}').decode('utf-8')
replace_all = {replace_all}

try:
    old = open(old_path, 'rb').read().decode('utf-8')
    new = open(new_path, 'rb').read().decode('utf-8')
except Exception as e:
    print(json.dumps({{'error': 'temp_read_failed', 'detail': str(e)}}))
    sys.exit(0)
finally:
    for p in (old_path, new_path):
        try: os.remove(p)
        except OSError: pass

try:
    st = os.stat(target)
    if not _stat.S_ISREG(st.st_mode):
        print(json.dumps({{'error': 'not_a_file'}}))
        sys.exit(0)

    with open(target, 'rb') as f:
        raw = f.read()

    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        print(json.dumps({{'error': 'not_a_text_file'}}))
        sys.exit(0)

    # Match-driven CRLF handling -- see _EDIT_COMMAND_TEMPLATE.
    old_crlf = old.replace('\\r\\n', '\\n').replace('\\n', '\\r\\n')
    old_lf = old.replace('\\r\\n', '\\n')
    new_crlf = new.replace('\\r\\n', '\\n').replace('\\n', '\\r\\n')
    new_lf = new.replace('\\r\\n', '\\n')
    count = 0
    matched_old, matched_new = old, new
    for cand_old, cand_new in ((old, new), (old_crlf, new_crlf), (old_lf, new_lf)):
        c = text.count(cand_old)
        if c >= 1:
            matched_old, matched_new, count = cand_old, cand_new, c
            break

    if count == 0:
        print(json.dumps({{'error': 'string_not_found'}}))
        sys.exit(0)
    if count > 1 and not replace_all:
        print(json.dumps({{'error': 'multiple_occurrences', 'count': count}}))
        sys.exit(0)

    result = text.replace(matched_old, matched_new) if replace_all else text.replace(matched_old, matched_new, 1)
    with open(target, 'wb') as f:
        f.write(result.encode('utf-8'))

    print(json.dumps({{'count': count}}))
except FileNotFoundError:
    print(json.dumps({{'error': 'file_not_found'}}))
except PermissionError:
    print(json.dumps({{'error': 'permission_denied'}}))
" 2>&1"""
"""Server-side file edit via temp-file upload, for large payloads.

Old/new strings are uploaded via `upload_files()`, then this script reads them,
performs the replacement on the source file (which never leaves the sandbox),
and removes the temp files. Same success contract as `_EDIT_COMMAND_TEMPLATE`;
additionally emits `{{"error": "temp_read_failed", "detail": ...}}` when the
uploaded temp files cannot be read.
"""

_READ_COMMAND_TEMPLATE = """python3 -c "
import codecs, os, stat as _stat, sys, base64, json

MAX_OUTPUT_BYTES = 500 * 1024
MAX_BINARY_BYTES = 500 * 1024
TRUNCATION_MSG = '\\n\\n' + (
    '[Output was truncated due to size limits. '
    'This paginated read result exceeded the sandbox stdout limit. '
    'Continue reading with a larger offset or smaller limit to inspect the rest of the file.]'
)

path = base64.b64decode('{path_b64}').decode('utf-8')

try:
    st = os.stat(path)
    if not _stat.S_ISREG(st.st_mode):
        print(json.dumps({{'error': 'not_a_file'}}))
        sys.exit(0)

    if st.st_size == 0:
        print(json.dumps({{'encoding': 'utf-8', 'content': ''}}))
        sys.exit(0)

    file_type = '{file_type}'
    if file_type != 'text':
        if st.st_size > MAX_BINARY_BYTES:
            print(json.dumps({{'error': 'Binary file exceeds maximum preview size of ' + str(MAX_BINARY_BYTES) + ' bytes'}}))
            sys.exit(0)
        with open(path, 'rb') as f:
            raw = f.read()
        print(json.dumps({{'encoding': 'base64', 'content': base64.b64encode(raw).decode('ascii')}}))
        sys.exit(0)

    with open(path, 'rb') as f:
        raw_prefix = f.read(8192)

    # The 8192-byte prefix can slice a multi-byte UTF-8 char (CJK is 3 bytes,
    # emoji 4); the incremental decoder buffers a trailing partial sequence
    # instead of raising, so legitimate text isn't misclassified as binary.
    is_binary = False
    try:
        codecs.getincrementaldecoder('utf-8')().decode(raw_prefix, final=False)
    except UnicodeDecodeError:
        is_binary = True

    if is_binary:
        if st.st_size > MAX_BINARY_BYTES:
            print(json.dumps({{'error': 'Binary file exceeds maximum preview size of ' + str(MAX_BINARY_BYTES) + ' bytes'}}))
            sys.exit(0)
        with open(path, 'rb') as f:
            raw = f.read()
        print(json.dumps({{'encoding': 'base64', 'content': base64.b64encode(raw).decode('ascii')}}))
        sys.exit(0)

    offset = {offset}
    limit = {limit}
    line_count = 0
    returned_lines = 0
    truncated = False
    parts = []
    current_bytes = 0
    msg_bytes = len(TRUNCATION_MSG.encode('utf-8'))
    effective_limit = MAX_OUTPUT_BYTES - msg_bytes

    with open(path, 'r', encoding='utf-8', newline=None) as f:
        for raw_line in f:
            line_count += 1
            if line_count <= offset:
                continue
            if returned_lines >= limit:
                break

            line = raw_line.rstrip('\\n').rstrip('\\r')
            piece = line if returned_lines == 0 else '\\n' + line
            piece_bytes = len(piece.encode('utf-8'))
            if current_bytes + piece_bytes > effective_limit:
                truncated = True
                remaining_bytes = effective_limit - current_bytes
                if remaining_bytes > 0:
                    prefix = piece.encode('utf-8')[:remaining_bytes].decode('utf-8', errors='ignore')
                    if prefix:
                        parts.append(prefix)
                        current_bytes += len(prefix.encode('utf-8'))
                break

            parts.append(piece)
            current_bytes += piece_bytes
            returned_lines += 1

    if returned_lines == 0 and not truncated:
        print(json.dumps({{'error': 'Line offset ' + str(offset) + ' exceeds file length (' + str(line_count) + ' lines)'}}))
        sys.exit(0)

    text = ''.join(parts)
    if truncated:
        text += TRUNCATION_MSG

    print(json.dumps({{'encoding': 'utf-8', 'content': text}}))
except FileNotFoundError:
    print(json.dumps({{'error': 'file_not_found'}}))
except PermissionError:
    print(json.dumps({{'error': 'permission_denied'}}))
" 2>&1"""
r"""Read file content with server-side pagination.

Only the requested page crosses the wire, so a paginated read of a huge file
never transfers the whole file. The path is base64-encoded; `file_type`,
`offset`, and `limit` are interpolated directly (they originate in this module,
not in user input).

Text is read with universal newlines, so CRLF and bare CR both collapse to LF —
`edit()` compensates on the way back.

Output: single-line JSON, either `{{"encoding": ..., "content": ...}}` on success
or `{{"error": ...}}` on failure. An empty file yields empty `content`, which the
protocol's `read` shim renders as the standard empty-file reminder.
"""

_EXECUTE_CAPTURE_SENTINEL: Final = "__BOG_AGENTS_EXEC_META__"
"""First-line marker of capture-wrapper output: `<sentinel> <exit_code> <offloaded> <capped>`."""

_EXECUTE_CAPTURE_HEAD_LINES: Final = 5
_EXECUTE_CAPTURE_TAIL_LINES: Final = 5
_EXECUTE_CAPTURE_HEAD_BYTES: Final = 2000
_EXECUTE_CAPTURE_TAIL_BYTES: Final = 2000

_EXECUTE_CAPTURE_META_FIELDS: Final = 4
"""Number of space-separated fields on the capture wrapper's meta line."""

_EXECUTE_CAPTURE_MAX_BYTES: Final = 10 * 1024 * 1024
"""Hard cap on captured stdout/stderr persisted to the sandbox.

Bounds sandbox disk use for runaway output: the captured stream is piped through
`head -c`, so once the cap is hit nothing further reaches disk. Set well above
the inline budget so legitimately large output is still preserved in full;
output beyond the cap is truncated and flagged.
"""

# The captured stream is piped into `head -c` (caps the on-disk file) followed by
# `cat > /dev/null` (drains the rest), so the file can never exceed the cap yet the
# command still reaches EOF and exits normally -- closing the pipe early would
# SIGPIPE-kill it and corrupt its exit code. Because the command runs in a pipeline,
# its real exit code is recovered from a sidecar file rather than `$?` (which would
# be the pipeline's). The command runs in a subshell so a command `exit` cannot
# abort the wrapper, and `eval` preserves the backend's own shell/env. The command
# is embedded via a quoted heredoc with a random delimiter to avoid shell-quoting
# issues; the (internal, sanitized) capture path is shell-quoted.
_EXECUTE_CAPTURE_CMD_TEMPLATE = """# ===== bog-agents capture-at-source offload (auto-generated wrapper) =====
# Runs the requested command below, capturing its combined output to a file in
# the sandbox: returned inline when small, or as a head/tail preview when large
# (the full result stays at the path for read_file). Disable this wrapping with
# BaseSandbox.enable_capture_offload = False.
__bog_f=__PATH_Q__
__bog_ecf="$__bog_f.ec"
mkdir -p "$(dirname "$__bog_f")" 2>/dev/null
# ----- requested command (verbatim, between the heredoc markers) -----
__bog_cmd=$(cat <<'__DELIM__'
__COMMAND__
__DELIM__
)
# ----- end requested command; everything below is offload machinery -----
{ ( eval "$__bog_cmd" ); echo "$?" > "$__bog_ecf"; } 2>&1 | { head -c __MAXBYTES__ > "$__bog_f"; cat > /dev/null; }
__bog_ec=$(cat "$__bog_ecf" 2>/dev/null)
: "${__bog_ec:=1}"
rm -f "$__bog_ecf"
__bog_bytes=$(wc -c < "$__bog_f" 2>/dev/null | tr -d ' ')
: "${__bog_bytes:=0}"
__bog_capped=0
[ "$__bog_bytes" -ge __MAXBYTES__ ] && __bog_capped=1
if [ "$__bog_bytes" -le __BUDGET__ ]; then
  printf '%s %s %s %s\\n' '__SENTINEL__' "$__bog_ec" 0 0
  cat "$__bog_f"
  rm -f "$__bog_f"
else
  __bog_lines=$(wc -l < "$__bog_f" 2>/dev/null | tr -d ' ')
  : "${__bog_lines:=0}"
  __bog_omitted=$((__bog_lines - __HEADLINES__ - __TAILLINES__))
  printf '%s %s %s %s\\n' '__SENTINEL__' "$__bog_ec" 1 "$__bog_capped"
  if [ "$__bog_omitted" -gt 0 ]; then
    head -c __HEAD__ "$__bog_f" | head -n __HEADLINES__
    printf '... [%s lines truncated] ...\\n' "$__bog_omitted"
    tail -c __TAIL__ "$__bog_f" | tail -n __TAILLINES__
  else
    head -c $((__HEAD__ + __TAIL__)) "$__bog_f"
  fi
fi
"""
# Pure POSIX sh wrapper for capture-at-source `execute`; see the comment above.


# ---------------------------------------------------------------------------
# Command builders / output parsers (shared by the sync and async methods)
# ---------------------------------------------------------------------------


def _b64(value: str) -> str:
    """Base64-encode a UTF-8 string for safe interpolation into a shell command.

    Args:
        value: The string to encode.

    Returns:
        The ASCII base64 representation.
    """
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def _build_ls_cmd(path: str) -> str:
    """Build the `ls` command for `path`.

    Args:
        path: Absolute directory path to list.

    Returns:
        The shell command string.
    """
    path_b64 = _b64(path)
    return f"""python3 -c "
import os
import json
import base64

path = base64.b64decode('{path_b64}').decode('utf-8')

try:
    with os.scandir(path) as it:
        for entry in it:
            result = {{
                'path': os.path.join(path, entry.name),
                'is_dir': entry.is_dir(follow_symlinks=False)
            }}
            print(json.dumps(result))
except FileNotFoundError:
    print(json.dumps({{'error': 'path_not_found'}}))
except NotADirectoryError:
    print(json.dumps({{'error': 'not_a_directory'}}))
except PermissionError:
    print(json.dumps({{'error': 'permission_denied'}}))
" 2>/dev/null"""


def _parse_ls_output(output: str, path: str) -> LsResult:
    """Parse the JSON-lines output of `_build_ls_cmd`.

    Args:
        output: Raw command output.
        path: The listed path, used in the error message.

    Returns:
        `LsResult` with entries, or an error when the script reported one.
    """
    file_infos: list[FileInfo] = []
    error: str | None = None
    for line in output.strip().split("\n"):
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "error" in data:
            error = data["error"]
            continue
        file_infos.append({"path": data["path"], "is_dir": data["is_dir"]})
    if error is not None:
        return LsResult(error=f"Path '{path}': {error}")
    return LsResult(entries=file_infos)


def _build_read_cmd(file_path: str, offset: int, limit: int) -> str:
    """Build the paginated read command for `file_path`.

    Args:
        file_path: Absolute path to read.
        offset: Line offset (0-indexed).
        limit: Maximum number of lines.

    Returns:
        The shell command string.
    """
    file_type = _get_backend_read_file_type(file_path)
    # Defensive int coercion in case a caller bypasses type checking: these two
    # are interpolated into the script body unquoted.
    return _READ_COMMAND_TEMPLATE.format(
        path_b64=_b64(file_path),
        file_type=file_type,
        offset=int(offset),
        limit=int(limit),
    )


def _parse_read_output(output: str, file_path: str) -> ReadResult:
    """Parse the single-line JSON output of `_build_read_cmd`.

    Args:
        output: Raw command output.
        file_path: The path read, used in error messages.

    Returns:
        `ReadResult` carrying the sliced `FileData`, or an error.
    """
    output = output.rstrip()
    try:
        data = json.loads(output)
    except (json.JSONDecodeError, ValueError):
        detail = output[:200] if output else "(empty)"
        return ReadResult(error=f"File '{file_path}': unexpected server response: {detail}")
    if not isinstance(data, dict):
        detail = output[:200] if output else "(empty)"
        return ReadResult(error=f"File '{file_path}': unexpected server response: {detail}")
    if "error" in data:
        return ReadResult(error=f"File '{file_path}': {data['error']}")
    return ReadResult(
        file_data=FileData(
            content=data["content"],
            encoding=data.get("encoding", "utf-8"),
        )
    )


def _build_write_preflight_cmd(file_path: str) -> str:
    """Build the parent-directory-creation command for a write.

    Args:
        file_path: Absolute path about to be written.

    Returns:
        The shell command string.
    """
    return _WRITE_CHECK_TEMPLATE.format(path_b64=_b64(file_path))


def _check_preflight_result(result: ExecuteResponse, file_path: str) -> WriteResult | None:
    """Turn a preflight `ExecuteResponse` into a `WriteResult` error, or `None`.

    Args:
        result: Response from running the preflight command.
        file_path: Path the write targets, used in the error message.

    Returns:
        `None` when the preflight passed; a `WriteResult` with `error` set
            otherwise.
    """
    if result.exit_code != 0 or "Error:" in result.output:
        error_msg = result.output.strip() or f"Failed to write file '{file_path}'"
        return WriteResult(error=error_msg)
    return None


def _build_grep_cmd(pattern: str, path: str | None, glob: str | None) -> str:
    """Build the grep command for a literal-text search.

    Basename-only globs go through GNU `grep --include`; globs containing a `/`
    are routed to `_GREP_PATH_GLOB_TEMPLATE`, because `--include` only matches
    basenames and would silently return zero results for `src/**/*.py`.

    Args:
        pattern: Literal string to search for.
        path: Directory or file to search. Defaults to `"."`.
        glob: Optional include-glob.

    Returns:
        The shell command string.
    """
    if glob and "/" in glob:
        return _GREP_PATH_GLOB_TEMPLATE.format(
            path_b64=_b64(path or "."),
            glob_b64=_b64(glob),
            pattern_b64=_b64(pattern),
        )

    search_path = shlex.quote(path or ".")
    # `-Z` separates the filename from the line data with NUL, so filenames may
    # contain `:` without making the output ambiguous.
    grep_opts = "-rHnFZ"
    pattern_escaped = shlex.quote(pattern)
    glob_pattern = f"--include={shlex.quote(glob)}" if glob else ""
    return f"grep {grep_opts} {glob_pattern} -e {pattern_escaped} {search_path} 2>/dev/null || true"


def _parse_grep_output(result: ExecuteResponse, path: str | None) -> GrepResult:
    r"""Parse `path\0line:text` grep records into a `GrepResult`.

    Args:
        result: Response from running the grep command.
        path: The search root, used in error messages.

    Returns:
        `GrepResult` with matches, or an error on a non-zero exit.
    """
    output = result.output.rstrip("\n")
    if result.exit_code is not None and result.exit_code != 0:
        detail = output.strip() if output else f"exit code {result.exit_code}"
        return GrepResult(error=f"Path '{path or '.'}': {detail}")
    if not output:
        return GrepResult(matches=[])
    matches: list[GrepMatch] = []
    parse_error: str | None = None
    for line in output.split("\n"):
        try:
            file_path, rest = line.split("\0", 1)
            line_num_str, text = rest.split(":", 1)
            matches.append({"path": file_path, "line": int(line_num_str), "text": text})
        except ValueError:
            parse_error = line
    if parse_error is not None and not matches:
        return GrepResult(error=f"Path '{path or '.'}': {parse_error}")
    return GrepResult(matches=matches)


def _build_glob_cmd(pattern: str, search_path: str) -> str:
    """Build the glob command.

    Args:
        pattern: Glob pattern.
        search_path: Base directory to search from.

    Returns:
        The shell command string.
    """
    return _GLOB_COMMAND_TEMPLATE.format(path_b64=_b64(search_path), pattern_b64=_b64(pattern))


def _parse_glob_output(output: str, search_path: str) -> GlobResult:
    """Parse the JSON-lines output of `_build_glob_cmd`.

    Args:
        output: Raw command output.
        search_path: The search root, used in the error message.

    Returns:
        `GlobResult` with matches, or an error when the script reported one.
    """
    output = output.strip()
    if not output:
        return GlobResult(matches=[])
    file_infos: list[FileInfo] = []
    error: str | None = None
    for line in output.split("\n"):
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "error" in data:
            error = data["error"]
            continue
        file_infos.append({"path": data["path"], "is_dir": data["is_dir"]})
    if error is not None:
        return GlobResult(error=f"Path '{search_path}': {error}")
    return GlobResult(matches=file_infos)


def _build_edit_inline_cmd(file_path: str, old_string: str, new_string: str, *, replace_all: bool) -> str:
    """Build the inline (heredoc-payload) edit command.

    Args:
        file_path: Absolute path to edit.
        old_string: Exact substring to find.
        new_string: Replacement string.
        replace_all: Whether to replace every occurrence.

    Returns:
        The shell command string.
    """
    payload = json.dumps({"path": file_path, "old": old_string, "new": new_string, "replace_all": replace_all})
    return _EDIT_COMMAND_TEMPLATE.format(payload_b64=_b64(payload))


def _build_edit_tmpfile_cmd(file_path: str, old_tmp: str, new_tmp: str, *, replace_all: bool) -> str:
    """Build the temp-file edit command used for large payloads.

    Args:
        file_path: Absolute path to edit.
        old_tmp: Sandbox path of the uploaded `old_string` temp file.
        new_tmp: Sandbox path of the uploaded `new_string` temp file.
        replace_all: Whether to replace every occurrence.

    Returns:
        The shell command string.
    """
    return _EDIT_TMPFILE_TEMPLATE.format(
        old_path_b64=_b64(old_tmp),
        new_path_b64=_b64(new_tmp),
        target_b64=_b64(file_path),
        replace_all=replace_all,
    )


def _map_edit_error(error: str, file_path: str, old_string: str) -> EditResult:
    """Map a server-side edit error code to an `EditResult`.

    Args:
        error: Error code emitted by the edit script.
        file_path: Path that was edited.
        old_string: The search string, echoed in the message.

    Returns:
        `EditResult` with a user-facing `error`.
    """
    messages: dict[str, str] = {
        "file_not_found": f"Error: File '{file_path}' not found",
        "permission_denied": f"Error: Permission denied editing file '{file_path}'",
        "not_a_file": f"Error: '{file_path}' is not a regular file",
        "not_a_text_file": f"Error: File '{file_path}' is not a text file",
        "string_not_found": f"Error: String not found in file: '{old_string}'",
        "multiple_occurrences": f"Error: String '{old_string}' appears multiple times. Use replace_all=True to replace all occurrences.",
    }
    return EditResult(error=messages.get(error, f"Error editing file '{file_path}': {error}"))


def _parse_edit_output(output: str, file_path: str, old_string: str) -> EditResult:
    """Parse the single-line JSON output of an edit command.

    Args:
        output: Raw command output.
        file_path: Path that was edited.
        old_string: The search string, echoed in error messages.

    Returns:
        `EditResult` with `occurrences` on success, or an error.
    """
    output = output.rstrip()
    try:
        data = json.loads(output)
    except (json.JSONDecodeError, ValueError):
        detail = output[:200] if output else "(empty)"
        return EditResult(error=f"Error editing file '{file_path}': unexpected server response: {detail}")
    if not isinstance(data, dict):
        detail = output[:200] if output else "(empty)"
        return EditResult(error=f"Error editing file '{file_path}': unexpected server response: {detail}")
    if "error" in data:
        return _map_edit_error(data["error"], file_path, old_string)
    # External storage: nothing to thread back into LangGraph state.
    return EditResult(path=file_path, files_update=None, occurrences=data.get("count", 1))


def _edit_tmp_paths() -> tuple[str, str]:
    """Return a unique `(old_tmp, new_tmp)` pair of sandbox temp paths.

    Returns:
        Two `/tmp` paths sharing an 80-bit random uid, so concurrent edits
            cannot collide.
    """
    uid = base64.b32encode(os.urandom(10)).decode("ascii").lower()
    return (
        f"/tmp/.bog_agents_edit_{uid}_old",
        f"/tmp/.bog_agents_edit_{uid}_new",
    )


def _new_heredoc_delim() -> str:
    """Return a random heredoc delimiter, e.g. `__BOG_AGENTS_CMD_<80 random bits>__`.

    Returns:
        The delimiter string.
    """
    return "__BOG_AGENTS_CMD_" + base64.b32encode(os.urandom(10)).decode("ascii").rstrip("=") + "__"


def _build_capture_execute_cmd(
    command: str,
    capture_path: str,
    *,
    inline_budget: int,
    max_capture_bytes: int | None = None,
) -> str:
    """Build the capture-at-source wrapper command for `execute`.

    Args:
        command: The command to run, embedded verbatim in a quoted heredoc.
        capture_path: Sandbox path the combined output is captured to.
        inline_budget: Byte threshold at or below which output is returned
            inline. Above it, the output is left at `capture_path` and only a
            head/tail preview is returned.
        max_capture_bytes: Hard cap on bytes persisted to the sandbox. Defaults
            to `_EXECUTE_CAPTURE_MAX_BYTES` (resolved here so it stays
            patchable).

    Returns:
        The wrapper shell command string.
    """
    cap = max_capture_bytes if max_capture_bytes is not None else _EXECUTE_CAPTURE_MAX_BYTES
    # The command is embedded in a quoted heredoc; guarantee the delimiter cannot
    # appear inside it, or the command could terminate the heredoc early. The
    # delimiter carries 80 random bits, so this regenerates only astronomically
    # rarely.
    delim = _new_heredoc_delim()
    while delim in command:
        delim = _new_heredoc_delim()
    # __COMMAND__ is substituted last so command content can never collide with a
    # remaining placeholder token.
    return (
        _EXECUTE_CAPTURE_CMD_TEMPLATE.replace("__PATH_Q__", shlex.quote(capture_path))
        .replace("__DELIM__", delim)
        .replace("__MAXBYTES__", str(cap))
        .replace("__BUDGET__", str(inline_budget))
        .replace("__SENTINEL__", _EXECUTE_CAPTURE_SENTINEL)
        .replace("__HEADLINES__", str(_EXECUTE_CAPTURE_HEAD_LINES))
        .replace("__TAILLINES__", str(_EXECUTE_CAPTURE_TAIL_LINES))
        .replace("__HEAD__", str(_EXECUTE_CAPTURE_HEAD_BYTES))
        .replace("__TAIL__", str(_EXECUTE_CAPTURE_TAIL_BYTES))
        .replace("__COMMAND__", command)
    )


def _parse_capture_execute_output(output: str, *, backend_truncated: bool = False) -> ExecuteOffloadResult:
    r"""Parse capture-wrapper stdout into an `ExecuteOffloadResult`.

    The wrapper emits a meta line followed by the body:

        <sentinel> <exit_code> <offloaded> <capped>\n<inline output or preview>

    Falls back to `offloaded=False` with the raw output when the meta line is
    absent or malformed (e.g. the backend truncated transport) — the caller must
    not re-run the command in that case.

    Args:
        output: Raw stdout of the wrapper command.
        backend_truncated: Whether the underlying `execute` reported truncation.

    Returns:
        `ExecuteOffloadResult` describing where the output ended up.
    """
    first, _, body = output.partition("\n")
    parts = first.split(" ")
    if len(parts) != _EXECUTE_CAPTURE_META_FIELDS or parts[0] != _EXECUTE_CAPTURE_SENTINEL:
        return ExecuteOffloadResult(offloaded=False, response=ExecuteResponse(output=output, truncated=backend_truncated))
    try:
        exit_code = int(parts[1])
    except ValueError:
        return ExecuteOffloadResult(offloaded=False, response=ExecuteResponse(output=output, truncated=backend_truncated))
    return ExecuteOffloadResult(
        offloaded=parts[2] == "1",
        response=ExecuteResponse(output=body, exit_code=exit_code, truncated=parts[3] == "1" or backend_truncated),
    )


class BaseSandbox(SandboxBackendProtocol, ABC):
    """Base sandbox implementation with `execute()` as the core abstract method.

    Provides default implementations for every protocol method. Listing, grep,
    glob, and read run shell commands via `execute()`; read is paginated
    server-side so only the requested page crosses the wire. Write delegates
    content transfer to `upload_files()`. Edit runs a server-side script for
    small payloads and uploads old/new strings as temp files for large ones.

    !!! note

        `BaseSandbox` does not reduce or partition the trust boundary of
        `execute()`. Its helpers are convenience wrappers over the
        subclass-provided command-execution primitive, and assume a caller who
        can use `BaseSandbox` already has whatever shell-execution capability
        that backend exposes.

    Subclasses must implement `execute()`, `upload_files()`, `download_files()`,
    and the `id` property.
    """

    enable_capture_offload: bool = False
    """Whether `execute_with_offload` may use capture-at-source offload.

    When `True`, large `execute` output is captured to a file in the sandbox and
    only a preview is returned, so a runaway command cannot blow out the context
    window. Defaults to `False` (opt-in) because the capture wrapper's shell and
    coreutils assumptions are not guaranteed on every sandbox image; subclasses
    known to be compatible set it to `True`. When `False`, `execute_with_offload`
    runs the command unwrapped and returns the full output with
    `offloaded=False`, so callers fall back to their own handling.
    """

    @abstractmethod
    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        """Execute a command in the sandbox and return `ExecuteResponse`.

        Args:
            command: Full shell command string to execute.
            timeout: Maximum time in seconds to wait for the command to complete.

                If `None`, uses the backend's default timeout.

        Returns:
            `ExecuteResponse` with combined output, exit code, and truncation flag.
        """

    # -- execute with offload -------------------------------------------------

    def execute_with_offload(
        self,
        command: str,
        capture_path: str,
        *,
        max_inline_bytes: int,
        max_capture_bytes: int | None = None,
        timeout: int | None = None,
    ) -> ExecuteOffloadResult:
        """Run `command`, offloading large output to a file in the sandbox.

        Captures the command's combined output: returned inline when it is at or
        below `max_inline_bytes`, otherwise left at `capture_path` (so the caller
        can hand the model a `read_file` pointer instead of the whole blob) with
        only a head/tail preview returned. Captured output is hard-capped at
        `max_capture_bytes` without killing the command, so its exit code
        survives.

        When `enable_capture_offload` is `False`, the command runs unwrapped and
        the full output is returned with `offloaded=False`.

        Args:
            command: Full shell command string to execute.
            capture_path: Sandbox path to capture the combined output to.
            max_inline_bytes: Byte budget for returning output inline.
            max_capture_bytes: Hard cap on captured bytes. Defaults to
                `_EXECUTE_CAPTURE_MAX_BYTES`.
            timeout: Maximum time in seconds to wait for the command.

        Returns:
            `ExecuteOffloadResult`. `offloaded=True` means the result was left at
                `capture_path` and `response.output` holds only the preview;
                `offloaded=False` means `response.output` is the complete output.
        """
        use_timeout = timeout is not None and execute_accepts_timeout(type(self))
        if not self.enable_capture_offload:
            result = self.execute(command, timeout=timeout) if use_timeout else self.execute(command)
            return ExecuteOffloadResult(offloaded=False, response=result)
        wrapper = _build_capture_execute_cmd(command, capture_path, inline_budget=max_inline_bytes, max_capture_bytes=max_capture_bytes)
        result = self.execute(wrapper, timeout=timeout) if use_timeout else self.execute(wrapper)
        return _parse_capture_execute_output(result.output, backend_truncated=result.truncated)

    async def aexecute_with_offload(
        self,
        command: str,
        capture_path: str,
        *,
        max_inline_bytes: int,
        max_capture_bytes: int | None = None,
        # ASYNC109 - forwarded to the backend's own execute, not an asyncio.timeout() contract.
        timeout: int | None = None,  # noqa: ASYNC109
    ) -> ExecuteOffloadResult:
        """Async version of `execute_with_offload`, delegating to `aexecute`.

        Args:
            command: Full shell command string to execute.
            capture_path: Sandbox path to capture the combined output to.
            max_inline_bytes: Byte budget for returning output inline.
            max_capture_bytes: Hard cap on captured bytes.
            timeout: Maximum time in seconds to wait for the command.

        Returns:
            `ExecuteOffloadResult` describing where the output ended up.
        """
        use_timeout = timeout is not None and execute_accepts_timeout(type(self))
        if not self.enable_capture_offload:
            result = await self.aexecute(command, timeout=timeout) if use_timeout else await self.aexecute(command)
            return ExecuteOffloadResult(offloaded=False, response=result)
        wrapper = _build_capture_execute_cmd(command, capture_path, inline_budget=max_inline_bytes, max_capture_bytes=max_capture_bytes)
        result = await self.aexecute(wrapper, timeout=timeout) if use_timeout else await self.aexecute(wrapper)
        return _parse_capture_execute_output(result.output, backend_truncated=result.truncated)

    # -- listing --------------------------------------------------------------

    def ls(self, path: str) -> LsResult:
        """List a directory with metadata, via `os.scandir` on the sandbox.

        Args:
            path: Absolute path to the directory to list.

        Returns:
            `LsResult` with directory entries, or an error.
        """
        result = self.execute(_build_ls_cmd(path))
        return _parse_ls_output(result.output, path)

    async def als(self, path: str) -> LsResult:
        """Async version of `ls`, delegating to `aexecute`.

        Args:
            path: Absolute path to the directory to list.

        Returns:
            `LsResult` with directory entries, or an error.
        """
        result = await self.aexecute(_build_ls_cmd(path))
        return _parse_ls_output(result.output, path)

    # -- read -----------------------------------------------------------------

    def read_file(
        self,
        file_path: str,
        offset: int = 0,
        limit: int = 2000,
    ) -> ReadResult:
        """Read file content with server-side line-based pagination.

        Runs a Python script on the sandbox that reads the file, detects the
        encoding, and applies offset/limit pagination for text files. Only the
        requested page crosses the wire, and text output is capped at
        `MAX_OUTPUT_BYTES` to avoid backend stdout/log transport failures; on
        overflow the content is truncated and `TRUNCATION_MSG` appended.

        Binary files (by extension, or by a failed UTF-8 decode of the leading
        bytes) are returned base64-encoded without pagination, up to
        `MAX_BINARY_BYTES`.

        Args:
            file_path: Absolute path to the file to read.
            offset: Line number to start from (0-indexed). Text files only.
            limit: Maximum number of lines to return. Text files only.

        Returns:
            `ReadResult` with `file_data` on success, or `error` on failure.
        """
        result = self.execute(_build_read_cmd(file_path, offset, limit))
        return _parse_read_output(result.output, file_path)

    async def aread_file(
        self,
        file_path: str,
        offset: int = 0,
        limit: int = 2000,
    ) -> ReadResult:
        """Async version of `read_file`, delegating to `aexecute`.

        Args:
            file_path: Absolute path to the file to read.
            offset: Line number to start from (0-indexed).
            limit: Maximum number of lines to return.

        Returns:
            `ReadResult` with `file_data` on success, or `error` on failure.
        """
        result = await self.aexecute(_build_read_cmd(file_path, offset, limit))
        return _parse_read_output(result.output, file_path)

    # -- write ----------------------------------------------------------------

    def _write_preflight(self, file_path: str) -> WriteResult | None:
        """Create parent directories for `write()`.

        Subclasses overriding `write()` (e.g. to use a native SDK transport)
        should call this first so they preserve the parent-mkdir semantics of
        `BaseSandbox.write()`. There is a TOCTOU window between this and the
        actual write — inherent to splitting the operation across two backend
        calls.

        Args:
            file_path: Absolute path for the file about to be written.

        Returns:
            `None` if the preflight passes (parents created); a `WriteResult`
                with `error` set if it fails.
        """
        result = self.execute(_build_write_preflight_cmd(file_path))
        return _check_preflight_result(result, file_path)

    async def _awrite_preflight(self, file_path: str) -> WriteResult | None:
        """Async version of `_write_preflight`, delegating to `aexecute`.

        Args:
            file_path: Absolute path for the file about to be written.

        Returns:
            `None` if the preflight passes; a `WriteResult` with `error` set if
                it fails.
        """
        result = await self.aexecute(_build_write_preflight_cmd(file_path))
        return _check_preflight_result(result, file_path)

    def write(
        self,
        file_path: str,
        content: str,
    ) -> WriteResult:
        """Write content to a file, creating it or overwriting an existing one.

        Content transfer is delegated to `upload_files()` rather than embedded in
        a shell command, so writing a large file cannot hit ARG_MAX or a
        provider's `execute` request-body limit.

        Args:
            file_path: Absolute path for the file.
            content: UTF-8 text content to write.

        Returns:
            `WriteResult` with `path` on success, or `error` on failure.

        Raises:
            AssertionError: If `upload_files()` returns no response for the
                single file it was handed — a broken backend contract.
        """
        preflight_error = self._write_preflight(file_path)
        if preflight_error is not None:
            return preflight_error

        responses = self.upload_files([(file_path, content.encode("utf-8"))])
        return self._write_result_from_upload(responses, file_path)

    async def awrite(
        self,
        file_path: str,
        content: str,
    ) -> WriteResult:
        """Async version of `write`, delegating to `aexecute` and `aupload_files`.

        Args:
            file_path: Absolute path for the file.
            content: UTF-8 text content to write.

        Returns:
            `WriteResult` with `path` on success, or `error` on failure.

        Raises:
            AssertionError: If `aupload_files()` returns no response.
        """
        preflight_error = await self._awrite_preflight(file_path)
        if preflight_error is not None:
            return preflight_error

        responses = await self.aupload_files([(file_path, content.encode("utf-8"))])
        return self._write_result_from_upload(responses, file_path)

    @staticmethod
    def _write_result_from_upload(responses: list[FileUploadResponse], file_path: str) -> WriteResult:
        """Turn the upload responses for a single-file write into a `WriteResult`.

        Args:
            responses: Responses returned by `upload_files` / `aupload_files`.
            file_path: The path that was written.

        Returns:
            `WriteResult` with `path` on success, or `error` on failure.

        Raises:
            AssertionError: If `responses` is empty — the backend violated the
                one-response-per-input-file contract.
        """
        if not responses:
            msg = f"upload_files was expected to return 1 result for '{file_path}', but it returned {len(responses)}"
            raise AssertionError(msg)
        response = responses[0]
        if response.error:
            return WriteResult(error=f"Failed to write file '{file_path}': {response.error}")
        # External storage: nothing to thread back into LangGraph state.
        return WriteResult(path=file_path, files_update=None)

    # -- edit -----------------------------------------------------------------

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
        *,
        base_content: dict[str, Any] | None = None,  # noqa: ARG002  # accepted for protocol parity; the sandbox is the source of truth
    ) -> EditResult:
        """Edit a file by replacing exact string occurrences.

        For small payloads (combined old/new under `_EDIT_INLINE_MAX_BYTES`) this
        runs a server-side script via `execute()` — a single round-trip, no file
        transfer. For larger payloads it uploads old/new as temp files and runs a
        server-side replace script, so the source file never leaves the sandbox
        and the command stays well under ARG_MAX.

        `read_file()` normalizes CRLF to LF, so `old_string` is typically LF-only
        even for a CRLF file. The server-side script tries `old_string` as sent,
        then CRLF- and LF-normalized variants, and applies the same transform to
        `new_string` so the file's line-ending style survives the write. On a
        mixed-ending file, `replace_all=True` only touches occurrences in the
        first matching style; a follow-up edit can replace the rest.

        Args:
            file_path: Absolute path to the file to edit.
            old_string: The exact substring to find.
            new_string: The replacement string.
            replace_all: If `True`, replace every occurrence. If `False`
                (default), error when more than one occurrence exists.
            base_content: Ignored. The sandbox filesystem already reflects prior
                edits, so a batch caller's working copy is not needed.

        Returns:
            `EditResult` with `path` and `occurrences` on success, or `error` on
                failure.
        """
        if self._edit_payload_size(old_string, new_string) <= _EDIT_INLINE_MAX_BYTES:
            return self._edit_inline(file_path, old_string, new_string, replace_all=replace_all)
        return self._edit_via_upload(file_path, old_string, new_string, replace_all=replace_all)

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
        *,
        base_content: dict[str, Any] | None = None,  # noqa: ARG002  # accepted for protocol parity; the sandbox is the source of truth
    ) -> EditResult:
        """Async version of `edit`, delegating to `aexecute` and `aupload_files`.

        Args:
            file_path: Absolute path to the file to edit.
            old_string: The exact substring to find.
            new_string: The replacement string.
            replace_all: If `True`, replace every occurrence.
            base_content: Ignored — see `edit`.

        Returns:
            `EditResult` with `path` and `occurrences` on success, or `error` on
                failure.
        """
        if self._edit_payload_size(old_string, new_string) <= _EDIT_INLINE_MAX_BYTES:
            return await self._aedit_inline(file_path, old_string, new_string, replace_all=replace_all)
        return await self._aedit_via_upload(file_path, old_string, new_string, replace_all=replace_all)

    @staticmethod
    def _edit_payload_size(old_string: str, new_string: str) -> int:
        """Return the combined UTF-8 byte size of an edit's strings.

        Args:
            old_string: The search string.
            new_string: The replacement string.

        Returns:
            Combined size in bytes.
        """
        return len(old_string.encode("utf-8")) + len(new_string.encode("utf-8"))

    def _edit_inline(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        *,
        replace_all: bool,
    ) -> EditResult:
        """Replace server-side via `execute()` — a single round-trip.

        Args:
            file_path: Absolute path to the file to edit.
            old_string: The exact substring to find.
            new_string: The replacement string.
            replace_all: Whether to replace every occurrence.

        Returns:
            `EditResult` with `occurrences` on success, or `error`.
        """
        result = self.execute(_build_edit_inline_cmd(file_path, old_string, new_string, replace_all=replace_all))
        return _parse_edit_output(result.output, file_path, old_string)

    async def _aedit_inline(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        *,
        replace_all: bool,
    ) -> EditResult:
        """Async version of `_edit_inline`, delegating to `aexecute`.

        Args:
            file_path: Absolute path to the file to edit.
            old_string: The exact substring to find.
            new_string: The replacement string.
            replace_all: Whether to replace every occurrence.

        Returns:
            `EditResult` with `occurrences` on success, or `error`.
        """
        result = await self.aexecute(_build_edit_inline_cmd(file_path, old_string, new_string, replace_all=replace_all))
        return _parse_edit_output(result.output, file_path, old_string)

    def _edit_via_upload(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        *,
        replace_all: bool,
    ) -> EditResult:
        """Upload old/new as temp files, then replace server-side.

        The source file never leaves the sandbox: only the old/new strings are
        transferred, via `upload_files()`. The server-side script reads them,
        performs the replacement, and removes the temp files.

        Args:
            file_path: Absolute path to the file to edit.
            old_string: The exact substring to find.
            new_string: The replacement string.
            replace_all: Whether to replace every occurrence.

        Returns:
            `EditResult` with `occurrences` on success, or `error`.
        """
        old_tmp, new_tmp = _edit_tmp_paths()
        responses = self.upload_files([(old_tmp, old_string.encode("utf-8")), (new_tmp, new_string.encode("utf-8"))])
        upload_error = self._check_edit_upload(responses, file_path)
        if upload_error is not None:
            return upload_error

        result = self.execute(_build_edit_tmpfile_cmd(file_path, old_tmp, new_tmp, replace_all=replace_all))
        output = result.output.rstrip()
        if not self._edit_output_is_json(output):
            # The script may not have started, or its `finally` may not have run:
            # best-effort cleanup so the temp files don't leak.
            cleanup = self.execute(f"rm -f {shlex.quote(old_tmp)} {shlex.quote(new_tmp)}")
            if cleanup.exit_code != 0:
                logger.warning("Failed to clean up temp files for edit %s: %s", file_path, cleanup.output[:200])
        return _parse_edit_output(output, file_path, old_string)

    async def _aedit_via_upload(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        *,
        replace_all: bool,
    ) -> EditResult:
        """Async version of `_edit_via_upload`, delegating to `aexecute` and `aupload_files`.

        Args:
            file_path: Absolute path to the file to edit.
            old_string: The exact substring to find.
            new_string: The replacement string.
            replace_all: Whether to replace every occurrence.

        Returns:
            `EditResult` with `occurrences` on success, or `error`.
        """
        old_tmp, new_tmp = _edit_tmp_paths()
        responses = await self.aupload_files([(old_tmp, old_string.encode("utf-8")), (new_tmp, new_string.encode("utf-8"))])
        upload_error = self._check_edit_upload(responses, file_path)
        if upload_error is not None:
            return upload_error

        result = await self.aexecute(_build_edit_tmpfile_cmd(file_path, old_tmp, new_tmp, replace_all=replace_all))
        output = result.output.rstrip()
        if not self._edit_output_is_json(output):
            cleanup = await self.aexecute(f"rm -f {shlex.quote(old_tmp)} {shlex.quote(new_tmp)}")
            if cleanup.exit_code != 0:
                logger.warning("Failed to clean up temp files for edit %s: %s", file_path, cleanup.output[:200])
        return _parse_edit_output(output, file_path, old_string)

    @staticmethod
    def _check_edit_upload(responses: list[FileUploadResponse], file_path: str) -> EditResult | None:
        """Validate the temp-file uploads for a large edit.

        Args:
            responses: Responses from uploading the old/new temp files.
            file_path: The file being edited, used in error messages.

        Returns:
            `None` when both uploads succeeded; an `EditResult` with `error`
                otherwise.
        """
        expected = 2
        if len(responses) < expected:
            return EditResult(error=f"Error editing file '{file_path}': upload returned no response")
        for response in responses:
            if response.error:
                return EditResult(error=f"Error editing file '{file_path}': {response.error}")
        return None

    @staticmethod
    def _edit_output_is_json(output: str) -> bool:
        """Return whether the edit script produced parseable JSON.

        Used to decide whether the script's own temp-file cleanup ran.

        Args:
            output: Raw command output.

        Returns:
            True when `output` parses as JSON.
        """
        try:
            json.loads(output)
        except (json.JSONDecodeError, ValueError):
            return False
        return True

    # -- delete ---------------------------------------------------------------

    def delete(self, file_path: str) -> DeleteResult:
        """Delete a file or directory from the sandbox via a server-side `rm -rf`.

        Runs `test -e || test -L` first, so a path that does not exist (and is
        not a broken symlink) reports a not-found error, matching the contract of
        `FilesystemBackend` and `StateBackend`. A shell `test` has no error
        channel, so a non-zero probe conflates "absent" with "unstattable" (e.g.
        an unsearchable parent); an *unknown* exit code is not treated as absent
        and falls through to the delete.

        `rm -rf` removes directories recursively. Unlike the state-backed
        backends, the sandbox does not enumerate the removed children — that
        would cost an extra round-trip — so `deleted_paths` carries only the
        requested path.

        Args:
            file_path: Absolute path to the file or directory to delete.

        Returns:
            `DeleteResult` with the deleted path on success, or an error if the
                path does not exist or `rm` fails.
        """
        exists = self.execute(self._build_delete_probe_cmd(file_path))
        if exists.exit_code is not None and exists.exit_code != 0:
            return DeleteResult(error=f"Error: '{file_path}' not found")
        result = self.execute(f"rm -rf {shlex.quote(file_path)}")
        return self._parse_delete_result(result, file_path)

    async def adelete(self, file_path: str) -> DeleteResult:
        """Async version of `delete`, delegating to `aexecute`.

        Args:
            file_path: Absolute path to the file or directory to delete.

        Returns:
            `DeleteResult` with the deleted path on success, or an error.
        """
        exists = await self.aexecute(self._build_delete_probe_cmd(file_path))
        if exists.exit_code is not None and exists.exit_code != 0:
            return DeleteResult(error=f"Error: '{file_path}' not found")
        result = await self.aexecute(f"rm -rf {shlex.quote(file_path)}")
        return self._parse_delete_result(result, file_path)

    @staticmethod
    def _build_delete_probe_cmd(file_path: str) -> str:
        """Build the existence probe run before a delete.

        `shlex.quote` only neutralizes shell metacharacters so the path reaches
        `rm` as a single literal argument. It is NOT a security boundary: it does
        not confine the deletion to any sandbox root, nor block traversal.
        Whatever the sandbox shell can reach, this can delete.

        Args:
            file_path: Absolute path to probe.

        Returns:
            The shell command string.
        """
        quoted = shlex.quote(file_path)
        return f"test -e {quoted} || test -L {quoted}"

    @staticmethod
    def _parse_delete_result(result: ExecuteResponse, file_path: str) -> DeleteResult:
        """Turn the `rm -rf` response into a `DeleteResult`.

        Args:
            result: Response from running `rm -rf`.
            file_path: The path that was deleted.

        Returns:
            `DeleteResult` with the deleted path on success, or an error.
        """
        if result.exit_code == 0:
            # External storage: nothing to thread back into LangGraph state.
            return DeleteResult(path=file_path, files_update=None, deleted_paths=[file_path])
        return DeleteResult(error=f"Error deleting file '{file_path}': {result.output.strip() or 'unknown error'}")

    # -- search ---------------------------------------------------------------

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> GrepResult:
        """Search file contents for a literal string using `grep -F`.

        Args:
            pattern: Literal string to search for (not a regex).
            path: Directory or file to search in. Defaults to `"."`.
            glob: Optional glob restricting which files are searched. Patterns
                without a `/` (e.g. `*.py`) match basenames at any depth via
                `grep --include`; patterns containing a `/` (e.g. `src/**/*.py`)
                match the search-root-relative path via an in-sandbox Python
                glob, because `--include` would match zero files.

        Returns:
            `GrepResult` with a list of `GrepMatch` dicts, or `error` on failure.
        """
        result = self.execute(_build_grep_cmd(pattern, path, glob))
        return _parse_grep_output(result, path)

    async def agrep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> GrepResult:
        """Async version of `grep`, delegating to `aexecute` under a timeout guard.

        Args:
            pattern: Literal string to search for (not a regex).
            path: Directory or file to search in.
            glob: Optional glob restricting which files are searched.

        Returns:
            `GrepResult` with matches, or `error` on failure or timeout.
        """
        result: ExecuteResponse | None = None
        with anyio.move_on_after(ASYNC_GREP_TIMEOUT):
            result = await self.aexecute(_build_grep_cmd(pattern, path, glob))

        if result is None:
            logger.warning("agrep timed out after %ds (pattern=%r, path=%r, glob=%r)", ASYNC_GREP_TIMEOUT, pattern, path, glob)
            return GrepResult(error=f"Error: grep timed out after {ASYNC_GREP_TIMEOUT}s. Try a more specific pattern or a narrower path.")
        return _parse_grep_output(result, path)

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        """Find files matching a glob pattern.

        Args:
            pattern: Glob pattern with wildcards.
            path: Base directory to search from. Defaults to `"/"`.

        Returns:
            `GlobResult` with matching files, or `error` on failure.
        """
        search_path = path or "/"
        result = self.execute(_build_glob_cmd(pattern, search_path))
        return _parse_glob_output(result.output, search_path)

    async def aglob(self, pattern: str, path: str | None = None) -> GlobResult:
        """Async version of `glob`, delegating to `aexecute`.

        Args:
            pattern: Glob pattern with wildcards.
            path: Base directory to search from.

        Returns:
            `GlobResult` with matching files, or `error` on failure.
        """
        search_path = path or "/"
        result = await self.aexecute(_build_glob_cmd(pattern, search_path))
        return _parse_glob_output(result.output, search_path)

    # -- abstract surface -----------------------------------------------------

    @property
    @abstractmethod
    def id(self) -> str:
        """Unique identifier for the sandbox backend."""

    @abstractmethod
    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """Upload multiple files to the sandbox.

        Implementations must support partial success: catch exceptions per-file
        and return errors in `FileUploadResponse` objects rather than raising.

        Implementations are responsible for creating the parent directory when
        the caller's permissions allow it.
        """

    @abstractmethod
    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Download multiple files from the sandbox.

        Implementations must support partial success: catch exceptions per-file
        and return errors in `FileDownloadResponse` objects rather than raising.
        """
