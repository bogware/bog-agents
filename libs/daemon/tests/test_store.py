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
    reconcile_orphaned_runs,
    record_run_result,
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


class TestCorruptQuarantine:
    """DMN-4b: an unreadable jobs.json is moved aside, never silently overwritten."""

    def test_invalid_json_is_quarantined(self, tmp_daemon_dir: Path):
        jobs_file = tmp_daemon_dir / "jobs.json"
        jobs_file.write_text("NOT JSON", encoding="utf-8")
        assert load_jobs() == []
        # The original bytes are preserved in a quarantine sibling, and the
        # live path is gone so it can't be clobbered.
        assert not jobs_file.exists()
        quarantined = list(tmp_daemon_dir.glob("jobs.json.corrupt-*"))
        assert len(quarantined) == 1
        assert quarantined[0].read_text(encoding="utf-8") == "NOT JSON"

    def test_non_list_json_is_quarantined(self, tmp_daemon_dir: Path):
        jobs_file = tmp_daemon_dir / "jobs.json"
        jobs_file.write_text('{"not": "a list"}', encoding="utf-8")
        assert load_jobs() == []
        assert list(tmp_daemon_dir.glob("jobs.json.corrupt-*"))

    def test_next_save_does_not_destroy_quarantined_jobs(self, tmp_daemon_dir: Path):
        # The core data-loss guard: after a corrupt read, a subsequent write
        # must not overwrite the preserved copy.
        jobs_file = tmp_daemon_dir / "jobs.json"
        jobs_file.write_text('[{"job_id": "keepme"}]... truncated garbage', encoding="utf-8")
        original = jobs_file.read_text(encoding="utf-8")
        assert load_jobs() == []
        save_jobs([AmbientJob(name="fresh")])  # would previously clobber the original
        quarantined = list(tmp_daemon_dir.glob("jobs.json.corrupt-*"))
        assert len(quarantined) == 1
        assert quarantined[0].read_text(encoding="utf-8") == original

    def test_transient_oserror_does_not_quarantine(self, tmp_daemon_dir: Path, monkeypatch: pytest.MonkeyPatch):
        # A momentary read error must NOT move a possibly-good file aside.
        import bog_agents_daemon.store as store_mod

        job = AmbientJob(name="valuable")
        save_jobs([job])

        real_read_text = Path.read_text
        state = {"fail_next": True}

        def _flaky_read(self: Path, *args: object, **kwargs: object) -> str:
            # Fail the very first read of the jobs file, then behave normally —
            # simulating a momentary lock/permission blip that clears.
            if self == store_mod._JOBS_FILE and state["fail_next"]:
                state["fail_next"] = False
                raise OSError("device busy")
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", _flaky_read)
        # First read hits the transient error → empty list, but NO quarantine.
        assert load_jobs() == []
        assert not list(tmp_daemon_dir.glob("jobs.json.corrupt-*"))
        # Once the blip clears the untouched file loads fine.
        assert len(load_jobs()) == 1


class TestOrphanedRunRecovery:
    """A run left RUNNING by a crashed daemon is reconciled to FAILED on startup."""

    def test_running_run_reconciled_to_failed(self, tmp_daemon_dir: Path):
        orphan = JobRun(job_id="j1", job_name="j", status=JobStatus.RUNNING)
        orphan.finished_at = 0.0
        save_run(orphan)

        count = reconcile_orphaned_runs()
        assert count == 1

        reloaded = list_runs("j1")[0]
        assert reloaded.status == JobStatus.FAILED
        assert reloaded.finished_at > 0
        assert "interrupted" in reloaded.error.lower()

    def test_completed_run_left_untouched(self, tmp_daemon_dir: Path):
        done = JobRun(job_id="j1", job_name="j", status=JobStatus.COMPLETED, output="ok")
        done.finished_at = 123.0
        save_run(done)

        assert reconcile_orphaned_runs() == 0
        reloaded = list_runs("j1")[0]
        assert reloaded.status == JobStatus.COMPLETED
        assert reloaded.finished_at == 123.0

    def test_reconcile_is_idempotent(self, tmp_daemon_dir: Path):
        orphan = JobRun(job_id="j1", job_name="j", status=JobStatus.RUNNING)
        orphan.finished_at = 0.0
        save_run(orphan)
        assert reconcile_orphaned_runs() == 1
        # A second pass finds nothing left to fix.
        assert reconcile_orphaned_runs() == 0

    def test_save_is_atomic(self, tmp_daemon_dir: Path):
        """Saving uses a temp file then replaces — jobs.json never partially written."""
        jobs = [AmbientJob(name=f"job-{i}") for i in range(10)]
        save_jobs(jobs)
        loaded = load_jobs()
        assert len(loaded) == 10

    def test_retry_policy_survives_round_trip(self, tmp_daemon_dir: Path):
        job = AmbientJob(name="r", prompt="x", max_retries=3, retry_backoff_seconds=5.5)
        save_jobs([job])
        loaded = load_jobs()[0]
        assert loaded.max_retries == 3
        assert loaded.retry_backoff_seconds == 5.5

    def test_legacy_job_without_retry_fields_defaults(self, tmp_daemon_dir: Path):
        # A jobs.json written before retry existed must load with safe defaults.
        jobs_file = tmp_daemon_dir / "jobs.json"
        jobs_file.write_text('[{"job_id": "old", "name": "legacy"}]', encoding="utf-8")
        loaded = load_jobs()[0]
        assert loaded.max_retries == 0
        assert loaded.retry_backoff_seconds == 2.0


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

    def test_record_run_result_preserves_concurrent_config_edit(self, tmp_daemon_dir: Path):
        # P1-56: a run finishing must merge only run-state fields, not clobber
        # a config edit (e.g. prompt) that landed while the run was in flight.
        job = AmbientJob(name="j", prompt="original prompt")
        upsert_job(job)
        # Simulate a config edit committed mid-run (new prompt on disk).
        edited = get_job(job.job_id)
        assert edited is not None
        edited.prompt = "edited mid-run"
        upsert_job(edited)
        # The runner finishes with its STALE snapshot (still has the old prompt).
        record_run_result(
            job,
            last_run_at=123.0,
            last_status=JobStatus.COMPLETED,
            last_output="done",
        )
        merged = get_job(job.job_id)
        assert merged is not None
        assert merged.prompt == "edited mid-run"  # config edit survived
        assert merged.last_status == JobStatus.COMPLETED  # run-state applied
        assert merged.last_run_at == 123.0
        assert merged.run_count == 1


class TestRunAttempts:
    def test_attempts_survive_disk_round_trip(self, tmp_daemon_dir: Path):
        run = JobRun(run_id="r1", job_id="j1", job_name="j", attempts=4)
        save_run(run)
        assert list_runs("j1")[0].attempts == 4

    def test_legacy_run_without_attempts_defaults_to_one(self, tmp_daemon_dir: Path):
        runs_dir = tmp_daemon_dir / "runs"
        (runs_dir / "j1_r1.json").write_text(
            '{"run_id": "r1", "job_id": "j1", "job_name": "j", "status": "completed"}',
            encoding="utf-8",
        )
        assert list_runs("j1")[0].attempts == 1


class TestRunDispatchErrors:
    def test_dispatch_errors_survive_disk_round_trip(self, tmp_daemon_dir: Path):
        # P1-52: dispatch_errors used to reset to [] on every read-back.
        run = JobRun(
            run_id="r1",
            job_id="j1",
            job_name="j",
            status=JobStatus.COMPLETED,
            dispatch_errors=[{"target": "slack", "error": "401 Unauthorized"}],
        )
        save_run(run)
        loaded = list_runs("j1")
        assert loaded
        assert loaded[0].dispatch_errors == [{"target": "slack", "error": "401 Unauthorized"}]


class TestRunWriteAtomicity:
    """DMN-10: run records are written via temp file + rename, and a corrupt
    run file is logged loudly (not silently skipped) by the loaders."""

    def test_corrupt_run_file_logged_and_skipped_by_list_runs(self, tmp_daemon_dir: Path, caplog: pytest.LogCaptureFixture):
        import logging

        good = JobRun(job_id="j1", job_name="j", status=JobStatus.COMPLETED, output="ok")
        save_run(good)
        # Simulate a crash mid-write under the OLD in-place scheme: a truncated file.
        (tmp_daemon_dir / "runs" / "j1_truncated.json").write_text('{"run_id": "r2", "job_id": "j1", "sta', encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="bog_agents_daemon.store"):
            runs = list_runs("j1")

        # The valid run still loads; the corrupt one is skipped with a WARNING.
        assert [r.run_id for r in runs] == [good.run_id]
        assert "Skipping unreadable run file" in caplog.text
        assert "j1_truncated.json" in caplog.text

    def test_corrupt_run_file_logged_during_reconciliation(self, tmp_daemon_dir: Path, caplog: pytest.LogCaptureFixture):
        import logging

        orphan = JobRun(job_id="j1", job_name="j", status=JobStatus.RUNNING)
        orphan.finished_at = 0.0
        save_run(orphan)
        (tmp_daemon_dir / "runs" / "j1_corrupt.json").write_text("NOT JSON", encoding="utf-8")

        from bog_agents_daemon.store import reconcile_orphaned_runs

        with caplog.at_level(logging.WARNING, logger="bog_agents_daemon.store"):
            count = reconcile_orphaned_runs()

        # The corrupt file doesn't stop reconciliation of the healthy orphan.
        assert count == 1
        assert "Skipping unreadable run file" in caplog.text

    def test_save_run_leaves_no_tmp_sibling(self, tmp_daemon_dir: Path):
        save_run(JobRun(job_id="j1", job_name="j"))
        runs_dir = tmp_daemon_dir / "runs"
        assert not list(runs_dir.glob("*.tmp"))
        assert len(list(runs_dir.glob("j1_*.json"))) == 1

    def test_serialization_failure_touches_nothing(self, tmp_daemon_dir: Path):
        # A failure before any I/O must leave neither a destination nor a tmp file.
        from bog_agents_daemon.store import _write_json_durable

        target = tmp_daemon_dir / "runs" / "j1_r1.json"
        with pytest.raises(TypeError):
            _write_json_durable(target, {"bad": object()})
        assert not target.exists()
        assert not list((tmp_daemon_dir / "runs").glob("*.tmp"))

    def test_failed_rename_preserves_original_and_cleans_tmp(self, tmp_daemon_dir: Path, monkeypatch: pytest.MonkeyPatch):
        # A crash/failure at the rename step must leave the previously
        # committed record intact — never a partial file at the final path.
        from bog_agents_daemon.store import _write_json_durable

        target = tmp_daemon_dir / "runs" / "j1_r1.json"
        _write_json_durable(target, {"run_id": "r1", "status": "completed"})
        original = target.read_text(encoding="utf-8")

        def _boom(self: Path, other: Path) -> Path:
            raise OSError("disk full")

        monkeypatch.setattr(Path, "replace", _boom)
        with pytest.raises(OSError):
            _write_json_durable(target, {"run_id": "r1", "status": "clobbered"})

        assert target.read_text(encoding="utf-8") == original
        assert not list((tmp_daemon_dir / "runs").glob("*.tmp"))


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
