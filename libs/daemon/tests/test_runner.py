"""Integration tests for the daemon runner (job execution and output dispatch)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bog_agents_daemon.models import (
    AmbientJob,
    JobStatus,
    OutputConfig,
    OutputTarget,
    TriggerType,
)
from bog_agents_daemon.runner import (
    _build_prompt,
    _dispatch_file,
    _dispatch_output,
    _invoke_agent,
    run_job,
)
from bog_agents_daemon.store import list_runs, load_jobs

# ---------------------------------------------------------------------------
# _build_prompt
# ---------------------------------------------------------------------------


class TestBuildPrompt:
    def test_uses_prompt_field(self) -> None:
        job = AmbientJob(name="j", prompt="Hello world")
        assert _build_prompt(job) == "Hello world"

    def test_falls_back_to_skill(self) -> None:
        # Skill resolution now reads the actual SKILL.md content. With no
        # such skill on disk, _build_prompt raises with the skill name in
        # the message — verify that path.
        job = AmbientJob(name="j", skill_name="definitely-not-a-real-skill-xyz")
        with pytest.raises(ValueError, match="definitely-not-a-real-skill-xyz"):
            _build_prompt(job)

    def test_falls_back_to_pipeline(self) -> None:
        # Pipeline resolution now reads the actual yaml. With no such
        # pipeline on disk, _build_prompt raises with the pipeline name.
        job = AmbientJob(name="j", pipeline_name="definitely-not-a-real-pipeline-xyz")
        with pytest.raises(ValueError, match="definitely-not-a-real-pipeline-xyz"):
            _build_prompt(job)

    def test_skill_resolves_when_skill_md_exists(self, tmp_path, monkeypatch) -> None:
        skills = tmp_path / ".bog-agents" / "skills" / "smoke" / "SKILL.md"
        skills.parent.mkdir(parents=True)
        skills.write_text("Smoke skill body content.", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        job = AmbientJob(name="j", skill_name="smoke")
        prompt = _build_prompt(job)
        assert "smoke" in prompt
        assert "Smoke skill body content." in prompt

    def test_pipeline_resolves_when_yaml_exists(self, tmp_path, monkeypatch) -> None:
        pipeline_dir = tmp_path / ".bog-agents" / "pipelines"
        pipeline_dir.mkdir(parents=True)
        (pipeline_dir / "smoke.yaml").write_text(
            "name: smoke\ndescription: smoke pipeline\nsteps:\n  - id: do-thing\n    type: message\n    text: Do the thing\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        job = AmbientJob(name="j", pipeline_name="smoke")
        prompt = _build_prompt(job)
        assert "smoke pipeline" in prompt
        assert "Do the thing" in prompt

    def test_raises_when_nothing_configured(self) -> None:
        job = AmbientJob(name="j")
        with pytest.raises(ValueError, match="no prompt"):
            _build_prompt(job)


# ---------------------------------------------------------------------------
# _invoke_agent
# ---------------------------------------------------------------------------


class TestInvokeAgent:
    async def test_returns_last_ai_message(self) -> None:
        job = AmbientJob(name="j", prompt="do it")

        class _FakeMsg:
            type = "ai"
            content = "result text"

        async def _fake_astream(_input: object) -> object:
            yield {"node": {"messages": [_FakeMsg()]}}

        fake_agent = MagicMock()
        fake_agent.astream = _fake_astream

        with patch("bog_agents.create_agent", return_value=fake_agent):
            result = await _invoke_agent(job, "do it")

        assert result == "result text"

    async def test_timeout_raises(self) -> None:
        job = AmbientJob(name="j", prompt="slow")

        async def _slow_astream(_input: object) -> object:
            await asyncio.sleep(9999)
            return
            yield  # make it a generator

        fake_agent = MagicMock()
        fake_agent.astream = _slow_astream

        import bog_agents_daemon.runner as runner_mod

        original_timeout = runner_mod._AGENT_TIMEOUT_SECONDS
        runner_mod._AGENT_TIMEOUT_SECONDS = 0.05
        try:
            with patch("bog_agents.create_agent", return_value=fake_agent), pytest.raises(TimeoutError):
                await _invoke_agent(job, "slow")
        finally:
            runner_mod._AGENT_TIMEOUT_SECONDS = original_timeout


# ---------------------------------------------------------------------------
# run_job — end-to-end orchestration
# ---------------------------------------------------------------------------


class TestRunJob:
    async def test_successful_run_persisted(self, tmp_daemon_dir: Path) -> None:
        job = AmbientJob(name="succeed", prompt="go")

        with patch("bog_agents_daemon.runner._invoke_agent", new_callable=AsyncMock, return_value="output text"):
            run = await run_job(job, trigger_type=TriggerType.MANUAL)

        assert run.status == JobStatus.COMPLETED
        assert run.output == "output text"
        assert run.finished_at > 0

        # Job state updated in store
        updated = next(j for j in load_jobs() if j.job_id == job.job_id)
        assert updated.run_count == 1
        assert updated.last_status == JobStatus.COMPLETED

        # Run persisted to disk
        runs = list_runs(job_id=job.job_id)
        assert len(runs) == 1
        assert runs[0].status == JobStatus.COMPLETED

    async def test_failed_run_persisted(self, tmp_daemon_dir: Path) -> None:
        job = AmbientJob(name="fail", prompt="boom")

        async def _explode(_job: AmbientJob, _prompt: str, **_kwargs: object) -> str:
            msg = "agent exploded"
            raise RuntimeError(msg)

        with patch("bog_agents_daemon.runner._invoke_agent", side_effect=_explode):
            run = await run_job(job, trigger_type=TriggerType.MANUAL)

        assert run.status == JobStatus.FAILED
        assert "agent exploded" in run.error

        updated = next(j for j in load_jobs() if j.job_id == job.job_id)
        assert updated.last_status == JobStatus.FAILED

    async def test_trigger_context_stored(self, tmp_daemon_dir: Path) -> None:
        job = AmbientJob(name="ctx", prompt="x")
        ctx = {"ref": "refs/heads/main", "new_sha": "abc"}

        with patch("bog_agents_daemon.runner._invoke_agent", new_callable=AsyncMock, return_value="ok"):
            run = await run_job(job, trigger_type=TriggerType.GIT_PUSH, trigger_context=ctx)

        assert run.trigger_context == ctx
        assert run.trigger_type == TriggerType.GIT_PUSH


# ---------------------------------------------------------------------------
# _dispatch_file — path traversal guard
# ---------------------------------------------------------------------------


class TestDispatchFile:
    async def test_writes_to_tmp(self, tmp_path: Path) -> None:
        out_file = tmp_path / "output.txt"
        job = AmbientJob(name="j", prompt="x")

        from bog_agents_daemon.models import JobRun

        run = JobRun(job_id=job.job_id, job_name=job.name, output="hello")
        cfg = OutputConfig(target=OutputTarget.FILE, file_path=str(out_file), append=False)

        await _dispatch_file(run, cfg)
        assert out_file.exists()
        assert "hello" in out_file.read_text()

    async def test_rejects_etc_passwd(self, tmp_path: Path) -> None:
        from bog_agents_daemon.models import JobRun

        job = AmbientJob(name="j", prompt="x")
        run = JobRun(job_id=job.job_id, job_name=job.name, output="evil")
        cfg = OutputConfig(target=OutputTarget.FILE, file_path="/etc/passwd", append=False)

        # Should log error and return without writing — no exception raised
        await _dispatch_file(run, cfg)
        # /etc/passwd should be unchanged (we can't actually check this safely,
        # but the function must not raise)

    async def test_missing_file_path_warns(self, tmp_path: Path) -> None:
        from bog_agents_daemon.models import JobRun

        job = AmbientJob(name="j", prompt="x")
        run = JobRun(job_id=job.job_id, job_name=job.name, output="x")
        cfg = OutputConfig(target=OutputTarget.FILE, file_path="")
        # Must not raise
        await _dispatch_file(run, cfg)


# ---------------------------------------------------------------------------
# _dispatch_output — routing
# ---------------------------------------------------------------------------


class TestDispatchOutput:
    async def test_routes_to_file_handler(self, tmp_path: Path) -> None:
        from bog_agents_daemon.models import JobRun

        job = AmbientJob(name="j", prompt="x")
        run = JobRun(job_id=job.job_id, job_name=job.name, output="x")
        out_file = tmp_path / "routed.txt"
        cfg = OutputConfig(target=OutputTarget.FILE, file_path=str(out_file))

        await _dispatch_output(run, cfg)
        assert out_file.exists()

    async def test_log_target_does_not_raise(self, tmp_path: Path) -> None:
        from bog_agents_daemon.models import JobRun

        job = AmbientJob(name="j", prompt="x")
        run = JobRun(job_id=job.job_id, job_name=job.name, output="x")
        cfg = OutputConfig(target=OutputTarget.LOG)
        await _dispatch_output(run, cfg)  # no exception

    async def test_stdout_target_does_not_raise(self, tmp_path: Path) -> None:
        from bog_agents_daemon.models import JobRun

        job = AmbientJob(name="j", prompt="x")
        run = JobRun(job_id=job.job_id, job_name=job.name, output="x")
        cfg = OutputConfig(target=OutputTarget.STDOUT)
        await _dispatch_output(run, cfg)  # no exception
