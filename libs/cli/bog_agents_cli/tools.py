"""Custom tools for the CLI agent."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    import requests as _requests_mod
    from tavily import TavilyClient

_UNSET = object()
_tavily_client: TavilyClient | object | None = _UNSET

if TYPE_CHECKING:
    from bog_agents.cost_ledger import CostLedger

_web_search_ledger: CostLedger | None = None


def set_web_search_ledger(ledger: CostLedger | None) -> None:
    """Point `web_search` at the session's `CostLedger` (ROADMAP #51).

    Every search is counted through `register_web_search`, so
    `cost.max_web_searches` actually fires. The tool is a module-level
    function shared by every agent in the process, so the most recently
    created agent's ledger is the one that counts.

    Args:
        ledger: The ledger, or `None` to stop counting.
    """
    global _web_search_ledger  # noqa: PLW0603 - module-level tool state
    _web_search_ledger = ledger


# Hard ceiling on redirect hops the requests-based fetchers will follow.
# Each hop's target host is re-validated through the SSRF guard.
_MAX_REDIRECTS = 5


def _request_with_guarded_redirects(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    json_body: object | None = None,
    data: object | None = None,
    timeout: int,
) -> _requests_mod.Response:
    """Issue an HTTP request with an SSRF guard and per-hop redirect checks.

    Redirects are disabled at the ``requests`` layer (``allow_redirects=False``)
    and followed manually so that every hop's target host is re-validated by
    :func:`bog_agents_cli.web_fetch.assert_fetch_allowed` before a connection
    is made. This blocks both a directly-private target and a public→private
    302 redirect (e.g. into the cloud metadata endpoint).

    Args:
        method: HTTP method (already upper-cased by the caller).
        url: Initial target URL.
        headers: Optional request headers (sent on every hop).
        params: Optional query parameters (applied to the first hop only).
        json_body: Optional JSON body.
        data: Optional raw body.
        timeout: Per-request timeout in seconds.

    Returns:
        The final `requests.Response` after following any safe redirects.

    Note:
        :class:`bog_agents_cli.web_fetch.SsrfError` propagates from the
        per-hop ``assert_fetch_allowed`` call when a target is blocked.

    Raises:
        WebFetchError: When the redirect limit is exceeded.
    """
    from urllib.parse import urljoin

    import requests

    from bog_agents_cli.web_fetch import WebFetchError, assert_fetch_allowed

    current_url = url
    current_params = params
    for _hop in range(_MAX_REDIRECTS + 1):
        assert_fetch_allowed(current_url)
        kwargs: dict[str, Any] = {"allow_redirects": False, "timeout": timeout}
        if headers:
            kwargs["headers"] = headers
        if current_params:
            kwargs["params"] = current_params
        if json_body is not None:
            kwargs["json"] = json_body
        elif data is not None:
            kwargs["data"] = data

        response = requests.request(  # noqa: S113  # timeout is set in kwargs above
            method, current_url, **kwargs
        )
        if response.is_redirect or response.is_permanent_redirect:
            location = response.headers.get("Location")
            if not location:
                break
            current_url = urljoin(current_url, location)
            current_params = None  # query already encoded into Location
            continue
        return response

    msg = f"Too many redirects (>{_MAX_REDIRECTS}) starting from {url!r}."
    raise WebFetchError(msg)


def _get_tavily_client() -> TavilyClient | None:
    """Get or initialize the lazy Tavily client singleton.

    Returns:
        TavilyClient instance, or None if API key is not configured.
    """
    global _tavily_client  # noqa: PLW0603  # Module-level cache requires global statement
    if _tavily_client is not _UNSET:
        return _tavily_client  # narrowed by sentinel check

    from bog_agents_cli.config import settings

    if settings.has_tavily:
        from tavily import TavilyClient as _TavilyClient

        _tavily_client = _TavilyClient(api_key=settings.tavily_api_key)
    else:
        _tavily_client = None
    return _tavily_client


def http_request(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: str | dict | None = None,
    params: dict[str, str] | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """Make HTTP requests to APIs and web services.

    Args:
        url: Target URL
        method: HTTP method (GET, POST, PUT, DELETE, etc.)
        headers: HTTP headers to include
        data: Request body data (string or dict)
        params: URL query parameters
        timeout: Request timeout in seconds

    Returns:
        Dictionary with response data including status, headers, and content
    """
    import requests

    from bog_agents_cli.web_fetch import WebFetchError

    try:
        json_body = data if isinstance(data, dict) else None
        raw_body = data if not isinstance(data, dict) else None
        response = _request_with_guarded_redirects(
            method.upper(),
            url,
            headers=headers,
            params=params,
            json_body=json_body,
            data=raw_body,
            timeout=timeout,
        )

        try:
            content = response.json()
        except (ValueError, requests.exceptions.JSONDecodeError):
            content = response.text

        return {
            "success": response.status_code < 400,  # HTTP status code threshold
            "status_code": response.status_code,
            "headers": dict(response.headers),
            "content": content,
            "url": response.url,
        }

    except WebFetchError as e:
        return {
            "success": False,
            "status_code": 0,
            "headers": {},
            "content": f"Request blocked: {e!s}",
            "url": url,
        }
    except requests.exceptions.Timeout:
        return {
            "success": False,
            "status_code": 0,
            "headers": {},
            "content": f"Request timed out after {timeout} seconds",
            "url": url,
        }
    except requests.exceptions.RequestException as e:
        return {
            "success": False,
            "status_code": 0,
            "headers": {},
            "content": f"Request error: {e!s}",
            "url": url,
        }


def web_search(  # noqa: ANN201  # Return type depends on dynamic tool configuration
    query: str,
    max_results: int = 5,
    topic: Literal["general", "news", "finance"] = "general",
    include_raw_content: bool = False,
):
    """Search the web using Tavily for current information and documentation.

    This tool searches the web and returns relevant results. After receiving results,
    you MUST synthesize the information into a natural, helpful response for the user.

    Args:
        query: The search query (be specific and detailed)
        max_results: Number of results to return (default: 5)
        topic: Search topic type - "general" for most queries, "news" for current events
        include_raw_content: Include full page content (warning: uses more tokens)

    Returns:
        Dictionary containing:
        - results: List of search results, each with:
            - title: Page title
            - url: Page URL
            - content: Relevant excerpt from the page
            - score: Relevance score (0-1)
        - query: The original search query

    IMPORTANT: After using this tool:
    1. Read through the 'content' field of each result
    2. Extract relevant information that answers the user's question
    3. Synthesize this into a clear, natural language response
    4. Cite sources by mentioning the page titles or URLs
    5. NEVER show the raw JSON to the user - always provide a formatted response
    """
    ledger = _web_search_ledger
    if ledger is not None:
        decision = ledger.register_web_search()
        if not decision.allowed:
            return {
                "error": f"Web search refused: {decision.reason}. Raise cost.max_web_searches to continue.",
                "query": query,
            }
    try:
        import requests
        from tavily import (
            BadRequestError,
            InvalidAPIKeyError,
            MissingAPIKeyError,
            UsageLimitExceededError,
        )
        from tavily.errors import ForbiddenError, TimeoutError as TavilyTimeoutError
    except ImportError as exc:
        return {
            "error": f"Required package not installed: {exc.name}. "
            "Install with: pip install 'bog-agents[cli]'",
            "query": query,
        }

    client = _get_tavily_client()
    if client is None:
        return {
            "error": "Tavily API key not configured. "
            "Please set TAVILY_API_KEY environment variable.",
            "query": query,
        }

    try:
        return client.search(
            query,
            max_results=max_results,
            include_raw_content=include_raw_content,
            topic=topic,
        )
    except (
        requests.exceptions.RequestException,
        ValueError,
        TypeError,
        # Tavily-specific exceptions
        BadRequestError,
        ForbiddenError,
        InvalidAPIKeyError,
        MissingAPIKeyError,
        TavilyTimeoutError,
        UsageLimitExceededError,
    ) as e:
        return {"error": f"Web search error: {e!s}", "query": query}


def fetch_url(url: str, timeout: int = 30) -> dict[str, Any]:
    """Fetch content from a URL and convert HTML to markdown format.

    This tool fetches web page content and converts it to clean markdown text,
    making it easy to read and process HTML content. After receiving the markdown,
    you MUST synthesize the information into a natural, helpful response for the user.

    Args:
        url: The URL to fetch (must be a valid HTTP/HTTPS URL)
        timeout: Request timeout in seconds (default: 30)

    Returns:
        Dictionary containing:
        - success: Whether the request succeeded
        - url: The final URL after redirects
        - markdown_content: The page content converted to markdown
        - status_code: HTTP status code
        - content_length: Length of the markdown content in characters

    IMPORTANT: After using this tool:
    1. Read through the markdown content
    2. Extract relevant information that answers the user's question
    3. Synthesize this into a clear, natural language response
    4. NEVER show the raw markdown to the user unless specifically requested
    """
    try:
        import requests
        from markdownify import markdownify
    except ImportError as exc:
        return {
            "error": f"Required package not installed: {exc.name}. "
            "Install with: pip install 'bog-agents[cli]'",
            "url": url,
        }

    from bog_agents_cli.web_fetch import WebFetchError

    try:
        response = _request_with_guarded_redirects(
            "GET",
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; Bog Agents/1.0)"},
            timeout=timeout,
        )
        response.raise_for_status()

        # Convert HTML content to markdown
        markdown_content = markdownify(response.text)

        return {
            "url": str(response.url),
            "markdown_content": markdown_content,
            "status_code": response.status_code,
            "content_length": len(markdown_content),
        }
    except WebFetchError as e:
        return {"error": f"Fetch URL blocked: {e!s}", "url": url}
    except requests.exceptions.RequestException as e:
        return {"error": f"Fetch URL error: {e!s}", "url": url}
