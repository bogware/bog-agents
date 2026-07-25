"""Unit tests for bog_agents_harbor.backend (HarborSandbox)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bog_agents.backends.protocol import (
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
)

from bog_agents_harbor.backend import (
    _EXIT_DECODE_FAILED,
    _EXIT_FILE_MISSING,
    _EXIT_MULTIPLE_MATCHES,
    _EXIT_NOT_FOUND,
    DEFAULT_COMMAND_TIMEOUT_SEC,
    HarborSandbox,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_exec_result(stdout: str = "", stderr: str = "", return_code: int = 0) -> MagicMock:
    """Build a mock Harbor exec result."""
    result = MagicMock()
    result.stdout = stdout
    result.stderr = stderr
    result.return_code = return_code
    return result


def _make_env() -> tuple[MagicMock, HarborSandbox]:
    """Return a mock BaseEnvironment and a HarborSandbox wrapping it."""
    env = MagicMock()
    env.session_id = "test-session-id"
    env.exec = AsyncMock()
    sandbox = HarborSandbox(env)
    return env, sandbox


# ---------------------------------------------------------------------------
# id property
# ---------------------------------------------------------------------------


def test_id_returns_session_id() -> None:
    _env, sandbox = _make_env()
    assert sandbox.id == "test-session-id"


# ---------------------------------------------------------------------------
# Sync methods raise NotImplementedError (HarborSandbox is async-only; the
# BaseSandbox sync surface routes through the raising sync `execute`).
# ---------------------------------------------------------------------------


def test_sync_execute_raises() -> None:
    _, sandbox = _make_env()
    with pytest.raises(NotImplementedError):
        sandbox.execute("ls")


def test_sync_read_file_raises() -> None:
    _, sandbox = _make_env()
    with pytest.raises(NotImplementedError):
        sandbox.read_file("file.txt")


def test_sync_write_raises() -> None:
    _, sandbox = _make_env()
    with pytest.raises(NotImplementedError):
        sandbox.write("file.txt", "content")


def test_sync_ls_raises() -> None:
    _, sandbox = _make_env()
    with pytest.raises(NotImplementedError):
        sandbox.ls(".")


def test_sync_grep_raises() -> None:
    _, sandbox = _make_env()
    with pytest.raises(NotImplementedError):
        sandbox.grep("pattern")


def test_sync_glob_raises() -> None:
    _, sandbox = _make_env()
    with pytest.raises(NotImplementedError):
        sandbox.glob("*.py")


def test_upload_files_raises() -> None:
    _, sandbox = _make_env()
    with pytest.raises(NotImplementedError):
        sandbox.upload_files([("f.txt", b"x")])


# ---------------------------------------------------------------------------
# aexecute
# ---------------------------------------------------------------------------


class TestAExecute:
    async def test_basic_success(self) -> None:
        env, sandbox = _make_env()
        env.exec.return_value = _make_exec_result(stdout="hello\n")
        result = await sandbox.aexecute("echo hello")
        assert result.output == "hello"
        assert result.exit_code == 0

    async def test_stderr_appended(self) -> None:
        env, sandbox = _make_env()
        env.exec.return_value = _make_exec_result(stdout="out", stderr="err")
        result = await sandbox.aexecute("cmd")
        assert "stderr" in result.output
        assert "err" in result.output

    async def test_bash_artifacts_filtered(self) -> None:
        env, sandbox = _make_env()
        artifact = "bash: cannot set terminal process group (-1): Inappropriate ioctl for device"
        env.exec.return_value = _make_exec_result(stdout=f"real output\n{artifact}", stderr="")
        result = await sandbox.aexecute("cmd")
        # Artifact is moved from stdout to stderr section — stdout prefix stays clean
        stdout_part = result.output.split(" stderr: ")[0]
        assert artifact not in stdout_part
        assert "real output" in result.output

    async def test_timeout_returns_error_response(self) -> None:
        env, sandbox = _make_env()
        env.exec.side_effect = TimeoutError()

        with patch("asyncio.wait_for", side_effect=TimeoutError()):
            result = await sandbox.aexecute("sleep 999", timeout=1)

        assert result.exit_code == 124
        assert "timed out" in result.output.lower()

    async def test_zero_exit_code_no_timeout(self) -> None:
        env, sandbox = _make_env()
        env.exec.return_value = _make_exec_result(return_code=0)
        result = await sandbox.aexecute("cmd", timeout=0)
        assert result.exit_code == 0

    async def test_default_timeout_used(self) -> None:
        env, sandbox = _make_env()
        env.exec.return_value = _make_exec_result()
        with patch("asyncio.wait_for", new_callable=AsyncMock) as mock_wait:
            mock_wait.return_value = _make_exec_result()
            await sandbox.aexecute("cmd")
            _, kwargs = mock_wait.call_args
            assert kwargs.get("timeout") == DEFAULT_COMMAND_TIMEOUT_SEC


# ---------------------------------------------------------------------------
# awrite
# ---------------------------------------------------------------------------


class TestAWrite:
    async def test_successful_write(self) -> None:
        env, sandbox = _make_env()
        env.exec.return_value = _make_exec_result(return_code=0)
        result = await sandbox.awrite("new_file.txt", "content")
        assert result.path == "new_file.txt"
        assert result.error is None

    async def test_write_returns_error_on_failure(self) -> None:
        env, sandbox = _make_env()
        env.exec.return_value = _make_exec_result(
            stdout="Error: File already exists", return_code=1
        )
        result = await sandbox.awrite("existing.txt", "content")
        assert result.error is not None
        assert "Error" in result.error

    async def test_base64_used(self) -> None:
        """awrite should encode content as base64 to avoid shell escaping."""
        env, sandbox = _make_env()
        env.exec.return_value = _make_exec_result(return_code=0)
        await sandbox.awrite("file.txt", "hello $world\n`date`")
        # We can't assert the exact command, but exec must have been called once
        assert env.exec.call_count == 1
        cmd = env.exec.call_args[0][0]
        assert "base64" in cmd


# ---------------------------------------------------------------------------
# aedit
# ---------------------------------------------------------------------------


class TestAEdit:
    async def test_successful_edit(self) -> None:
        env, sandbox = _make_env()
        env.exec.return_value = _make_exec_result(stdout="1", return_code=0)
        result = await sandbox.aedit("file.txt", "old", "new")
        assert result.path == "file.txt"
        assert result.error is None

    async def test_not_found(self) -> None:
        env, sandbox = _make_env()
        env.exec.return_value = _make_exec_result(return_code=_EXIT_NOT_FOUND)
        result = await sandbox.aedit("file.txt", "old", "new")
        assert result.error is not None
        assert "not found" in result.error.lower()

    async def test_multiple_matches(self) -> None:
        env, sandbox = _make_env()
        env.exec.return_value = _make_exec_result(return_code=_EXIT_MULTIPLE_MATCHES)
        result = await sandbox.aedit("file.txt", "old", "new")
        assert result.error is not None
        assert "multiple" in result.error.lower()

    async def test_file_missing(self) -> None:
        env, sandbox = _make_env()
        env.exec.return_value = _make_exec_result(return_code=_EXIT_FILE_MISSING)
        result = await sandbox.aedit("missing.txt", "old", "new")
        assert result.error is not None
        assert "not found" in result.error.lower()

    async def test_decode_failed(self) -> None:
        env, sandbox = _make_env()
        env.exec.return_value = _make_exec_result(
            stdout="decode error", return_code=_EXIT_DECODE_FAILED
        )
        result = await sandbox.aedit("file.txt", "old", "new")
        assert result.error is not None
        assert "decode" in result.error.lower()

    async def test_replace_all_flag(self) -> None:
        env, sandbox = _make_env()
        env.exec.return_value = _make_exec_result(stdout="3", return_code=0)
        result = await sandbox.aedit("file.txt", "old", "new", replace_all=True)
        assert result.occurrences == 3
        cmd = env.exec.call_args[0][0]
        assert "true" in cmd.lower()


# ---------------------------------------------------------------------------
# Structured listing/read/search surface (SAT-1)
#
# HarborSandbox inherits als / aread_file / agrep / aglob / adelete from
# BaseSandbox, which derives each from `aexecute()`. Before the rebase these
# raised NotImplementedError (HarborSandbox only overrode the *deprecated*
# als_info/agrep_raw/aglob_info names), so every eval run had broken ls/grep/glob
# tools. These tests pin that the structured methods now delegate to the Harbor
# environment and return the structured result types. End-to-end parsing of real
# shell output is covered by SandboxConformanceSuite against a real shell.
# ---------------------------------------------------------------------------


class TestStructuredSurfaceDelegatesToExec:
    async def test_als_delegates_and_returns_lsresult(self) -> None:
        env, sandbox = _make_env()
        env.exec.return_value = _make_exec_result(stdout="", return_code=0)
        result = await sandbox.als("/app")
        assert isinstance(result, LsResult)
        assert env.exec.await_count == 1

    async def test_aread_file_delegates_and_returns_readresult(self) -> None:
        env, sandbox = _make_env()
        env.exec.return_value = _make_exec_result(stdout="", return_code=0)
        result = await sandbox.aread_file("/app/x.txt")
        assert isinstance(result, ReadResult)
        assert env.exec.await_count >= 1

    async def test_agrep_delegates_and_returns_grepresult(self) -> None:
        env, sandbox = _make_env()
        env.exec.return_value = _make_exec_result(stdout="", return_code=0)
        result = await sandbox.agrep("hello", path="/app")
        assert isinstance(result, GrepResult)
        assert env.exec.await_count >= 1

    async def test_aglob_delegates_and_returns_globresult(self) -> None:
        env, sandbox = _make_env()
        env.exec.return_value = _make_exec_result(stdout="", return_code=0)
        result = await sandbox.aglob("*.py", path="/app")
        assert isinstance(result, GlobResult)
        assert env.exec.await_count >= 1

    async def test_structured_methods_do_not_raise_not_implemented(self) -> None:
        # The exact regression: none of these may raise NotImplementedError.
        env, sandbox = _make_env()
        env.exec.return_value = _make_exec_result(stdout="", return_code=0)
        await sandbox.als("/app")
        await sandbox.aread_file("/app/x.txt")
        await sandbox.agrep("x", path="/app")
        await sandbox.aglob("*", path="/app")
