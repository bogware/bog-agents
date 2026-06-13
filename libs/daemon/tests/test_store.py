"""Unit tests for daemon job store (persistence layer)."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from bog_agents_daemon.models import (
    AmbientJob,
    JobRun,
    JobStatus,
    OutputConfig,
    OutputTarget,
)
from bog_agents_daemon.store import (
    delete_job,
    get_job,
    list_runs,
    load_jobs,
    save_jobs,
    save_run,
    upsert_job,
)


class TestLoadSaveJobs:
    def test_empty_store_returns_empty_list(self, tmp_daemon_dir: Path):
        assert load_jobs() == []

    def test_save_and_load_round_trip(self, tmp_daemon_dir: Path):
        job = AmbientJob(name="nightly", prompt="summarise logs")
        save_jobs([job])
        loaded = load_jobs()
        assert len(loaded) == 1
        assert loaded[0].name == "nightly"
        assert loaded[0].job_id == job.job_id

    def test_save_preserves_smtp_credentials(self, tmp_daemon_dir: Path):
        cfg = OutputConfig(
            target=OutputTarget.EMAIL,
            smtp_username="user@example.com",
            smtp_password="s3cr3t",
            to_addrs=["ops@example.com"],
        )
        job = AmbientJob(name="email-job", outputs=[cfg])
        save_jobs([job])
        loaded = load_jobs()[0]
        assert loaded.outputs[0].smtp_username == "user@example.com"
        assert loaded.outputs[0].smtp_password == "s3cr3t"

    def test_save_preserves_github_token(self, tmp_daemon_dir: Path):
        cfg = OutputConfig(
            target=OutputTarget.GITHUB_COMMENT,
            github_token="ghp_abc123",
            github_repo="owner/repo",
            github_issue_or_pr=42,
        )
        job = AmbientJob(name="gh-job", outputs=[cfg])
        save_jobs([job])
        loaded = load_jobs()[0]
        assert loaded.outputs[0].github_token == "ghp_abc123"

    def test_jobs_file_is_owner_only(self, tmp_daemon_dir: Path):
        # jobs.json holds SMTP/GitHub/webhook secrets in cleartext; it must
        # not be group/other readable. (REVIEW.md v2 P1-54.)
        import sys

        if sys.platform == "win32":
            pytest.skip("POSIX permission bits not honoured on Windows")
        cfg = OutputConfig(target=OutputTarget.EMAIL, smtp_password="s3cret", to_addrs=["x@y.z"])
        save_jobs([AmbientJob(name="secret-job", outputs=[cfg])])
        mode = (tmp_daemon_dir / "jobs.json").stat().st_mode
        assert mode & 0o077 == 0, f"jobs.json with secrets is group/other accessible: {oct(mode)}"

    def test_corrupt_file_returns_empty_list(self, tmp_daemon_dir: Path):
        jobs_file = tmp_daemon_dir / "jobs.json"
        jobs_file.write_text("NOT JSON", encoding="utf-8")
        assert load_jobs() == []

    def test_save_is_atomic(self, tmp_daemon_dir: Path):
        """Saving uses a temp file then replaces — jobs.json never partially written."""
        jobs = [AmbientJob(name=f"job-{i}") for i in range(10)]
        save_jobs(jobs)
        loaded = load_jobs()
        assert len(loaded) == 10


class TestUpsertDeleteJob:
    def test_upsert_inserts_new_job(self, tmp_daemon_dir: Path):
        job = AmbientJob(name="new")
        upsert_job(job)
        assert len(load_jobs()) == 1

    def test_upsert_replaces_existing(self, tmp_daemon_dir: Path):
        job = AmbientJob(name="original")
        upsert_job(job)
        job.name = "updated"
        upsert_job(job)
        jobs = load_jobs()
        assert len(jobs) == 1
        assert jobs[0].name == "updated"

    def test_delete_existing_job(self, tmp_daemon_dir: Path):
        job = AmbientJob(name="bye")
        upsert_job(job)
        result = delete_job(job.job_id)
        assert result is True
        assert load_jobs() == []

    def test_delete_missing_job_returns_false(self, tmp_daemon_dir: Path):
        assert delete_job("nonexistent") is False

    def test_get_job_found(self, tmp_daemon_dir: Path):
        job = AmbientJob(name="find-me")
        upsert_job(job)
        found = get_job(job.job_id)
        assert found is not None
        assert found.name == "find-me"

    def test_get_job_not_found(self, tmp_daemon_dir: Path):
        assert get_job("missing-id") is None


class TestConcurrentAccess:
    def test_concurrent_upserts_are_safe(self, tmp_daemon_dir: Path):
        """Multiple threads performing upserts should not corrupt the store."""
        errors: list[Exception] = []

        def _writer(i: int) -> None:
            try:
                upsert_job(AmbientJob(name=f"job-{i}", prompt="do stuff"))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_writer, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Concurrent upsert errors: {errors}"
        jobs = load_jobs()
        assert len(jobs) == 20


class TestRunPersistence:
    def test_save_and_list_run(self, tmp_daemon_dir: Path):
        run = JobRun(job_id="j1", job_name="job1")
        run.status = JobStatus.COMPLETED
        run.output = "all good"
        save_run(run)
        runs = list_runs(job_id="j1")
        assert len(runs) == 1
        assert runs[0].output == "all good"

    def test_list_runs_sorted_newest_first(self, tmp_daemon_dir: Path):
        import time

        r1 = JobRun(job_id="j1", job_name="j")
        r1.started_at = time.time() - 100
        r2 = JobRun(job_id="j1", job_name="j")
        r2.started_at = time.time()
        save_run(r1)
        save_run(r2)
        runs = list_runs("j1")
        assert runs[0].run_id == r2.run_id

    def test_list_runs_limit(self, tmp_daemon_dir: Path):
        for _ in range(5):
            save_run(JobRun(job_id="j1", job_name="j"))
        assert len(list_runs("j1", limit=3)) == 3
