"""Built-in web search tool with multiple provider support.

Feature #20: Web search — built-in search beyond Tavily, supporting
multiple search providers.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Provider detection order
SEARCH_PROVIDERS = ["tavily", "serper", "searxng"]

# Environment variable mapping for each provider
PROVIDER_ENV_KEYS: dict[str, str] = {
    "tavily": "TAVILY_API_KEY",
    "serper": "SERPER_API_KEY",
    "searxng": "SEARXNG_URL",
}


def detect_search_provider() -> str | None:
    """Detect the available search provider from environment variables.

    Returns:
        Provider name, or None if no provider is configured.
    """
    for provider in SEARCH_PROVIDERS:
        env_key = PROVIDER_ENV_KEYS[provider]
        if os.environ.get(env_key):
            return provider
    return None


async def web_search(
    query: str,
    *,
    max_results: int = 5,
    provider: str | None = None,
) -> list[dict[str, Any]]:
    """Perform a web search using the configured provider.

    Args:
        query: Search query string.
        max_results: Maximum number of results to return.
        provider: Force a specific provider. Auto-detects if None.

    Returns:
        List of search result dicts with 'title', 'url', 'snippet' keys.

    Raises:
        ValueError: If no search provider is configured.
    """
    provider = provider or detect_search_provider()

    if provider == "tavily":
        return await _search_tavily(query, max_results=max_results)
    if provider == "serper":
        return await _search_serper(query, max_results=max_results)
    if provider == "searxng":
        return await _search_searxng(query, max_results=max_results)
    msg = (
        "No search provider configured. Set one of: "
        "TAVILY_API_KEY, SERPER_API_KEY, or SEARXNG_URL"
    )
    raise ValueError(msg)


async def _search_tavily(query: str, *, max_results: int = 5) -> list[dict[str, Any]]:
    """Search using Tavily API.

    Args:
        query: Search query.
        max_results: Max results.

    Returns:
        List of result dicts.
    """
    import asyncio

    from langchain_community.tools.tavily_search import TavilySearchResults

    tool = TavilySearchResults(max_results=max_results)
    results = await asyncio.to_thread(tool.invoke, query)

    if isinstance(results, list):
        return [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("content", ""),
            }
            for r in results
        ]
    return []


async def _search_serper(query: str, *, max_results: int = 5) -> list[dict[str, Any]]:
    """Search using Serper (Google Search API).

    Args:
        query: Search query.
        max_results: Max results.

    Returns:
        List of result dicts.
    """
    import asyncio
    import json
    import urllib.request

    api_key = os.environ.get("SERPER_API_KEY", "")
    data = json.dumps({"q": query, "num": max_results}).encode()
    req = urllib.request.Request(
        "https://google.serper.dev/search",
        data=data,
        headers={
            "X-API-KEY": api_key,
            "Content-Type": "application/json",
        },
    )

    response = await asyncio.to_thread(
        urllib.request.urlopen,
        req,
        timeout=15,  # noqa: S310
    )
    result = json.loads(response.read())

    return [
        {
            "title": r.get("title", ""),
            "url": r.get("link", ""),
            "snippet": r.get("snippet", ""),
        }
        for r in result.get("organic", [])[:max_results]
    ]


async def _search_searxng(query: str, *, max_results: int = 5) -> list[dict[str, Any]]:
    """Search using a self-hosted SearXNG instance.

    Args:
        query: Search query.
        max_results: Max results.

    Returns:
        List of result dicts.
    """
    import asyncio
    import json
    import urllib.parse
    import urllib.request

    base_url = os.environ.get("SEARXNG_URL", "").rstrip("/")
    params = urllib.parse.urlencode({"q": query, "format": "json"})
    url = f"{base_url}/search?{params}"

    req = urllib.request.Request(url)  # noqa: S310
    response = await asyncio.to_thread(
        urllib.request.urlopen,
        req,
        timeout=15,  # noqa: S310
    )
    result = json.loads(response.read())

    return [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": r.get("content", ""),
        }
        for r in result.get("results", [])[:max_results]
    ]
