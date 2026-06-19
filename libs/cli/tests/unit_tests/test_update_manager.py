"""Tests for the resilient self-update core (`update_manager`)."""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

from bog_agents_cli import update_manager as um
from bog_agents_cli.update_manager import (
    InstallMethod,
    PackageStatus,
    SuiteStatus,
    UpdatePlan,
    UpgradeOutcome,
)


class TestDetectInstallMethod:
    def test_editable_checkout(self, monkeypatch) -> None:
        monkeypatch.setattr(
            um, "_is_editable_install", lambda name=um.CLI_PACKAGE: True
        )
        assert um.detect_install_method() == InstallMethod.EDITABLE

    def test_uv_tool_path(self, monkeypatch) -> None:
        monkeypatch.setattr(um, "_is_editable_install", lambda *a, **k: False)
        monkeypatch.delenv("UV_TOOL_DIR", raising=False)
        monkeypatch.delenv("PIPX_HOME", raising=False)
        monkeypatch.setattr(
            um,
            "_normalised_executable",
            lambda: "/home/u/.local/share/uv/tools/bog-agents-cli/bin/python",
        )
        assert um.detect_install_method() == InstallMethod.UV_TOOL

    def test_pipx_path(self, monkeypatch) -> None:
        monkeypatch.setattr(um, "_is_editable_install", lambda *a, **k: False)
        monkeypatch.delenv("UV_TOOL_DIR", raising=False)
        monkeypatch.delenv("PIPX_HOME", raising=False)
        monkeypatch.setattr(
            um,
            "_normalised_executable",
            lambda: "/home/u/.local/pipx/venvs/bog-agents-cli/bin/python",
        )
        assert um.detect_install_method() == InstallMethod.PIPX

    def test_pip_fallback(self, monkeypatch) -> None:
        monkeypatch.setattr(um, "_is_editable_install", lambda *a, **k: False)
        monkeypatch.delenv("UV_TOOL_DIR", raising=False)
        monkeypatch.delenv("PIPX_HOME", raising=False)
        monkeypatch.setattr(um, "_normalised_executable", lambda: "/usr/bin/python3")
        assert um.detect_install_method() == InstallMethod.PIP

    def test_uv_tool_dir_env_override(self, monkeypatch) -> None:
        monkeypatch.setattr(um, "_is_editable_install", lambda *a, **k: False)
        monkeypatch.setenv("UV_TOOL_DIR", "/custom/uvtools")
        monkeypatch.delenv("PIPX_HOME", raising=False)
        monkeypatch.setattr(
            um,
            "_normalised_executable",
            lambda: "/custom/uvtools/bog-agents-cli/bin/python",
        )
        assert um.detect_install_method() == InstallMethod.UV_TOOL

    def test_detection_never_raises(self, monkeypatch) -> None:
        def _boom(*_a, **_k):
            raise RuntimeError("metadata exploded")

        monkeypatch.setattr(um, "_is_editable_install", _boom)
        assert um.detect_install_method() == InstallMethod.UNKNOWN

    def test_uvx_cache_windows_is_unknown(self, monkeypatch) -> None:
        # uvx ephemeral runs must NOT be offered a (no-op) pip auto-upgrade.
        monkeypatch.setattr(um, "_is_editable_install", lambda *a, **k: False)
        monkeypatch.delenv("UV_TOOL_DIR", raising=False)
        monkeypatch.delenv("PIPX_HOME", raising=False)
        monkeypatch.setattr(
            um,
            "_normalised_executable",
            lambda: "c:/users/u/appdata/local/uv/cache/archive-v0/h/scripts/python.exe",
        )
        assert um.detect_install_method() == InstallMethod.UNKNOWN

    def test_uvx_cache_posix_is_unknown(self, monkeypatch) -> None:
        monkeypatch.setattr(um, "_is_editable_install", lambda *a, **k: False)
        monkeypatch.delenv("UV_TOOL_DIR", raising=False)
        monkeypatch.delenv("PIPX_HOME", raising=False)
        monkeypatch.setattr(
            um,
            "_normalised_executable",
            lambda: "/home/u/.cache/uv/archive-v0/h/bin/python",
        )
        assert um.detect_install_method() == InstallMethod.UNKNOWN


class TestIsNewer:
    def test_strictly_newer(self) -> None:
        assert um.is_newer("0.10.0", "0.9.8") is True

    def test_same_version(self) -> None:
        assert um.is_newer("0.9.8", "0.9.8") is False

    def test_older(self) -> None:
        assert um.is_newer("0.9.7", "0.9.8") is False

    def test_malformed_never_triggers_update(self) -> None:
        assert um.is_newer("not-a-version", "0.9.8") is False

    def test_differing_segment_counts_are_equal(self) -> None:
        # "1.0.0" must not be treated as newer than "1.0" (zero-pad fallback).
        assert um.is_newer("1.0.0", "1.0") is False
        assert um.is_newer("1.0", "1.0.0") is False


class TestCliUpdateAvailable:
    """The startup-banner helper folded in from the old update_check module."""

    def test_update_available(self, monkeypatch) -> None:
        monkeypatch.setattr(
            um, "_fetch_latest_pypi", lambda name, *, force=False: "99.0.0"
        )
        monkeypatch.setattr(
            um, "_installed_version", lambda name, fallback=None: "0.9.8"
        )
        available, latest = um.cli_update_available()
        assert available is True
        assert latest == "99.0.0"

    def test_up_to_date(self, monkeypatch) -> None:
        monkeypatch.setattr(
            um, "_fetch_latest_pypi", lambda name, *, force=False: "0.9.8"
        )
        monkeypatch.setattr(
            um, "_installed_version", lambda name, fallback=None: "0.9.8"
        )
        available, _latest = um.cli_update_available()
        assert available is False

    def test_fetch_failure_is_safe(self, monkeypatch) -> None:
        monkeypatch.setattr(um, "_fetch_latest_pypi", lambda name, *, force=False: None)
        available, latest = um.cli_update_available()
        assert available is False
        assert latest is None


class TestBuildUpgradeArgv:
    def test_uv_tool_with_uv_present(self, monkeypatch) -> None:
        monkeypatch.setattr(um.shutil, "which", lambda _n: "/x/uv")
        argv = um.build_upgrade_argv(InstallMethod.UV_TOOL)
        assert argv == ["/x/uv", "tool", "upgrade", "bog-agents-cli"]

    def test_uv_tool_missing_executable_returns_none(self, monkeypatch) -> None:
        monkeypatch.setattr(um.shutil, "which", lambda _n: None)
        assert um.build_upgrade_argv(InstallMethod.UV_TOOL) is None

    def test_pipx(self, monkeypatch) -> None:
        monkeypatch.setattr(um.shutil, "which", lambda _n: "/x/pipx")
        assert um.build_upgrade_argv(InstallMethod.PIPX) == [
            "/x/pipx",
            "upgrade",
            "bog-agents-cli",
        ]

    def test_pip_uses_current_interpreter(self) -> None:
        assert um.build_upgrade_argv(InstallMethod.PIP) == [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--upgrade",
            "bog-agents-cli",
        ]

    def test_editable_and_unknown_return_none(self) -> None:
        assert um.build_upgrade_argv(InstallMethod.EDITABLE) is None
        assert um.build_upgrade_argv(InstallMethod.UNKNOWN) is None


def _patch_suite(
    monkeypatch,
    *,
    method: InstallMethod,
    installed: dict[str, str | None],
    latest: dict[str, str | None],
) -> None:
    monkeypatch.setattr(um, "detect_install_method", lambda: method)
    monkeypatch.setattr(
        um,
        "_installed_version",
        lambda name, fallback=None: installed.get(name, fallback),
    )
    monkeypatch.setattr(
        um, "_fetch_latest_pypi", lambda name, *, force=True: latest.get(name)
    )


class TestGetSuiteStatus:
    def test_update_available_cli_only(self, monkeypatch) -> None:
        _patch_suite(
            monkeypatch,
            method=InstallMethod.UV_TOOL,
            installed={um.CLI_PACKAGE: "0.9.8", um.SDK_PACKAGE: "0.9.8"},
            latest={um.CLI_PACKAGE: "0.10.0"},
        )
        status = um.get_suite_status()
        assert status.method == InstallMethod.UV_TOOL
        assert status.cli.update_available is True
        assert status.cli.latest == "0.10.0"
        assert status.daemon is None  # not installed
        assert status.any_update is True

    def test_up_to_date(self, monkeypatch) -> None:
        _patch_suite(
            monkeypatch,
            method=InstallMethod.PIP,
            installed={um.CLI_PACKAGE: "0.10.0", um.SDK_PACKAGE: "0.10.0"},
            latest={um.CLI_PACKAGE: "0.10.0"},
        )
        status = um.get_suite_status()
        assert status.cli.update_available is False
        assert status.any_update is False

    def test_daemon_included_when_installed(self, monkeypatch) -> None:
        _patch_suite(
            monkeypatch,
            method=InstallMethod.UV_TOOL,
            installed={
                um.CLI_PACKAGE: "0.9.8",
                um.SDK_PACKAGE: "0.9.8",
                um.DAEMON_PACKAGE: "0.9.8",
            },
            latest={um.CLI_PACKAGE: "0.10.0", um.DAEMON_PACKAGE: "0.10.0"},
        )
        status = um.get_suite_status()
        assert status.daemon is not None
        assert status.daemon.update_available is True

    def test_network_failure_means_no_update(self, monkeypatch) -> None:
        _patch_suite(
            monkeypatch,
            method=InstallMethod.UV_TOOL,
            installed={um.CLI_PACKAGE: "0.9.8", um.SDK_PACKAGE: "0.9.8"},
            latest={um.CLI_PACKAGE: None},  # PyPI unreachable
        )
        status = um.get_suite_status()
        assert status.cli.update_available is False
        assert status.any_update is False


class TestBuildPlan:
    def _status(
        self,
        *,
        method: InstallMethod,
        cli_update: bool,
        daemon: PackageStatus | None = None,
    ) -> SuiteStatus:
        cli = PackageStatus(
            um.CLI_PACKAGE, "bog-agents-cli", True, "0.9.8", "0.10.0", cli_update
        )
        sdk = PackageStatus(
            um.SDK_PACKAGE, "bog-agents (SDK)", True, "0.9.8", None, False
        )
        return SuiteStatus(method=method, cli=cli, sdk=sdk, daemon=daemon)

    def test_auto_updatable_pip(self, monkeypatch) -> None:
        monkeypatch.setattr(um.shutil, "which", lambda _n: "/x/uv")
        plan = um.build_plan(self._status(method=InstallMethod.PIP, cli_update=True))
        assert plan.needs_update is True
        assert plan.can_auto_update is True
        assert plan.argv is not None

    def test_editable_refuses_auto_update(self) -> None:
        plan = um.build_plan(
            self._status(method=InstallMethod.EDITABLE, cli_update=True)
        )
        assert plan.needs_update is True
        assert plan.can_auto_update is False
        assert plan.argv is None
        assert "git pull" in plan.guidance

    def test_no_update(self) -> None:
        plan = um.build_plan(
            self._status(method=InstallMethod.UV_TOOL, cli_update=False)
        )
        assert plan.needs_update is False
        assert plan.can_auto_update is False

    def test_daemon_note_present(self, monkeypatch) -> None:
        monkeypatch.setattr(um.shutil, "which", lambda _n: "/x/uv")
        daemon = PackageStatus(
            um.DAEMON_PACKAGE, "bog-agents-daemon", True, "0.9.8", "0.10.0", True
        )
        plan = um.build_plan(
            self._status(method=InstallMethod.UV_TOOL, cli_update=True, daemon=daemon)
        )
        assert "bog-agents-daemon" in plan.daemon_note


class TestRunUpgrade:
    def test_no_command(self) -> None:
        outcome = um.run_upgrade(None)
        assert outcome.ok is False
        assert outcome.error == "no_command"

    def test_success(self, monkeypatch) -> None:
        monkeypatch.setattr(
            um.subprocess,
            "run",
            lambda *a, **k: SimpleNamespace(returncode=0, stdout="done", stderr=""),
        )
        outcome = um.run_upgrade(["uv", "tool", "upgrade", "bog-agents-cli"])
        assert outcome.ok is True
        assert outcome.error is None
        assert outcome.stdout == "done"

    def test_nonzero_exit(self, monkeypatch) -> None:
        monkeypatch.setattr(
            um.subprocess,
            "run",
            lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
        )
        outcome = um.run_upgrade(["pipx", "upgrade", "bog-agents-cli"])
        assert outcome.ok is False
        assert outcome.error is None
        assert outcome.returncode == 1

    def test_timeout(self, monkeypatch) -> None:
        def _raise(*_a, **_k):
            raise subprocess.TimeoutExpired(cmd="x", timeout=1)

        monkeypatch.setattr(um.subprocess, "run", _raise)
        outcome = um.run_upgrade(["uv", "tool", "upgrade", "bog-agents-cli"])
        assert outcome.ok is False
        assert outcome.error == "timeout"

    def test_executable_not_found(self, monkeypatch) -> None:
        def _raise(*_a, **_k):
            raise FileNotFoundError("no uv")

        monkeypatch.setattr(um.subprocess, "run", _raise)
        outcome = um.run_upgrade(["uv", "tool", "upgrade", "bog-agents-cli"])
        assert outcome.ok is False
        assert outcome.error == "not_found"


def test_describe_failure_includes_manual_command() -> None:
    plan = UpdatePlan(
        needs_update=True,
        method=InstallMethod.UV_TOOL,
        package=um.CLI_PACKAGE,
        current="0.9.8",
        latest="0.10.0",
        argv=["uv", "tool", "upgrade", "bog-agents-cli"],
        display_command="uv tool upgrade bog-agents-cli",
        can_auto_update=True,
        guidance="",
        daemon_note="",
    )
    outcome = UpgradeOutcome(False, 1, "", "permission denied", None)
    message = um.describe_failure(outcome, plan)
    assert "unchanged" in message
    assert "uv tool upgrade bog-agents-cli" in message
    assert "permission denied" in message


def test_render_status_shows_arrow_for_update() -> None:
    cli = PackageStatus(um.CLI_PACKAGE, "bog-agents-cli", True, "0.9.8", "0.10.0", True)
    sdk = PackageStatus(um.SDK_PACKAGE, "bog-agents (SDK)", True, "0.9.8", None, False)
    status = SuiteStatus(method=InstallMethod.UV_TOOL, cli=cli, sdk=sdk, daemon=None)
    rendered = um.render_status(status)
    assert "uv tool" in rendered
    assert "0.9.8" in rendered
    assert "0.10.0" in rendered
    assert "->" in rendered
