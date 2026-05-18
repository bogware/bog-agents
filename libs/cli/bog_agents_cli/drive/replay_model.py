"""Deterministic chat-model shims for drive scripts.

Three flavors of model spec are recognised by the drive runner:

* ``fake:<text>`` — a single fixed response. No fixture file, no IO.
* ``replay:<path>`` — JSONL fixture of recorded turns. Each line is one
  ``{"messages": [...], "response": "..."}`` record; the model emits
  responses in order. Loops back to the start if the script exceeds the
  fixture length, with a warning.
* ``record:<path>`` — wraps a real chat model and writes every
  request/response pair to a JSONL fixture suitable for later replay.
  The wrapped model is resolved via the existing model factory.

Returning a real ``BaseChatModel`` (not a Pregel) is enough — the SDK's
:func:`create_agent` accepts any chat model, and the rest of the agent
graph runs untouched.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from langchain_core.callbacks.manager import CallbackManagerForLLMRun
    from langchain_core.messages import BaseMessage
    from langchain_core.runnables import Runnable
    from langchain_core.tools import BaseTool

logger = logging.getLogger(__name__)


__all__ = [
    "DriveModelSpec",
    "FakeChatModel",
    "RecordingChatModel",
    "ReplayChatModel",
    "is_drive_model_spec",
    "parse_drive_model_spec",
    "resolve_drive_model",
]


# ---------------------------------------------------------------------------
# Spec parsing
# ---------------------------------------------------------------------------


_DRIVE_SCHEMES = ("fake:", "replay:", "record:")


def is_drive_model_spec(spec: str | None) -> bool:
    """True iff ``spec`` is one of the drive-only schemes."""
    if not spec:
        return False
    return spec.startswith(_DRIVE_SCHEMES)


class DriveModelSpec:
    """Parsed drive-only model spec."""

    __slots__ = ("kind", "payload")

    def __init__(self, kind: str, payload: str) -> None:
        self.kind = kind
        self.payload = payload

    def __repr__(self) -> str:
        return f"DriveModelSpec(kind={self.kind!r}, payload={self.payload!r})"


def parse_drive_model_spec(spec: str) -> DriveModelSpec | None:
    """Return a :class:`DriveModelSpec` if *spec* is a drive scheme, else None."""
    for scheme in _DRIVE_SCHEMES:
        if spec.startswith(scheme):
            return DriveModelSpec(kind=scheme.rstrip(":"), payload=spec[len(scheme) :])
    return None


# ---------------------------------------------------------------------------
# Fake model — fixed single response
# ---------------------------------------------------------------------------


class FakeChatModel(BaseChatModel):
    """Returns a fixed text response for every invocation.

    Used by ``fake:<text>`` to keep tests trivial when the agent never
    needs to think — e.g. UI-only smoke tests that just walk through
    slash commands.
    """

    response_text: str = "Hello from FakeChatModel."

    @property
    def _llm_type(self) -> str:
        return "drive-fake"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        del messages, stop, run_manager, kwargs
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=self.response_text))]
        )

    def bind_tools(  # type: ignore[override]
        self,
        tools: Sequence[dict[str, Any] | type | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[Any, BaseMessage]:
        """Drive models ignore tools — return self so the agent factory
        doesn't blow up when middleware calls ``bind_tools`` during graph
        construction. ``BaseChatModel.bind_tools`` is abstract; this is
        the conventional no-op for response-only fakes.
        """
        del tools, tool_choice, kwargs
        return self


# ---------------------------------------------------------------------------
# Replay model — JSONL fixture
# ---------------------------------------------------------------------------


class ReplayChatModel(BaseChatModel):
    """Plays back recorded chunks from a JSONL fixture.

    Each fixture line is::

        {"response": "the text the AI returned", "tool_calls": [{"name": ..., "args": {...}, "id": "..."}]}

    The ``tool_calls`` list is optional. The model walks turns in order;
    once exhausted it loops to start and logs a warning so a malformed
    fixture is loud rather than silently truncating tests.
    """

    fixture_path: str = ""
    _turns: list[dict[str, Any]] | None = None
    _cursor: int = 0

    @property
    def _llm_type(self) -> str:
        return "drive-replay"

    def _load_turns(self) -> list[dict[str, Any]]:
        if self._turns is not None:
            return self._turns
        path = Path(self.fixture_path)
        if not path.is_file():
            msg = f"replay fixture not found: {path}"
            raise FileNotFoundError(msg)
        turns: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as fh:
            for line_no, raw in enumerate(fh, 1):
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    msg = f"{path}:{line_no}: invalid JSON ({exc})"
                    raise ValueError(msg) from exc
                if not isinstance(record, dict):
                    msg = f"{path}:{line_no}: each line must be a JSON object"
                    raise ValueError(msg)
                turns.append(record)
        if not turns:
            msg = f"replay fixture {path} contained no usable lines"
            raise ValueError(msg)
        self._turns = turns
        return turns

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        del messages, stop, run_manager, kwargs
        turns = self._load_turns()
        if self._cursor >= len(turns):
            logger.warning(
                "replay fixture %s exhausted after %d turns; looping",
                self.fixture_path,
                len(turns),
            )
            self._cursor = 0
        record = turns[self._cursor]
        self._cursor = self._cursor + 1
        content = str(record.get("response", ""))
        tool_calls = record.get("tool_calls") or []
        msg = AIMessage(content=content, tool_calls=tool_calls)
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def bind_tools(  # type: ignore[override]
        self,
        tools: Sequence[dict[str, Any] | type | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[Any, BaseMessage]:
        del tools, tool_choice, kwargs
        return self


# ---------------------------------------------------------------------------
# Recording wrapper — used when a script declares ``record:path`` so a
# real run can be captured into a replay fixture for CI.
# ---------------------------------------------------------------------------


class RecordingChatModel(BaseChatModel):
    """Wraps a real chat model and writes each turn to a JSONL fixture."""

    inner: BaseChatModel
    fixture_path: str = ""

    @property
    def _llm_type(self) -> str:
        return "drive-record"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        result = self.inner._generate(messages, stop, run_manager, **kwargs)
        try:
            ai = result.generations[0].message
        except (IndexError, AttributeError):
            return result
        record = {
            "response": getattr(ai, "content", ""),
            "tool_calls": list(getattr(ai, "tool_calls", []) or []),
        }
        path = Path(self.fixture_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False))
            fh.write("\n")
        return result

    def bind_tools(  # type: ignore[override]
        self,
        tools: Sequence[dict[str, Any] | type | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[Any, BaseMessage]:
        return self.inner.bind_tools(tools, tool_choice=tool_choice, **kwargs)


# ---------------------------------------------------------------------------
# Public resolver
# ---------------------------------------------------------------------------


def resolve_drive_model(spec: str) -> BaseChatModel:
    """Build a chat model for a drive script's session.model spec.

    Args:
        spec: One of ``fake:<text>``, ``replay:<path>``, ``record:<path>``,
            or a normal ``provider:model`` spec that delegates to the
            real model factory.

    Returns:
        A :class:`BaseChatModel`. The caller passes it straight to
        :func:`bog_agents.create_agent`.

    Raises:
        ValueError: For malformed drive specs.
    """
    parsed = parse_drive_model_spec(spec)
    if parsed is None:
        return _build_real_model(spec)
    if parsed.kind == "fake":
        text = parsed.payload or "Hello from FakeChatModel."
        return FakeChatModel(response_text=text)
    if parsed.kind == "replay":
        if not parsed.payload:
            msg = "replay: requires a fixture path (e.g. replay:fixtures/run1.jsonl)"
            raise ValueError(msg)
        return ReplayChatModel(fixture_path=parsed.payload)
    if parsed.kind == "record":
        if ":" not in parsed.payload:
            msg = "record: requires <path>:<real-model-spec>"
            raise ValueError(msg)
        fixture, _, inner_spec = parsed.payload.partition(":")
        return RecordingChatModel(
            inner=_build_real_model(inner_spec),
            fixture_path=fixture,
        )
    msg = f"unknown drive scheme: {parsed.kind!r}"
    raise ValueError(msg)


def _build_real_model(spec: str) -> BaseChatModel:
    """Build a real provider chat model via the CLI's model factory."""
    # Deferred to avoid pulling the heavy model_config import path when
    # users only run fake/replay scripts.
    from bog_agents_cli.config import create_model_with_fallback

    result = create_model_with_fallback(spec)
    return result.model


# ---------------------------------------------------------------------------
# Helpers for record/replay round-trip
# ---------------------------------------------------------------------------


def iter_replay_records(path: Path) -> Iterator[dict[str, Any]]:
    """Read a replay fixture and yield each turn as a dict (line-iterator).

    Used by tooling that wants to inspect or transform fixtures without
    instantiating a model.

    Yields:
        Each non-empty, non-comment line of the JSONL fixture parsed
        as a dict.
    """
    with path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            yield json.loads(line)


def write_replay_records(path: Path, records: Sequence[dict[str, Any]]) -> None:
    """Write a sequence of turn records as JSONL at *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False))
            fh.write("\n")
