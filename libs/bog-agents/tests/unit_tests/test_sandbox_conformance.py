"""Validate SandboxConformanceSuite against a real backend (LocalShellBackend).

This proves the shipped conformance suite (`bog_agents.testing`) actually
exercises the structured file surface end-to-end. Satellite backends (harbor,
daytona) subclass the same suite with their own real-sandbox fixture.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bog_agents.backends.local_shell import LocalShellBackend
from bog_agents.testing import SandboxConformanceSuite

if TYPE_CHECKING:
    from pathlib import Path


class TestLocalShellConformance(SandboxConformanceSuite):
    @pytest.fixture
    def sandbox(self, tmp_path: Path) -> LocalShellBackend:
        # virtual_mode=True so paths are anchored under the tmp root ("/x.txt"
        # -> tmp_path/x.txt), matching the suite's absolute-path convention.
        return LocalShellBackend(root_dir=tmp_path, virtual_mode=True)
