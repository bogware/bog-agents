"""Image and multimodal input CLI interface.

Feature #19: Screenshot/image input.
Feature #20: Screenshot-to-code.
Feature #21: Visual diff review.
Feature #22: Diagram generation.
Feature #23: Clipboard image paste.
"""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path

logger = logging.getLogger(__name__)

# Supported image extensions
SUPPORTED_IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}


def is_image_file(path: str | Path) -> bool:
    """Check if a file is an image.

    Args:
        path: File path.

    Returns:
        True if the file is a supported image type.
    """
    return Path(path).suffix.lower() in SUPPORTED_IMAGE_TYPES


def detect_image_in_input(text: str) -> str | None:
    """Detect image file references in user input.

    Looks for @file.png or file path patterns.

    Args:
        text: User input text.

    Returns:
        Image path if detected, None otherwise.
    """
    import re

    # Match @path/to/image.png or plain paths
    for match in re.finditer(
        r"@?(\S+\.(?:png|jpg|jpeg|gif|webp|bmp|svg))", text, re.IGNORECASE
    ):
        path = match.group(1)
        if Path(path).exists():
            return path
    return None


def format_image_info(path: str | Path) -> str:
    """Format image file information.

    Args:
        path: Path to image file.

    Returns:
        Formatted info string.
    """
    p = Path(path)
    if not p.exists():
        return f"Image not found: {path}"

    mime_type, _ = mimetypes.guess_type(str(p))
    size = p.stat().st_size

    size_str = f"{size:,} bytes"
    if size > 1024 * 1024:
        size_str = f"{size / (1024 * 1024):.1f} MB"
    elif size > 1024:
        size_str = f"{size / 1024:.1f} KB"

    return f"Image: {p.name} ({mime_type or 'unknown'}, {size_str})"


def parse_image_command(text: str) -> dict[str, str]:
    """Parse an /image command.

    Subcommands:
    - /image analyze <path> — analyze an image
    - /image paste — paste from clipboard
    - /image diagram <type> <description> — generate diagram
    - /image to-code <path> [framework] — convert to code

    Args:
        text: Command text after /image.

    Returns:
        Parsed command dict.
    """
    parts = text.strip().split(maxsplit=2)
    action = parts[0] if parts else "analyze"
    arg1 = parts[1] if len(parts) > 1 else ""
    arg2 = parts[2] if len(parts) > 2 else ""
    return {"action": action, "arg1": arg1, "arg2": arg2}
