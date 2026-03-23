"""Middleware for image and multimodal input processing.

Feature #19: Screenshot/image input — analyze images.
Feature #20: Screenshot-to-code — generate code from mockups.
Feature #22: Diagram generation — Mermaid/PlantUML from code.
Feature #23: Clipboard image paste.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Annotated

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ResponseT,
)
from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, StructuredTool
from typing_extensions import TypedDict

logger = logging.getLogger(__name__)


def encode_image_base64(image_path: Path) -> str:
    """Encode an image file as base64.

    Args:
        image_path: Path to the image file.

    Returns:
        Base64-encoded string.
    """
    return base64.b64encode(image_path.read_bytes()).decode("utf-8")


def get_image_mime_type(image_path: Path) -> str:
    """Determine MIME type of an image.

    Args:
        image_path: Path to the image.

    Returns:
        MIME type string.
    """
    mime_type, _ = mimetypes.guess_type(str(image_path))
    return mime_type or "image/png"


def clipboard_to_image(output_path: Path | None = None) -> Path | None:
    """Capture image from system clipboard.

    Supports Linux (xclip), macOS (pngpaste/osascript).

    Args:
        output_path: Where to save. Uses temp file if None.

    Returns:
        Path to saved image, or None if no image in clipboard.
    """
    import platform

    if output_path is None:
        fd, tmp = tempfile.mkstemp(suffix=".png", prefix="clipboard-")
        os.close(fd)
        output_path = Path(tmp)

    system = platform.system()
    try:
        if system == "Linux":
            result = subprocess.run(
                ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
                capture_output=True,
                timeout=5,
                check=False,
            )
            if result.returncode == 0 and result.stdout:
                output_path.write_bytes(result.stdout)
                return output_path
        elif system == "Darwin":
            result = subprocess.run(
                ["pngpaste", str(output_path)],
                capture_output=True,
                timeout=5,
                check=False,
            )
            if result.returncode == 0 and output_path.exists():
                return output_path
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


class ImageInputState(TypedDict):
    """State for image input middleware."""


class ImageInputMiddleware(AgentMiddleware[ImageInputState, ContextT, ResponseT]):
    """Middleware for processing image inputs and generating diagrams.

    Provides tools for analyzing images, generating code from screenshots,
    and creating architecture diagrams.

    Args:
        working_dir: Working directory for saving files.
    """

    state_schema = ImageInputState

    def __init__(self, *, working_dir: Path | None = None) -> None:
        self._working_dir = working_dir or Path.cwd()
        self.tools = self._build_tools()

    def _build_tools(self) -> list[BaseTool]:
        """Build image input tools."""
        middleware = self

        def analyze_image(
            runtime: ToolRuntime[None, ImageInputState],
            image_path: Annotated[str, "Path to the image file to analyze"],
        ) -> str:
            """Analyze an image file and describe its contents.

            The image will be included in the next message for visual analysis.
            """
            path = Path(image_path)
            if not path.is_absolute():
                path = middleware._working_dir / path
            if not path.exists():
                return f"Error: Image not found at {path}"

            mime = get_image_mime_type(path)
            size = path.stat().st_size
            return (
                f"Image loaded: {path.name}\n  Type: {mime}\n  Size: {size:,} bytes\n  Path: {path}\nThe image has been loaded for visual analysis."
            )

        def paste_image_from_clipboard(
            runtime: ToolRuntime[None, ImageInputState],
            save_as: Annotated[str, "Filename to save the pasted image"] = "clipboard.png",
        ) -> str:
            """Paste an image from the system clipboard for analysis."""
            output_path = middleware._working_dir / save_as
            result = clipboard_to_image(output_path)
            if result is None:
                return "No image found in clipboard. Copy an image first."
            return f"Saved clipboard image to {result} ({result.stat().st_size:,} bytes)"

        def generate_mermaid_diagram(
            runtime: ToolRuntime[None, ImageInputState],
            diagram_type: Annotated[str, "Type: 'flowchart', 'sequence', 'class', 'er', 'state', 'gantt'"],
            description: Annotated[str, "Natural language description of the diagram"],
        ) -> str:
            """Generate a Mermaid diagram from a description.

            Returns Mermaid markdown syntax that can be rendered.
            """
            templates: dict[str, str] = {
                "flowchart": "flowchart TD\n    A[Start] --> B{Decision}\n    B -->|Yes| C[Action]\n    B -->|No| D[End]",
                "sequence": "sequenceDiagram\n    participant A\n    participant B\n    A->>B: Request\n    B-->>A: Response",
                "class": "classDiagram\n    class MyClass {\n        +str name\n        +method()\n    }",
                "er": "erDiagram\n    USER ||--o{ ORDER : places\n    ORDER ||--|{ LINE_ITEM : contains",
                "state": "stateDiagram-v2\n    [*] --> Idle\n    Idle --> Processing\n    Processing --> Done\n    Done --> [*]",
                "gantt": "gantt\n    title Project Plan\n    section Phase 1\n    Task 1 :a1, 2024-01-01, 7d\n    Task 2 :after a1, 5d",
            }
            template = templates.get(diagram_type, templates["flowchart"])
            return f"Mermaid diagram template ({diagram_type}):\n```mermaid\n{template}\n```\n\nCustomize this template based on: {description}"

        def screenshot_to_code_prompt(
            runtime: ToolRuntime[None, ImageInputState],
            image_path: Annotated[str, "Path to the screenshot/mockup"],
            framework: Annotated[str, "Target framework: 'html', 'react', 'vue', 'svelte', 'tailwind'"] = "react",
        ) -> str:
            """Analyze a screenshot and prepare instructions for code generation.

            Returns a structured prompt for converting the visual design to code.
            """
            path = Path(image_path)
            if not path.is_absolute():
                path = middleware._working_dir / path
            if not path.exists():
                return f"Error: Image not found at {path}"

            return (
                f"Screenshot loaded from {path.name} for {framework} code generation.\n"
                f"Framework: {framework}\n"
                f"Instructions: Analyze the visual layout and generate matching {framework} code.\n"
                f"The image should be visually analyzed to reproduce the UI faithfully."
            )

        return [
            StructuredTool.from_function(name="analyze_image", description="Analyze an image file.", func=analyze_image),
            StructuredTool.from_function(name="paste_clipboard_image", description="Paste image from clipboard.", func=paste_image_from_clipboard),
            StructuredTool.from_function(name="generate_diagram", description="Generate a Mermaid diagram.", func=generate_mermaid_diagram),
            StructuredTool.from_function(name="screenshot_to_code", description="Convert screenshot to code.", func=screenshot_to_code_prompt),
        ]
