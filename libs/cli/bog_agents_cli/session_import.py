"""Session import / export (ROADMAP #62).

Bring past conversations from Claude Code, Codex and Cline into bog as real
checkpointed threads (so `/resume`, `/threads` and `/threads search` treat
them like any other), and export a bog thread under the `com.bogware.thread`
namespace so it can travel the other way (or into another bog install).

Parsers are pure functions over files; the checkpoint write goes through the
CLI's own `sessions.get_checkpointer()` with a minimal `MessagesState` graph,
which is exactly how the TUI persists a turn.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

EXPORT_FORMAT = "com.bogware.thread"
EXPORT_VERSION = 1
SUPPORTED_SOURCES = ("claude", "codex", "cline", "bog")
_MAX_TITLE = 80


@dataclass
class ImportedMessage:
    """One turn of an imported conversation."""

    role: str  # "user" | "assistant"
    text: str
    ts: float | None = None


@dataclass
class ImportedThread:
    """A conversation ready to become a bog thread."""

    source: str
    source_id: str
    title: str
    cwd: str = ""
    messages: list[ImportedMessage] = field(default_factory=list)
    created_at: float | None = None


@dataclass
class ImportSummary:
    """Outcome of an import run."""

    source: str
    imported: list[tuple[str, str]] = field(default_factory=list)
    skipped: int = 0
    dry_run: bool = False
    notes: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ helpers


def _iso_to_ts(value: Any) -> float | None:  # noqa: ANN401 - loosely typed JSON
    if isinstance(value, (int, float)):
        return float(value) / (1000.0 if value > 1e12 else 1.0)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            return None
    return None


def _text_from_content(content: Any) -> str:  # noqa: ANN401 - loosely typed JSON
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif (
                isinstance(block, dict)
                and block.get("type") in {"text", "input_text", "output_text"}
                and isinstance(block.get("text"), str)
            ):
                parts.append(block["text"])
        return "\n".join(p for p in parts if p.strip())
    return ""


def _title_from(messages: list[ImportedMessage], fallback: str) -> str:
    for msg in messages:
        if msg.role == "user" and msg.text.strip():
            first = msg.text.strip().splitlines()[0]
            return (first[: _MAX_TITLE - 1] + "…") if len(first) > _MAX_TITLE else first
    return fallback


def _iter_jsonl(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


# ------------------------------------------------------------------ parsers


def parse_claude_code_jsonl(path: Path) -> ImportedThread | None:
    """Parse one `~/.claude/projects/<slug>/<session>.jsonl` transcript."""
    messages: list[ImportedMessage] = []
    cwd = ""
    summary = ""
    created: float | None = None
    for item in _iter_jsonl(path):
        kind = item.get("type")
        if kind == "summary" and isinstance(item.get("summary"), str):
            summary = item["summary"]
            continue
        if (
            kind not in {"user", "assistant"}
            or item.get("isMeta")
            or item.get("isSidechain")
        ):
            continue
        message = item.get("message") or {}
        text = _text_from_content(message.get("content"))
        if not text.strip():
            continue
        if not cwd and isinstance(item.get("cwd"), str):
            cwd = item["cwd"]
        ts = _iso_to_ts(item.get("timestamp"))
        if created is None and ts is not None:
            created = ts
        messages.append(ImportedMessage(role=str(kind), text=text, ts=ts))
    if not messages:
        return None
    return ImportedThread(
        source="claude",
        source_id=path.stem,
        title=summary or _title_from(messages, path.stem),
        cwd=cwd,
        messages=messages,
        created_at=created,
    )


def parse_codex_rollout(path: Path) -> ImportedThread | None:
    """Parse one Codex `~/.codex/sessions/**/rollout-*.jsonl` file (new and legacy shapes)."""
    messages: list[ImportedMessage] = []
    cwd = ""
    created: float | None = None
    for item in _iter_jsonl(path):
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else item
        if item.get("type") == "session_meta" and isinstance(payload, dict):
            cwd = str(payload.get("cwd") or cwd)
            created = created or _iso_to_ts(
                payload.get("timestamp") or item.get("timestamp")
            )
            continue
        if not isinstance(payload, dict) or payload.get("type") != "message":
            if isinstance(item.get("cwd"), str) and not cwd:
                cwd = item["cwd"]
            continue
        role = str(payload.get("role") or "")
        if role not in {"user", "assistant"}:
            continue
        text = _text_from_content(payload.get("content"))
        if not text.strip() or text.lstrip().startswith("<environment_context>"):
            continue
        ts = _iso_to_ts(item.get("timestamp"))
        created = created or ts
        messages.append(ImportedMessage(role=role, text=text, ts=ts))
    if not messages:
        return None
    return ImportedThread(
        source="codex",
        source_id=path.stem,
        title=_title_from(messages, path.stem),
        cwd=cwd,
        messages=messages,
        created_at=created,
    )


def parse_cline_task(task_dir: Path) -> ImportedThread | None:
    """Parse one Cline task directory (`api_conversation_history.json`)."""
    history = task_dir / "api_conversation_history.json"
    try:
        items = json.loads(history.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(items, list):
        return None
    messages: list[ImportedMessage] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "")
        if role not in {"user", "assistant"}:
            continue
        text = _text_from_content(item.get("content"))
        text = text.replace("<task>", "").replace("</task>", "").strip()
        if "<environment_details>" in text:
            text = text.split("<environment_details>", 1)[0].strip()
        if text:
            messages.append(ImportedMessage(role=role, text=text))
    if not messages:
        return None
    created = None
    if task_dir.name.isdigit():
        created = int(task_dir.name) / 1000.0
    return ImportedThread(
        source="cline",
        source_id=task_dir.name,
        title=_title_from(messages, task_dir.name),
        messages=messages,
        created_at=created,
    )


def parse_bog_export(path: Path) -> ImportedThread | None:
    """Parse a `com.bogware.thread` JSONL export."""
    items = _iter_jsonl(path)
    if not items or items[0].get("format") != EXPORT_FORMAT:
        return None
    head = items[0]
    messages = [
        ImportedMessage(
            role=str(i.get("role")),
            text=str(i.get("text") or ""),
            ts=_iso_to_ts(i.get("ts")),
        )
        for i in items[1:]
        if i.get("record") == "message"
        and i.get("role") in {"user", "assistant"}
        and str(i.get("text") or "").strip()
    ]
    if not messages:
        return None
    return ImportedThread(
        source="bog",
        source_id=str(head.get("thread_id") or path.stem),
        title=str(head.get("title") or _title_from(messages, path.stem)),
        cwd=str(head.get("cwd") or ""),
        messages=messages,
        created_at=_iso_to_ts(head.get("created_at")),
    )


# ------------------------------------------------------------------ discovery


def _cline_storage_roots(home: Path) -> list[Path]:
    return [
        home
        / "AppData"
        / "Roaming"
        / "Code"
        / "User"
        / "globalStorage"
        / "saoudrizwan.claude-dev"
        / "tasks",
        home
        / "Library"
        / "Application Support"
        / "Code"
        / "User"
        / "globalStorage"
        / "saoudrizwan.claude-dev"
        / "tasks",
        home
        / ".config"
        / "Code"
        / "User"
        / "globalStorage"
        / "saoudrizwan.claude-dev"
        / "tasks",
    ]


def discover_sessions(source: str, *, home: Path | None = None) -> list[Path]:
    """Find transcript files/dirs for `source`, newest first."""
    home = home or Path.home()
    source = source.strip().lower()
    paths: list[Path] = []
    if source == "claude":
        paths = list((home / ".claude" / "projects").glob("*/*.jsonl"))
    elif source == "codex":
        paths = list((home / ".codex" / "sessions").rglob("*.jsonl"))
    elif source == "cline":
        for root in _cline_storage_roots(home):
            if root.is_dir():
                paths.extend(
                    p
                    for p in root.iterdir()
                    if p.is_dir() and (p / "api_conversation_history.json").is_file()
                )
    try:
        return sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return paths


def parse_session(source: str, path: Path) -> ImportedThread | None:
    """Dispatch to the right parser."""
    source = source.strip().lower()
    if source == "claude":
        return parse_claude_code_jsonl(path)
    if source == "codex":
        return parse_codex_rollout(path)
    if source == "cline":
        return parse_cline_task(path)
    if source == "bog":
        return parse_bog_export(path)
    return None


# ------------------------------------------------------------------ writing threads


def _messages_graph() -> Any:  # noqa: ANN401 - compiled LangGraph
    from langgraph.graph import END, START, MessagesState, StateGraph

    builder: Any = StateGraph(MessagesState)
    builder.add_node("noop", lambda _state: {})
    builder.add_edge(START, "noop")
    builder.add_edge("noop", END)
    return builder


async def import_thread(thread: ImportedThread, *, agent_name: str = "agent") -> str:
    """Persist `thread` as a new checkpointed bog thread; returns its id.

    Args:
        thread: The parsed conversation.
        agent_name: The agent the thread is listed under.

    Returns:
        The new thread id.
    """
    from langchain_core.messages import AIMessage, HumanMessage

    from bog_agents_cli.sessions import (
        _upsert_thread_metadata,
        generate_thread_id,
        get_checkpointer,
    )

    thread_id = generate_thread_id()
    stamp = (
        datetime.fromtimestamp(thread.created_at, tz=UTC).isoformat()
        if thread.created_at
        else datetime.now(UTC).isoformat()
    )
    metadata = {
        "assistant_id": agent_name,
        "agent_name": agent_name,
        "updated_at": stamp,
        "imported_from": thread.source,
        "imported_id": thread.source_id,
    }
    if thread.cwd:
        metadata["cwd"] = thread.cwd
    lc_messages = [
        HumanMessage(content=m.text) if m.role == "user" else AIMessage(content=m.text)
        for m in thread.messages
    ]
    config: dict[str, Any] = {
        "configurable": {"thread_id": thread_id},
        "metadata": metadata,
    }
    async with get_checkpointer() as saver:
        graph = _messages_graph().compile(checkpointer=saver)
        await graph.aupdate_state(config, {"messages": lc_messages})
        stored = await saver.aget_tuple(config)
        if stored is not None and not (stored.metadata or {}).get("agent_name"):
            # Older LangGraph versions drop config metadata on update_state; re-stamp it.
            await saver.aput(
                config, stored.checkpoint, {**(stored.metadata or {}), **metadata}, {}
            )
    first_prompt = next((m.text for m in thread.messages if m.role == "user"), "")
    await _upsert_thread_metadata(
        thread_id,
        label=thread.title,
        summary=first_prompt[:400],
        tags=["imported", thread.source],
    )
    return thread_id


async def import_sessions(
    source: str,
    *,
    home: Path | None = None,
    agent_name: str = "agent",
    limit: int = 20,
    dry_run: bool = False,
    paths: list[Path] | None = None,
) -> ImportSummary:
    """Import up to `limit` transcripts from `source`.

    Args:
        source: `claude`, `codex`, `cline` or `bog` (a JSONL export path in `paths`).
        home: Home override (tests).
        agent_name: Agent to list the threads under.
        limit: Max threads to import (newest first).
        dry_run: Parse and report without writing.
        paths: Explicit transcript paths (skips discovery).

    Returns:
        The `ImportSummary`.
    """
    source = source.strip().lower()
    summary = ImportSummary(source=source, dry_run=dry_run)
    if source not in SUPPORTED_SOURCES:
        summary.notes.append(
            f"unknown source {source!r}; supported: {', '.join(SUPPORTED_SOURCES)} (opencode has no stable transcript format yet)"
        )
        return summary
    candidates = paths if paths is not None else discover_sessions(source, home=home)
    if not candidates:
        summary.notes.append(f"no {source} transcripts found")
        return summary
    for path in candidates[: max(1, limit)]:
        thread = parse_session(source, path)
        if thread is None:
            summary.skipped += 1
            continue
        if dry_run:
            summary.imported.append((f"(dry-run) {path.name}", thread.title))
            continue
        try:
            thread_id = await import_thread(thread, agent_name=agent_name)
        except Exception as exc:
            logger.debug("session import failed for %s", path, exc_info=True)
            summary.notes.append(f"{path.name}: {exc}")
            summary.skipped += 1
            continue
        summary.imported.append((thread_id, thread.title))
    return summary


def format_import_summary(summary: ImportSummary) -> str:
    """Render an import summary."""
    verb = "Would import" if summary.dry_run else "Imported"
    lines = [
        f"{verb} {len(summary.imported)} {summary.source} session(s); skipped {summary.skipped}"
    ]
    lines.extend(f"  {tid}  {title}" for tid, title in summary.imported)
    lines.extend(f"  note: {n}" for n in summary.notes)
    if summary.imported and not summary.dry_run:
        lines.append(
            "Open one with /resume <thread_id> or find it with /threads search <text>."
        )
    return "\n".join(lines)


# ------------------------------------------------------------------ export


def render_export(thread: ImportedThread, *, thread_id: str) -> str:
    """Serialize a thread as `com.bogware.thread` JSONL."""
    head = {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "thread_id": thread_id,
        "title": thread.title,
        "cwd": thread.cwd,
        "created_at": datetime.fromtimestamp(thread.created_at, tz=UTC).isoformat()
        if thread.created_at
        else None,
        "exported_at": datetime.now(UTC).isoformat(),
        "source": thread.source,
    }
    lines = [json.dumps(head, ensure_ascii=False)]
    lines.extend(
        json.dumps(
            {
                "record": "message",
                "role": m.role,
                "text": m.text,
                "ts": datetime.fromtimestamp(m.ts, tz=UTC).isoformat()
                if m.ts
                else None,
            },
            ensure_ascii=False,
        )
        for m in thread.messages
    )
    return "\n".join(lines) + "\n"


async def load_thread_for_export(thread_id: str) -> ImportedThread | None:
    """Read a bog thread's latest checkpoint into an `ImportedThread`."""
    from bog_agents_cli.sessions import get_checkpointer, get_thread_metadata

    async with get_checkpointer() as saver:
        stored = await saver.aget_tuple({"configurable": {"thread_id": thread_id}})
    if stored is None:
        return None
    values = (
        stored.checkpoint.get("channel_values", {})
        if isinstance(stored.checkpoint, dict)
        else {}
    )
    raw_messages = values.get("messages") or []
    messages: list[ImportedMessage] = []
    for msg in raw_messages:
        kind = getattr(msg, "type", "")
        role = "user" if kind == "human" else "assistant" if kind == "ai" else ""
        text = _text_from_content(getattr(msg, "content", ""))
        if role and text.strip():
            messages.append(ImportedMessage(role=role, text=text))
    meta = stored.metadata or {}
    try:
        extra = await get_thread_metadata(thread_id)
    except Exception:
        extra = {}
    title = str(
        extra.get("label")
        or (messages[0].text.splitlines()[0][:_MAX_TITLE] if messages else thread_id)
    )
    return ImportedThread(
        source="bog",
        source_id=thread_id,
        title=title,
        cwd=str(meta.get("cwd") or ""),
        messages=messages,
        created_at=_iso_to_ts(meta.get("updated_at")),
    )


async def export_thread(thread_id: str, out: Path) -> Path | None:
    """Write `thread_id` to `out` as `com.bogware.thread` JSONL; `None` when unknown."""
    thread = await load_thread_for_export(thread_id)
    if thread is None:
        return None
    import asyncio

    text = render_export(thread, thread_id=thread_id)

    def _write() -> None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")

    await asyncio.to_thread(_write)
    return out


def default_export_path(thread_id: str, *, base: Path | None = None) -> Path:
    """`<base or cwd>/bog-thread-<id>.jsonl`."""
    return (base or Path.cwd()) / f"bog-thread-{thread_id}.jsonl"


def now_ts() -> float:
    """Injectable clock (tests)."""
    return time.time()
