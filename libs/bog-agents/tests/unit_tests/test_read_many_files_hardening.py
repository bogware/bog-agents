"""Hardening tests for the read_many_files tool (S15).

Covers the timeout guard around glob expansion, the per-file ``limit`` clamp,
and the total-output-size cap. The tool's sync/async closures are invoked
directly via the ``StructuredTool``'s ``func``/``coroutine`` with a fake
backend, so no TUI or network is required.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock

from langchain.tools import ToolRuntime

from bog_agents.middleware import read_many_files
from bog_agents.middleware.read_many_files import (
    MAX_LIMIT,
    MAX_TOTAL_OUTPUT,
    create_read_many_files_tool,
)


def _runtime() -> MagicMock:
    runtime = MagicMock(spec=ToolRuntime)
    runtime.tool_call_id = "tc-test"
    runtime.state = {}
    return runtime


class _RecordingBackend:
    """Fake backend that records the ``limit`` it was asked to read with."""

    def __init__(self, *, content: str = "data", glob_paths: list[str] | None = None, glob_sleep: float = 0.0) -> None:
        self._content = content
        self._glob_paths = glob_paths or []
        self._glob_sleep = glob_sleep
        self.read_limits: list[int] = []

    def glob_info(self, pattern: str, path: str = "/") -> list[dict[str, Any]]:
        if self._glob_sleep:
            time.sleep(self._glob_sleep)
        return [{"path": p} for p in self._glob_paths]

    async def aglob_info(self, pattern: str, path: str = "/") -> list[dict[str, Any]]:
        return [{"path": p} for p in self._glob_paths]

    def read(self, path: str, offset: int = 0, limit: int = 100) -> str:
        self.read_limits.append(limit)
        return self._content

    async def aread(self, path: str, offset: int = 0, limit: int = 100) -> str:
        self.read_limits.append(limit)
        return self._content


def _make_tool(backend: _RecordingBackend):
    return create_read_many_files_tool(backend, lambda _runtime: backend)


def test_glob_timeout_returns_marker_not_hang(monkeypatch):
    """A glob that exceeds GLOB_TIMEOUT yields a timeout marker rather than hanging."""
    monkeypatch.setattr(read_many_files, "GLOB_TIMEOUT", 0.1)
    backend = _RecordingBackend(glob_paths=["/a.py"], glob_sleep=1.0)
    tool = _make_tool(backend)

    result = tool.func(paths=["/src/**/*.py"], runtime=_runtime())

    assert "timed out" in result
    # The slow glob never resolved, so no file was read.
    assert backend.read_limits == []


def test_limit_is_clamped_to_max():
    """An oversized per-file limit is clamped to MAX_LIMIT before hitting the backend."""
    backend = _RecordingBackend(glob_paths=[])
    tool = _make_tool(backend)

    tool.func(paths=["/a.py"], runtime=_runtime(), limit=10_000_000)

    assert backend.read_limits == [MAX_LIMIT]


def test_limit_floor_is_one():
    """A non-positive limit is floored to 1 rather than passed through."""
    backend = _RecordingBackend(glob_paths=[])
    tool = _make_tool(backend)

    tool.func(paths=["/a.py"], runtime=_runtime(), limit=0)

    assert backend.read_limits == [1]


def test_total_output_size_capped():
    """Once the concatenated output exceeds MAX_TOTAL_OUTPUT, remaining files are skipped."""
    big = "x" * (MAX_TOTAL_OUTPUT // 2 + 1)
    glob_paths = [f"/f{i}.txt" for i in range(10)]
    backend = _RecordingBackend(content=big, glob_paths=glob_paths)
    tool = _make_tool(backend)

    result = tool.func(paths=["/*.txt"], runtime=_runtime())

    assert "Output truncated" in result
    # Two big files push past the cap; the loop must stop before reading all ten.
    assert len(backend.read_limits) < len(glob_paths)


async def test_async_limit_clamped_and_glob_timeout_marker(monkeypatch):
    """Async path clamps the limit and surfaces a timeout marker on asyncio.wait_for."""
    backend = _RecordingBackend(glob_paths=["/a.py"])
    tool = _make_tool(backend)

    await tool.coroutine(paths=["/a.py"], runtime=_runtime(), limit=999_999)
    assert backend.read_limits == [MAX_LIMIT]

    async def _slow(_pattern, path="/"):
        import asyncio

        await asyncio.sleep(1.0)
        return []

    monkeypatch.setattr(read_many_files, "GLOB_TIMEOUT", 0.05)
    monkeypatch.setattr(backend, "aglob_info", _slow)
    result = await tool.coroutine(paths=["/src/**/*.py"], runtime=_runtime())
    assert "timed out" in result
