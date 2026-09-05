"""ROADMAP #68: archive / unread flags as tags and `/threads group pr`."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from bog_agents_cli import sessions, thread_flags

if TYPE_CHECKING:
    import pytest


def _thread(
    thread_id: str,
    *,
    branch: str | None,
    tags: list[str] | None = None,
    label: str = "",
    updated: str = "2026-09-05T10:00:00+00:00",
) -> dict[str, Any]:
    return {
        "thread_id": thread_id,
        "agent_name": "agent",
        "updated_at": updated,
        "git_branch": branch,
        "tags": tags or [],
        "label": label,
    }


def test_group_hides_archived_and_marks_unread() -> None:
    threads = [
        _thread("t1", branch="feat/x", label="first"),
        _thread(
            "t2",
            branch="feat/x",
            label="second",
            updated="2026-09-05T11:00:00+00:00",
            tags=["unread"],
        ),
        _thread("t3", branch=None, label="loose"),
        _thread("t4", branch="feat/y", label="old", tags=["archived"]),
    ]
    groups = thread_flags.group_threads(threads)
    assert list(groups) == ["feat/x", thread_flags.NO_BRANCH]
    assert [t["thread_id"] for t in groups["feat/x"]] == ["t2", "t1"]  # newest first
    text = thread_flags.render_grouped(groups, archived_hidden=1)
    assert (
        "## feat/x  (2)" in text
        and "[unread]" in text
        and "1 archived thread(s) hidden" in text
    )
    assert "feat/y" in thread_flags.group_threads(threads, include_archived=True)
    assert thread_flags.render_grouped({}) == "No threads to show."


async def test_flags_round_trip_through_thread_metadata(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    with patch.object(sessions, "get_db_path", return_value=db):
        assert thread_flags.ARCHIVED_TAG in await thread_flags.archive_thread("t1")
        assert thread_flags.is_archived(
            {"tags": (await sessions.get_thread_metadata("t1"))["tags"]}
        )
        assert await thread_flags.mark_unread("t1") == ["archived", "unread"]
        assert await thread_flags.unarchive_thread("t1") == ["unread"]
        assert await thread_flags.mark_read("t1") == []


async def test_threads_verbs_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    from bog_agents_cli.widgets import messages

    monkeypatch.setattr(messages, "AppMessage", lambda text: f"APP:{text}")
    monkeypatch.setattr(messages, "UserMessage", lambda text: f"USER:{text}")
    mounted: list[str] = []

    async def _mount(widget: object) -> None:
        mounted.append(str(widget))

    app = SimpleNamespace(_mount_message=_mount)

    async def _list_threads(**_kw: object) -> list[dict[str, Any]]:
        return [
            _thread("t1", branch="feat/x", label="first"),
            _thread("t2", branch="feat/x", tags=["archived"]),
        ]

    monkeypatch.setattr(sessions, "list_threads", _list_threads)
    assert await thread_flags.maybe_run_threads_verb(
        app, "/threads group pr", "group pr"
    )
    assert "## feat/x  (1)" in mounted[-1] and "hidden" in mounted[-1]
    assert await thread_flags.maybe_run_threads_verb(
        app, "/threads list --group pr all", "list --group pr all"
    )
    assert "## feat/x  (2)" in mounted[-1]
    assert await thread_flags.maybe_run_threads_verb(app, "/threads archive", "archive")
    assert "Usage: /threads archive" in mounted[-1]
    assert not await thread_flags.maybe_run_threads_verb(
        app, "/threads search x", "search x"
    )
    assert not await thread_flags.maybe_run_threads_verb(app, "/threads", "")
