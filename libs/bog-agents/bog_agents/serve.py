"""HTTP API server for bog-agents.

Exposes a running agent as a REST + SSE API server. Enables production
deployments via ``bog-agents --serve`` or programmatic use in Docker containers,
microservices, and orchestration systems.

Endpoints
---------
- ``POST /invoke``                    -- Run agent synchronously, return final result
- ``POST /stream``                    -- Stream agent responses via SSE
- ``GET  /health``                    -- Health check (no auth required)
- ``GET  /info``                      -- Agent info and capabilities
- ``POST /threads``                   -- Create a new thread
- ``GET  /threads``                   -- List threads
- ``POST /threads/{id}/messages``     -- Send a message to a thread
- ``GET  /threads/{id}/history``      -- Get thread message history
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ServerConfig:
    """Configuration for the bog-agents HTTP server.

    Attributes:
        cors_origins: Browser origins allowed to read responses cross-origin.
            Defaults to ``[]`` (no cross-origin access) — SDK-CORE-1: the old
            ``["*"]`` default let any drive-by web page drive the agent and read
            its replies. Set explicit origins to opt in.
        enable_streaming: When False, ``POST /stream`` returns 501 instead of
            silently streaming anyway (the flag used to be inert).
        stream_queue_maxsize: Bound on the per-stream producer→consumer buffer.
            Decouples agent production from client consumption so a slow reader
            can't pin a concurrency slot indefinitely (SDK-CORE-5).
    """

    host: str = "127.0.0.1"
    port: int = 8420
    cors_origins: list[str] = field(default_factory=list)
    api_key: str | None = None
    max_concurrent_requests: int = 10
    request_timeout: float = 300.0
    enable_streaming: bool = True
    stream_queue_maxsize: int = 256
    max_tracked_threads: int = 1000


@dataclass
class ThreadState:
    """In-memory state for a conversation thread."""

    thread_id: str
    created_at: float = field(default_factory=time.time)
    messages: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class AgentServer:
    """HTTP API server wrapping a bog-agents agent.

    Example:
        ```python
        from bog_agents import create_agent
        from bog_agents.serve import AgentServer, ServerConfig

        agent = create_agent(model="anthropic:claude-sonnet-4-6")
        server = AgentServer(agent, config=ServerConfig(port=8420))
        server.run()
        ```
    """

    def __init__(
        self,
        agent: Any,
        *,
        config: ServerConfig | None = None,
    ) -> None:
        """Initialize the agent server.

        If no API key is set, checks the ``BOG_AGENTS_SERVE_API_KEY``
        environment variable. When binding to a non-localhost address
        without an API key, a random key is auto-generated and logged.

        Args:
            agent: A compiled LangGraph agent from ``create_agent()``.
            config: Server configuration.
        """
        self.agent = agent
        self.config = config or ServerConfig()
        # Ordered (insertion/recency) map so we can LRU-evict when the
        # tracked-thread ceiling is exceeded, bounding memory on a
        # long-lived server.
        self._threads: OrderedDict[str, ThreadState] = OrderedDict()
        self._request_count = 0
        self._start_time = time.time()
        # Created lazily inside the running event loop so the server can be
        # constructed outside of one (binding a Semaphore to a loop at
        # __init__ time can raise / cross loops under test).
        self._request_semaphore: asyncio.Semaphore | None = None

        # Auth: env var -> config -> auto-generate for non-localhost
        if self.config.api_key is None:
            env_key = os.environ.get("BOG_AGENTS_SERVE_API_KEY")
            if env_key:
                self.config.api_key = env_key

        is_localhost = self.config.host in ("127.0.0.1", "localhost", "::1")
        if self.config.api_key is None and not is_localhost:
            self.config.api_key = str(uuid.uuid4())
            logger.warning(
                "No API key set for non-localhost server. Auto-generated key: %s",
                self.config.api_key,
            )
            logger.warning(
                "Set BOG_AGENTS_SERVE_API_KEY or pass api_key in ServerConfig to use a stable key.",
            )
        elif self.config.api_key is None and is_localhost:
            logger.info(
                "Running on localhost without API key. Set BOG_AGENTS_SERVE_API_KEY for authenticated access.",
            )

        # SDK-CORE-1: wildcard CORS with no key is the drive-by-web-page hole —
        # any site the user visits could script the agent and read its replies.
        # The default is now [] (no cross-origin), so this only fires when a
        # caller explicitly opens it up without also requiring a key.
        if "*" in self.config.cors_origins and self.config.api_key is None:
            logger.warning(
                "cors_origins allows '*' with no API key: any web page can drive this "
                "agent and read its responses. Set an api_key or restrict cors_origins.",
            )

    def _get_or_create_thread(self, thread_id: str | None = None) -> ThreadState:
        """Get an existing thread or create a new one.

        Args:
            thread_id: Optional thread ID. Creates new if ``None``.

        Returns:
            ``ThreadState`` for the thread.
        """
        if thread_id is None:
            thread_id = str(uuid.uuid4())
        if thread_id not in self._threads:
            self._threads[thread_id] = ThreadState(thread_id=thread_id)
        else:
            # Mark as most-recently-used so it survives eviction.
            self._threads.move_to_end(thread_id)
        self._evict_stale_threads()
        return self._threads[thread_id]

    def _evict_stale_threads(self) -> None:
        """Bound the in-memory thread map by evicting least-recently-used entries.

        Keeps at most ``config.max_tracked_threads`` threads so a long-lived
        server cannot grow ``_threads`` without limit (OOM). The ceiling is
        clamped to a minimum of 1 so the just-created/just-touched thread (kept
        most-recent) is never evicted out from under the caller.
        """
        ceiling = max(1, self.config.max_tracked_threads)
        while len(self._threads) > ceiling:
            # popitem(last=False) removes the oldest (least-recently-used) entry.
            self._threads.popitem(last=False)

    def _get_request_semaphore(self) -> asyncio.Semaphore:
        """Return the per-request concurrency semaphore, creating it lazily.

        The semaphore is bound to the currently running event loop on first
        use so the server can be constructed outside of an event loop.

        Returns:
            The bounded ``asyncio.Semaphore`` gating concurrent requests.
        """
        if self._request_semaphore is None:
            limit = max(1, self.config.max_concurrent_requests)
            self._request_semaphore = asyncio.Semaphore(limit)
        return self._request_semaphore

    def _check_api_key(self, provided_key: str | None) -> bool:
        """Validate the API key if one is configured.

        Uses a constant-time comparison so a matching-prefix length cannot be
        recovered via response timing.

        Args:
            provided_key: The key provided in the request.

        Returns:
            ``True`` if valid or no key required.
        """
        if self.config.api_key is None:
            return True
        return secrets.compare_digest(
            (provided_key or "").encode("utf-8"),
            self.config.api_key.encode("utf-8"),
        )

    def _has_checkpointer(self) -> bool:
        """Whether the wrapped agent persists conversation state itself.

        A compiled LangGraph agent exposes a ``checkpointer`` attribute when one
        was configured. When present, thread continuity is the graph's job and
        we send only the new turn; when absent, the server must replay its own
        tracked history (see ``_build_input`` / SDK-CORE-4).
        """
        return getattr(self.agent, "checkpointer", None) is not None

    def _build_input(self, thread: ThreadState, message: str) -> dict[str, Any]:
        """Build the ``messages`` input for an agent invocation.

        SDK-CORE-4: without a checkpointer, sending only the newest message made
        every turn amnesiac — turn 2 silently lost all prior context while
        ``/history`` implied continuity. When the agent has no checkpointer we
        replay the thread's full tracked history (which already includes the
        just-appended user message); when it has one we send only the new turn
        and let the graph resume from its own state.

        Args:
            thread: The conversation thread (its ``messages`` already include
                the current user message).
            message: The current user message.

        Returns:
            The ``input_data`` dict to pass to the agent.
        """
        if self._has_checkpointer():
            return {"messages": [{"role": "user", "content": message}]}
        # Replay the full conversation so a checkpointer-less agent still sees
        # the prior turns. Copy so downstream mutation can't corrupt our record.
        return {"messages": [dict(m) for m in thread.messages]}

    async def invoke(
        self,
        message: str,
        *,
        thread_id: str | None = None,
        metadata: dict[str, Any] | None = None,  # noqa: ARG002
    ) -> dict[str, Any]:
        """Invoke the agent with a message and return the result.

        Args:
            message: User message.
            thread_id: Optional thread for conversation continuity.
            metadata: Optional metadata.

        Returns:
            Dict with ``thread_id``, ``response``, ``metadata``.
        """
        thread = self._get_or_create_thread(thread_id)
        thread.messages.append({"role": "user", "content": message})
        self._request_count += 1

        config = {"configurable": {"thread_id": thread.thread_id}}
        input_data = self._build_input(thread, message)

        # Gate concurrency and bound each invocation by request_timeout so a
        # hung/slow agent cannot block the server indefinitely.
        async with self._get_request_semaphore():
            try:
                result = await asyncio.wait_for(
                    self.agent.ainvoke(input_data, config=config),
                    timeout=self.config.request_timeout,
                )
            except TimeoutError:
                logger.warning(
                    "Agent invocation timed out after %ss (thread=%s)",
                    self.config.request_timeout,
                    thread.thread_id,
                )
                return {
                    "thread_id": thread.thread_id,
                    "error": "Gateway timeout: agent did not respond in time",
                    "status_code": 504,
                    "metadata": {},
                }
            except Exception:
                logger.exception("Agent invocation failed")
                return {
                    "thread_id": thread.thread_id,
                    "error": "Internal server error",
                    "metadata": {},
                }

        response_messages = result.get("messages", [])
        assistant_msg = ""
        for msg in reversed(response_messages):
            if hasattr(msg, "content") and getattr(msg, "type", None) == "ai":
                assistant_msg = msg.content
                break
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                assistant_msg = msg.get("content", "")
                break

        thread.messages.append({"role": "assistant", "content": assistant_msg})
        return {
            "thread_id": thread.thread_id,
            "response": assistant_msg,
            "metadata": {"message_count": len(thread.messages)},
        }

    async def stream(
        self,
        message: str,
        *,
        thread_id: str | None = None,
    ) -> Any:
        """Stream agent responses.

        Args:
            message: User message.
            thread_id: Optional thread ID.

        Yields:
            Server-Sent Events with agent output chunks.
        """
        thread = self._get_or_create_thread(thread_id)
        thread.messages.append({"role": "user", "content": message})
        self._request_count += 1

        config = {"configurable": {"thread_id": thread.thread_id}}
        input_data = self._build_input(thread, message)

        # SDK-CORE-5: decouple production from consumption. A background producer
        # holds a concurrency slot ONLY while producing, pushing events into a
        # bounded queue; each enqueue is bounded by request_timeout, so a slow
        # client that stops draining releases the slot after the timeout instead
        # of pinning it forever. The client drains the queue WITHOUT holding a
        # slot, so a stalled reader can no longer exhaust max_concurrent_requests.
        maxsize = max(1, self.config.stream_queue_maxsize)
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=maxsize)
        done = object()
        timeout = self.config.request_timeout

        async def _bounded_put(item: Any) -> None:
            # A full queue means the client isn't draining; bound the wait so a
            # stalled reader can't pin the producer's slot past request_timeout.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(queue.put(item), timeout=timeout)

        async def _produce() -> None:
            async with self._get_request_semaphore():
                try:
                    events = self.agent.astream_events(input_data, config=config, version="v2")
                    async for event in _timeboxed_aiter(events, timeout=timeout):
                        await _bounded_put({"event": event.get("event", ""), "data": event.get("data", {})})
                except TimeoutError:
                    logger.warning("Agent stream timed out after %ss (thread=%s)", timeout, thread.thread_id)
                    await _bounded_put({"event": "error", "data": {"error": "Gateway timeout: agent did not respond in time", "status_code": 504}})
                except Exception as exc:
                    await _bounded_put({"event": "error", "data": {"error": str(exc)}})
                finally:
                    await _bounded_put(done)

        producer = asyncio.create_task(_produce())
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=1.0)
                except TimeoutError:
                    # Wake periodically so a dropped terminal marker (producer
                    # gave up under backpressure) still ends the stream.
                    if producer.done():
                        while not queue.empty():
                            leftover = queue.get_nowait()
                            if leftover is not done:
                                yield leftover
                        break
                    continue
                if item is done:
                    break
                yield item
        finally:
            if not producer.done():
                producer.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await producer

    def get_health(self) -> dict[str, Any]:
        """Get server health status.

        Returns:
            Health check response.
        """
        return {
            "status": "healthy",
            "uptime_seconds": time.time() - self._start_time,
            "request_count": self._request_count,
            "active_threads": len(self._threads),
        }

    def get_info(self) -> dict[str, Any]:
        """Get server and agent information.

        Returns:
            Server info response.
        """
        return {
            "name": "bog-agents",
            "version": _get_version(),
            "endpoints": [
                "POST /invoke",
                "POST /stream",
                "GET /health",
                "GET /info",
                "GET /openapi.json",
                "POST /threads",
                "GET /threads",
                "POST /threads/{id}/messages",
                "GET /threads/{id}/history",
            ],
            "config": {
                "host": self.config.host,
                "port": self.config.port,
                "streaming": self.config.enable_streaming,
            },
        }

    def _build_openapi_schema(self) -> dict[str, Any]:
        """Return a hand-rolled OpenAPI 3.0 document describing the server's HTTP API.

        Kept in sync with the Starlette route table by hand. Validated against
        the OpenAPI 3.0.3 schema; consumers like Swagger UI, Stoplight, and
        openapi-typescript can introspect the API without a live SDK install.

        Returns:
            The OpenAPI document as a plain dict suitable for JSON serialization.
        """
        invoke_request = {
            "type": "object",
            "required": ["message"],
            "properties": {
                "message": {"type": "string", "description": "User input for the agent"},
                "thread_id": {"type": "string", "description": "Existing thread ID to resume"},
            },
        }
        invoke_response = {
            "type": "object",
            "properties": {
                "thread_id": {"type": "string"},
                "response": {"type": "string"},
                "metadata": {"type": "object", "additionalProperties": True},
            },
        }
        thread_summary = {
            "type": "object",
            "properties": {
                "thread_id": {"type": "string"},
                "created_at": {"type": "number"},
                "message_count": {"type": "integer"},
            },
        }
        return {
            "openapi": "3.0.3",
            "info": {
                "title": "Bog Agents HTTP API",
                "version": _get_version(),
                "description": "REST + SSE interface to a long-lived bog-agents agent.",
            },
            "servers": [{"url": f"http://{self.config.host}:{self.config.port}"}],
            "paths": {
                "/health": {
                    "get": {
                        "summary": "Liveness probe",
                        "responses": {
                            "200": {
                                "description": "Server is healthy",
                                "content": {"application/json": {"schema": {"type": "object"}}},
                            }
                        },
                    }
                },
                "/info": {
                    "get": {
                        "summary": "Server + agent metadata",
                        "responses": {"200": {"description": "OK"}},
                    }
                },
                "/invoke": {
                    "post": {
                        "summary": "Send one message to the agent and receive a response",
                        "requestBody": {
                            "required": True,
                            "content": {"application/json": {"schema": invoke_request}},
                        },
                        "responses": {
                            "200": {
                                "description": "Agent response",
                                "content": {"application/json": {"schema": invoke_response}},
                            },
                            "400": {"description": "Invalid request"},
                            "401": {"description": "Unauthorized"},
                        },
                    }
                },
                "/stream": {
                    "post": {
                        "summary": "Server-Sent Events stream of agent updates",
                        "requestBody": {
                            "required": True,
                            "content": {"application/json": {"schema": invoke_request}},
                        },
                        "responses": {
                            "200": {
                                "description": "text/event-stream of {event, data} chunks",
                                "content": {"text/event-stream": {}},
                            }
                        },
                    }
                },
                "/threads": {
                    "get": {
                        "summary": "List threads",
                        "responses": {
                            "200": {
                                "description": "List of thread summaries",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "properties": {
                                                "threads": {"type": "array", "items": thread_summary},
                                            },
                                        }
                                    }
                                },
                            }
                        },
                    },
                    "post": {
                        "summary": "Create a thread",
                        "responses": {"201": {"description": "Created"}},
                    },
                },
                "/threads/{thread_id}/messages": {
                    "post": {
                        "summary": "Append a message to a specific thread",
                        "parameters": [{"name": "thread_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                        "responses": {"200": {"description": "OK"}, "404": {"description": "Thread not found"}},
                    }
                },
                "/threads/{thread_id}/history": {
                    "get": {
                        "summary": "Read a thread's full message history",
                        "parameters": [{"name": "thread_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                        "responses": {"200": {"description": "OK"}, "404": {"description": "Thread not found"}},
                    }
                },
            },
            "components": {"securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}},
        }

    def list_threads(self) -> list[dict[str, Any]]:
        """List all conversation threads.

        Returns:
            List of thread summaries.
        """
        return [
            {
                "thread_id": t.thread_id,
                "created_at": t.created_at,
                "message_count": len(t.messages),
            }
            for t in self._threads.values()
        ]

    def get_thread_history(self, thread_id: str) -> dict[str, Any] | None:
        """Get message history for a thread.

        Args:
            thread_id: Thread ID.

        Returns:
            Thread history or ``None`` if not found.
        """
        thread = self._threads.get(thread_id)
        if thread is None:
            return None
        return {
            "thread_id": thread.thread_id,
            "created_at": thread.created_at,
            "messages": thread.messages,
        }

    def create_app(self) -> Any:
        """Create a Starlette/ASGI application.

        Returns:
            ASGI application instance.

        Raises:
            ImportError: If starlette is not installed.
        """
        from sse_starlette.sse import EventSourceResponse
        from starlette.applications import Starlette
        from starlette.middleware.cors import CORSMiddleware
        from starlette.requests import Request
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        server = self

        def _extract_bearer(request: Request) -> str | None:
            """Extract Bearer token from Authorization header."""
            auth = request.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                return auth[len("Bearer ") :]
            return auth or None

        def _unauthorized() -> JSONResponse:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        async def health_endpoint(_request: Request) -> JSONResponse:
            return JSONResponse(server.get_health())

        async def openapi_endpoint(_request: Request) -> JSONResponse:
            """Serve a hand-rolled OpenAPI 3.0 schema for discovery.

            Switching to FastAPI for free OpenAPI generation is a non-trivial
            refactor (Route() + EventSourceResponse patterns differ from
            FastAPI's decorator model and the existing test suite asserts
            the Starlette behavior). Hand-rolling the schema is the safer
            ship: any compliant OpenAPI client can introspect endpoints,
            and the schema lives next to the route definitions so it can't
            drift unnoticed.
            """
            return JSONResponse(server._build_openapi_schema())

        async def info_endpoint(request: Request) -> JSONResponse:
            if not server._check_api_key(_extract_bearer(request)):
                return _unauthorized()
            return JSONResponse(server.get_info())

        async def invoke_endpoint(request: Request) -> JSONResponse:
            if not server._check_api_key(_extract_bearer(request)):
                return _unauthorized()
            try:
                body = await request.json()
            except (json.JSONDecodeError, ValueError):
                return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
            message = body.get("message", "")
            if not message:
                return JSONResponse({"error": "message is required"}, status_code=400)
            result = await server.invoke(message, thread_id=body.get("thread_id"))
            status_code = result.get("status_code", 200) if isinstance(result, dict) else 200
            return JSONResponse(result, status_code=status_code)

        async def stream_endpoint(request: Request) -> JSONResponse | EventSourceResponse:
            if not server._check_api_key(_extract_bearer(request)):
                return _unauthorized()
            # SDK-CORE-6: enable_streaming was inert — /stream streamed regardless.
            # Honor it: when disabled, refuse instead of silently streaming.
            if not server.config.enable_streaming:
                return JSONResponse(
                    {"error": "Streaming is disabled on this server"},
                    status_code=501,
                )
            try:
                body = await request.json()
            except (json.JSONDecodeError, ValueError):
                return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
            message = body.get("message", "")
            if not message:
                return JSONResponse({"error": "message is required"}, status_code=400)
            thread_id = body.get("thread_id")

            async def event_generator() -> Any:
                async for event in server.stream(message, thread_id=thread_id):
                    yield {
                        "event": event.get("event", "message"),
                        "data": json.dumps(event.get("data", {})),
                    }

            return EventSourceResponse(event_generator())

        async def list_threads_endpoint(request: Request) -> JSONResponse:
            if not server._check_api_key(_extract_bearer(request)):
                return _unauthorized()
            return JSONResponse({"threads": server.list_threads()})

        async def create_thread_endpoint(request: Request) -> JSONResponse:
            if not server._check_api_key(_extract_bearer(request)):
                return _unauthorized()
            try:
                raw = await request.body()
                body = json.loads(raw) if raw else {}
            except (json.JSONDecodeError, ValueError):
                return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
            thread = server._get_or_create_thread()
            if isinstance(body, dict) and "metadata" in body:
                thread.metadata = body["metadata"]
            return JSONResponse(
                {"thread_id": thread.thread_id, "created_at": thread.created_at},
                status_code=201,
            )

        async def thread_messages_endpoint(request: Request) -> JSONResponse:
            if not server._check_api_key(_extract_bearer(request)):
                return _unauthorized()
            thread_id = request.path_params["thread_id"]
            try:
                body = await request.json()
            except (json.JSONDecodeError, ValueError):
                return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
            message = body.get("message", "")
            if not message:
                return JSONResponse({"error": "message is required"}, status_code=400)
            result = await server.invoke(message, thread_id=thread_id)
            status_code = result.get("status_code", 200) if isinstance(result, dict) else 200
            return JSONResponse(result, status_code=status_code)

        async def thread_history_endpoint(request: Request) -> JSONResponse:
            if not server._check_api_key(_extract_bearer(request)):
                return _unauthorized()
            thread_id = request.path_params["thread_id"]
            history = server.get_thread_history(thread_id)
            if history is None:
                return JSONResponse({"error": "Thread not found"}, status_code=404)
            return JSONResponse(history)

        routes = [
            Route("/health", health_endpoint, methods=["GET"]),
            Route("/openapi.json", openapi_endpoint, methods=["GET"]),
            Route("/info", info_endpoint, methods=["GET"]),
            Route("/invoke", invoke_endpoint, methods=["POST"]),
            Route("/stream", stream_endpoint, methods=["POST"]),
            Route("/threads", list_threads_endpoint, methods=["GET"]),
            Route("/threads", create_thread_endpoint, methods=["POST"]),
            Route(
                "/threads/{thread_id}/messages",
                thread_messages_endpoint,
                methods=["POST"],
            ),
            Route(
                "/threads/{thread_id}/history",
                thread_history_endpoint,
                methods=["GET"],
            ),
        ]

        app = Starlette(routes=routes)
        app.add_middleware(
            CORSMiddleware,
            allow_origins=server.config.cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )
        return app

    def run(self) -> None:
        """Run the server (blocking).

        Starts a uvicorn server with the configured host and port.

        Raises:
            ImportError: If uvicorn is not installed.
        """
        import uvicorn

        app = self.create_app()
        logger.info(
            "Starting bog-agents server on %s:%d",
            self.config.host,
            self.config.port,
        )
        uvicorn.run(
            app,
            host=self.config.host,
            port=self.config.port,
            log_level="info",
        )


async def _timeboxed_aiter(source: Any, *, timeout: float) -> Any:  # noqa: ASYNC109 -- timeout is forwarded to wait_for
    """Yield from an async iterator, bounding the wait for each item by ``timeout``.

    If no next item is produced within ``timeout`` seconds, ``TimeoutError``
    is raised so the caller can surface a 504-style response instead of hanging.

    Args:
        source: The async iterable/iterator to consume.
        timeout: Maximum seconds to wait for each item.

    Yields:
        Items from the underlying async iterator.

    Raises:
        TimeoutError: If an item is not produced within ``timeout``.
    """
    iterator = source.__aiter__()
    while True:
        try:
            item = await asyncio.wait_for(iterator.__anext__(), timeout=timeout)
        except StopAsyncIteration:
            return
        yield item


def _get_version() -> str:
    """Get the bog-agents version string."""
    try:
        from bog_agents import __version__

        return __version__
    except ImportError:
        return "unknown"
