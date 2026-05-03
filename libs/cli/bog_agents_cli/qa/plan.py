"""QA plan model — typed steps that test acceptance criteria.

A :class:`QAPlan` is the saved-to-YAML output of /qa new. Each plan binds:

- A list of :class:`AcceptanceCriterion` (what's being tested)
- A :class:`bog_agents_cli.vars.VarBundle` declaration (what runtime
  parameters or secrets the plan needs)
- A list of :class:`QAStep` (how to test it — hybrid: agent / shell /
  http / mcp)

Steps reference AC ids via the ``ac`` field; multiple steps can target the
same AC, and each step can declare a ``verdict`` rule that decides whether
its execution counts as a pass for that AC.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from bog_agents_cli.qa.ac import AcceptanceCriterion

logger = logging.getLogger(__name__)


class StepKind(StrEnum):
    """How a single QA step is executed."""

    AGENT = "agent"
    """LLM-interpreted instruction. Adaptive, but non-deterministic."""

    SHELL = "shell"
    """Run a shell command. Deterministic; verdict checks exit code +
    stdout against ``expect`` and ``pass_when`` patterns."""

    HTTP = "http"
    """Issue an HTTP request. Verdict checks status code and body."""

    MCP = "mcp"
    """Call a configured MCP tool by name with explicit args."""


@dataclass
class StepVerdict:
    """Pass/fail rules for a step.

    Attributes:
        exit_code: Expected exit code for ``shell`` steps. ``None`` means
            "any non-error" (exit_code 0).
        status: Expected HTTP status for ``http`` steps. May be a single
            int or list (treated as "any of").
        contains: Substring(s) that must appear in stdout/body.
        not_contains: Substring(s) that must NOT appear.
        regex: Regex(es) that must match somewhere in stdout/body.
        not_regex: Regex(es) that must NOT match.
        json_path: For ``http``/``mcp`` JSON responses — a dotted path
            (e.g. ``data.user.id``) that must exist.
    """

    exit_code: int | None = None
    status: int | list[int] | None = None
    contains: list[str] = field(default_factory=list)
    not_contains: list[str] = field(default_factory=list)
    regex: list[str] = field(default_factory=list)
    not_regex: list[str] = field(default_factory=list)
    json_path: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> StepVerdict:
        if not d:
            return cls()
        # ``contains`` etc. accept either a single string or a list.

        def _as_list(key: str) -> list[str]:
            v = d.get(key)
            if v is None:
                return []
            if isinstance(v, str):
                return [v]
            return [str(x) for x in v]

        return cls(
            exit_code=d.get("exit_code"),
            status=d.get("status"),
            contains=_as_list("contains"),
            not_contains=_as_list("not_contains"),
            regex=_as_list("regex"),
            not_regex=_as_list("not_regex"),
            json_path=str(d.get("json_path", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.exit_code is not None:
            out["exit_code"] = self.exit_code
        if self.status is not None:
            out["status"] = self.status
        if self.contains:
            out["contains"] = list(self.contains)
        if self.not_contains:
            out["not_contains"] = list(self.not_contains)
        if self.regex:
            out["regex"] = list(self.regex)
        if self.not_regex:
            out["not_regex"] = list(self.not_regex)
        if self.json_path:
            out["json_path"] = self.json_path
        return out

    def is_empty(self) -> bool:
        return (
            self.exit_code is None
            and self.status is None
            and not self.contains
            and not self.not_contains
            and not self.regex
            and not self.not_regex
            and not self.json_path
        )

    def evaluate(
        self,
        *,
        exit_code: int | None = None,
        status: int | None = None,
        body: str = "",
        json_data: Any = None,
    ) -> tuple[bool, str]:
        """Apply the verdict rules to a step's execution result.

        Args:
            exit_code: Process exit code (for shell steps).
            status: HTTP status (for http/mcp steps).
            body: stdout / response body / mcp result text.
            json_data: Already-parsed JSON, when relevant.

        Returns:
            ``(passed, reason)``. ``passed=True`` means all rules satisfied.
        """
        # Exit code.
        if self.exit_code is not None and exit_code is not None and exit_code != self.exit_code:
            return False, f"exit_code {exit_code} ≠ expected {self.exit_code}"
        # Status.
        if self.status is not None and status is not None:
            allowed = self.status if isinstance(self.status, list) else [self.status]
            if status not in allowed:
                return False, f"status {status} ∉ expected {allowed}"
        # Contains / not_contains.
        for needle in self.contains:
            if needle not in body:
                return False, f"missing required substring: {needle!r}"
        for needle in self.not_contains:
            if needle in body:
                return False, f"forbidden substring present: {needle!r}"
        # Regex.
        for pat in self.regex:
            try:
                if not re.search(pat, body):
                    return False, f"regex /{pat}/ did not match"
            except re.error as exc:
                return False, f"invalid regex /{pat}/: {exc}"
        for pat in self.not_regex:
            try:
                if re.search(pat, body):
                    return False, f"forbidden regex /{pat}/ matched"
            except re.error as exc:
                return False, f"invalid regex /{pat}/: {exc}"
        # JSON path.
        if self.json_path:
            if json_data is None:
                return False, f"json_path {self.json_path!r} requires a JSON response"
            cur: Any = json_data
            for piece in self.json_path.split("."):
                if isinstance(cur, dict) and piece in cur:
                    cur = cur[piece]
                else:
                    return False, f"json_path {self.json_path!r} not found"
        return True, "all verdict rules satisfied"


@dataclass
class QAStep:
    """One executable step in a QA plan."""

    id: str
    kind: StepKind
    description: str = ""
    ac: list[str] = field(default_factory=list)
    # Kind-specific fields. Only the relevant ones are populated.
    prompt: str = ""             # AGENT
    run: str = ""                # SHELL
    cwd: str = ""                # SHELL
    env: dict[str, str] = field(default_factory=dict)  # SHELL
    timeout_s: int = 60          # SHELL / HTTP / MCP
    method: str = "GET"          # HTTP
    url: str = ""                # HTTP
    headers: dict[str, str] = field(default_factory=dict)  # HTTP
    body: str = ""               # HTTP
    tool: str = ""               # MCP
    args: dict[str, Any] = field(default_factory=dict)     # MCP
    verdict: StepVerdict = field(default_factory=StepVerdict)
    on_fail: str = "continue"    # "continue" | "abort"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> QAStep:
        kind_raw = d.get("kind", "agent")
        try:
            kind = StepKind(kind_raw)
        except ValueError as exc:
            msg = f"unknown step kind: {kind_raw!r}"
            raise ValueError(msg) from exc
        ac_field = d.get("ac")
        ac_list: list[str]
        if ac_field is None:
            ac_list = []
        elif isinstance(ac_field, str):
            ac_list = [ac_field]
        else:
            ac_list = [str(x) for x in ac_field]
        return cls(
            id=str(d.get("id", "")) or _new_step_id(),
            kind=kind,
            description=str(d.get("description", "")),
            ac=ac_list,
            prompt=str(d.get("prompt", "")),
            run=str(d.get("run", "")),
            cwd=str(d.get("cwd", "")),
            env=dict(d.get("env", {}) or {}),
            timeout_s=int(d.get("timeout_s", 60)),
            method=str(d.get("method", "GET")).upper(),
            url=str(d.get("url", "")),
            headers=dict(d.get("headers", {}) or {}),
            body=str(d.get("body", "")),
            tool=str(d.get("tool", "")),
            args=dict(d.get("args", {}) or {}),
            verdict=StepVerdict.from_dict(d.get("verdict")),
            on_fail=str(d.get("on_fail", "continue")),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind.value,
        }
        if self.description:
            out["description"] = self.description
        if self.ac:
            out["ac"] = list(self.ac)
        if self.kind is StepKind.AGENT and self.prompt:
            out["prompt"] = self.prompt
        if self.kind is StepKind.SHELL:
            out["run"] = self.run
            if self.cwd:
                out["cwd"] = self.cwd
            if self.env:
                out["env"] = dict(self.env)
        if self.kind is StepKind.HTTP:
            out["method"] = self.method
            out["url"] = self.url
            if self.headers:
                out["headers"] = dict(self.headers)
            if self.body:
                out["body"] = self.body
        if self.kind is StepKind.MCP:
            out["tool"] = self.tool
            if self.args:
                out["args"] = dict(self.args)
        if self.timeout_s != 60:
            out["timeout_s"] = self.timeout_s
        if not self.verdict.is_empty():
            out["verdict"] = self.verdict.to_dict()
        if self.on_fail != "continue":
            out["on_fail"] = self.on_fail
        return out


def _new_step_id() -> str:
    return f"step-{uuid.uuid4().hex[:6]}"


@dataclass
class QAPlan:
    """A complete QA plan."""

    plan_id: str
    name: str = ""
    product: str = ""
    description: str = ""
    created_at: float = 0.0
    acceptance_criteria: list[AcceptanceCriterion] = field(default_factory=list)
    vars_spec: dict[str, dict[str, Any]] = field(default_factory=dict)
    steps: list[QAStep] = field(default_factory=list)
    artifact_format: str = "markdown"  # markdown | json | jira-comment | stdout

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "name": self.name,
            "product": self.product,
            "description": self.description,
            "created_at": self.created_at,
            "acceptance_criteria": [ac.to_dict() for ac in self.acceptance_criteria],
            "vars": self.vars_spec,
            "steps": [s.to_dict() for s in self.steps],
            "artifact_format": self.artifact_format,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> QAPlan:
        ac_raw = d.get("acceptance_criteria", []) or []
        acs = [
            AcceptanceCriterion.from_dict(ac, fallback_id=f"AC{i}")
            for i, ac in enumerate(ac_raw, 1)
        ]
        steps_raw = d.get("steps", []) or []
        steps = [QAStep.from_dict(s) for s in steps_raw]
        return cls(
            plan_id=str(d.get("plan_id") or _new_plan_id()),
            name=str(d.get("name", "")),
            product=str(d.get("product", "")),
            description=str(d.get("description", "")),
            created_at=float(d.get("created_at", 0.0) or 0.0),
            acceptance_criteria=acs,
            vars_spec=dict(d.get("vars", {}) or {}),
            steps=steps,
            artifact_format=str(d.get("artifact_format", "markdown")),
        )


def _new_plan_id() -> str:
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    short = uuid.uuid4().hex[:6]
    return f"qa-{stamp}-{short}"


# ---------------------------------------------------------------------------
# Save / load — project-scoped to <project>/.bog-agents/qa-plans/
# ---------------------------------------------------------------------------


def plans_dir(project_root: Path) -> Path:
    """Return ``<project>/.bog-agents/qa-plans/`` (created on demand)."""
    d = project_root / ".bog-agents" / "qa-plans"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_plan(project_root: Path, plan: QAPlan) -> Path:
    """Save ``plan`` as YAML and return the file path."""
    if not plan.plan_id:
        plan.plan_id = _new_plan_id()
    if not plan.created_at:
        plan.created_at = time.time()
    path = plans_dir(project_root) / f"{plan.plan_id}.yaml"
    text = "# QA plan — edit freely. Run with `/qa run <plan_id>`.\n"
    text += "# 'vars' uses bog-agents Vars syntax (string|secret|enum|int|bool).\n"
    text += "# Step kinds: agent (LLM), shell, http, mcp.\n\n"
    text += yaml.safe_dump(plan.to_dict(), sort_keys=False, allow_unicode=True)
    path.write_text(text, encoding="utf-8")
    return path


def load_plan(path: Path) -> QAPlan:
    """Load a plan from YAML / JSON. Tolerates either suffix."""
    raw = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix == ".json":
        import json

        data = json.loads(raw)
    else:
        data = yaml.safe_load(raw) or {}
    if not isinstance(data, dict):
        msg = f"plan {path} did not parse to a dict"
        raise ValueError(msg)
    return QAPlan.from_dict(data)


def list_plans(project_root: Path) -> list[QAPlan]:
    """Return all plans in the project, newest first."""
    d = project_root / ".bog-agents" / "qa-plans"
    if not d.exists():
        return []
    plans: list[QAPlan] = []
    for path in sorted(d.iterdir()):
        if path.suffix.lower() not in (".yaml", ".yml", ".json"):
            continue
        try:
            plans.append(load_plan(path))
        except (yaml.YAMLError, OSError, ValueError) as exc:
            logger.warning("skipping unparseable plan %s: %s", path, exc)
    plans.sort(key=lambda p: p.created_at, reverse=True)
    return plans


def find_plan(project_root: Path, plan_id: str) -> Path | None:
    """Resolve ``plan_id`` to a plan file (exact or substring match)."""
    d = project_root / ".bog-agents" / "qa-plans"
    if not d.exists():
        return None
    for ext in (".yaml", ".yml", ".json"):
        candidate = d / f"{plan_id}{ext}"
        if candidate.is_file():
            return candidate
    matches = sorted(p for p in d.iterdir() if plan_id in p.stem)
    return matches[0] if matches else None
