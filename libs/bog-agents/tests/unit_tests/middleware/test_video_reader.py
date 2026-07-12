"""Tests for the video_reader middleware (video-frame read capability)."""

from __future__ import annotations

import sys

import pytest

from bog_agents.middleware import video_reader
from bog_agents.middleware.video_reader import (
    MAX_VIDEO_DECODE_SECONDS,
    MAX_VIDEO_EMITTED_BYTES,
    MAX_VIDEO_FRAME_PIXELS,
    MAX_VIDEO_FRAME_SIDE,
    MAX_VIDEO_OUTPUT_HEIGHT,
    MAX_VIDEO_OUTPUT_WIDTH,
    MAX_VIDEO_SAMPLED_FRAMES,
    MISSING_VIDEO_HINT,
    VideoExtractionError,
    extract_video_frames,
    video_dependencies_available,
)


class TestPublicSurface:
    """The module exports the symbols the filesystem middleware depends on."""

    def test_caps_are_positive_ints(self) -> None:
        for cap in (
            MAX_VIDEO_SAMPLED_FRAMES,
            MAX_VIDEO_FRAME_PIXELS,
            MAX_VIDEO_FRAME_SIDE,
            MAX_VIDEO_OUTPUT_WIDTH,
            MAX_VIDEO_OUTPUT_HEIGHT,
            MAX_VIDEO_EMITTED_BYTES,
        ):
            assert isinstance(cap, int)
            assert cap > 0

    def test_decode_deadline_is_positive(self) -> None:
        assert MAX_VIDEO_DECODE_SECONDS > 0

    def test_missing_hint_mentions_video_extra(self) -> None:
        assert "bog-agents[video]" in MISSING_VIDEO_HINT

    def test_error_is_runtime_error(self) -> None:
        assert issubclass(VideoExtractionError, RuntimeError)

    def test_dependency_probe_returns_bool(self) -> None:
        assert isinstance(video_dependencies_available(), bool)


class TestDependencyAbsentPath:
    """When the optional deps are missing, callers get a clean error — never an ImportError crash."""

    def test_import_av_converts_missing_module_to_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`_import_av` turns a real `import av` failure into a VideoExtractionError with the hint.

        Setting `sys.modules['av'] = None` makes the `import av` statement raise
        ImportError, exercising the actual lazy-import guard rather than a stub.
        """
        monkeypatch.setitem(sys.modules, "av", None)
        with pytest.raises(VideoExtractionError) as excinfo:
            video_reader._import_av()
        assert "bog-agents[video]" in str(excinfo.value)

    def test_extract_surfaces_hint_when_av_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The public entry point wraps the absent-dep failure, not a raw ImportError."""
        monkeypatch.setitem(sys.modules, "av", None)
        with pytest.raises(VideoExtractionError) as excinfo:
            extract_video_frames(b"not-a-real-video", offset_seconds=0.0, duration_seconds=1.0, sampling_rate=1.0)
        assert "bog-agents[video]" in str(excinfo.value)

    def test_encode_jpeg_surfaces_hint_when_pillow_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A missing Pillow during frame encoding also yields the clean hint error."""
        monkeypatch.setitem(sys.modules, "PIL", None)
        monkeypatch.setitem(sys.modules, "PIL.Image", None)
        with pytest.raises(VideoExtractionError) as excinfo:
            video_reader._encode_jpeg(object())
        assert "bog-agents[video]" in str(excinfo.value)


class TestArgumentValidation:
    """Window validation happens before any heavy import, surfaced as VideoExtractionError."""

    @pytest.mark.parametrize(
        ("offset", "duration", "rate"),
        [
            (-1.0, 1.0, 1.0),
            (0.0, 0.0, 1.0),
            (0.0, -1.0, 1.0),
            (0.0, 1.0, 0.0),
            (0.0, 1.0, -1.0),
        ],
    )
    def test_invalid_window_raises(self, monkeypatch: pytest.MonkeyPatch, offset: float, duration: float, rate: float) -> None:
        # Force the av import to fail loudly so we prove validation runs first.
        def _boom() -> None:
            raise AssertionError("av import must not be reached for an invalid window")

        monkeypatch.setattr(video_reader, "_import_av", _boom)
        with pytest.raises(VideoExtractionError):
            extract_video_frames(b"payload", offset_seconds=offset, duration_seconds=duration, sampling_rate=rate)


class TestFormatTimestamp:
    """The internal timestamp formatter shapes the per-frame text header."""

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (0.0, "00:00:00.000"),
            (-5.0, "00:00:00.000"),
            (1.5, "00:00:01.500"),
            (3661.25, "01:01:01.250"),
        ],
    )
    def test_format(self, seconds: float, expected: str) -> None:
        assert video_reader._format_timestamp(seconds) == expected


class TestActualExtraction:
    """End-to-end decode — skipped unless the [video] extra (av) is installed."""

    def test_extract_real_frames(self) -> None:
        av = pytest.importorskip("av")
        pytest.importorskip("PIL")

        import fractions
        import io

        # Synthesize a tiny 3-second, 10fps video entirely in-memory.
        buf = io.BytesIO()
        container = av.open(buf, mode="w", format="mp4")
        stream = container.add_stream("mpeg4", rate=10)
        stream.width = 64
        stream.height = 48
        stream.pix_fmt = "yuv420p"
        stream.time_base = fractions.Fraction(1, 10)

        from PIL import Image

        for i in range(30):
            img = Image.new("RGB", (64, 48), color=(i * 8 % 256, 0, 0))
            frame = av.VideoFrame.from_image(img)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
        container.close()

        blocks = extract_video_frames(buf.getvalue(), offset_seconds=0.0, duration_seconds=2.0, sampling_rate=1.0)
        assert blocks
        image_blocks = [b for b in blocks if b.get("type") == "image"]
        assert image_blocks
        assert all(b["mime_type"] == "image/jpeg" for b in image_blocks)
        assert any(b.get("type") == "text" and "Frame at t=" in b.get("text", "") for b in blocks)
