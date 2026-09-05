"""Daemon tools: `schedule`, `subscribe`, `list_subscriptions`, `unsubscribe` (ROADMAP #55).

An interactive agent can hand work to the ambient daemon and keep the thread:
every job these tools create carries the originating `thread_id` (read from
the tool runtime's `configurable.thread_id`) and a `goal_ref`, so when the
daemon fires it reopens the CLI's checkpointer on that thread and streams the
event as the next message — `/goal` state and memory survive the hand-off.

- `schedule(prompt, when)`: `"in 2 hours"`, `"at 09:30"`, an ISO datetime or a
  5-field cron (one-shot forms set `max_runs=1`; `"every 30 minutes"` recurs).
- `subscribe(source, prompt)`: `github:pr:123`, `github:issue:45`, `github`,
  `webhook:<path>`, `file:<dir>[:<glob>]`; `until_runs` caps the attempts.

The HTTP client is injectable (`DaemonClient` or any object with
`request(method, path, payload)`), so the bundle unit-tests without a daemon;
the default client reads the token the daemon writes under
`$BOG_AGENTS_HOME/daemon/token` and reports a missing daemon as a plain
`Error:` string instead of raising into the model.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langchain.tools import ToolRuntime  # noqa: TC002  # runtime type hint consumed by pydantic via StructuredTool
from langchain_core.tools import BaseTool, StructuredTool

if TYPE_CHECKING:
    from collections.abc import Callable

DEFAULT_DAEMON_URL = "http://127.0.0.1:7391"
_UNIT_SECONDS = {"minute": 60, "min": 60, "m": 60, "hour": 3600, "h": 3600, "day": 86400, "d": 86400, "week": 604800, "w": 604800}
_MAX_LIST = 50


class DaemonUnavailableError(RuntimeError):
    """The daemon could not be reached (not running, wrong token, or refused)."""


def bog_agents_home() -> Path:
    """`$BOG_AGENTS_HOME` or `~/.bog-agents` (the same rule the CLI uses)."""
    raw = os.environ.get("BOG_AGENTS_HOME", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".bog-agents"


def default_token_path() -> Path:
    """Where `bog-agents daemon start` writes its auth token."""
    return bog_agents_home() / "daemon" / "token"


def read_daemon_token(path: Path | None = None) -> str | None:
    """The daemon token, or `None` when it has never been started."""
    target = path or default_token_path()
    try:
        return target.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


@dataclass
class DaemonClient:
    """Minimal JSON client for the daemon's HTTP API."""

    base_url: str = DEFAULT_DAEMON_URL
    token: str | None = None
    timeout: float = 10.0

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:  # noqa: ANN401 - JSON
        """Send one request; raise `DaemonUnavailableError` with a readable reason on failure."""
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"X-Daemon-Token": self.token or "", "Content-Type": "application/json"}
        req = urllib.request.Request(f"{self.base_url}{path}", data=body, headers=headers, method=method)  # noqa: S310 - loopback daemon URL
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 - loopback daemon URL
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            msg = f"daemon returned HTTP {exc.code} for {method} {path}: {detail}"
            raise DaemonUnavailableError(msg) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            msg = f"daemon not reachable at {self.base_url} ({exc}); start it with `bog-agents daemon start`"
            raise DaemonUnavailableError(msg) from exc
        return json.loads(raw) if raw else None


# --------------------------------------------------------------------------- parsing


def _cron_for(moment: datetime) -> str:
    return f"{moment.minute} {moment.hour} {moment.day} {moment.month} *"


def _unit_seconds(unit: str) -> int:
    """Seconds for a unit word in any of its spellings (`m`, `min`, `minutes`, `h`, `hours`, `d`, `w`, …)."""
    key = unit.lower()
    if key not in _UNIT_SECONDS and key.endswith("s"):
        key = key[:-1]
    return _UNIT_SECONDS[key]


def parse_when(when: str, *, now: datetime | None = None) -> dict[str, Any]:
    """Turn a human schedule into a daemon trigger dict plus `max_runs` and a summary.

    Args:
        when: `"in 2 hours"`, `"in 45m"`, `"every 30 minutes"`, `"daily"`, `"hourly"`,
            `"at 09:30"`, an ISO datetime, or a 5-field cron expression.
        now: Reference time (tests).

    Returns:
        `{"trigger": {...}, "max_runs": int, "summary": str}`.

    Raises:
        ValueError: When `when` matches none of the forms.
    """
    text = when.strip().lower()
    base = now or datetime.now().astimezone()
    if not text:
        msg = "when must not be empty"
        raise ValueError(msg)
    match = re.fullmatch(r"in\s+(\d+)\s*(minutes?|mins?|m|hours?|h|days?|d|weeks?|w)", text)
    if match:
        moment = base + timedelta(seconds=int(match.group(1)) * _unit_seconds(match.group(2)))
        return {"trigger": {"type": "cron", "cron": _cron_for(moment)}, "max_runs": 1, "summary": f"once at {moment:%Y-%m-%d %H:%M}"}
    match = re.fullmatch(r"every\s+(\d+)\s*(minutes?|mins?|m|hours?|h|days?|d|weeks?|w)", text)
    if match:
        seconds = int(match.group(1)) * _unit_seconds(match.group(2))
        return {"trigger": {"type": "interval", "interval_seconds": seconds}, "max_runs": 0, "summary": f"every {seconds // 60} minutes"}
    if text in ("daily", "every day"):
        return {"trigger": {"type": "interval", "interval_seconds": 86400}, "max_runs": 0, "summary": "every day"}
    if text in ("hourly", "every hour"):
        return {"trigger": {"type": "interval", "interval_seconds": 3600}, "max_runs": 0, "summary": "every hour"}
    match = re.fullmatch(r"at\s+(\d{1,2}):(\d{2})", text)
    if match:
        moment = base.replace(hour=int(match.group(1)), minute=int(match.group(2)), second=0, microsecond=0)
        if moment <= base:
            moment += timedelta(days=1)
        return {"trigger": {"type": "cron", "cron": _cron_for(moment)}, "max_runs": 1, "summary": f"once at {moment:%Y-%m-%d %H:%M}"}
    try:
        moment = datetime.fromisoformat(when.strip())
    except ValueError:
        moment = None
    if moment is not None:
        return {"trigger": {"type": "cron", "cron": _cron_for(moment)}, "max_runs": 1, "summary": f"once at {moment:%Y-%m-%d %H:%M}"}
    if len(text.split()) == 5:  # noqa: PLR2004 - a cron expression has five fields
        return {"trigger": {"type": "cron", "cron": when.strip()}, "max_runs": 0, "summary": f"cron {when.strip()}"}
    msg = f"could not parse when={when!r}; use 'in 2 hours', 'every 30 minutes', 'at 09:30', an ISO datetime or a cron expression"
    raise ValueError(msg)


def parse_source(source: str) -> dict[str, Any]:
    """Turn a subscription source into a daemon trigger dict.

    Args:
        source: `github:pr:<n>`, `github:issue:<n>`, `github`, `webhook:<path>`,
            `file:<dir>[:<glob>]` or `cron:<expr>`.

    Returns:
        A trigger dict for the daemon API.

    Raises:
        ValueError: For an unknown source.
    """
    text = source.strip()
    lowered = text.lower()
    if lowered in ("github", "github:*", "github:repo"):
        return {"type": "github"}
    match = re.fullmatch(r"github:(pr|pull|issue):(\d+)", lowered)
    if match:
        kinds = (
            ["pr_review_comment", "pr_comment", "ci_failure", "pr_review"]
            if match.group(1) in ("pr", "pull")
            else ["issue_comment", "issue_assigned", "issue_labeled"]
        )
        return {"type": "github", "github_number": int(match.group(2)), "github_kinds": kinds}
    if lowered.startswith("webhook:"):
        path = text.split(":", 1)[1].strip().lstrip("/")
        if not path:
            msg = "webhook source needs a path, e.g. webhook:deploys"
            raise ValueError(msg)
        return {"type": "webhook", "webhook_path": path}
    if lowered.startswith("file:"):
        parts = text.split(":", 2)
        watch_dir = parts[1].strip() if len(parts) > 1 else ""
        pattern = parts[2].strip() if len(parts) > 2 else "*"  # noqa: PLR2004 - dir:glob form
        if not watch_dir:
            msg = "file source needs a directory, e.g. file:src:*.py"
            raise ValueError(msg)
        return {"type": "file_change", "watch_dir": watch_dir, "watch_patterns": [pattern or "*"]}
    if lowered.startswith("cron:"):
        return {"type": "cron", "cron": text.split(":", 1)[1].strip()}
    msg = f"unknown source {source!r}; use github:pr:<n>, github:issue:<n>, github, webhook:<path>, file:<dir>[:<glob>] or cron:<expr>"
    raise ValueError(msg)


def build_job_payload(
    *,
    name: str,
    prompt: str,
    trigger: dict[str, Any],
    max_runs: int,
    thread_id: str,
    goal_ref: str,
    working_dir: str,
    model: str = "",
) -> dict[str, Any]:
    """The `POST /jobs` body for a thread-linked job."""
    return {
        "name": name[:200],
        "description": f"created from thread {thread_id}" if thread_id else "created by the agent",
        "prompt": prompt,
        "model": model,
        "working_dir": working_dir,
        "max_runs": max(0, int(max_runs)),
        "thread_id": thread_id,
        "goal_ref": goal_ref,
        "triggers": [trigger],
        "outputs": [{"target": "log"}],
        "enabled": True,
    }


def thread_id_from_runtime(runtime: Any) -> str:  # noqa: ANN401 - ToolRuntime
    """The interactive thread a tool call belongs to (`configurable.thread_id`), or `""`."""
    config = getattr(runtime, "config", None)
    if isinstance(config, dict):
        configurable = config.get("configurable") or {}
        if isinstance(configurable, dict) and configurable.get("thread_id"):
            return str(configurable["thread_id"])
    return ""


# --------------------------------------------------------------------------- bundle


def daemon_tools_bundle(
    *,
    client: Any = None,  # noqa: ANN401 - DaemonClient or any object with request(method, path, payload)
    base_url: str | None = None,
    token: str | None = None,
    thread_id: str | None = None,
    working_dir: str | Path | None = None,
    goal_ref: str | Path | None = None,
    model: str = "",
    clock: Callable[[], datetime] | None = None,
) -> list[BaseTool]:
    """Return `schedule`, `subscribe`, `list_subscriptions` and `unsubscribe`.

    Args:
        client: Injected HTTP client (tests); built from `base_url` / `token` when `None`.
        base_url: Daemon URL (default `http://127.0.0.1:7391`, env `BOG_DAEMON_URL`).
        token: Daemon token (default: the token file under the bog home).
        thread_id: Originating thread; read from the tool runtime when `None`.
        working_dir: Directory the daemon runs the job in (default: cwd).
        goal_ref: Path of the thread's goal file, carried on the job.
        model: Model spec for the daemon job (`""` = daemon default).
        clock: Reference time for schedule parsing (tests).

    Returns:
        Four `StructuredTool`s.
    """
    api = client or DaemonClient(base_url=base_url or os.environ.get("BOG_DAEMON_URL") or DEFAULT_DAEMON_URL, token=token or read_daemon_token())
    wd = str(working_dir or Path.cwd())
    goal = str(goal_ref) if goal_ref else ""

    def _thread(runtime: Any) -> str:  # noqa: ANN401 - ToolRuntime
        return thread_id or thread_id_from_runtime(runtime)

    def _create(payload: dict[str, Any]) -> str:
        try:
            job = api.request("POST", "/jobs", payload)
        except DaemonUnavailableError as exc:
            return f"Error: {exc}"
        return job.get("job_id", "?") if isinstance(job, dict) else "?"

    def schedule(runtime: ToolRuntime[None, Any], prompt: str, when: str, name: str = "") -> str:
        """Schedule a prompt to run later through the ambient daemon, on this same thread.

        `when` accepts "in 2 hours", "in 45m", "at 09:30", an ISO datetime,
        "every 30 minutes" / "daily" / "hourly", or a 5-field cron expression.
        One-shot forms run once; recurring forms keep running until unsubscribed.
        """
        try:
            parsed = parse_when(when, now=clock() if clock else None)
        except ValueError as exc:
            return f"Error: {exc}"
        thread = _thread(runtime)
        payload = build_job_payload(
            name=name or f"scheduled: {prompt[:40]}",
            prompt=prompt,
            trigger=parsed["trigger"],
            max_runs=parsed["max_runs"],
            thread_id=thread,
            goal_ref=goal,
            working_dir=wd,
            model=model,
        )
        job_id = _create(payload)
        if job_id.startswith("Error:"):
            return job_id
        where = f" on thread {thread}" if thread else ""
        return f"Scheduled job {job_id} ({parsed['summary']}){where}. It shows under /tasks; unsubscribe('{job_id}') cancels it."

    def subscribe(runtime: ToolRuntime[None, Any], source: str, prompt: str, until_runs: int = 3, name: str = "") -> str:
        """Subscribe this thread to an event source; each event runs `prompt` with the event as context.

        Sources: github:pr:<n> (review comments, CI failures on that PR),
        github:issue:<n>, github (every event), webhook:<path>, file:<dir>[:<glob>].
        `until_runs` caps how many times the job may fire (0 = unlimited).
        """
        try:
            trigger = parse_source(source)
        except ValueError as exc:
            return f"Error: {exc}"
        thread = _thread(runtime)
        payload = build_job_payload(
            name=name or f"subscription: {source}",
            prompt=prompt,
            trigger=trigger,
            max_runs=until_runs,
            thread_id=thread,
            goal_ref=goal,
            working_dir=wd,
            model=model,
        )
        job_id = _create(payload)
        if job_id.startswith("Error:"):
            return job_id
        cap = f"up to {until_runs} run(s)" if until_runs else "unlimited runs"
        return f"Subscribed job {job_id} to {source} ({cap}); the thread resumes with each event. unsubscribe('{job_id}') stops it."

    def list_subscriptions(runtime: ToolRuntime[None, Any]) -> str:
        """List the daemon jobs created from this thread (all jobs when the thread is unknown)."""
        try:
            jobs = api.request("GET", "/jobs")
        except DaemonUnavailableError as exc:
            return f"Error: {exc}"
        thread = _thread(runtime)
        rows = [j for j in (jobs or []) if isinstance(j, dict) and (not thread or j.get("thread_id") == thread)]
        if not rows:
            return "No daemon jobs for this thread."
        lines = []
        for job in rows[:_MAX_LIST]:
            triggers = ", ".join(str(t.get("type", "?")) for t in job.get("triggers", []) if isinstance(t, dict))
            cap = f"{job.get('run_count', 0)}/{job.get('max_runs') or '∞'} runs"
            state = "enabled" if job.get("enabled", True) else "disabled"
            lines.append(f"- {job.get('job_id')} {job.get('name')} [{triggers}] {cap} {state}")
        return "\n".join(lines)

    def unsubscribe(runtime: ToolRuntime[None, Any], job_id: str) -> str:
        """Delete a daemon job created with schedule or subscribe."""
        del runtime
        try:
            api.request("DELETE", f"/jobs/{job_id}")
        except DaemonUnavailableError as exc:
            return f"Error: {exc}"
        return f"Deleted daemon job {job_id}."

    return [
        StructuredTool.from_function(func=schedule, name="schedule"),
        StructuredTool.from_function(func=subscribe, name="subscribe"),
        StructuredTool.from_function(func=list_subscriptions, name="list_subscriptions"),
        StructuredTool.from_function(func=unsubscribe, name="unsubscribe"),
    ]


__all__ = [
    "DEFAULT_DAEMON_URL",
    "DaemonClient",
    "DaemonUnavailableError",
    "bog_agents_home",
    "build_job_payload",
    "daemon_tools_bundle",
    "default_token_path",
    "parse_source",
    "parse_when",
    "read_daemon_token",
    "thread_id_from_runtime",
]
