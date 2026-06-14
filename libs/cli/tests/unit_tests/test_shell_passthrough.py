"""Tests for agent-visible shell pass-through (`!command`)."""

from __future__ import annotations

from bog_agents_cli.shell_passthrough import format_shell_context


class TestFormatShellContext:
    def test_includes_command_output_and_exit(self) -> None:
        msg = format_shell_context("ls -la", "file1\nfile2", 0)
        assert "shell pass-through" in msg
        assert "$ ls -la" in msg
        assert "file1" in msg
        assert "exit code 0" in msg
        # It must tell the agent it did NOT run this itself.
        assert "did not run this" in msg.lower()

    def test_no_output(self) -> None:
        assert "(no output)" in format_shell_context("true", "", 0)

    def test_unknown_returncode(self) -> None:
        assert "completed" in format_shell_context("x", "y", None)

    def test_truncates_long_output(self) -> None:
        big = "x" * 50000
        msg = format_shell_context("cat big", big, 0, max_chars=1000)
        assert "truncated" in msg
        assert len(msg) < len(big)


class _FakeAgent:
    def __init__(self) -> None:
        self.calls: list[tuple[object, object]] = []

    async def aupdate_state(self, config: object, values: object) -> None:
        self.calls.append((config, values))


class TestRecordShellRunForAgent:
    async def test_injects_into_thread(self) -> None:
        from bog_agents_cli.app import BogAgentsApp

        app = BogAgentsApp()
        app._agent = _FakeAgent()  # type: ignore[assignment]
        app._lc_thread_id = "thread-x"
        await app._record_shell_run_for_agent("ls", "file1\nfile2", 0)

        agent = app._agent
        assert len(agent.calls) == 1
        config, values = agent.calls[0]
        assert config["configurable"]["thread_id"] == "thread-x"
        message = values["messages"][0]
        assert "ls" in message.content
        assert "file1" in message.content

    async def test_noop_without_thread(self) -> None:
        from bog_agents_cli.app import BogAgentsApp

        app = BogAgentsApp()
        app._agent = _FakeAgent()  # type: ignore[assignment]
        app._lc_thread_id = None
        await app._record_shell_run_for_agent("ls", "out", 0)
        assert app._agent.calls == []

    async def test_noop_without_agent(self) -> None:
        from bog_agents_cli.app import BogAgentsApp

        app = BogAgentsApp()
        app._agent = None
        app._lc_thread_id = "t"
        # Must not raise when there's no agent to inject into.
        await app._record_shell_run_for_agent("ls", "out", 0)
