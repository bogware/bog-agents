"""`/proxy` — register a shell command template as a LangChain tool.

The mental model: most "I want the agent to use my CLI" cases don't
need a full MCP server. They need a one-line declaration that turns
``gh issue list --state open --json number,title`` into a tool the
model can call with a JSON arg.

This module persists proxy definitions to
``~/.bog-agents/proxies.toml`` and exposes a builder that materialises
them as LangChain ``StructuredTool`` instances at agent build time.

A proxy declaration looks like:

.. code-block:: toml

    [tools.list_issues]
    description = "List open issues on the current repo"
    command = "gh issue list --state open --json number,title --limit 20"
    timeout_seconds = 15

Templated arguments use ``{name}`` placeholders:

.. code-block:: toml

    [tools.search_repo]
    description = "Search the repo with ripgrep"
    command = "rg -n --max-count 50 -- {pattern} ."
    args = ["pattern"]
    timeout_seconds = 10

Each ``args`` entry becomes a string parameter on the tool's pydantic
schema. Substituted values are always ``shlex.quote``-wrapped (POSIX)
or cmd.exe-escaped (Windows) so the agent cannot inject shell metas
through the parameter — same defence the QA executor uses.
"""

from __future__ import annotations

import logging
import re
import shlex
import subprocess  # noqa: S404 — shell invocation IS the feature
import sys
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import tomli_w

from bog_agents_cli.feature_helpers import feature_state_dir


def _shell_quote_for_platform(value: str) -> str:
    """Quote ``value`` so it cannot break out of a shell-command argument.

    POSIX: standard :func:`shlex.quote`. Windows: ``subprocess.list2cmdline``
    for the C-runtime-quoted form plus carat-escapes for the residual
    cmd.exe metacharacters (``^ & | < > ( ) % !``) that survive C
    quoting. Used when interpolating untrusted variable values into a
    template that will be handed to ``subprocess.Popen(..., shell=True)``.
    This is a local copy of the same helper that lives in
    :mod:`bog_agents_cli.vars` for the QA executor — keeping it
    co-located avoids a circular dependency back through ``vars`` and
    keeps the security defence visible right next to the shell-spawn
    site below.
    """
    if sys.platform != "win32":
        return shlex.quote(value)
    quoted = subprocess.list2cmdline([value])
    for meta in ("^", "&", "|", "<", ">", "(", ")", "%", "!"):
        quoted = quoted.replace(meta, "^" + meta)
    return quoted


if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

logger = logging.getLogger(__name__)


_PROXIES_FILENAME = "proxies.toml"
_DEFAULT_TIMEOUT = 30
_MAX_OUTPUT_CHARS = 12_000
_TEMPLATE_PATTERN = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
_TOOL_NAME_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


class ProxyError(ValueError):
    """User-facing /proxy subsystem failure."""


@dataclass
class ProxyDefinition:
    """One persisted proxy tool."""

    name: str
    """Identifier used as the LangChain tool name."""

    description: str
    """One-line description shown to the model."""

    command: str
    """Shell command template with ``{name}`` placeholders for args."""

    args: list[str] = field(default_factory=list)
    """Names of parameters the model must supply (matches ``{name}`` placeholders)."""

    timeout_seconds: int = _DEFAULT_TIMEOUT
    cwd: str = ""
    """Optional working directory. Defaults to the active app cwd."""

    def validate(self) -> None:
        """Check the definition for soundness.

        Raises:
            ProxyError: When name/description/command is missing, when
                placeholders reference undeclared args, or when the
                timeout is out of range.
        """
        if not _TOOL_NAME_PATTERN.match(self.name):
            msg = (
                f"proxy name {self.name!r} must match {_TOOL_NAME_PATTERN.pattern} "
                "(letters, digits, underscore; must start with letter/underscore)"
            )
            raise ProxyError(msg)
        if not self.description.strip():
            msg = f"proxy {self.name!r} requires a description"
            raise ProxyError(msg)
        if not self.command.strip():
            msg = f"proxy {self.name!r} requires a non-empty command"
            raise ProxyError(msg)
        placeholders = set(_TEMPLATE_PATTERN.findall(self.command))
        declared = set(self.args)
        unknown = placeholders - declared
        unused = declared - placeholders
        if unknown:
            msg = (
                f"proxy {self.name!r} references undeclared args "
                f"{sorted(unknown)} — add them to the [args] list"
            )
            raise ProxyError(msg)
        if unused:
            logger.warning(
                "proxy %s declares unused args %s — they'll never be substituted",
                self.name,
                sorted(unused),
            )
        if self.timeout_seconds <= 0 or self.timeout_seconds > 600:
            msg = (
                f"proxy {self.name!r} timeout_seconds={self.timeout_seconds} "
                "must be in (0, 600]"
            )
            raise ProxyError(msg)


# --------------------------------------------------------------------------- #
# Persistence                                                                 #
# --------------------------------------------------------------------------- #


def proxies_path() -> Path:
    return feature_state_dir() / _PROXIES_FILENAME


def load_proxies(path: Path | None = None) -> dict[str, ProxyDefinition]:
    """Load all proxy definitions from disk.

    Malformed entries are skipped with a logged warning; valid entries
    are returned. Returns an empty dict when the file doesn't exist.
    """
    target = path or proxies_path()
    if not target.exists():
        return {}
    try:
        data = tomllib.loads(target.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        logger.warning("Failed to parse %s: %s", target, exc)
        return {}

    tools_section = data.get("tools")
    if not isinstance(tools_section, dict):
        return {}

    out: dict[str, ProxyDefinition] = {}
    for name, spec in tools_section.items():
        if not isinstance(spec, dict):
            continue
        try:
            proxy = ProxyDefinition(
                name=str(name),
                description=str(spec.get("description", "")),
                command=str(spec.get("command", "")),
                args=[str(a) for a in spec.get("args", [])],
                timeout_seconds=int(spec.get("timeout_seconds", _DEFAULT_TIMEOUT)),
                cwd=str(spec.get("cwd", "")),
            )
            proxy.validate()
        except (ProxyError, TypeError, ValueError) as exc:
            logger.warning("Skipping malformed proxy %s: %s", name, exc)
            continue
        out[proxy.name] = proxy
    return out


def save_proxies(
    proxies: dict[str, ProxyDefinition], *, path: Path | None = None
) -> None:
    """Atomically write the proxies file."""
    target = path or proxies_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"tools": {}}
    for name, proxy in proxies.items():
        entry: dict[str, Any] = {
            "description": proxy.description,
            "command": proxy.command,
            "timeout_seconds": proxy.timeout_seconds,
        }
        if proxy.args:
            entry["args"] = proxy.args
        if proxy.cwd:
            entry["cwd"] = proxy.cwd
        payload["tools"][name] = entry
    tmp = target.with_suffix(target.suffix + ".tmp")
    with tmp.open("wb") as fh:
        tomli_w.dump(payload, fh)
    tmp.replace(target)


def add_proxy(proxy: ProxyDefinition, *, path: Path | None = None) -> None:
    """Validate + persist a new proxy. Overwrites on name collision."""
    proxy.validate()
    proxies = load_proxies(path)
    proxies[proxy.name] = proxy
    save_proxies(proxies, path=path)


def remove_proxy(name: str, *, path: Path | None = None) -> bool:
    """Delete a proxy by name. Returns whether anything was removed."""
    proxies = load_proxies(path)
    if name not in proxies:
        return False
    del proxies[name]
    save_proxies(proxies, path=path)
    return True


# --------------------------------------------------------------------------- #
# Argument parser for /proxy add                                              #
# --------------------------------------------------------------------------- #


_QUOTED_PAIR_RE = re.compile(r"--(\w+)\s+\"([^\"]*)\"")
_UNQUOTED_PAIR_RE = re.compile(r"--(\w+)\s+(\S+)")


def parse_add_args(raw: str) -> ProxyDefinition:
    """Parse a ``/proxy add`` command into a :class:`ProxyDefinition`.

    Expected forms (any order, but ``--cmd`` is required)::

        --name list_issues --cmd "gh issue list --state open"
            --desc "List open issues"

        --name search_repo --cmd "rg -n -- {pattern} ."
            --desc "Search the repo" --args pattern --timeout 10

    Args:
        raw: The argument string after ``/proxy add``.

    Returns:
        Validated :class:`ProxyDefinition`.

    Raises:
        ProxyError: When required fields are missing or malformed.
    """
    if not raw.strip():
        msg = (
            'Usage: /proxy add --name <name> --cmd "<command>" '
            '--desc "<description>" [--args arg1 arg2 ...] '
            "[--timeout <seconds>] [--cwd <path>]"
        )
        raise ProxyError(msg)

    fields: dict[str, str] = {}
    consumed: set[int] = set()

    # Quoted values first so they win over bare-token matching.
    for m in _QUOTED_PAIR_RE.finditer(raw):
        fields[m.group(1).lower()] = m.group(2)
        consumed.update(range(m.start(), m.end()))

    # Remaining unquoted "--key value" pairs.
    for m in _UNQUOTED_PAIR_RE.finditer(raw):
        if m.start() in consumed:
            continue
        key = m.group(1).lower()
        if key in fields:
            continue
        fields[key] = m.group(2)
        consumed.update(range(m.start(), m.end()))

    name = fields.get("name", "").strip()
    command = fields.get("cmd") or fields.get("command") or ""
    description = fields.get("desc") or fields.get("description") or ""
    timeout_raw = (
        fields.get("timeout") or fields.get("timeout_seconds") or str(_DEFAULT_TIMEOUT)
    )
    cwd = fields.get("cwd", "").strip()
    args_raw = fields.get("args", "").strip()

    if not name:
        msg = "missing required --name <tool_name>"
        raise ProxyError(msg)
    if not command:
        msg = 'missing required --cmd "<command template>"'
        raise ProxyError(msg)
    if not description:
        msg = 'missing required --desc "<description>"'
        raise ProxyError(msg)

    try:
        timeout = int(timeout_raw)
    except ValueError as exc:
        msg = f"--timeout must be an integer, got {timeout_raw!r}"
        raise ProxyError(msg) from exc

    args: list[str] = []
    if args_raw:
        # Allow either comma-separated or whitespace-separated.
        parts = re.split(r"[,\s]+", args_raw)
        args = [p.strip() for p in parts if p.strip()]

    proxy = ProxyDefinition(
        name=name,
        description=description,
        command=command,
        args=args,
        timeout_seconds=timeout,
        cwd=cwd,
    )
    proxy.validate()
    return proxy


# --------------------------------------------------------------------------- #
# Execution                                                                   #
# --------------------------------------------------------------------------- #


@dataclass
class ProxyResult:
    """Outcome of a single proxy execution."""

    exit_code: int
    output: str
    error: str
    elapsed_seconds: float


def render_command(proxy: ProxyDefinition, values: dict[str, str]) -> str:
    """Substitute placeholders, applying shell-quote to every value.

    Raises:
        ProxyError: If a declared arg is missing from ``values``.
    """
    missing = [a for a in proxy.args if a not in values]
    if missing:
        msg = (
            f"proxy {proxy.name!r} requires args {missing} which were "
            "not supplied by the model"
        )
        raise ProxyError(msg)

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        raw = str(values.get(key, ""))
        return _shell_quote_for_platform(raw)

    return _TEMPLATE_PATTERN.sub(_replace, proxy.command)


def execute_proxy(
    proxy: ProxyDefinition,
    values: dict[str, str],
    *,
    cwd: Path | None = None,
) -> ProxyResult:
    """Run a proxy and return its captured output.

    Output is capped at :data:`_MAX_OUTPUT_CHARS` so a wedged command
    can't blow up the agent's context. The full size is recorded in
    ``error`` when truncation happens, so the agent can decide whether
    to re-run with a tighter query.
    """
    rendered = render_command(proxy, values)
    work_dir = proxy.cwd or (str(cwd) if cwd else None)
    start = time.monotonic()
    try:
        result = subprocess.run(  # noqa: S602 — shell is the entire point; quoting handled above
            rendered,
            shell=True,
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=proxy.timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - start
        return ProxyResult(
            exit_code=-1,
            output="",
            error=f"timeout after {proxy.timeout_seconds}s",
            elapsed_seconds=elapsed,
        )
    except FileNotFoundError as exc:
        elapsed = time.monotonic() - start
        return ProxyResult(
            exit_code=-1,
            output="",
            error=f"launch failed: {exc}",
            elapsed_seconds=elapsed,
        )

    elapsed = time.monotonic() - start
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    combined = stdout or stderr
    if len(combined) > _MAX_OUTPUT_CHARS:
        combined = combined[:_MAX_OUTPUT_CHARS] + "\n…[output truncated]"
    error_text = "" if result.returncode == 0 else stderr.strip()[:1_000]
    return ProxyResult(
        exit_code=result.returncode,
        output=combined,
        error=error_text,
        elapsed_seconds=elapsed,
    )


# --------------------------------------------------------------------------- #
# LangChain tool wrapping                                                     #
# --------------------------------------------------------------------------- #


def build_proxy_tools(
    proxies: dict[str, ProxyDefinition] | None = None,
    *,
    cwd: Path | None = None,
) -> list[BaseTool]:
    """Materialise every persisted proxy as a LangChain ``StructuredTool``.

    Called from agent.py at agent-build time. When no proxies are
    configured, returns an empty list and the agent stack is unchanged.

    Args:
        proxies: Optional override map (tests). Falls back to the
            on-disk file.
        cwd: Working directory passed through to executions.

    Returns:
        A list of ``BaseTool`` instances ready to attach to the agent.
    """
    try:
        from langchain_core.tools import StructuredTool
        from pydantic import BaseModel, Field, create_model
    except ImportError:
        logger.debug("langchain_core/pydantic not installed; skipping proxy tools")
        return []

    resolved = proxies if proxies is not None else load_proxies()
    if not resolved:
        return []

    tools: list[BaseTool] = []
    for proxy in resolved.values():
        # Build a pydantic schema mirroring the proxy's declared args.
        if proxy.args:
            field_defs: dict[str, Any] = {
                arg: (str, Field(..., description=f"Value for {{{arg}}}"))
                for arg in proxy.args
            }
            arg_schema: type[BaseModel] = create_model(
                f"{proxy.name.title()}Args", **field_defs
            )
        else:
            arg_schema = create_model(f"{proxy.name.title()}Args")

        def _make_runner(p: ProxyDefinition) -> Any:  # noqa: ANN401 — StructuredTool accepts any callable
            def _run(**kwargs: str) -> str:
                # Cast all incoming values to string; the schema enforces
                # presence, but the model might supply ints when the prompt
                # implied a number.
                values = {k: str(v) for k, v in kwargs.items()}
                result = execute_proxy(p, values, cwd=cwd)
                if result.exit_code == 0:
                    return result.output
                err = result.error or "(no stderr)"
                return f"[exit {result.exit_code}] {err}\n\n{result.output}"

            return _run

        tools.append(
            StructuredTool.from_function(
                func=_make_runner(proxy),
                name=proxy.name,
                description=proxy.description,
                args_schema=arg_schema,
            )
        )
    return tools


# --------------------------------------------------------------------------- #
# App handler glue                                                            #
# --------------------------------------------------------------------------- #


def _format_proxy(proxy: ProxyDefinition) -> str:
    args = ", ".join(proxy.args) if proxy.args else "(no args)"
    return (
        f"[bold]{proxy.name}[/bold] [dim]({args}, "
        f"{proxy.timeout_seconds}s)[/dim]\n"
        f"  {proxy.description}\n"
        f"  [cyan]{proxy.command}[/cyan]"
    )


async def handle_proxy_subcommand(app: object, raw_arg: str) -> None:
    """Dispatch ``/proxy <sub>`` subcommands."""
    from bog_agents_cli.widgets.chat_messages import AppMessage, ErrorMessage

    arg = raw_arg.strip()
    head, _, rest = arg.partition(" ")
    head = head.lower()
    rest = rest.strip()

    if not head or head == "list":
        proxies = load_proxies()
        if not proxies:
            await app._mount_message(  # type: ignore[attr-defined]
                AppMessage(
                    "[dim]No proxy tools configured.[/dim]\n"
                    "Add one with: [bold]/proxy add --name <n> --cmd "
                    '"<cmd>" --desc "<desc>"[/bold]'
                )
            )
            return
        lines = [
            f"[bold]{len(proxies)} proxy tools[/bold] ([cyan]{proxies_path()}[/cyan])\n"
        ]
        for proxy in proxies.values():
            lines.append(_format_proxy(proxy))
        lines.append(
            "\n[dim]Restart the agent (or open a new session) to "
            "load tool changes.[/dim]"
        )
        await app._mount_message(AppMessage("\n".join(lines)))  # type: ignore[attr-defined]
        return

    if head == "show":
        target = rest.strip()
        proxies = load_proxies()
        proxy = proxies.get(target)
        if proxy is None:
            await app._mount_message(  # type: ignore[attr-defined]
                ErrorMessage(f"No proxy named {target!r}")
            )
            return
        await app._mount_message(AppMessage(_format_proxy(proxy)))  # type: ignore[attr-defined]
        return

    if head == "add":
        try:
            proxy = parse_add_args(rest)
            add_proxy(proxy)
        except ProxyError as exc:
            await app._mount_message(ErrorMessage(f"/proxy add: {exc}"))  # type: ignore[attr-defined]
            return
        await app._mount_message(  # type: ignore[attr-defined]
            AppMessage(
                f"[bold]Registered proxy[/bold] [bold]{proxy.name}[/bold]\n"
                f"{_format_proxy(proxy)}\n\n"
                "[dim]Restart the agent (or open a new session) so the "
                "tool becomes available to the model.[/dim]"
            )
        )
        return

    if head in {"remove", "rm", "delete"}:
        target = rest.strip()
        if not target:
            await app._mount_message(  # type: ignore[attr-defined]
                ErrorMessage("Usage: /proxy remove <name>")
            )
            return
        if not remove_proxy(target):
            await app._mount_message(  # type: ignore[attr-defined]
                ErrorMessage(f"No proxy named {target!r}")
            )
            return
        await app._mount_message(  # type: ignore[attr-defined]
            AppMessage(
                f"[bold]Removed proxy[/bold] [bold]{target}[/bold]\n"
                "[dim]The tool will be gone next time the agent is built.[/dim]"
            )
        )
        return

    await app._mount_message(  # type: ignore[attr-defined]
        AppMessage(
            "Usage:\n"
            "  /proxy list                    Show registered proxies\n"
            "  /proxy show <name>             Show one proxy's full definition\n"
            '  /proxy add --name <n> --cmd "<cmd>" --desc "<desc>"\n'
            "                                 [--args arg1 arg2 ...]\n"
            "                                 [--timeout <secs>] [--cwd <path>]\n"
            "  /proxy remove <name>           Unregister a proxy"
        )
    )


# Re-export for tests
__all__ = [
    "ProxyDefinition",
    "ProxyError",
    "ProxyResult",
    "add_proxy",
    "build_proxy_tools",
    "execute_proxy",
    "load_proxies",
    "parse_add_args",
    "remove_proxy",
    "render_command",
    "save_proxies",
]
