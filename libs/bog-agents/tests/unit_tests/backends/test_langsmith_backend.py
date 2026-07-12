"""Tests for `LangSmithSandbox`.

The LangSmith SDK is stubbed with a fake `Sandbox` object: the backend only ever
calls `run`, `read`, `write`, and `name` on it. The real
`langsmith.sandbox` exception classes are used, so the `except` clauses are
exercised for real rather than against a look-alike.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest
from langsmith.sandbox import ResourceNotFoundError, SandboxClientError

from bog_agents.backends.langsmith import LangSmithSandbox
from bog_agents.backends.sandbox import MAX_BINARY_BYTES, MAX_OUTPUT_BYTES, TRUNCATION_MSG


class _RunResult:
    def __init__(self, stdout: str = "", stderr: str = "", exit_code: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code


class FakeSandbox:
    """Stand-in for `langsmith.sandbox.Sandbox`."""

    def __init__(self) -> None:
        self.name = "fake-sandbox"
        self.files: dict[str, bytes] = {}
        self.commands: list[tuple[str, int | None]] = []
        self.run_result = _RunResult()
        self.read_error: Exception | None = None
        self.write_error: Exception | None = None

    def run(self, command: str, timeout: int | None = None) -> _RunResult:
        self.commands.append((command, timeout))
        return self.run_result

    def read(self, path: str) -> bytes:
        if self.read_error is not None:
            raise self.read_error
        if path not in self.files:
            msg = f"no such file: {path}"
            raise ResourceNotFoundError(msg)
        return self.files[path]

    def write(self, path: str, content: bytes) -> None:
        if self.write_error is not None:
            raise self.write_error
        self.files[path] = content


@pytest.fixture
def sandbox() -> FakeSandbox:
    return FakeSandbox()


@pytest.fixture
def backend(sandbox: FakeSandbox) -> LangSmithSandbox:
    return LangSmithSandbox(sandbox)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# basics
# ---------------------------------------------------------------------------


def test_id_is_sandbox_name(backend: LangSmithSandbox) -> None:
    assert backend.id == "fake-sandbox"


def test_capture_offload_is_enabled(backend: LangSmithSandbox) -> None:
    """LangSmith images ship the shell/coreutils the capture wrapper needs."""
    assert backend.enable_capture_offload is True


def test_execute_combines_stdout_and_stderr(backend: LangSmithSandbox, sandbox: FakeSandbox) -> None:
    sandbox.run_result = _RunResult(stdout="out", stderr="err", exit_code=1)

    result = backend.execute("ls", timeout=5)

    assert result.output == "out\nerr"
    assert result.exit_code == 1
    assert sandbox.commands == [("ls", 5)]


def test_execute_uses_default_timeout(backend: LangSmithSandbox, sandbox: FakeSandbox) -> None:
    backend.execute("ls")

    assert sandbox.commands[0][1] == 30 * 60


def test_execute_stderr_only(backend: LangSmithSandbox, sandbox: FakeSandbox) -> None:
    sandbox.run_result = _RunResult(stdout="", stderr="boom", exit_code=2)

    assert backend.execute("ls").output == "boom"


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


def test_write_uses_the_sdk_not_the_shell(backend: LangSmithSandbox, sandbox: FakeSandbox) -> None:
    content = "x" * 500_000

    result = backend.write("/workspace/big.txt", content)

    assert result.error is None
    assert result.path == "/workspace/big.txt"
    assert sandbox.files["/workspace/big.txt"] == content.encode()
    # Only the preflight (mkdir) went through the shell — content never did.
    assert len(sandbox.commands) == 1
    assert "os.makedirs" in sandbox.commands[0][0]
    assert all(content not in cmd for cmd, _ in sandbox.commands)


def test_write_overwrites_existing_file(backend: LangSmithSandbox, sandbox: FakeSandbox) -> None:
    backend.write("/f.txt", "one")
    result = backend.write("/f.txt", "two")

    assert result.error is None
    assert sandbox.files["/f.txt"] == b"two"


def test_write_reports_sdk_error(backend: LangSmithSandbox, sandbox: FakeSandbox) -> None:
    sandbox.write_error = SandboxClientError("disk full")

    result = backend.write("/f.txt", "x")

    assert result.path is None
    assert "Failed to write file '/f.txt'" in (result.error or "")


def test_write_preflight_failure_skips_the_write(backend: LangSmithSandbox, sandbox: FakeSandbox) -> None:
    sandbox.run_result = _RunResult(stdout="Error: Permission denied", exit_code=1)

    result = backend.write("/root/f.txt", "x")

    assert result.error == "Error: Permission denied"
    assert sandbox.files == {}


async def test_awrite(backend: LangSmithSandbox, sandbox: FakeSandbox) -> None:
    result = await backend.awrite("/f.txt", "hello")

    assert result.path == "/f.txt"
    assert sandbox.files["/f.txt"] == b"hello"


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


def test_read_file_paginates(backend: LangSmithSandbox, sandbox: FakeSandbox) -> None:
    sandbox.files["/f.txt"] = b"a\nb\nc\nd\ne\n"

    result = backend.read_file("/f.txt", offset=1, limit=2)

    assert result.error is None
    assert result.file_data == {"content": "b\nc", "encoding": "utf-8"}


def test_read_file_normalizes_crlf(backend: LangSmithSandbox, sandbox: FakeSandbox) -> None:
    r"""A CRLF file must come back LF-only, or `edit`'s old_string never matches."""
    sandbox.files["/f.txt"] = b"line1\r\nline2\r\n"

    result = backend.read_file("/f.txt")

    assert result.file_data is not None
    assert result.file_data["content"] == "line1\nline2"
    assert "\r" not in result.file_data["content"]


def test_read_file_normalizes_bare_cr(backend: LangSmithSandbox, sandbox: FakeSandbox) -> None:
    sandbox.files["/f.txt"] = b"line1\rline2"

    result = backend.read_file("/f.txt")

    assert result.file_data is not None
    assert result.file_data["content"] == "line1\nline2"


def test_read_file_empty(backend: LangSmithSandbox, sandbox: FakeSandbox) -> None:
    sandbox.files["/f.txt"] = b""

    result = backend.read_file("/f.txt")

    assert result.error is None
    assert result.file_data is not None
    assert result.file_data["content"] == ""
    # The protocol's rendered `read` turns empty content into the reminder.
    assert backend.read("/f.txt") == "System reminder: File exists but has empty contents"


def test_read_file_missing(backend: LangSmithSandbox) -> None:
    result = backend.read_file("/nope.txt")

    assert result.file_data is None
    assert result.error == "File '/nope.txt': file_not_found"


def test_read_file_sdk_error(backend: LangSmithSandbox, sandbox: FakeSandbox) -> None:
    sandbox.read_error = SandboxClientError("upstream is down")

    result = backend.read_file("/f.txt")

    assert result.file_data is None
    assert "SandboxClientError" in (result.error or "")


def test_read_file_offset_beyond_eof(backend: LangSmithSandbox, sandbox: FakeSandbox) -> None:
    sandbox.files["/f.txt"] = b"a\nb\n"

    result = backend.read_file("/f.txt", offset=10)

    assert result.file_data is None
    assert result.error == "File '/f.txt': Line offset 10 exceeds file length (2 lines)"


def test_read_file_binary_by_extension(backend: LangSmithSandbox, sandbox: FakeSandbox) -> None:
    raw = b"\x89PNG\r\n\x1a\n\x00\x01"
    sandbox.files["/img.png"] = raw

    result = backend.read_file("/img.png")

    assert result.file_data is not None
    assert result.file_data["encoding"] == "base64"
    assert base64.b64decode(result.file_data["content"]) == raw


def test_read_file_text_extension_with_invalid_utf8_falls_back_to_base64(backend: LangSmithSandbox, sandbox: FakeSandbox) -> None:
    raw = b"\xff\xfe\x00binary-in-a-txt"
    sandbox.files["/f.txt"] = raw

    result = backend.read_file("/f.txt")

    assert result.file_data is not None
    assert result.file_data["encoding"] == "base64"
    assert base64.b64decode(result.file_data["content"]) == raw


def test_read_file_binary_over_cap(backend: LangSmithSandbox, sandbox: FakeSandbox) -> None:
    sandbox.files["/img.png"] = b"\x00" * (MAX_BINARY_BYTES + 1)

    result = backend.read_file("/img.png")

    assert result.file_data is None
    assert result.error == f"File '/img.png': Binary file exceeds maximum preview size of {MAX_BINARY_BYTES} bytes"


def test_read_file_text_over_cap_is_truncated(backend: LangSmithSandbox, sandbox: FakeSandbox) -> None:
    sandbox.files["/f.txt"] = b"z" * (MAX_OUTPUT_BYTES + 10_000)

    result = backend.read_file("/f.txt")

    assert result.file_data is not None
    content = result.file_data["content"]
    assert content.endswith(TRUNCATION_MSG)
    assert len(content.encode("utf-8")) <= MAX_OUTPUT_BYTES


def test_read_file_mkv_routes_as_binary(backend: LangSmithSandbox, sandbox: FakeSandbox) -> None:
    """`.mkv` is absent from the extension map but must never be text-decoded."""
    sandbox.files["/clip.mkv"] = b"\x1a\x45\xdf\xa3video"

    result = backend.read_file("/clip.mkv")

    assert result.file_data is not None
    assert result.file_data["encoding"] == "base64"


async def test_aread_file_uses_the_sdk_transport(backend: LangSmithSandbox, sandbox: FakeSandbox) -> None:
    """The async read must not fall back to the base class's shell script."""
    sandbox.files["/f.txt"] = b"a\nb\n"

    result = await backend.aread_file("/f.txt", offset=1, limit=1)

    assert result.file_data == {"content": "b", "encoding": "utf-8"}
    assert sandbox.commands == []


# ---------------------------------------------------------------------------
# bulk transfer
# ---------------------------------------------------------------------------


def test_download_files_partial_success(backend: LangSmithSandbox, sandbox: FakeSandbox) -> None:
    sandbox.files["/a.txt"] = b"data"

    responses = backend.download_files(["/a.txt", "/missing.txt", "relative.txt"])

    assert [r.error for r in responses] == [None, "file_not_found", "invalid_path"]
    assert responses[0].content == b"data"


def test_download_files_maps_is_directory(backend: LangSmithSandbox, sandbox: FakeSandbox) -> None:
    sandbox.read_error = SandboxClientError("/a is a directory")

    responses = backend.download_files(["/a"])

    assert responses[0].error == "is_directory"


def test_upload_files_partial_success(backend: LangSmithSandbox, sandbox: FakeSandbox) -> None:
    responses = backend.upload_files([("/a.txt", b"one"), ("relative.txt", b"two")])

    assert [r.error for r in responses] == [None, "invalid_path"]
    assert sandbox.files == {"/a.txt": b"one"}


def test_upload_files_maps_sdk_error(backend: LangSmithSandbox, sandbox: FakeSandbox) -> None:
    sandbox.write_error = SandboxClientError("nope")

    responses = backend.upload_files([("/a.txt", b"one")])

    assert responses[0].error == "permission_denied"


def test_module_does_not_import_the_sandbox_sdk_at_module_scope() -> None:
    """`langsmith.sandbox` (and its HTTP stack) must stay unloaded until a method runs.

    `langsmith` itself is pulled in by langchain regardless, so the contract this
    guards is narrower: the `langsmith.sandbox` submodule — the optional, heavy
    piece — must only load when a `LangSmithSandbox` method is actually called.
    """
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent(
        """
        import sys

        import bog_agents.backends.langsmith  # noqa: F401

        print('langsmith.sandbox' in sys.modules)
        """
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
    loaded: Any = result.stdout.strip()

    assert loaded == "False", "importing bog_agents.backends.langsmith eagerly pulled langsmith.sandbox"


def test_inherits_base_sandbox_edit(backend: LangSmithSandbox, sandbox: FakeSandbox) -> None:
    """Edit is not overridden — it runs the base class's server-side script."""
    sandbox.run_result = _RunResult(stdout='{"count": 1}', exit_code=0)

    result = backend.edit("/f.txt", "old", "new")

    assert result.occurrences == 1
    assert "__BOG_AGENTS_EDIT_EOF__" in sandbox.commands[0][0]
