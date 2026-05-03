"""Peat job model and on-disk persistence.

A *Peat job* is a saved task description plus a schedule. When the
scheduler fires a job, Peat (the sub-agent) executes the task with the
restrictive scheduled-tool set and writes the run output to
``~/.bog-agents/peat/runs/<job_id>/<run_id>.md``.

YAML schema::

    job_id:    "morning-brief"               # stable id, also the file stem
    name:      "Morning brief"
    prompt:    |
      Read the latest /qa results from yesterday and summarize them
      in a 5-bullet brief. Save to peat/digests/<date>.md.
    schedule:  "0 9 * * 1-5"                 # cron, or "@once @ 2026-05-04T09:00:00Z"
    enabled:   true
    concurrent: false                        # forbid overlapping runs
    timeout_s: 600                           # hard cap on each run
    last_fired_at: 1732208122.0              # mutable, scheduler updates
    next_fire_at: 1732208122.0               # mutable, scheduler updates
    run_count: 14
    notify_inbox: true                       # write inbox.json entry on each run
    on_failure: "notify"                     # "notify" | "disable" | "silent"
    vars:                                    # optional per-job vars
      target_dir: { type: string, default: "/work/myproject" }
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


@dataclass
class PeatJob:
    """A scheduled or one-shot Peat task."""

    job_id: str
    name: str = ""
    prompt: str = ""
    schedule: str = ""
    """Either a cron expression (5-field) or ``"@once @ <ISO-8601>"`` for one-shots, or empty for manual-only."""
    enabled: bool = True
    concurrent: bool = False
    timeout_s: int = 600
    notify_inbox: bool = True
    on_failure: str = "notify"
    """``notify`` (write inbox + keep enabled), ``disable`` (auto-disable after 3 fails), or ``silent``."""
    last_fired_at: float = 0.0
    next_fire_at: float = 0.0
    run_count: int = 0
    consecutive_failures: int = 0
    vars_spec: dict[str, dict[str, Any]] = field(default_factory=dict)
    created_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "name": self.name,
            "prompt": self.prompt,
            "schedule": self.schedule,
            "enabled": self.enabled,
            "concurrent": self.concurrent,
            "timeout_s": self.timeout_s,
            "notify_inbox": self.notify_inbox,
            "on_failure": self.on_failure,
            "last_fired_at": self.last_fired_at,
            "next_fire_at": self.next_fire_at,
            "run_count": self.run_count,
            "consecutive_failures": self.consecutive_failures,
            "vars": self.vars_spec,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PeatJob:
        return cls(
            job_id=str(d.get("job_id") or _new_job_id()),
            name=str(d.get("name", "")),
            prompt=str(d.get("prompt", "")),
            schedule=str(d.get("schedule", "")),
            enabled=bool(d.get("enabled", True)),
            concurrent=bool(d.get("concurrent")),
            timeout_s=int(d.get("timeout_s", 600)),
            notify_inbox=bool(d.get("notify_inbox", True)),
            on_failure=str(d.get("on_failure", "notify")),
            last_fired_at=float(d.get("last_fired_at", 0.0) or 0.0),
            next_fire_at=float(d.get("next_fire_at", 0.0) or 0.0),
            run_count=int(d.get("run_count", 0)),
            consecutive_failures=int(d.get("consecutive_failures", 0)),
            vars_spec=dict(d.get("vars", {}) or {}),
            created_at=float(d.get("created_at", 0.0) or 0.0),
        )


@dataclass
class PeatJobRun:
    """A single execution of a job — captures its output and verdict."""

    job_id: str
    run_id: str
    started_at: float
    duration_s: float
    status: str  # "ok" | "fail" | "timeout" | "cancelled"
    summary: str = ""
    output_path: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _new_job_id() -> str:
    """Return a stable, sortable job id (timestamp + short uuid)."""
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    short = uuid.uuid4().hex[:6]
    return f"job-{stamp}-{short}"


# ---------------------------------------------------------------------------
# Save / load
# ---------------------------------------------------------------------------


def jobs_dir(config_dir: Path) -> Path:
    """Return ``<config_dir>/peat/jobs/`` (created on demand)."""
    d = config_dir / "peat" / "jobs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_job(config_dir: Path, job: PeatJob) -> Path:
    """Save ``job`` as YAML to the jobs directory and return the file path."""
    if not job.job_id:
        job.job_id = _new_job_id()
    if not job.created_at:
        job.created_at = time.time()
    path = jobs_dir(config_dir) / f"{job.job_id}.yaml"
    text = "# Peat job — edit freely. Manage via `/peat jobs`.\n"
    text += "# Schedule formats:  cron expression (5 fields)  |  '@once @ ISO-8601'\n\n"
    text += yaml.safe_dump(job.to_dict(), sort_keys=False, allow_unicode=True)
    path.write_text(text, encoding="utf-8")
    return path


def load_job(path: Path) -> PeatJob:
    """Load a job from a YAML file."""
    raw = path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw) or {}
    if not isinstance(data, dict):
        msg = f"job {path} did not parse to a dict"
        raise ValueError(msg)
    return PeatJob.from_dict(data)


def list_jobs(config_dir: Path) -> list[PeatJob]:
    """Return all saved jobs, sorted by created_at descending (newest first)."""
    d = config_dir / "peat" / "jobs"
    if not d.exists():
        return []
    out: list[PeatJob] = []
    for path in sorted(d.iterdir()):
        if path.suffix.lower() not in (".yaml", ".yml"):
            continue
        try:
            out.append(load_job(path))
        except (yaml.YAMLError, OSError, ValueError) as exc:
            logger.warning("skipping unparseable peat job %s: %s", path, exc)
    out.sort(key=lambda j: j.created_at, reverse=True)
    return out


def find_job(config_dir: Path, token: str) -> Path | None:
    """Resolve ``token`` to a job file (exact match, then substring)."""
    d = config_dir / "peat" / "jobs"
    if not d.exists():
        return None
    for ext in (".yaml", ".yml"):
        candidate = d / f"{token}{ext}"
        if candidate.is_file():
            return candidate
    matches = sorted(p for p in d.iterdir() if token in p.stem)
    return matches[0] if matches else None


def delete_job(config_dir: Path, token: str) -> Path | None:
    """Delete a job file. Returns the deleted path, or None if nothing matched."""
    path = find_job(config_dir, token)
    if path is None:
        return None
    path.unlink()
    return path
