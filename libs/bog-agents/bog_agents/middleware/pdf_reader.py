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
    data: bytes | None = None,
    start_page: int = 0,
    max_pages: int = DEFAULT_PAGE_LIMIT,
) -> str:
    """Read text content from a PDF.

    Attempts to use pypdf/PyPDF2, then falls back to pdfplumber, then pymupdf.
    When `data` is provided the PDF is parsed from those bytes (so this works
    for non-local backends that hand back file content); otherwise `file_path`
    is opened directly. `file_path` is always used for display/labelling.

    Args:
        file_path: Path to the PDF (used for labels; opened when `data` is None).
        data: Raw PDF bytes to parse instead of opening `file_path`.
        start_page: Page number to start reading from (0-indexed).
        max_pages: Maximum number of pages to read.

    Returns:
        Extracted text content with page markers, or a clear error string.
    """
    max_pages = min(max_pages, MAX_PAGES_PER_REQUEST)
    end_page = start_page + max_pages

    # Try pypdf / PyPDF2 first
    try:
        return _read_with_pypdf2(file_path, start_page, end_page, data=data)
    except ImportError:
        pass
    except Exception as e:
        logger.debug("pypdf failed for %s: %s", file_path, e)

    # Try pdfplumber
    try:
        return _read_with_pdfplumber(file_path, start_page, end_page, data=data)
    except ImportError:
        pass
    except Exception as e:
        logger.debug("pdfplumber failed for %s: %s", file_path, e)

    # Try pymupdf (fitz)
    try:
        return _read_with_pymupdf(file_path, start_page, end_page, data=data)
    except ImportError:
        pass
    except Exception as e:
        logger.debug("pymupdf failed for %s: %s", file_path, e)

    return (
        f"Error: Could not read PDF '{file_path}'. Install the PDF extra: pip install 'bog-agents[pdf]' (or pip install pypdf / pdfplumber / pymupdf)"
    )


def _read_with_pypdf2(file_path: str, start_page: int, end_page: int, *, data: bytes | None = None) -> str:
    """Read PDF using pypdf (falling back to the legacy PyPDF2 import).

    Args:
        file_path: PDF file path (used when `data` is None).
        start_page: Start page (0-indexed).
        end_page: End page (exclusive).
        data: Raw PDF bytes to parse instead of opening `file_path`.

    Returns:
        Extracted text.
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        from PyPDF2 import PdfReader

    import io

    source: object = io.BytesIO(data) if data is not None else file_path
    reader = PdfReader(source)
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


def _read_with_pdfplumber(file_path: str, start_page: int, end_page: int, *, data: bytes | None = None) -> str:
    """Read PDF using pdfplumber.

    Args:
        file_path: PDF file path (used when `data` is None).
        start_page: Start page (0-indexed).
        end_page: End page (exclusive).
        data: Raw PDF bytes to parse instead of opening `file_path`.

    Returns:
        Extracted text.
    """
    import io

    import pdfplumber

    source: object = io.BytesIO(data) if data is not None else file_path
    with pdfplumber.open(source) as pdf:
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


def _read_with_pymupdf(file_path: str, start_page: int, end_page: int, *, data: bytes | None = None) -> str:
    """Read PDF using PyMuPDF (fitz).

    Args:
        file_path: PDF file path (used when `data` is None).
        start_page: Start page (0-indexed).
        end_page: End page (exclusive).
        data: Raw PDF bytes to parse instead of opening `file_path`.

    Returns:
        Extracted text.
    """
    import fitz

    doc = fitz.open(stream=data, filetype="pdf") if data is not None else fitz.open(file_path)
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
