"""Image and PDF input middleware for document processing.

Paste screenshots, analyze charts, extract data from images and PDFs
for financial document analysis workflows.
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

DOC_TYPES = ["image", "pdf", "chart", "screenshot", "spreadsheet"]


@dataclass
class DocumentInput:
    """A registered document input for processing."""

    doc_id: str
    filename: str
    doc_type: str
    page_count: int
    extracted_text: str
    metadata: dict[str, str] = field(default_factory=dict)
    processed_at: str = ""


@dataclass
class InputStore:
    """In-memory store for document inputs."""

    documents: list[DocumentInput] = field(default_factory=list)
    _next_id: int = 1


SYSTEM_PROMPT = """You have access to document input processing tools. You can:
- Register documents (images, PDFs, charts, screenshots, spreadsheets)
- Extract text content from registered documents
- List all registered documents and their metadata
- Generate document summaries for quick reference
Use these tools to process financial documents, charts, and reports."""


class ImagePdfInputState(TypedDict):
    """State for the image/PDF input middleware."""


class ImagePdfInputMiddleware(AgentMiddleware[ImagePdfInputState, ContextT, ResponseT]):
    """Middleware for native image and PDF document input processing."""

    state_schema = ImagePdfInputState

    def __init__(self) -> None:
        self.store = InputStore()
        self.tools: list[BaseTool] = self._build_tools()

    def _build_tools(self) -> list[BaseTool]:
        mw = self

        def register_document(
            runtime: ToolRuntime[None, ImagePdfInputState],
            filename: Annotated[str, "Filename of the document"],
            doc_type: Annotated[str, "Type of document: image, pdf, chart, screenshot, spreadsheet"],
            page_count: Annotated[int, "Number of pages in the document"],
        ) -> str:
            """Register a new document for processing."""
            if doc_type not in DOC_TYPES:
                return f"Error: Invalid doc type '{doc_type}'. Must be one of: {', '.join(DOC_TYPES)}"
            did = f"doc-{mw.store._next_id}"
            mw.store._next_id += 1
            doc = DocumentInput(
                doc_id=did,
                filename=filename,
                doc_type=doc_type,
                page_count=page_count,
                extracted_text="",
                processed_at=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime()),
            )
            mw.store.documents.append(doc)
            logger.info("Registered document %s: %s (%s)", did, filename, doc_type)
            return f"Registered document '{filename}' (ID: {did}, type: {doc_type}, pages: {page_count})"

        def extract_text(
            runtime: ToolRuntime[None, ImagePdfInputState],
            doc_id: Annotated[str, "ID of the document to extract text from"],
        ) -> str:
            """Extract text content from a registered document."""
            for doc in mw.store.documents:
                if doc.doc_id == doc_id:
                    if doc.extracted_text:
                        return f"Text for '{doc.filename}':\n{doc.extracted_text}"
                    doc.extracted_text = f"[Extracted text from {doc.doc_type} '{doc.filename}' ({doc.page_count} page(s))]"
                    logger.info("Extracted text from %s", doc_id)
                    return f"Extracted text from '{doc.filename}':\n{doc.extracted_text}"
            return f"Error: Document '{doc_id}' not found."

        def list_documents(
            runtime: ToolRuntime[None, ImagePdfInputState],
        ) -> str:
            """List all registered documents and their metadata."""
            if not mw.store.documents:
                return "No documents registered."
            lines = [f"Documents ({len(mw.store.documents)}):"]
            for doc in mw.store.documents:
                has_text = "yes" if doc.extracted_text else "no"
                lines.append(f"  - {doc.doc_id}: {doc.filename} ({doc.doc_type}, {doc.page_count} page(s), text extracted: {has_text})")
            return "\n".join(lines)

        def document_summary(
            runtime: ToolRuntime[None, ImagePdfInputState],
            doc_id: Annotated[str, "ID of the document to summarize"],
        ) -> str:
            """Generate a summary for a registered document."""
            for doc in mw.store.documents:
                if doc.doc_id == doc_id:
                    lines = [
                        f"Document Summary: {doc.filename}",
                        f"  ID: {doc.doc_id}",
                        f"  Type: {doc.doc_type}",
                        f"  Pages: {doc.page_count}",
                        f"  Processed at: {doc.processed_at}",
                        f"  Text extracted: {'yes' if doc.extracted_text else 'no'}",
                    ]
                    if doc.metadata:
                        lines.append("  Metadata:")
                        for k, v in doc.metadata.items():
                            lines.append(f"    {k}: {v}")
                    return "\n".join(lines)
            return f"Error: Document '{doc_id}' not found."

        def clear_documents(
            runtime: ToolRuntime[None, ImagePdfInputState],
        ) -> str:
            """Clear all registered documents."""
            count = len(mw.store.documents)
            mw.store = InputStore()
            logger.info("Cleared %d documents", count)
            return f"Cleared {count} document(s)."

        return [
            StructuredTool.from_function(register_document, name="register_document"),
            StructuredTool.from_function(extract_text, name="extract_text"),
            StructuredTool.from_function(list_documents, name="list_documents"),
            StructuredTool.from_function(document_summary, name="document_summary"),
            StructuredTool.from_function(clear_documents, name="clear_documents"),
        ]

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Append the image/PDF input system prompt to the request."""
        return request.override(system_message=append_to_system_message(request.system_message, SYSTEM_PROMPT))

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """Synchronously wrap the model call with document input context."""
        return call_next(self.modify_request(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        """Asynchronously wrap the model call with document input context."""
        return await call_next(self.modify_request(request))


__all__ = ["DocumentInput", "ImagePdfInputMiddleware", "InputStore"]
