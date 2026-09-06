"""`daemon install` on Windows registers a Task Scheduler task (v6 DMN-3)."""

from __future__ import annotations

import subprocess

from bog_agents_daemon.install import WINDOWS_TASK_NAME, generate_windows_task_command, install_windows_task

EXE = "C:/tools/bog-agents-daemon.exe"


def test_command_registers_logon_task_without_a_shell() -> None:
    argv = generate_windows_task_command(EXE)
    assert argv[0] == "schtasks"
    assert argv[argv.index("/TN") + 1] == WINDOWS_TASK_NAME
    assert argv[argv.index("/SC") + 1] == "ONLOGON"
    assert argv[argv.index("/TR") + 1] == f'"{EXE}" start'
    assert "/F" in argv


def test_install_reports_success_and_management_commands() -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="SUCCESS", stderr="")

    text = install_windows_task(EXE, runner=fake_run)
    assert calls and calls[0][0] == "schtasks"
    assert "registered" in text and "schtasks /Delete /TN BogAgentsDaemon /F" in text


def test_install_reports_failure_with_manual_command() -> None:
    def fake_run(argv: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="ERROR: Access is denied.")

    text = install_windows_task(EXE, runner=fake_run)
    assert "failed" in text and "Access is denied" in text and "/Create" in text


def test_install_reports_missing_schtasks() -> None:
    def fake_run(argv: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("schtasks")

    text = install_windows_task(EXE, runner=fake_run)
    assert "Could not run schtasks" in text
