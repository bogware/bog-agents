"""Unit tests for daemon data models."""

from __future__ import annotations

from bog_agents_daemon.models import (
    AmbientJob,
    JobRun,
    JobStatus,
    OutputConfig,
    OutputTarget,
    TriggerConfig,
    TriggerType,
)


class TestAmbientJob:
    def test_default_job_id_generated(self):
        job = AmbientJob(name="test")
        assert len(job.job_id) == 12
        assert job.enabled is True
        assert job.run_count == 0

    def test_two_jobs_have_different_ids(self):
        assert AmbientJob().job_id != AmbientJob().job_id

    def test_job_status_defaults_to_pending(self):
        job = AmbientJob()
        assert job.last_status == JobStatus.PENDING


class TestOutputConfig:
    def test_smtp_credentials_fields_exist(self):
        cfg = OutputConfig(target=OutputTarget.EMAIL)
        assert cfg.smtp_username == ""
        assert cfg.smtp_password == ""

    def test_github_token_field_exists(self):
        cfg = OutputConfig(target=OutputTarget.GITHUB_COMMENT)
        assert cfg.github_token == ""

    def test_file_output_defaults(self):
        cfg = OutputConfig(target=OutputTarget.FILE, file_path="/tmp/out.log")
        assert cfg.append is True
        assert cfg.file_path == "/tmp/out.log"


class TestTriggerConfig:
    def test_file_change_trigger_defaults(self):
        t = TriggerConfig(type=TriggerType.FILE_CHANGE)
        assert t.debounce_seconds == 5.0
        assert t.watch_patterns == []

    def test_webhook_trigger_has_secret_field(self):
        t = TriggerConfig(type=TriggerType.WEBHOOK, webhook_secret="s3cr3t")
        assert t.webhook_secret == "s3cr3t"

    def test_git_push_trigger_branch_pattern(self):
        t = TriggerConfig(type=TriggerType.GIT_PUSH, git_branch_pattern="main")
        assert t.git_branch_pattern == "main"


class TestJobRun:
    def test_run_id_generated(self):
        run = JobRun(job_id="abc", job_name="test")
        assert len(run.run_id) == 12

    def test_defaults(self):
        run = JobRun()
        assert run.status == JobStatus.RUNNING
        assert run.finished_at == 0.0
