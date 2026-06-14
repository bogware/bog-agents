"""Unit tests for BogAgentsApp."""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import signal
import sys
import webbrowser
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, ClassVar
from unittest.mock import AsyncMock, MagicMock, call, patch

if TYPE_CHECKING:
    from bog_agents_cli.sessions import ThreadInfo

import pytest
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Container
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Checkbox, Input, Static

import bog_agents_cli.app as app_module
from bog_agents_cli.app import (
    _ITERM_CURSOR_GUIDE_OFF,
    _ITERM_CURSOR_GUIDE_ON,
    BogAgentsApp,
    QueuedMessage,
    TextualSessionState,
    _write_iterm_escape,
)
from bog_agents_cli.widgets.chat_input import ChatInput
from bog_agents_cli.widgets.messages import (
    AppMessage,
    ErrorMessage,
    QueuedUserMessage,
    UserMessage,
)


class TestInitialPromptOnMount:
    """Test that -m initial prompt is submitted on mount."""

    async def test_initial_prompt_triggers_handle_user_message(self) -> None:
        """When initial_prompt is set, the prompt should be auto-submitted."""
        mock_agent = MagicMock()
        app = BogAgentsApp(
            agent=mock_agent,
            thread_id="new-thread-123",
            initial_prompt="hello world",
        )
        submitted: list[str] = []

        # Must be async to match _handle_user_message's signature
        async def capture(msg: str) -> None:
            submitted.append(msg)

        app._handle_user_message = capture  # type: ignore[assignment]

        async with app.run_test() as pilot:
            # Give call_after_refresh time to fire
            await pilot.pause()
            await pilot.pause()

        assert submitted == ["hello world"]


class TestAppCSSValidation:
    """Test that app CSS is valid and doesn't cause runtime errors."""

    async def test_app_css_validates_on_mount(self) -> None:
        """App should mount without CSS validation errors.

        This test catches invalid CSS properties like 'overflow: visible'
        which are only validated at runtime when styles are applied.
        """
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            # Give the app time to render and apply CSS
            await pilot.pause()
            # If we get here without exception, CSS is valid
            assert app.is_running


class TestThreadCachePrewarm:
    """Tests for startup thread-cache prewarming."""

    async def test_prewarm_uses_current_thread_limit(self) -> None:
        """Prewarm helper should pass the resolved thread limit through."""
        app = BogAgentsApp(agent=MagicMock(), thread_id="thread-123")

        with (
            patch("bog_agents_cli.sessions.get_thread_limit", return_value=7),
            patch(
                "bog_agents_cli.sessions.prewarm_thread_message_counts",
                new_callable=AsyncMock,
            ) as mock_prewarm,
        ):
            await app._prewarm_threads_cache()

        mock_prewarm.assert_awaited_once_with(limit=7)

    async def test_show_thread_selector_uses_cached_rows(self) -> None:
        """Thread selector should receive prefetched rows when available."""
        cached_threads = [
            {
                "thread_id": "thread-abc",
                "agent_name": "agent1",
                "updated_at": "2024-01-01T00:00:00+00:00",
                "message_count": 2,
            }
        ]
        app = BogAgentsApp()

        async with app.run_test() as pilot:
            await pilot.pause()
            with (
                patch("bog_agents_cli.sessions.get_thread_limit", return_value=9),
                patch(
                    "bog_agents_cli.sessions.get_cached_threads",
                    return_value=cached_threads,
                ),
                patch("bog_agents_cli.app.ThreadSelectorScreen") as mock_screen_cls,
                patch.object(app, "push_screen") as mock_push_screen,
            ):
                mock_screen = MagicMock()
                mock_screen_cls.return_value = mock_screen
                await app._show_thread_selector()

                assert app._session_state is not None
                mock_screen_cls.assert_called_once_with(
                    current_thread=app._session_state.thread_id,
                    thread_limit=9,
                    initial_threads=cached_threads,
                )
                mock_push_screen.assert_called_once()


class TestAppBindings:
    """Test app keybindings."""

    def test_ctrl_c_binding_has_priority(self) -> None:
        """Ctrl+C should be priority-bound so focused modal inputs don't swallow it."""
        bindings = [b for b in BogAgentsApp.BINDINGS if isinstance(b, Binding)]
        bindings_by_key = {b.key: b for b in bindings}
        ctrl_c = bindings_by_key.get("ctrl+c")

        assert ctrl_c is not None
        assert ctrl_c.action == "quit_or_interrupt"
        assert ctrl_c.priority is True

    def test_toggle_tool_output_has_ctrl_e_binding(self) -> None:
        """Ctrl+E should be bound to toggle_tool_output with priority."""
        bindings = [b for b in BogAgentsApp.BINDINGS if isinstance(b, Binding)]
        bindings_by_key = {b.key: b for b in bindings}
        ctrl_e = bindings_by_key.get("ctrl+e")

        assert ctrl_e is not None
        assert ctrl_e.action == "toggle_tool_output"
        assert ctrl_e.priority is True

    def test_ctrl_o_not_bound_to_toggle_tool_output(self) -> None:
        """Ctrl+O should not exist (replaced by Ctrl+E)."""
        bindings = [b for b in BogAgentsApp.BINDINGS if isinstance(b, Binding)]
        bindings_by_key = {b.key: b for b in bindings}
        assert "ctrl+o" not in bindings_by_key

    def test_copy_binding_exists(self) -> None:
        """Copy should be available without overloading Ctrl+C."""
        bindings = [b for b in BogAgentsApp.BINDINGS if isinstance(b, Binding)]
        assert any(
            b.key == "ctrl+shift+c,ctrl+insert" and b.action == "copy_selection"
            for b in bindings
        )

    def test_paste_binding_exists(self) -> None:
        """Paste should be available even when the terminal paste path fails."""
        bindings = [b for b in BogAgentsApp.BINDINGS if isinstance(b, Binding)]
        assert any(
            b.key == "ctrl+shift+v,shift+insert" and b.action == "paste_clipboard"
            for b in bindings
        )


class TestClipboardActions:
    """Tests for clipboard shortcuts and lighter mouse handling."""

    async def test_action_paste_clipboard_routes_to_chat_input(self) -> None:
        """Clipboard paste should route through the shared chat-input handler."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._chat_input is not None

            with (
                patch("bog_agents_cli.app.read_clipboard_text", return_value="hello"),
                patch.object(
                    app._chat_input,
                    "handle_external_paste",
                    return_value=True,
                ) as mock_paste,
            ):
                app.action_paste_clipboard()

            mock_paste.assert_called_once_with("hello")

    async def test_action_paste_clipboard_warns_when_unavailable(self) -> None:
        """Empty or unavailable clipboard should produce a small warning."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            with (
                patch("bog_agents_cli.app.read_clipboard_text", return_value=None),
                patch.object(app, "notify") as mock_notify,
            ):
                app.action_paste_clipboard()

            mock_notify.assert_called_once()

    async def test_mouse_up_skips_copy_without_drag(self) -> None:
        """Simple clicks should not trigger whole-app clipboard scans."""
        app = BogAgentsApp()
        app._mouse_drag_distance = 0

        async with app.run_test() as pilot:
            await pilot.pause()

            with patch("bog_agents_cli.app.copy_selection_to_clipboard") as mock_copy:
                app.on_mouse_up(SimpleNamespace())

            mock_copy.assert_not_called()

    async def test_mouse_up_copies_after_drag_selection(self) -> None:
        """Drag selections should still auto-copy on mouse release."""
        app = BogAgentsApp()
        app._mouse_drag_distance = 3

        async with app.run_test() as pilot:
            await pilot.pause()

            with patch("bog_agents_cli.app.copy_selection_to_clipboard") as mock_copy:
                app.on_mouse_up(SimpleNamespace())

            mock_copy.assert_called_once_with(app)

    async def test_click_after_drag_does_not_refocus_input(self) -> None:
        """Selection drags should not be followed by eager input refocus."""
        app = BogAgentsApp()
        app._mouse_drag_distance = 3

        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._chat_input is not None

            with patch.object(app._chat_input, "focus_input") as mock_focus:
                app.on_click(SimpleNamespace(widget=None))

            mock_focus.assert_not_called()


class TestITerm2CursorGuide:
    """Test iTerm2 cursor guide handling."""

    def test_escape_sequences_are_valid(self) -> None:
        """Escape sequences should be properly formatted OSC 1337 commands.

        Format: OSC (ESC ]) + "1337;" + command + ST (ESC backslash)
        """
        assert _ITERM_CURSOR_GUIDE_OFF.startswith("\x1b]1337;")
        assert _ITERM_CURSOR_GUIDE_OFF.endswith("\x1b\\")
        assert "HighlightCursorLine=no" in _ITERM_CURSOR_GUIDE_OFF

        assert _ITERM_CURSOR_GUIDE_ON.startswith("\x1b]1337;")
        assert _ITERM_CURSOR_GUIDE_ON.endswith("\x1b\\")
        assert "HighlightCursorLine=yes" in _ITERM_CURSOR_GUIDE_ON

    def test_write_iterm_escape_does_nothing_when_not_iterm(self) -> None:
        """_write_iterm_escape should no-op when _IS_ITERM is False."""
        mock_stderr = MagicMock()
        with (
            patch("bog_agents_cli.app._IS_ITERM", False),
            patch("sys.__stderr__", mock_stderr),
        ):
            _write_iterm_escape(_ITERM_CURSOR_GUIDE_ON)
            mock_stderr.write.assert_not_called()

    def test_write_iterm_escape_writes_sequence_when_iterm(self) -> None:
        """_write_iterm_escape should write sequence when in iTerm2."""
        mock_stderr = io.StringIO()
        with (
            patch("bog_agents_cli.app._IS_ITERM", True),
            patch("sys.__stderr__", mock_stderr),
        ):
            _write_iterm_escape(_ITERM_CURSOR_GUIDE_ON)
            assert mock_stderr.getvalue() == _ITERM_CURSOR_GUIDE_ON

    def test_write_iterm_escape_handles_oserror_gracefully(self) -> None:
        """_write_iterm_escape should not raise on OSError."""
        mock_stderr = MagicMock()
        mock_stderr.write.side_effect = OSError("Broken pipe")
        with (
            patch("bog_agents_cli.app._IS_ITERM", True),
            patch("sys.__stderr__", mock_stderr),
        ):
            _write_iterm_escape(_ITERM_CURSOR_GUIDE_ON)

    def test_write_iterm_escape_handles_none_stderr(self) -> None:
        """_write_iterm_escape should handle None __stderr__ gracefully."""
        with (
            patch("bog_agents_cli.app._IS_ITERM", True),
            patch("sys.__stderr__", None),
        ):
            _write_iterm_escape(_ITERM_CURSOR_GUIDE_ON)


class TestITerm2Detection:
    """Test iTerm2 detection logic."""

    def test_detection_requires_tty(self) -> None:
        """_IS_ITERM should check that stderr is a TTY.

        Detection happens at module load, so we test the logic pattern directly.
        """
        with (
            patch.dict(os.environ, {"LC_TERMINAL": "iTerm2"}, clear=False),
            patch("os.isatty", return_value=False),
        ):
            result = (
                (
                    os.environ.get("LC_TERMINAL", "") == "iTerm2"
                    or os.environ.get("TERM_PROGRAM", "") == "iTerm.app"
                )
                and hasattr(os, "isatty")
                and os.isatty(2)
            )
            assert result is False

    def test_detection_via_lc_terminal(self) -> None:
        """Detection should match LC_TERMINAL=iTerm2."""
        with (
            patch.dict(
                os.environ, {"LC_TERMINAL": "iTerm2", "TERM_PROGRAM": ""}, clear=False
            ),
            patch("os.isatty", return_value=True),
        ):
            result = (
                (
                    os.environ.get("LC_TERMINAL", "") == "iTerm2"
                    or os.environ.get("TERM_PROGRAM", "") == "iTerm.app"
                )
                and hasattr(os, "isatty")
                and os.isatty(2)
            )
            assert result is True

    def test_detection_via_term_program(self) -> None:
        """Detection should match TERM_PROGRAM=iTerm.app."""
        env = {"LC_TERMINAL": "", "TERM_PROGRAM": "iTerm.app"}
        with (
            patch.dict(os.environ, env, clear=False),
            patch("os.isatty", return_value=True),
        ):
            result = (
                (
                    os.environ.get("LC_TERMINAL", "") == "iTerm2"
                    or os.environ.get("TERM_PROGRAM", "") == "iTerm.app"
                )
                and hasattr(os, "isatty")
                and os.isatty(2)
            )
            assert result is True


class TestModalScreenEscapeDismissal:
    """Test that escape key dismisses modal screens."""

    @staticmethod
    async def test_escape_dismisses_modal_screen() -> None:
        """Escape should dismiss any active ModalScreen.

        The app's action_interrupt binding intercepts escape with priority=True.
        When a modal screen is active, it should dismiss the modal rather than
        performing the default interrupt behavior.
        """

        class SimpleModal(ModalScreen[str | None]):
            """A simple test modal."""

            BINDINGS: ClassVar[list[BindingType]] = [("escape", "cancel", "Cancel")]

            def compose(self) -> ComposeResult:
                yield Static("Test Modal")

            def action_cancel(self) -> None:
                self.dismiss(None)

        class TestApp(App[None]):
            """Test app with escape -> action_interrupt binding."""

            BINDINGS: ClassVar[list[BindingType]] = [
                Binding("escape", "interrupt", "Interrupt", priority=True)
            ]

            def __init__(self) -> None:
                super().__init__()
                self.modal_dismissed = False
                self.interrupt_called = False

            def compose(self) -> ComposeResult:
                yield Container()

            def action_interrupt(self) -> None:
                if isinstance(self.screen, ModalScreen):
                    self.screen.dismiss(None)
                    return
                self.interrupt_called = True

            def show_modal(self) -> None:
                def on_dismiss(_result: str | None) -> None:
                    self.modal_dismissed = True

                self.push_screen(SimpleModal(), on_dismiss)

        app = TestApp()
        async with app.run_test() as pilot:
            app.show_modal()
            await pilot.pause()

            # Escape should dismiss the modal, not call interrupt
            await pilot.press("escape")
            await pilot.pause()

            assert app.modal_dismissed is True
            assert app.interrupt_called is False


class TestModalScreenCtrlDHandling:
    """Tests for app-level Ctrl+D behavior while modals are open."""

    async def test_ctrl_d_deletes_in_thread_selector_instead_of_quitting(self) -> None:
        """App-level quit binding should delegate to thread delete in the modal."""
        from bog_agents_cli.widgets.thread_selector import ThreadSelectorScreen

        mock_threads: list[ThreadInfo] = [
            {
                "thread_id": "thread-123",
                "agent_name": "agent",
                "updated_at": "2026-03-08T02:00:00+00:00",
                "created_at": "2026-03-08T01:00:00+00:00",
                "initial_prompt": "prompt",
            }
        ]
        with patch(
            "bog_agents_cli.sessions.list_threads",
            new_callable=AsyncMock,
            return_value=mock_threads,
        ):
            app = BogAgentsApp()
            async with app.run_test() as pilot:
                await pilot.pause()

                screen = ThreadSelectorScreen(
                    current_thread=None,
                    initial_threads=mock_threads,
                )
                app.push_screen(screen)
                await pilot.pause()

                with patch.object(app, "exit") as mock_exit:
                    await pilot.press("ctrl+d")
                    await pilot.pause()
                    await pilot.pause()

                assert screen._confirming_delete is True
                mock_exit.assert_not_called()

    async def test_escape_closes_thread_delete_confirm_without_dismissing_modal(
        self,
    ) -> None:
        """Escape should close thread delete confirmation before dismissing modal."""
        from bog_agents_cli.widgets.thread_selector import ThreadSelectorScreen

        mock_threads: list[ThreadInfo] = [
            {
                "thread_id": "thread-123",
                "agent_name": "agent",
                "updated_at": "2026-03-08T02:00:00+00:00",
                "created_at": "2026-03-08T01:00:00+00:00",
                "initial_prompt": "prompt",
            }
        ]
        with patch(
            "bog_agents_cli.sessions.list_threads",
            new_callable=AsyncMock,
            return_value=mock_threads,
        ):
            app = BogAgentsApp()
            async with app.run_test() as pilot:
                await pilot.pause()

                screen = ThreadSelectorScreen(
                    current_thread=None,
                    initial_threads=mock_threads,
                )
                app.push_screen(screen)
                await pilot.pause()

                await pilot.press("ctrl+d")
                await pilot.pause()
                await pilot.pause()
                assert screen.is_delete_confirmation_open is True

                await pilot.press("escape")
                await pilot.pause()
                await pilot.pause()

                assert app.screen is screen
                assert screen.is_delete_confirmation_open is False

    async def test_ctrl_d_twice_quits_from_delete_confirmation(self) -> None:
        """Ctrl+D should use a double-press quit flow inside delete confirmation."""
        from bog_agents_cli.widgets.thread_selector import (
            DeleteThreadConfirmScreen,
            ThreadSelectorScreen,
        )

        mock_threads: list[ThreadInfo] = [
            {
                "thread_id": "thread-123",
                "agent_name": "agent",
                "updated_at": "2026-03-08T02:00:00+00:00",
                "created_at": "2026-03-08T01:00:00+00:00",
                "initial_prompt": "prompt",
            }
        ]
        with patch(
            "bog_agents_cli.sessions.list_threads",
            new_callable=AsyncMock,
            return_value=mock_threads,
        ):
            app = BogAgentsApp()
            async with app.run_test() as pilot:
                await pilot.pause()

                screen = ThreadSelectorScreen(
                    current_thread=None,
                    initial_threads=mock_threads,
                )
                app.push_screen(screen)
                await pilot.pause()

                await pilot.press("ctrl+d")
                await pilot.pause()
                await pilot.pause()
                assert isinstance(app.screen, DeleteThreadConfirmScreen)

                with (
                    patch.object(app, "notify") as notify_mock,
                    patch.object(app, "exit") as exit_mock,
                ):
                    await pilot.press("ctrl+d")
                    await pilot.pause()
                    notify_mock.assert_called_once_with(
                        "Press Ctrl+D again to quit",
                        timeout=3,
                    )
                    assert app._quit_pending is True
                    exit_mock.assert_not_called()

                    await pilot.press("ctrl+d")
                    await pilot.pause()
                    exit_mock.assert_called_once()

    async def test_ctrl_c_still_works_from_delete_confirmation(self) -> None:
        """Ctrl+C should preserve the normal double-press quit flow in confirmation."""
        from bog_agents_cli.widgets.thread_selector import (
            DeleteThreadConfirmScreen,
            ThreadSelectorScreen,
        )

        mock_threads: list[ThreadInfo] = [
            {
                "thread_id": "thread-123",
                "agent_name": "agent",
                "updated_at": "2026-03-08T02:00:00+00:00",
                "created_at": "2026-03-08T01:00:00+00:00",
                "initial_prompt": "prompt",
            }
        ]
        with patch(
            "bog_agents_cli.sessions.list_threads",
            new_callable=AsyncMock,
            return_value=mock_threads,
        ):
            app = BogAgentsApp()
            async with app.run_test() as pilot:
                await pilot.pause()

                screen = ThreadSelectorScreen(
                    current_thread=None,
                    initial_threads=mock_threads,
                )
                app.push_screen(screen)
                await pilot.pause()

                await pilot.press("ctrl+d")
                await pilot.pause()
                await pilot.pause()
                assert isinstance(app.screen, DeleteThreadConfirmScreen)

                with (
                    patch.object(app, "notify") as notify_mock,
                    patch.object(app, "exit") as exit_mock,
                ):
                    app.action_quit_or_interrupt()
                    notify_mock.assert_called_once_with(
                        "Press Ctrl+C again to quit",
                        timeout=3,
                    )
                    assert app._quit_pending is True
                    exit_mock.assert_not_called()

                    app.action_quit_or_interrupt()
                    exit_mock.assert_called_once()

    async def test_ctrl_d_quits_from_model_selector_with_input_focused(
        self,
    ) -> None:
        """Ctrl+D should not be swallowed or ignored in the model selector."""
        from bog_agents_cli.widgets.model_selector import ModelSelectorScreen

        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            screen = ModelSelectorScreen(
                current_model="claude-sonnet-4-5",
                current_provider="anthropic",
            )
            app.push_screen(screen)
            await pilot.pause()

            filter_input = screen.query_one("#model-filter", Input)
            assert filter_input.has_focus

            with patch.object(app, "exit") as exit_mock:
                await pilot.press("ctrl+d")
                await pilot.pause()

            exit_mock.assert_called_once()

    async def test_ctrl_d_quits_from_mcp_viewer(self) -> None:
        """Ctrl+D should still quit while the MCP viewer modal is open."""
        from bog_agents_cli.mcp_tools import MCPServerInfo, MCPToolInfo
        from bog_agents_cli.widgets.mcp_viewer import MCPViewerScreen

        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            screen = MCPViewerScreen(
                server_info=[
                    MCPServerInfo(
                        name="filesystem",
                        transport="stdio",
                        tools=[
                            MCPToolInfo(
                                name="read_file",
                                description="Read a file",
                            )
                        ],
                    )
                ]
            )
            app.push_screen(screen)
            await pilot.pause()

            with patch.object(app, "exit") as exit_mock:
                await pilot.press("ctrl+d")
                await pilot.pause()

            exit_mock.assert_called_once()


class TestModalScreenShiftTabHandling:
    """Tests for app-level Shift+Tab behavior while modals are open."""

    async def test_shift_tab_cycles_permission_modes(self) -> None:
        """Shift+Tab cycles default -> accept-edits -> plan -> default."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._current_permission_mode() == "default"

            await pilot.press("shift+tab")
            await pilot.pause()
            assert app._current_permission_mode() == "accept-edits"
            assert app._auto_mode is True

            await pilot.press("shift+tab")
            await pilot.pause()
            assert app._current_permission_mode() == "plan"
            assert app._plan_mode_enabled is True
            assert app._auto_mode is False

            await pilot.press("shift+tab")
            await pilot.pause()
            assert app._current_permission_mode() == "default"
            assert app._plan_mode_enabled is False

    async def test_ctrl_t_toggles_bypass(self) -> None:
        """Ctrl+T toggles the bypass (approve-everything) mode."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._auto_approve is False

            await pilot.press("ctrl+t")
            await pilot.pause()
            assert app._current_permission_mode() == "bypass"
            assert app._auto_approve is True

            await pilot.press("ctrl+t")
            await pilot.pause()
            assert app._current_permission_mode() == "default"
            assert app._auto_approve is False

    async def test_cycle_from_bypass_reenters_at_default(self) -> None:
        """Cycling out of an out-of-cycle mode (bypass) returns to default."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._apply_permission_mode("bypass")
            assert app._current_permission_mode() == "bypass"

            await pilot.press("shift+tab")
            await pilot.pause()
            assert app._current_permission_mode() == "default"

    async def test_shift_tab_moves_backward_in_thread_selector(self) -> None:
        """Shift+Tab should move backward in the thread selector controls."""
        from bog_agents_cli.widgets.thread_selector import ThreadSelectorScreen

        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            screen = ThreadSelectorScreen(
                current_thread=None,
                initial_threads=[
                    {
                        "thread_id": "thread-123",
                        "agent_name": "agent",
                        "updated_at": "2026-03-08T02:00:00+00:00",
                        "created_at": "2026-03-08T01:00:00+00:00",
                        "initial_prompt": "prompt",
                    }
                ],
            )
            app.push_screen(screen)
            await pilot.pause()

            assert app._auto_approve is False
            filter_input = screen.query_one("#thread-filter", Input)
            sort_switch = screen.query_one("#thread-sort-toggle", Checkbox)

            await pilot.press("tab")
            await pilot.pause()
            assert sort_switch.has_focus

            await pilot.press("shift+tab")
            await pilot.pause()

            assert filter_input.has_focus
            assert app._auto_approve is False


class TestModalScreenCtrlCHandling:
    """Tests for app-level Ctrl+C behavior while modals are open."""

    async def test_ctrl_c_quits_from_thread_selector_with_input_focused(
        self,
    ) -> None:
        """Ctrl+C should reach the app even when the thread filter has focus."""
        from bog_agents_cli.widgets.thread_selector import ThreadSelectorScreen

        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            screen = ThreadSelectorScreen(
                current_thread=None,
                initial_threads=[
                    {
                        "thread_id": "thread-123",
                        "agent_name": "agent",
                        "updated_at": "2026-03-08T02:00:00+00:00",
                        "created_at": "2026-03-08T01:00:00+00:00",
                        "initial_prompt": "prompt",
                    }
                ],
            )
            app.push_screen(screen)
            await pilot.pause()

            filter_input = screen.query_one("#thread-filter", Input)
            assert filter_input.has_focus

            with (
                patch.object(app, "notify") as notify_mock,
                patch.object(app, "exit") as exit_mock,
            ):
                await pilot.press("ctrl+c")
                await pilot.pause()
                notify_mock.assert_called_once_with(
                    "Press Ctrl+C again to quit",
                    timeout=3,
                )
                assert app._quit_pending is True
                exit_mock.assert_not_called()

                await pilot.press("ctrl+c")
                await pilot.pause()
                exit_mock.assert_called_once()

    async def test_ctrl_c_quits_from_model_selector_with_input_focused(
        self,
    ) -> None:
        """Ctrl+C should not be swallowed by the model filter input."""
        from bog_agents_cli.widgets.model_selector import ModelSelectorScreen

        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            screen = ModelSelectorScreen(
                current_model="claude-sonnet-4-5",
                current_provider="anthropic",
            )
            app.push_screen(screen)
            await pilot.pause()

            filter_input = screen.query_one("#model-filter", Input)
            assert filter_input.has_focus

            with (
                patch.object(app, "notify") as notify_mock,
                patch.object(app, "exit") as exit_mock,
            ):
                await pilot.press("ctrl+c")
                await pilot.pause()
                notify_mock.assert_called_once_with(
                    "Press Ctrl+C again to quit",
                    timeout=3,
                )
                assert app._quit_pending is True
                exit_mock.assert_not_called()

                await pilot.press("ctrl+c")
                await pilot.pause()
                exit_mock.assert_called_once()

    async def test_ctrl_c_quits_from_mcp_viewer(self) -> None:
        """Ctrl+C should still trigger app quit flow while the MCP modal is open."""
        from bog_agents_cli.mcp_tools import MCPServerInfo, MCPToolInfo
        from bog_agents_cli.widgets.mcp_viewer import MCPViewerScreen

        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            screen = MCPViewerScreen(
                server_info=[
                    MCPServerInfo(
                        name="filesystem",
                        transport="stdio",
                        tools=[
                            MCPToolInfo(
                                name="read_file",
                                description="Read a file",
                            )
                        ],
                    )
                ]
            )
            app.push_screen(screen)
            await pilot.pause()

            with (
                patch.object(app, "notify") as notify_mock,
                patch.object(app, "exit") as exit_mock,
            ):
                await pilot.press("ctrl+c")
                await pilot.pause()
                notify_mock.assert_called_once_with(
                    "Press Ctrl+C again to quit",
                    timeout=3,
                )
                assert app._quit_pending is True
                exit_mock.assert_not_called()

                await pilot.press("ctrl+c")
                await pilot.pause()
                exit_mock.assert_called_once()


class TestMountMessageNoMatches:
    """Test _mount_message resilience when #messages container is missing.

    When a user interrupts a streaming response, the cancellation handler and
    error handler both call _mount_message. If the screen has been torn down
    (e.g. #messages container no longer exists), this should not crash.
    """

    async def test_mount_message_no_crash_when_messages_missing(self) -> None:
        """_mount_message should not raise NoMatches when #messages is absent."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            # Verify the #messages container exists initially
            messages_container = app.query_one("#messages", Container)
            assert messages_container is not None

            # Remove #messages to simulate a torn-down screen state
            await messages_container.remove()

            # Verify it's truly gone
            with pytest.raises(NoMatches):
                app.query_one("#messages", Container)

            # _mount_message should handle the missing container gracefully
            # Before the fix, this raises NoMatches
            await app._mount_message(AppMessage("Interrupted by user"))

    async def test_mount_error_message_no_crash_when_messages_missing(
        self,
    ) -> None:
        """ErrorMessage via _mount_message should not crash without #messages.

        This is the second crash in the cascade: after _mount_message fails
        in the CancelledError handler, _run_agent_task's except clause also
        calls _mount_message(ErrorMessage(...)), which fails the same way.
        """
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            messages_container = app.query_one("#messages", Container)
            await messages_container.remove()

            # Should not raise
            await app._mount_message(ErrorMessage("Agent error: something"))


class TestQueuedMessage:
    """Test QueuedMessage dataclass."""

    def test_frozen(self) -> None:
        """QueuedMessage should be immutable."""
        msg = QueuedMessage(text="hello", mode="normal")
        with pytest.raises(AttributeError):
            msg.text = "changed"  # type: ignore[misc]

    def test_fields(self) -> None:
        """QueuedMessage should store text and mode."""
        msg = QueuedMessage(text="hello", mode="shell")
        assert msg.text == "hello"
        assert msg.mode == "shell"


class TestMessageQueue:
    """Test message queue behavior in BogAgentsApp."""

    async def test_message_queued_when_agent_running(self) -> None:
        """Messages should be queued when agent is running."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._agent_running = True

            app.post_message(ChatInput.Submitted("queued msg", "normal"))
            await pilot.pause()

            assert len(app._pending_messages) == 1
            assert app._pending_messages[0].text == "queued msg"
            assert app._pending_messages[0].mode == "normal"

    async def test_message_queued_while_connecting(self) -> None:
        """Messages submitted during server startup should be queued."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._connecting = True

            app.post_message(ChatInput.Submitted("early msg", "normal"))
            await pilot.pause()

            assert len(app._pending_messages) == 1
            assert app._pending_messages[0].text == "early msg"
            widgets = app.query(QueuedUserMessage)
            assert len(widgets) == 1

    async def test_message_blocked_while_thread_switching(self) -> None:
        """Submissions should be ignored while thread switching is in-flight."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._thread_switching = True
            with patch.object(app, "notify") as notify_mock:
                app.post_message(ChatInput.Submitted("blocked msg", "normal"))
                await pilot.pause()

                assert len(app._pending_messages) == 0
                user_msgs = app.query(UserMessage)
                assert not any(w._content == "blocked msg" for w in user_msgs)
                notify_mock.assert_called_once_with(
                    "Thread switch in progress. Please wait.",
                    severity="warning",
                    timeout=3,
                )

    async def test_queued_widget_mounted(self) -> None:
        """Queued messages should produce a QueuedUserMessage widget."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._agent_running = True

            app.post_message(ChatInput.Submitted("test msg", "normal"))
            await pilot.pause()

            widgets = app.query(QueuedUserMessage)
            assert len(widgets) == 1
            assert len(app._queued_widgets) == 1

    async def test_immediate_processing_when_agent_idle(self) -> None:
        """Messages should process immediately when agent is not running."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert not app._agent_running

            app.post_message(ChatInput.Submitted("direct msg", "normal"))
            await pilot.pause()

            # Should not be queued
            assert len(app._pending_messages) == 0
            # Should be mounted as a regular UserMessage
            user_msgs = app.query(UserMessage)
            assert any(w._content == "direct msg" for w in user_msgs)

    async def test_fifo_order(self) -> None:
        """Queued messages should process in FIFO order."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._agent_running = True

            app.post_message(ChatInput.Submitted("first", "normal"))
            await pilot.pause()
            app.post_message(ChatInput.Submitted("second", "normal"))
            await pilot.pause()

            assert len(app._pending_messages) == 2
            assert app._pending_messages[0].text == "first"
            assert app._pending_messages[1].text == "second"

    async def test_queue_cleared_on_interrupt(self) -> None:
        """Interrupt should clear the message queue."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._agent_running = True
            # Simulate a worker so action_interrupt has something to cancel
            mock_worker = MagicMock()
            app._agent_worker = mock_worker

            app.post_message(ChatInput.Submitted("msg1", "normal"))
            await pilot.pause()
            app.post_message(ChatInput.Submitted("msg2", "normal"))
            await pilot.pause()

            assert len(app._pending_messages) == 2

            # Interrupt (escape key handler)
            app.action_interrupt()

            assert len(app._pending_messages) == 0
            assert len(app._queued_widgets) == 0
            mock_worker.cancel.assert_called_once()

    async def test_interrupt_dismisses_completion_without_stopping_agent(self) -> None:
        """Esc should dismiss completion popup without interrupting the agent."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._agent_running = True
            mock_worker = MagicMock()
            app._agent_worker = mock_worker

            # Activate completion by typing "/"
            chat = app._chat_input
            assert chat is not None
            assert chat._text_area is not None
            chat._text_area.text = "/"
            await pilot.pause()
            assert chat._current_suggestions  # completion is active

            # Esc should dismiss completion, NOT cancel the agent
            app.action_interrupt()

            assert chat._current_suggestions == []
            mock_worker.cancel.assert_not_called()
            assert app._agent_running is True

    async def test_interrupt_falls_through_when_no_completion(self) -> None:
        """Esc should interrupt the agent when completion is not active."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._agent_running = True
            mock_worker = MagicMock()
            app._agent_worker = mock_worker

            # No completion active — interrupt should reach the agent
            chat = app._chat_input
            assert chat is not None
            assert not chat._current_suggestions

            app.action_interrupt()

            mock_worker.cancel.assert_called_once()

    async def test_queue_cleared_on_ctrl_c(self) -> None:
        """Ctrl+C should clear the message queue."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._agent_running = True
            mock_worker = MagicMock()
            app._agent_worker = mock_worker

            app.post_message(ChatInput.Submitted("msg", "normal"))
            await pilot.pause()

            app.action_quit_or_interrupt()

            assert len(app._pending_messages) == 0
            assert len(app._queued_widgets) == 0

    async def test_process_next_from_queue_removes_widget(self) -> None:
        """Processing a queued message should remove its ephemeral widget."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            # Manually enqueue
            app._pending_messages.append(QueuedMessage(text="test", mode="normal"))
            widget = QueuedUserMessage("test")
            messages = app.query_one("#messages", Container)
            await messages.mount(widget)
            app._queued_widgets.append(widget)

            await app._process_next_from_queue()
            await pilot.pause()

            assert len(app._queued_widgets) == 0

    async def test_shell_command_continues_chain(self) -> None:
        """Shell/command messages should not break the queue processing chain."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            # Queue a shell command followed by a normal message
            app._pending_messages.append(QueuedMessage(text="!echo hi", mode="shell"))
            app._pending_messages.append(
                QueuedMessage(text="hello agent", mode="normal")
            )

            await app._process_next_from_queue()
            await pilot.pause()
            await pilot.pause()
            await pilot.pause()  # Extra flush needed on Windows ProactorEventLoop

            # The shell command should have been processed and the normal
            # message should also have been picked up (mounted as UserMessage)
            user_msgs = app.query(UserMessage)
            assert any(w._content == "hello agent" for w in user_msgs)


class TestAskUserLifecycle:
    """Tests for ask_user widget cleanup flows."""

    async def test_request_ask_user_timeout_cleans_old_widget(self) -> None:
        """Timeout cleanup should cancel then remove the previous widget."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            old_widget = MagicMock()
            old_widget.remove = AsyncMock()
            app._pending_ask_user_widget = old_widget

            with patch("bog_agents_cli.app._monotonic", side_effect=[0.0, 31.0]):
                await app._request_ask_user([{"question": "Name?", "type": "text"}])

            old_widget.action_cancel.assert_called_once()
            old_widget.remove.assert_awaited_once()
            assert old_widget.mock_calls[:2] == [call.action_cancel(), call.remove()]
            assert app._pending_ask_user_widget is not old_widget

    async def test_on_ask_user_menu_answered_ignores_remove_errors(self) -> None:
        """Answered handler should swallow remove races and clear tracking."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            widget = MagicMock()
            widget.remove = AsyncMock(side_effect=RuntimeError("already removed"))
            app._pending_ask_user_widget = widget

            await app.on_ask_user_menu_answered(object())
            await pilot.pause()

            assert app._pending_ask_user_widget is None
            widget.remove.assert_awaited_once()

    async def test_on_ask_user_menu_cancelled_ignores_remove_errors(self) -> None:
        """Cancelled handler should swallow remove races and clear tracking."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            widget = MagicMock()
            widget.remove = AsyncMock(side_effect=RuntimeError("already removed"))
            app._pending_ask_user_widget = widget

            await app.on_ask_user_menu_cancelled(object())
            await pilot.pause()

            assert app._pending_ask_user_widget is None
            widget.remove.assert_awaited_once()


class TestTraceCommand:
    """Test /trace slash command."""

    async def test_trace_opens_browser_when_configured(self) -> None:
        """Should open the LangSmith thread URL in the browser."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._session_state = TextualSessionState(thread_id="test-thread-123")

            with (
                patch(
                    "bog_agents_cli.app.build_langsmith_thread_url",
                    return_value="https://smith.langchain.com/o/org/projects/p/proj/t/test-thread-123",
                ),
                patch("bog_agents_cli.app.webbrowser.open") as mock_open,
            ):
                await app._handle_trace_command("/trace")
                await pilot.pause()

            mock_open.assert_called_once_with(
                "https://smith.langchain.com/o/org/projects/p/proj/t/test-thread-123"
            )
            app_msgs = app.query(AppMessage)
            assert any(  # not a URL check—just verifying the link was rendered
                "https://smith.langchain.com/o/org/projects/p/proj/t/test-thread-123"
                in str(w._content)
                for w in app_msgs
            )

    async def test_trace_shows_error_when_not_configured(self) -> None:
        """Should show configuration hint when LangSmith is not set up."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._session_state = TextualSessionState()

            with patch(
                "bog_agents_cli.app.build_langsmith_thread_url",
                return_value=None,
            ):
                await app._handle_trace_command("/trace")
                await pilot.pause()

            app_msgs = app.query(AppMessage)
            assert any("LANGSMITH_API_KEY" in str(w._content) for w in app_msgs)

    async def test_trace_shows_error_when_no_session(self) -> None:
        """Should show error when there is no active session."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._session_state = None

            await app._handle_trace_command("/trace")
            await pilot.pause()

            app_msgs = app.query(AppMessage)
            assert any("No active session" in str(w._content) for w in app_msgs)

    async def test_trace_shows_link_when_browser_fails(self) -> None:
        """Should still display the URL link even if the browser cannot open."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._session_state = TextualSessionState(thread_id="test-thread-123")

            with (
                patch(
                    "bog_agents_cli.app.build_langsmith_thread_url",
                    return_value="https://smith.langchain.com/t/test-thread-123",
                ),
                patch(
                    "bog_agents_cli.app.webbrowser.open",
                    side_effect=webbrowser.Error("no browser"),
                ),
            ):
                await app._handle_trace_command("/trace")
                await pilot.pause()

            app_msgs = app.query(AppMessage)
            assert any(  # not a URL check—just verifying the link was rendered
                "https://smith.langchain.com/t/test-thread-123" in str(w._content)
                for w in app_msgs
            )

    async def test_trace_shows_error_when_url_build_raises(self) -> None:
        """Should show error message when build_langsmith_thread_url raises."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._session_state = TextualSessionState(thread_id="test-thread-123")

            with patch(
                "bog_agents_cli.app.build_langsmith_thread_url",
                side_effect=RuntimeError("SDK error"),
            ):
                await app._handle_trace_command("/trace")
                await pilot.pause()

            app_msgs = app.query(AppMessage)
            assert any("Failed to resolve" in str(w._content) for w in app_msgs)

    async def test_trace_routed_from_handle_command(self) -> None:
        """'/trace' should be correctly routed through _handle_command."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._session_state = None

            await app._handle_command("/trace")
            await pilot.pause()

            app_msgs = app.query(AppMessage)
            assert any("No active session" in str(w._content) for w in app_msgs)


class TestCommandSurfaceEnhancements:
    """Tests for added slash command handlers."""

    async def test_resume_last_uses_most_recent_other_thread(self) -> None:
        """`/resume last` should auto-jump to the latest thread that is not current.

        Bare ``/resume`` now opens the interactive picker — see
        ``test_resume_opens_thread_selector``. The auto-jump shortcut
        moved to the explicit ``/resume last`` (or ``latest`` /
        ``recent``) keyword for users who want a one-keystroke jump.
        """
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._session_state is not None
            current_thread = app._session_state.thread_id

            with (
                patch(
                    "bog_agents_cli.sessions.list_threads",
                    new=AsyncMock(
                        return_value=[
                            {"thread_id": current_thread},
                            {"thread_id": "thread-other"},
                        ]
                    ),
                ),
                patch.object(
                    app, "_resume_thread", new_callable=AsyncMock
                ) as mock_resume,
            ):
                await app._handle_command("/resume last")
                await pilot.pause()

            mock_resume.assert_awaited_once_with("thread-other")

    async def test_resume_opens_thread_selector(self) -> None:
        """Bare `/resume` should open the interactive thread selector."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            with patch.object(
                app, "_show_thread_selector", new_callable=AsyncMock
            ) as mock_picker:
                await app._handle_command("/resume")
                await pilot.pause()

            mock_picker.assert_awaited_once()

    async def test_session_command_shows_session_details(self) -> None:
        """`/session` should render a compact session summary."""
        app = BogAgentsApp(thread_id="thread-session-123")
        async with app.run_test() as pilot:
            await pilot.pause()

            await app._handle_command("/session")
            await pilot.pause()

            app_msgs = app.query(AppMessage)
            assert any(
                "Thread: thread-session-123" in str(w._content) for w in app_msgs
            )
            assert any("Agent: agent" in str(w._content) for w in app_msgs)

    async def test_permissions_command_shows_shell_policy(self) -> None:
        """`/permissions` should display approval state and shell policy."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            with patch.object(app_module.settings, "shell_allow_list", None):
                await app._handle_command("/permissions")
                await pilot.pause()

            app_msgs = app.query(AppMessage)
            assert any(
                "Permission mode: default" in str(w._content) for w in app_msgs
            )
            assert any(
                "Shell allow-list: disabled" in str(w._content) for w in app_msgs
            )

    async def test_keybindings_command_shows_formatted_bindings(self) -> None:
        """`/keybindings` should render the current keybinding config."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            with (
                patch(
                    "bog_agents_cli.keybindings.load_keybindings",
                    return_value=MagicMock(),
                ),
                patch(
                    "bog_agents_cli.keybindings.format_keybindings",
                    return_value="## Keybindings\nsubmit enter",
                ),
            ):
                await app._handle_command("/keybindings")
                await pilot.pause()

            app_msgs = app.query(AppMessage)
            assert any("Keybindings file:" in str(w._content) for w in app_msgs)
            assert any("submit enter" in str(w._content) for w in app_msgs)

    async def test_skills_command_shows_skill_summary(self) -> None:
        """`/skills` should summarize loaded skills and their sources."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            with patch(
                "bog_agents_cli.skills.load.list_skills",
                return_value=[
                    {"name": "alpha", "source": "project"},
                    {"name": "beta", "source": "user"},
                    {"name": "gamma", "source": "built-in"},
                ],
            ):
                await app._handle_command("/skills")
                await pilot.pause()

            app_msgs = app.query(AppMessage)
            assert any("Loaded skills: 3" in str(w._content) for w in app_msgs)
            assert any("Project skills: 1" in str(w._content) for w in app_msgs)
            assert any(
                "Examples: alpha, beta, gamma" in str(w._content) for w in app_msgs
            )

    async def test_help_query_surfaces_matching_commands(self) -> None:
        """`/help <term>` should act like a command browser."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            await app._handle_command("/help perm")
            await pilot.pause()

            app_msgs = app.query(AppMessage)
            assert any("/permissions" in str(w._content) for w in app_msgs)

    async def test_unknown_command_shows_suggestions(self) -> None:
        """Unknown slash commands should suggest close matches."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            await app._handle_command("/permisions")
            await pilot.pause()

            app_msgs = app.query(AppMessage)
            assert any("Closest matches:" in str(w._content) for w in app_msgs)
            assert any("/permissions" in str(w._content) for w in app_msgs)

    async def test_cost_alias_routes_to_token_view(self) -> None:
        """`/cost` should reuse the `/tokens` handler."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            await app._handle_command("/cost")
            await pilot.pause()

            app_msgs = app.query(AppMessage)
            assert any("No token usage yet" in str(w._content) for w in app_msgs)

    async def test_doctor_command_runs_local_diagnostics(self) -> None:
        """`/doctor` should render the diagnostic report."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            with patch("bog_agents_cli.doctor.run_doctor", return_value="doctor ok"):
                await app._handle_command("/doctor")
                await pilot.pause()

            app_msgs = app.query(AppMessage)
            assert any("doctor ok" in str(w._content) for w in app_msgs)

    async def test_review_command_builds_prompt_and_sends_to_agent(self) -> None:
        """`/review` should generate a review prompt and send it."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            with (
                patch(
                    "bog_agents_cli.review_command.generate_review_prompt",
                    return_value="review prompt",
                ),
                patch.object(
                    app, "_send_prompt_to_agent", new=AsyncMock()
                ) as mock_send,
            ):
                await app._handle_command("/review src/foo.py")
                await pilot.pause()

            mock_send.assert_awaited_once_with("review prompt")
            app_msgs = app.query(AppMessage)
            assert any(
                "Starting structured code review" in str(w._content) for w in app_msgs
            )

    async def test_profile_command_applies_runtime_controls(self) -> None:
        """`/profile` should update the session runtime controls."""
        from bog_agents_cli.profiles import Profile

        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            with (
                patch(
                    "bog_agents_cli.profiles.load_profiles",
                    return_value={
                        "review": Profile(
                            name="review",
                            description="Review mode",
                            model="openai:gpt-5.4-mini",
                            effort_level="max",
                            auto_approve=False,
                            plan_mode=True,
                            system_prompt_append="Review carefully.",
                        )
                    },
                ),
                patch.object(
                    app,
                    "_apply_runtime_model_override",
                    new=AsyncMock(return_value="openai:gpt-5.4-mini"),
                ),
            ):
                await app._handle_command("/profile review")
                await pilot.pause()

            assert app._active_profile_name == "review"
            assert app._plan_mode_enabled is True
            assert app._effort_level == "max"
            assert app._active_profile_prompt == "Review carefully."
            app_msgs = app.query(AppMessage)
            assert any("Profile activated: review" in str(w._content) for w in app_msgs)

    async def test_plan_command_toggles_runtime_plan_mode(self) -> None:
        """`/plan toggle` should flip the runtime plan mode flag."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            await app._handle_command("/plan toggle")
            await pilot.pause()
            assert app._plan_mode_enabled is True

            await app._handle_command("/plan off")
            await pilot.pause()
            assert app._plan_mode_enabled is False

    async def test_effort_command_sets_runtime_preset(self) -> None:
        """`/effort` should persist the selected runtime effort preset."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            await app._handle_command("/effort low")
            await pilot.pause()

            assert app._effort_level == "low"
            app_msgs = app.query(AppMessage)
            assert any("Effort set to low" in str(w._content) for w in app_msgs)

    async def test_diff_command_renders_git_output(self) -> None:
        """`/diff` should show the git diff output when available."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            with patch.object(
                app,
                "_run_git",
                new=AsyncMock(return_value=(True, "diff --git a/app.py b/app.py")),
            ):
                await app._handle_command("/diff")
                await pilot.pause()

            app_msgs = app.query(AppMessage)
            assert any(
                "diff --git a/app.py b/app.py" in str(w._content) for w in app_msgs
            )

    async def test_health_command_builds_prompt_and_sends(self) -> None:
        """`/health` should build a prompt and forward it to the agent."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            with patch.object(
                app, "_send_prompt_to_agent", new=AsyncMock()
            ) as mock_send:
                await app._handle_command("/health quick libs/cli")
                await pilot.pause()

            mock_send.assert_awaited_once()
            assert mock_send.await_args is not None
            assert "Analyze the health of libs/cli" in mock_send.await_args.args[0]

    async def test_test_command_generate_builds_prompt_and_sends(self) -> None:
        """`/test generate` should construct a test-generation prompt."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            with patch.object(
                app, "_send_prompt_to_agent", new=AsyncMock()
            ) as mock_send:
                await app._handle_command("/test generate src/app.py pytest")
                await pilot.pause()

            mock_send.assert_awaited_once()
            assert mock_send.await_args is not None
            assert (
                "Generate comprehensive unit tests for src/app.py"
                in mock_send.await_args.args[0]
            )

    async def test_resolve_command_detects_conflicts_and_sends_prompt(self) -> None:
        """`/resolve` should inspect git conflicts and send a resolution prompt."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            with (
                patch.object(
                    app,
                    "_run_git",
                    new=AsyncMock(return_value=(True, "foo.py\nbar.py\n")),
                ),
                patch.object(
                    app, "_send_prompt_to_agent", new=AsyncMock()
                ) as mock_send,
            ):
                await app._handle_command("/resolve")
                await pilot.pause()

            mock_send.assert_awaited_once()
            assert mock_send.await_args is not None
            prompt = mock_send.await_args.args[0]
            assert "foo.py" in prompt
            assert "bar.py" in prompt

    async def test_branch_command_create_uses_git_switch(self) -> None:
        """`/branch create` should dispatch to `git switch -c`."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            with (
                patch.object(
                    app,
                    "_get_repo_root",
                    new=AsyncMock(return_value=app_module.Path("E:/Code/bog-agents")),
                ),
                patch("bog_agents_cli.app._get_git_branch", return_value="main"),
                patch.object(
                    app,
                    "_run_git",
                    new=AsyncMock(return_value=(True, "Switched to a new branch")),
                ) as mock_git,
            ):
                await app._handle_command("/branch create feature/test")
                await pilot.pause()

            mock_git.assert_awaited_once()
            assert mock_git.await_args is not None
            assert mock_git.await_args.args[0] == ["switch", "-c", "feature/test"]

    async def test_undo_command_restore_uses_git_restore(self) -> None:
        """`/undo restore` should dispatch to `git restore`."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            with (
                patch.object(
                    app,
                    "_get_repo_root",
                    new=AsyncMock(return_value=app_module.Path("E:/Code/bog-agents")),
                ),
                patch("bog_agents_cli.app._get_git_branch", return_value="main"),
                patch.object(
                    app,
                    "_run_git",
                    new=AsyncMock(
                        side_effect=[
                            (True, ""),
                            (True, " M other.py"),
                        ]
                    ),
                ) as mock_git,
            ):
                await app._handle_command("/undo restore src/app.py")
                await pilot.pause()

            assert mock_git.await_count == 2
            assert mock_git.await_args_list[0].args[0] == [
                "restore",
                "--source=HEAD",
                "--staged",
                "--worktree",
                "--",
                "src/app.py",
            ]

    async def test_preview_command_tracks_server_lifecycle(self) -> None:
        """`/preview` should start, list, and stop tracked preview servers."""
        app = BogAgentsApp()
        process = MagicMock()
        process.returncode = None
        process.wait = AsyncMock(return_value=0)
        process.terminate = MagicMock()
        process.kill = MagicMock()

        async with app.run_test() as pilot:
            await pilot.pause()

            with (
                patch(
                    "bog_agents_cli.app.asyncio.create_subprocess_shell",
                    new=AsyncMock(return_value=process),
                ),
                patch.object(webbrowser, "open"),
            ):
                await app._handle_command("/preview start npm run dev --port 3000")
                await pilot.pause()
                await app._handle_command("/preview")
                await pilot.pause()
                await app._handle_command("/preview stop all")
                await pilot.pause()

            app_msgs = app.query(AppMessage)
            assert any("Started preview server" in str(w._content) for w in app_msgs)
            assert any("Preview servers:" in str(w._content) for w in app_msgs)
            assert any("Stopped preview server(s)" in str(w._content) for w in app_msgs)
            process.terminate.assert_called_once()

    async def test_record_and_replay_commands_round_trip(self) -> None:
        """`/record` and `/replay run` should capture and reuse replay sessions.

        The new flow attaches a live SessionRecorder to the recording state
        and feeds it from ``_mount_message``. We exercise that path by
        mounting a real UserMessage between start and stop.
        """
        from bog_agents_cli.replay import ReplaySession, ReplayStep
        from bog_agents_cli.widgets.messages import UserMessage as _UserMsg

        app = BogAgentsApp(thread_id="thread-123")
        replay_session = ReplaySession(
            session_id="replay-abc123",
            name="bugfix-flow",
            recorded_at=1_700_000_000.0,
            description="Recorded from thread thread-123",
            steps=[ReplayStep(kind="user_message", content="Investigate the bug")],
        )
        async with app.run_test() as pilot:
            await pilot.pause()

            with (
                patch(
                    "bog_agents_cli.replay.save_replay_session",
                    return_value=app_module.Path(
                        "E:/Code/bog-agents/.tmp/replays/replay-abc123.yaml"
                    ),
                ),
                patch(
                    "bog_agents_cli.replay.save_drive_script_for_session",
                    return_value=app_module.Path(
                        "E:/Code/bog-agents/.tmp/replays/replay-abc123.drive.yaml"
                    ),
                ),
            ):
                await app._handle_command("/record start bugfix-flow")
                await pilot.pause()
                # Drive a real user message through the live capture path.
                await app._mount_message(_UserMsg("Investigate the bug"))
                await pilot.pause()
                await app._handle_command("/record stop")
                await pilot.pause()

            # Live recorder should have captured at least one step.
            assert app._recording_state is None  # cleared after stop

            with (
                patch(
                    "bog_agents_cli.replay.list_replay_sessions",
                    return_value=[replay_session],
                ),
                patch(
                    "bog_agents_cli.replay.save_drive_script_for_session",
                    return_value=app_module.Path(
                        "E:/Code/bog-agents/.tmp/replays/replay-abc123.drive.yaml"
                    ),
                ),
                patch.object(
                    app, "_send_prompt_to_agent", new=AsyncMock()
                ) as mock_send,
            ):
                await app._handle_command("/replay run bugfix-flow")
                await pilot.pause()

            # /replay run now drives each recorded user message
            # individually through the agent rather than baking the
            # session into a single prose prompt (the old
            # build_replay_prompt path). One user_message in the
            # fixture -> one send.
            mock_send.assert_awaited_once()
            assert mock_send.await_args is not None
            assert "Investigate the bug" in mock_send.await_args.args[0]
            app_msgs = app.query(AppMessage)
            assert any("Saved replay" in str(w._content) for w in app_msgs)

    async def test_agent_command_spawn_uses_background_manager(self) -> None:
        """`/agent spawn` should submit work to the background manager."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            with (
                patch.object(
                    app,
                    "_ensure_background_manager",
                    new=AsyncMock(),
                ),
                patch.object(
                    app,
                    "_submit_managed_local_task",
                    new=AsyncMock(return_value="bg-001"),
                ) as mock_submit,
            ):
                await app._handle_command("/agent spawn investigate auth flow")
            await pilot.pause()

            assert mock_submit.await_count == 1
            assert mock_submit.await_args is not None
            assert mock_submit.await_args.args[0] == "investigate auth flow"
            assert mock_submit.await_args.kwargs["strategy"] == "local"
            app_msgs = app.query(AppMessage)
            assert any(
                "Spawned 1 managed agent task(s)" in str(w._content) for w in app_msgs
            )

    async def test_agent_command_spawn_remote_batch_tracks_remote_tasks(self) -> None:
        """`/agent spawn --remote --count` should create multiple tracked tasks."""
        app = BogAgentsApp()
        remote_tasks = [
            MagicMock(task_id="run-101", prompt="inspect repo"),
            MagicMock(task_id="run-102", prompt="inspect repo"),
        ]
        async with app.run_test() as pilot:
            await pilot.pause()

            with (
                patch.object(
                    app,
                    "_ensure_background_manager",
                    new=AsyncMock(),
                ),
                patch(
                    "bog_agents_cli.remote.load_remote_config",
                    return_value=MagicMock(),
                ),
                patch(
                    "bog_agents_cli.remote.submit_remote_task",
                    new=AsyncMock(side_effect=remote_tasks),
                ) as mock_submit,
                patch("bog_agents_cli.remote.save_remote_tasks"),
            ):
                await app._handle_command(
                    "/agent spawn --remote --count 2 --label scout inspect repo"
                )
                await pilot.pause()

            assert set(app._remote_tasks) == {"run-101", "run-102"}
            assert mock_submit.await_count == 2
            assert mock_submit.await_args_list[0].kwargs["label"] == "scout"
            assert mock_submit.await_args_list[1].kwargs["label"] == "scout #2"

    async def test_agent_command_spawn_worktree_creates_isolated_branch(
        self, tmp_path: app_module.Path
    ) -> None:
        """`/agent spawn --worktree` should create a worktree-backed task."""
        worktree_path = tmp_path / "review-branch"
        repo_path = tmp_path / "repo"
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            worktree = MagicMock(path=worktree_path)
            with (
                patch.object(
                    app,
                    "_ensure_background_manager",
                    new=AsyncMock(),
                ),
                patch.object(
                    app,
                    "_get_repo_root",
                    new=AsyncMock(return_value=repo_path),
                ),
                patch(
                    "bog_agents.middleware.worktree.create_worktree",
                    return_value=worktree,
                ),
                patch.object(
                    app,
                    "_submit_managed_local_task",
                    new=AsyncMock(return_value="bg-002"),
                ) as mock_submit,
            ):
                await app._handle_command(
                    "/agent spawn --worktree --label review inspect repo"
                )
                await pilot.pause()

            assert mock_submit.await_count == 1
            assert mock_submit.await_args is not None
            assert mock_submit.await_args.kwargs["strategy"] == "worktree"
            assert mock_submit.await_args.kwargs["working_dir"] == str(worktree_path)
            assert mock_submit.await_args.kwargs["worktree_branch"].startswith(
                "agent/review-"
            )

    async def test_background_runner_uses_cli_agent_factory(self) -> None:
        """Managed background work should use the CLI-configured agent factory."""
        app = BogAgentsApp()
        mock_graph = MagicMock()
        mock_graph.ainvoke = AsyncMock(return_value={"result": "done"})
        task = SimpleNamespace(
            task_id="bg-123",
            prompt="inspect repo",
            model="openai:gpt-5.4",
            working_dir="E:/repo",
        )

        with patch(
            "bog_agents_cli.agent.create_cli_agent",
            return_value=(mock_graph, MagicMock()),
        ) as mock_create:
            result = await app._build_background_runner()(task)

        assert result == {"result": "done"}
        assert mock_create.call_args is not None
        assert mock_create.call_args.kwargs["assistant_id"] == app._assistant_id
        assert mock_create.call_args.kwargs["cwd"] == "E:/repo"
        mock_graph.ainvoke.assert_awaited_once()

    async def test_background_command_uses_managed_local_runner(self) -> None:
        """`/background` should reuse the managed local task path."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            with (
                patch.object(
                    app,
                    "_submit_managed_local_task",
                    new=AsyncMock(return_value="bg-201"),
                ) as mock_submit,
                patch.object(
                    app,
                    "_ensure_background_manager",
                    new=AsyncMock(),
                ),
            ):
                await app._handle_command("/background inspect the repository")
                await pilot.pause()

            assert mock_submit.await_count == 1
            assert mock_submit.await_args is not None
            assert mock_submit.await_args.args[0] == "inspect the repository"
            assert mock_submit.await_args.kwargs["strategy"] == "background"
            assert mock_submit.await_args.kwargs["metadata"] == {
                "command": "/background",
                "effective_prompt": "inspect the repository",
            }
            app_msgs = app.query(AppMessage)
            assert any(
                "Background task submitted: bg-201" in str(w._content) for w in app_msgs
            )

    async def test_agent_spawn_inherits_active_team_context(
        self, tmp_path: Path
    ) -> None:
        """`/agent spawn` should attach active team metadata and prompt context."""
        from bog_agents_cli.team_orchestration import (
            TeamMember,
            TeamMessage,
            TeamProfile,
            TeamRegistry,
            save_team_registry,
        )

        save_team_registry(
            TeamRegistry(
                active_team="Core",
                teams=[
                    TeamProfile(
                        name="Core",
                        summary="Stabilize release readiness",
                        members=[TeamMember(name="Scout", role="reviewer")],
                        messages=[
                            TeamMessage(
                                body="Prioritize install and runtime regressions",
                                sender="lead",
                            )
                        ],
                    )
                ],
            ),
            tmp_path,
        )

        app = BogAgentsApp()
        app._cwd = tmp_path
        async with app.run_test() as pilot:
            await pilot.pause()

            with (
                patch.object(
                    app,
                    "_ensure_background_manager",
                    new=AsyncMock(),
                ),
                patch.object(
                    app,
                    "_submit_managed_local_task",
                    new=AsyncMock(return_value="bg-777"),
                ) as mock_submit,
            ):
                await app._handle_command("/agent spawn inspect repo")
                await pilot.pause()

            metadata = mock_submit.await_args.kwargs["metadata"]
            assert metadata["team_name"] == "Core"
            assert (
                "Shared summary: Stabilize release readiness" in metadata["team_brief"]
            )
            assert metadata["effective_prompt"].startswith("# Team coordination brief")
            assert metadata["effective_prompt"].endswith(
                "# Assigned task\ninspect repo"
            )
            app_msgs = app.query(AppMessage)
            assert any("[Core]" in str(w._content) for w in app_msgs)

    async def test_team_command_create_summary_and_status(self, tmp_path: Path) -> None:
        """`/team` should persist created teams and mixed-case summary updates."""
        from bog_agents_cli.team_orchestration import load_team_registry

        app = BogAgentsApp()
        app._cwd = tmp_path
        async with app.run_test() as pilot:
            await pilot.pause()

            await app._handle_command("/team create Core")
            await app._handle_command("/team add-member Core scout reviewer")
            await app._handle_command(
                "/team summary Core set Release coordination lane"
            )
            await app._handle_command("/team status core")
            await pilot.pause()

        registry = load_team_registry(tmp_path)
        assert registry.active_team == "Core"
        assert len(registry.teams) == 1
        assert registry.teams[0].summary == "Release coordination lane"
        assert registry.teams[0].members[0].name == "scout"
        assert registry.teams[0].members[0].role == "reviewer"
        status_text = app._build_team_status("Core")
        assert "Release coordination lane" in status_text
        assert "scout (reviewer)" in status_text

    async def test_team_command_message_to_task_queues_inbox(
        self, tmp_path: Path
    ) -> None:
        """`/team message <task-id>` should queue inbox work and persist a team note."""
        from bog_agents_cli.team_orchestration import load_team_registry

        app = BogAgentsApp()
        app._cwd = tmp_path
        task = SimpleNamespace(task_id="bg-404", metadata={"team_name": "Core"})
        app._bg_manager = MagicMock()
        app._bg_manager.get_status.return_value = task

        async with app.run_test() as pilot:
            await pilot.pause()

            await app._handle_command("/team message bg-404 Please verify the fix")
            await pilot.pause()

        inbox = task.metadata["inbox"]
        assert len(inbox) == 1
        assert inbox[0]["body"] == "Please verify the fix"
        registry = load_team_registry(tmp_path)
        assert registry.teams[0].name == "Core"
        assert registry.teams[0].messages[-1].body == "Please verify the fix"
        assert app._task_inbox_count(task) == 1

    async def test_team_command_sync_updates_shared_summary(
        self, tmp_path: Path
    ) -> None:
        """`/team sync` should summarize existing team notes and worker results."""
        from bog_agents_cli.team_orchestration import (
            TeamMessage,
            TeamProfile,
            TeamRegistry,
            load_team_registry,
            save_team_registry,
        )

        save_team_registry(
            TeamRegistry(
                active_team="Core",
                teams=[
                    TeamProfile(
                        name="Core",
                        messages=[TeamMessage(body="Focus on install regressions")],
                    )
                ],
            ),
            tmp_path,
        )

        app = BogAgentsApp()
        app._cwd = tmp_path
        app._bg_manager = MagicMock()
        app._bg_manager.all_tasks = [
            SimpleNamespace(
                metadata={"team_name": "Core"},
                result="Resolved the provider fallback failure.",
                output="",
                status_line="[bg-201] completed",
            )
        ]

        async with app.run_test() as pilot:
            await pilot.pause()

            await app._handle_command("/team sync Core")
            await pilot.pause()

        registry = load_team_registry(tmp_path)
        assert "Focus on install regressions" in registry.teams[0].summary
        assert "Resolved the provider fallback failure." in registry.teams[0].summary
        status_text = app._build_team_status("Core")
        assert "Resolved the provider fallback failure." in status_text

    async def test_worktree_command_lists_current_worktrees(self) -> None:
        """`/worktree` should render known git worktrees."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            with (
                patch.object(
                    app,
                    "_get_repo_root",
                    new=AsyncMock(return_value=app_module.Path("E:/repo")),
                ),
                patch(
                    "bog_agents.middleware.worktree.list_worktrees",
                    return_value=[
                        MagicMock(branch="main", path="E:/repo", is_main=True),
                        MagicMock(
                            branch="feature/x",
                            path="E:/tmp/feature-x",
                            is_main=False,
                        ),
                    ],
                ),
            ):
                await app._handle_command("/worktree")
                await pilot.pause()

            app_msgs = app.query(AppMessage)
            assert any("feature/x" in str(w._content) for w in app_msgs)

    async def test_worktree_command_merges_branch_into_target(self) -> None:
        """`/worktree merge` should checkout target and merge source branch."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            with (
                patch.object(
                    app,
                    "_get_repo_root",
                    new=AsyncMock(return_value=app_module.Path("E:/repo")),
                ),
                patch.object(
                    app,
                    "_run_git",
                    new=AsyncMock(side_effect=[(True, "Switched"), (True, "Merged")]),
                ) as mock_run_git,
            ):
                await app._handle_command("/worktree merge feature/x main")
                await pilot.pause()

            assert mock_run_git.await_args_list == [
                call(["checkout", "main"], cwd=app_module.Path("E:/repo")),
                call(["merge", "feature/x"], cwd=app_module.Path("E:/repo")),
            ]
            app_msgs = app.query(AppMessage)
            assert any(
                "Merged feature/x into main." in str(w._content) for w in app_msgs
            )

    async def test_plugin_command_lists_plugins_and_extensions(self) -> None:
        """`/plugin` should summarize both plugins and extensions."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            with patch(
                "bog_agents_cli.extensibility.format_extensibility_list",
                return_value=(
                    "Plugins\n  [enabled] formatter v1.0.0\n\n"
                    "Extensions\nNo extensions installed."
                ),
            ):
                await app._handle_command("/plugin")
                await pilot.pause()

            app_msgs = app.query(AppMessage)
            assert any("formatter v1.0.0" in str(w._content) for w in app_msgs)

    async def test_plugin_info_shows_package_details(self) -> None:
        """`/plugin info` should show unified package metadata."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            with patch(
                "bog_agents_cli.extensibility.find_extensibility_item",
                return_value=MagicMock(
                    name="formatter",
                    kind="extension",
                    version="1.2.0",
                    description="Formatting helpers",
                    author="Bog",
                    homepage="https://example.com",
                    enabled=True,
                    install_path=app_module.Path("E:/plugins/formatter"),
                    skills=("format",),
                    commands=("/format",),
                ),
            ):
                await app._handle_command("/plugin info formatter")
                await pilot.pause()

            app_msgs = app.query(AppMessage)
            assert any("Type: extension" in str(w._content) for w in app_msgs)
            assert any("Commands: /format" in str(w._content) for w in app_msgs)

    async def test_resume_project_uses_metadata_matches(self) -> None:
        """`/resume project` should switch to the newest matching thread."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            with (
                patch(
                    "bog_agents_cli.sessions.find_threads_with_metadata",
                    new=AsyncMock(
                        return_value=[
                            {"thread_id": "project-thread", "project": "sdk"},
                        ]
                    ),
                ),
                patch.object(
                    app, "_resume_thread", new_callable=AsyncMock
                ) as mock_resume,
            ):
                await app._handle_command("/resume project sdk")
                await pilot.pause()

            mock_resume.assert_awaited_once_with("project-thread")

    async def test_session_rename_persists_metadata(self) -> None:
        """`/session rename` should persist the thread label."""
        app = BogAgentsApp(thread_id="thread-session-123")
        async with app.run_test() as pilot:
            await pilot.pause()

            with patch(
                "bog_agents_cli.sessions.set_thread_label",
                new=AsyncMock(),
            ) as mock_set_label:
                await app._handle_command("/session rename Launch Prep")
                await pilot.pause()

            assert app._session_name == "Launch Prep"
            mock_set_label.assert_awaited_once_with("thread-session-123", "Launch Prep")

    async def test_session_export_writes_json(self, tmp_path: app_module.Path) -> None:
        """`/session export` should write a JSON export file."""
        app = BogAgentsApp(thread_id="thread-export-123")
        export_path = tmp_path / "tmp-session-export.json"
        async with app.run_test() as pilot:
            await pilot.pause()

            with patch(
                "bog_agents_cli.sessions.export_thread",
                new=AsyncMock(
                    return_value={"thread": {"thread_id": "thread-export-123"}}
                ),
            ):
                await app._handle_command(f"/session export {export_path}")
                await pilot.pause()

        assert export_path.exists()
        assert '"thread-export-123"' in export_path.read_text(encoding="utf-8")

    async def test_rewind_lists_available_checkpoints(self) -> None:
        """`/rewind` should render a numbered checkpoint browser."""
        agent = MagicMock()
        agent.aget_state = AsyncMock(return_value=SimpleNamespace(values={}))
        app = BogAgentsApp(agent=agent, thread_id="thread-rewind-123")
        checkpoints = [
            {
                "checkpoint_id": "checkpoint-001",
                "updated_at": "2026-04-12T12:00:00+00:00",
                "message_count": 6,
                "initial_prompt": "Investigate why release validation failed",
            }
        ]
        async with app.run_test() as pilot:
            await pilot.pause()

            with patch(
                "bog_agents_cli.sessions.list_thread_checkpoints",
                new=AsyncMock(return_value=checkpoints),
            ):
                await app._handle_command("/rewind")
                await pilot.pause()

            app_msgs = app.query(AppMessage)
            assert any(
                "Checkpoint history for thread-rewind-123" in str(w._content)
                for w in app_msgs
            )
            assert any(
                "Investigate why release validation failed" in str(w._content)
                for w in app_msgs
            )

    async def test_rewind_to_checkpoint_forks_new_thread(self) -> None:
        """`/rewind to` should seed a new thread from the selected checkpoint."""
        agent = MagicMock()
        agent.aget_state = AsyncMock(return_value=SimpleNamespace(values={}))
        agent.aupdate_state = AsyncMock()
        app = BogAgentsApp(agent=agent, thread_id="thread-rewind-123")
        checkpoints = [
            {
                "checkpoint_id": "checkpoint-001",
                "updated_at": "2026-04-12T12:00:00+00:00",
                "message_count": 3,
                "initial_prompt": "First prompt",
            },
            {
                "checkpoint_id": "checkpoint-002",
                "updated_at": "2026-04-12T12:05:00+00:00",
                "message_count": 8,
                "initial_prompt": "Second prompt",
            },
        ]
        payload = {
            "checkpoint_id": "checkpoint-002",
            "thread_id": "thread-rewind-123",
            "message_count": 8,
            "initial_prompt": "Second prompt",
            "messages": ["stub-message"],
        }
        async with app.run_test() as pilot:
            await pilot.pause()

            with (
                patch(
                    "bog_agents_cli.sessions.list_thread_checkpoints",
                    new=AsyncMock(return_value=checkpoints),
                ),
                patch(
                    "bog_agents_cli.sessions.get_thread_checkpoint_payload",
                    new=AsyncMock(return_value=payload),
                ),
                patch(
                    "bog_agents_cli.sessions.get_thread_metadata",
                    new=AsyncMock(
                        return_value={
                            "label": "Launch Prep",
                            "project": "release",
                            "tags": ["prod"],
                        }
                    ),
                ),
                patch(
                    "bog_agents_cli.sessions.set_thread_label",
                    new=AsyncMock(),
                ) as mock_set_label,
                patch(
                    "bog_agents_cli.sessions.set_thread_project",
                    new=AsyncMock(),
                ) as mock_set_project,
                patch(
                    "bog_agents_cli.sessions.set_thread_tags",
                    new=AsyncMock(),
                ) as mock_set_tags,
                patch.object(
                    app, "_resume_thread", new_callable=AsyncMock
                ) as mock_resume,
            ):
                await app._handle_command("/rewind to 2")
                await pilot.pause()

            assert agent.aupdate_state.await_count == 1
            assert agent.aupdate_state.await_args is not None
            new_thread_id = agent.aupdate_state.await_args.args[0]["configurable"][
                "thread_id"
            ]
            assert new_thread_id != "thread-rewind-123"
            mock_resume.assert_awaited_once_with(new_thread_id)
            mock_set_label.assert_awaited_once_with(
                new_thread_id, "Launch Prep (rewind)"
            )
            mock_set_project.assert_awaited_once_with(new_thread_id, "release")
            mock_set_tags.assert_awaited_once()

    async def test_unknown_extension_command_sends_prompt(self) -> None:
        """Unknown slash commands should route through enabled extension commands."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            with (
                patch(
                    "bog_agents_cli.extensibility.find_extension_command",
                    return_value=MagicMock(
                        name="/scout",
                        extension_name="review-pack",
                        prompt_template="Scout this repo: {args}",
                    ),
                ),
                patch(
                    "bog_agents_cli.extensibility.render_extension_command_prompt",
                    return_value="Scout this repo: services/api",
                ),
                patch.object(
                    app, "_send_prompt_to_agent", new_callable=AsyncMock
                ) as mock_send,
            ):
                await app._handle_command("/scout services/api")
                await pilot.pause()

            mock_send.assert_awaited_once_with("Scout this repo: services/api")

    async def test_remote_command_submit_tracks_task(self) -> None:
        """`/remote submit` should add the returned task to app state."""
        remote_task = MagicMock(task_id="run-123", prompt="ship it")
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            with (
                patch(
                    "bog_agents_cli.remote.load_remote_config",
                    return_value=MagicMock(),
                ),
                patch(
                    "bog_agents_cli.remote.submit_remote_task",
                    new=AsyncMock(return_value=remote_task),
                ),
                patch("bog_agents_cli.remote.save_remote_tasks"),
                patch(
                    "bog_agents_cli.remote.format_remote_tasks",
                    return_value="[>>>] run-123: ship it",
                ),
            ):
                await app._handle_command(
                    "/remote submit --label scout --model gpt-5.4 ship it"
                )
                await pilot.pause()

            assert app._remote_tasks["run-123"] is remote_task
            app_msgs = app.query(AppMessage)
            assert any("run-123" in str(w._content) for w in app_msgs)

    async def test_remote_command_submit_passes_label_and_model(self) -> None:
        """`/remote submit` should forward parsed label and model overrides."""
        remote_task = MagicMock(task_id="run-123", prompt="ship it")
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            with (
                patch(
                    "bog_agents_cli.remote.load_remote_config",
                    return_value=MagicMock(),
                ),
                patch(
                    "bog_agents_cli.remote.submit_remote_task",
                    new=AsyncMock(return_value=remote_task),
                ) as mock_submit,
                patch("bog_agents_cli.remote.save_remote_tasks"),
                patch(
                    "bog_agents_cli.remote.format_remote_tasks",
                    return_value="[>>>] run-123: ship it",
                ),
            ):
                await app._handle_command(
                    "/remote submit --label scout --model gpt-5.4 ship it"
                )
                await pilot.pause()

            assert mock_submit.await_count == 1
            assert mock_submit.await_args is not None
            assert mock_submit.await_args.kwargs["label"] == "scout"
            assert mock_submit.await_args.kwargs["model"] == "gpt-5.4"

    async def test_remote_command_cleanup_removes_finished_tasks(self) -> None:
        """`/remote cleanup` should drop completed remote tasks from tracking."""
        from bog_agents_cli.remote import RemoteStatus, RemoteTask

        app = BogAgentsApp()
        app._remote_tasks = {
            "run-1": RemoteTask("run-1", "active", status=RemoteStatus.RUNNING),
            "run-2": RemoteTask("run-2", "done", status=RemoteStatus.COMPLETED),
            "run-3": RemoteTask("run-3", "failed", status=RemoteStatus.FAILED),
        }
        async with app.run_test() as pilot:
            await pilot.pause()

            with (
                patch(
                    "bog_agents_cli.remote.load_remote_config",
                    return_value=MagicMock(),
                ),
                patch("bog_agents_cli.remote.save_remote_tasks"),
            ):
                await app._handle_command("/remote cleanup")
                await pilot.pause()

            assert set(app._remote_tasks) == {"run-1"}
            app_msgs = app.query(AppMessage)
            assert any(
                "Removed 2 completed remote task(s)." in str(w._content)
                for w in app_msgs
            )

    async def test_remote_command_stop_cancels_task(self) -> None:
        """`/remote stop` should invoke provider-level task cancellation."""
        remote_task = MagicMock(task_id="run-123", prompt="ship it")
        remote_task.status = "running"
        app = BogAgentsApp()
        app._remote_tasks = {"run-123": remote_task}
        async with app.run_test() as pilot:
            await pilot.pause()

            with (
                patch(
                    "bog_agents_cli.remote.load_remote_config",
                    return_value=MagicMock(),
                ),
                patch(
                    "bog_agents_cli.remote.cancel_remote_task",
                    new=AsyncMock(return_value=remote_task),
                ) as mock_cancel,
                patch("bog_agents_cli.remote.save_remote_tasks"),
                patch(
                    "bog_agents_cli.remote.format_remote_tasks",
                    return_value="[---] run-123: cancelled",
                ),
            ):
                await app._handle_command("/remote stop run-123")
                await pilot.pause()

            assert mock_cancel.await_count == 1
            app_msgs = app.query(AppMessage)
            assert any("run-123" in str(w._content) for w in app_msgs)

    async def test_remote_command_reattach_loads_persisted_task(self) -> None:
        """`/remote reattach` should recover a task from persisted state."""
        from bog_agents_cli.remote import RemoteTask

        remote_task = RemoteTask(
            "run-999",
            "ship it",
            metadata={"provider": "ssh", "status_file": "/srv/status.json"},
        )
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            with (
                patch(
                    "bog_agents_cli.remote.load_remote_config",
                    return_value=MagicMock(),
                ),
                patch(
                    "bog_agents_cli.remote.load_remote_tasks",
                    return_value=[remote_task],
                ),
                patch(
                    "bog_agents_cli.remote.check_remote_task",
                    new=AsyncMock(return_value=remote_task),
                ),
                patch("bog_agents_cli.remote.save_remote_tasks"),
                patch(
                    "bog_agents_cli.remote.format_remote_tasks",
                    return_value="[>>>] run-999: ship it",
                ),
            ):
                await app._handle_command("/remote reattach run-999")
                await pilot.pause()

            assert app._remote_tasks["run-999"] is remote_task
            app_msgs = app.query(AppMessage)
            assert any("run-999" in str(w._content) for w in app_msgs)

    def test_handler_registry_covers_supported_commands(self) -> None:
        """Every supported command and alias should have a handler mapping."""
        from bog_agents_cli.command_registry import get_registered_command_names
        from bog_agents_cli.commands import COMMAND_HANDLER_MAP

        supported_names = set(get_registered_command_names(include_aliases=True))
        handler_names = set(COMMAND_HANDLER_MAP)
        assert supported_names <= handler_names


class TestRunAgentTaskMediaTracker:
    """Tests image tracker wiring from app into textual execution."""

    async def test_run_agent_task_passes_image_tracker(self) -> None:
        """`_run_agent_task` should forward the shared image tracker."""
        app = BogAgentsApp(agent=MagicMock())
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._ui_adapter is not None

            with patch(
                "bog_agents_cli.app.execute_task_textual", new_callable=AsyncMock
            ) as mock_execute:
                await app._run_agent_task("hello")

            mock_execute.assert_awaited_once()
            assert mock_execute.await_args is not None
            assert mock_execute.await_args.kwargs["image_tracker"] is app._image_tracker

    async def test_run_agent_task_finalizes_pending_tools_on_error(self) -> None:
        """Unexpected agent errors should stop/clear in-flight tool widgets."""
        app = BogAgentsApp(agent=MagicMock())
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._ui_adapter is not None

            pending_tool = MagicMock()
            app._ui_adapter._current_tool_messages = {"tool-1": pending_tool}

            with patch(
                "bog_agents_cli.app.execute_task_textual",
                new_callable=AsyncMock,
                side_effect=RuntimeError("boom"),
            ):
                await app._run_agent_task("hello")
                await pilot.pause()

            pending_tool.set_error.assert_called_once_with("Agent error: boom")
            assert app._ui_adapter._current_tool_messages == {}

            errors = app.query(ErrorMessage)
            assert any("Agent error: boom" in str(w._content) for w in errors)

    async def test_run_agent_task_surfaces_tool_capability_help(self) -> None:
        """Tool capability failures should not be mislabeled as auth errors."""
        app = BogAgentsApp(agent=MagicMock())
        async with app.run_test() as pilot:
            await pilot.pause()

            with patch(
                "bog_agents_cli.app.execute_task_textual",
                new_callable=AsyncMock,
                side_effect=RuntimeError("model does not support tools"),
            ):
                await app._run_agent_task("hello")
                await pilot.pause()

            errors = app.query(ErrorMessage)
            rendered = "\n".join(str(widget._content) for widget in errors)
            assert "This model does not support tool use in the CLI." in rendered
            assert "authentication/credential error" not in rendered


class TestAppFocusRestoresChatInput:
    """Test `on_app_focus` restores chat input focus after terminal regains focus."""

    async def test_app_focus_restores_chat_input(self) -> None:
        """Regaining terminal focus should re-focus the chat input."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._chat_input is not None
            assert app._chat_input._text_area is not None

            # Blur the input to simulate focus loss from webbrowser.open
            app._chat_input._text_area.blur()
            await pilot.pause()

            app.on_app_focus()
            await pilot.pause()

            # chat_input.focus_input should have been called
            assert app._chat_input._text_area.has_focus

    async def test_app_focus_skips_when_modal_open(self) -> None:
        """Regaining focus should not steal focus from an open modal."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            # Push a modal screen
            from bog_agents_cli.widgets.thread_selector import ThreadSelectorScreen

            screen = ThreadSelectorScreen(current_thread=None)
            app.push_screen(screen)
            await pilot.pause()

            assert isinstance(app.screen, ModalScreen)

            # on_app_focus should be a no-op with modal open
            with patch.object(app._chat_input, "focus_input") as mock_focus:
                app.on_app_focus()

            mock_focus.assert_not_called()

    async def test_app_focus_skips_when_approval_pending(self) -> None:
        """Regaining focus should not steal focus from the approval widget."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._chat_input is not None

            # Simulate a pending approval widget
            app._pending_approval_widget = MagicMock()

            with patch.object(app._chat_input, "focus_input") as mock_focus:
                app.on_app_focus()

            mock_focus.assert_not_called()


class TestPasteRouting:
    """Tests app-level paste routing when chat input focus lags."""

    async def test_on_paste_routes_unfocused_event_to_chat_input(self) -> None:
        """Unfocused paste events should be forwarded to chat input handler."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._chat_input is not None

            event = events.Paste("/tmp/photo.png")
            with (
                patch.object(app, "_is_input_focused", return_value=False),
                patch.object(
                    app._chat_input, "handle_external_paste", return_value=True
                ) as mock_handle,
                patch.object(event, "prevent_default") as mock_prevent,
                patch.object(event, "stop") as mock_stop,
            ):
                app.on_paste(event)

            mock_handle.assert_called_once_with("/tmp/photo.png")
            mock_prevent.assert_called_once()
            mock_stop.assert_called_once()

    async def test_on_paste_does_not_route_when_input_already_focused(self) -> None:
        """Focused input should keep normal TextArea paste handling path."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._chat_input is not None

            event = events.Paste("/tmp/photo.png")
            with (
                patch.object(app, "_is_input_focused", return_value=True),
                patch.object(
                    app._chat_input, "handle_external_paste", return_value=True
                ) as mock_handle,
                patch.object(event, "prevent_default") as mock_prevent,
                patch.object(event, "stop") as mock_stop,
            ):
                app.on_paste(event)

            mock_handle.assert_not_called()
            mock_prevent.assert_not_called()
            mock_stop.assert_not_called()


class TestShellCommandInterrupt:
    """Tests for interruptible shell commands (! prefix) using worker pattern."""

    async def test_escape_cancels_shell_worker(self) -> None:
        """Esc while shell command is running should cancel the worker."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            app._shell_running = True
            mock_worker = MagicMock()
            app._shell_worker = mock_worker

            app.action_interrupt()

            mock_worker.cancel.assert_called_once()
            assert len(app._pending_messages) == 0

    async def test_ctrl_c_cancels_shell_worker(self) -> None:
        """Ctrl+C while shell command is running should cancel the worker."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            app._shell_running = True
            mock_worker = MagicMock()
            app._shell_worker = mock_worker

            # Queue a message to verify it gets cleared
            app._pending_messages.append(QueuedMessage(text="queued", mode="normal"))

            app.action_quit_or_interrupt()

            mock_worker.cancel.assert_called_once()
            assert len(app._pending_messages) == 0
            assert app._quit_pending is False

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="POSIX process group management not available on Windows",
    )
    async def test_process_killed_on_cancelled_error(self) -> None:
        """CancelledError in _run_shell_task should kill the process."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(side_effect=asyncio.CancelledError)
            mock_proc.returncode = None
            mock_proc.pid = 12345
            mock_proc.wait = AsyncMock()

            with (
                patch(
                    "asyncio.create_subprocess_shell",
                    return_value=mock_proc,
                ),
                patch("bog_agents_cli.app.sys") as mock_sys,
                patch("os.killpg", create=True) as mock_killpg,
                patch("os.getpgid", return_value=12345, create=True),
            ):
                mock_sys.platform = "linux"
                with pytest.raises(asyncio.CancelledError):
                    await app._run_shell_task("sleep 999")

            mock_killpg.assert_called()

    async def test_cleanup_clears_state(self) -> None:
        """_cleanup_shell_task should reset all shell state."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            app._shell_running = True
            app._shell_worker = MagicMock()
            app._shell_worker.is_cancelled = False
            app._shell_process = None

            await app._cleanup_shell_task()

            assert app._shell_process is None
            assert app._shell_running is False
            assert app._shell_worker is None

    async def test_messages_queued_during_shell(self) -> None:
        """Messages should be queued while shell command runs."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._shell_running = True

            app.post_message(ChatInput.Submitted("queued msg", "normal"))
            await pilot.pause()

            assert len(app._pending_messages) == 1
            assert app._pending_messages[0].text == "queued msg"

    async def test_queue_drains_after_shell_completes(self) -> None:
        """Pending messages should drain after _cleanup_shell_task."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            app._shell_running = True
            app._shell_worker = MagicMock()
            app._shell_worker.is_cancelled = False
            app._shell_process = None

            # Enqueue a message
            app._pending_messages.append(
                QueuedMessage(text="after shell", mode="normal")
            )

            await app._cleanup_shell_task()
            await pilot.pause()

            # Message should have been processed (mounted as UserMessage)
            user_msgs = app.query(UserMessage)
            assert any(w._content == "after shell" for w in user_msgs)

    async def test_interrupted_shows_message(self) -> None:
        """Cancelled worker should show 'Command interrupted'."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            app._shell_running = True
            mock_worker = MagicMock()
            mock_worker.is_cancelled = True
            app._shell_worker = mock_worker
            # Process still set means it was interrupted mid-flight
            mock_proc = MagicMock()
            mock_proc.returncode = None
            app._shell_process = mock_proc

            await app._cleanup_shell_task()
            await pilot.pause()

            app_msgs = app.query(AppMessage)
            assert any("Command interrupted" in str(w._content) for w in app_msgs)

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="POSIX process group management not available on Windows",
    )
    async def test_timeout_kills_and_shows_error(self) -> None:
        """Timeout in _run_shell_task should kill process and show error."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
            mock_proc.returncode = None
            mock_proc.pid = 12345
            mock_proc.wait = AsyncMock()

            with (
                patch(
                    "asyncio.create_subprocess_shell",
                    return_value=mock_proc,
                ),
                patch("bog_agents_cli.app.sys") as mock_sys,
                patch("os.killpg", create=True),
                patch("os.getpgid", return_value=12345, create=True),
            ):
                mock_sys.platform = "linux"
                await app._run_shell_task("sleep 999")
                await pilot.pause()

            assert app._shell_process is None
            error_msgs = app.query(ErrorMessage)
            assert any("timed out" in w._content for w in error_msgs)

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="POSIX process group management not available on Windows",
    )
    async def test_posix_killpg_called(self) -> None:
        """On POSIX, _kill_shell_process should use os.killpg with SIGTERM."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            mock_proc = AsyncMock()
            mock_proc.returncode = None
            mock_proc.pid = 42
            mock_proc.wait = AsyncMock()
            app._shell_process = mock_proc

            with (
                patch("bog_agents_cli.app.sys") as mock_sys,
                patch("os.killpg", create=True) as mock_killpg,
                patch("os.getpgid", return_value=42, create=True) as mock_getpgid,
            ):
                mock_sys.platform = "linux"
                await app._kill_shell_process()

            mock_getpgid.assert_called_once_with(42)
            mock_killpg.assert_called_once_with(42, signal.SIGTERM)

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="POSIX process group management not available on Windows",
    )
    async def test_sigkill_escalation(self) -> None:
        """SIGKILL should be sent when SIGTERM times out."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            mock_proc = AsyncMock()
            mock_proc.returncode = None
            mock_proc.pid = 42
            mock_proc.wait = AsyncMock(side_effect=asyncio.TimeoutError)
            mock_proc.kill = MagicMock()
            app._shell_process = mock_proc

            with (
                patch("bog_agents_cli.app.sys") as mock_sys,
                patch("os.killpg", create=True) as mock_killpg,
                patch("os.getpgid", return_value=42, create=True),
            ):
                mock_sys.platform = "linux"
                await app._kill_shell_process()

            # First call: SIGTERM, second call: SIGKILL
            kill_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
            assert mock_killpg.call_count == 2
            mock_killpg.assert_any_call(42, signal.SIGTERM)
            mock_killpg.assert_any_call(42, kill_signal)

    async def test_no_op_when_no_shell_running(self) -> None:
        """Ctrl+C with no shell command running should fall through to quit hint."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            assert not app._shell_running
            app.action_quit_or_interrupt()

            assert app._quit_pending is True

    async def test_oserror_shows_error_message(self) -> None:
        """OSError from create_subprocess_shell should display error."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            with patch(
                "asyncio.create_subprocess_shell",
                side_effect=OSError("Permission denied"),
            ):
                await app._run_shell_task("forbidden")
                await pilot.pause()

            assert app._shell_process is None
            error_msgs = app.query(ErrorMessage)
            assert any("Permission denied" in w._content for w in error_msgs)

    async def test_handle_shell_command_sets_running_state(self) -> None:
        """_handle_shell_command should set _shell_running and spawn worker."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            with patch.object(app, "run_worker") as mock_rw:
                mock_rw.return_value = MagicMock()
                await app._handle_shell_command("echo hi")

            assert app._shell_running is True
            assert app._shell_worker is not None
            mock_rw.assert_called_once()
            # Close the unawaited coroutine to suppress RuntimeWarning
            coro = mock_rw.call_args[0][0]
            coro.close()

    async def test_kill_noop_when_already_exited(self) -> None:
        """_kill_shell_process should no-op if process already exited."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            mock_proc = AsyncMock()
            mock_proc.returncode = 0
            mock_proc.pid = 42
            app._shell_process = mock_proc

            with patch("os.killpg", create=True) as mock_killpg:
                await app._kill_shell_process()

            mock_killpg.assert_not_called()
            mock_proc.terminate.assert_not_called()

    async def test_end_to_end_escape_during_shell(self) -> None:
        """Esc during a running shell worker should cancel execution."""
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            # Simulate a running shell state with a mock worker
            app._shell_running = True
            mock_worker = MagicMock()
            app._shell_worker = mock_worker

            await pilot.press("escape")
            await pilot.pause()

            mock_worker.cancel.assert_called_once()


class TestInterruptApprovalPriority:
    """Tests for escape interrupt priority when HITL approval is pending."""

    async def test_escape_rejects_approval_before_canceling_worker(self) -> None:
        """When both HITL approval and worker are active, reject approval first."""
        app = BogAgentsApp()
        approval = MagicMock()
        worker = MagicMock()

        async with app.run_test() as pilot:
            await pilot.pause()

            app._pending_approval_widget = approval
            app._agent_running = True
            app._agent_worker = worker

            app.action_interrupt()

        approval.action_select_reject.assert_called_once()
        worker.cancel.assert_not_called()

    async def test_escape_cancels_worker_when_no_approval_pending(self) -> None:
        """Escape cancels active worker and clears queued messages when no approval."""
        app = BogAgentsApp()
        worker = MagicMock()
        queued_w1 = MagicMock()
        queued_w2 = MagicMock()

        async with app.run_test() as pilot:
            await pilot.pause()

            app._pending_approval_widget = None
            app._agent_running = True
            app._agent_worker = worker
            app._pending_messages.append(QueuedMessage(text="q", mode="normal"))
            app._queued_widgets.append(queued_w1)
            app._queued_widgets.append(queued_w2)

            app.action_interrupt()

        worker.cancel.assert_called_once()
        queued_w1.remove.assert_called_once()
        queued_w2.remove.assert_called_once()
        assert len(app._pending_messages) == 0
        assert len(app._queued_widgets) == 0

    async def test_escape_rejects_approval_when_no_worker(self) -> None:
        """Approval rejection works even without an active agent worker."""
        app = BogAgentsApp()
        approval = MagicMock()

        async with app.run_test() as pilot:
            await pilot.pause()

            app._pending_approval_widget = approval
            app._agent_running = False
            app._agent_worker = None

            app.action_interrupt()

        approval.action_select_reject.assert_called_once()

    async def test_ctrl_c_rejects_approval_before_canceling_worker(self) -> None:
        """Ctrl+C should also reject approval before canceling worker."""
        app = BogAgentsApp()
        approval = MagicMock()
        worker = MagicMock()

        async with app.run_test() as pilot:
            await pilot.pause()

            app._pending_approval_widget = approval
            app._agent_running = True
            app._agent_worker = worker

            app.action_quit_or_interrupt()

        approval.action_select_reject.assert_called_once()
        worker.cancel.assert_not_called()
        assert app._quit_pending is False


class TestFetchThreadHistoryData:
    """Verify _fetch_thread_history_data handles server-mode resume scenarios."""

    async def test_dict_messages_converted_to_message_objects(self) -> None:
        """Dict-based messages from server mode are deserialized before conversion."""
        from bog_agents_cli.widgets.message_store import MessageData, MessageType

        state = MagicMock()
        state.values = {
            "messages": [
                {"type": "human", "content": "hello", "id": "h1"},
                {
                    "type": "ai",
                    "content": "Hi there!",
                    "id": "a1",
                    "tool_calls": [],
                },
            ],
        }

        mock_agent = AsyncMock()
        mock_agent.aget_state.return_value = state

        app = BogAgentsApp(agent=mock_agent, thread_id="t-1")
        result = await app._fetch_thread_history_data("t-1")

        assert len(result) == 2
        assert isinstance(result[0], MessageData)
        assert result[0].type == MessageType.USER
        assert result[0].content == "hello"
        assert isinstance(result[1], MessageData)
        assert result[1].type == MessageType.ASSISTANT
        assert result[1].content == "Hi there!"

    async def test_server_mode_falls_back_to_checkpointer(self) -> None:
        """When the server returns empty state, read SQLite checkpointer directly."""
        from langchain_core.messages import AIMessage, HumanMessage

        from bog_agents_cli.remote_client import RemoteAgent
        from bog_agents_cli.widgets.message_store import MessageData, MessageType

        # Server returns empty state (fresh restart, thread not loaded)
        empty_state = MagicMock()
        empty_state.values = {}

        # spec=RemoteAgent so _remote_agent() isinstance check passes
        mock_agent = MagicMock(spec=RemoteAgent)
        mock_agent.aget_state = AsyncMock(return_value=empty_state)

        app = BogAgentsApp(agent=mock_agent, thread_id="t-1")

        # Patch the checkpointer fallback to return messages
        checkpointer_msgs = [
            HumanMessage(content="hello", id="h1"),
            AIMessage(content="world", id="a1"),
        ]
        with patch.object(
            BogAgentsApp,
            "_read_channel_values_from_checkpointer",
            return_value={"messages": checkpointer_msgs},
        ):
            result = await app._fetch_thread_history_data("t-1")

        assert len(result) == 2
        assert result[0].type == MessageType.USER
        assert result[0].content == "hello"
        assert result[1].type == MessageType.ASSISTANT
        assert result[1].content == "world"


class TestRemoteAgent:
    """Tests for BogAgentsApp._remote_agent()."""

    def test_returns_instance_with_remote_agent(self) -> None:
        from bog_agents_cli.remote_client import RemoteAgent

        app = BogAgentsApp()
        agent = RemoteAgent("http://test:0")
        app._agent = agent
        assert app._remote_agent() is agent

    def test_none_when_agent_is_none(self) -> None:
        app = BogAgentsApp()
        assert app._remote_agent() is None

    def test_none_with_non_remote_agent(self) -> None:
        """Local Pregel-like agent returns None."""
        app = BogAgentsApp()
        app._agent = MagicMock()
        assert app._remote_agent() is None

    def test_none_with_mock_spec_pregel(self) -> None:
        """MagicMock without RemoteAgent spec returns None."""
        app = BogAgentsApp()
        app._agent = MagicMock(spec=[])
        assert app._remote_agent() is None


class TestWaveNTimeoutResolvers:
    """H1/H2: env-driven timeout knobs used by the agent worker.

    Pure function tests — no Textual app needed.
    """

    def test_turn_timeout_default(self, monkeypatch):
        monkeypatch.delenv("BOG_AGENTS_TURN_TIMEOUT_SECONDS", raising=False)
        from bog_agents_cli.app import (
            _DEFAULT_TURN_TIMEOUT_SECONDS,
            _resolve_turn_timeout,
        )

        assert _resolve_turn_timeout() == _DEFAULT_TURN_TIMEOUT_SECONDS

    def test_turn_timeout_explicit_seconds(self, monkeypatch):
        monkeypatch.setenv("BOG_AGENTS_TURN_TIMEOUT_SECONDS", "120")
        from bog_agents_cli.app import _resolve_turn_timeout

        assert _resolve_turn_timeout() == 120.0

    def test_turn_timeout_disabled_via_zero(self, monkeypatch):
        monkeypatch.setenv("BOG_AGENTS_TURN_TIMEOUT_SECONDS", "0")
        from bog_agents_cli.app import _resolve_turn_timeout

        assert _resolve_turn_timeout() is None

    def test_turn_timeout_disabled_via_none(self, monkeypatch):
        monkeypatch.setenv("BOG_AGENTS_TURN_TIMEOUT_SECONDS", "none")
        from bog_agents_cli.app import _resolve_turn_timeout

        assert _resolve_turn_timeout() is None

    def test_turn_timeout_invalid_falls_back(self, monkeypatch):
        monkeypatch.setenv("BOG_AGENTS_TURN_TIMEOUT_SECONDS", "not-a-number")
        from bog_agents_cli.app import (
            _DEFAULT_TURN_TIMEOUT_SECONDS,
            _resolve_turn_timeout,
        )

        assert _resolve_turn_timeout() == _DEFAULT_TURN_TIMEOUT_SECONDS

    def test_stream_chunk_timeout_default(self, monkeypatch):
        # The headless per-chunk cap is DISABLED by default: a long tool
        # call legitimately produces no stream chunks for its duration,
        # so any finite cap kills real work. The model-layer read
        # deadline + the remote liveness watchdog are the genuine-hang
        # backstops.
        monkeypatch.delenv("BOG_AGENTS_STREAM_CHUNK_TIMEOUT_SECONDS", raising=False)
        from bog_agents_cli.non_interactive import (
            _DEFAULT_STREAM_CHUNK_TIMEOUT_SECONDS,
            _resolve_stream_chunk_timeout,
        )

        assert _DEFAULT_STREAM_CHUNK_TIMEOUT_SECONDS is None
        assert _resolve_stream_chunk_timeout() is None

    def test_stream_chunk_timeout_explicit_override(self, monkeypatch):
        # A user who wants a hard per-chunk cap back can set one.
        monkeypatch.setenv("BOG_AGENTS_STREAM_CHUNK_TIMEOUT_SECONDS", "300")
        from bog_agents_cli.non_interactive import _resolve_stream_chunk_timeout

        assert _resolve_stream_chunk_timeout() == 300.0

    def test_stream_chunk_timeout_disabled(self, monkeypatch):
        monkeypatch.setenv("BOG_AGENTS_STREAM_CHUNK_TIMEOUT_SECONDS", "off")
        from bog_agents_cli.non_interactive import _resolve_stream_chunk_timeout

        assert _resolve_stream_chunk_timeout() is None


class TestWaveNTaskExceptionLogging:
    """H4: failed background tasks must surface via logging."""

    def test_log_task_exception_logs_failure(self, caplog):
        import asyncio

        from bog_agents_cli.app import _log_task_exception

        async def boom():
            msg = "synthetic"
            raise RuntimeError(msg)

        loop = asyncio.new_event_loop()
        try:
            task = loop.create_task(boom(), name="probe")
            with caplog.at_level("ERROR"):
                with contextlib.suppress(RuntimeError):
                    loop.run_until_complete(task)
                _log_task_exception(task)
            assert any(
                "Background task 'probe' failed" in r.message for r in caplog.records
            )
        finally:
            loop.close()

    def test_log_task_exception_silent_on_success(self, caplog):
        import asyncio

        from bog_agents_cli.app import _log_task_exception

        async def ok():
            return 42

        loop = asyncio.new_event_loop()
        try:
            task = loop.create_task(ok(), name="probe-ok")
            loop.run_until_complete(task)
            with caplog.at_level("ERROR"):
                _log_task_exception(task)
            assert not any("failed" in r.message for r in caplog.records)
        finally:
            loop.close()

    def test_log_task_exception_silent_on_cancelled(self, caplog):
        import asyncio

        from bog_agents_cli.app import _log_task_exception

        async def hangs():
            await asyncio.sleep(60)

        loop = asyncio.new_event_loop()
        try:
            task = loop.create_task(hangs(), name="probe-cancel")
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                loop.run_until_complete(task)
            with caplog.at_level("ERROR"):
                _log_task_exception(task)
            assert not any("failed" in r.message for r in caplog.records)
        finally:
            loop.close()
