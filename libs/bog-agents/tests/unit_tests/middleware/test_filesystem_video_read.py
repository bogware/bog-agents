"""Wave 2 F1: the read_file tool must sample frames from video files.

Before this change the ported `middleware/video_reader.py` had zero production
callers — a video read never reached `extract_video_frames`. These tests pin
the new read-path dispatch:

1. With the `[video]` extra absent (the default CI case) a video read takes the
   graceful `MISSING_VIDEO_HINT` fallback instead of crashing or decoding the
   container as text — on both the sync and async paths.
2. With `av`/Pillow present, a real video read reaches `extract_video_frames`
   and returns sampled image content blocks (guarded by `importorskip`).
3. A non-video binary read (`.png`) is unchanged — it still returns the image
   content block and never routes through video handling.
"""

from __future__ import annotations

import base64
import io
from typing import Any

import pytest
from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage

from bog_agents.backends.utils import create_file_data
from bog_agents.middleware.filesystem import (
    MAX_VIDEO_INPUT_BYTES,
    VIDEO_EXTENSIONS,
    FilesystemMiddleware,
    FilesystemState,
    _handle_video_read,
    _video_window_header,
)
from bog_agents.middleware.video_reader import MISSING_VIDEO_HINT

_VIDEO_READER = "bog_agents.middleware.video_reader.video_dependencies_available"


def _runtime(state: FilesystemState, tool_call_id: str = "call_1") -> ToolRuntime:
    return ToolRuntime(state=state, context=None, tool_call_id=tool_call_id, store=None, stream_writer=lambda _: None, config={})


def _state(files: dict[str, Any] | None = None) -> FilesystemState:
    return FilesystemState(messages=[], files=files or {})


def _tool(middleware: FilesystemMiddleware, name: str) -> Any:
    return next(tool for tool in middleware.tools if tool.name == name)


def _binary_file(raw: bytes) -> dict[str, Any]:
    """Store raw bytes as a base64-encoded backend file (as images/videos are)."""
    return create_file_data(base64.b64encode(raw).decode("ascii"), encoding="base64")


# ---------------------------------------------------------------------------
# 1. Fallback when the optional [video] extra is absent (default CI case)
# ---------------------------------------------------------------------------


class TestVideoDepsAbsentFallback:
    def test_sync_video_read_returns_hint_when_deps_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_VIDEO_READER, lambda: False)
        state = _state({"/clip.mp4": _binary_file(b"not-a-real-video")})
        result = _tool(FilesystemMiddleware(), "read_file").invoke({"file_path": "/clip.mp4", "runtime": _runtime(state)})
        assert result == MISSING_VIDEO_HINT

    async def test_async_video_read_returns_hint_when_deps_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_VIDEO_READER, lambda: False)
        state = _state({"/clip.mkv": _binary_file(b"not-a-real-video")})
        result = await _tool(FilesystemMiddleware(), "read_file").ainvoke({"file_path": "/clip.mkv", "runtime": _runtime(state)})
        assert result == MISSING_VIDEO_HINT

    def test_video_read_does_not_crash_and_does_not_decode_as_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The raw container bytes must never surface to the model as text.
        monkeypatch.setattr(_VIDEO_READER, lambda: False)
        state = _state({"/clip.webm": _binary_file(b"\x00\x01\x02binary\xff")})
        result = _tool(FilesystemMiddleware(), "read_file").invoke({"file_path": "/clip.webm", "runtime": _runtime(state)})
        assert result == MISSING_VIDEO_HINT
        assert "binary" not in result

    def test_all_advertised_video_extensions_take_the_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_VIDEO_READER, lambda: False)
        tool = _tool(FilesystemMiddleware(), "read_file")
        for ext in sorted(VIDEO_EXTENSIONS):
            state = _state({f"/clip{ext}": _binary_file(b"payload")})
            result = tool.invoke({"file_path": f"/clip{ext}", "runtime": _runtime(state)})
            assert result == MISSING_VIDEO_HINT, ext


# ---------------------------------------------------------------------------
# 2. Pure-logic dispatch helpers (no optional deps required)
# ---------------------------------------------------------------------------


class TestVideoDispatchHelpers:
    def test_window_header_from_start(self) -> None:
        assert _video_window_header("/a.mp4", 0.0, 30.0, 0.5) == "Reading first 30s of /a.mp4 at 0.5 fps."

    def test_window_header_with_offset(self) -> None:
        header = _video_window_header("/a.mp4", 5.0, 10.0, 0.5)
        assert header == "Reading [5.000s, 15.000s) of /a.mp4 at 0.5 fps."

    def test_non_positive_limit_is_a_tool_error(self) -> None:
        # Reached before any av import; must return a recoverable error string.
        result = _handle_video_read("/a.mp4", b"data", "call_1", offset=0, limit=0)
        assert isinstance(result, str)
        assert "limit must be > 0" in result

    def test_oversize_payload_is_rejected(self) -> None:
        class _Huge(bytes):
            def __len__(self) -> int:  # avoid allocating a real 1GB buffer
                return MAX_VIDEO_INPUT_BYTES + 1

        result = _handle_video_read("/a.mp4", _Huge(b""), "call_1", offset=0, limit=10)
        assert isinstance(result, str)
        assert "maximum input size" in result


# ---------------------------------------------------------------------------
# 3. Real frame sampling (requires the [video] extra)
# ---------------------------------------------------------------------------


def _make_tiny_video() -> bytes:
    """Encode a short solid-color clip to an in-memory mp4 for a real read."""
    import av
    from PIL import Image

    buf = io.BytesIO()
    container = av.open(buf, mode="w", format="mp4")
    try:
        stream = container.add_stream("mpeg4", rate=5)
        stream.width = 64
        stream.height = 64
        stream.pix_fmt = "yuv420p"
        for i in range(20):  # 4 seconds at 5 fps
            img = Image.new("RGB", (64, 64), (i * 12 % 256, 90, 160))
            frame = av.VideoFrame.from_image(img)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    finally:
        container.close()
    return buf.getvalue()


class TestRealVideoFrameSampling:
    def test_read_file_samples_video_frames(self) -> None:
        pytest.importorskip("av")
        pytest.importorskip("PIL")
        try:
            video_bytes = _make_tiny_video()
        except Exception as exc:  # noqa: BLE001 - encoder/codec availability varies by wheel
            pytest.skip(f"could not encode a test video with this av build: {exc}")

        state = _state({"/clip.mp4": _binary_file(video_bytes)})
        result = _tool(FilesystemMiddleware(), "read_file").invoke({"file_path": "/clip.mp4", "runtime": _runtime(state)})

        # The F1 fix is only real if extract_video_frames was actually reached:
        # the result is a media ToolMessage carrying JPEG image blocks, not text.
        assert isinstance(result, ToolMessage)
        blocks = result.content_blocks
        image_blocks = [b for b in blocks if isinstance(b, dict) and b.get("type") == "image"]
        assert image_blocks, "expected at least one sampled frame image block"
        assert all(b.get("mime_type") == "image/jpeg" for b in image_blocks)
        assert blocks[0].get("type") == "text" and "fps" in blocks[0].get("text", "")
        assert result.additional_kwargs.get("read_file_frame_count") == len(image_blocks)

    def test_undecodable_video_returns_graceful_error(self) -> None:
        pytest.importorskip("av")
        state = _state({"/broken.mp4": _binary_file(b"this is not a valid video container")})
        result = _tool(FilesystemMiddleware(), "read_file").invoke({"file_path": "/broken.mp4", "runtime": _runtime(state)})
        # A malformed payload must not crash the turn — it comes back as text.
        assert isinstance(result, str)
        assert "Error reading video" in result


# ---------------------------------------------------------------------------
# 4. Non-video binary read is unchanged (regression guard)
# ---------------------------------------------------------------------------


class TestNonVideoBinaryUnchanged:
    def test_png_read_still_returns_image_block(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Even if video deps were present, a .png must not route through video.
        monkeypatch.setattr(_VIDEO_READER, lambda: True)
        raw = b"\x89PNG\r\n\x1a\n-fake-image-bytes"
        state = _state({"/pic.png": _binary_file(raw)})
        result = _tool(FilesystemMiddleware(), "read_file").invoke({"file_path": "/pic.png", "runtime": _runtime(state)})
        assert isinstance(result, ToolMessage)
        assert result.content_blocks[0]["type"] == "image"
        assert result.additional_kwargs.get("read_file_media_type") == "image/png"
        # No video-specific metadata leaked onto a plain image read.
        assert "read_file_frame_count" not in result.additional_kwargs

    async def test_png_aread_still_returns_image_block(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_VIDEO_READER, lambda: True)
        raw = b"\x89PNG\r\n\x1a\n-fake-image-bytes"
        state = _state({"/pic.png": _binary_file(raw)})
        result = await _tool(FilesystemMiddleware(), "read_file").ainvoke({"file_path": "/pic.png", "runtime": _runtime(state)})
        assert isinstance(result, ToolMessage)
        assert result.content_blocks[0]["type"] == "image"
