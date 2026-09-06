"""ROADMAP #51 in the daemon: budgets that pause + resume, daily ceilings, spend recording."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from bog_agents.spend_ledger import SpendLedger, daemon_scope

from bog_agents_daemon import runner
from bog_agents_daemon.models import AmbientJob, JobStatus, TriggerType
from bog_agents_daemon.runner import BudgetPausedError, _budget_interrupt_payload, resume_paused_run, run_job
from bog_agents_daemon.store import _job_from_dict, _job_to_dict, list_runs, load_jobs, spend_db_path


class _Interrupt:
    def __init__(self, value: dict[str, Any]) -> None:
        self.value = value


class _AIMsg:
    type = "ai"

    def __init__(self, content: str, usage: dict[str, int] | None = None) -> None:
        self.content = content
        self.usage_metadata = usage


PAYLOAD = {"type": "budget_reached", "spent_usd": 1.5, "budget_usd": 1.0, "message": "Cost budget exceeded: $1.5000 spent of $1.00 budget."}


@pytest.fixture(autouse=True)
def _clear_paused() -> None:
    runner._PAUSED_RUNS.clear()
    yield
    runner._PAUSED_RUNS.clear()


def test_job_budget_fields_round_trip() -> None:
    job = AmbientJob(name="j", prompt="p", budget_usd=2.5, daily_ceiling_usd=10.0)
    again = _job_from_dict(_job_to_dict(job))
    assert (again.budget_usd, again.daily_ceiling_usd) == (2.5, 10.0)
    assert _job_from_dict({"name": "old"}).budget_usd is None


def test_budget_interrupt_payload_extraction() -> None:
    assert _budget_interrupt_payload({"__interrupt__": (_Interrupt(PAYLOAD),)}) == PAYLOAD
    assert _budget_interrupt_payload({"__interrupt__": (_Interrupt({"type": "ask_user"}),)}) is None
    assert _budget_interrupt_payload({"node": {"messages": []}}) is None
    assert _budget_interrupt_payload("not a dict") is None


class TestPauseAndResume:
    async def test_run_pauses_then_resumes_to_completion(self, tmp_daemon_dir: Path) -> None:
        job = AmbientJob(name="capped", prompt="go", budget_usd=1.0, model="anthropic:claude-sonnet-4-6")
        calls: list[Any] = []

        class _Agent:
            async def astream(self, stream_input: Any, config: Any = None) -> Any:
                calls.append((stream_input, config))
                if len(calls) == 1:
                    yield {"model": {"messages": [_AIMsg("partial", {"input_tokens": 1_000_000, "output_tokens": 0})]}}
                    yield {"__interrupt__": (_Interrupt(PAYLOAD),)}
                else:
                    yield {"model": {"messages": [_AIMsg("final answer", {"input_tokens": 10, "output_tokens": 5})]}}

        with patch("bog_agents.create_agent", return_value=_Agent()):
            run = await run_job(job, trigger_type=TriggerType.MANUAL)

        assert run.status == JobStatus.PAUSED
        assert run.error.startswith("budget_reached:")
        assert runner.is_paused(run.run_id)
        assert calls[0][1]["configurable"]["thread_id"] == run.run_id
        # The pause is persisted and the job's last status says so.
        assert list_runs(job_id=job.job_id)[0].status == JobStatus.PAUSED
        assert next(j for j in load_jobs() if j.job_id == job.job_id).last_status == JobStatus.PAUSED
        # Spend up to the pause was recorded for the job ($3 per 1M input tokens).
        assert SpendLedger(spend_db_path()).total_usd(daemon_scope(job.job_id)) == pytest.approx(3.0)

        resumed = await resume_paused_run(run.run_id, budget_usd=5.0)
        assert resumed.status == JobStatus.COMPLETED
        assert resumed.output == "final answer"
        assert not runner.is_paused(run.run_id)
        assert calls[1][0].resume == {"budget_usd": 5.0}
        assert list_runs(job_id=job.job_id)[0].status == JobStatus.COMPLETED

    async def test_resume_unknown_run_raises(self) -> None:
        with pytest.raises(KeyError):
            await resume_paused_run("nope", budget_usd=9.0)

    async def test_budget_pause_is_not_retried(self, tmp_daemon_dir: Path) -> None:
        job = AmbientJob(name="retry", prompt="go", max_retries=3, budget_usd=1.0)
        attempts = 0

        async def _pause(*_a: Any, **_k: Any) -> str:
            nonlocal attempts
            attempts += 1
            raise BudgetPausedError(PAYLOAD, agent=object(), config={})

        with patch("bog_agents_daemon.runner._invoke_agent", side_effect=_pause):
            run = await run_job(job, trigger_type=TriggerType.MANUAL)
        assert run.status == JobStatus.PAUSED
        assert attempts == 1


class TestDailyCeiling:
    async def test_run_is_skipped_once_the_ceiling_is_reached(self, tmp_daemon_dir: Path) -> None:
        job = AmbientJob(name="ceiling", prompt="go", daily_ceiling_usd=1.0)
        SpendLedger(spend_db_path()).record(daemon_scope(job.job_id), 1.25)
        invoke = AsyncMock(return_value="should not run")
        with patch("bog_agents_daemon.runner._invoke_agent", invoke):
            run = await run_job(job, trigger_type=TriggerType.CRON)
        assert run.status == JobStatus.SKIPPED
        assert "daily ceiling reached" in run.error
        invoke.assert_not_awaited()

    async def test_under_the_ceiling_runs_normally(self, tmp_daemon_dir: Path) -> None:
        job = AmbientJob(name="ceiling", prompt="go", daily_ceiling_usd=10.0)
        with patch("bog_agents_daemon.runner._invoke_agent", AsyncMock(return_value="ok")):
            run = await run_job(job, trigger_type=TriggerType.CRON)
        assert run.status == JobStatus.COMPLETED


class TestUncappedJobsKeepTheirShape:
    async def test_no_budget_means_no_checkpointer_and_positional_stream(self, tmp_daemon_dir: Path) -> None:
        job = AmbientJob(name="plain", prompt="go")
        seen: dict[str, Any] = {}

        class _Agent:
            async def astream(self, stream_input: Any) -> Any:  # no `config` kwarg accepted
                seen["input"] = stream_input
                yield {"model": {"messages": [_AIMsg("done")]}}

        def _create(**kwargs: Any) -> _Agent:
            seen["kwargs"] = kwargs
            return _Agent()

        with patch("bog_agents.create_agent", side_effect=_create):
            run = await run_job(job, trigger_type=TriggerType.MANUAL)
        assert run.status == JobStatus.COMPLETED
        assert "checkpointer" not in seen["kwargs"]
        assert seen["kwargs"]["config"].enable_cost_tracking is False
