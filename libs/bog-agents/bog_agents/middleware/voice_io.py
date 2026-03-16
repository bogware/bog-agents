"""Voice Input/Output middleware for hands-free research queries."""

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
class VoiceCommand:
    """A processed voice input command."""

    cmd_id: int
    transcript: str
    confidence: float
    language: str
    processed_at: str


@dataclass
class VoiceResponse:
    """A generated voice output response."""

    response_id: int
    text: str
    audio_format: str  # mp3, wav, ogg
    duration_secs: float
    created_at: str


@dataclass
class VoiceStore:
    """Storage for voice commands and responses."""

    commands: list[VoiceCommand] = field(default_factory=list)
    responses: list[VoiceResponse] = field(default_factory=list)
    active_language: str = "en"
    _next_cmd_id: int = 1
    _next_resp_id: int = 1


SYSTEM_PROMPT = """You have access to voice input/output tools for hands-free research queries. \
Supported languages: en, es, fr, de, zh, ja. Supported audio formats: mp3, wav, ogg. Use these \
tools to process voice input, generate voice responses, configure language, and review history."""


class VoiceIOState(TypedDict):
    """State for voice I/O middleware."""


class VoiceIOMiddleware(AgentMiddleware[VoiceIOState, ContextT, ResponseT]):
    """Middleware enabling voice input and output for hands-free interaction."""

    state_schema = VoiceIOState

    def __init__(self) -> None:
        self.store = VoiceStore()
        self.tools: list[BaseTool] = self._build_tools()

    def _build_tools(self) -> list[BaseTool]:
        """Build the voice I/O tools."""
        mw = self

        def process_voice_input(
            runtime: ToolRuntime[None, VoiceIOState],
            transcript: Annotated[str, "Transcribed text from voice input"],
            confidence: Annotated[float, "Confidence score between 0.0 and 1.0"],
            language: Annotated[str, "Language code: en, es, fr, de, zh, or ja"] = "",
        ) -> str:
            """Process a voice input command from transcribed audio."""
            lang = language or mw.store.active_language
            if lang not in ("en", "es", "fr", "de", "zh", "ja"):
                return f"Unsupported language: {lang}. Supported: en, es, fr, de, zh, ja."
            if not 0.0 <= confidence <= 1.0:
                return "Confidence must be between 0.0 and 1.0."
            cmd = VoiceCommand(
                cmd_id=mw.store._next_cmd_id,
                transcript=transcript,
                confidence=confidence,
                language=lang,
                processed_at=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime()),
            )
            mw.store._next_cmd_id += 1
            mw.store.commands.append(cmd)
            logger.info("Processed voice command %d (lang=%s, conf=%.2f)", cmd.cmd_id, lang, confidence)
            return f"Voice command #{cmd.cmd_id} processed: '{transcript}' (language: {lang}, confidence: {confidence:.2f})."

        def generate_voice_response(
            runtime: ToolRuntime[None, VoiceIOState],
            text: Annotated[str, "Text content to convert to voice output"],
            audio_format: Annotated[str, "Audio format: mp3, wav, or ogg"] = "mp3",
            duration_secs: Annotated[float, "Estimated duration in seconds"] = 0.0,
        ) -> str:
            """Generate a voice response from text."""
            if audio_format not in ("mp3", "wav", "ogg"):
                return f"Unsupported audio format: {audio_format}. Supported: mp3, wav, ogg."
            resp = VoiceResponse(
                response_id=mw.store._next_resp_id,
                text=text,
                audio_format=audio_format,
                duration_secs=duration_secs,
                created_at=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime()),
            )
            mw.store._next_resp_id += 1
            mw.store.responses.append(resp)
            logger.info("Generated voice response %d (%s, %.1fs)", resp.response_id, audio_format, duration_secs)
            return f"Voice response #{resp.response_id} generated: {audio_format} format, {duration_secs:.1f}s duration."

        def set_voice_language(
            runtime: ToolRuntime[None, VoiceIOState],
            language: Annotated[str, "Language code: en, es, fr, de, zh, or ja"],
        ) -> str:
            """Set the active language for voice processing."""
            if language not in ("en", "es", "fr", "de", "zh", "ja"):
                return f"Unsupported language: {language}. Supported: en, es, fr, de, zh, ja."
            old = mw.store.active_language
            mw.store.active_language = language
            logger.info("Voice language changed from %s to %s", old, language)
            return f"Voice language changed from '{old}' to '{language}'."

        def voice_history(
            runtime: ToolRuntime[None, VoiceIOState],
        ) -> str:
            """Get voice command and response history."""
            lines = [f"# Voice History (language: {mw.store.active_language})", ""]
            lines.append("## Commands")
            if not mw.store.commands:
                lines.append("No voice commands recorded.")
            else:
                for cmd in mw.store.commands:
                    lines.append(f"- #{cmd.cmd_id} [{cmd.language}] (conf: {cmd.confidence:.2f}) at {cmd.processed_at}: {cmd.transcript}")
            lines.append("")
            lines.append("## Responses")
            if not mw.store.responses:
                lines.append("No voice responses generated.")
            else:
                for resp in mw.store.responses:
                    lines.append(
                        f"- #{resp.response_id} [{resp.audio_format}] ({resp.duration_secs:.1f}s) "
                        f"at {resp.created_at}: {resp.text[:80]}{'...' if len(resp.text) > 80 else ''}"
                    )
            return "\n".join(lines)

        def clear_voice(
            runtime: ToolRuntime[None, VoiceIOState],
        ) -> str:
            """Clear all voice commands and responses."""
            cmd_count = len(mw.store.commands)
            resp_count = len(mw.store.responses)
            mw.store.commands.clear()
            mw.store.responses.clear()
            mw.store._next_cmd_id = 1
            mw.store._next_resp_id = 1
            logger.info("Cleared %d commands and %d responses", cmd_count, resp_count)
            return f"Cleared {cmd_count} voice command(s) and {resp_count} response(s)."

        return [
            StructuredTool.from_function(
                func=process_voice_input,
                name="process_voice_input",
                description="Process a voice input command from transcribed audio.",
            ),
            StructuredTool.from_function(
                func=generate_voice_response,
                name="generate_voice_response",
                description="Generate a voice response from text.",
            ),
            StructuredTool.from_function(
                func=set_voice_language,
                name="set_voice_language",
                description="Set the active language for voice processing.",
            ),
            StructuredTool.from_function(
                func=voice_history,
                name="voice_history",
                description="Get voice command and response history.",
            ),
            StructuredTool.from_function(
                func=clear_voice,
                name="clear_voice",
                description="Clear all voice commands and responses.",
            ),
        ]

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Append voice I/O system prompt to the request."""
        return request.override(
            system_message=append_to_system_message(request.system_message, SYSTEM_PROMPT),
        )

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """Wrap synchronous model call with voice I/O context."""
        return call_next(self.modify_request(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        """Wrap asynchronous model call with voice I/O context."""
        return await call_next(self.modify_request(request))


__all__ = [
    "VoiceCommand",
    "VoiceIOMiddleware",
    "VoiceResponse",
    "VoiceStore",
]
