"""P0-K regression — dangerous-command pattern coverage.

Lives in its own file so the module-level Windows skip on
``test_local_shell_backend.py`` doesn't suppress these. The
dangerous-command gate is pure-Python regex matching that fires
BEFORE ``subprocess.run``, so it is platform-independent.

REVIEW.md P0-K called out specific bypasses of the previous regex set
(``find … -delete``, ``rm --recursive --force``, ``shutil.rmtree`` via
``python -c``, ``git clean -fdx``, plus the Windows ``del /f /s /q`` /
``Remove-Item -Recurse -Force`` family). This file pins each one.
"""

from __future__ import annotations

import subprocess

import pytest

from bog_agents.backends.local_shell import LocalShellBackend


@pytest.fixture
def backend(tmp_path):
    return LocalShellBackend(root_dir=str(tmp_path))


@pytest.mark.parametrize(
    "command",
    [
        # rm bypasses noted in REVIEW.md
        "find / -delete",
        "find ~ -delete",
        "find /home/user -delete",
        "rm --recursive --force /tmp/x",
        "git clean -fdx /",
        "git clean -xfd",  # flag-order variant
        # python/shutil bypass
        "python -c 'import shutil; shutil.rmtree(\"/\")'",
        "python3 -c \"import shutil; shutil.rmtree('/home/user')\"",
        # ssh-key targeting (a common adversarial pattern)
        "python -c \"import os; os.unlink('/home/user/.ssh/id_rsa')\"",
        # Windows equivalents (raw strings — backslashes are literal)
        r"del /f /s /q C:\Users",
        r"del /F /S /Q %USERPROFILE%",
        r"rmdir /s C:\Windows",
        r"format c: /q",
        r"cipher /w:C:\Users\me",
        r"Remove-Item -Recurse -Force C:\Users",
        r"Remove-Item -Force -Recurse $env:USERPROFILE",
        "Clear-Disk -Number 0 -RemoveData",
    ],
)
def test_pattern_trips_gate(backend, command):
    with pytest.raises(PermissionError) as exc_info:
        backend.execute(command)
    assert "Dangerous command blocked" in str(exc_info.value)


@pytest.mark.parametrize(
    "command",
    [
        "ls -la",
        "echo hello",
        "git status",
        "rm tmp/single_file.txt",  # rm without -r and not targeting /
        "find . -name '*.py'",  # find without -delete
        "git clean -n",  # dry-run only
    ],
)
def test_benign_commands_pass(backend, command, monkeypatch):
    """Confirm the gate doesn't reject ordinary commands.

    Stubs ``subprocess.run`` so this stays offline and platform-independent.
    """

    class _Res:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *_a, **_kw: _Res())
    backend.execute(command)  # would raise PermissionError if the gate fired
