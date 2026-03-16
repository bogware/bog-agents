"""OpenSearch RAG middleware.

Feature #16: Hybrid search over firm documents with keyword matching,
document indexing, and search history tracking.

## Tools

- `index_document`: Index a document into the RAG store
- `search_documents`: Search indexed documents by keyword query
- `list_indexed`: List all indexed documents
- `search_history`: View recent search queries and results
- `clear_index`: Clear all indexed documents and search history

## Usage

```python
from bog_agents.middleware.opensearch_rag import OpenSearchRAGMiddleware

middleware = OpenSearchRAGMiddleware()
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
class RAGDocument:
    """An indexed document in the RAG store.

    Attributes:
        doc_id: Unique document identifier.
        title: Document title.
        content: Full document content.
        source: Document source or origin.
        doc_type: Type of document (e.g., report, memo, policy).
        metadata: Additional metadata key-value pairs.
        indexed_at: ISO timestamp when the document was indexed.
    """

    doc_id: str
    title: str
    content: str
    source: str = ""
    doc_type: str = ""
    metadata: dict[str, str] = field(default_factory=dict)
    indexed_at: str = ""


@dataclass
class RAGResult:
    """A single search result from a RAG query.

    Attributes:
        doc_id: Document identifier.
        title: Document title.
        snippet: Matching text snippet.
        score: Relevance score.
        source: Document source.
    """

    doc_id: str
    title: str
    snippet: str
    score: float
    source: str


@dataclass
class RAGQuery:
    """A recorded search query with results.

    Attributes:
        query_id: Unique query identifier.
        query_text: The search query text.
        results: List of search results.
        timestamp: ISO timestamp of the query.
    """

    query_id: str
    query_text: str
    results: list[RAGResult] = field(default_factory=list)
    timestamp: str = ""


@dataclass
class RAGStore:
    """In-memory store for RAG documents and queries.

    Attributes:
        documents: Indexed documents keyed by doc_id.
        queries: History of search queries.
        _next_doc_id: Counter for generating document IDs.
        _next_query_id: Counter for generating query IDs.
    """

    documents: dict[str, RAGDocument] = field(default_factory=dict)
    queries: list[RAGQuery] = field(default_factory=list)
    _next_doc_id: int = 1
    _next_query_id: int = 1

    def index_document(
        self,
        title: str,
        content: str,
        source: str = "",
        doc_type: str = "",
        metadata: dict[str, str] | None = None,
    ) -> RAGDocument:
        """Index a new document into the store.

        Args:
            title: Document title.
            content: Full document content.
            source: Document source or origin.
            doc_type: Type of document.
            metadata: Additional metadata.

        Returns:
            The indexed document.
        """
        doc_id = f"doc-{self._next_doc_id}"
        self._next_doc_id += 1
        doc = RAGDocument(
            doc_id=doc_id,
            title=title,
            content=content,
            source=source,
            doc_type=doc_type,
            metadata=metadata or {},
            indexed_at=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime()),
        )
        self.documents[doc_id] = doc
        return doc

    def search(self, query_text: str, top_k: int = 5) -> RAGQuery:
        """Search indexed documents by keyword matching on title and content.

        Args:
            query_text: The search query.
            top_k: Maximum number of results to return.

        Returns:
            A RAGQuery with matched results.
        """
        query_id = f"q-{self._next_query_id}"
        self._next_query_id += 1

        terms = query_text.lower().split()
        scored: list[tuple[float, RAGDocument]] = []

        for doc in self.documents.values():
            score = 0.0
            title_lower = doc.title.lower()
            content_lower = doc.content.lower()
            for term in terms:
                if term in title_lower:
                    score += 2.0
                if term in content_lower:
                    score += 1.0
            if score > 0:
                scored.append((score, doc))

        scored.sort(key=lambda x: -x[0])
        results = []
        for score, doc in scored[:top_k]:
            snippet = doc.content[:200] + ("..." if len(doc.content) > 200 else "")
            results.append(
                RAGResult(
                    doc_id=doc.doc_id,
                    title=doc.title,
                    snippet=snippet,
                    score=score,
                    source=doc.source,
                )
            )

        query = RAGQuery(
            query_id=query_id,
            query_text=query_text,
            results=results,
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime()),
        )
        self.queries.append(query)
        return query

    def format_results(self, query: RAGQuery) -> str:
        """Format search results for display.

        Args:
            query: The RAG query with results.

        Returns:
            Formatted string of search results.
        """
        if not query.results:
            return f"No results found for: {query.query_text}"
        lines = [
            f"## Search Results for: {query.query_text}",
            f"Found {len(query.results)} result(s)",
            "",
        ]
        for i, result in enumerate(query.results, 1):
            lines.append(f"### {i}. {result.title} (score: {result.score:.1f})")
            lines.append(f"   ID: {result.doc_id} | Source: {result.source}")
            lines.append(f"   {result.snippet}")
            lines.append("")
        return "\n".join(lines)


RAG_SYSTEM_PROMPT = """## OpenSearch RAG Tools

You have access to a document retrieval system for searching firm documents.

**Available Tools:**
- `index_document`: Add documents to the search index
- `search_documents`: Search indexed documents by keyword
- `list_indexed`: View all indexed documents
- `search_history`: Review past search queries
- `clear_index`: Reset the document index

**Workflow:**
1. Index relevant documents using `index_document`
2. Search with `search_documents` to find relevant information
3. Use `search_history` to review past queries

Always cite document sources when presenting information from search results."""


class OpenSearchRAGState(TypedDict):
    """State for OpenSearch RAG middleware."""


class OpenSearchRAGMiddleware(AgentMiddleware[OpenSearchRAGState, ContextT, ResponseT]):
    """Middleware for hybrid document search and retrieval.

    Provides tools for indexing firm documents and performing keyword-based
    search with relevance scoring and search history tracking.
    """

    state_schema = OpenSearchRAGState

    def __init__(self) -> None:
        self.store = RAGStore()
        self.tools: list[BaseTool] = self._build_tools()

    def _build_tools(self) -> list[BaseTool]:
        """Build RAG search tools."""
        mw = self

        def index_document(
            runtime: ToolRuntime[None, OpenSearchRAGState],
            title: Annotated[str, "Document title"],
            content: Annotated[str, "Full document content"],
            source: Annotated[str, "Document source or origin"] = "",
            doc_type: Annotated[str, "Document type (report, memo, policy, etc.)"] = "",
        ) -> str:
            """Index a document into the RAG store for later search."""
            doc = mw.store.index_document(
                title=title,
                content=content,
                source=source,
                doc_type=doc_type,
            )
            return f"Indexed document '{doc.title}' as {doc.doc_id}. Total documents: {len(mw.store.documents)}"

        def search_documents(
            runtime: ToolRuntime[None, OpenSearchRAGState],
            query: Annotated[str, "Search query text"],
            top_k: Annotated[int, "Maximum number of results to return"] = 5,
        ) -> str:
            """Search indexed documents by keyword query."""
            result = mw.store.search(query_text=query, top_k=top_k)
            return mw.store.format_results(result)

        def list_indexed(
            runtime: ToolRuntime[None, OpenSearchRAGState],
        ) -> str:
            """List all indexed documents."""
            if not mw.store.documents:
                return "No documents indexed."
            lines = [f"## Indexed Documents ({len(mw.store.documents)})", ""]
            for doc in mw.store.documents.values():
                lines.append(f"- **{doc.doc_id}**: {doc.title}")
                lines.append(f"  Type: {doc.doc_type or 'N/A'} | Source: {doc.source or 'N/A'} | Indexed: {doc.indexed_at}")
            return "\n".join(lines)

        def search_history(
            runtime: ToolRuntime[None, OpenSearchRAGState],
        ) -> str:
            """View recent search queries and their results."""
            if not mw.store.queries:
                return "No search history."
            lines = [f"## Search History ({len(mw.store.queries)} queries)", ""]
            for query in mw.store.queries[-10:]:
                lines.append(f'- **{query.query_id}**: "{query.query_text}" ({len(query.results)} results) at {query.timestamp}')
            return "\n".join(lines)

        def clear_index(
            runtime: ToolRuntime[None, OpenSearchRAGState],
        ) -> str:
            """Clear all indexed documents and search history."""
            count = len(mw.store.documents)
            mw.store = RAGStore()
            return f"Cleared {count} document(s) and all search history."

        return [
            StructuredTool.from_function(
                name="index_document", description="Index a document into the RAG store for keyword search.", func=index_document
            ),
            StructuredTool.from_function(
                name="search_documents", description="Search indexed documents by keyword query with relevance scoring.", func=search_documents
            ),
            StructuredTool.from_function(name="list_indexed", description="List all indexed documents with metadata.", func=list_indexed),
            StructuredTool.from_function(name="search_history", description="View recent search queries and result counts.", func=search_history),
            StructuredTool.from_function(name="clear_index", description="Clear all indexed documents and search history.", func=clear_index),
        ]

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Inject RAG search instructions.

        Args:
            request: Model request to modify.

        Returns:
            Modified request.
        """
        return request.override(system_message=append_to_system_message(request.system_message, RAG_SYSTEM_PROMPT))

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """Inject RAG search instructions.

        Args:
            request: Model request.
            call_next: Handler.

        Returns:
            Model response.
        """
        return call_next(self.modify_request(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        """Async version.

        Args:
            request: Model request.
            call_next: Async handler.

        Returns:
            Model response.
        """
        return await call_next(self.modify_request(request))


__all__ = ["OpenSearchRAGMiddleware", "RAGDocument", "RAGQuery", "RAGResult", "RAGStore"]
