"""ROADMAP #56: sessions, the cross-process queue, detach / attach and the CLI plumbing."""

from __future__ import annotations

import asyncio
import io
import sys
import threading
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from bog_agents.session_registry import (
    SessionRecord,
    list_sessions,
    load_session,
    register,
)

from bog_agents_cli import session_controller as sc
from bog_agents_cli.server import ServerProcess

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(sc, "registry_dir", lambda: tmp_path / "sessions")
    monkeypatch.setattr(sc, "mailbox_path", lambda: tmp_path / "mailbox.db")
    sc.configure_launch()
    yield
    sc.configure_launch()


class FakeApp:
    """The slice of `BogAgentsApp` the controller touches."""

    def __init__(self, cwd: Path) -> None:
        self._cwd = str(cwd)
        self._model_override = "anthropic:claude"
        self._lc_thread_id = "thread-1"
        self._turns = SimpleNamespace(busy=False)
        self._connecting = False
        self._exit = False
        self._pending_messages: deque = deque()
        self._session_queue: sc.SessionQueue | None = None
        self._detached = False
        self._server_proc: object | None = None
        self.processed: list[str] = []
        self.last_text = "the answer"

    async def _process_next_from_queue(self) -> None:
        while self._pending_messages:
            self.processed.append(self._pending_messages.popleft().text)

    def _get_last_assistant_text(self) -> str:
        return self.last_text


def _record(app: FakeApp) -> SessionRecord:
    assert app._session_queue is not None
    loaded = load_session(
        app._session_queue.record.session_id, registry_dir=sc.registry_dir()
    )
    assert loaded is not None
    return loaded


class TestSessionLifecycle:
    def test_start_registers_heartbeats_and_closes(self, tmp_path: Path) -> None:
        sc.configure_launch(name="fix-tests")
        app = FakeApp(tmp_path)
        app._session_queue = sc.start_session_queue(app)
        record = _record(app)
        assert (
            record.name == "fix-tests"
            and record.kind == "tui"
            and record.state == "idle"
        )
        assert record.thread_id == "thread-1" and record.cwd == str(tmp_path)
        assert record.mailbox_path.endswith("mailbox.db")

        app._turns.busy = True
        asyncio.run(sc.poll_session_queue(app))
        assert _record(app).state == "busy"

        app._session_queue.close()
        assert list_sessions(include_stale=True, registry_dir=sc.registry_dir()) == []

    def test_close_keeps_a_detached_record(self, tmp_path: Path) -> None:
        app = FakeApp(tmp_path)
        app._session_queue = sc.start_session_queue(app)
        app._session_queue.close(detached=True)
        assert (
            len(list_sessions(include_stale=True, registry_dir=sc.registry_dir())) == 1
        )

    def test_sessions_report_lists_and_prunes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        register(
            SessionRecord(session_id="live1", name="alpha"),
            registry_dir=sc.registry_dir(),
        )
        register(
            SessionRecord(
                session_id="dead1",
                name="beta",
                pid=4_000_000,
                heartbeat=time.time() - 9999,
            ),
            registry_dir=sc.registry_dir(),
        )
        monkeypatch.setattr(
            "bog_agents.session_registry.pid_alive", lambda pid: pid != 4_000_000
        )
        report = sc.sessions_report()
        assert "alpha" in report and "beta" not in report
        assert "beta" in sc.sessions_report(include_stale=True)
        pruned = sc.sessions_report(prune=True)
        assert "Pruned 1 stale record(s)." in pruned
        assert "beta" not in sc.sessions_report(include_stale=True)


class TestQueue:
    def test_prompts_run_when_idle_and_answers_reach_the_waiter(
        self, tmp_path: Path
    ) -> None:
        sc.configure_launch(name="worker")
        app = FakeApp(tmp_path)
        app._session_queue = sc.start_session_queue(app)

        code, text = sc.enqueue_prompt("worker", "run the tests")
        assert code == 0 and "Queued for worker" in text

        app._turns.busy = True
        asyncio.run(sc.poll_session_queue(app))
        assert app.processed == []  # busy: nothing pulled

        app._turns.busy = False
        asyncio.run(sc.poll_session_queue(app))
        assert app.processed == ["run the tests"]
        assert app._session_queue.waiting == 1
        assert sc.turn_finished(app) == 1
        assert sc.turn_finished(app) == 0  # nobody left waiting

        results: list[tuple[int, str]] = []
        waiter = threading.Thread(
            target=lambda: results.append(
                sc.enqueue_prompt("worker", "second", wait=10.0)
            )
        )
        waiter.start()
        deadline = time.monotonic() + 10.0
        while "second" not in app.processed and time.monotonic() < deadline:
            asyncio.run(sc.poll_session_queue(app))
            time.sleep(0.05)
        assert "second" in app.processed
        sc.turn_finished(app)
        waiter.join(timeout=10.0)
        assert results == [(0, "the answer")]

    def test_errors_and_timeouts(self, tmp_path: Path) -> None:
        assert sc.enqueue_prompt("ghost", "hi")[0] == 1
        app = FakeApp(tmp_path)
        sc.configure_launch(name="quiet")
        app._session_queue = sc.start_session_queue(app)
        assert sc.enqueue_prompt("quiet", "   ")[0] == 1
        code, text = sc.enqueue_prompt("quiet", "anyone?", wait=0.3)
        assert code == 2 and "no answer within" in text
        # The prompt is still queued for the session's next idle tick.
        asyncio.run(sc.poll_session_queue(app))
        assert app.processed == ["anyone?"]

    def test_poll_without_a_queue_is_a_noop(self, tmp_path: Path) -> None:
        app = FakeApp(tmp_path)
        asyncio.run(sc.poll_session_queue(app))
        assert app.processed == [] and sc.turn_finished(app) == 0


class TestDetachAttach:
    def test_detach_records_the_server_and_attach_target_finds_it(
        self, tmp_path: Path
    ) -> None:
        sc.configure_launch(name="long-run")
        app = FakeApp(tmp_path)
        app._session_queue = sc.start_session_queue(app)
        assert "Nothing to detach" in sc.detach(app)
        assert not app._detached

        class FakeProc:
            def detach(self) -> tuple[int, str]:
                return 4242, "http://127.0.0.1:2024"

        app._server_proc = FakeProc()
        message = sc.detach(app)
        assert app._detached and "bog-agents attach long-run" in message
        record = _record(app)
        assert (
            record.state == "detached"
            and record.server_url == "http://127.0.0.1:2024"
            and record.server_pid == 4242
        )

        target = sc.attach_target("long-run")
        assert target.session_id == record.session_id

        # Re-attaching adopts the record instead of registering a new one.
        sc.configure_launch(name=target.name, attach=target)
        app2 = FakeApp(tmp_path)
        app2._session_queue = sc.start_session_queue(app2)
        assert app2._session_queue.record.session_id == record.session_id
        assert _record(app2).state == "idle"
        with pytest.raises(LookupError, match="not detached"):
            sc.attach_target("long-run")

    def test_server_process_detach_and_adopt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = ServerProcess(host="127.0.0.1", port=2031)

        class FakePopen:
            pid = 777

            def poll(self) -> None:
                return None

        proc._process = FakePopen()  # type: ignore[assignment]
        proc._owns_config_dir = True
        proc._log_file = io.StringIO()
        pid, url = proc.detach()
        assert pid == 777 and url == proc.url
        assert (
            proc._process is None
            and not proc._owns_config_dir
            and proc._log_file is None
        )
        proc.stop()  # nothing left to stop

        with pytest.raises(RuntimeError, match="no running server"):
            ServerProcess(host="127.0.0.1", port=2032).detach()

        adopted = ServerProcess.adopt("http://127.0.0.1:2050", 4242)
        assert adopted.port == 2050 and adopted.url.endswith(":2050")
        killed: list[int] = []
        monkeypatch.setattr(
            "bog_agents_cli._proc.terminate",
            lambda pid, force=False: killed.append(pid) or True,
        )
        adopted.stop()
        assert killed == [4242]
        adopted.stop()
        assert killed == [4242]  # pid cleared after the first stop


class TestCliArgs:
    def test_sessions_queue_attach_and_name(self) -> None:
        from bog_agents_cli.main import parse_args

        with patch.object(sys, "argv", ["bog-agents", "sessions", "--prune"]):
            args = parse_args()
        assert args.command == "sessions" and args.prune and not args.all
        with patch.object(
            sys,
            "argv",
            ["bog-agents", "queue", "--session", "w", "--wait", "hello there"],
        ):
            args = parse_args()
        assert (
            args.command == "queue"
            and args.session == "w"
            and args.wait is True
            and args.timeout == 600.0
            and args.prompt == "hello there"
        )
        with patch.object(
            sys,
            "argv",
            ["bog-agents", "queue", "--session", "w", "--wait", "--timeout", "5", "hi"],
        ):
            assert parse_args().timeout == 5.0
        with patch.object(sys, "argv", ["bog-agents", "queue", "--session", "w", "hi"]):
            assert parse_args().wait is False
        with patch.object(sys, "argv", ["bog-agents", "attach", "w"]):
            args = parse_args()
        assert args.command == "attach" and args.session == "w"
        with patch.object(sys, "argv", ["bog-agents", "--name", "nightly"]):
            assert parse_args().name == "nightly"
