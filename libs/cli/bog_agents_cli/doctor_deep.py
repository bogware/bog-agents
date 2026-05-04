"""Deep doctor — probe external deps and print a one-page health summary.

The base ``--doctor`` checks environment + dependencies + config (static).
``--doctor-deep`` adds *runtime* probes:

- Can we reach the configured model provider's endpoint? (no actual model
  call — just a TCP/TLS handshake to keep this fast and free)
- Is git available + functional?
- Can we write to ``~/.bog-agents/`` and the project's
  ``.bog-agents/`` directory?
- Is the MCP config parseable and do declared servers' commands resolve
  on PATH?
- For each provider with a credential in env, does the env var look
  syntactically valid (length, prefix)?

Each check returns a :class:`Probe` and the report aggregates them into
a one-page text block. Probes never raise — every failure becomes a
``Probe(status="fail", detail=...)``. Total runtime is bounded; each
probe has its own short timeout.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Probe:
    name: str
    status: str  # "ok" | "warn" | "fail" | "skip"
    detail: str = ""
    duration_ms: int = 0


# Order matters: base environment first, then external deps, then auth.
ProbeFn = Callable[[], Probe]


def run_deep_doctor() -> str:
    """Run all probes and return a formatted report."""
    started = time.time()
    probes: list[Probe] = []
    for fn in _PROBES:
        probes.append(_safe_run(fn))
    duration_ms = int((time.time() - started) * 1000)
    return _format_report(probes, total_ms=duration_ms)


def _safe_run(fn: ProbeFn) -> Probe:
    """Run a probe and convert any unhandled exception into a Probe(fail).

    Probes are written defensively, but a host-specific OS quirk could
    still raise — we don't want one bad probe to abort the whole report.
    """
    started = time.time()
    try:
        probe = fn()
    except Exception as exc:
        return Probe(
            name=getattr(fn, "__name__", "<unknown>"),
            status="fail",
            detail=f"probe crashed: {exc.__class__.__name__}: {exc}",
            duration_ms=int((time.time() - started) * 1000),
        )
    if probe.duration_ms == 0:
        probe.duration_ms = int((time.time() - started) * 1000)
    return probe


# ---------------------------------------------------------------------------
# Individual probes
# ---------------------------------------------------------------------------


def _probe_python() -> Probe:
    return Probe(
        name="python",
        status="ok",
        detail=f"{sys.version.split()[0]} on {sys.platform}",
    )


def _probe_user_agents_dir_writable() -> Probe:
    home = Path.home() / ".bog-agents"
    try:
        home.mkdir(parents=True, exist_ok=True)
        probe_file = home / ".doctor-probe"
        probe_file.write_text("doctor probe", encoding="utf-8")
        probe_file.unlink()
    except OSError as exc:
        return Probe(name="user-agents-dir", status="fail", detail=f"{home}: {exc}")
    return Probe(name="user-agents-dir", status="ok", detail=str(home))


def _probe_project_dir_writable() -> Probe:
    project = Path.cwd() / ".bog-agents"
    try:
        project.mkdir(parents=True, exist_ok=True)
        probe_file = project / ".doctor-probe"
        probe_file.write_text("doctor probe", encoding="utf-8")
        probe_file.unlink()
    except OSError as exc:
        return Probe(name="project-dir", status="warn", detail=f"{project}: {exc}")
    return Probe(name="project-dir", status="ok", detail=str(project))


def _probe_git() -> Probe:
    git_path = shutil.which("git")
    if git_path is None:
        return Probe(
            name="git",
            status="warn",
            detail="git not on PATH — auto-commit and /record will be limited",
        )
    try:
        from bog_agents_cli._constants import GIT_PROBE_TIMEOUT_S

        result = subprocess.run(  # noqa: S603
            [git_path, "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=GIT_PROBE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return Probe(name="git", status="fail", detail="git --version timed out")
    if result.returncode != 0:
        return Probe(name="git", status="fail", detail=f"exit {result.returncode}")
    return Probe(name="git", status="ok", detail=result.stdout.strip())


def _probe_provider_envs() -> Probe:
    """Detect which provider credentials are present (no calls)."""
    candidates = [
        ("ANTHROPIC_API_KEY", "sk-ant"),
        ("OPENAI_API_KEY", "sk-"),
        ("GOOGLE_API_KEY", ""),
        ("GROQ_API_KEY", "gsk_"),
        ("DEEPSEEK_API_KEY", ""),
        ("MISTRAL_API_KEY", ""),
        ("FIREWORKS_API_KEY", ""),
        ("AWS_ACCESS_KEY_ID", "AKIA"),
    ]
    present = []
    issues = []
    for var, expected_prefix in candidates:
        val = os.environ.get(var, "").strip()
        if not val:
            continue
        present.append(var)
        if expected_prefix and not val.startswith(expected_prefix):
            issues.append(f"{var} does not start with expected prefix {expected_prefix!r}")
        if len(val) < 12:
            issues.append(f"{var} is unusually short ({len(val)} chars)")
    if not present:
        return Probe(
            name="provider-envs",
            status="warn",
            detail="no provider API keys found in env — agent runs will fail without one",
        )
    if issues:
        return Probe(
            name="provider-envs",
            status="warn",
            detail=f"{len(present)} key(s) present; issues: " + "; ".join(issues),
        )
    return Probe(
        name="provider-envs",
        status="ok",
        detail=f"{len(present)} provider key(s): " + ", ".join(present),
    )


_TCP_PROBE_TIMEOUT = 3.0


def _probe_anthropic_reachable() -> Probe:
    return _tcp_probe("anthropic-api", "api.anthropic.com", 443)


def _probe_openai_reachable() -> Probe:
    return _tcp_probe("openai-api", "api.openai.com", 443)


def _tcp_probe(name: str, host: str, port: int) -> Probe:
    """TCP connect-only probe. Doesn't call the API — just verifies network."""
    try:
        with socket.create_connection((host, port), timeout=_TCP_PROBE_TIMEOUT):
            return Probe(name=name, status="ok", detail=f"{host}:{port} reachable")
    except (TimeoutError, OSError) as exc:
        return Probe(
            name=name,
            status="warn",
            detail=f"{host}:{port}: {exc.__class__.__name__}: {exc}",
        )


def _probe_mcp_config() -> Probe:
    cfg = Path.home() / ".bog-agents" / ".mcp.json"
    if not cfg.is_file():
        return Probe(name="mcp-config", status="ok", detail="no MCP config (none required)")
    try:
        from bog_agents_cli.mcp_config_manager import load_user_mcp_config

        data = load_user_mcp_config()
    except Exception as exc:
        return Probe(name="mcp-config", status="fail", detail=f"unparseable: {exc}")
    servers = data.get("mcpServers", {}) or {}
    if not servers:
        return Probe(name="mcp-config", status="ok", detail="0 servers configured")
    missing = []
    for name, server in servers.items():
        if not isinstance(server, dict):
            missing.append(name)
            continue
        cmd = server.get("command")
        if isinstance(cmd, str) and cmd and shutil.which(cmd) is None:
            # Could be a python module run via -m; that case wouldn't
            # resolve via which but is still valid — surface as a warning.
            missing.append(f"{name} (cmd={cmd!r} not on PATH)")
    if missing:
        return Probe(
            name="mcp-config",
            status="warn",
            detail=f"{len(servers)} server(s); issues: " + "; ".join(missing),
        )
    return Probe(name="mcp-config", status="ok", detail=f"{len(servers)} server(s) — all commands resolve")


def _probe_settings_files() -> Probe:
    found = []
    issues = []
    for label, path in [
        ("user", Path.home() / ".bog-agents" / "settings.json"),
        ("project", Path.cwd() / ".bog-agents" / "settings.json"),
    ]:
        if not path.is_file():
            continue
        found.append(label)
        try:
            import json

            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                issues.append(f"{label} settings.json top-level is not an object")
        except (OSError, json.JSONDecodeError) as exc:
            issues.append(f"{label}: {exc.__class__.__name__}: {exc}")
    if issues:
        return Probe(name="settings", status="fail", detail="; ".join(issues))
    if not found:
        return Probe(name="settings", status="ok", detail="no settings files (defaults active)")
    return Probe(name="settings", status="ok", detail=f"loaded: {', '.join(found)}")


def _probe_panic_dir() -> Probe:
    crash = Path.home() / ".bog-agents" / "crash"
    if not crash.exists():
        return Probe(name="crash-dumps", status="ok", detail="no recent panics")
    files = sorted(crash.glob("*.log"), reverse=True)
    if not files:
        return Probe(name="crash-dumps", status="ok", detail="dir present, no dumps")
    return Probe(
        name="crash-dumps",
        status="warn",
        detail=f"{len(files)} dump(s); most recent: {files[0].name}",
    )


_PROBES: list[ProbeFn] = [
    _probe_python,
    _probe_user_agents_dir_writable,
    _probe_project_dir_writable,
    _probe_settings_files,
    _probe_git,
    _probe_provider_envs,
    _probe_anthropic_reachable,
    _probe_openai_reachable,
    _probe_mcp_config,
    _probe_panic_dir,
]


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------


_GLYPH = {"ok": "✓", "warn": "!", "fail": "✗", "skip": "-"}


def _format_report(probes: list[Probe], *, total_ms: int) -> str:
    width = max((len(p.name) for p in probes), default=10) + 2
    lines = ["bog-agents doctor --deep", "=" * 60]
    counts: dict[str, int] = {"ok": 0, "warn": 0, "fail": 0, "skip": 0}
    for p in probes:
        glyph = _GLYPH.get(p.status, "?")
        name = p.name.ljust(width)
        lines.append(f"  {glyph} {name} {p.detail}")
        counts[p.status] = counts.get(p.status, 0) + 1
    lines.append("=" * 60)
    summary = ", ".join(
        f"{label}={counts[label]}" for label in ("ok", "warn", "fail") if counts[label]
    ) or "no checks run"
    lines.append(f"  {summary}  ({total_ms}ms)")
    if counts.get("fail", 0) > 0:
        lines.append("")
        lines.append(
            "Some critical checks failed. Fix these before running interactive "
            "agent sessions; warnings are advisory."
        )
    return "\n".join(lines) + "\n"


# Defensive: silence unused-import warnings.
_ = field
