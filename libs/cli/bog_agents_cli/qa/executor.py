"""QA plan executor — hybrid agent / shell / http / mcp step runner.

The executor walks a :class:`QAPlan`, runs each :class:`QAStep` according
to its ``kind``, and aggregates per-AC verdicts. Variable substitution
flows through a :class:`VarBundle`; secrets are unwrapped only at the
boundary (shell env, HTTP header, MCP arg) and never logged.

The executor is deliberately *injectable* — callers pass in:

- a ``run_agent_step`` async callback (so we can use the live agent) and
- a ``run_mcp_step`` async callback (so we don't import LangChain here).

Shell and HTTP have no live-system dependencies and are executed inline.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shlex
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from bog_agents_cli.qa.plan import QAPlan, QAStep, StepKind, StepVerdict
from bog_agents_cli.vault import SecretStr

if TYPE_CHECKING:
    from bog_agents_cli.vars import VarBundle

logger = logging.getLogger(__name__)


# Public callback signatures.
AgentStepFn = Callable[[QAStep, str], Awaitable["StepResult"]]
"""Agent step runner.

Args:
    step: The QAStep being executed (kind=AGENT).
    prompt: The substituted prompt text.
"""

MCPStepFn = Callable[[QAStep, dict[str, Any]], Awaitable["StepResult"]]
"""MCP step runner.

Args:
    step: The QAStep being executed (kind=MCP).
    rendered_args: Tool args with vars substituted and secrets unwrapped.
"""


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class StepResult:
    """Outcome of a single step's execution."""

    step_id: str
    kind: str
    started_at: float
    duration_s: float
    passed: bool
    reason: str = ""
    output: str = ""
    exit_code: int | None = None
    status_code: int | None = None
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ACOutcome:
    """Aggregate verdict for one AC across all steps that targeted it."""

    ac_id: str
    text: str
    verdict: str  # "pass" | "fail" | "inconclusive"
    step_ids: list[str] = field(default_factory=list)
    failed_step_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExecutionResult:
    """Full QA run result."""

    plan_id: str
    run_id: str
    started_at: float
    duration_s: float
    overall_verdict: str  # "pass" | "fail" | "inconclusive"
    step_results: list[StepResult] = field(default_factory=list)
    ac_outcomes: list[ACOutcome] = field(default_factory=list)
    aborted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "duration_s": self.duration_s,
            "overall_verdict": self.overall_verdict,
            "aborted": self.aborted,
            "step_results": [s.to_dict() for s in self.step_results],
            "ac_outcomes": [a.to_dict() for a in self.ac_outcomes],
        }


# ---------------------------------------------------------------------------
# Inline executors
# ---------------------------------------------------------------------------


def _run_shell_sync(rendered_run: str, cwd: str, env: dict[str, str] | None, timeout_s: int) -> tuple[int, str, str]:
    """Synchronous shell runner — wrapped in an executor by the async caller.

    Two Windows-specific footguns we work around here:

    1. ``subprocess.run(..., shell=True, timeout=...)`` on Windows does
       NOT kill the grandchild process when timeout fires (cmd.exe is
       killed but the spawned executable stays alive holding the pipes,
       so ``communicate()`` hangs). We detect timeout via ``Popen`` +
       manual wait and then tree-kill.
    2. We must launch with ``CREATE_NEW_PROCESS_GROUP`` so we can
       terminate the whole tree, not just cmd.exe.

    Returns:
        ``(exit_code, body, error)`` where ``error`` is non-empty only on
        a launch failure or timeout.
    """
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    try:
        proc = subprocess.Popen(  # noqa: S602
            rendered_run,
            shell=True,
            cwd=cwd or None,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            start_new_session=(sys.platform != "win32"),
        )
    except OSError as exc:
        return -1, "", f"launch_error: {exc}"
    try:
        stdout, _ = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        # Drain whatever was already written so we can return it.
        try:
            stdout, _ = proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            stdout = b""
        body = (stdout or b"").decode("utf-8", errors="replace")
        return -1, body, "timeout"
    body = (stdout or b"").decode("utf-8", errors="replace")
    return proc.returncode, body, ""


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    """Best-effort tree-kill — wrap shell-orchestrated child processes.

    On Windows we shell out to ``taskkill /F /T`` which terminates the
    entire process tree rooted at our pid. On POSIX a simple ``proc.kill``
    is enough since we launched with ``start_new_session=True``.
    """
    if sys.platform == "win32":
        # taskkill returncode meanings:
        #   0   = process(es) terminated
        #   128 = pid not found (process already gone — nothing to kill)
        #   anything else = something else went wrong; fall back to
        #     ``proc.kill()`` so we at least take a swing at the parent
        # The previous code only fell back on OSError / TimeoutExpired,
        # which meant ``check=False`` quietly hid these other-failure
        # cases.
        try:
            result = subprocess.run(  # noqa: S603
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            with contextlib.suppress(Exception):
                proc.kill()
            return
        if result.returncode not in (0, 128):
            with contextlib.suppress(Exception):
                proc.kill()
    else:
        with contextlib.suppress(Exception):
            proc.kill()


async def _run_shell_step(step: QAStep, bundle: VarBundle) -> StepResult:
    """Run a shell step. Captures stdout+stderr, applies verdict rules."""
    rendered_run = bundle.substitute(step.run)
    rendered_env = bundle.substitute(step.env) if step.env else {}
    rendered_env = bundle.vault.render(rendered_env)
    started = time.time()
    loop = asyncio.get_running_loop()
    exit_code, body, error = await loop.run_in_executor(
        None,
        _run_shell_sync,
        rendered_run,
        step.cwd,
        _merged_env(rendered_env),
        step.timeout_s,
    )
    duration = time.time() - started
    if error == "timeout":
        return StepResult(
            step_id=step.id,
            kind=step.kind.value,
            started_at=started,
            duration_s=duration,
            passed=False,
            reason=f"timed out after {step.timeout_s}s",
            error="timeout",
        )
    if error.startswith("launch_error"):
        return StepResult(
            step_id=step.id,
            kind=step.kind.value,
            started_at=started,
            duration_s=duration,
            passed=False,
            reason=f"failed to launch: {error[len('launch_error: '):]}",
            error=error,
        )
    # Default expectation: exit_code 0 unless verdict overrides.
    verdict = step.verdict if not step.verdict.is_empty() else StepVerdict(exit_code=0)
    passed, reason = verdict.evaluate(exit_code=exit_code, body=body)
    return StepResult(
        step_id=step.id,
        kind=step.kind.value,
        started_at=started,
        duration_s=duration,
        passed=passed,
        reason=reason,
        output=body[:8000],
        exit_code=exit_code,
    )


def _merged_env(extra: dict[str, Any]) -> dict[str, str] | None:
    """Merge ``extra`` over the parent process env. Returns None if extra is empty."""
    if not extra:
        return None
    import os

    merged = dict(os.environ)
    for k, v in extra.items():
        merged[str(k)] = str(v)
    return merged


async def _run_http_step(step: QAStep, bundle: VarBundle) -> StepResult:
    """Run an HTTP step using stdlib urllib (avoids adding a dependency).

    For complex auth flows the user is expected to use an MCP step or
    write a shell step that calls curl.
    """
    import urllib.error
    import urllib.request

    started = time.time()
    rendered_url = bundle.substitute(step.url)
    rendered_headers = bundle.substitute(step.headers) if step.headers else {}
    rendered_headers = bundle.vault.render(rendered_headers)
    rendered_body = bundle.substitute(step.body) if step.body else ""
    rendered_body = bundle.vault.render(rendered_body)

    body_bytes = rendered_body.encode("utf-8") if rendered_body else None
    req = urllib.request.Request(
        rendered_url,
        method=step.method,
        data=body_bytes,
        headers={str(k): str(v) for k, v in rendered_headers.items()},
    )
    try:
        # HTTP step doesn't make sense to time-bound under 1s in practice.
        loop = asyncio.get_running_loop()
        resp = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=step.timeout_s)),
            timeout=step.timeout_s + 5,
        )
        try:
            raw = resp.read()
        finally:
            resp.close()
        body_text = raw.decode("utf-8", errors="replace")
        status = resp.getcode()
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        status = exc.code
    except (TimeoutError, asyncio.TimeoutError):  # noqa: UP041
        duration = time.time() - started
        return StepResult(
            step_id=step.id,
            kind=step.kind.value,
            started_at=started,
            duration_s=duration,
            passed=False,
            reason=f"http timed out after {step.timeout_s}s",
            error="timeout",
        )
    except (urllib.error.URLError, OSError) as exc:
        duration = time.time() - started
        return StepResult(
            step_id=step.id,
            kind=step.kind.value,
            started_at=started,
            duration_s=duration,
            passed=False,
            reason=f"http error: {exc}",
            error=str(exc),
        )

    duration = time.time() - started
    json_data: Any = None
    if step.verdict.json_path:
        try:
            json_data = json.loads(body_text)
        except json.JSONDecodeError:
            json_data = None
    verdict = step.verdict if not step.verdict.is_empty() else StepVerdict(status=200)
    passed, reason = verdict.evaluate(status=status, body=body_text, json_data=json_data)
    return StepResult(
        step_id=step.id,
        kind=step.kind.value,
        started_at=started,
        duration_s=duration,
        passed=passed,
        reason=reason,
        output=body_text[:8000],
        status_code=status,
    )


# ---------------------------------------------------------------------------
# Top-level executor
# ---------------------------------------------------------------------------


async def execute_plan(
    plan: QAPlan,
    bundle: VarBundle,
    *,
    run_agent_step: AgentStepFn | None = None,
    run_mcp_step: MCPStepFn | None = None,
    on_progress: Callable[[QAStep, StepResult], None] | None = None,
) -> ExecutionResult:
    """Execute a QA plan and return a complete :class:`ExecutionResult`.

    Args:
        plan: Loaded plan.
        bundle: Vars bundle. Must already be resolved (call
            ``await bundle.resolve(...)`` first).
        run_agent_step: Async callback for ``kind=agent`` steps. If None,
            agent steps are skipped with an "inconclusive" reason.
        run_mcp_step: Async callback for ``kind=mcp`` steps. If None,
            MCP steps are skipped.
        on_progress: Optional sync callback invoked after each step with
            ``(step, step_result)``. Useful for UI updates.

    Returns:
        The completed :class:`ExecutionResult`.
    """
    started = time.time()
    run_id = f"run-{int(started)}"
    results: list[StepResult] = []
    aborted = False

    for step in plan.steps:
        if step.kind is StepKind.SHELL:
            sr = await _run_shell_step(step, bundle)
        elif step.kind is StepKind.HTTP:
            sr = await _run_http_step(step, bundle)
        elif step.kind is StepKind.AGENT:
            if run_agent_step is None:
                sr = _skip(step, "no agent runner provided")
            else:
                rendered_prompt = bundle.substitute(step.prompt)
                try:
                    sr = await run_agent_step(step, rendered_prompt)
                except Exception as exc:
                    logger.exception("agent step %s failed", step.id)
                    sr = _err(step, exc)
        elif step.kind is StepKind.MCP:
            if run_mcp_step is None:
                sr = _skip(step, "no MCP runner provided")
            else:
                rendered_args = bundle.substitute(step.args)
                rendered_args = bundle.vault.render(rendered_args)
                try:
                    sr = await run_mcp_step(step, rendered_args)
                except Exception as exc:
                    logger.exception("mcp step %s failed", step.id)
                    sr = _err(step, exc)
        else:
            sr = _skip(step, f"unknown kind: {step.kind}")

        results.append(sr)
        if on_progress is not None:
            try:
                on_progress(step, sr)
            except Exception:
                logger.warning("on_progress callback raised", exc_info=True)
        if not sr.passed and step.on_fail == "abort":
            aborted = True
            break

    duration = time.time() - started
    ac_outcomes = _aggregate_ac_outcomes(plan, results)
    overall = _overall_verdict(ac_outcomes, aborted=aborted)
    return ExecutionResult(
        plan_id=plan.plan_id,
        run_id=run_id,
        started_at=started,
        duration_s=duration,
        overall_verdict=overall,
        step_results=results,
        ac_outcomes=ac_outcomes,
        aborted=aborted,
    )


def _skip(step: QAStep, reason: str) -> StepResult:
    return StepResult(
        step_id=step.id,
        kind=step.kind.value,
        started_at=time.time(),
        duration_s=0.0,
        passed=False,
        reason=f"skipped — {reason}",
        error="skipped",
    )


def _err(step: QAStep, exc: BaseException) -> StepResult:
    return StepResult(
        step_id=step.id,
        kind=step.kind.value,
        started_at=time.time(),
        duration_s=0.0,
        passed=False,
        reason=f"runner exception: {exc.__class__.__name__}: {exc}",
        error=str(exc),
    )


def _aggregate_ac_outcomes(plan: QAPlan, results: list[StepResult]) -> list[ACOutcome]:
    """Roll up step results into per-AC verdicts.

    An AC passes when at least one step targeting it passes AND no step
    targeting it fails. (i.e. all referenced steps must pass.)
    """
    by_id = {sr.step_id: sr for sr in results}
    outcomes: list[ACOutcome] = []
    # Build a step→ACs index from the plan.
    step_to_acs = {step.id: list(step.ac) for step in plan.steps}
    for ac in plan.acceptance_criteria:
        related_step_ids = [sid for sid, acs in step_to_acs.items() if ac.id in acs]
        if not related_step_ids:
            outcomes.append(
                ACOutcome(
                    ac_id=ac.id,
                    text=ac.text,
                    verdict="inconclusive",
                    step_ids=[],
                    failed_step_ids=[],
                )
            )
            continue
        executed = [by_id[sid] for sid in related_step_ids if sid in by_id]
        if not executed:
            outcomes.append(
                ACOutcome(
                    ac_id=ac.id,
                    text=ac.text,
                    verdict="inconclusive",
                    step_ids=related_step_ids,
                    failed_step_ids=[],
                )
            )
            continue
        failed = [s.step_id for s in executed if not s.passed]
        verdict = "fail" if failed else "pass"
        outcomes.append(
            ACOutcome(
                ac_id=ac.id,
                text=ac.text,
                verdict=verdict,
                step_ids=[s.step_id for s in executed],
                failed_step_ids=failed,
            )
        )
    return outcomes


def _overall_verdict(outcomes: list[ACOutcome], *, aborted: bool) -> str:
    if aborted:
        return "fail"
    if not outcomes:
        return "inconclusive"
    if any(o.verdict == "fail" for o in outcomes):
        return "fail"
    if any(o.verdict == "inconclusive" for o in outcomes):
        return "inconclusive"
    return "pass"


# Make these importable for static analysers etc.
__all__ = [
    "ACOutcome",
    "AgentStepFn",
    "ExecutionResult",
    "MCPStepFn",
    "StepResult",
    "execute_plan",
]


# Defensive: silence unused-import warnings from the type-only imports above.
_ = (shlex, subprocess, sys, SecretStr)
