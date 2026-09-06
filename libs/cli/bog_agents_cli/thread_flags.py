"""Thread flags and grouping for `/threads` (ROADMAP #68 session-UX table stakes).

`archived` and `unread` are ordinary tags in the thread metadata table, so they
travel with `/threads` exports, survive an index rebuild and need no schema
change; the FTS sidecar is untouched. `group_threads` buckets threads by git
branch (the closest thing the checkpointer records to "the PR this thread
worked on"), which is what `/threads group pr` renders.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

ARCHIVED_TAG = "archived"
UNREAD_TAG = "unread"
NO_BRANCH = "(no branch)"


def has_tag(thread: Any, tag: str) -> bool:  # noqa: ANN401 - ThreadInfo mapping
    """Whether a `ThreadInfo` carries `tag`."""
    tags = (
        thread.get("tags")
        if isinstance(thread, dict)
        else getattr(thread, "tags", None)
    )
    return bool(tags) and tag in {str(t).lower() for t in tags}


def is_archived(thread: Any) -> bool:  # noqa: ANN401 - ThreadInfo mapping
    """Whether the thread was archived with `/threads archive`."""
    return has_tag(thread, ARCHIVED_TAG)


def is_unread(thread: Any) -> bool:  # noqa: ANN401 - ThreadInfo mapping
    """Whether the thread was marked unread with `/threads unread`."""
    return has_tag(thread, UNREAD_TAG)


async def set_flag(thread_id: str, tag: str, *, on: bool) -> list[str]:
    """Add or remove `tag` on a thread's persisted tag list; returns the new list."""
    from bog_agents_cli.sessions import get_thread_metadata, set_thread_tags

    metadata = await get_thread_metadata(thread_id)
    raw_tags = metadata.get("tags")
    current = [str(t) for t in raw_tags] if isinstance(raw_tags, list) else []
    without = [t for t in current if t.lower() != tag]
    tags = [*without, tag] if on else without
    return await set_thread_tags(thread_id, tags)


async def archive_thread(thread_id: str) -> list[str]:
    """Tag a thread `archived` (hidden from grouped listings by default)."""
    return await set_flag(thread_id, ARCHIVED_TAG, on=True)


async def unarchive_thread(thread_id: str) -> list[str]:
    """Remove the `archived` tag."""
    return await set_flag(thread_id, ARCHIVED_TAG, on=False)


async def mark_unread(thread_id: str) -> list[str]:
    """Tag a thread `unread` so it stands out in listings."""
    return await set_flag(thread_id, UNREAD_TAG, on=True)


async def mark_read(thread_id: str) -> list[str]:
    """Remove the `unread` tag (called when a thread is resumed)."""
    return await set_flag(thread_id, UNREAD_TAG, on=False)


def group_threads(
    threads: Iterable[Any], *, include_archived: bool = False
) -> dict[str, list[Any]]:
    """Bucket threads by git branch, most recently updated first inside each bucket."""
    groups: dict[str, list[Any]] = {}
    for thread in threads:
        if not include_archived and is_archived(thread):
            continue
        branch = (
            thread.get("git_branch")
            if isinstance(thread, dict)
            else getattr(thread, "git_branch", None)
        )
        groups.setdefault(str(branch or NO_BRANCH), []).append(thread)
    for bucket in groups.values():
        bucket.sort(
            key=lambda t: str(t.get("updated_at") or "") if isinstance(t, dict) else "",
            reverse=True,
        )
    ordered = sorted(groups.items(), key=lambda kv: (kv[0] == NO_BRANCH, kv[0].lower()))
    return dict(ordered)


def render_grouped(groups: dict[str, list[Any]], *, archived_hidden: int = 0) -> str:
    """Text listing of `group_threads` output."""
    from bog_agents_cli.sessions import format_relative_timestamp

    if not groups:
        return "No threads to show." + (
            f" ({archived_hidden} archived hidden)" if archived_hidden else ""
        )
    lines: list[str] = []
    for branch, threads in groups.items():
        lines.append(f"## {branch}  ({len(threads)})")
        for thread in threads:
            thread_id = str(thread.get("thread_id", "?"))
            label = thread.get("label") or thread.get("initial_prompt") or ""
            when = format_relative_timestamp(thread.get("updated_at"))
            flags = " [unread]" if is_unread(thread) else ""
            lines.append(
                f"  {thread_id[:12]:<12}  {when:<12}  {str(label)[:60]}{flags}"
            )
        lines.append("")
    if archived_hidden:
        lines.append(
            f"({archived_hidden} archived thread(s) hidden — /threads group pr all)"
        )
    return "\n".join(lines).rstrip()


async def maybe_run_threads_verb(app: Any, command: str, rest: str) -> bool:  # noqa: ANN401 - the App
    """Handle `/threads search|delete|resume|group|archive|unarchive|unread|read …`; `False` when the verb is not ours."""
    from bog_agents_cli.widgets.messages import AppMessage, UserMessage

    words = rest.split()
    if not words:
        return False
    verb = words[0].lower()
    if verb == "search":
        from bog_agents_cli.session_search import format_search_results, search_sessions

        query = " ".join(words[1:]).strip()
        await app._mount_message(UserMessage(command))
        if not query:
            await app._mount_message(
                AppMessage(
                    "Usage: [bold]/threads search <text>[/bold] — full-text search past threads."
                )
            )
            return True
        hits = await search_sessions(query, limit=20)
        await app._mount_message(AppMessage(format_search_results(query, hits)))
        return True
    if verb in ("delete", "resume"):
        # ROADMAP #71: the typed forms of what the selector screen offers.
        await app._mount_message(UserMessage(command))
        if len(words) < 2:
            await app._mount_message(AppMessage(f"Usage: /threads {verb} <thread-id>"))
            return True
        thread_id = words[1]
        if verb == "delete":
            from bog_agents_cli.sessions import delete_thread

            removed = await delete_thread(thread_id)
            await app._mount_message(
                AppMessage(
                    f"Deleted thread {thread_id}."
                    if removed
                    else f"No thread {thread_id!r}."
                )
            )
            return True
        await app._resume_thread(thread_id)
        return True
    if verb == "list" and len(words) >= 2 and words[1].lower() in ("--group", "group"):
        words = ["group", *words[2:]]
        verb = "group"
    if verb == "group":
        from bog_agents_cli.sessions import get_thread_limit, list_threads

        include_archived = any(w.lower() == "all" for w in words[1:])
        await app._mount_message(UserMessage(command))
        threads = await list_threads(limit=get_thread_limit())
        groups = group_threads(threads, include_archived=include_archived)
        hidden = 0 if include_archived else sum(1 for t in threads if is_archived(t))
        await app._mount_message(
            AppMessage(render_grouped(groups, archived_hidden=hidden))
        )
        return True
    if verb in ("archive", "unarchive", "unread", "read"):
        await app._mount_message(UserMessage(command))
        if len(words) < 2:
            await app._mount_message(AppMessage(f"Usage: /threads {verb} <thread-id>"))
            return True
        thread_id = words[1]
        action = {
            "archive": archive_thread,
            "unarchive": unarchive_thread,
            "unread": mark_unread,
            "read": mark_read,
        }[verb]
        try:
            tags = await action(thread_id)
        except Exception as exc:  # report, never crash the TUI
            await app._mount_message(AppMessage(f"Could not update {thread_id}: {exc}"))
            return True
        await app._mount_message(AppMessage(f"{thread_id}: tags now {tags or '[]'}"))
        return True
    return False
