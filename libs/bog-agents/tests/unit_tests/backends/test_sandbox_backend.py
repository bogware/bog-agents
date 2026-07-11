"""Tests for `BaseSandbox`.

Covers the command templates (they must survive `.format()` — curly braces in
the embedded Python have to be escaped), the builder/parser split that the sync
and async methods share, capture-at-source offload, the inline-vs-upload edit
routing, delete, and binary reads.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from bog_agents.backends.protocol import (
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
)
from bog_agents.backends.sandbox import (
    _EDIT_COMMAND_TEMPLATE,
    _EDIT_INLINE_MAX_BYTES,
    _EXECUTE_CAPTURE_SENTINEL,
    _GLOB_COMMAND_TEMPLATE,
    _GREP_PATH_GLOB_TEMPLATE,
    _READ_COMMAND_TEMPLATE,
    _WRITE_CHECK_TEMPLATE,
    MAX_BINARY_BYTES,
    MAX_OUTPUT_BYTES,
    TRUNCATION_MSG,
    BaseSandbox,
    _build_capture_execute_cmd,
    _parse_capture_execute_output,
)


class MockSandbox(BaseSandbox):
    """Concrete `BaseSandbox` that records commands instead of running them."""

    def __init__(self, *, output: str = "", exit_code: int = 0) -> None:
        self.commands: list[str] = []
        self.uploads: list[tuple[str, bytes]] = []
        self.output = output
        self.exit_code = exit_code
        # Per-command output overrides, matched by substring of the command.
        self.responses: list[tuple[str, ExecuteResponse]] = []

    @property
    def id(self) -> str:
        return "mock-sandbox"

    @property
    def last_command(self) -> str:
        return self.commands[-1]

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        self.commands.append(command)
        for needle, response in self.responses:
            if needle in command:
                return response
        return ExecuteResponse(output=self.output, exit_code=self.exit_code, truncated=False)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        self.uploads.extend(files)
        return [FileUploadResponse(path=path, error=None) for path, _ in files]

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return [FileDownloadResponse(path=p, content=None, error="not_implemented") for p in paths]


def _read_ok(content: str, encoding: str = "utf-8") -> ExecuteResponse:
    return ExecuteResponse(output=json.dumps({"encoding": encoding, "content": content}), exit_code=0)


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


def test_read_command_template_formats() -> None:
    cmd = _READ_COMMAND_TEMPLATE.format(path_b64="cGF0aA==", file_type="text", offset=0, limit=10)
    assert "python3 -c" in cmd
    assert "cGF0aA==" in cmd


def test_edit_command_template_formats() -> None:
    payload_b64 = base64.b64encode(json.dumps({"path": "/f", "old": "{a}", "new": "{b}"}).encode()).decode()
    cmd = _EDIT_COMMAND_TEMPLATE.format(payload_b64=payload_b64)
    assert payload_b64 in cmd
    assert "__BOG_AGENTS_EDIT_EOF__" in cmd
    # The heredoc feed must end on a newline or some integrations never see EOF.
    assert cmd.endswith("\n")


def test_glob_and_grep_and_write_templates_format() -> None:
    assert "python3 -c" in _GLOB_COMMAND_TEMPLATE.format(path_b64="cA==", pattern_b64="cQ==")
    assert "python3 -c" in _GREP_PATH_GLOB_TEMPLATE.format(path_b64="cA==", glob_b64="Zw==", pattern_b64="cQ==")
    assert "python3 -c" in _WRITE_CHECK_TEMPLATE.format(path_b64="cA==")


def test_read_constants_match_template() -> None:
    """The Python constants and the literals inside the read script must agree."""
    assert MAX_BINARY_BYTES == 500 * 1024
    assert MAX_OUTPUT_BYTES == 500 * 1024
    assert "MAX_OUTPUT_BYTES = 500 * 1024" in _READ_COMMAND_TEMPLATE
    assert "MAX_BINARY_BYTES = 500 * 1024" in _READ_COMMAND_TEMPLATE
    # The message the script appends is the one callers strip/detect.
    assert TRUNCATION_MSG.strip().startswith("[Output was truncated")
    assert "[Output was truncated due to size limits. " in _READ_COMMAND_TEMPLATE


# ---------------------------------------------------------------------------
# ls
# ---------------------------------------------------------------------------


def test_ls_parses_entries() -> None:
    sandbox = MockSandbox(output='{"path": "/a/f.txt", "is_dir": false}\n{"path": "/a/sub", "is_dir": true}')

    result = sandbox.ls("/a")

    assert result.error is None
    assert result.entries == [
        {"path": "/a/f.txt", "is_dir": False},
        {"path": "/a/sub", "is_dir": True},
    ]


def test_ls_surfaces_script_error() -> None:
    sandbox = MockSandbox(output='{"error": "path_not_found"}')

    result = sandbox.ls("/nope")

    assert result.entries is None
    assert result.error == "Path '/nope': path_not_found"


async def test_als_uses_the_same_command() -> None:
    sandbox = MockSandbox(output='{"path": "/a/f.txt", "is_dir": false}')

    result = await sandbox.als("/a")

    assert result.entries == [{"path": "/a/f.txt", "is_dir": False}]


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


def test_read_file_returns_file_data() -> None:
    sandbox = MockSandbox()
    sandbox.responses = [("python3 -c", _read_ok("hello\nworld"))]

    result = sandbox.read_file("/f.txt", offset=0, limit=10)

    assert result.error is None
    assert result.file_data == {"content": "hello\nworld", "encoding": "utf-8"}
    # The path is base64-encoded into the script, never interpolated raw.
    assert "/f.txt" not in sandbox.last_command
    assert base64.b64encode(b"/f.txt").decode() in sandbox.last_command


def test_read_file_binary_extension_requests_binary_route() -> None:
    raw = b"\x89PNG\r\n\x1a\n"
    sandbox = MockSandbox()
    sandbox.responses = [("python3 -c", _read_ok(base64.b64encode(raw).decode(), encoding="base64"))]

    result = sandbox.read_file("/img.png")

    assert result.file_data is not None
    assert result.file_data["encoding"] == "base64"
    assert base64.b64decode(result.file_data["content"]) == raw
    # The extension is classified client-side and passed to the script.
    assert "file_type = 'image'" in sandbox.last_command


def test_read_file_surfaces_script_error() -> None:
    sandbox = MockSandbox()
    sandbox.responses = [("python3 -c", ExecuteResponse(output='{"error": "file_not_found"}', exit_code=0))]

    result = sandbox.read_file("/missing.txt")

    assert result.file_data is None
    assert result.error == "File '/missing.txt': file_not_found"


def test_read_file_rejects_non_json_response() -> None:
    sandbox = MockSandbox(output="bash: python3: command not found")

    result = sandbox.read_file("/f.txt")

    assert result.file_data is None
    assert "unexpected server response" in (result.error or "")


def test_read_renders_line_numbers_via_protocol_shim() -> None:
    """`read()` is the rendered form of `read_file()` — supplied by the protocol."""
    sandbox = MockSandbox()
    sandbox.responses = [("python3 -c", _read_ok("alpha\nbeta"))]

    rendered = sandbox.read("/f.txt")

    assert rendered == "     1\talpha\n     2\tbeta"


async def test_aread_file_native() -> None:
    sandbox = MockSandbox()
    sandbox.responses = [("python3 -c", _read_ok("hi"))]

    result = await sandbox.aread_file("/f.txt")

    assert result.file_data == {"content": "hi", "encoding": "utf-8"}


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


def test_write_creates_parents_then_uploads() -> None:
    sandbox = MockSandbox()

    result = sandbox.write("/deep/dir/f.txt", "content")

    assert result.error is None
    assert result.path == "/deep/dir/f.txt"
    assert result.files_update is None
    # Preflight first, and the content goes through upload_files (no ARG_MAX).
    assert "os.makedirs" in sandbox.commands[0]
    assert sandbox.uploads == [("/deep/dir/f.txt", b"content")]


def test_write_overwrites_existing_file() -> None:
    """Approved divergence from the old behavior: write() no longer errors on an
    existing path. It overwrites, matching upstream and every other backend.
    """
    sandbox = MockSandbox()

    first = sandbox.write("/f.txt", "one")
    second = sandbox.write("/f.txt", "two")

    assert first.error is None
    assert second.error is None
    assert second.path == "/f.txt"
    assert sandbox.uploads == [("/f.txt", b"one"), ("/f.txt", b"two")]


def test_write_reports_upload_failure() -> None:
    sandbox = MockSandbox()

    def failing_upload(files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return [FileUploadResponse(path=p, error="permission_denied") for p, _ in files]

    sandbox.upload_files = failing_upload  # type: ignore[method-assign]

    result = sandbox.write("/f.txt", "x")

    assert result.path is None
    assert result.error == "Failed to write file '/f.txt': permission_denied"


def test_write_preflight_failure_short_circuits_upload() -> None:
    sandbox = MockSandbox(output="Error: Permission denied", exit_code=1)

    result = sandbox.write("/root/f.txt", "x")

    assert result.error == "Error: Permission denied"
    assert sandbox.uploads == []


def test_write_large_content_never_enters_the_command() -> None:
    sandbox = MockSandbox()
    content = "x" * 200_000

    sandbox.write("/big.txt", content)

    assert all(content not in cmd for cmd in sandbox.commands)
    assert sandbox.uploads == [("/big.txt", content.encode())]


async def test_awrite_uses_async_upload() -> None:
    sandbox = MockSandbox()

    result = await sandbox.awrite("/f.txt", "content")

    assert result.path == "/f.txt"
    assert sandbox.uploads == [("/f.txt", b"content")]


# ---------------------------------------------------------------------------
# edit
# ---------------------------------------------------------------------------


def test_edit_inline_for_small_payload() -> None:
    sandbox = MockSandbox(output=json.dumps({"count": 2}))

    result = sandbox.edit("/f.txt", "old", "new", replace_all=True)

    assert result.error is None
    assert result.occurrences == 2
    assert result.path == "/f.txt"
    assert sandbox.uploads == []
    assert "__BOG_AGENTS_EDIT_EOF__" in sandbox.last_command


def test_edit_accepts_and_ignores_base_content() -> None:
    """`base_content` exists for protocol parity; the sandbox is the source of truth."""
    sandbox = MockSandbox(output=json.dumps({"count": 1}))

    result = sandbox.edit("/f.txt", "old", "new", base_content={"content": "stale", "encoding": "utf-8"})

    assert result.occurrences == 1


def test_edit_routes_large_payload_through_upload() -> None:
    sandbox = MockSandbox(output=json.dumps({"count": 1}))
    big = "y" * (_EDIT_INLINE_MAX_BYTES + 1)

    result = sandbox.edit("/f.txt", big, "small")

    assert result.occurrences == 1
    # old/new were transferred as temp files, not embedded in the command.
    assert len(sandbox.uploads) == 2
    old_tmp, old_bytes = sandbox.uploads[0]
    assert old_tmp.startswith("/tmp/.bog_agents_edit_")
    assert old_bytes == big.encode()
    assert all(big not in cmd for cmd in sandbox.commands)


def test_edit_via_upload_cleans_up_on_garbage_response() -> None:
    sandbox = MockSandbox(output="Killed")
    big = "y" * (_EDIT_INLINE_MAX_BYTES + 1)

    result = sandbox.edit("/f.txt", big, "small")

    assert "unexpected server response" in (result.error or "")
    assert any(cmd.startswith("rm -f /tmp/.bog_agents_edit_") for cmd in sandbox.commands)


def test_edit_via_upload_reports_upload_error() -> None:
    sandbox = MockSandbox()

    def failing_upload(files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return [FileUploadResponse(path=p, error="permission_denied") for p, _ in files]

    sandbox.upload_files = failing_upload  # type: ignore[method-assign]

    result = sandbox.edit("/f.txt", "y" * (_EDIT_INLINE_MAX_BYTES + 1), "small")

    assert result.error == "Error editing file '/f.txt': permission_denied"
    assert sandbox.commands == []


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("string_not_found", "Error: String not found in file: 'old'"),
        ("multiple_occurrences", "Error: String 'old' appears multiple times. Use replace_all=True to replace all occurrences."),
        ("file_not_found", "Error: File '/f.txt' not found"),
        ("not_a_text_file", "Error: File '/f.txt' is not a text file"),
        ("permission_denied", "Error: Permission denied editing file '/f.txt'"),
    ],
)
def test_edit_maps_server_error_codes(code: str, expected: str) -> None:
    sandbox = MockSandbox(output=json.dumps({"error": code}))

    result = sandbox.edit("/f.txt", "old", "new")

    assert result.error == expected


async def test_aedit_inline() -> None:
    sandbox = MockSandbox(output=json.dumps({"count": 1}))

    result = await sandbox.aedit("/f.txt", "old", "new")

    assert result.occurrences == 1


async def test_aedit_routes_large_payload_through_upload() -> None:
    sandbox = MockSandbox(output=json.dumps({"count": 1}))

    result = await sandbox.aedit("/f.txt", "y" * (_EDIT_INLINE_MAX_BYTES + 1), "small")

    assert result.occurrences == 1
    assert len(sandbox.uploads) == 2


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


def test_delete_removes_existing_path() -> None:
    sandbox = MockSandbox()

    result = sandbox.delete("/a/f.txt")

    assert result.error is None
    assert result.path == "/a/f.txt"
    assert result.deleted_paths == ["/a/f.txt"]
    assert result.files_update is None
    assert sandbox.commands[0].startswith("test -e ")
    assert sandbox.commands[1] == "rm -rf /a/f.txt"


def test_delete_reports_missing_path() -> None:
    sandbox = MockSandbox()
    sandbox.responses = [("test -e", ExecuteResponse(output="", exit_code=1))]

    result = sandbox.delete("/missing")

    assert result.error == "Error: '/missing' not found"
    # The probe failed, so no `rm` was issued.
    assert len(sandbox.commands) == 1


def test_delete_quotes_path() -> None:
    sandbox = MockSandbox()

    sandbox.delete("/a b/f.txt; rm -rf /")

    assert "rm -rf '/a b/f.txt; rm -rf /'" in sandbox.commands[1]


def test_delete_reports_rm_failure() -> None:
    sandbox = MockSandbox()
    sandbox.responses = [("rm -rf", ExecuteResponse(output="Permission denied", exit_code=1))]

    result = sandbox.delete("/a")

    assert result.error == "Error deleting file '/a': Permission denied"


def test_delete_is_advertised_as_supported() -> None:
    from bog_agents.backends.protocol import supports_delete

    assert supports_delete(MockSandbox()) is True


async def test_adelete() -> None:
    sandbox = MockSandbox()

    result = await sandbox.adelete("/a/f.txt")

    assert result.path == "/a/f.txt"
    assert result.deleted_paths == ["/a/f.txt"]


# ---------------------------------------------------------------------------
# grep / glob
# ---------------------------------------------------------------------------


def test_grep_parses_nul_separated_records() -> None:
    sandbox = MockSandbox(output="/a/f.py\x001:import os\n/a/g.py\x0042:import os")

    result = sandbox.grep("import os", path="/a")

    assert result.error is None
    assert result.matches == [
        {"path": "/a/f.py", "line": 1, "text": "import os"},
        {"path": "/a/g.py", "line": 42, "text": "import os"},
    ]
    assert "grep -rHnFZ" in sandbox.last_command


def test_grep_basename_glob_uses_include() -> None:
    sandbox = MockSandbox()

    sandbox.grep("needle", path="/a", glob="*.py")

    assert "--include='*.py'" in sandbox.last_command


def test_grep_path_glob_routes_to_python() -> None:
    """GNU `--include` only matches basenames, so `src/**/*.py` must not use it."""
    sandbox = MockSandbox()

    sandbox.grep("needle", path="/a", glob="src/**/*.py")

    assert "--include" not in sandbox.last_command
    assert "python3 -c" in sandbox.last_command
    assert base64.b64encode(b"src/**/*.py").decode() in sandbox.last_command


def test_grep_glob_cannot_break_out_of_the_shell() -> None:
    """P1-2 regression: a hostile glob must never reach the shell unquoted."""
    import shlex

    malicious_basename = "x'; touch /tmp/pwned #"  # contains `/`, so it takes the Python route
    malicious_flat = "x'; touch pwned #"

    sandbox = MockSandbox()
    sandbox.grep("needle", path=".", glob=malicious_flat)
    assert f"--include={shlex.quote(malicious_flat)}" in sandbox.last_command
    assert "--include='x'; touch" not in sandbox.last_command

    sandbox.grep("needle", path=".", glob=malicious_basename)
    # Base64-encoded into the Python script — the payload never reaches the shell.
    assert malicious_basename not in sandbox.last_command
    assert base64.b64encode(malicious_basename.encode()).decode() in sandbox.last_command


def test_grep_reports_nonzero_exit() -> None:
    sandbox = MockSandbox(output="grep: /nope: No such file", exit_code=2)

    result = sandbox.grep("needle", path="/nope")

    assert result.matches is None
    assert "No such file" in (result.error or "")


async def test_agrep_native() -> None:
    sandbox = MockSandbox(output="/a/f.py\x001:hit")

    result = await sandbox.agrep("hit", path="/a")

    assert result.matches == [{"path": "/a/f.py", "line": 1, "text": "hit"}]


def test_glob_parses_entries() -> None:
    sandbox = MockSandbox(output='{"path": "main.py", "is_dir": false}')

    result = sandbox.glob("**/*.py", path="/src")

    assert result.matches == [{"path": "main.py", "is_dir": False}]
    assert base64.b64encode(b"/src").decode() in sandbox.last_command


def test_glob_surfaces_script_error() -> None:
    sandbox = MockSandbox(output='{"error": "path_not_found"}')

    result = sandbox.glob("*.py", path="/nope")

    assert result.matches is None
    assert result.error == "Path '/nope': path_not_found"


async def test_aglob_native() -> None:
    sandbox = MockSandbox(output='{"path": "main.py", "is_dir": false}')

    result = await sandbox.aglob("*.py", path="/src")

    assert result.matches == [{"path": "main.py", "is_dir": False}]


# ---------------------------------------------------------------------------
# capture-at-source offload
# ---------------------------------------------------------------------------


def test_offload_disabled_runs_command_unwrapped() -> None:
    sandbox = MockSandbox(output="hello")

    result = sandbox.execute_with_offload("echo hello", "/tmp/cap", max_inline_bytes=100)

    assert result.offloaded is False
    assert result.response.output == "hello"
    assert sandbox.last_command == "echo hello"


class _OffloadSandbox(MockSandbox):
    enable_capture_offload = True


def test_offload_enabled_wraps_command() -> None:
    sandbox = _OffloadSandbox(output=f"{_EXECUTE_CAPTURE_SENTINEL} 0 0 0\nhello")

    result = sandbox.execute_with_offload("echo hello", "/tmp/cap.txt", max_inline_bytes=100)

    assert result.offloaded is False
    assert result.response.output == "hello"
    assert result.response.exit_code == 0
    # The command is embedded verbatim in the wrapper's heredoc.
    assert "echo hello" in sandbox.last_command
    assert "__bog_f=/tmp/cap.txt" in sandbox.last_command


def test_offload_enabled_reports_offloaded_preview() -> None:
    sandbox = _OffloadSandbox(output=f"{_EXECUTE_CAPTURE_SENTINEL} 3 1 1\nhead...\n... [900 lines truncated] ...\n...tail")

    result = sandbox.execute_with_offload("big", "/tmp/cap.txt", max_inline_bytes=10)

    assert result.offloaded is True
    assert result.response.exit_code == 3
    assert result.response.truncated is True
    assert result.response.output.startswith("head...")


def test_offload_falls_back_when_meta_line_is_missing() -> None:
    """If transport mangled the wrapper output, return it verbatim — never re-run."""
    sandbox = _OffloadSandbox(output="partial output with no sentinel")

    result = sandbox.execute_with_offload("cmd", "/tmp/cap.txt", max_inline_bytes=10)

    assert result.offloaded is False
    assert result.response.output == "partial output with no sentinel"


def test_capture_wrapper_delimiter_avoids_collision_with_command() -> None:
    wrapper = _build_capture_execute_cmd("echo hi", "/tmp/cap", inline_budget=64, max_capture_bytes=1024)

    assert "__BOG_AGENTS_CMD_" in wrapper
    assert "head -c 1024" in wrapper
    assert "-le 64" in wrapper
    # Every placeholder was substituted.
    assert "__COMMAND__" not in wrapper
    assert "__SENTINEL__" not in wrapper
    assert "__MAXBYTES__" not in wrapper


def test_capture_path_is_shell_quoted() -> None:
    wrapper = _build_capture_execute_cmd("cmd", "/tmp/a b/cap.txt", inline_budget=64)

    assert "__bog_f='/tmp/a b/cap.txt'" in wrapper


def test_parse_capture_output_rejects_non_integer_exit_code() -> None:
    result = _parse_capture_execute_output(f"{_EXECUTE_CAPTURE_SENTINEL} abc 0 0\nbody")

    assert result.offloaded is False
    assert result.response.output.startswith(_EXECUTE_CAPTURE_SENTINEL)


def test_parse_capture_output_propagates_backend_truncation() -> None:
    result = _parse_capture_execute_output(f"{_EXECUTE_CAPTURE_SENTINEL} 0 0 0\nbody", backend_truncated=True)

    assert result.response.truncated is True


async def test_aexecute_with_offload() -> None:
    sandbox = _OffloadSandbox(output=f"{_EXECUTE_CAPTURE_SENTINEL} 0 1 0\npreview")

    result = await sandbox.aexecute_with_offload("cmd", "/tmp/cap.txt", max_inline_bytes=10)

    assert result.offloaded is True
    assert result.response.output == "preview"


def test_offload_forwards_timeout() -> None:
    seen: dict[str, Any] = {}

    class _TimeoutSandbox(_OffloadSandbox):
        def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
            seen["timeout"] = timeout
            return super().execute(command, timeout=timeout)

    sandbox = _TimeoutSandbox(output=f"{_EXECUTE_CAPTURE_SENTINEL} 0 0 0\nok")
    sandbox.execute_with_offload("cmd", "/tmp/cap", max_inline_bytes=10, timeout=7)

    assert seen["timeout"] == 7
