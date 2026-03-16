"""Persistent knowledge base middleware with per-client namespaces.

Feature #20: Isolated client namespaces for storing and retrieving knowledge
items across sessions.

## Overview

The client knowledge base middleware provides tools for:

- Setting the active client context
- Storing knowledge items with categories
- Retrieving knowledge by key
- Listing knowledge filtered by category
- Clearing knowledge for a client

## Knowledge Categories

Supported categories: profile, portfolio, preferences, notes, history.

## Usage

```python
from bog_agents.middleware.client_knowledge_base import ClientKnowledgeBaseMiddleware

middleware = ClientKnowledgeBaseMiddleware()
```
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)
from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, StructuredTool
from typing_extensions import TypedDict

from bog_agents.middleware._utils import append_to_system_message

logger = logging.getLogger(__name__)


@dataclass
class KnowledgeItem:
    """A single knowledge item stored in a client namespace.

    Attributes:
        key: Unique key for this item within the namespace.
        value: The knowledge content.
        category: Category (profile, portfolio, preferences, notes, history).
        created_at: ISO 8601 timestamp when the item was created.
        updated_at: ISO 8601 timestamp when the item was last updated.
    """

    key: str
    value: str
    category: str
    created_at: str
    updated_at: str


@dataclass
class ClientNamespace:
    """An isolated namespace for a single client.

    Attributes:
        client_id: Unique identifier for this client.
        items: Map of key to KnowledgeItem.
    """

    client_id: str
    items: dict[str, KnowledgeItem] = field(default_factory=dict)

    def store(
        self,
        *,
        key: str,
        value: str,
        category: str,
    ) -> KnowledgeItem:
        """Store or update a knowledge item.

        Args:
            key: Unique key for the item.
            value: The knowledge content.
            category: Category for the item.

        Returns:
            The stored or updated knowledge item.
        """
        now = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime())
        if key in self.items:
            item = self.items[key]
            item.value = value
            item.category = category
            item.updated_at = now
            logger.debug("Updated knowledge item '%s' for client '%s'", key, self.client_id)
        else:
            item = KnowledgeItem(
                key=key,
                value=value,
                category=category,
                created_at=now,
                updated_at=now,
            )
            self.items[key] = item
            logger.debug("Stored knowledge item '%s' for client '%s'", key, self.client_id)
        return item

    def retrieve(self, key: str) -> KnowledgeItem | None:
        """Retrieve a knowledge item by key.

        Args:
            key: The item key.

        Returns:
            The knowledge item, or None if not found.
        """
        return self.items.get(key)

    def list_items(self, category: str = "") -> list[KnowledgeItem]:
        """List all knowledge items, optionally filtered by category.

        Args:
            category: Optional category filter. Empty string means all.

        Returns:
            List of matching knowledge items.
        """
        if category:
            return [item for item in self.items.values() if item.category == category]
        return list(self.items.values())

    def format_listing(self, category: str = "") -> str:
        """Format a human-readable listing of knowledge items.

        Args:
            category: Optional category filter.

        Returns:
            Formatted listing string.
        """
        items = self.list_items(category)
        if not items:
            filter_msg = f" in category '{category}'" if category else ""
            return f"No knowledge items found{filter_msg} for client '{self.client_id}'."

        lines = [
            f"## Knowledge Base: {self.client_id}",
            f"Items: {len(items)}",
            "",
        ]

        # Group by category
        by_category: dict[str, list[KnowledgeItem]] = {}
        for item in items:
            by_category.setdefault(item.category, []).append(item)

        for cat, cat_items in sorted(by_category.items()):
            lines.append(f"### {cat}")
            for item in cat_items:
                lines.append(f"- **{item.key}**: {item.value[:100]}")
                lines.append(f"  Updated: {item.updated_at}")
            lines.append("")

        return "\n".join(lines)


@dataclass
class KnowledgeBaseStore:
    """Top-level store managing multiple client namespaces.

    Attributes:
        namespaces: Map of client_id to ClientNamespace.
        active_client: Currently active client ID.
    """

    namespaces: dict[str, ClientNamespace] = field(default_factory=dict)
    active_client: str = ""

    def set_active(self, client_id: str) -> ClientNamespace:
        """Set the active client namespace, creating it if needed.

        Args:
            client_id: The client identifier.

        Returns:
            The active client namespace.
        """
        if client_id not in self.namespaces:
            self.namespaces[client_id] = ClientNamespace(client_id=client_id)
            logger.debug("Created namespace for client '%s'", client_id)
        self.active_client = client_id
        return self.namespaces[client_id]

    def get_active_namespace(self) -> ClientNamespace | None:
        """Get the currently active client namespace.

        Returns:
            The active namespace, or None if no client is active.
        """
        if not self.active_client:
            return None
        return self.namespaces.get(self.active_client)

    def format_status(self) -> str:
        """Format a status overview of the knowledge base store.

        Returns:
            Formatted status string.
        """
        lines = [
            "## Knowledge Base Status",
            f"Active client: {self.active_client or '(none)'}",
            f"Total clients: {len(self.namespaces)}",
            "",
        ]
        for client_id, ns in sorted(self.namespaces.items()):
            active = " (active)" if client_id == self.active_client else ""
            lines.append(f"- {client_id}: {len(ns.items)} items{active}")

        return "\n".join(lines)


CLIENT_KB_SYSTEM_PROMPT = """## Client Knowledge Base

You have access to a persistent knowledge base with per-client namespaces.

**Workflow:**
1. Use `set_client_context` to select the active client
2. Use `store_knowledge` to save information (profile, portfolio, preferences, notes, history)
3. Use `retrieve_knowledge` to look up stored items by key
4. Use `list_knowledge` to browse all items, optionally filtered by category

Always set the client context before storing or retrieving knowledge."""


class ClientKnowledgeBaseState(TypedDict):
    """State for client knowledge base middleware."""


class ClientKnowledgeBaseMiddleware(AgentMiddleware[ClientKnowledgeBaseState, ContextT, ResponseT]):
    """Middleware for persistent per-client knowledge storage.

    Provides tools for managing isolated client namespaces with categorized
    knowledge items.
    """

    state_schema = ClientKnowledgeBaseState

    def __init__(self) -> None:
        self.store = KnowledgeBaseStore()
        self.tools: list[BaseTool] = self._build_tools()

    def _build_tools(self) -> list[BaseTool]:
        """Build client knowledge base tools."""
        mw = self

        def set_client_context(
            runtime: ToolRuntime[None, ClientKnowledgeBaseState],
            client_id: Annotated[str, "Unique identifier for the client"],
        ) -> str:
            """Set the active client namespace. Creates the namespace if it does not exist."""
            ns = mw.store.set_active(client_id)
            return f"Active client set to '{client_id}' ({len(ns.items)} existing items)."

        def store_knowledge(
            runtime: ToolRuntime[None, ClientKnowledgeBaseState],
            key: Annotated[str, "Unique key for the knowledge item"],
            value: Annotated[str, "The knowledge content to store"],
            category: Annotated[str, "Category: profile, portfolio, preferences, notes, or history"] = "notes",
        ) -> str:
            """Store a knowledge item for the active client."""
            ns = mw.store.get_active_namespace()
            if ns is None:
                return "Error: No active client. Use `set_client_context` first."
            item = ns.store(key=key, value=value, category=category)
            return f"Stored [{category}] '{key}' for client '{ns.client_id}' (updated: {item.updated_at})."

        def retrieve_knowledge(
            runtime: ToolRuntime[None, ClientKnowledgeBaseState],
            key: Annotated[str, "Key of the knowledge item to retrieve"],
        ) -> str:
            """Retrieve a knowledge item by key for the active client."""
            ns = mw.store.get_active_namespace()
            if ns is None:
                return "Error: No active client. Use `set_client_context` first."
            item = ns.retrieve(key)
            if item is None:
                return f"Knowledge item '{key}' not found for client '{ns.client_id}'."
            return f"## {item.key}\nCategory: {item.category}\nCreated: {item.created_at}\nUpdated: {item.updated_at}\n\n{item.value}"

        def list_knowledge(
            runtime: ToolRuntime[None, ClientKnowledgeBaseState],
            category: Annotated[str, "Optional category filter (profile, portfolio, preferences, notes, history)"] = "",
        ) -> str:
            """List all knowledge items for the active client, optionally filtered by category."""
            ns = mw.store.get_active_namespace()
            if ns is None:
                return "Error: No active client. Use `set_client_context` first."
            return ns.format_listing(category)

        def clear_client_knowledge(
            runtime: ToolRuntime[None, ClientKnowledgeBaseState],
        ) -> str:
            """Clear all knowledge items for the active client."""
            ns = mw.store.get_active_namespace()
            if ns is None:
                return "Error: No active client. Use `set_client_context` first."
            count = len(ns.items)
            ns.items.clear()
            return f"Cleared {count} knowledge items for client '{ns.client_id}'."

        return [
            StructuredTool.from_function(
                name="set_client_context",
                description="Set the active client namespace. Creates the namespace if it does not exist.",
                func=set_client_context,
            ),
            StructuredTool.from_function(
                name="store_knowledge",
                description="Store a knowledge item for the active client with a key, value, and category.",
                func=store_knowledge,
            ),
            StructuredTool.from_function(
                name="retrieve_knowledge",
                description="Retrieve a knowledge item by key for the active client.",
                func=retrieve_knowledge,
            ),
            StructuredTool.from_function(
                name="list_knowledge",
                description="List all knowledge items for the active client, optionally filtered by category.",
                func=list_knowledge,
            ),
            StructuredTool.from_function(
                name="clear_client_knowledge",
                description="Clear all knowledge items for the active client.",
                func=clear_client_knowledge,
            ),
        ]

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Inject client knowledge base instructions into the system prompt.

        Args:
            request: Model request to modify.

        Returns:
            Modified request with knowledge base instructions.
        """
        new_system_message = append_to_system_message(request.system_message, CLIENT_KB_SYSTEM_PROMPT)
        return request.override(system_message=new_system_message)

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """Inject client knowledge base instructions.

        Args:
            request: Model request.
            call_next: Handler function.

        Returns:
            Model response.
        """
        modified = self.modify_request(request)
        return call_next(modified)

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        """Async version of wrap_model_call.

        Args:
            request: Model request.
            call_next: Async handler function.

        Returns:
            Model response.
        """
        modified = self.modify_request(request)
        return await call_next(modified)


__all__ = [
    "ClientKnowledgeBaseMiddleware",
    "ClientNamespace",
    "KnowledgeBaseStore",
    "KnowledgeItem",
]
