"""Hash-chained per-run action log (ROADMAP #74).

One JSONL file per run. Every event carries the SHA-256 of the previous
event's line (`prev`) and its own hash over `prev` plus its canonical body, so
a line removed, edited or reordered anywhere breaks `verify()` from that point
on. Producers append: `ActionLogMiddleware` (model calls with token usage, tool
calls), the CLI's approval path (`approval` events), Expert Mode verdicts
(`expert_sink`), cost / budget events — anything a compliance questionnaire
asks "who decided what, when, at what cost". `export()` bundles the events
with the head hash and, when a signer is injected (the CLI passes its
TraceFile Ed25519 signer), a detached signature over the canonical bundle.
`apply_retention()` deletes run files older than the policy. No dependencies
beyond the standard library; the middleware is the only LangChain-facing part.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware.types import AgentMiddleware, ModelResponse

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from langchain.agents.middleware.types import ModelRequest
    from langchain.tools.tool_node import ToolCallRequest

logger = logging.getLogger(__name__)

GENESIS = "sha256:" + "0" * 64
_ARGS_PREVIEW = 400


def _canonical(data: dict[str, Any]) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


@dataclass
class ActionEvent:
    """One line of the log."""

    seq: int
    ts: float
    kind: str
    data: dict[str, Any] = field(default_factory=dict)
    prev: str = GENESIS
    hash: str = ""

    def body(self) -> dict[str, Any]:
        """The hashed part (everything but `hash`)."""
        return {"seq": self.seq, "ts": self.ts, "kind": self.kind, "data": self.data, "prev": self.prev}

    def compute_hash(self) -> str:
        """`sha256(prev || canonical(body))`."""
        return _digest(self.prev.encode("utf-8") + _canonical(self.body()))

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready mapping."""
        return {**self.body(), "hash": self.hash}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ActionEvent:
        """Rebuild from a stored line."""
        return cls(
            seq=int(data.get("seq", 0)),
            ts=float(data.get("ts", 0.0)),
            kind=str(data.get("kind", "")),
            data=dict(data.get("data") or {}),
            prev=str(data.get("prev", GENESIS)),
            hash=str(data.get("hash", "")),
        )


@dataclass
class VerifyResult:
    """Outcome of `verify()`."""

    ok: bool
    checked: int
    head: str
    broken_at: int | None = None
    reason: str = ""

    def describe(self) -> str:
        """One line."""
        if self.ok:
            return f"chain intact: {self.checked} event(s), head {self.head[:19]}"
        return f"chain BROKEN at event {self.broken_at}: {self.reason} ({self.checked} checked)"


class ActionLog:
    """Append-only, hash-chained JSONL for one run."""

    def __init__(self, path: str | Path, *, run_id: str = "", clock: Callable[[], float] = time.time) -> None:
        """Bind to `path`; an existing file continues its chain."""
        self.path = Path(path)
        self.run_id = run_id or self.path.stem
        self._clock = clock
        self._lock = threading.Lock()
        self._seq = 0
        self._head = GENESIS
        if self.path.is_file():
            for event in self.events():
                self._seq = event.seq
                self._head = event.hash

    @property
    def head(self) -> str:
        """Hash of the last event (`GENESIS` when empty)."""
        return self._head

    def append(self, kind: str, **data: Any) -> ActionEvent:
        """Append one event and return it (best effort on disk errors: the event is still returned)."""
        with self._lock:
            event = ActionEvent(seq=self._seq + 1, ts=self._clock(), kind=kind, data=dict(data), prev=self._head)
            event.hash = event.compute_hash()
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event.to_dict(), default=str) + "\n")
            except OSError:
                logger.debug("action log append failed", exc_info=True)
            self._seq = event.seq
            self._head = event.hash
            return event

    def events(self) -> Iterator[ActionEvent]:
        """Stored events, oldest first (unparseable lines are skipped — and break verification)."""
        yield from _read_events(self.path)

    def verify(self) -> VerifyResult:
        """Walk the chain and report the first break."""
        return verify_chain(self.path)

    def export(self, *, sign: Callable[[bytes], str] | None = None, signer_id: str = "") -> dict[str, Any]:
        """The events plus head hash and, with a signer, a detached signature over the canonical bundle."""
        events = [e.to_dict() for e in self.events()]
        bundle: dict[str, Any] = {"run_id": self.run_id, "events": events, "head": self._head, "count": len(events), "exported_at": self._clock()}
        if sign is not None:
            try:
                bundle["signature"] = sign(signed_payload(bundle))
                bundle["signer"] = signer_id
            except Exception:  # noqa: BLE001 - an unsigned export is still an export
                logger.warning("action log export could not be signed", exc_info=True)
        return bundle


def signed_payload(bundle: dict[str, Any]) -> bytes:
    """The bytes a signature covers: the bundle without `signature` / `signer`."""
    return _canonical({k: v for k, v in bundle.items() if k not in ("signature", "signer")})


def _read_events(path: Path) -> Iterator[ActionEvent]:
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        if not line.strip():
            continue
        try:
            yield ActionEvent.from_dict(json.loads(line))
        except (ValueError, TypeError):
            yield ActionEvent(seq=-1, ts=0.0, kind="<unparseable>", prev="", hash="")


def verify_chain(path: str | Path) -> VerifyResult:
    """Verify a log file: each event's `prev` is the previous hash and its `hash` recomputes."""
    head = GENESIS
    checked = 0
    for event in _read_events(Path(path)):
        if event.seq == -1:
            return VerifyResult(ok=False, checked=checked, head=head, broken_at=checked + 1, reason="unparseable line")
        if event.prev != head:
            return VerifyResult(ok=False, checked=checked, head=head, broken_at=event.seq, reason="prev hash does not match the previous event")
        if event.compute_hash() != event.hash:
            return VerifyResult(ok=False, checked=checked, head=head, broken_at=event.seq, reason="event hash does not recompute (edited)")
        if event.seq != checked + 1:
            return VerifyResult(ok=False, checked=checked, head=head, broken_at=event.seq, reason="sequence gap")
        head = event.hash
        checked += 1
    return VerifyResult(ok=True, checked=checked, head=head)


def verify_export(bundle: dict[str, Any], *, verify: Callable[[bytes, str], bool] | None = None) -> VerifyResult:
    """Verify an exported bundle: the chain inside it and, with a verifier, its signature."""
    head = GENESIS
    checked = 0
    for raw in bundle.get("events", []):
        event = ActionEvent.from_dict(raw)
        if event.prev != head or event.compute_hash() != event.hash or event.seq != checked + 1:
            return VerifyResult(ok=False, checked=checked, head=head, broken_at=event.seq, reason="chain break inside the export")
        head = event.hash
        checked += 1
    if head != bundle.get("head"):
        return VerifyResult(ok=False, checked=checked, head=head, broken_at=None, reason="head hash does not match the events")
    signature = bundle.get("signature")
    if verify is not None and signature and not verify(signed_payload(bundle), str(signature)):
        return VerifyResult(ok=False, checked=checked, head=head, broken_at=None, reason="signature does not verify")
    return VerifyResult(ok=True, checked=checked, head=head)


def apply_retention(directory: str | Path, *, keep_days: float, now: float | None = None) -> int:
    """Delete run logs whose last modification is older than `keep_days`; returns how many."""
    root = Path(directory)
    if not root.is_dir():
        return 0
    cutoff = (time.time() if now is None else now) - keep_days * 86400.0
    removed = 0
    for path in root.glob("*.jsonl"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            logger.debug("retention could not remove %s", path, exc_info=True)
    return removed


def expert_sink(log: ActionLog) -> Callable[[str, dict[str, Any]], None]:
    """An Expert-Mode `AuditSink` that records every verdict as an `expert_verdict` event."""

    def _sink(action: str, details: dict[str, Any]) -> None:
        log.append("expert_verdict", action=action, **{k: v for k, v in details.items() if k != "action"})

    return _sink


def _usage_of(response: ModelResponse) -> tuple[str, int, int]:
    messages = getattr(response, "result", None) or []
    for message in reversed(messages):
        if getattr(message, "type", "") != "ai":
            continue
        usage = getattr(message, "usage_metadata", None) or {}
        meta = getattr(message, "response_metadata", None) or {}
        model = str(meta.get("model_name") or meta.get("model") or "")
        return model, int(usage.get("input_tokens", 0) or 0), int(usage.get("output_tokens", 0) or 0)
    return "", 0, 0


def _args_preview(args: Any) -> str:  # noqa: ANN401 - tool args are free-form
    try:
        text = json.dumps(args, sort_keys=True, default=str)
    except (TypeError, ValueError):
        text = str(args)
    return text[:_ARGS_PREVIEW]


class ActionLogMiddleware(AgentMiddleware[Any, Any, Any]):
    """Record every model call (with token usage) and tool call into an `ActionLog`."""

    def __init__(self, log: ActionLog, *, price: Callable[[str, int, int], float | None] | None = None) -> None:
        """Bind to a log; `price(model, in, out)` (optional) adds `cost_usd` to model events."""
        super().__init__()
        self.log = log
        self._price = price

    def _record_model(self, request: ModelRequest, response: ModelResponse) -> None:
        model, tokens_in, tokens_out = _usage_of(response)
        if not model:
            model = str(getattr(getattr(request, "model", None), "model_name", "") or getattr(getattr(request, "model", None), "model", "") or "")
        cost = self._price(model, tokens_in, tokens_out) if self._price and (tokens_in or tokens_out) else None
        self.log.append("model_call", model=model, input_tokens=tokens_in, output_tokens=tokens_out, cost_usd=cost)

    def wrap_model_call(self, request: ModelRequest, handler: Any) -> ModelResponse:  # type: ignore[override]  # noqa: ANN401
        """Record the call after it returns (a failure records `model_error`)."""
        try:
            response = handler(request)
        except Exception as exc:
            self.log.append("model_error", error=exc.__class__.__name__)
            raise
        self._record_model(request, response)
        return response

    async def awrap_model_call(self, request: ModelRequest, handler: Any) -> ModelResponse:  # type: ignore[override]  # noqa: ANN401
        """Async twin of `wrap_model_call`."""
        try:
            response = await handler(request)
        except Exception as exc:
            self.log.append("model_error", error=exc.__class__.__name__)
            raise
        self._record_model(request, response)
        return response

    def _record_tool(self, request: ToolCallRequest, result: Any, error: str = "") -> None:  # noqa: ANN401
        call = getattr(request, "tool_call", None) or {}
        status = error or str(getattr(result, "status", "") or "success")
        self.log.append("tool_call", tool=str(call.get("name", "")), args=_args_preview(call.get("args", {})), status=status)

    def wrap_tool_call(self, request: ToolCallRequest, handler: Any) -> Any:  # type: ignore[override]  # noqa: ANN401
        """Record the tool call with its outcome."""
        try:
            result = handler(request)
        except Exception as exc:
            self._record_tool(request, None, error=exc.__class__.__name__)
            raise
        self._record_tool(request, result)
        return result

    async def awrap_tool_call(self, request: ToolCallRequest, handler: Any) -> Any:  # type: ignore[override]  # noqa: ANN401
        """Async twin of `wrap_tool_call`."""
        try:
            result = await handler(request)
        except Exception as exc:
            self._record_tool(request, None, error=exc.__class__.__name__)
            raise
        self._record_tool(request, result)
        return result


__all__ = [
    "GENESIS",
    "ActionEvent",
    "ActionLog",
    "ActionLogMiddleware",
    "VerifyResult",
    "apply_retention",
    "expert_sink",
    "signed_payload",
    "verify_chain",
    "verify_export",
]
