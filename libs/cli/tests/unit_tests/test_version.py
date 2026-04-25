"""Tests for version-related functionality."""

import subprocess
import sys
import tomllib
from importlib import import_module
from importlib.metadata import version as pkg_version
from pathlib import Path
from unittest.mock import patch

import pytest

from bog_agents_cli._version import __version__


def test_version_matches_pyproject() -> None:
    """Verify `__version__` in `_version.py` matches version in `pyproject.toml`."""
    # Get the project root directory
    project_root = Path(__file__).parent.parent.parent
    pyproject_path = project_root / "pyproject.toml"

    # Read the version from pyproject.toml
    with pyproject_path.open("rb") as f:
        pyproject_data = tomllib.load(f)
    pyproject_version = pyproject_data["project"]["version"]

    # Compare versions
    assert __version__ == pyproject_version, (
        f"Version mismatch: _version.py has '{__version__}' "
        f"but pyproject.toml has '{pyproject_version}'"
    )


def test_sdk_dependency_is_compatible_with_workspace_version() -> None:
    """Verify the CLI dependency range is compatible with the local SDK version.

    The CLI uses a compatible release range (>=X.Y.Z,<X+1) rather than an exact
    pin to allow users to install patch/minor updates without rebuilding from source.
    The range must still be compatible with the SDK version in this monorepo.
    """
    import re

    project_root = Path(__file__).parent.parent.parent
    cli_pyproject_path = project_root / "pyproject.toml"
    sdk_pyproject_path = project_root.parent / "bog-agents" / "pyproject.toml"

    with cli_pyproject_path.open("rb") as f:
        cli_pyproject = tomllib.load(f)
    with sdk_pyproject_path.open("rb") as f:
        sdk_pyproject = tomllib.load(f)

    sdk_version = sdk_pyproject["project"]["version"]
    cli_dependencies = cli_pyproject["project"]["dependencies"]

    # Find the bog-agents dependency (exact pin or compatible range)
    bog_agents_dep = next(
        (d for d in cli_dependencies if d.startswith("bog-agents")), None
    )
    assert bog_agents_dep is not None, (
        "bog-agents dependency not found in CLI pyproject.toml"
    )

    # Accept either exact pin (==) or compatible range (>=X.Y.Z,<X.Y+1.Z)
    exact_match = f"bog-agents=={sdk_version}"
    range_match = re.match(
        r"bog-agents>=(\d+\.\d+\.\d+),<(\d+\.\d+\.\d+)", bog_agents_dep
    )
    assert bog_agents_dep == exact_match or range_match is not None, (
        f"bog-agents must be pinned to =={sdk_version} or a compatible range like "
        f">=X.Y.Z,<X.Y+1.0, got: {bog_agents_dep!r}"
    )

    if range_match:
        # Verify the SDK version falls within the declared range
        min_parts = tuple(int(x) for x in range_match.group(1).split("."))
        max_parts = tuple(int(x) for x in range_match.group(2).split("."))
        sdk_parts = tuple(int(x) for x in sdk_version.split(".")[:3])
        assert min_parts <= sdk_parts < max_parts, (
            f"SDK version {sdk_version} is outside the declared range {bog_agents_dep}"
        )


def test_cli_version_flag() -> None:
    """Verify that `--version` flag outputs the correct version."""
    result = subprocess.run(
        [sys.executable, "-m", "bog_agents_cli.main", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    # argparse exits with 0 for --version
    assert result.returncode == 0
    assert f"bog-agents-cli {__version__}" in result.stdout
    sdk_version = pkg_version("bog-agents")
    assert f"bog-agents (SDK) {sdk_version}" in result.stdout


async def test_version_slash_command_message_format() -> None:
    """Verify the `/version` slash command outputs both CLI and SDK versions."""
    from bog_agents_cli.app import BogAgentsApp
    from bog_agents_cli.widgets.messages import AppMessage

    sdk_version = pkg_version("bog-agents")

    app = BogAgentsApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._handle_command("/version")
        await pilot.pause()

        app_msgs = app.query(AppMessage)
        content = str(app_msgs[-1]._content)
        assert f"bog-agents-cli version: {__version__}" in content
        assert f"bog-agents (SDK) version: {sdk_version}" in content


async def test_version_slash_command_sdk_unavailable() -> None:
    """Verify `/version` shows 'unknown' when SDK package metadata is missing."""
    from importlib.metadata import PackageNotFoundError

    from bog_agents_cli.app import BogAgentsApp
    from bog_agents_cli.widgets.messages import AppMessage

    def patched_version(name: str) -> str:
        if name == "bog-agents":
            raise PackageNotFoundError(name)
        return pkg_version(name)

    app = BogAgentsApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        with patch("importlib.metadata.version", side_effect=patched_version):
            await app._handle_command("/version")
        await pilot.pause()

        app_msgs = app.query(AppMessage)
        content = str(app_msgs[-1]._content)
        assert f"bog-agents-cli version: {__version__}" in content
        assert "bog-agents (SDK) version: unknown" in content


async def test_version_slash_command_cli_version_unavailable() -> None:
    """Verify `/version` shows 'unknown' when CLI _version module is missing."""
    from bog_agents_cli.app import BogAgentsApp
    from bog_agents_cli.widgets.messages import AppMessage

    app = BogAgentsApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        # Setting a module to None in sys.modules causes ImportError on import
        with patch.dict(sys.modules, {"bog_agents_cli._version": None}):
            await app._handle_command("/version")
        await pilot.pause()

        app_msgs = app.query(AppMessage)
        content = str(app_msgs[-1]._content)
        assert "bog-agents-cli version: unknown" in content


def test_help_mentions_version_flag() -> None:
    """Verify that the CLI help text mentions `--version` and SDK."""
    result = subprocess.run(
        [sys.executable, "-m", "bog_agents_cli.main", "help"],
        capture_output=True,
        text=True,
        check=False,
    )
    # Help command should succeed
    assert result.returncode == 0
    # Help output should mention --version and SDK
    assert "--version" in result.stdout
    assert "SDK" in result.stdout


def test_package_import_does_not_eager_import_main() -> None:
    """Importing `bog_agents_cli` should not eagerly import `main`."""
    sys.modules.pop("bog_agents_cli", None)
    sys.modules.pop("bog_agents_cli.main", None)

    package = import_module("bog_agents_cli")

    assert callable(package.cli_main)
    assert "bog_agents_cli.main" not in sys.modules


def test_cli_help_flag() -> None:
    """Verify that `--help` flag shows help and exits with code 0."""
    result = subprocess.run(
        [sys.executable, "-m", "bog_agents_cli.main", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    # --help should exit with 0
    assert result.returncode == 0
    # Help output should mention key options
    assert "--version" in result.stdout
    assert "--agent" in result.stdout


def test_doctor_flag_uses_shared_report() -> None:
    """`--doctor` should render the same shared report used by `/doctor`."""
    from bog_agents_cli.doctor import run_doctor

    result = subprocess.run(
        [sys.executable, "-m", "bog_agents_cli.main", "--doctor"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert run_doctor().splitlines()[0] in result.stdout


def test_cli_help_flag_short() -> None:
    """Verify that `-h` flag shows help and exits with code 0."""
    result = subprocess.run(
        [sys.executable, "-m", "bog_agents_cli.main", "-h"],
        capture_output=True,
        text=True,
        check=False,
    )
    # -h should exit with 0
    assert result.returncode == 0
    # Help output should mention key options
    assert "--version" in result.stdout
    assert "--agent" in result.stdout


def test_help_excludes_interactive_features() -> None:
    """Verify that `--help` does not contain Interactive Features section."""
    result = subprocess.run(
        [sys.executable, "-m", "bog_agents_cli.main", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    # Help should succeed
    assert result.returncode == 0
    # Help should NOT contain Interactive Features section
    assert "Interactive Features" not in result.stdout
