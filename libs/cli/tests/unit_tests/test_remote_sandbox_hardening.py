"""Hardening tests for the SSH-backed remote sandbox helpers (S31).

A wedged/prompting ssh must not deadlock the CLI: `run_remote_python` bounds
the exchange with a timeout and `ssh_base_args` enforces a non-interactive
posture (`BatchMode=yes`) plus a connect ceiling (`ConnectTimeout`).
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from bog_agents_cli.remote_sandbox import run_remote_python, ssh_base_args


class _HangingProcess:
    """A fake subprocess whose communicate() never returns until killed."""

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.killed = False
        self.waited = False

    async def communicate(self, _input: bytes | None = None) -> tuple[bytes, bytes]:
        # Sleep far longer than the test timeout so wait_for must fire.
        await asyncio.sleep(60)
        return b"", b""

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        self.waited = True
        return 1


class _OkProcess:
    """A fake subprocess that returns immediately with success output."""

    def __init__(self) -> None:
        self.returncode = 0

    async def communicate(self, _input: bytes | None = None) -> tuple[bytes, bytes]:
        return b'{"ok": true}\n', b""


class TestSshBaseArgsHardening:
    """`ssh_base_args` should enforce a non-interactive ssh posture."""

    def test_includes_batchmode_and_connect_timeout(self) -> None:
        """BatchMode and ConnectTimeout are always prepended."""
        args = ssh_base_args(port=22, identity_file="", ssh_options=[])
        assert "BatchMode=yes" in args
        assert any(opt.startswith("ConnectTimeout=") for opt in args)

    def test_connect_timeout_is_configurable_and_omittable(self) -> None:
        """A falsy connect_timeout drops the ConnectTimeout option."""
        args = ssh_base_args(
            port=0, identity_file="", ssh_options=[], connect_timeout=0
        )
        assert "BatchMode=yes" in args
        assert not any(opt.startswith("ConnectTimeout=") for opt in args)

    def test_user_options_are_appended_after_defaults(self) -> None:
        """Caller-supplied options follow the hardened defaults."""
        args = ssh_base_args(
            port=2222,
            identity_file="~/.ssh/id_ed25519",
            ssh_options=["StrictHostKeyChecking=accept-new"],
        )
        assert "StrictHostKeyChecking=accept-new" in args
        assert args.index("StrictHostKeyChecking=accept-new") > args.index(
            "BatchMode=yes"
        )


class TestRunRemotePythonTimeout:
    """`run_remote_python` must not block forever on a wedged ssh."""

    async def test_timeout_kills_process_and_reports_failure(self) -> None:
        """A hanging ssh is killed and surfaced as a non-zero failure."""
        hanging = _HangingProcess()

        async def _fake_create_subprocess_exec(
            *_args: object, **_kwargs: object
        ) -> _HangingProcess:
            return hanging

        with (
            patch(
                "bog_agents_cli.remote_sandbox.asyncio.create_subprocess_exec",
                _fake_create_subprocess_exec,
            ),
            patch(
                "bog_agents_cli.remote_sandbox.asset_text",
                return_value="print('hi')",
            ),
        ):
            rc, stdout, stderr = await run_remote_python(
                host="sandbox.example.com",
                user="bog",
                port=22,
                identity_file="",
                ssh_options=[],
                python_command="python3",
                script_name="ssh_status.py",
                args=[],
                timeout=0.05,
            )

        assert rc == 1
        assert stdout == ""
        assert stderr == "SSH command timed out"
        assert hanging.killed is True
        assert hanging.waited is True

    async def test_success_path_returns_output(self) -> None:
        """A prompt ssh response is decoded and returned unchanged."""
        ok = _OkProcess()

        async def _fake_create_subprocess_exec(
            *_args: object, **_kwargs: object
        ) -> _OkProcess:
            return ok

        with (
            patch(
                "bog_agents_cli.remote_sandbox.asyncio.create_subprocess_exec",
                _fake_create_subprocess_exec,
            ),
            patch(
                "bog_agents_cli.remote_sandbox.asset_text",
                return_value="print('hi')",
            ),
        ):
            rc, stdout, stderr = await run_remote_python(
                host="sandbox.example.com",
                user="bog",
                port=22,
                identity_file="",
                ssh_options=[],
                python_command="python3",
                script_name="ssh_status.py",
                args=[],
            )

        assert rc == 0
        assert stdout == '{"ok": true}'
        assert stderr == ""
