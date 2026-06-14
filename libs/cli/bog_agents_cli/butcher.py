"""Butcher mode — a strong model slices work into foolproof vertical cuts.

The user hands the butcher a big prompt. The butcher (best available
model, configurable) studies the repo and carves the work into very
small **vertical slices** — each one so explicit, so self-contained,
that a weak worker model (a 7B local model is the design target) cannot
mess it up. Every slice becomes its own instruction file on disk; the
manifest ties them together. Workers then execute the slices
**sequentially, in-place**, and the butcher verifies each result before
the next slice starts. A failing slice is retried once with the
butcher's correction notes, then escalated up a model ladder.

The job directory is the contract (and deliberately tool-agnostic —
the slice files are plain markdown anyone can hand to any model):

    .bog-agents/butcher/<job-id>/
    ├── manifest.json     # machine-readable plan + live status
    ├── slice-01.md       # one self-contained instruction file per slice
    ├── slice-02.md
    └── report.md         # written when the job finishes

Pure-logic module: models are injected as async ``invoke`` callables
and the worker loop takes its tool list as an argument, so unit tests
drive everything without a live LLM. CLI wiring lives in
:func:`handle_butcher_subcommand` / :func:`start_butcher_job`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bog_agents_cli.feature_helpers import (
    collect_git_context,
    extract_json_object,
    feature_state_dir,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain_core.language_models import BaseChatModel
    from langchain_core.tools import BaseTool

logger = logging.getLogger(__name__)


_CONFIG_NAME = "butcher.toml"
_JOBS_SUBDIR = Path(".bog-agents") / "butcher"
_PLAN_TIMEOUT_SECONDS = 300.0
_VERIFY_TIMEOUT_SECONDS = 120.0
_WORKER_CALL_TIMEOUT_SECONDS = 240.0
_CHECK_TIMEOUT_SECONDS = 300.0
_MAX_SLICES = 16


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class ButcherConfig:
    """Tuning knobs persisted to ``~/.bog-agents/butcher.toml``."""

    butcher_model: str = ""
    """Model spec for planning + verification. Empty = operator ``max`` tier
    when operator mode is configured, else the session's active model."""

    worker_model: str = ""
    """Model spec for slice execution. Empty = operator ``easy`` tier when
    available, else the butcher model (workers can never be model-less)."""

    escalation_models: list[str] = field(default_factory=list)
    """Ladder tried (in order) after the worker fails a slice twice.
    Empty = derived: operator ``medium`` then ``hard`` tiers when available,
    else the butcher model."""

    max_slices: int = _MAX_SLICES
    worker_max_iterations: int = 16
    """Hard cap on one worker's model→tool cycles per attempt."""


def butcher_config_path() -> Path:
    """Return ``~/.bog-agents/butcher.toml``."""
    return feature_state_dir() / _CONFIG_NAME


def load_butcher_config(path: Path | None = None) -> ButcherConfig:
    """Load butcher config, falling back to defaults on missing/malformed."""
    target = path or butcher_config_path()
    cfg = ButcherConfig()
    if not target.exists():
        return cfg
    try:
        data = tomllib.loads(target.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError):
        logger.warning("Failed to parse butcher.toml; using defaults", exc_info=True)
        return cfg
    if isinstance(data.get("butcher_model"), str):
        cfg.butcher_model = data["butcher_model"].strip()
    if isinstance(data.get("worker_model"), str):
        cfg.worker_model = data["worker_model"].strip()
    if isinstance(data.get("escalation_models"), list):
        cfg.escalation_models = [
            str(m).strip()
            for m in data["escalation_models"]
            if isinstance(m, str) and m.strip()
        ]
    if isinstance(data.get("max_slices"), int):
        cfg.max_slices = max(1, min(_MAX_SLICES, data["max_slices"]))
    if isinstance(data.get("worker_max_iterations"), int):
        cfg.worker_max_iterations = max(2, min(64, data["worker_max_iterations"]))
    return cfg


# ---------------------------------------------------------------------------
# Plan model — slices and jobs
# ---------------------------------------------------------------------------


@dataclass
class Slice:
    """One vertical cut of the job."""

    number: int
    title: str
    instructions: str
    """Step-by-step, fully explicit. The worker sees ONLY this file."""

    files: list[str] = field(default_factory=list)
    """Paths the worker is allowed to touch. Everything else is off-limits."""

    acceptance_check: str = ""
    """Shell command that must exit 0 (empty = model-verification only)."""

    context: str = ""
    """Inlined background the worker needs (excerpts, signatures, conventions)."""

    status: str = "pending"
    """pending → running → done / failed."""

    attempts: int = 0
    executed_by: str = ""
    notes: str = ""


@dataclass
class ButcherJob:
    """A planned job: the manifest in object form."""

    job_id: str
    prompt: str
    title: str
    slices: list[Slice]
    butcher_model: str
    worker_model: str
    created_at: float = field(default_factory=time.time)

    @property
    def job_dir_name(self) -> str:
        """Directory name under ``.bog-agents/butcher/`` for this job."""
        return self.job_id


@dataclass
class SliceResult:
    """Outcome of executing + verifying one slice."""

    slice_number: int
    ok: bool
    executed_by: str
    attempts: int
    duration_seconds: float
    worker_summary: str = ""
    verify_notes: str = ""
    error: str = ""


@dataclass
class ButcherReport:
    """Outcome of a whole job."""

    job: ButcherJob
    results: list[SliceResult]
    elapsed_seconds: float

    @property
    def ok(self) -> bool:
        """True when every slice finished and verified."""
        return bool(self.results) and all(r.ok for r in self.results)


# ---------------------------------------------------------------------------
# Planning (the butcher's knife)
# ---------------------------------------------------------------------------


BUTCHER_PLAN_SYSTEM_PROMPT = """You are the BUTCHER — a principal engineer who
carves a chunk of work into small vertical slices so simple that a weak
worker model cannot mess them up. The workers are NOT smart: they follow
instructions literally, they cannot infer context you did not write down,
and they panic when given choices. Your job is to remove every choice.

Rules for slices:

1. VERTICAL — each slice delivers one complete, verifiable change
   (not "do the models layer" then "do the views layer").
2. TINY — a slice touches 1-3 files and fits in a junior dev's head.
3. SELF-CONTAINED — inline every excerpt, signature, naming convention
   and convention the worker needs. The worker sees ONLY its slice file.
4. EXPLICIT — exact file paths, exact function names, exact expected
   behavior. Say what NOT to touch.
5. CHECKABLE — give each slice an acceptance check: a command that exits 0
   on success. Write checks as `python -c "..."` one-liners (or the
   project's own test runner, e.g. `pytest path -q`) — NEVER as shell
   utilities like grep/find/type/cat, which differ or get shadowed across
   platforms. Example:
   `python -c "import sys; sys.exit(0 if open('hello.txt', encoding='utf-8').read().strip() == 'hello world' else 1)"`
   Use relative paths; the check runs from the project root. When no
   command makes sense, leave it empty and describe success criteria
   precisely in the instructions.
6. ORDERED — slices run sequentially; later slices may rely on earlier
   ones being done.

Reply with STRICT JSON only — no prose, no markdown fence:

{"title": "<short job title>",
 "slices": [
   {"title": "<slice title>",
    "instructions": "<complete step-by-step instructions, markdown allowed>",
    "files": ["<path>", ...],
    "acceptance_check": "<shell command or empty string>",
    "context": "<inlined background the worker needs>"},
   ...]}
"""


def parse_plan_response(
    text: str, *, max_slices: int = _MAX_SLICES
) -> tuple[str, list[Slice]] | None:
    """Parse the butcher's planning reply into ``(title, slices)``.

    Tolerates surrounding prose/fences. Returns None when no usable plan
    can be recovered.
    """
    candidate = extract_json_object(text)
    if candidate is None:
        return None
    raw_slices = candidate.get("slices")
    if not isinstance(raw_slices, list) or not raw_slices:
        return None
    title = str(candidate.get("title", "")).strip() or "untitled job"
    slices: list[Slice] = []
    for i, raw in enumerate(raw_slices[:max_slices], start=1):
        if not isinstance(raw, dict):
            continue
        instructions = str(raw.get("instructions", "")).strip()
        if not instructions:
            continue
        files_raw = raw.get("files", [])
        files = (
            [str(f).strip() for f in files_raw if isinstance(f, str) and str(f).strip()]
            if isinstance(files_raw, list)
            else []
        )
        slices.append(
            Slice(
                number=i,
                title=str(raw.get("title", f"slice {i}")).strip() or f"slice {i}",
                instructions=instructions,
                files=files,
                acceptance_check=str(raw.get("acceptance_check", "")).strip(),
                context=str(raw.get("context", "")).strip(),
            )
        )
    if not slices:
        return None
    # Renumber after any skips so slice files stay contiguous.
    for i, s in enumerate(slices, start=1):
        s.number = i
    return title, slices


async def plan_job(
    prompt: str,
    *,
    invoke: Callable[[str, str], Awaitable[str]],
    working_dir: Path,
    butcher_model: str,
    worker_model: str,
    max_slices: int = _MAX_SLICES,
) -> ButcherJob | None:
    """Ask the butcher model for a slice plan. Returns None when planning fails."""
    import platform

    git = collect_git_context(working_dir)
    context_lines = [
        f"Working directory: {working_dir}",
        f"Platform: {platform.system()} (acceptance checks run through this system's shell)",
    ]
    if git.branch:
        context_lines.append(f"Git branch: {git.branch} (dirty: {git.is_dirty})")
    if git.recent_commits:
        context_lines.append(
            "Recent commits:\n  " + "\n  ".join(git.recent_commits[:8])
        )
    user_block = (
        f"{chr(10).join(context_lines)}\n\n"
        f"Cut this job into at most {max_slices} slices:\n\n{prompt.strip()}"
    )
    try:
        reply = await invoke(BUTCHER_PLAN_SYSTEM_PROMPT, user_block)
    except Exception:
        logger.warning("Butcher planning call failed", exc_info=True)
        return None
    parsed = parse_plan_response(reply, max_slices=max_slices)
    if parsed is None:
        logger.warning("Butcher plan unparseable: %r", reply[:300])
        return None
    title, slices = parsed
    job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + _slugify(title)
    return ButcherJob(
        job_id=job_id,
        prompt=prompt.strip(),
        title=title,
        slices=slices,
        butcher_model=butcher_model,
        worker_model=worker_model,
    )


def _slugify(text: str, *, max_len: int = 32) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "job"


# ---------------------------------------------------------------------------
# Job directory (the contract on disk)
# ---------------------------------------------------------------------------


def jobs_root(working_dir: Path) -> Path:
    """Return ``<working_dir>/.bog-agents/butcher``."""
    return working_dir / _JOBS_SUBDIR


def render_slice_file(job: ButcherJob, sl: Slice) -> str:
    """Render one slice as a self-contained markdown instruction file."""
    files_block = (
        "\n".join(f"- `{f}`" for f in sl.files)
        if sl.files
        else "- (the slice instructions name them)"
    )
    check_block = (
        f"```\n{sl.acceptance_check}\n```"
        if sl.acceptance_check
        else "(no command — success criteria are in the instructions)"
    )
    context_block = sl.context or "(none needed)"
    return (
        f"# Slice {sl.number:02d} — {sl.title}\n\n"
        f"Job: {job.title} (`{job.job_id}`)\n\n"
        "## Iron rules\n\n"
        "1. Do ONLY what this file says. Touch ONLY the allowed files.\n"
        '2. Do not refactor, rename, reformat, or "improve" anything you were not asked to change.\n'
        "3. If something needed is missing from these instructions, STOP and report it — do not guess.\n"
        "4. When you are done, state plainly what you changed, file by file.\n\n"
        f"## Allowed files\n\n{files_block}\n\n"
        f"## Context\n\n{context_block}\n\n"
        f"## Instructions\n\n{sl.instructions}\n\n"
        f"## Acceptance check\n\n{check_block}\n"
    )


def write_job_dir(job: ButcherJob, working_dir: Path) -> Path:
    """Write the manifest + slice files. Returns the job directory."""
    job_dir = jobs_root(working_dir) / job.job_dir_name
    job_dir.mkdir(parents=True, exist_ok=True)
    for sl in job.slices:
        (job_dir / f"slice-{sl.number:02d}.md").write_text(
            render_slice_file(job, sl), encoding="utf-8"
        )
    write_manifest(job, job_dir)
    return job_dir


def write_manifest(job: ButcherJob, job_dir: Path) -> None:
    """Write/refresh ``manifest.json`` (live status included)."""
    payload = {
        "job_id": job.job_id,
        "title": job.title,
        "prompt": job.prompt,
        "butcher_model": job.butcher_model,
        "worker_model": job.worker_model,
        "created_at": job.created_at,
        "slices": [
            {
                "number": s.number,
                "title": s.title,
                "file": f"slice-{s.number:02d}.md",
                "files": s.files,
                "acceptance_check": s.acceptance_check,
                "status": s.status,
                "attempts": s.attempts,
                "executed_by": s.executed_by,
                "notes": s.notes,
            }
            for s in job.slices
        ],
    }
    target = job_dir / "manifest.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(target)


# ---------------------------------------------------------------------------
# Worker tools (write-capable, scoped to the working dir)
# ---------------------------------------------------------------------------


def screen_dangerous_command(command: str) -> str | None:
    """Return a refusal reason if a butcher shell command is obviously destructive.

    Butcher runs LLM/worker-authored shell via shell=True. This reuses the SDK
    shell backend's accident-catcher patterns (rm -rf, mkfs, dd to a device,
    fork bombs, curl|sh, etc.) so the most dangerous commands are refused
    before execution rather than run blindly. It is an accident-catcher, not a
    security boundary — the real safeguard is the job-level approval gate.
    (REVIEW.md v2 P1-42.)
    """
    if not command or not isinstance(command, str):
        return None
    try:
        # Reuse the SDK shell backend's accident-catcher patterns (same project).
        from bog_agents.backends.local_shell import _DANGEROUS_PATTERNS  # noqa: PLC2701
    except Exception:  # never let a screen import break execution
        return None
    for pattern, description in _DANGEROUS_PATTERNS:
        if pattern.search(command):
            return description
    return None


def build_worker_tools(working_dir: Path) -> list[BaseTool]:
    """Read tools (sidecar's trio) + scoped write/edit/run tools for workers.

    Same path-escape rules as the sidecar: nothing above ``working_dir``,
    no symlinks. ``run_command`` exists so a worker can run its slice's
    acceptance check or a quick compile — it is not a general shell.
    """
    import subprocess  # noqa: S404 — scoped check-runner for slice verification

    from langchain_core.tools import StructuredTool

    from bog_agents_cli.sidecar import build_readonly_tools

    root = working_dir.resolve()

    def _resolve_safe(rel: str) -> Path:
        candidate = (root / rel).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            msg = f"path {rel!r} resolves outside the working directory"
            raise PermissionError(msg) from exc
        if candidate.is_symlink():
            msg = f"refusing to follow symlink {rel!r}"
            raise PermissionError(msg)
        return candidate

    def write_file(path: str, content: str) -> str:
        """Create or overwrite a UTF-8 text file under the working directory."""
        try:
            resolved = _resolve_safe(path)
        except PermissionError as exc:
            return f"Error: {exc}"
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(content, encoding="utf-8")
        except OSError as exc:
            return f"Error writing {path!r}: {exc}"
        return f"Wrote {len(content)} chars to {path}"

    def edit_file(path: str, old_string: str, new_string: str) -> str:
        """Replace an exact, unique occurrence of ``old_string`` in the file."""
        try:
            resolved = _resolve_safe(path)
        except PermissionError as exc:
            return f"Error: {exc}"
        if not resolved.is_file():
            return f"Error: {path!r} is not a regular file"
        try:
            text = resolved.read_text(encoding="utf-8")
        except OSError as exc:
            return f"Error reading {path!r}: {exc}"
        count = text.count(old_string)
        if count == 0:
            return f"Error: old_string not found in {path!r}"
        if count > 1:
            return f"Error: old_string occurs {count} times in {path!r} — provide a longer, unique excerpt"
        try:
            resolved.write_text(
                text.replace(old_string, new_string, 1), encoding="utf-8"
            )
        except OSError as exc:
            return f"Error writing {path!r}: {exc}"
        return f"Edited {path}"

    def run_command(command: str, *, timeout_seconds: int = 120) -> str:
        """Run a shell command in the working directory; returns exit code + output."""
        danger = screen_dangerous_command(command)
        if danger is not None:
            return f"Error: refused dangerous command ({danger}). Butcher workers cannot run this."
        timeout_seconds = max(1, min(int(timeout_seconds), int(_CHECK_TIMEOUT_SECONDS)))
        try:
            result = subprocess.run(  # noqa: S602 — slice-scoped check runner, local CLI context
                command,
                shell=True,
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return f"Error: command timed out after {timeout_seconds}s"
        except OSError as exc:
            return f"Error running command: {exc}"
        out = (result.stdout or "") + (("\n" + result.stderr) if result.stderr else "")
        if len(out) > 8_000:
            out = out[:8_000] + "\n…[truncated]…"
        return f"exit code: {result.returncode}\n{out.strip()}"

    tools = build_readonly_tools(working_dir=root, web_search=False)
    tools.extend(
        [
            StructuredTool.from_function(write_file, name="write_file"),
            StructuredTool.from_function(edit_file, name="edit_file"),
            StructuredTool.from_function(run_command, name="run_command"),
        ]
    )
    return tools


# ---------------------------------------------------------------------------
# Worker execution loop
# ---------------------------------------------------------------------------


WORKER_SYSTEM_PROMPT = """You are a WORKER executing exactly one slice of a
larger job. The slice file you receive is your entire world.

* Follow the instructions LITERALLY and COMPLETELY.
* Touch only the allowed files.
* Use the tools (read_file, glob, grep, write_file, edit_file, run_command)
  to make the change and to run the acceptance check if one is given.
* Never invent work that is not in the instructions.
* If the instructions are impossible or contradict the code you read,
  STOP and explain the problem instead of improvising.
* Finish with a short plain-text summary: what you changed, file by file,
  and the result of the acceptance check if you ran it.
"""


@dataclass
class WorkerOutcome:
    """What one worker attempt produced."""

    ok: bool
    summary: str = ""
    tool_calls_made: list[str] = field(default_factory=list)
    error: str = ""


async def run_worker(
    slice_text: str,
    *,
    model: BaseChatModel,
    tools: list[BaseTool],
    max_iterations: int = 16,
    correction_notes: str = "",
) -> WorkerOutcome:
    """Run one worker attempt: an async model→tool loop over the slice file.

    Mirrors the sidecar loop (deliberately independent of the SDK
    middleware stack) but write-capable and async — model calls go
    through ``ainvoke`` and tool calls through a thread so a long
    acceptance check cannot stall the TUI event loop.
    """
    from langchain_core.messages import (
        AIMessage,
        HumanMessage,
        SystemMessage,
        ToolMessage,
    )

    user_block = slice_text
    if correction_notes.strip():
        user_block += (
            "\n\n## Correction notes from the previous failed attempt\n\n"
            f"{correction_notes.strip()}\n\nFix these problems. Everything else above still applies."
        )
    messages: list[Any] = [
        SystemMessage(content=WORKER_SYSTEM_PROMPT),
        HumanMessage(content=user_block),
    ]
    try:
        bound = model.bind_tools(tools) if tools else model
    except (NotImplementedError, AttributeError):
        bound = model
    tools_by_name = {t.name: t for t in tools}
    outcome = WorkerOutcome(ok=False)

    for _ in range(max_iterations):
        try:
            response = await asyncio.wait_for(
                bound.ainvoke(messages), timeout=_WORKER_CALL_TIMEOUT_SECONDS
            )
        except Exception as exc:
            outcome.error = f"worker model call failed: {exc}"
            return outcome
        if not isinstance(response, AIMessage):
            outcome.ok = True
            outcome.summary = str(getattr(response, "content", response))
            return outcome
        messages.append(response)
        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            # Tool-call rescue: weak local models routinely emit the tool
            # invocation as JSON *text* instead of a structured tool call.
            # Recognise that shape, execute it, and keep the loop alive —
            # this is the difference between local workers working and not.
            rescued = rescue_text_tool_call(
                _coerce_text(response.content), tools_by_name
            )
            if rescued is not None:
                name, args = rescued
                outcome.tool_calls_made.append(name)
                try:
                    tool_text = str(
                        await asyncio.to_thread(tools_by_name[name].invoke, args)
                    )
                except Exception as exc:
                    tool_text = f"Error invoking {name}: {exc}"
                messages.append(
                    HumanMessage(
                        content=(
                            f"[tool result for {name}]\n{tool_text}\n\n"
                            "Continue with the slice. To call another tool, use a real tool call "
                            "(or the same JSON shape). When fully done, reply with a plain-text "
                            "summary only — no JSON."
                        )
                    )
                )
                continue
            outcome.ok = True
            outcome.summary = _coerce_text(response.content)
            return outcome
        for call in tool_calls:
            name = call.get("name", "")
            args = call.get("args", {}) or {}
            tool = tools_by_name.get(name)
            outcome.tool_calls_made.append(name)
            if tool is None:
                tool_text = f"Error: tool {name!r} is not available to workers."
            else:
                try:
                    tool_text = str(await asyncio.to_thread(tool.invoke, args))
                except Exception as exc:
                    tool_text = f"Error invoking {name}: {exc}"
            messages.append(
                ToolMessage(
                    content=tool_text, tool_call_id=str(call.get("id", "")), name=name
                )
            )

    outcome.error = f"worker hit max_iterations={max_iterations} without finishing"
    return outcome


def rescue_text_tool_call(
    content: str, tools_by_name: dict[str, BaseTool]
) -> tuple[str, dict[str, Any]] | None:
    """Recover a tool invocation a model wrote as JSON text instead of a tool call.

    Accepts the common shapes weak models produce — ``{"name": …,
    "arguments": {…}}`` (also ``args`` / ``parameters`` / ``tool_input``,
    or ``{"tool": …}``), optionally inside a markdown fence or prose.
    Returns ``(tool_name, args)`` only when the named tool actually
    exists; anything else returns None so a genuine final answer is
    never mistaken for a tool call.
    """
    if not content or "{" not in content:
        return None
    candidate = extract_json_object(content)
    if candidate is None:
        return None
    name = candidate.get("name") or candidate.get("tool")
    if not isinstance(name, str) or name not in tools_by_name:
        return None
    args: Any = None
    for key in ("arguments", "args", "parameters", "tool_input", "input"):
        if isinstance(candidate.get(key), dict):
            args = candidate[key]
            break
    if args is None:
        # Flat shape: {"name": "write_file", "path": …, "content": …}
        args = {k: v for k, v in candidate.items() if k not in {"name", "tool"}}
    if not isinstance(args, dict) or not args:
        return None
    return name, args


def _coerce_text(content: Any) -> str:  # noqa: ANN401 — LangChain content is str | list[blocks]
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            b["text"]
            for b in content
            if isinstance(b, dict)
            and b.get("type") == "text"
            and isinstance(b.get("text"), str)
        ]
        return "".join(parts)
    return str(content)


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


VERIFY_SYSTEM_PROMPT = """You are the BUTCHER verifying a worker's slice.
You wrote the slice; now judge whether the worker actually delivered it.

You receive: the slice file, the worker's summary, and the acceptance-check
result (when a check command exists). Judge ONLY against the slice's own
requirements — not against taste.

Reply with STRICT JSON only:

{"pass": true|false, "notes": "<when failing: precise, actionable corrections for the worker. When passing: empty string.>"}
"""


def parse_verify_response(text: str) -> tuple[bool, str] | None:
    """Parse the verifier's reply. None = unparseable (treated as inconclusive)."""
    candidate = extract_json_object(text)
    if candidate is None or not isinstance(candidate.get("pass"), bool):
        return None
    return bool(candidate["pass"]), str(candidate.get("notes", "")).strip()


async def run_acceptance_check(command: str, working_dir: Path) -> tuple[bool, str]:
    """Run the slice's acceptance command. Returns ``(exit_ok, output)``."""
    import subprocess  # noqa: S404 — acceptance checks come from the butcher's own plan

    danger = screen_dangerous_command(command)
    if danger is not None:
        return (False, f"acceptance check refused — dangerous command ({danger})")

    def _run() -> tuple[bool, str]:
        try:
            result = subprocess.run(  # noqa: S602 — butcher-authored check command, local CLI context
                command,
                shell=True,
                cwd=str(working_dir),
                capture_output=True,
                text=True,
                timeout=_CHECK_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return (
                False,
                f"acceptance check timed out after {_CHECK_TIMEOUT_SECONDS:.0f}s",
            )
        except OSError as exc:
            return False, f"acceptance check failed to start: {exc}"
        out = (result.stdout or "") + (("\n" + result.stderr) if result.stderr else "")
        return (
            result.returncode == 0,
            f"exit code: {result.returncode}\n{out.strip()[:4_000]}",
        )

    return await asyncio.to_thread(_run)


async def verify_slice(
    sl: Slice,
    worker_summary: str,
    *,
    invoke: Callable[[str, str], Awaitable[str]],
    working_dir: Path,
) -> tuple[bool, str]:
    """Verify one slice: acceptance command (when present) + model judgement.

    A failing acceptance command fails the slice outright. The model
    verdict then confirms the work matches the instructions. An
    unparseable model verdict counts as a pass **only** when the
    acceptance command already passed (the command is the harder gate);
    with no command it counts as a fail — never trust silently.
    """
    check_result = ""
    if sl.acceptance_check:
        check_ok, check_result = await run_acceptance_check(
            sl.acceptance_check, working_dir
        )
        if not check_ok:
            return False, f"acceptance check failed:\n{check_result}"
    user_block = (
        f"## Slice file\n\n{render_slice_file_stub(sl)}\n\n"
        f"## Worker summary\n\n{worker_summary.strip() or '(worker gave no summary)'}\n\n"
        f"## Acceptance check result\n\n{check_result or '(no check command for this slice)'}"
    )
    try:
        reply = await invoke(VERIFY_SYSTEM_PROMPT, user_block)
    except Exception:
        logger.warning("Butcher verification call failed", exc_info=True)
        return (
            bool(sl.acceptance_check),
            "verifier unavailable — accepted on green acceptance check"
            if sl.acceptance_check
            else "verifier unavailable and no acceptance check — failing safe",
        )
    parsed = parse_verify_response(reply)
    if parsed is None:
        if sl.acceptance_check:
            return (
                True,
                "verifier verdict unparseable — accepted on green acceptance check",
            )
        return (
            False,
            "verifier verdict unparseable and no acceptance check — failing safe",
        )
    return parsed


def render_slice_file_stub(sl: Slice) -> str:
    """The slice content shown to the verifier (no job header needed)."""
    return (
        f"### {sl.title}\n\nAllowed files: {', '.join(sl.files) or '(per instructions)'}\n\n"
        f"Instructions:\n{sl.instructions}\n\nAcceptance check: {sl.acceptance_check or '(none)'}"
    )


# ---------------------------------------------------------------------------
# The job runner
# ---------------------------------------------------------------------------


async def run_butcher_job(
    job: ButcherJob,
    *,
    working_dir: Path,
    verify_invoke: Callable[[str, str], Awaitable[str]],
    worker_model_factory: Callable[[str], BaseChatModel],
    escalation_models: list[str],
    worker_max_iterations: int = 16,
    progress: Callable[[str], Awaitable[None]] | None = None,
) -> ButcherReport:
    """Execute a planned job: sequential slices, verify each, retry/escalate.

    Per slice: worker attempt → verify → on fail, one retry with the
    butcher's correction notes → on fail again, one attempt per ladder
    model. A slice that exhausts the ladder is marked failed and the job
    continues (later slices may still be independent); the report tells
    the truth either way.

    Args:
        job: A planned job (see :func:`plan_job`).
        working_dir: Repo root; slices execute in-place here.
        verify_invoke: ``async (system, user) -> str`` on the butcher model.
        worker_model_factory: ``(model_spec) -> BaseChatModel`` — builds
            worker models lazily so ladder models only load when needed.
        escalation_models: Ladder specs tried after the worker fails twice.
        worker_max_iterations: Per-attempt cap on worker model→tool cycles.
        progress: Optional async callback for per-slice status lines.
    """
    job_dir = write_job_dir(job, working_dir)
    started = time.monotonic()
    results: list[SliceResult] = []

    async def _say(text: str) -> None:
        if progress is not None:
            try:
                await progress(text)
            except Exception:
                logger.debug("butcher progress callback failed", exc_info=True)

    for sl in job.slices:
        sl.status = "running"
        write_manifest(job, job_dir)
        slice_started = time.monotonic()
        slice_text = render_slice_file(job, sl)
        ladder = [job.worker_model, job.worker_model, *escalation_models]
        correction = ""
        ok = False
        notes = ""
        summary = ""
        executed_by = ""
        for attempt, model_spec in enumerate(ladder, start=1):
            sl.attempts = attempt
            escalated = " [escalated]" if attempt > 2 else ""
            await _say(
                f"slice {sl.number:02d}/{len(job.slices)} — attempt {attempt} on {model_spec}{escalated}: {sl.title}"
            )
            try:
                model = worker_model_factory(model_spec)
            except Exception as exc:
                notes = f"could not build worker model {model_spec!r}: {exc}"
                logger.warning(
                    "Butcher worker model %r unavailable", model_spec, exc_info=True
                )
                continue
            tools = build_worker_tools(working_dir)
            outcome = await run_worker(
                slice_text,
                model=model,
                tools=tools,
                max_iterations=worker_max_iterations,
                correction_notes=correction,
            )
            executed_by = model_spec
            summary = outcome.summary or outcome.error
            if not outcome.ok:
                correction = (
                    outcome.error
                    or "the previous attempt never finished — be more direct and finish"
                )
                notes = outcome.error
                continue
            ok, notes = await verify_slice(
                sl, outcome.summary, invoke=verify_invoke, working_dir=working_dir
            )
            if ok:
                break
            correction = notes
        sl.status = "done" if ok else "failed"
        sl.executed_by = executed_by
        sl.notes = notes
        write_manifest(job, job_dir)
        results.append(
            SliceResult(
                slice_number=sl.number,
                ok=ok,
                executed_by=executed_by,
                attempts=sl.attempts,
                duration_seconds=time.monotonic() - slice_started,
                worker_summary=summary,
                verify_notes=notes,
                error="" if ok else (notes or "failed"),
            )
        )
        await _say(
            f"slice {sl.number:02d} {'✓ done' if ok else '✗ FAILED'} ({results[-1].duration_seconds:.0f}s)"
        )

    report = ButcherReport(
        job=job, results=results, elapsed_seconds=time.monotonic() - started
    )
    (job_dir / "report.md").write_text(render_report(report), encoding="utf-8")
    return report


def render_report(report: ButcherReport) -> str:
    """Render the final job report (also written to ``report.md``)."""
    job = report.job
    lines = [
        f"# Butcher report — {job.title}",
        "",
        f"Job `{job.job_id}` — {'**all slices done**' if report.ok else '**FINISHED WITH FAILURES**'} "
        f"in {report.elapsed_seconds:.0f}s. Butcher: `{job.butcher_model}` — worker: `{job.worker_model}`.",
        "",
        "| # | Slice | Status | Attempts | Executed by | Time |",
        "|---|-------|--------|----------|-------------|------|",
    ]
    by_number = {s.number: s for s in job.slices}
    for r in report.results:
        sl = by_number.get(r.slice_number)
        title = sl.title if sl else "?"
        status = "done" if r.ok else "FAILED"
        lines.append(
            f"| {r.slice_number:02d} | {title} | {status} | {r.attempts} | `{r.executed_by or '-'}` | {r.duration_seconds:.0f}s |"
        )
    failures = [r for r in report.results if not r.ok]
    if failures:
        lines.append("")
        lines.append("## Failures")
        for r in failures:
            lines.append(
                f"\n### Slice {r.slice_number:02d}\n\n{r.verify_notes or r.error}"
            )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def _resolve_models(app: object, cfg: ButcherConfig) -> tuple[str, str, list[str]]:
    """Resolve (butcher_model, worker_model, ladder) from config + operator tiers."""
    from bog_agents_cli.feature_helpers import resolve_active_model_spec

    tiers: dict[str, Any] = {}
    try:
        from bog_agents_cli.operator_mode import ensure_session

        tiers = ensure_session(app).tiers
    except Exception:
        logger.debug("operator tiers unavailable to butcher", exc_info=True)
    active = resolve_active_model_spec(app)
    butcher_model = (
        cfg.butcher_model or (tiers["max"].model if "max" in tiers else "") or active
    )
    worker_model = (
        cfg.worker_model
        or (tiers["easy"].model if "easy" in tiers else "")
        or butcher_model
    )
    ladder = cfg.escalation_models or [
        spec
        for spec in (
            [
                tiers["medium"].model if "medium" in tiers else "",
                tiers["hard"].model if "hard" in tiers else "",
            ]
        )
        if spec and spec != worker_model
    ]
    if not ladder and butcher_model != worker_model:
        ladder = [butcher_model]
    return butcher_model, worker_model, ladder


async def start_butcher_job(app: object, prompt: str) -> None:
    """Plan and run a butcher job end-to-end, reporting into the chat."""
    from bog_agents_cli.config import create_model_with_fallback
    from bog_agents_cli.feature_helpers import invoke_model
    from bog_agents_cli.widgets.messages import AppMessage, ErrorMessage

    if not prompt.strip():
        await app._mount_message(
            ErrorMessage("Usage: /butcher <the job to slice and execute>")
        )  # type: ignore[attr-defined]
        return
    cfg = load_butcher_config()
    butcher_spec, worker_spec, ladder = _resolve_models(app, cfg)
    if not butcher_spec:
        await app._mount_message(
            ErrorMessage(
                "No model available — run /model first or set butcher_model in butcher.toml."
            )
        )  # type: ignore[attr-defined]
        return
    working_dir = Path(getattr(app, "_cwd", Path.cwd()))
    profile = getattr(app, "_profile_override", None)
    try:
        butcher_model = create_model_with_fallback(
            butcher_spec, profile_overrides=profile
        ).model
    except Exception as exc:
        await app._mount_message(
            ErrorMessage(f"Could not build butcher model {butcher_spec!r}: {exc}")
        )  # type: ignore[attr-defined]
        return

    async def _butcher_invoke(system: str, user: str) -> str:
        return await invoke_model(
            butcher_model, system, user, timeout_seconds=_PLAN_TIMEOUT_SECONDS
        )

    async def _verify_invoke(system: str, user: str) -> str:
        return await invoke_model(
            butcher_model, system, user, timeout_seconds=_VERIFY_TIMEOUT_SECONDS
        )

    def _worker_factory(spec: str) -> BaseChatModel:
        return create_model_with_fallback(spec, profile_overrides=profile).model

    await app._mount_message(
        AppMessage(
            f"[bold]Butcher[/bold] planning with `{butcher_spec}` — workers: `{worker_spec}` …"
        )
    )  # type: ignore[attr-defined]
    job = await plan_job(
        prompt,
        invoke=_butcher_invoke,
        working_dir=working_dir,
        butcher_model=butcher_spec,
        worker_model=worker_spec,
        max_slices=cfg.max_slices,
    )
    if job is None:
        await app._mount_message(
            ErrorMessage(
                "The butcher could not produce a usable slice plan — try a more concrete prompt."
            )
        )  # type: ignore[attr-defined]
        return
    plan_lines = "\n".join(
        f"  {s.number:02d}. {s.title}"
        + (f"  [dim](check: {s.acceptance_check})[/dim]" if s.acceptance_check else "")
        for s in job.slices
    )
    job_dir = jobs_root(working_dir) / job.job_dir_name
    await app._mount_message(  # type: ignore[attr-defined]
        AppMessage(
            f"[bold]{job.title}[/bold] — {len(job.slices)} slices → [cyan]{job_dir}[/cyan]\n{plan_lines}\n\n"
            "[dim]Workers run shell commands in this directory; obviously-destructive "
            "commands (rm -rf, curl|sh, dd, …) are screened and refused. "
            "Executing sequentially…[/dim]"
        )
    )

    async def _progress(text: str) -> None:
        await app._mount_message(AppMessage(f"[dim]butcher:[/dim] {text}"))  # type: ignore[attr-defined]

    report = await run_butcher_job(
        job,
        working_dir=working_dir,
        verify_invoke=_verify_invoke,
        worker_model_factory=_worker_factory,
        escalation_models=ladder,
        worker_max_iterations=cfg.worker_max_iterations,
        progress=_progress,
    )
    await app._mount_message(AppMessage(render_report(report)))  # type: ignore[attr-defined]


async def handle_butcher_subcommand(app: object, raw_arg: str) -> None:
    """Dispatch ``/butcher <sub>``: a prompt (run a job), list, show, config."""
    from bog_agents_cli.widgets.messages import AppMessage, ErrorMessage

    arg = raw_arg.strip()
    head, _, rest = arg.partition(" ")
    head_lower = head.lower()
    rest = rest.strip()
    working_dir = Path(getattr(app, "_cwd", Path.cwd()))

    if head_lower == "list":
        root = jobs_root(working_dir)
        jobs = (
            sorted(p.name for p in root.iterdir() if p.is_dir())
            if root.exists()
            else []
        )
        if not jobs:
            await app._mount_message(
                AppMessage("No butcher jobs yet. Run /butcher <job> to start one.")
            )  # type: ignore[attr-defined]
            return
        await app._mount_message(
            AppMessage(
                "[bold]Butcher jobs[/bold]\n" + "\n".join(f"  {j}" for j in jobs)
            )
        )  # type: ignore[attr-defined]
        return

    if head_lower == "show":
        if not rest:
            await app._mount_message(ErrorMessage("Usage: /butcher show <job-id>"))  # type: ignore[attr-defined]
            return
        report_path = jobs_root(working_dir) / rest / "report.md"
        manifest_path = jobs_root(working_dir) / rest / "manifest.json"
        if report_path.exists():
            await app._mount_message(
                AppMessage(report_path.read_text(encoding="utf-8"))
            )  # type: ignore[attr-defined]
        elif manifest_path.exists():
            await app._mount_message(
                AppMessage(
                    f"Job exists but has no report yet:\n```\n{manifest_path.read_text(encoding='utf-8')}\n```"
                )
            )  # type: ignore[attr-defined]
        else:
            await app._mount_message(
                ErrorMessage(f"No job {rest!r} under {jobs_root(working_dir)}")
            )  # type: ignore[attr-defined]
        return

    if head_lower == "config":
        cfg = load_butcher_config()
        await app._mount_message(  # type: ignore[attr-defined]
            AppMessage(
                f"[bold]Butcher configuration[/bold] [cyan]{butcher_config_path()}[/cyan]\n"
                f"  butcher_model:          {cfg.butcher_model or '(operator max tier / active model)'}\n"
                f"  worker_model:           {cfg.worker_model or '(operator easy tier / butcher model)'}\n"
                f"  escalation_models:      {', '.join(cfg.escalation_models) or '(operator medium→hard tiers)'}\n"
                f"  max_slices:             {cfg.max_slices}\n"
                f"  worker_max_iterations:  {cfg.worker_max_iterations}\n\n"
                "Create/edit the TOML to change these values."
            )
        )
        return

    # Anything else is the job prompt itself.
    await start_butcher_job(app, arg)
