"""ROADMAP #61: the opt-in `powershell` tool bundle and the WindowsApps alias guard."""

from __future__ import annotations

from pathlib import Path

import pytest

from bog_agents.backends.local_shell import dangerous_command_match
from bog_agents.tools import find_powershell, powershell_tool_bundle
from bog_agents.tools.powershell import is_windows_apps_alias, powershell_argv, run_powershell

_PS = find_powershell()


class TestDiscovery:
    def test_skips_windows_apps_alias_and_prefers_pwsh(self, tmp_path: Path) -> None:
        alias_dir = tmp_path / "Microsoft" / "WindowsApps"
        alias_dir.mkdir(parents=True)
        alias = alias_dir / "pwsh.exe"
        alias.write_bytes(b"")  # the Store alias is a zero-byte reparse point
        real = tmp_path / "PowerShell" / "7" / "pwsh.exe"
        real.parent.mkdir(parents=True)
        real.write_bytes(b"MZ")
        legacy = tmp_path / "powershell.exe"
        legacy.write_bytes(b"MZ")
        assert is_windows_apps_alias(alias)
        assert not is_windows_apps_alias(real)

        table = {"pwsh": str(alias), "powershell": str(legacy)}
        assert find_powershell(which=table.get) == str(legacy)
        table["pwsh"] = str(real)
        assert find_powershell(which=table.get) == str(real)
        assert find_powershell(which=lambda _name: None) is None

    def test_bundle_is_empty_without_powershell(self) -> None:
        assert powershell_tool_bundle(executable=None) == [] or _PS is not None

    def test_argv_never_goes_through_a_shell(self) -> None:
        argv = powershell_argv("pwsh", "Get-ChildItem | Select-Object -First 1")
        assert argv[0] == "pwsh" and argv[-1] == "Get-ChildItem | Select-Object -First 1"
        assert "-NoProfile" in argv and "-NonInteractive" in argv


class TestGate:
    def test_shared_dangerous_patterns_apply(self) -> None:
        assert dangerous_command_match("Remove-Item -Recurse -Force C:\\") is not None
        assert dangerous_command_match("Get-ChildItem") is None
        out = run_powershell("Remove-Item -Recurse -Force C:\\", executable="definitely-missing-pwsh")
        assert out.startswith("Error: dangerous command blocked")
        assert run_powershell("   ", executable="x").startswith("Error: command must be")
        assert run_powershell("Get-Date", executable="x", timeout=0).startswith("Error: timeout")

    def test_missing_executable_is_an_error_not_an_exception(self) -> None:
        out = run_powershell("Get-Date", executable="definitely-missing-pwsh-binary")
        assert out.startswith("Error: could not start PowerShell")


@pytest.mark.skipif(_PS is None, reason="no pwsh/powershell on PATH")
class TestRealPowerShell:
    def test_runs_and_reports_exit_code(self, tmp_path: Path) -> None:
        tools = powershell_tool_bundle(cwd=tmp_path)
        assert [t.name for t in tools] == ["powershell"]
        tool = tools[0]
        assert tool.func is not None
        out = tool.func(None, command="Write-Output 'hello from ps'")
        assert "hello from ps" in out
        failing = tool.func(None, command="exit 3")
        assert "[exit code 3]" in failing
        cwd_out = tool.func(None, command="(Get-Location).Path")
        assert str(tmp_path.name) in cwd_out

    def test_timeout_kills_the_process(self) -> None:
        assert _PS is not None
        out = run_powershell("Start-Sleep -Seconds 30", executable=_PS, timeout=1)
        assert out.startswith("Error: command timed out")
