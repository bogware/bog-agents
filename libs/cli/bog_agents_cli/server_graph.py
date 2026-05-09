"""Server-side graph entry point for `langgraph dev`.

This module is referenced by the generated `langgraph.json` and exposes the CLI
agent graph as a module-level variable that the LangGraph server can load
and serve.

The graph is created at module import time via `make_graph()`, which reads
configuration from `ServerConfig.from_env()` — the same dataclass the CLI uses
to *write* the configuration via `ServerConfig.to_env()`. This shared schema
ensures the two sides stay in sync.
"""

from __future__ import annotations

import atexit
import faulthandler
import logging
import os
import sys
import threading
import time
import traceback
from typing import Any

from bog_agents_cli._server_config import ServerConfig
from bog_agents_cli.project_utils import ProjectContext, get_server_project_context

logger = logging.getLogger(__name__)


def _dump_asyncio_tasks(stream: Any) -> None:  # noqa: ANN401  # any text-stream-like
    """Dump every live ``asyncio.Task`` and its suspended frame.

    This is the diagnostic that ``faulthandler.dump_traceback`` cannot
    produce. ``faulthandler`` only sees Python THREAD frames, so a
    coroutine that is suspended on ``await foo()`` is invisible — its
    state lives inside an ``asyncio.Task`` object, not on any thread's
    stack. To find them we walk ``gc.get_objects()`` (which includes
    every live Python object including ``Task`` instances on every
    event loop in any thread) and call ``task.get_stack()`` /
    ``task.get_coro()``.

    ``gc.get_objects()`` is slow (full heap walk) but we only invoke
    it on a confirmed stall, so the cost is irrelevant. It's safe to
    call from any thread without holding any locks.
    """
    import asyncio
    import gc

    try:
        tasks = [obj for obj in gc.get_objects() if isinstance(obj, asyncio.Task)]
    except Exception as exc:
        print(f"\n[stall-watchdog] gc.get_objects() failed: {exc}", file=stream)
        return

    print(
        f"\n=== asyncio.Task dump: {len(tasks)} live tasks ===",
        file=stream,
    )
    for i, task in enumerate(tasks):
        try:
            coro = task.get_coro()
            qualname = getattr(coro, "__qualname__", repr(coro))
            done = task.done()
            cancelled = task.cancelled() if done else False
            print(
                f"\n--- Task #{i}: {qualname} "
                f"(done={done}, cancelled={cancelled}) ---",
                file=stream,
            )
            # ``get_stack()`` returns frames where the task is currently
            # suspended (or the running frame if it's not suspended).
            # On a wedged ``await`` the deepest frame is exactly the
            # line we need. The list goes outermost → innermost, so we
            # print in reverse to match a normal "most recent call last"
            # traceback.
            frames = task.get_stack(limit=30)
            if not frames:
                print("  (no frames — task complete or never started)", file=stream)
                continue
            for frame in frames:
                filename = frame.f_code.co_filename
                lineno = frame.f_lineno
                funcname = frame.f_code.co_name
                print(
                    f'  File "{filename}", line {lineno}, in {funcname}',
                    file=stream,
                )
        except Exception as exc:
            print(f"  (failed to format task: {exc})", file=stream)
    stream.flush()


def _install_stall_diagnostics() -> None:
    """Install faulthandler + a periodic activity heartbeat.

    Two layers of diagnostics that activate immediately at module
    import (i.e. when ``langgraph dev`` first loads ``server_graph``):

    1. ``faulthandler.enable()`` — installs handlers so a hard crash
       (segfault, abort) dumps Python stacks to stderr (which is the
       server log file). Free and harmless when nothing crashes.

    2. A daemon thread that, every ``BOG_AGENTS_STALL_DUMP_SECS`` (default
       45) seconds, dumps **all live thread stacks** to the server log
       *if* no log activity has been observed in that window. This is
       the change that actually surfaces a stall: when the agent gets
       wedged inside a middleware ``wrap_model_call`` and produces no
       further langgraph events, the heartbeat fires and the user
       (and us) get the exact Python frame that's blocked.

    Disable by setting ``BOG_AGENTS_STALL_DUMP_SECS=0``. The threshold
    can be raised on slow networks where 45s of model latency is normal.
    """
    try:
        faulthandler.enable()
    except (RuntimeError, ValueError):
        # Some embedding scenarios reject faulthandler.enable(); the
        # heartbeat below still works so we keep going.
        logger.debug("faulthandler.enable() failed", exc_info=True)

    raw = os.environ.get("BOG_AGENTS_STALL_DUMP_SECS", "45").strip()
    try:
        interval = float(raw)
    except ValueError:
        interval = 45.0
    if interval <= 0:
        return

    # Activity tracking: install a logging Handler that updates a
    # shared timestamp on every emitted record — but ONLY for records
    # from loggers that signal real graph progress. Without this
    # filter, periodic background noise (``langgraph_runtime_inmem``
    # emits "Queue stats" / "Worker stats" every ~60s) keeps resetting
    # the timestamp, so a wedged run that stays silent except for that
    # background noise would NEVER trigger the watchdog. The
    # ``LocalContextMiddleware -> [silence] -> killed`` symptom we just
    # chased is exactly this case.
    #
    # The allow-rule is "ignore loggers we know are periodic
    # noise". Anything else (bog_agents_cli, httpx, langgraph_api.worker,
    # langchain, etc.) is treated as a real-progress signal.
    noise_logger_prefixes = (
        "langgraph_runtime_inmem.queue",   # "Queue stats", "Worker stats"
        "langgraph_runtime_inmem._persistence",  # flush loop
        "langgraph_api.cron_scheduler",     # cron tick
        "langgraph_api.metadata",           # metadata refresh loop
    )

    last_activity = [time.monotonic()]
    last_dump = [0.0]

    class _ActivityProbe(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:  # noqa: PLR6301
            name = record.name or ""
            for prefix in noise_logger_prefixes:
                if name.startswith(prefix):
                    return
            last_activity[0] = time.monotonic()

    probe = _ActivityProbe(level=logging.DEBUG)
    logging.getLogger().addHandler(probe)

    # Sleep granularity. We re-check every ``interval/3`` seconds so a
    # stall is detected within ~1.3x the configured interval at worst.
    poll = max(1.0, interval / 3.0)

    def _heartbeat_loop() -> None:
        while True:
            try:
                time.sleep(poll)
            except BaseException:  # noqa: S112  # daemon must never die on signal during shutdown
                continue
            now = time.monotonic()
            quiet_for = now - last_activity[0]
            since_last_dump = now - last_dump[0]
            if quiet_for < interval:
                continue
            # Throttle: don't dump more often than every ``interval``
            # seconds even if the stall persists.
            if since_last_dump < interval:
                continue
            try:
                logger.warning(
                    "stall-watchdog: no log activity for %.0fs (>= %.0fs threshold); "
                    "dumping all thread stacks AND asyncio tasks for diagnosis",
                    quiet_for,
                    interval,
                )
                faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
                _dump_asyncio_tasks(sys.stderr)
            except Exception:  # diagnostics must never crash the server
                logger.exception("stall-watchdog: dump_traceback failed")
            last_dump[0] = now

    t = threading.Thread(
        target=_heartbeat_loop,
        name="bog-agents-stall-watchdog",
        daemon=True,
    )
    t.start()
    logger.info(
        "Stall watchdog armed (interval=%.0fs). Set BOG_AGENTS_STALL_DUMP_SECS=0 "
        "to disable.",
        interval,
    )


# Install diagnostics at module import — before the graph is built, so
# even a hang during ``make_graph()`` itself produces a stack dump.
_install_stall_diagnostics()

# Module-level sandbox state kept alive for the server process lifetime.
_sandbox_cm: Any = None
_sandbox_backend: Any = None


def _build_tools(
    config: ServerConfig,
    project_context: ProjectContext | None,
) -> tuple[list[Any], list[Any] | None]:
    """Assemble the tool list based on server config.

    Loads built-in tools (conditionally including web search when Tavily is
    available) and MCP tools when enabled.

    MCP discovery runs synchronously via `asyncio.run` because this function is
    called during module-level graph construction (before the server's async
    event loop is available).

    **MCP failures are non-fatal.** A single broken server config used to
    crash the entire LangGraph dev server, leaving the user unable to run
    bog-agents at all and unable to fix the bad config from inside the
    agent (because the agent never starts). We now log the failure
    loudly, store it on the module so the CLI can surface it to the
    user, and bring up the agent with empty MCP tools. The user can
    then run `/mcp list` and `/mcp remove <name>` to fix the bad
    config.

    Args:
        config: Deserialized server configuration.
        project_context: Resolved project context for MCP discovery.

    Returns:
        Tuple of `(tools, mcp_server_info)`.
    """
    from bog_agents_cli.config import settings
    from bog_agents_cli.tools import fetch_url, http_request, web_search

    tools: list[Any] = [http_request, fetch_url]
    if settings.has_tavily:
        tools.append(web_search)

    mcp_server_info: list[Any] | None = None
    if not config.no_mcp:
        import asyncio

        from bog_agents_cli.mcp_tools import resolve_and_load_mcp_tools

        try:
            mcp_tools, _, mcp_server_info = asyncio.run(
                resolve_and_load_mcp_tools(
                    explicit_config_path=config.mcp_config_path,
                    no_mcp=config.no_mcp,
                    trust_project_mcp=config.trust_project_mcp,
                    project_context=project_context,
                )
            )
            # ``resolve_and_load_mcp_tools`` returns connection-bound
            # tools (per-call sessions), so there is no session manager
            # whose lifetime needs anchoring here. Each tool call spawns
            # a fresh stdio subprocess from the live loop, which is the
            # only design that survives ``asyncio.run`` closing the
            # build-time loop. See ``mcp_tools._load_tools_from_config``
            # for the rationale.
        except FileNotFoundError as exc:
            # Explicit ``--mcp-config <path>`` pointed at a missing file.
            # Used to ``raise`` and crash the server. Now logged and stored
            # so the CLI can show the user a meaningful error in chat.
            logger.error(  # noqa: TRY400  # context already in record_mcp_load_failure
                "MCP config file not found: %s", config.mcp_config_path
            )
            _record_mcp_load_failure(
                f"MCP config file not found: {exc}. "
                f"Fix or remove the path and restart, or use `/mcp` "
                f"from inside the agent to manage configurations."
            )
            mcp_tools, mcp_server_info = [], []
        except RuntimeError as exc:
            # ``_validate_server_config`` rejected an entry, or a stdio
            # server failed to spawn. The agent stays up; the user
            # reads the error and uses ``/mcp remove <name>``.
            logger.error(  # noqa: TRY400  # context already in record_mcp_load_failure
                "Failed to load MCP tools (config: %s): %s",
                config.mcp_config_path,
                exc,
            )
            _record_mcp_load_failure(
                f"Failed to load MCP tools: {exc}. "
                f"Use `/mcp list` to inspect configured servers and "
                f"`/mcp remove <name>` to drop a broken one."
            )
            mcp_tools, mcp_server_info = [], []
        except (
            Exception
        ) as exc:  # last-resort fallback for any unexpected error during MCP setup
            # Belt-and-suspenders: any other surprise (network blip on
            # an HTTP MCP server during the spawn handshake, an MCP
            # client library bug, etc.) used to take down the whole
            # server. Same fallback path: log, record, continue without
            # MCP.
            logger.exception(
                "Unexpected error loading MCP tools (config: %s)",
                config.mcp_config_path,
            )
            _record_mcp_load_failure(
                f"Unexpected error loading MCP tools: "
                f"{type(exc).__name__}: {exc}. "
                f"Agent started without MCP. See logs for traceback."
            )
            mcp_tools, mcp_server_info = [], []

        tools.extend(mcp_tools)
        if mcp_tools:
            logger.info("Loaded %d MCP tool(s)", len(mcp_tools))

    return tools, mcp_server_info


# Module-level slot for the CLI to read after server startup. When MCP
# loading fails, the message is stored here so the CLI process can
# surface it to the user via ``_mount_message`` instead of dying
# silently. The value is also written to ``DA_SERVER_MCP_ERROR`` for
# the rare case where the CLI and server live in different processes
# and need to communicate via env var.
_MCP_LOAD_FAILURE: str | None = None


def _record_mcp_load_failure(message: str) -> None:
    """Persist an MCP load failure for the CLI to surface."""
    global _MCP_LOAD_FAILURE  # noqa: PLW0603
    _MCP_LOAD_FAILURE = message
    # Also write to the env var so out-of-process readers can see it.
    import os as _os

    _os.environ["DA_SERVER_MCP_ERROR"] = message


def make_graph() -> Any:  # noqa: ANN401
    """Create the CLI agent graph from environment-based configuration.

    Reads `DA_SERVER_*` env vars via `ServerConfig.from_env()` (the inverse of
    `ServerConfig.to_env()` used by the CLI process), resolves a model,
    assembles tools, and compiles the agent graph.

    Returns:
        Compiled LangGraph agent graph.
    """
    config = ServerConfig.from_env()
    project_context = get_server_project_context()

    from bog_agents_cli.agent import create_cli_agent
    from bog_agents_cli.config import (
        create_model_with_fallback as create_model,
        settings,
    )

    if project_context is not None:
        settings.reload_from_environment(start_path=project_context.user_cwd)

    result = create_model(config.model, extra_kwargs=config.model_params)
    result.apply_to_settings()

    tools, mcp_server_info = _build_tools(config, project_context)

    # Create sandbox backend if a sandbox provider is configured.
    # The context manager is held open at module level and cleaned up via
    # atexit so the sandbox lives for the entire server process lifetime.
    global _sandbox_cm, _sandbox_backend  # noqa: PLW0603
    sandbox_backend = None
    if config.sandbox_type:
        from bog_agents_cli.integrations.sandbox_factory import create_sandbox

        try:
            _sandbox_cm = create_sandbox(
                config.sandbox_type,
                sandbox_id=config.sandbox_id,
                setup_script_path=config.sandbox_setup,
            )
            _sandbox_backend = _sandbox_cm.__enter__()  # noqa: PLC2801  # Context manager kept open for server process lifetime
            sandbox_backend = _sandbox_backend

            def _cleanup_sandbox() -> None:
                if _sandbox_cm is not None:
                    _sandbox_cm.__exit__(None, None, None)

            atexit.register(_cleanup_sandbox)
        except ImportError:
            logger.exception(
                "Sandbox provider '%s' is not installed", config.sandbox_type
            )
            pip_hint = {
                "modal": "pip install 'bog-agents-cli[modal]'",
                "daytona": "pip install 'bog-agents-cli[daytona]'",
                "runloop": "pip install 'bog-agents-cli[runloop]'",
            }.get(
                config.sandbox_type,
                f"pip install 'bog-agents-cli[{config.sandbox_type}]'",
            )
            print(  # noqa: T201  # stderr fallback — logger may not reach parent process
                f"Sandbox provider '{config.sandbox_type}' is not installed. "
                f"Install it with: {pip_hint}",
                file=sys.stderr,
            )
            sys.exit(1)
        except NotImplementedError:
            logger.exception("Sandbox type '%s' is not supported", config.sandbox_type)
            print(  # noqa: T201  # stderr fallback — logger may not reach parent process
                f"Sandbox type '{config.sandbox_type}' is not supported",
                file=sys.stderr,
            )
            sys.exit(1)
        except Exception as exc:
            logger.exception("Sandbox creation failed for '%s'", config.sandbox_type)
            print(  # noqa: T201  # stderr fallback — logger may not reach parent process
                f"Sandbox creation failed for '{config.sandbox_type}': {exc}",
                file=sys.stderr,
            )
            sys.exit(1)

    agent, _ = create_cli_agent(
        model=result.model,
        assistant_id=config.assistant_id,
        tools=tools,
        sandbox=sandbox_backend,
        sandbox_type=config.sandbox_type,
        system_prompt=config.system_prompt,
        interactive=config.interactive,
        auto_approve=config.auto_approve,
        enable_memory=config.enable_memory,
        enable_skills=config.enable_skills,
        enable_shell=config.enable_shell,
        mcp_server_info=mcp_server_info,
        cwd=project_context.user_cwd if project_context is not None else config.cwd,
        project_context=project_context,
    )
    return agent


try:
    graph = make_graph()
except Exception as exc:
    logger.critical("Failed to initialize server graph", exc_info=True)
    # Print a *prominent* banner to stderr so the user can see the real
    # cause without scrolling past LangGraph's own boilerplate. The
    # outer Server failed to start: code 3 message in the parent
    # process truncates a lot of context, so we emit our own
    # well-marked block here.
    msg = (
        "\n"
        "================================================================\n"
        "BOG AGENTS SERVER FAILED TO INITIALIZE\n"
        "================================================================\n"
        f"Cause:    {type(exc).__name__}: {exc}\n"
        "----------------------------------------------------------------\n"
        f"{traceback.format_exc()}"
        "----------------------------------------------------------------\n"
        "Common causes and recovery:\n"
        "  - Bad MCP config: edit ~/.bog-agents/.mcp.json or run\n"
        "    `bog-agents --no-mcp` to start without MCP, then `/mcp remove <name>`.\n"
        "  - Missing API key: set ANTHROPIC_API_KEY (or run `/settings`).\n"
        "  - Sandbox provider not installed: pip install the missing extra,\n"
        "    or run `bog-agents --sandbox-type none`.\n"
        "================================================================\n"
    )
    print(msg, file=sys.stderr)  # noqa: T201  # stderr fallback — logger may not reach parent process
    sys.exit(1)
