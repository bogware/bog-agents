"""Single-use loopback HTTP server for MCP OAuth authorization callbacks.

The MCP SDK's `OAuthClientProvider` drives the browser to the authorization
server, which redirects back to a `redirect_uri` carrying `?code=&state=`. For
a CLI there is no long-lived web server to receive that redirect, so this
module binds a throwaway `127.0.0.1` server for exactly one callback, hands the
captured `(code, state)` to the awaiting login flow, and shuts down.

A headless `parse_callback_url` fallback lets environments without a browser or
without loopback connectivity finish the flow by pasting the redirected URL
back into the terminal.
"""

from __future__ import annotations

import asyncio
import html
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Literal, Self
from urllib.parse import parse_qs, urlparse

if TYPE_CHECKING:
    from types import TracebackType

logger = logging.getLogger(__name__)

_BIND_HOST = "127.0.0.1"
# Advertise the literal loopback IP (not `localhost`): on a dual-stack host
# `localhost` can resolve to `::1` first, so the browser callback would hit IPv6
# and fail to reach the IPv4-bound server. RFC 8252 s7.3 recommends `127.0.0.1`.
_URI_HOST = "127.0.0.1"
_CALLBACK_PATH = "/callback"


class LoopbackCallbackError(RuntimeError):
    """Raised when the loopback callback cannot complete.

    Covers both provider-reported `error=` callbacks and a callback URL that
    arrives without a `code` parameter.
    """


class LoopbackTimeoutError(RuntimeError):
    """Raised when no authorization callback arrives before the timeout."""


def parse_callback_url(url: str) -> tuple[str, str | None]:
    """Parse an OAuth redirect URL into `(code, state)`.

    The headless paste-back fallback: a user copies the full URL the browser
    was redirected to and pastes it into the terminal.

    Args:
        url: The full redirect URL, including its query string.

    Returns:
        The `code` and optional `state` query parameters.

    Raises:
        LoopbackCallbackError: If the URL carries `error=` or lacks `code`.
    """
    params = parse_qs(urlparse(url).query)
    if "error" in params:
        err = params["error"][0]
        desc = (params.get("error_description") or [""])[0]
        detail = f": {desc}" if desc else ""
        msg = f"Authorization denied by provider: {err}{detail}"
        raise LoopbackCallbackError(msg)
    if not params.get("code"):
        msg = "Callback URL is missing the 'code' parameter."
        raise LoopbackCallbackError(msg)
    return params["code"][0], (params.get("state") or [None])[0]


class LoopbackCallbackServer:
    """A one-shot `127.0.0.1` server that captures a single OAuth callback.

    Binds and starts serving on construction so `redirect_uri` and `port` are
    known immediately (the provider needs the redirect URI before it builds the
    authorization URL). The first well-formed `/callback` request wins; later
    requests get a "already handled" page and never overwrite the result.
    """

    def __init__(self, *, port: int = 0) -> None:
        """Bind the callback server and begin serving in a background thread.

        Propagates `OSError` from the underlying `ThreadingHTTPServer` when the
        socket cannot be bound (e.g. the requested port is in use).

        Args:
            port: TCP port to bind on `127.0.0.1`. `0` (default) picks an
                ephemeral free port.
        """
        self._event = threading.Event()
        self._result: tuple[str, str | None] | None = None
        self._error: str | None = None
        self._closed = False

        parent = self

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # stdlib handler name
                parent._handle_get(self)

            def log_message(  # noqa: PLR6301  # stdlib override signature
                self,
                format: str,  # noqa: A002  # stdlib parameter name
                *args: object,
            ) -> None:
                del format, args

        self._server = ThreadingHTTPServer((_BIND_HOST, port), _Handler)
        self._port = self._server.server_address[1]
        self.redirect_uri = f"http://{_URI_HOST}:{self._port}{_CALLBACK_PATH}"
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="bog-mcp-oauth-callback",
            daemon=True,
        )
        self._thread.start()

    @property
    def port(self) -> int:
        """The bound loopback TCP port."""
        return self._port

    def __enter__(self) -> Self:
        """Enter a context that guarantees `close()` on exit."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Shut the server down on context exit."""
        self.close()

    async def wait_for_code(
        self,
        timeout: float = 300.0,  # noqa: ASYNC109  # passed to a threaded Event.wait, not an asyncio.timeout
    ) -> tuple[str, str | None]:
        """Wait for the browser callback and return `(code, state)`.

        Args:
            timeout: Seconds to wait for the callback before giving up.

        Returns:
            The captured authorization `code` and optional `state`.

        Raises:
            LoopbackTimeoutError: If no callback arrives before `timeout`.
            LoopbackCallbackError: If the provider reported an error or the
                callback lacked a `code`.
        """
        got = await asyncio.to_thread(self._event.wait, timeout)
        if not got:
            msg = "Browser callback was not received before the timeout."
            raise LoopbackTimeoutError(msg)
        if self._error is not None:
            raise LoopbackCallbackError(self._error)
        if self._result is None:  # pragma: no cover - event set implies one is set
            msg = "Callback completed without a result."
            raise LoopbackCallbackError(msg)
        return self._result

    def close(self) -> None:
        """Stop serving and release the socket. Idempotent."""
        if self._closed:
            return
        self._closed = True
        self._server.shutdown()
        self._server.server_close()

    def _handle_get(self, handler: BaseHTTPRequestHandler) -> None:
        """Handle one GET, capturing the callback exactly once."""
        if self._event.is_set():
            # Duplicate request (favicon, retry, prefetch) after the flow
            # already resolved. Respond without touching the stored result.
            if self._error is None:
                self._send(handler, 200, _success_html())
            else:
                self._send(handler, 400, _error_html(self._error))
            return

        parsed = urlparse(handler.path)
        if parsed.path != _CALLBACK_PATH:
            self._send(handler, 404, _error_html("Callback route not found."))
            return

        params = parse_qs(parsed.query)
        if "error" in params:
            err = params["error"][0]
            desc = (params.get("error_description") or [""])[0]
            detail = f": {desc}" if desc else ""
            self._error = f"Authorization denied by provider: {err}{detail}"
            self._send(handler, 400, _error_html(self._error))
            self._event.set()
            return
        if not params.get("code"):
            self._error = "Callback URL is missing the 'code' parameter."
            self._send(handler, 400, _error_html(self._error))
            self._event.set()
            return

        self._result = (params["code"][0], (params.get("state") or [None])[0])
        self._send(handler, 200, _success_html())
        self._event.set()

    @staticmethod
    def _send(handler: BaseHTTPRequestHandler, status: int, body: str) -> None:
        """Write an HTML response body with the given status."""
        payload = body.encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)


def _success_html() -> str:
    return _result_html(
        title="Authorization complete",
        heading="You're signed in",
        message=(
            "MCP authorization complete. You can close this tab and return to "
            "your terminal."
        ),
        status="success",
    )


def _error_html(message: str) -> str:
    return _result_html(
        title="Authorization failed",
        heading="Authorization failed",
        message=message,
        status="error",
    )


def _result_html(
    *,
    title: str,
    heading: str,
    message: str,
    status: Literal["success", "error"],
) -> str:
    """Render a small styled result page for the callback tab."""
    accent = "#137333" if status == "success" else "#b3261e"
    background = "#eef7f0" if status == "success" else "#fceeee"
    mark = "✓" if status == "success" else "!"
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title>"
        "<style>"
        "body{margin:0;min-height:100vh;display:grid;place-items:center;"
        "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
        "background:#f8faf9;color:#1f2328}"
        ".panel{width:min(480px,calc(100vw - 40px));box-sizing:border-box;"
        "padding:32px;border:1px solid #d8dee4;border-radius:8px;"
        "background:#fff;box-shadow:0 18px 45px rgba(31,35,40,.08)}"
        ".mark{width:44px;height:44px;border-radius:50%;display:grid;"
        "place-items:center;margin-bottom:20px;font-weight:700}"
        "h1{font-size:24px;line-height:1.2;margin:0 0 10px}"
        "p{font-size:15px;line-height:1.5;margin:0;color:#57606a}"
        "</style></head><body>"
        '<main class="panel">'
        f'<div class="mark" style="background:{background};color:{accent}">'
        f"{mark}</div>"
        f"<h1>{html.escape(heading)}</h1><p>{html.escape(message)}</p>"
        "</main></body></html>"
    )
