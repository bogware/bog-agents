"""ROADMAP #61 (CLI): opt-in powershell tool wiring, shell classification, doctor guards."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from bog_agents.tools.powershell import find_powershell

from bog_agents_cli import doctor_deep
from bog_agents_cli.auto_mode import never_allow_entry_for
from bog_agents_cli.config import SHELL_TOOL_NAMES
from bog_agents_cli.config_manifest import resolve_option


def test_powershell_is_a_shell_tool_everywhere() -> None:
    assert "powershell" in SHELL_TOOL_NAMES
    entry = never_allow_entry_for("powershell", {"command": "Get-Date"})
    assert (
        entry.startswith("execute: ") and "Get\\-Date" in entry
    ) or "Get-Date" in entry


def test_manifest_option_defaults_off_and_reads_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bog_agents_cli.config_manifest.load_config_toml", dict)
    monkeypatch.delenv("BOG_AGENTS_POWERSHELL_TOOL", raising=False)
    assert resolve_option("tools.powershell") is False
    monkeypatch.setenv("BOG_AGENTS_POWERSHELL_TOOL", "true")
    assert resolve_option("tools.powershell") is True


def test_doctor_probe_flags_the_store_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alias_dir = tmp_path / "Microsoft" / "WindowsApps"
    alias_dir.mkdir(parents=True)
    alias = alias_dir / "pwsh.exe"
    alias.write_bytes(b"")
    monkeypatch.setattr(
        "bog_agents.tools.powershell.find_powershell", lambda *a, **k: None
    )
    monkeypatch.setattr(
        shutil, "which", lambda name: str(alias) if name == "pwsh" else None
    )
    probe = doctor_deep._probe_powershell()
    assert probe.status == "warn" and "execution alias" in probe.detail

    monkeypatch.setattr(
        "bog_agents.tools.powershell.find_powershell",
        lambda *a, **k: "C:/Program Files/PowerShell/7/pwsh.exe",
    )
    probe = doctor_deep._probe_powershell()
    assert probe.status == "ok" and probe.detail.endswith("pwsh.exe")


@pytest.mark.skipif(find_powershell() is None, reason="no pwsh/powershell on PATH")
def test_cli_agent_registers_powershell_only_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bog_agents_cli.tokens_audit_controller import audit_cli_agent

    monkeypatch.setattr("bog_agents_cli.config_manifest.load_config_toml", dict)
    monkeypatch.delenv("BOG_AGENTS_POWERSHELL_TOOL", raising=False)
    off = audit_cli_agent(harness_profile=None, cwd=tmp_path, method="approx")
    assert "powershell" not in {t.name for t in off.tools}
    monkeypatch.setenv("BOG_AGENTS_POWERSHELL_TOOL", "1")
    on = audit_cli_agent(harness_profile=None, cwd=tmp_path, method="approx")
    assert "powershell" in {t.name for t in on.tools}
