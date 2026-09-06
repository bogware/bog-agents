"""Governed Code Mode (ROADMAP #72): a script calls tools, every call re-enters the tool path.

`CodeModeMiddleware` adds a `run_code` tool. The model writes a Python
script; it runs in a child interpreter (`python -I`, no site packages, cwd
pinned, optionally wrapped in `LocalSandbox`) that has no tools of its own —
only a `tools` proxy. Every `tools.<name>(**kwargs)` the script makes is sent
back to the parent as a JSON line and executed there through the *same*
wrapper chain the model's own tool calls take (Expert rules, SafeTools, the
action log, cost tracking …), so a script cannot reach past governance by
going through code. Two things are stricter than a direct call, on purpose:
a tool the session gates behind human approval is refused inside code mode
(a script cannot pause for a human), and every `tools.task(...)` counts
against `RunawayCaps` before it spawns. `fanout` and `vote` helpers live in
the child so a script can map a tool over many inputs and pick a majority
answer without inventing its own loop. `execute_mcp_script` is the same tool
with the namespace narrowed to the connected MCP tools.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import sys
import tempfile
import textwrap
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain.tools import ToolRuntime
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, StructuredTool

if TYPE_CHECKING:
    from bog_agents.cost_ledger import CostLedger
    from bog_agents.sandbox.local_sandbox import LocalSandbox

logger = logging.getLogger(__name__)

PROTOCOL_PREFIX = "\x00BOG\x00"
DEFAULT_TIMEOUT = 120.0
DEFAULT_MAX_CALLS = 200
_OUTPUT_CHARS = 20_000
_RESULT_CHARS = 8_000

RUN_CODE_DESCRIPTION = """Run a Python script that orchestrates tools programmatically (Code Mode).

Inside the script:
- `tools.<name>(**kwargs)` calls a tool and returns its text result (the same tools you can call directly; every call is governed exactly like a direct call — denied calls raise `ToolDenied`).
- `tools.task(subagent_type=..., description=...)` delegates to a subagent (each spawn counts against the session caps).
- `fanout(fn, items, limit=8)` maps `fn` over `items` sequentially and returns the results; `vote(candidates)` returns the majority answer.
- `print(...)` output is returned to you; the script's final expression is not — print what you need.
Use it for loops over many files, chained tool calls whose intermediate results you do not need to see, or fan-out / vote patterns. Keep scripts short; tools that need human approval must be called directly instead.
Available tools in this namespace: {tool_names}"""

_CHILD_RUNNER = textwrap.dedent(
    '''
    import json, sys, builtins, traceback
    PREFIX = "\\x00BOG\\x00"
    _out = sys.stdout
    _in = sys.stdin

    class ToolDenied(RuntimeError):
        """The parent refused a tool call (governance)."""

    def _rpc(payload, wait=True):
        _out.write(PREFIX + json.dumps(payload) + "\\n")
        _out.flush()
        if not wait:
            return ""
        line = _in.readline()
        if not line:
            raise RuntimeError("code mode: parent closed the channel")
        reply = json.loads(line)
        if reply.get("error"):
            raise ToolDenied(reply["error"])
        return reply.get("result", "")

    class _Tools:
        def __getattr__(self, name):
            if name.startswith("_"):
                raise AttributeError(name)
            def _call(*args, **kwargs):
                if args:
                    raise TypeError(f"tools.{name}: pass keyword arguments only")
                return _rpc({"op": "call", "name": name, "kwargs": kwargs})
            return _call

    tools = _Tools()

    def fanout(fn, items, limit=8):
        results = []
        for item in list(items)[: max(1, int(limit))]:
            results.append(fn(item))
        return results

    def vote(candidates):
        counts = {}
        for c in candidates:
            key = str(c).strip()
            counts[key] = counts.get(key, 0) + 1
        if not counts:
            return None
        return max(counts.items(), key=lambda kv: kv[1])[0]

    def _print(*args, sep=" ", end="\\n", file=None, flush=False):
        _rpc({"op": "print", "text": sep.join(str(a) for a in args) + end}, wait=False)

    builtins.print = _print
    _script = _in.readline()
    _source = json.loads(_script)
    _globals = {"__name__": "__main__", "tools": tools, "fanout": fanout, "vote": vote, "ToolDenied": ToolDenied}
    try:
        exec(compile(_source, "<code-mode>", "exec"), _globals)
    except ToolDenied as exc:
        _rpc({"op": "error", "text": f"ToolDenied: {exc}"}, wait=False)
    except Exception:
        _rpc({"op": "error", "text": traceback.format_exc(limit=4)}, wait=False)
    _rpc({"op": "done"}, wait=False)
    '''
)


def _text_of(result: Any) -> str:
    if isinstance(result, ToolMessage):
        content = result.content if isinstance(result.content, str) else json.dumps(result.content, default=str)
        return content if result.status != "error" else f"ERROR: {content}"
    update = getattr(result, "update", None)
    if isinstance(update, dict):
        messages = update.get("messages") or []
        for message in reversed(messages):
            if isinstance(message, ToolMessage):
                return _text_of(message)
        return json.dumps({k: v for k, v in update.items() if k != "messages"}, default=str)[:_RESULT_CHARS]
    return str(result)


def _hitl_gated_names(middleware: Sequence[AgentMiddleware]) -> set[str]:
    names: set[str] = set()
    for mw in middleware:
        if type(mw).__name__ == "HumanInTheLoopMiddleware":
            gated = getattr(mw, "interrupt_on", {}) or {}
            names.update(name for name, cfg in gated.items() if cfg)
    return names


class CodeModeMiddleware(AgentMiddleware[Any, Any, Any]):
    """The `run_code` tool and the governed bridge behind it.

    Args:
        timeout: Wall-clock limit for one script (seconds).
        allowed_tools: Names the script may call; `None` = every bound tool.
        cost_ledger: Session ledger whose caps gate `tools.task(...)` and `tools.web_search(...)`.
        sandbox: When given and a launcher exists, the child runs inside it.
        max_calls: Tool calls one script may make.
        python: Interpreter to run scripts with (default: this one).
        mcp_tool_names: Names of connected MCP tools; when non-empty an
            `execute_mcp_script` tool narrows the namespace to them.
    """

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        allowed_tools: Sequence[str] | None = None,
        cost_ledger: CostLedger | None = None,
        sandbox: LocalSandbox | None = None,
        max_calls: int = DEFAULT_MAX_CALLS,
        python: str | None = None,
        mcp_tool_names: Sequence[str] = (),
    ) -> None:
        """See the class docstring."""
        super().__init__()
        self._timeout = max(1.0, float(timeout))
        self._allowed = set(allowed_tools) if allowed_tools is not None else None
        self._cost_ledger = cost_ledger
        self._sandbox = sandbox
        self._max_calls = max(1, int(max_calls))
        self._python = python or sys.executable
        self._mcp_names = set(mcp_tool_names)
        self._tools_by_name: dict[str, BaseTool] = {}
        self._chain: Callable[[ToolCallRequest, Callable[[ToolCallRequest], Any]], Any] | None = None
        self._hitl_gated: set[str] = set()
        self._bound = False
        self.tools = self._build_tools()

    # -- binding ---------------------------------------------------------------

    def bind(self, tools: Sequence[Any], middleware: Sequence[AgentMiddleware]) -> None:
        """Learn the agent's tools and compose the governance chain (called once the agent is assembled).

        Args:
            tools: Every tool the agent has (explicit ones and middleware-provided ones).
            middleware: The agent's middleware list; each `wrap_tool_call` override
                except HITL's (and this middleware's own) becomes a layer of the
                nested chain, outermost first, in the same order the agent uses.
        """
        self._tools_by_name = {t.name: t for t in (tools or ()) if isinstance(t, BaseTool)}
        for mw in middleware:
            for tool in getattr(mw, "tools", None) or []:
                if isinstance(tool, BaseTool):
                    self._tools_by_name.setdefault(tool.name, tool)
        self._hitl_gated = _hitl_gated_names(middleware)
        wrappers = [
            mw.wrap_tool_call
            for mw in middleware
            if mw is not self and type(mw).__name__ != "HumanInTheLoopMiddleware" and type(mw).wrap_tool_call is not AgentMiddleware.wrap_tool_call
        ]
        self._chain = _compose(wrappers)
        self._bound = True
        self.tools = self._build_tools()

    @property
    def tool_names(self) -> list[str]:
        """Names a script may call."""
        names = sorted(self._tools_by_name)
        if self._allowed is not None:
            names = [n for n in names if n in self._allowed]
        return [n for n in names if n not in ("run_code", "execute_mcp_script")]

    # -- the tools -------------------------------------------------------------

    def _build_tools(self) -> list[BaseTool]:
        description = RUN_CODE_DESCRIPTION.format(tool_names=", ".join(self.tool_names) or "(bound at start)")

        def run_code(script: str, runtime: ToolRuntime) -> str:
            """Run a Python script against the governed `tools` namespace."""
            return self.execute(script, runtime=runtime, namespace=None)

        tools: list[BaseTool] = [StructuredTool.from_function(name="run_code", func=run_code, description=description)]
        if self._mcp_names:
            mcp_description = (
                "Run a Python script whose `tools` namespace holds only the connected MCP tools "
                f"({', '.join(sorted(self._mcp_names))}); otherwise identical to run_code."
            )

            def execute_mcp_script(script: str, runtime: ToolRuntime) -> str:
                """Run a script limited to the connected MCP tools."""
                return self.execute(script, runtime=runtime, namespace=self._mcp_names)

            tools.append(StructuredTool.from_function(name="execute_mcp_script", func=execute_mcp_script, description=mcp_description))
        return tools

    # -- the bridge ------------------------------------------------------------

    def _refusal(self, name: str, namespace: set[str] | None) -> str | None:
        if namespace is not None and name not in namespace:
            return f"tool {name!r} is outside this script's namespace"
        if self._allowed is not None and name not in self._allowed:
            return f"tool {name!r} is not allowed in code mode"
        if name in ("run_code", "execute_mcp_script"):
            return "code mode cannot nest itself"
        if name not in self._tools_by_name:
            return f"unknown tool {name!r}; available: {', '.join(self.tool_names)}"
        if name in self._hitl_gated:
            return f"tool {name!r} needs human approval; call it directly, not from code mode"
        ledger = self._cost_ledger
        if ledger is not None:
            if name == "task":
                decision = ledger.register_subagent_spawn()
                if not decision.allowed:
                    return f"spawn refused: {decision.reason}"
            elif name == "web_search":
                decision = ledger.register_web_search()
                if not decision.allowed:
                    return f"web search refused: {decision.reason}"
        return None

    def invoke_tool(
        self, name: str, kwargs: dict[str, Any], *, runtime: ToolRuntime | None, namespace: set[str] | None = None, call_id: str = ""
    ) -> str:
        """Run one governed tool call on behalf of a script (raises nothing; errors come back as text)."""
        refusal = self._refusal(name, namespace)
        if refusal is not None:
            return f"ERROR: {refusal}"
        tool = self._tools_by_name[name]
        request = ToolCallRequest(
            tool_call={"name": name, "args": dict(kwargs), "id": call_id or f"code-{name}", "type": "tool_call"},
            tool=tool,
            state=dict(getattr(runtime, "state", None) or {}),
            runtime=runtime,  # type: ignore[arg-type]
        )

        def execute(req: ToolCallRequest) -> ToolMessage:
            args = dict(req.tool_call.get("args") or {})
            try:
                raw = req.tool.invoke(args) if req.tool is not None else tool.invoke(args)
            except Exception as exc:
                return ToolMessage(content=f"{exc.__class__.__name__}: {exc}", tool_call_id=req.tool_call["id"], name=name, status="error")
            if isinstance(raw, ToolMessage):
                return raw
            return ToolMessage(content=raw if isinstance(raw, str) else json.dumps(raw, default=str), tool_call_id=req.tool_call["id"], name=name)

        try:
            result = self._chain(request, execute) if self._chain is not None else execute(request)
        except Exception as exc:
            logger.debug("code mode: nested call %s failed", name, exc_info=True)
            return f"ERROR: {exc.__class__.__name__}: {exc}"
        return _text_of(result)[:_RESULT_CHARS]

    def _command(self, workdir: Path) -> list[str]:
        argv = [self._python, "-I", "-u", "-c", _CHILD_RUNNER]
        if self._sandbox is not None:
            from bog_agents.sandbox.local_sandbox import sandbox_launcher_available, wrap_command_with_sandbox

            if sandbox_launcher_available():
                return wrap_command_with_sandbox(shlex.join(argv), self._sandbox)
            logger.warning("code mode: no OS sandbox launcher on this platform; the script runs unsandboxed in %s", workdir)
        return argv

    def execute(self, script: str, *, runtime: ToolRuntime | None, namespace: set[str] | None) -> str:
        """Run `script` in a child interpreter, serving its tool calls; returns the printed output."""
        if not self._bound:
            return "ERROR: code mode is not bound to an agent yet"
        if not script.strip():
            return "ERROR: empty script"
        output: list[str] = []
        errors: list[str] = []
        calls = 0
        workdir = Path(tempfile.mkdtemp(prefix="bog_code_mode_"))
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME", "USERPROFILE", "LANG")}
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            proc = subprocess.Popen(
                self._command(workdir),
                cwd=str(workdir),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
        except OSError as exc:
            return f"ERROR: could not start the interpreter: {exc}"
        assert proc.stdin is not None
        assert proc.stdout is not None
        import threading

        timer = threading.Timer(self._timeout, proc.kill)
        timer.start()
        try:
            proc.stdin.write(json.dumps(script) + "\n")
            proc.stdin.flush()
            while True:
                line = proc.stdout.readline()
                if not line:
                    break
                if not line.startswith(PROTOCOL_PREFIX):
                    output.append(line)
                    continue
                try:
                    message = json.loads(line[len(PROTOCOL_PREFIX) :])
                except ValueError:
                    output.append(line)
                    continue
                op = message.get("op")
                if op == "print":
                    output.append(str(message.get("text", "")))
                elif op == "call":
                    calls += 1
                    if calls > self._max_calls:
                        reply = {"error": f"code mode call budget exhausted ({self._max_calls})"}
                    else:
                        text = self.invoke_tool(
                            str(message.get("name", "")),
                            dict(message.get("kwargs") or {}),
                            runtime=runtime,
                            namespace=namespace,
                            call_id=f"code-{calls}",
                        )
                        reply = {"error": text[len("ERROR: ") :]} if text.startswith("ERROR: ") else {"result": text}
                    proc.stdin.write(json.dumps(reply) + "\n")
                    proc.stdin.flush()
                elif op == "error":
                    errors.append(str(message.get("text", "")))
                elif op == "done":
                    break
            proc.stdin.close()
            proc.wait(timeout=5)
        except (BrokenPipeError, OSError, subprocess.TimeoutExpired) as exc:
            errors.append(f"interpreter channel failed: {exc.__class__.__name__}")
        finally:
            timed_out = not timer.is_alive()
            timer.cancel()
            if proc.poll() is None:
                proc.kill()
            stderr = ""
            try:
                stderr = (proc.stderr.read() if proc.stderr else "")[:2000]
            except Exception:
                stderr = ""
            import shutil

            shutil.rmtree(workdir, ignore_errors=True)
        text = "".join(output)[:_OUTPUT_CHARS]
        if timed_out:
            errors.append(f"script exceeded {self._timeout:.0f}s and was killed")
        if errors:
            text += ("\n" if text else "") + "ERROR: " + "\n".join(errors)
        elif stderr.strip() and proc.returncode:
            text += ("\n" if text else "") + f"ERROR: interpreter exited {proc.returncode}: {stderr.strip()}"
        return text or "(no output)"


def _compose(wrappers: Sequence[Callable[..., Any]]) -> Callable[[ToolCallRequest, Callable[[ToolCallRequest], Any]], Any] | None:
    """Compose `wrap_tool_call` layers, first = outermost (mirrors langchain's tool node)."""
    if not wrappers:
        return None
    result = wrappers[-1]
    for wrapper in reversed(wrappers[:-1]):

        def composed(request: ToolCallRequest, execute: Callable[[ToolCallRequest], Any], *, _outer: Any = wrapper, _inner: Any = result) -> Any:
            return _outer(request, lambda req: _inner(req, execute))

        result = composed
    return result


__all__ = ["DEFAULT_MAX_CALLS", "DEFAULT_TIMEOUT", "RUN_CODE_DESCRIPTION", "CodeModeMiddleware"]
