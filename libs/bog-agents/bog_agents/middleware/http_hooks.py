"""HTTP Hooks middleware for webhook-based lifecycle event integration.

Extends the shell-based lifecycle hooks with HTTP webhook support.
POST JSON payloads to configured URLs on agent events and receive
JSON responses that can modify agent behavior.

Supports: Slack notifications, CI triggers, dashboard updates,
custom orchestration, and any HTTP-accessible service.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class WebhookEvent(StrEnum):
    """Events that can trigger HTTP webhooks."""

    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"
    PRE_MODEL_CALL = "pre_model_call"
    POST_MODEL_CALL = "post_model_call"
    ON_ERROR = "on_error"
    ON_AGENT_START = "on_agent_start"
    ON_AGENT_STOP = "on_agent_stop"
    ON_CHECKPOINT = "on_checkpoint"
    ON_COST_UPDATE = "on_cost_update"
    ON_FILE_CHANGE = "on_file_change"
    ON_APPROVAL_REQUEST = "on_approval_request"


class WebhookAction(StrEnum):
    """Actions a webhook response can request."""

    CONTINUE = "continue"
    BLOCK = "block"
    MODIFY = "modify"
    LOG = "log"


@dataclass
class WebhookEndpoint:
    """Configuration for an HTTP webhook endpoint."""

    url: str
    """The URL to POST to."""

    events: list[WebhookEvent]
    """Events this endpoint listens to."""

    headers: dict[str, str] = field(default_factory=dict)
    """Additional HTTP headers (e.g., Authorization)."""

    timeout_seconds: float = 10.0
    """Request timeout in seconds."""

    retry_count: int = 2
    """Number of retries on failure."""

    retry_delay_seconds: float = 1.0
    """Delay between retries."""

    secret: str | None = None
    """Shared secret for HMAC signature verification."""

    name: str = ""
    """Human-readable endpoint name."""

    async_fire: bool = True
    """Whether to fire and forget (True) or wait for response (False)."""


@dataclass
class WebhookPayload:
    """Payload sent to webhook endpoints."""

    event: str
    timestamp: float
    session_id: str | None
    tool_name: str | None
    tool_args: dict[str, Any] | None
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "event": self.event,
            "timestamp": self.timestamp,
            "session_id": self.session_id,
            "tool_name": self.tool_name,
            "tool_args": self.tool_args,
            "metadata": self.metadata,
        }


@dataclass
class WebhookResponse:
    """Parsed response from a webhook endpoint."""

    action: WebhookAction = WebhookAction.CONTINUE
    message: str | None = None
    modifications: dict[str, Any] | None = None
    status_code: int = 200

    @classmethod
    def from_dict(cls, data: dict[str, Any], status_code: int = 200) -> WebhookResponse:
        """Parse a webhook response from a dictionary.

        Args:
            data: Response JSON data.
            status_code: HTTP status code.

        Returns:
            Parsed WebhookResponse.
        """
        action_str = data.get("action", "continue")
        try:
            action = WebhookAction(action_str)
        except ValueError:
            action = WebhookAction.CONTINUE

        return cls(
            action=action,
            message=data.get("message"),
            modifications=data.get("modifications"),
            status_code=status_code,
        )


def _compute_signature(payload_bytes: bytes, secret: str) -> str:
    """Compute HMAC-SHA256 signature for payload verification.

    Args:
        payload_bytes: The JSON payload as bytes.
        secret: Shared secret for HMAC.

    Returns:
        Hex-encoded HMAC-SHA256 signature.
    """
    import hashlib
    import hmac

    return hmac.new(
        secret.encode("utf-8"),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()


async def fire_webhook(
    endpoint: WebhookEndpoint,
    payload: WebhookPayload,
) -> WebhookResponse | None:
    """Fire a webhook to a configured endpoint.

    Args:
        endpoint: The webhook endpoint configuration.
        payload: The event payload to send.

    Returns:
        WebhookResponse if sync, None if async fire-and-forget.
    """
    try:
        import aiohttp
    except ImportError:
        logger.warning("aiohttp not installed; falling back to synchronous requests for webhooks")
        return _fire_webhook_sync(endpoint, payload)

    payload_dict = payload.to_dict()
    payload_bytes = json.dumps(payload_dict).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "bog-agents-webhook/1.0",
        **endpoint.headers,
    }

    if endpoint.secret:
        sig = _compute_signature(payload_bytes, endpoint.secret)
        headers["X-Webhook-Signature"] = f"sha256={sig}"

    for attempt in range(endpoint.retry_count + 1):
        try:
            timeout = aiohttp.ClientTimeout(total=endpoint.timeout_seconds)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    endpoint.url,
                    data=payload_bytes,
                    headers=headers,
                ) as resp:
                    if resp.status >= 200 and resp.status < 300:
                        if not endpoint.async_fire:
                            body = await resp.json()
                            return WebhookResponse.from_dict(body, resp.status)
                        return None
                    logger.warning(
                        "Webhook %s returned status %d (attempt %d/%d)",
                        endpoint.name or endpoint.url,
                        resp.status,
                        attempt + 1,
                        endpoint.retry_count + 1,
                    )
        except Exception:
            logger.debug(
                "Webhook %s failed (attempt %d/%d)",
                endpoint.name or endpoint.url,
                attempt + 1,
                endpoint.retry_count + 1,
                exc_info=True,
            )
            if attempt < endpoint.retry_count:
                await asyncio.sleep(endpoint.retry_delay_seconds * (attempt + 1))

    logger.error("Webhook %s exhausted all retries", endpoint.name or endpoint.url)
    return None


def _fire_webhook_sync(
    endpoint: WebhookEndpoint,
    payload: WebhookPayload,
) -> WebhookResponse | None:
    """Synchronous fallback when aiohttp is not available.

    Args:
        endpoint: Webhook endpoint configuration.
        payload: Event payload.

    Returns:
        WebhookResponse if sync mode, None otherwise.
    """
    try:
        import requests
    except ImportError:
        logger.error("Neither aiohttp nor requests installed; cannot fire webhooks")
        return None

    payload_dict = payload.to_dict()
    payload_bytes = json.dumps(payload_dict).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "bog-agents-webhook/1.0",
        **endpoint.headers,
    }

    if endpoint.secret:
        sig = _compute_signature(payload_bytes, endpoint.secret)
        headers["X-Webhook-Signature"] = f"sha256={sig}"

    for attempt in range(endpoint.retry_count + 1):
        try:
            resp = requests.post(
                endpoint.url,
                data=payload_bytes,
                headers=headers,
                timeout=endpoint.timeout_seconds,
            )
            if resp.ok:
                if not endpoint.async_fire:
                    return WebhookResponse.from_dict(resp.json(), resp.status_code)
                return None
            logger.warning(
                "Webhook %s returned status %d (attempt %d/%d)",
                endpoint.name or endpoint.url,
                resp.status_code,
                attempt + 1,
                endpoint.retry_count + 1,
            )
        except Exception:
            logger.debug(
                "Webhook %s failed (attempt %d/%d)",
                endpoint.name or endpoint.url,
                attempt + 1,
                endpoint.retry_count + 1,
                exc_info=True,
            )
            if attempt < endpoint.retry_count:
                time.sleep(endpoint.retry_delay_seconds * (attempt + 1))

    return None


class HttpHooksMiddleware(AgentMiddleware):
    """Middleware that fires HTTP webhooks on agent lifecycle events.

    Example:
        ```python
        from bog_agents.middleware.http_hooks import (
            HttpHooksMiddleware,
            WebhookEndpoint,
            WebhookEvent,
        )

        middleware = HttpHooksMiddleware(
            endpoints=[
                WebhookEndpoint(
                    url="https://hooks.slack.com/services/...",
                    events=[WebhookEvent.ON_ERROR, WebhookEvent.ON_AGENT_STOP],
                    name="slack-errors",
                ),
                WebhookEndpoint(
                    url="http://localhost:8080/agent-events",
                    events=[WebhookEvent.POST_TOOL_USE],
                    secret="my-shared-secret",
                    async_fire=False,
                    name="local-dashboard",
                ),
            ],
        )
        ```
    """

    endpoints: list[WebhookEndpoint]
    session_id: str | None

    def __init__(
        self,
        *,
        endpoints: list[WebhookEndpoint] | None = None,
        session_id: str | None = None,
    ) -> None:
        """Initialize HTTP hooks middleware.

        Args:
            endpoints: List of webhook endpoints to register.
            session_id: Optional session identifier included in payloads.
        """
        self.endpoints = endpoints or []
        self.session_id = session_id

    def _endpoints_for_event(self, event: WebhookEvent) -> list[WebhookEndpoint]:
        """Get endpoints listening for a specific event."""
        return [ep for ep in self.endpoints if event in ep.events]

    async def _fire_event(
        self,
        event: WebhookEvent,
        *,
        tool_name: str | None = None,
        tool_args: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> list[WebhookResponse | None]:
        """Fire an event to all registered endpoints.

        Args:
            event: The event type.
            tool_name: Optional tool name for tool events.
            tool_args: Optional tool arguments.
            metadata: Additional metadata.

        Returns:
            List of responses from endpoints.
        """
        endpoints = self._endpoints_for_event(event)
        if not endpoints:
            return []

        payload = WebhookPayload(
            event=event.value,
            timestamp=time.time(),
            session_id=self.session_id,
            tool_name=tool_name,
            tool_args=tool_args,
            metadata=metadata or {},
        )

        tasks = [fire_webhook(ep, payload) for ep in endpoints]
        return list(await asyncio.gather(*tasks, return_exceptions=False))

    async def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Any,
        runtime: Any,
    ) -> ModelResponse[ResponseT]:
        """Wrap model calls with pre/post webhook events."""
        await self._fire_event(
            WebhookEvent.PRE_MODEL_CALL,
            metadata={"message_count": len(request.messages) if hasattr(request, "messages") else 0},
        )

        try:
            response = await call_next(request, runtime)
        except Exception as exc:
            await self._fire_event(
                WebhookEvent.ON_ERROR,
                metadata={"error": str(exc), "error_type": type(exc).__name__},
            )
            raise

        await self._fire_event(
            WebhookEvent.POST_MODEL_CALL,
            metadata={},
        )

        return response
