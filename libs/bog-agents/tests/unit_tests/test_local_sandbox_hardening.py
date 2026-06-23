"""Hardening tests for `bog_agents.sandbox.local_sandbox` (see [S43]).

These cover the seatbelt-profile temp-file path in `wrap_command_with_sandbox`:
the profile must be written with an explicit `utf-8` encoding, and the temp
file must persist after the call returns (sandbox-exec reads `-f` lazily at
exec time). These are platform-independent: the seatbelt branch is exercised
by stubbing `get_platform_sandbox_support` so the tests run on any OS.
"""

from __future__ import annotations

from pathlib import Path

from bog_agents.sandbox import local_sandbox
from bog_agents.sandbox.local_sandbox import (
    LocalSandbox,
    SandboxLevel,
    SandboxSupport,
    _build_seatbelt_profile,
    wrap_command_with_sandbox,
)


def _force_seatbelt(monkeypatch) -> None:
    """Force `wrap_command_with_sandbox` down the macOS seatbelt branch."""
    support = SandboxSupport(platform="darwin", seatbelt_available=True, best_method="seatbelt")
    monkeypatch.setattr(local_sandbox, "get_platform_sandbox_support", lambda: support)


def test_seatbelt_profile_written_with_utf8(monkeypatch, tmp_path: Path) -> None:
    """A non-ASCII path in the profile is written and round-trips as utf-8."""
    _force_seatbelt(monkeypatch)
    # Non-ASCII path component would crash a cp1252/cp932 default encoding.
    work_dir = tmp_path / "résumé-dir"
    work_dir.mkdir()
    sandbox = LocalSandbox(level=SandboxLevel.WORKSPACE_WRITE, working_dir=work_dir)

    args = wrap_command_with_sandbox("echo hi", sandbox)

    assert args[0] == "sandbox-exec"
    assert args[1] == "-f"
    profile_path = Path(args[2])
    try:
        # File must still exist after return (delete=False, lazy -f read).
        assert profile_path.exists()
        contents = profile_path.read_text(encoding="utf-8")
        assert str(work_dir) in contents
        assert contents == _build_seatbelt_profile(sandbox)
    finally:
        profile_path.unlink(missing_ok=True)


def test_seatbelt_temp_file_suffix_and_prefix(monkeypatch, tmp_path: Path) -> None:
    """The generated profile temp file keeps the documented naming scheme."""
    _force_seatbelt(monkeypatch)
    sandbox = LocalSandbox(level=SandboxLevel.READ_ONLY, working_dir=tmp_path)

    args = wrap_command_with_sandbox("ls", sandbox)
    profile_path = Path(args[2])
    try:
        assert profile_path.name.startswith("bog_agents_sandbox_")
        assert profile_path.suffix == ".sb"
    finally:
        profile_path.unlink(missing_ok=True)
