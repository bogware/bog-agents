"""PDF reading support for the read_file tool.

Feature #9: PDF reading — adds PDF file support with page range selection
to the filesystem middleware's read_file tool.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# PDF extension
PDF_EXTENSION = ".pdf"

# Maximum pages per request
MAX_PAGES_PER_REQUEST = 20

# Default pages to read if not specified
DEFAULT_PAGE_LIMIT = 10


def is_pdf_file(file_path: str) -> bool:
    """Check if a file path is a PDF file.

    Args:
        file_path: Path to check.

    Returns:
        True if the file has a .pdf extension.
    """
    return Path(file_path).suffix.lower() == PDF_EXTENSION


def read_pdf(
    file_path: str,
    *,
    start_page: int = 0,
    max_pages: int = DEFAULT_PAGE_LIMIT,
) -> str:
    """Read text content from a PDF file.

    Attempts to use PyPDF2, then falls back to pdfplumber, then to
    a basic text extraction approach.

    Args:
        file_path: Absolute path to the PDF file.
        start_page: Page number to start reading from (0-indexed).
        max_pages: Maximum number of pages to read.

    Returns:
        Extracted text content with page markers.
    """
    max_pages = min(max_pages, MAX_PAGES_PER_REQUEST)
    end_page = start_page + max_pages

    # Try PyPDF2 first
    try:
        return _read_with_pypdf2(file_path, start_page, end_page)
    except ImportError:
        pass
    except Exception as e:
        logger.debug("PyPDF2 failed for %s: %s", file_path, e)

    # Try pdfplumber
    try:
        return _read_with_pdfplumber(file_path, start_page, end_page)
    except ImportError:
        pass
    except Exception as e:
        logger.debug("pdfplumber failed for %s: %s", file_path, e)

    # Try pymupdf (fitz)
    try:
        return _read_with_pymupdf(file_path, start_page, end_page)
    except ImportError:
        pass
    except Exception as e:
        logger.debug("pymupdf failed for %s: %s", file_path, e)

    return f"Error: Could not read PDF '{file_path}'. Install a PDF library: pip install PyPDF2, pdfplumber, or pymupdf"


def _read_with_pypdf2(file_path: str, start_page: int, end_page: int) -> str:
    """Read PDF using PyPDF2.

    Args:
        file_path: PDF file path.
        start_page: Start page (0-indexed).
        end_page: End page (exclusive).

    Returns:
        Extracted text.
    """
    from PyPDF2 import PdfReader  # type: ignore[import-untyped]

    reader = PdfReader(file_path)
    total_pages = len(reader.pages)
    end_page = min(end_page, total_pages)

    parts = [f"PDF: {file_path} ({total_pages} pages total, showing pages {start_page + 1}-{end_page})\n"]

    for i in range(start_page, end_page):
        page = reader.pages[i]
        text = page.extract_text() or "[No text content on this page]"
        parts.append(f"\n--- Page {i + 1} ---\n{text}")

    if end_page < total_pages:
        parts.append(f"\n... {total_pages - end_page} more pages. Use offset to read more.")

    return "\n".join(parts)


def _read_with_pdfplumber(file_path: str, start_page: int, end_page: int) -> str:
    """Read PDF using pdfplumber.

    Args:
        file_path: PDF file path.
        start_page: Start page (0-indexed).
        end_page: End page (exclusive).

    Returns:
        Extracted text.
    """
    import pdfplumber  # type: ignore[import-untyped]

    with pdfplumber.open(file_path) as pdf:
        total_pages = len(pdf.pages)
        end_page = min(end_page, total_pages)

        parts = [f"PDF: {file_path} ({total_pages} pages total, showing pages {start_page + 1}-{end_page})\n"]

        for i in range(start_page, end_page):
            page = pdf.pages[i]
            text = page.extract_text() or "[No text content on this page]"
            parts.append(f"\n--- Page {i + 1} ---\n{text}")

        if end_page < total_pages:
            parts.append(f"\n... {total_pages - end_page} more pages. Use offset to read more.")

    return "\n".join(parts)


def _read_with_pymupdf(file_path: str, start_page: int, end_page: int) -> str:
    """Read PDF using PyMuPDF (fitz).

    Args:
        file_path: PDF file path.
        start_page: Start page (0-indexed).
        end_page: End page (exclusive).

    Returns:
        Extracted text.
    """
    import fitz  # type: ignore[import-untyped]

    doc = fitz.open(file_path)
    total_pages = len(doc)
    end_page = min(end_page, total_pages)

    parts = [f"PDF: {file_path} ({total_pages} pages total, showing pages {start_page + 1}-{end_page})\n"]

    for i in range(start_page, end_page):
        page = doc[i]
        text = page.get_text() or "[No text content on this page]"
        parts.append(f"\n--- Page {i + 1} ---\n{text}")

    doc.close()

    if end_page < total_pages:
        parts.append(f"\n... {total_pages - end_page} more pages. Use offset to read more.")

    return "\n".join(parts)
