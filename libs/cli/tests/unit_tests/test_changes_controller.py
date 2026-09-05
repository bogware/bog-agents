"""ROADMAP #66: the turn-end changes tray."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from bog_agents_cli import changes_controller as cc
from bog_agents_cli.file_ops import FileOperationRecord


def _rec(
    display: str,
    physical: Path | None,
    before: str,
    after: str,
    *,
    tool: str = "edit_file",
) -> FileOperationRecord:
    rec = FileOperationRecord(
        tool_name=tool,
        display_path=display,
        physical_path=physical,
        tool_call_id="t",
        status="success",
    )
    rec.before_content = before
    rec.after_content = after
    return rec


def _fake_app(cwd: Path) -> SimpleNamespace:
    mounted: list[object] = []

    async def _mount(widget: object) -> None:
        mounted.append(widget)

    return SimpleNamespace(_cwd=str(cwd), _mount_message=_mount, mounted=mounted)


def test_collect_folds_records_and_ranks(tmp_path: Path) -> None:
    api = tmp_path / "pkg" / "api.py"
    lock = tmp_path / "uv.lock"
    test = tmp_path / "tests" / "test_api.py"
    records = [
        _rec("uv.lock", lock, "a\n", "a\nb\n"),
        _rec(
            "pkg/api.py", api, "import os\n", "import os\ndef public():\n    return 1\n"
        ),
        _rec(
            "pkg/api.py",
            api,
            "import os\ndef public():\n    return 1\n",
            "import os\ndef public():\n    return 2\n",
        ),
        _rec(
            "tests/test_api.py",
            test,
            "",
            "def test_it():\n    pass\n",
            tool="write_file",
        ),
        _rec("README.md", tmp_path / "README.md", "same\n", "same\n"),
        _rec("notes.md", None, "x\n", "y\n", tool="read_file"),
    ]
    changes = cc.collect_turn_changes(records)
    assert [f.display_path for f in changes.files] == [
        "pkg/api.py",
        "tests/test_api.py",
        "uv.lock",
    ]
    api_change = changes.files[0]
    assert api_change.before == "import os\n"
    assert api_change.after.endswith("return 2\n")
    assert (api_change.added, api_change.removed) == (2, 0)
    assert changes.files[-1].muted is True
    tray = cc.render_tray(changes)
    assert "Changes this turn" in tray
    assert " 1. pkg/api.py" in tray
    assert "(muted)" in tray
    assert "No file changes" in cc.render_tray(cc.TurnChanges())


def test_show_revert_and_keep(tmp_path: Path) -> None:
    target = tmp_path / "x.txt"
    letters = [chr(ord("a") + i) for i in range(20)]
    before = "\n".join(letters) + "\n"
    edited = list(letters)
    edited[1] = "B"
    edited[17] = "R"
    after = "\n".join(edited) + "\n"
    only_first = list(letters)
    only_first[1] = "B"
    target.write_text(after, encoding="utf-8")
    app = _fake_app(tmp_path)
    app._last_changes = cc.collect_turn_changes([_rec("x.txt", target, before, after)])
    text, diff = cc.handle_changes_command(app, "/changes show 1")
    assert text is None and diff is not None and diff[1] == "x.txt" and "+B" in diff[0]
    text, _ = cc.handle_changes_command(app, "/changes revert 1 2")
    assert text.startswith("Reverted hunk 2")
    assert target.read_text(encoding="utf-8") == "\n".join(only_first) + "\n"
    text, _ = cc.handle_changes_command(app, "/changes revert 1")
    assert text.startswith("Restored x.txt")
    assert target.read_text(encoding="utf-8") == before
    text, _ = cc.handle_changes_command(app, "/changes")
    assert "+0" in text
    text, _ = cc.handle_changes_command(app, "/changes revert 9")
    assert text.startswith("Usage: /changes revert")
    text, _ = cc.handle_changes_command(app, "/changes keep")
    assert "tray cleared" in text
    assert app._last_changes is None
    text, _ = cc.handle_changes_command(app, "/changes")
    assert "No changes recorded" in text


async def test_mount_tray_and_run_command(tmp_path: Path) -> None:
    app = _fake_app(tmp_path)
    await cc.mount_changes_tray(app, SimpleNamespace(file_records=[]))
    assert app.mounted == []
    target = tmp_path / "y.py"
    target.write_text("def f():\n    return 2\n", encoding="utf-8")
    stats = SimpleNamespace(
        file_records=[
            _rec("y.py", target, "def f():\n    return 1\n", "def f():\n    return 2\n")
        ]
    )
    await cc.mount_changes_tray(app, stats)
    assert len(app.mounted) == 1
    assert app._last_changes is not None
    await cc.run_changes_command(app, "/changes show 1")
    assert len(app.mounted) == 2  # a DiffMessage


def test_maybe_reorder_diff_only_for_ordered() -> None:
    diff = "diff --git a/uv.lock b/uv.lock\n--- a/uv.lock\n+++ b/uv.lock\n@@ -1 +1,2 @@\n a\n+b\ndiff --git a/main.py b/main.py\n--- a/main.py\n+++ b/main.py\n@@ -1 +1,2 @@\n x\n+y\n"
    assert cc.maybe_reorder_diff("--stat", diff) == diff
    reordered = cc.maybe_reorder_diff("--ordered", diff)
    assert reordered.index("main.py") < reordered.index("uv.lock")
