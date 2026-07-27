"""Reusable test utilities for bog-agents backends.

`SandboxConformanceSuite` is a pytest mixin that pins the structured
`SandboxBackendProtocol` file surface a backend must implement, so a backend that
drifts off it — returning the wrong type, or raising `NotImplementedError` like
the pre-SAT-1 HarborSandbox did for `als`/`agrep`/`aglob` — fails loudly in CI
instead of silently shipping broken eval/agent runs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bog_agents.backends.protocol import (
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
)

if TYPE_CHECKING:
    from bog_agents.backends.protocol import SandboxBackendProtocol

__all__ = ["SandboxConformanceSuite"]


class SandboxConformanceSuite:
    """Contract tests for a sandbox's structured async file surface.

    Subclass in a test module (with a `Test`-prefixed name so pytest collects it)
    and provide a `sandbox` fixture returning a ready-to-use backend rooted at an
    empty, writable directory. Example::

        class TestMySandboxConformance(SandboxConformanceSuite):
            @pytest.fixture
            def sandbox(self, tmp_path):
                return MySandbox(root_dir=tmp_path)

    The suite exercises `awrite` / `aread_file` / `als` / `agrep` / `aglob` /
    `aedit` / `adelete` and asserts both the structured result TYPES and basic
    round-trips. Override `root` if the backend's working directory is not `/`.
    """

    root: str = "/"

    @pytest.fixture
    def sandbox(self) -> SandboxBackendProtocol:
        """Provide a ready-to-use sandbox rooted at an empty directory."""
        raise NotImplementedError("SandboxConformanceSuite subclasses must provide a `sandbox` fixture")

    def _p(self, name: str) -> str:
        return f"{self.root.rstrip('/')}/{name}"

    async def test_awrite_returns_writeresult(self, sandbox: SandboxBackendProtocol) -> None:
        result = await sandbox.awrite(self._p("conf_w.txt"), "hello\n")
        assert result.error is None, result.error
        assert result.path

    async def test_awrite_aread_file_roundtrip(self, sandbox: SandboxBackendProtocol) -> None:
        await sandbox.awrite(self._p("conf_rt.txt"), "alpha\nbeta\n")
        result = await sandbox.aread_file(self._p("conf_rt.txt"))
        assert isinstance(result, ReadResult)
        assert result.error is None, result.error
        assert "alpha" in (result.file_data or {}).get("content", "")

    async def test_als_lists_written_file(self, sandbox: SandboxBackendProtocol) -> None:
        await sandbox.awrite(self._p("conf_ls.txt"), "x")
        result = await sandbox.als(self.root)
        assert isinstance(result, LsResult)
        assert result.error is None, result.error
        assert any(e.get("path", "").endswith("conf_ls.txt") for e in (result.entries or []))

    async def test_agrep_finds_written_content(self, sandbox: SandboxBackendProtocol) -> None:
        await sandbox.awrite(self._p("conf_g.txt"), "needle_unique_marker\n")
        result = await sandbox.agrep("needle_unique_marker", path=self.root)
        assert isinstance(result, GrepResult)
        assert result.error is None, result.error
        assert any("needle_unique_marker" in m.get("text", "") for m in (result.matches or []))

    async def test_aglob_matches_written_file(self, sandbox: SandboxBackendProtocol) -> None:
        await sandbox.awrite(self._p("conf_glob.txt"), "x")
        result = await sandbox.aglob("*.txt", path=self.root)
        assert isinstance(result, GlobResult)
        assert result.error is None, result.error
        assert any(m.get("path", "").endswith("conf_glob.txt") for m in (result.matches or []))

    async def test_aedit_replaces_content(self, sandbox: SandboxBackendProtocol) -> None:
        await sandbox.awrite(self._p("conf_e.txt"), "before the change\n")
        edit = await sandbox.aedit(self._p("conf_e.txt"), "before", "after")
        assert edit.error is None, edit.error
        result = await sandbox.aread_file(self._p("conf_e.txt"))
        assert "after" in (result.file_data or {}).get("content", "")

    async def test_adelete_removes_file(self, sandbox: SandboxBackendProtocol) -> None:
        await sandbox.awrite(self._p("conf_d.txt"), "x")
        result = await sandbox.adelete(self._p("conf_d.txt"))
        assert result.error is None, result.error
