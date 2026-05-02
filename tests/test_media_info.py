"""
Unit tests for live media_info population on transcoded streams.

These tests cover the parser that extracts ffmpeg progress fields from stderr,
and verify that media_info propagates through stream_manager.get_stats() so the
m3u-editor UI can display live codec/bitrate/fps badges.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest
from unittest.mock import MagicMock


def _make_process():
    """Build a minimal SharedTranscodingProcess for parser-level assertions."""
    from pooled_stream_manager import SharedTranscodingProcess

    return SharedTranscodingProcess(
        stream_id="test-stream",
        url="http://example.com/test.ts",
        profile="default",
        ffmpeg_args=["-i", "input", "-c", "copy", "-f", "mpegts", "pipe:1"],
    )


def test_media_info_starts_empty():
    """Fresh processes should have an empty media_info dict, not None."""
    process = _make_process()

    assert process.media_info == {}


def test_parse_ffmpeg_progress_extracts_live_fields():
    """A typical ffmpeg stats line should populate bitrate/fps/frame/speed."""
    process = _make_process()

    line = (
        "frame=  243 fps= 30 q=28.0 size=    1152kB "
        "time=00:00:08.13 bitrate=1162.5kbits/s speed=1.01x"
    )
    process._parse_ffmpeg_progress(line)

    assert process.media_info["bitrate_kbps"] == 1162.5
    assert process.media_info["fps"] == 30.0
    assert process.media_info["frame"] == 243
    assert process.media_info["speed"] == 1.01


def test_parse_ffmpeg_progress_ignores_non_progress_lines():
    """Header/info lines should not contribute progress values."""
    process = _make_process()

    process._parse_ffmpeg_progress("Input #0, mpegts, from 'http://example.com':")
    process._parse_ffmpeg_progress("  Duration: N/A, start: 1.400000, bitrate: N/A")

    # The "bitrate: N/A" form is not in kbits/s and should be skipped.
    assert "bitrate_kbps" not in process.media_info
    assert "fps" not in process.media_info


def test_parse_ffmpeg_progress_updates_overwrite_previous_values():
    """Each new progress line should overwrite the prior live snapshot."""
    process = _make_process()

    process._parse_ffmpeg_progress("frame=10 fps=25 bitrate=1000.0kbits/s speed=1.0x")
    process._parse_ffmpeg_progress("frame=20 fps=30 bitrate=2000.5kbits/s speed=1.5x")

    assert process.media_info["frame"] == 20
    assert process.media_info["fps"] == 30.0
    assert process.media_info["bitrate_kbps"] == 2000.5
    assert process.media_info["speed"] == 1.5


def test_get_media_info_returns_empty_for_non_transcoded_streams():
    """
    Plain HTTP-proxy streams (no ffmpeg) must return empty media_info — the
    UI relies on this to hide metadata badges when there's nothing live to show.
    """
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
    from stream_manager import StreamInfo, StreamManager
    from datetime import datetime, timezone

    manager = StreamManager.__new__(StreamManager)
    manager.pooled_manager = None

    stream = StreamInfo(
        stream_id="plain-stream",
        original_url="http://example.com/plain.ts",
        created_at=datetime.now(timezone.utc),
        last_access=datetime.now(timezone.utc),
    )

    assert manager._get_media_info(stream) == {}


def test_get_media_info_pulls_from_linked_pooled_process():
    """
    Transcoded streams should surface the linked SharedTranscodingProcess's
    media_info dict so live ffmpeg data reaches the API response.
    """
    from stream_manager import StreamInfo, StreamManager
    from datetime import datetime, timezone

    manager = StreamManager.__new__(StreamManager)
    pooled = MagicMock()
    fake_process = MagicMock()
    fake_process.media_info = {
        "resolution": "1920x1080",
        "video_codec": "h264",
        "fps": 30.0,
        "bitrate_kbps": 4500.0,
    }
    pooled.shared_processes = {"key-abc": fake_process}
    manager.pooled_manager = pooled

    stream = StreamInfo(
        stream_id="t-stream",
        original_url="http://example.com/t.ts",
        created_at=datetime.now(timezone.utc),
        last_access=datetime.now(timezone.utc),
        transcode_stream_key="key-abc",
    )

    info = manager._get_media_info(stream)

    assert info["resolution"] == "1920x1080"
    assert info["video_codec"] == "h264"
    assert info["fps"] == 30.0
    assert info["bitrate_kbps"] == 4500.0


@pytest.mark.asyncio
async def test_probe_input_async_does_not_block_on_missing_ffprobe(monkeypatch):
    """
    If ffprobe isn't installed, _probe_input_async must swallow FileNotFoundError
    rather than propagate it — the stream must still play even if probing fails.
    """
    import asyncio

    process = _make_process()

    async def _raise(*args, **kwargs):
        raise FileNotFoundError("ffprobe not on PATH")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _raise)

    # Should complete cleanly without raising.
    await process._probe_input_async()

    assert process.media_info == {}
