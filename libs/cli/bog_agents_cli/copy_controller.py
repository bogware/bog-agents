"""Controller for the ``/copy`` slash command.

`build_copy_payload` / `parse_copy_mode` are pure logic over a thread's messages
(LangChain `BaseMessage` instances *or* the dicts the LangGraph dev server
returns in remote mode), unit-testable without the TUI. `handle_copy_command`
is the thin glue the app delegates to: it takes the app duck-typed (so it too
tests against a fake) and writes to the clipboard off the UI thread.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

logger = logging.getLogger(__name__)

CopyMode = Literal["last", "all"]

_ROLE_LABELS = {
    "human": "You",
    "ai": "Assistant",
    "system": "System",
    "tool": "Tool",
}


def _message_type(message: object) -> str:
    """Return a message's coarse type (`human`/`ai`/`system`/`tool`), or ''.

    Handles both LangChain message objects (``.type``) and the plain dicts the
    remote LangGraph server returns (``type`` or ``role`` key).
    """
    mtype = getattr(message, "type", None)
    if mtype is None and isinstance(message, dict):
        mtype = message.get("type") or message.get("role")
    return mtype if isinstance(mtype, str) else ""


def _message_text(message: object) -> str:
    """Extract plain text from a message with str- or block-list content."""
    content = getattr(message, "content", None)
    if content is None and isinstance(message, dict):
        content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


def build_copy_payload(
    messages: list[Any] | None,
    mode: CopyMode = "last",
) -> tuple[str | None, str]:
    """Build the text to copy and a short notification message.

    Args:
        messages: The thread's messages (LangChain objects or dicts).
        mode: ``"last"`` copies the last assistant response; ``"all"`` copies
            the whole conversation as a labeled transcript.

    Returns:
        A ``(text, notify)`` pair. ``text`` is ``None`` when there is nothing to
        copy, in which case ``notify`` explains why.
    """
    msgs = list(messages or [])
    if not msgs:
        return None, "Nothing to copy yet — the conversation is empty."

    if mode == "all":
        blocks: list[str] = []
        for message in msgs:
            mtype = _message_type(message)
            if mtype not in _ROLE_LABELS:
                continue
            text = _message_text(message).strip()
            if not text:
                continue
            blocks.append(f"## {_ROLE_LABELS[mtype]}\n{text}")
        if not blocks:
            return None, "Nothing to copy — no text content in the conversation."
        transcript = "\n\n".join(blocks)
        return transcript, f"Copied the full transcript ({len(transcript)} chars)."

    for message in reversed(msgs):
        if _message_type(message) != "ai":
            continue
        text = _message_text(message).strip()
        if text:
            return text, f"Copied the last response ({len(text)} chars)."
    return None, "No assistant response to copy yet."


def parse_copy_mode(command: str) -> CopyMode:
    """Parse the ``/copy`` argument into a mode (defaults to ``"last"``)."""
    parts = command.split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip().lower() in {
        "all",
        "transcript",
        "conversation",
    }:
        return "all"
    return "last"


async def handle_copy_command(app: Any, command: str) -> None:  # noqa: ANN401  # app is duck-typed (BogAgentsApp) so this stays testable with a fake
    """Copy the last response (or whole transcript) to the clipboard.

    `app` is duck-typed — it needs `_current_thread_id()`,
    `_get_thread_state_values()`, `notify()` and `run_worker()` — so this tests
    against a fake app. `/copy` copies the last assistant reply; `/copy all`
    copies the whole conversation.
    """
    from bog_agents_cli.clipboard import copy_text_to_clipboard_async

    messages: list[Any] = []
    thread_id = app._current_thread_id()
    if thread_id:
        try:
            values = await app._get_thread_state_values(thread_id)
            raw = values.get("messages")
            if isinstance(raw, list):
                messages = raw
        except Exception:
            logger.debug("copy: failed to read thread state", exc_info=True)

    text, notify_message = build_copy_payload(messages, parse_copy_mode(command))
    if text is None:
        app.notify(notify_message, severity="warning", timeout=3, markup=False)
        return
    copy_text_to_clipboard_async(app, text, notify_message)
