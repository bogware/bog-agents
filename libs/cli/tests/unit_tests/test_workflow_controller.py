"""ROADMAP #73: `/workflow`, `/<name>` dispatch, the agent tools, and prompt-command discovery."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest

from bog_agents_cli import workflow as wf, workflow_controller as wc

YAML = """
name: triage
description: Triage a bug report.
args: [report]
phases:
  - name: look
    kind: context
    tasks:
      - id: read
        title: Read {report}
  - name: answer
    kind: synthesize
    tasks:
      - title: Answer the report
"""


class FakeApp:
    """Just enough of the App for the controller."""

    def __init__(self, root: Path) -> None:
        self._cwd = str(root)
        self.messages: list[str] = []
        self.sessions: list[str] = []
        self.pending: list[asyncio.Task[None]] = []
        self._prompt_commands: dict[str, Any] = {}
        self.refreshed = 0

    async def _mount_message(self, message: object) -> None:
        self.messages.append(
            str(
                getattr(message, "text", None)
                or getattr(message, "_text", None)
                or getattr(message, "content", None)
                or message
            )
        )

    def _start_tracked_session(
        self, coro: Coroutine[Any, Any, None], *, name: str
    ) -> asyncio.Task[None]:
        self.sessions.append(name)
        task = asyncio.ensure_future(coro)
        self.pending.append(task)
        return task

    def call_later(
        self, callback: Callable[..., Awaitable[None]], *args: object
    ) -> None:
        self.pending.append(asyncio.ensure_future(callback(*args)))

    def _refresh_slash_command_cache(self) -> None:
        self.refreshed += 1

    async def drain(self) -> None:
        while self.pending:
            batch, self.pending = self.pending, []
            await asyncio.gather(*batch)


@pytest.fixture(autouse=True)
def _root_from_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    """The controller resolves the project root from settings first; tests pin it to the fake app's cwd."""
    monkeypatch.setattr(wc, "project_root", lambda app: Path(app._cwd))


def _text_of(app: FakeApp) -> str:
    return "\n".join(app.messages)


async def _runner(
    task: wf.WorkflowTask, prompt: str, phase: wf.PhaseRecord
) -> wf.TaskOutcome:
    return wf.TaskOutcome(
        success=True,
        output=f"{phase.name}:{task.id}:{prompt.splitlines()[0]}",
        cost_usd=0.01,
        input_tokens=3,
        output_tokens=2,
    )


def test_list_show_author_run_status_and_slash(tmp_path: Path) -> None:
    app = FakeApp(tmp_path)

    async def main() -> None:
        await wc.run_workflow_command(app, "/workflow list")
        assert "No workflows yet" in _text_of(app)
        await wc.run_workflow_command(
            app,
            "/workflow author triage bug reports --name triage",
            invoke=lambda _p: YAML,
        )
        assert (
            "Saved" in _text_of(app)
            and (tmp_path / ".bog-agents" / "workflows" / "triage.yaml").is_file()
            and app.refreshed == 1
        )
        app.messages.clear()
        await wc.run_workflow_command(app, "/workflow show triage")
        assert "/triage <report>" in _text_of(app) and "synthesize" in _text_of(app)
        await wc.run_workflow_command(app, "/workflow show nope")
        assert "No workflow named 'nope'" in _text_of(app)
        app.messages.clear()
        await wc.run_workflow_command(app, "/workflow run triage", task_runner=_runner)
        assert "missing report" in _text_of(app) and not app.sessions
        app.messages.clear()
        await wc.run_workflow_command(
            app, '/workflow run triage "crash on start"', task_runner=_runner
        )
        assert app.sessions == ["/triage"]
        await app.drain()
        text = _text_of(app)
        assert (
            "Running workflow /triage" in text
            and "run" in text
            and "answer:answer-1" in text
        )
        latest = wf.latest_run(tmp_path, "triage")
        assert (
            latest is not None
            and latest.status == "done"
            and latest.args["report"] == "crash on start"
        )
        app.messages.clear()
        await wc.run_workflow_command(app, "/workflow status triage")
        assert "done" in _text_of(app) and "spend $0.0200" in _text_of(app)
        await wc.run_workflow_command(app, "/workflow resume triage")
        assert "Nothing to resume" in _text_of(app)
        await wc.run_workflow_command(app, "/workflow dance")
        assert "Unknown verb" in _text_of(app)

        # `/triage ...` through the prompt-command path.
        from bog_agents_cli.prompt_commands import discover_prompt_commands

        commands = discover_prompt_commands(tmp_path, include_user=False)
        assert (
            "/triage" in commands
            and commands["/triage"].scope == "workflow"
            and commands["/triage"].argument_hint == "<report>"
        )
        app.messages.clear()
        app.sessions.clear()
        assert await wc.run_workflow_slash(
            app, commands["/triage"], "another one", task_runner=_runner
        )
        assert app.sessions == ["/triage"]
        await app.drain()
        assert "answer:answer-1" in _text_of(app)

    asyncio.run(main())


def test_author_failure_and_resume_with_budget(tmp_path: Path) -> None:
    app = FakeApp(tmp_path)

    async def main() -> None:
        await wc.run_workflow_command(
            app, "/workflow author something", invoke=lambda _p: "name: ["
        )
        assert "Could not author" in _text_of(app)
        wf.save_workflow(
            tmp_path,
            wf.parse_workflow(
                YAML.replace(
                    "description: Triage a bug report.",
                    "description: x\nbudget_usd: 0.005",
                )
            ),
        )
        app.messages.clear()
        await wc.run_workflow_command(
            app, "/workflow run triage r1", task_runner=_runner
        )
        await app.drain()
        assert "paused" in _text_of(
            app
        ) and "/workflow resume triage --budget" in _text_of(app)
        app.messages.clear()
        await wc.run_workflow_command(
            app, "/workflow resume triage --budget nope", task_runner=_runner
        )
        assert "--budget needs a number" in _text_of(app)
        await wc.run_workflow_command(
            app, "/workflow resume triage --budget 1", task_runner=_runner
        )
        await app.drain()
        latest = wf.latest_run(tmp_path, "triage")
        assert (
            latest is not None
            and latest.status == "done"
            and "Resuming workflow" in _text_of(app)
        )

    asyncio.run(main())


def test_real_runner_reads_meters(tmp_path: Path) -> None:
    class _Msg:
        type = "ai"

        def __init__(self, content: str, usage: dict[str, int]) -> None:
            self.content = content
            self.usage_metadata = usage

    class _Agent:
        async def ainvoke(self, payload: dict[str, Any]) -> dict[str, Any]:
            prompt = payload["messages"][0][1]
            return {
                "messages": [
                    _Msg("thinking", {"input_tokens": 100, "output_tokens": 10}),
                    _Msg(
                        f"done: {prompt[:10]}", {"input_tokens": 50, "output_tokens": 5}
                    ),
                ]
            }

    seen: list[dict[str, Any]] = []

    def factory(_model: object, **kwargs: Any) -> tuple[_Agent, None]:
        seen.append(kwargs)
        return _Agent(), None

    app = FakeApp(tmp_path)
    runner = wc.build_task_runner(
        app,
        agent_factory=factory,
        resolve_model=lambda spec: spec,
        model_spec="anthropic:claude-sonnet-4-5",
    )
    task = wf.WorkflowTask(id="t1", title="Do it")
    outcome = asyncio.run(
        runner(task, "Do it now", wf.PhaseRecord(name="p", kind="work"))
    )
    assert (
        outcome.success
        and outcome.output.startswith("done:")
        and (outcome.input_tokens, outcome.output_tokens) == (150, 15)
    )
    assert outcome.cost_usd > 0
    assert (
        seen[0]["auto_approve"]
        and not seen[0]["interactive"]
        and seen[0]["assistant_id"] == "workflow-p-t1"
    )


def test_agent_tools(tmp_path: Path) -> None:
    from bog_agents_cli.workflow_tools import workflow_tools_bundle

    tools = {t.name: t for t in workflow_tools_bundle(tmp_path)}
    assert set(tools) == {"author_workflow", "list_workflows"}
    bad = tools["author_workflow"].invoke({"yaml_text": "name: [x"})
    assert bad.startswith("Error:") and "Schema" in bad
    good = tools["author_workflow"].invoke({"yaml_text": YAML})
    assert "Saved /triage <report>" in good
    assert "/triage <report>" in tools["list_workflows"].invoke({})


@pytest.mark.parametrize("has_dir", [True, False])
def test_agent_registers_tools_only_when_workflows_exist(
    tmp_path: Path, has_dir: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bog_agents_cli import agent as agent_mod

    if has_dir:
        (tmp_path / ".bog-agents" / "workflows").mkdir(parents=True)
    names = [t.name for t in agent_mod._workflow_tools(tmp_path, restricted=False)]
    assert (set(names) == {"author_workflow", "list_workflows"}) is has_dir
    assert agent_mod._workflow_tools(tmp_path, restricted=True) == []
