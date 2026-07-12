"""Tests for the external editor module (`bog_agents_cli.editor`)."""

from __future__ import annotations

import pathlib
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

if TYPE_CHECKING:
    import pytest

from bog_agents_cli.editor import (
    GUI_WAIT_FLAG,
    VIM_EDITORS,
    _prepare_command,
    open_in_editor,
    resolve_editor,
)


class TestResolveEditor:
    """Tests for editor resolution from the environment."""

    def test_visual_takes_priority(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VISUAL", "nvim")
        monkeypatch.setenv("EDITOR", "nano")
        assert resolve_editor() == ["nvim"]

    def test_editor_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VISUAL", raising=False)
        monkeypatch.setenv("EDITOR", "nano")
        assert resolve_editor() == ["nano"]

    def test_default_vi_on_unix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VISUAL", raising=False)
        monkeypatch.delenv("EDITOR", raising=False)
        with patch("bog_agents_cli.editor.sys") as mock_sys:
            mock_sys.platform = "linux"
            result = resolve_editor()
        assert result == ["vi"]

    def test_default_notepad_on_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VISUAL", raising=False)
        monkeypatch.delenv("EDITOR", raising=False)
        with patch("bog_agents_cli.editor.sys") as mock_sys:
            mock_sys.platform = "win32"
            result = resolve_editor()
        assert result == ["notepad"]

    def test_editor_with_args(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VISUAL", raising=False)
        monkeypatch.setenv("EDITOR", "vim -u NONE")
        assert resolve_editor() == ["vim", "-u", "NONE"]

    def test_whitespace_only_editor_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("VISUAL", raising=False)
        monkeypatch.setenv("EDITOR", "   ")
        assert resolve_editor() is None

    def test_empty_visual_falls_through_to_editor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VISUAL", "")
        monkeypatch.setenv("EDITOR", "nano")
        assert resolve_editor() == ["nano"]


class TestPrepareCommand:
    """Tests for command preparation with flag injection."""

    def test_gui_editor_gets_wait_flag(self) -> None:
        cmd = _prepare_command(["code"], "/tmp/f.md")
        assert cmd == ["code", "--wait", "/tmp/f.md"]

    def test_subl_gets_w_flag(self) -> None:
        cmd = _prepare_command(["subl"], "/tmp/f.md")
        assert cmd == ["subl", "-w", "/tmp/f.md"]

    def test_no_duplicate_wait_flag(self) -> None:
        cmd = _prepare_command(["code", "--wait"], "/tmp/f.md")
        assert cmd.count("--wait") == 1

    def test_vim_gets_i_none(self) -> None:
        cmd = _prepare_command(["vim"], "/tmp/f.md")
        assert "-i" in cmd
        assert "NONE" in cmd

    def test_vim_no_duplicate_i_flag(self) -> None:
        cmd = _prepare_command(["vim", "-i", "/dev/null"], "/tmp/f.md")
        assert cmd.count("-i") == 1

    def test_plain_terminal_editor(self) -> None:
        cmd = _prepare_command(["nano"], "/tmp/f.md")
        assert cmd == ["nano", "/tmp/f.md"]

    def test_does_not_mutate_input(self) -> None:
        original = ["code"]
        _prepare_command(original, "/tmp/f.md")
        assert original == ["code"]

    def test_gui_editor_with_full_path(self) -> None:
        cmd = _prepare_command(["/usr/local/bin/code"], "/tmp/f.md")
        assert cmd == ["/usr/local/bin/code", "--wait", "/tmp/f.md"]

    def test_known_registries_are_populated(self) -> None:
        assert "code" in GUI_WAIT_FLAG
        assert "vim" in VIM_EDITORS


class TestOpenInEditor:
    """Tests for the full open_in_editor flow (no real editor is spawned)."""

    @patch("bog_agents_cli.editor.subprocess.run")
    def test_returns_edited_text(self, mock_run: MagicMock) -> None:
        def fake_run(cmd: list[str], **_: object) -> MagicMock:
            filepath = cmd[-1]
            pathlib.Path(filepath).write_text("edited content", encoding="utf-8")
            result = MagicMock()
            result.returncode = 0
            return result

        mock_run.side_effect = fake_run
        with patch("bog_agents_cli.editor.resolve_editor", return_value=["nano"]):
            result = open_in_editor("original")
        assert result == "edited content"

    @patch("bog_agents_cli.editor.subprocess.run")
    def test_round_trips_buffer_to_editor_and_back(self, mock_run: MagicMock) -> None:
        """The buffer is written out, the editor mutates it, the result comes back."""
        observed_initial: str | None = None

        def fake_run(cmd: list[str], **_: object) -> MagicMock:
            nonlocal observed_initial
            filepath = pathlib.Path(cmd[-1])
            observed_initial = filepath.read_text(encoding="utf-8")
            filepath.write_text(observed_initial + " + appended", encoding="utf-8")
            return MagicMock(returncode=0)

        mock_run.side_effect = fake_run
        with patch("bog_agents_cli.editor.resolve_editor", return_value=["nano"]):
            result = open_in_editor("draft prompt")
        assert observed_initial == "draft prompt"
        assert result == "draft prompt + appended"

    @patch("bog_agents_cli.editor.subprocess.run")
    def test_visual_beats_editor(
        self, mock_run: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """$VISUAL wins over $EDITOR when both are set (no mock of resolve_editor)."""
        monkeypatch.setenv("VISUAL", "my-visual-editor")
        monkeypatch.setenv("EDITOR", "my-fallback-editor")
        launched: list[str] = []

        def fake_run(cmd: list[str], **_: object) -> MagicMock:
            launched.extend(cmd)
            pathlib.Path(cmd[-1]).write_text("done", encoding="utf-8")
            return MagicMock(returncode=0)

        mock_run.side_effect = fake_run
        result = open_in_editor("text")
        assert result == "done"
        assert launched[0] == "my-visual-editor"

    @patch("bog_agents_cli.editor.subprocess.run")
    def test_returns_none_on_nonzero_exit(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=1)
        with patch("bog_agents_cli.editor.resolve_editor", return_value=["nano"]):
            result = open_in_editor("text")
        assert result is None

    @patch("bog_agents_cli.editor.subprocess.run")
    def test_returns_none_on_empty_edit(self, mock_run: MagicMock) -> None:
        def fake_run(cmd: list[str], **_: object) -> MagicMock:
            pathlib.Path(cmd[-1]).write_text("   \n  ", encoding="utf-8")
            return MagicMock(returncode=0)

        mock_run.side_effect = fake_run
        with patch("bog_agents_cli.editor.resolve_editor", return_value=["nano"]):
            result = open_in_editor("original")
        assert result is None

    def test_returns_none_on_editor_not_found(self) -> None:
        with (
            patch(
                "bog_agents_cli.editor.subprocess.run",
                side_effect=FileNotFoundError("not found"),
            ),
            patch(
                "bog_agents_cli.editor.resolve_editor",
                return_value=["nonexistent"],
            ),
        ):
            result = open_in_editor("text")
        assert result is None

    @patch("bog_agents_cli.editor.subprocess.run")
    def test_normalizes_line_endings(self, mock_run: MagicMock) -> None:
        def fake_run(cmd: list[str], **_: object) -> MagicMock:
            pathlib.Path(cmd[-1]).write_bytes(b"line1\r\nline2\rline3\n")
            return MagicMock(returncode=0)

        mock_run.side_effect = fake_run
        with patch("bog_agents_cli.editor.resolve_editor", return_value=["nano"]):
            result = open_in_editor("")
        assert result == "line1\nline2\nline3"

    @patch("bog_agents_cli.editor.subprocess.run")
    def test_cleans_up_temp_file(self, mock_run: MagicMock) -> None:
        created_path: str | None = None

        def fake_run(cmd: list[str], **_: object) -> MagicMock:
            nonlocal created_path
            created_path = cmd[-1]
            return MagicMock(returncode=0)

        mock_run.side_effect = fake_run
        with patch("bog_agents_cli.editor.resolve_editor", return_value=["nano"]):
            open_in_editor("text")
        assert created_path is not None
        assert not pathlib.Path(created_path).exists()

    @patch("bog_agents_cli.editor.subprocess.run")
    def test_cleans_up_temp_file_on_error(self, mock_run: MagicMock) -> None:
        created_path: str | None = None

        def fake_run(cmd: list[str], **_: object) -> MagicMock:
            nonlocal created_path
            created_path = cmd[-1]
            return MagicMock(returncode=1)

        mock_run.side_effect = fake_run
        with patch("bog_agents_cli.editor.resolve_editor", return_value=["nano"]):
            open_in_editor("text")
        assert created_path is not None
        assert not pathlib.Path(created_path).exists()

    @patch("bog_agents_cli.editor.subprocess.run")
    def test_temp_file_has_md_extension_and_prefix(self, mock_run: MagicMock) -> None:
        def fake_run(cmd: list[str], **_: object) -> MagicMock:
            filepath = cmd[-1]
            assert filepath.endswith(".md")
            assert pathlib.Path(filepath).name.startswith("bog-agents-edit-")
            return MagicMock(returncode=0)

        mock_run.side_effect = fake_run
        with patch("bog_agents_cli.editor.resolve_editor", return_value=["nano"]):
            open_in_editor("text")

    def test_returns_none_when_resolve_editor_is_none(self) -> None:
        with patch("bog_agents_cli.editor.resolve_editor", return_value=None):
            result = open_in_editor("text")
        assert result is None

    @patch("bog_agents_cli.editor.subprocess.run")
    def test_handles_permission_error_on_cleanup(self, mock_run: MagicMock) -> None:
        """A PermissionError during temp-file cleanup must not propagate."""

        def fake_run(cmd: list[str], **_: object) -> MagicMock:
            pathlib.Path(cmd[-1]).write_text("edited", encoding="utf-8")
            return MagicMock(returncode=0)

        mock_run.side_effect = fake_run
        with (
            patch("bog_agents_cli.editor.resolve_editor", return_value=["nano"]),
            patch.object(
                pathlib.Path,
                "unlink",
                side_effect=PermissionError("locked"),
            ),
        ):
            result = open_in_editor("text")
        assert result == "edited"

    @patch("bog_agents_cli.editor.subprocess.run")
    def test_handles_unexpected_exception(self, mock_run: MagicMock) -> None:
        """Unexpected exceptions from subprocess are caught, not propagated."""
        mock_run.side_effect = RuntimeError("unexpected")
        with patch("bog_agents_cli.editor.resolve_editor", return_value=["nano"]):
            result = open_in_editor("text")
        assert result is None

    @patch("bog_agents_cli.editor.subprocess.run")
    def test_writes_initial_content_to_temp_file(self, mock_run: MagicMock) -> None:
        """The current_text is written to the temp file before the editor launches."""
        observed_content: str | None = None

        def fake_run(cmd: list[str], **_: object) -> MagicMock:
            nonlocal observed_content
            filepath = pathlib.Path(cmd[-1])
            observed_content = filepath.read_text(encoding="utf-8")
            filepath.write_text("edited", encoding="utf-8")
            return MagicMock(returncode=0)

        mock_run.side_effect = fake_run
        with patch("bog_agents_cli.editor.resolve_editor", return_value=["nano"]):
            open_in_editor("hello world")
        assert observed_content == "hello world"

    @patch("bog_agents_cli.editor.subprocess.run")
    def test_unicode_smart_quote_round_trip(self, mock_run: MagicMock) -> None:
        """UTF-8 content with a smart quote and emoji survives the round trip."""
        payload = "It’s a test 世界 \U0001f680"

        def fake_run(cmd: list[str], **_: object) -> MagicMock:
            filepath = pathlib.Path(cmd[-1])
            # The initial buffer must also have been written as valid UTF-8.
            assert filepath.read_text(encoding="utf-8") == payload
            filepath.write_text(payload, encoding="utf-8")
            return MagicMock(returncode=0)

        mock_run.side_effect = fake_run
        with patch("bog_agents_cli.editor.resolve_editor", return_value=["nano"]):
            result = open_in_editor(payload)
        assert result == payload
