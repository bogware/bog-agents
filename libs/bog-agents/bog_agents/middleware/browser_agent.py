"""Middleware providing browser automation and web interaction.

Feature #24: Browser agent — headless browsing, testing, debugging.
Feature #25: Live preview server.
Feature #26: API testing tool.
Feature #27: Authenticated web fetching.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlparse

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ResponseT,
)
from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, StructuredTool
from typing_extensions import TypedDict

logger = logging.getLogger(__name__)


class BrowserAgentState(TypedDict):
    """State for the browser agent middleware."""


class BrowserAgentMiddleware(AgentMiddleware[BrowserAgentState, ContextT, ResponseT]):
    """Middleware for browser automation and web interaction.

    Provides tools for headless browsing, API testing, and web fetching.

    Args:
        working_dir: Working directory.
        allowed_domains: List of allowed domains for browsing.
    """

    state_schema = BrowserAgentState

    def __init__(
        self,
        *,
        working_dir: Path | None = None,
        allowed_domains: list[str] | None = None,
    ) -> None:
        self._working_dir = working_dir or Path.cwd()
        self._allowed_domains = set(allowed_domains) if allowed_domains else None
        self._preview_processes: dict[int, subprocess.Popen[str]] = {}
        self.tools = self._build_tools()

    def _is_domain_allowed(self, url: str) -> bool:
        """Check if URL domain is allowed.

        Args:
            url: URL to check.

        Returns:
            True if allowed.
        """
        if self._allowed_domains is None:
            return True
        parsed = urlparse(url)
        return parsed.hostname in self._allowed_domains if parsed.hostname else False

    def _build_tools(self) -> list[BaseTool]:
        """Build browser and API tools."""
        middleware = self

        def web_fetch(
            runtime: ToolRuntime[None, BrowserAgentState],
            url: Annotated[str, "URL to fetch"],
            method: Annotated[str, "HTTP method: GET, POST, PUT, DELETE, PATCH"] = "GET",
            headers: Annotated[dict[str, str] | None, "Optional HTTP headers"] = None,
            body: Annotated[str | None, "Optional request body"] = None,
            extract_text: Annotated[bool, "Extract readable text from HTML"] = True,
        ) -> str:
            """Fetch a URL with optional authentication headers.

            Supports various HTTP methods for API testing.
            """
            if not middleware._is_domain_allowed(url):
                return f"Error: Domain not in allowed list for URL: {url}"

            try:
                import urllib.request

                req = urllib.request.Request(url, method=method)
                if headers:
                    for key, value in headers.items():
                        req.add_header(key, value)
                if body:
                    req.data = body.encode("utf-8")

                with urllib.request.urlopen(req, timeout=30) as response:
                    content = response.read().decode("utf-8", errors="replace")
                    status = response.status
                    resp_headers = dict(response.headers)

                    result = f"HTTP {status} {method} {url}\n"
                    result += f"Headers: {json.dumps(resp_headers, indent=2)[:500]}\n"
                    if extract_text and "text/html" in resp_headers.get("Content-Type", ""):
                        # Basic HTML text extraction
                        import re

                        text = re.sub(r"<[^>]+>", " ", content)
                        text = re.sub(r"\s+", " ", text).strip()
                        result += f"Content (text): {text[:2000]}"
                    else:
                        result += f"Content: {content[:2000]}"
                    return result
            except Exception as e:
                return f"Error fetching {url}: {e}"

        def api_request(
            runtime: ToolRuntime[None, BrowserAgentState],
            url: Annotated[str, "API endpoint URL"],
            method: Annotated[str, "HTTP method"] = "GET",
            headers: Annotated[dict[str, str] | None, "Request headers (e.g., Authorization)"] = None,
            json_body: Annotated[dict[str, Any] | None, "JSON request body"] = None,
        ) -> str:
            """Send an API request and return the response with timing info."""
            import time
            import urllib.request

            start = time.monotonic()
            try:
                req = urllib.request.Request(url, method=method)
                req.add_header("Content-Type", "application/json")
                req.add_header("Accept", "application/json")
                if headers:
                    for key, value in headers.items():
                        req.add_header(key, value)
                data = json.dumps(json_body).encode("utf-8") if json_body else None
                if data:
                    req.data = data

                with urllib.request.urlopen(req, timeout=30) as response:
                    elapsed = (time.monotonic() - start) * 1000
                    content = response.read().decode("utf-8", errors="replace")
                    status = response.status

                    result = f"HTTP {status} {method} {url} ({elapsed:.0f}ms)\n"
                    try:
                        parsed = json.loads(content)
                        result += json.dumps(parsed, indent=2)[:3000]
                    except json.JSONDecodeError:
                        result += content[:3000]
                    return result
            except Exception as e:
                elapsed = (time.monotonic() - start) * 1000
                return f"Error: {method} {url} ({elapsed:.0f}ms): {e}"

        def start_preview_server(
            runtime: ToolRuntime[None, BrowserAgentState],
            command: Annotated[str, "Command to start dev server (e.g., 'npm run dev')"],
            port: Annotated[int, "Expected port number"] = 3000,
        ) -> str:
            """Start a local dev server for previewing changes."""
            try:
                process = subprocess.Popen(
                    command.split(),
                    cwd=middleware._working_dir,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                middleware._preview_processes[port] = process
                return f"Started preview server (PID={process.pid}) on port {port}.\nURL: http://localhost:{port}"
            except Exception as e:
                return f"Error starting server: {e}"

        def stop_preview_server(
            runtime: ToolRuntime[None, BrowserAgentState],
            port: Annotated[int, "Port of server to stop"] = 3000,
        ) -> str:
            """Stop a running preview server."""
            process = middleware._preview_processes.pop(port, None)
            if process is None:
                return f"No server running on port {port}"
            process.terminate()
            return f"Stopped server on port {port}"

        return [
            StructuredTool.from_function(name="web_fetch", description="Fetch a URL with auth support.", func=web_fetch),
            StructuredTool.from_function(name="api_request", description="Send an API request with timing.", func=api_request),
            StructuredTool.from_function(name="start_preview", description="Start a local dev server.", func=start_preview_server),
            StructuredTool.from_function(name="stop_preview", description="Stop a preview server.", func=stop_preview_server),
        ]
