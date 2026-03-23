"""HTTP API server for bog-agents.

Exposes a running agent as a REST/WebSocket API server. Enables production
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

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ServerConfig:
    """Configuration for the bog-agents HTTP server."""

    host: str = "127.0.0.1"
    port: int = 8420
    cors_origins: list[str] = field(default_factory=lambda: ["*"])
    api_key: str | None = None
    max_concurrent_requests: int = 10
    request_timeout: float = 300.0
    enable_streaming: bool = True
    enable_websocket: bool = True


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
        self._threads: dict[str, ThreadState] = {}
        self._request_count = 0
        self._start_time = time.time()

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
        return self._threads[thread_id]

    def _check_api_key(self, provided_key: str | None) -> bool:
        """Validate the API key if one is configured.

        Args:
            provided_key: The key provided in the request.

        Returns:
            ``True`` if valid or no key required.
        """
        if self.config.api_key is None:
            return True
        return provided_key == self.config.api_key

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
        input_data = {"messages": [{"role": "user", "content": message}]}

        try:
            result = await self.agent.ainvoke(input_data, config=config)
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
        except Exception:
            logger.exception("Agent invocation failed")
            return {
                "thread_id": thread.thread_id,
                "error": "Internal server error",
                "metadata": {},
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
        input_data = {"messages": [{"role": "user", "content": message}]}

        try:
            async for event in self.agent.astream_events(input_data, config=config, version="v2"):
                yield {
                    "event": event.get("event", ""),
                    "data": event.get("data", {}),
                }
        except Exception as exc:
            yield {
                "event": "error",
                "data": {"error": str(exc)},
            }

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
                "POST /threads",
                "GET /threads",
                "POST /threads/{id}/messages",
                "GET /threads/{id}/history",
            ],
            "config": {
                "host": self.config.host,
                "port": self.config.port,
                "streaming": self.config.enable_streaming,
                "websocket": self.config.enable_websocket,
            },
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
        from sse_starlette.sse import EventSourceResponse  # ty: ignore[unresolved-import]
        from starlette.applications import Starlette  # ty: ignore[unresolved-import]
        from starlette.middleware.cors import CORSMiddleware  # ty: ignore[unresolved-import]
        from starlette.requests import Request  # ty: ignore[unresolved-import]
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
            return JSONResponse(result)

        async def stream_endpoint(request: Request) -> JSONResponse | EventSourceResponse:
            if not server._check_api_key(_extract_bearer(request)):
                return _unauthorized()
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
            return JSONResponse(result)

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


def _get_version() -> str:
    """Get the bog-agents version string."""
    try:
        from bog_agents import __version__

        return __version__
    except ImportError:
        return "unknown"
