"""Screenshot + text-grid capture for drive scripts.

Two artefacts per snapshot, side by side:

* ``<stem>.svg`` — Textual's native :meth:`~textual.app.App.export_screenshot`.
  Faithful, visually reviewable, but large and unsuitable for diffing
  via ``git diff``.
* ``<stem>.txt`` — a plain-text rendering of the screen at capture
  time. Built by walking the rendered strips of each visible widget so
  diff tooling produces meaningful before/after pairs.

Both writes are best-effort: a failure on either side does NOT abort
the script (it surfaces in the JSONL row for the step).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from textual.app import App

logger = logging.getLogger(__name__)


__all__ = ["SnapshotResult", "capture_snapshot"]


@dataclass(frozen=True, slots=True)
class SnapshotResult:
    """Where the snapshot artefacts landed."""

    svg_path: Path | None
    txt_path: Path | None
    svg_error: str | None = None
    txt_error: str | None = None

    @property
    def ok(self) -> bool:
        return self.svg_error is None and self.txt_error is None


def capture_snapshot(app: App, stem: Path) -> SnapshotResult:
    """Write SVG + text snapshots whose base path is *stem*.

    Args:
        app: The running :class:`~textual.app.App`. Must be inside
            ``run_test()``; the screen must be composed.
        stem: Artifact stem. ``.svg`` and ``.txt`` are appended.

    Returns:
        A :class:`SnapshotResult` reporting per-format outcomes.
    """
    stem.parent.mkdir(parents=True, exist_ok=True)
    svg_path = stem.with_suffix(".svg")
    txt_path = stem.with_suffix(".txt")

    svg_error: str | None = None
    txt_error: str | None = None

    try:
        # ``export_screenshot`` returns the SVG string; ``save_screenshot``
        # writes to disk. We prefer save_screenshot when available because
        # it picks the right title/size; fall back to export+write.
        save = getattr(app, "save_screenshot", None)
        if callable(save):
            save(filename=svg_path.name, path=str(svg_path.parent))
        else:
            export = getattr(app, "export_screenshot", None)
            if not callable(export):
                msg = "App provides neither save_screenshot nor export_screenshot"
                raise RuntimeError(msg)
            svg_path.write_text(export(title=stem.name), encoding="utf-8")
    except Exception as exc:
        logger.warning("SVG snapshot failed for %s: %s", svg_path, exc)
        svg_error = str(exc)
        svg_path = None

    try:
        txt_path.write_text(_render_screen_text(app), encoding="utf-8")
    except Exception as exc:
        logger.warning("text snapshot failed for %s: %s", txt_path, exc)
        txt_error = str(exc)
        txt_path = None

    return SnapshotResult(
        svg_path=svg_path,
        txt_path=txt_path,
        svg_error=svg_error,
        txt_error=txt_error,
    )


def _render_screen_text(app: App) -> str:
    """Render the current screen as plain text.

    Walks every visible widget on the active screen and concatenates
    its rendered strips. Cells are joined by whitespace and rows by
    newlines so the resulting file diffs sensibly across runs.

    Falls back to a simple message-data dump if the live screen render
    is unavailable (e.g. the app exited).
    """
    try:
        screen = app.screen
    except Exception:
        return _fallback_message_dump(app)

    lines: list[str] = []
    seen_widgets: set[int] = set()
    for child in screen.query("*"):
        wid = id(child)
        if wid in seen_widgets:
            continue
        seen_widgets.add(wid)
        try:
            renderable = child.render()
        except Exception:
            continue
        text = _renderable_to_text(renderable).strip()
        if not text:
            continue
        css_id = child.id or type(child).__name__
        lines.append(f"### {css_id}")
        lines.append(text)
        lines.append("")
    if not lines:
        return _fallback_message_dump(app)
    return "\n".join(lines).rstrip() + "\n"


def _renderable_to_text(renderable: object) -> str:
    """Best-effort string conversion of a Rich renderable.

    Rich's :class:`~rich.console.Console` can render anything, but
    instantiating a console per widget is wasteful for our purposes.
    The renderables we see in the chat transcript are mostly
    :class:`~rich.text.Text`, :class:`~rich.panel.Panel`, and plain
    strings — all of which round-trip cleanly through ``str()``.
    """
    if renderable is None:
        return ""
    if isinstance(renderable, str):
        return renderable
    plain = getattr(renderable, "plain", None)
    if isinstance(plain, str):
        return plain
    try:
        return str(renderable)
    except Exception:
        return ""


def _fallback_message_dump(app: App) -> str:
    """When the live screen is unrenderable, dump the message store."""
    store = getattr(app, "_message_store", None)
    if store is None:
        return "(screen unavailable; no message store on app)\n"
    lines: list[str] = []
    for msg in store.get_all_messages():
        prefix = f"[{msg.type.value}]"
        if msg.tool_name:
            prefix = f"{prefix} {msg.tool_name}"
        lines.append(f"{prefix} {msg.content}".rstrip())
    return "\n".join(lines) + ("\n" if lines else "")
