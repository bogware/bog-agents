"""Unit tests for the Harbor wrapper's prompt context (v6 SAT-5).

`_get_formatted_system_prompt` used to call the deprecated `als_info`, which
`bog-agents==1.0.0` removes; it now reads the structured `als` result.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from bog_agents.backends.protocol import LsResult

from bog_agents_harbor.backend import run_sync
from bog_agents_harbor.bog_agents_wrapper import BogAgentsWrapper, _entry_name


class _FakeBackend:
    def __init__(self, result: LsResult, *, pwd: str = "/app\n") -> None:
        self._result = result
        self._pwd = pwd
        self.calls: list[str] = []

    async def als(self, path: str) -> LsResult:
        self.calls.append(f"als:{path}")
        return self._result

    async def aexecute(self, command: str) -> SimpleNamespace:
        self.calls.append(f"exec:{command}")
        return SimpleNamespace(output=self._pwd)


async def _prompt(backend: _FakeBackend) -> str:
    return await BogAgentsWrapper._get_formatted_system_prompt(SimpleNamespace(), backend)  # type: ignore[arg-type]


def test_entry_name_strips_parent_and_keeps_dir_marker() -> None:
    assert _entry_name({"path": "/app/main.py", "is_dir": False}) == "main.py"
    assert _entry_name({"path": "/app/src/", "is_dir": True}) == "src/"
    assert _entry_name({"path": "notes.md"}) == "notes.md"


def test_prompt_lists_files_from_structured_als() -> None:
    backend = _FakeBackend(
        LsResult(
            entries=[
                {"path": "/app/main.py", "is_dir": False},
                {"path": "/app/src/", "is_dir": True},
            ]
        )
    )
    text = asyncio.run(_prompt(backend))
    assert "Files in current directory (2 files):" in text
    assert "1. main.py" in text
    assert "2. src/" in text
    assert "Your current working directory is:\n/app" in text
    assert backend.calls[0] == "als:."


def test_prompt_handles_empty_and_errored_listings() -> None:
    assert "Current directory is empty." in asyncio.run(_prompt(_FakeBackend(LsResult(entries=[]))))
    assert "Current directory is empty." in asyncio.run(
        _prompt(_FakeBackend(LsResult(error="ls failed")))
    )


def test_prompt_caps_the_listing() -> None:
    entries = [{"path": f"/app/f{i}.txt", "is_dir": False} for i in range(15)]
    text = asyncio.run(_prompt(_FakeBackend(LsResult(entries=entries))))
    assert "showing first 10 of 15" in text
    assert "10. f9.txt" in text
    assert "f10.txt" not in text


def test_run_sync_uses_a_fresh_loop_each_call() -> None:
    async def value(n: int) -> int:
        await asyncio.sleep(0)
        return n * 2

    assert run_sync(value(2)) == 4
    assert run_sync(value(3)) == 6  # a second call must not trip over a closed loop
