"""Unit tests for bog_agents_harbor.backend (HarborSandbox)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
# Sync methods raise NotImplementedError
# ---------------------------------------------------------------------------


def test_execute_raises() -> None:
    _, sandbox = _make_env()
    with pytest.raises(NotImplementedError):
        sandbox.execute("ls")


def test_read_raises() -> None:
    _, sandbox = _make_env()
    with pytest.raises(NotImplementedError):
        sandbox.read("file.txt")


def test_write_raises() -> None:
    _, sandbox = _make_env()
    with pytest.raises(NotImplementedError):
        sandbox.write("file.txt", "content")


def test_edit_raises() -> None:
    _, sandbox = _make_env()
    with pytest.raises(NotImplementedError):
        sandbox.edit("file.txt", "old", "new")


def test_ls_info_raises() -> None:
    _, sandbox = _make_env()
    with pytest.raises(NotImplementedError):
        sandbox.ls_info(".")


def test_grep_raw_raises() -> None:
    _, sandbox = _make_env()
    with pytest.raises(NotImplementedError):
        sandbox.grep_raw("pattern")


def test_glob_info_raises() -> None:
    _, sandbox = _make_env()
    with pytest.raises(NotImplementedError):
        sandbox.glob_info("*.py")


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
# aread
# ---------------------------------------------------------------------------


class TestARead:
    async def test_reads_file(self) -> None:
        env, sandbox = _make_env()
        env.exec.return_value = _make_exec_result(stdout="     1\thello\n     2\tworld")
        content = await sandbox.aread("file.txt")
        assert "hello" in content
        assert "world" in content

    async def test_file_not_found(self) -> None:
        env, sandbox = _make_env()
        env.exec.return_value = _make_exec_result(stdout="Error: File not found", return_code=1)
        content = await sandbox.aread("missing.txt")
        assert "not found" in content.lower()

    async def test_nonzero_exit_code(self) -> None:
        env, sandbox = _make_env()
        env.exec.return_value = _make_exec_result(return_code=1)
        content = await sandbox.aread("file.txt")
        assert "not found" in content.lower()


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
# als_info
# ---------------------------------------------------------------------------


class TestAlsInfo:
    async def test_lists_directory(self) -> None:
        env, sandbox = _make_env()
        env.exec.return_value = _make_exec_result(stdout="file.py|false\nsubdir|true\n")
        infos = await sandbox.als_info(".")
        assert len(infos) == 2
        assert any(i["path"] == "file.py" and not i["is_dir"] for i in infos)
        assert any(i["path"] == "subdir" and i["is_dir"] for i in infos)

    async def test_nonzero_exit_returns_empty(self) -> None:
        env, sandbox = _make_env()
        env.exec.return_value = _make_exec_result(return_code=1)
        infos = await sandbox.als_info("/nonexistent")
        assert infos == []

    async def test_empty_dir_returns_empty(self) -> None:
        env, sandbox = _make_env()
        env.exec.return_value = _make_exec_result(stdout="", return_code=0)
        infos = await sandbox.als_info(".")
        assert infos == []

    async def test_malformed_lines_skipped(self) -> None:
        env, sandbox = _make_env()
        env.exec.return_value = _make_exec_result(
            stdout="good_file|false\nbad_line_no_pipe\nanother|true\n"
        )
        infos = await sandbox.als_info(".")
        assert len(infos) == 2


# ---------------------------------------------------------------------------
# agrep_raw
# ---------------------------------------------------------------------------


class TestAgrepRaw:
    async def test_returns_matches(self) -> None:
        env, sandbox = _make_env()
        env.exec.return_value = _make_exec_result(stdout="src/main.py:10:def hello():\n")
        matches = await sandbox.agrep_raw("hello", "src/")
        assert isinstance(matches, list)
        assert len(matches) == 1
        assert matches[0]["path"] == "src/main.py"
        assert matches[0]["line"] == 10
        assert "hello" in matches[0]["text"]

    async def test_no_matches_returns_empty_list(self) -> None:
        env, sandbox = _make_env()
        env.exec.return_value = _make_exec_result(stdout="")
        matches = await sandbox.agrep_raw("nonexistent")
        assert matches == []

    async def test_glob_pattern_in_command(self) -> None:
        env, sandbox = _make_env()
        env.exec.return_value = _make_exec_result(stdout="")
        await sandbox.agrep_raw("pattern", glob="*.py")
        cmd = env.exec.call_args[0][0]
        assert "*.py" in cmd

    async def test_malformed_lines_skipped(self) -> None:
        env, sandbox = _make_env()
        env.exec.return_value = _make_exec_result(stdout="good:1:text\nbad_no_colons\n")
        matches = await sandbox.agrep_raw("text")
        assert len(matches) == 1


# ---------------------------------------------------------------------------
# aglob_info
# ---------------------------------------------------------------------------


class TestAglobInfo:
    async def test_finds_files(self) -> None:
        env, sandbox = _make_env()
        env.exec.return_value = _make_exec_result(stdout="main.py|false\nutils.py|false\n")
        infos = await sandbox.aglob_info("*.py")
        assert len(infos) == 2
        assert all(not i["is_dir"] for i in infos)

    async def test_nonzero_exit_returns_empty(self) -> None:
        env, sandbox = _make_env()
        env.exec.return_value = _make_exec_result(return_code=1)
        infos = await sandbox.aglob_info("*.xyz", path="/no/such/dir")
        assert infos == []

    async def test_empty_output_returns_empty(self) -> None:
        env, sandbox = _make_env()
        env.exec.return_value = _make_exec_result(stdout="", return_code=0)
        infos = await sandbox.aglob_info("*.py")
        assert infos == []
