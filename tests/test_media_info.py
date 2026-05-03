"""
Unit tests for live media_info population on transcoded streams.

These tests cover the parsers that extract codec/container/resolution/audio
info and live progress (bitrate/fps/frame/speed) from ffmpeg's own stderr
output, and verify media_info propagates through stream_manager.get_stats()
so the m3u-editor UI can display live badges. We deliberately do NOT run a
separate ffprobe against the source URL — that doubles the upstream connection
count and trips per-user limits at IPTV providers.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from unittest.mock import MagicMock  # noqa: E402


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


def test_parse_ffmpeg_input_line_extracts_container():
    """The 'Input #0, FORMAT, from URL:' line should populate container."""
    process = _make_process()

    process._parse_ffmpeg_input_line(
        "Input #0, mpegts, from 'http://example.com/stream.ts':"
    )

    assert process.media_info["container"] == "MPEGTS"


def test_parse_ffmpeg_input_line_takes_first_synonym():
    """When ffmpeg lists multiple format synonyms, take the first one."""
    process = _make_process()

    process._parse_ffmpeg_input_line(
        "Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'file.mp4':"
    )

    assert process.media_info["container"] == "MOV"


def test_parse_ffmpeg_stream_line_extracts_video_codec_and_resolution():
    """Video stream lines populate video_codec and resolution."""
    process = _make_process()

    process._parse_ffmpeg_stream_line(
        "    Stream #0:0[0x100]: Video: h264 (Main) ([27][0][0][0] / 0x001B), "
        "yuv420p(progressive), 1280x720 [SAR 1:1 DAR 16:9], 50 fps, 50 tbr, 90k tbn"
    )

    assert process.media_info["video_codec"] == "h264"
    assert process.media_info["resolution"] == "1280x720"


def test_parse_ffmpeg_stream_line_extracts_audio_codec_and_channels():
    """Audio stream lines populate audio_codec and audio_channels."""
    process = _make_process()

    process._parse_ffmpeg_stream_line(
        "    Stream #0:1[0x101](eng): Audio: aac (LC) ([15][0][0][0] / 0x000F), "
        "48000 Hz, stereo, fltp, 192 kb/s"
    )

    assert process.media_info["audio_codec"] == "aac"
    assert process.media_info["audio_channels"] == "stereo"


def test_parse_ffmpeg_stream_line_handles_5_1_channel_layout():
    """5.1 surround layout should map correctly."""
    process = _make_process()

    process._parse_ffmpeg_stream_line(
        "    Stream #0:1: Audio: ac3, 48000 Hz, 5.1, fltp, 384 kb/s"
    )

    assert process.media_info["audio_channels"] == "5.1"


def test_parse_ffmpeg_stream_line_handles_dual_pid_brackets():
    """
    MPEG-TS streams commonly emit two consecutive PID bracket groups
    (e.g. [0x100][0x200]). The regex must match both so video_codec and
    resolution are populated — regression guard for the ? → * fix.
    """
    process = _make_process()

    process._parse_ffmpeg_stream_line(
        "    Stream #0:0[0x100][0x200]: Video: hevc (Main), "
        "yuv420p(tv, bt709), 1920x1080, 50 fps, 50 tbr, 90k tbn"
    )

    assert process.media_info["video_codec"] == "hevc"
    assert process.media_info["resolution"] == "1920x1080"


def test_parse_ffmpeg_stream_line_handles_5_1_side_channel_layout():
    """5.1(side) channel layout variant should map to '5.1'."""
    process = _make_process()

    process._parse_ffmpeg_stream_line(
        "    Stream #0:1: Audio: eac3, 48000 Hz, 5.1(side), fltp, 384 kb/s"
    )

    assert process.media_info["audio_channels"] == "5.1"


def test_parse_ffmpeg_stream_line_does_not_clobber_existing_codec():
    """
    The first Video stream wins so we don't overwrite with secondary streams
    (e.g. embedded thumbnails). Live progress fields (fps/bitrate) are still
    free to update because they're handled by _parse_ffmpeg_progress.
    """
    process = _make_process()
    process.media_info["video_codec"] = "h264"

    process._parse_ffmpeg_stream_line("    Stream #0:2: Video: png, rgba, 256x256")

    assert process.media_info["video_codec"] == "h264"


def test_parse_ffmpeg_input_line_ignores_unrelated_lines():
    """Non-Input lines should be no-ops."""
    process = _make_process()

    process._parse_ffmpeg_input_line(
        "frame=  243 fps= 30 bitrate=1162.5kbits/s speed=1.01x"
    )

    assert "container" not in process.media_info


def test_output_media_info_starts_empty_with_parser_state_unset():
    """Fresh processes track output info separately and start in input mode."""
    process = _make_process()

    assert process.output_media_info == {}
    assert process._parsing_output is False


def test_parse_ffmpeg_output_line_captures_container_and_flips_state():
    """The 'Output #0, FORMAT, to TARGET:' line should populate output_media_info
    and switch the parser into output mode for subsequent Stream lines."""
    process = _make_process()

    process._parse_ffmpeg_output_line("Output #0, mpegts, to 'pipe:1':")

    assert process.output_media_info["container"] == "MPEGTS"
    assert process._parsing_output is True
    # Input-side dict must not be polluted with the output container.
    assert "container" not in process.media_info


def test_parse_ffmpeg_output_line_takes_first_synonym():
    """Multi-synonym output containers collapse to the first canonical name."""
    process = _make_process()

    process._parse_ffmpeg_output_line(
        "Output #0, mov,mp4,m4a,3gp,3g2,mj2, to '/tmp/out.mp4':"
    )

    assert process.output_media_info["container"] == "MOV"


def test_parse_ffmpeg_output_line_handles_hls_muxer():
    """HLS output ('Output #0, hls, to ...') is the common transcoded path."""
    process = _make_process()

    process._parse_ffmpeg_output_line("Output #0, hls, to '/tmp/abc/index.m3u8':")

    assert process.output_media_info["container"] == "HLS"


def test_stream_lines_before_output_marker_go_to_input_media_info():
    """While the parser hasn't seen Output #0, codec lines describe the source."""
    process = _make_process()

    process._parse_ffmpeg_stream_line(
        "    Stream #0:0[0x100]: Video: hevc (Main), yuv420p, 1920x1080, 50 fps"
    )
    process._parse_ffmpeg_stream_line(
        "    Stream #0:1[0x101](eng): Audio: ac3, 48000 Hz, 5.1, fltp, 384 kb/s"
    )

    assert process.media_info["video_codec"] == "hevc"
    assert process.media_info["resolution"] == "1920x1080"
    assert process.media_info["audio_codec"] == "ac3"
    assert process.media_info["audio_channels"] == "5.1"
    assert process.output_media_info == {}


def test_stream_lines_after_output_marker_go_to_output_media_info():
    """Once Output # is seen, the encoder Stream lines populate output_media_info,
    leaving the input description intact."""
    process = _make_process()

    process._parse_ffmpeg_stream_line(
        "    Stream #0:0[0x100]: Video: hevc, yuv420p, 1920x1080, 50 fps"
    )
    process._parse_ffmpeg_stream_line(
        "    Stream #0:1[0x101]: Audio: ac3, 48000 Hz, 5.1, fltp, 384 kb/s"
    )
    process._parse_ffmpeg_output_line("Output #0, mpegts, to 'pipe:1':")
    process._parse_ffmpeg_stream_line(
        "    Stream #0:0: Video: h264, yuv420p, 1280x720, q=2-31, 25 fps"
    )
    process._parse_ffmpeg_stream_line(
        "    Stream #0:1: Audio: aac (LC), 48000 Hz, stereo, fltp, 128 kb/s"
    )

    # Source side untouched
    assert process.media_info["video_codec"] == "hevc"
    assert process.media_info["resolution"] == "1920x1080"
    assert process.media_info["audio_codec"] == "ac3"
    assert process.media_info["audio_channels"] == "5.1"
    # Encoder side captured separately
    assert process.output_media_info["video_codec"] == "h264"
    assert process.output_media_info["resolution"] == "1280x720"
    assert process.output_media_info["audio_codec"] == "aac"
    assert process.output_media_info["audio_channels"] == "stereo"
    assert process.output_media_info["container"] == "MPEGTS"


def test_progress_fields_dual_write_once_output_seen():
    """Progress numbers describe encoder output. Before Output #0 they only
    populate media_info (so the existing UI keeps getting bitrate/fps even on
    very early reads); after Output #0 they mirror into output_media_info."""
    process = _make_process()

    process._parse_ffmpeg_progress(
        "frame=10 fps=25 bitrate=1000.0kbits/s speed=1.0x"
    )
    assert process.media_info["bitrate_kbps"] == 1000.0
    assert process.media_info["fps"] == 25.0
    assert "bitrate_kbps" not in process.output_media_info

    process._parse_ffmpeg_output_line("Output #0, mpegts, to 'pipe:1':")
    process._parse_ffmpeg_progress(
        "frame=20 fps=30 bitrate=2000.5kbits/s speed=1.5x"
    )

    assert process.media_info["bitrate_kbps"] == 2000.5
    assert process.media_info["fps"] == 30.0
    assert process.output_media_info["bitrate_kbps"] == 2000.5
    assert process.output_media_info["fps"] == 30.0
    assert process.output_media_info["frame"] == 20
    assert process.output_media_info["speed"] == 1.5


def test_parse_ffmpeg_output_line_does_not_clobber_existing_container():
    """Some configs may emit multiple Output # markers (e.g. tee); only the
    first wins so output_media_info reflects the primary muxer."""
    process = _make_process()

    process._parse_ffmpeg_output_line("Output #0, mpegts, to 'pipe:1':")
    process._parse_ffmpeg_output_line("Output #1, hls, to '/tmp/idx.m3u8':")

    assert process.output_media_info["container"] == "MPEGTS"


def test_get_output_media_info_returns_empty_for_non_transcoded_streams():
    """Plain HTTP-proxy streams have no ffmpeg process — output_media_info must
    be empty so the editor knows not to render the Output Info badge row."""
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

    assert manager._get_output_media_info(stream) == {}


def test_get_output_media_info_pulls_from_linked_pooled_process():
    """Transcoded streams expose the linked process's output_media_info dict so
    encoder-side stats reach the API response alongside the input description."""
    from stream_manager import StreamInfo, StreamManager
    from datetime import datetime, timezone

    manager = StreamManager.__new__(StreamManager)
    pooled = MagicMock()
    fake_process = MagicMock()
    fake_process.output_media_info = {
        "resolution": "1280x720",
        "video_codec": "h264",
        "audio_codec": "aac",
        "audio_channels": "stereo",
        "container": "MPEGTS",
        "fps": 25.0,
        "bitrate_kbps": 2500.0,
    }
    pooled.shared_processes = {"key-out": fake_process}
    manager.pooled_manager = pooled

    stream = StreamInfo(
        stream_id="t-stream",
        original_url="http://example.com/t.ts",
        created_at=datetime.now(timezone.utc),
        last_access=datetime.now(timezone.utc),
        transcode_stream_key="key-out",
    )

    info = manager._get_output_media_info(stream)

    assert info["resolution"] == "1280x720"
    assert info["video_codec"] == "h264"
    assert info["audio_codec"] == "aac"
    assert info["audio_channels"] == "stereo"
    assert info["container"] == "MPEGTS"
    assert info["fps"] == 25.0
    assert info["bitrate_kbps"] == 2500.0


def test_force_stop_stream_does_not_clobber_concurrent_reinsertion():
    """
    Regression for failover race: force_stop_stream must tear down the process
    it was called for, even if a concurrent get_or_create_shared_stream
    re-inserts a brand-new SharedTranscodingProcess at the same stream_key
    while the old one is being awaited. Otherwise the freshly-started failover
    transcoder gets killed seconds after it starts and the stream goes dark.
    """
    import asyncio
    from pooled_stream_manager import PooledStreamManager

    async def _run():
        # Build a manager without the redis/event-loop setup; we only exercise
        # the in-memory force_stop_stream path.
        manager = PooledStreamManager.__new__(PooledStreamManager)
        manager.shared_processes = {}
        manager.client_streams = {}
        manager.stream_key_to_id = {}
        manager.redis_client = None
        manager.worker_id = "test-worker"

        # The "old" process — replace its async methods with stubs so we don't
        # actually need a running ffmpeg.
        old = _make_process()
        old.process = None  # cleanup() short-circuits when there's no process
        old.clients = {"client-a": 0.0}

        async def remove_client(client_id):
            # During this await, another coroutine swaps in a new process at
            # the same stream_key — simulating the client reconnect that
            # caused the original race.
            new = _make_process()
            new.media_info["video_codec"] = "h264"
            manager.shared_processes["key-X"] = new
            old.clients.pop(client_id, None)

        old.remove_client = remove_client
        manager.shared_processes["key-X"] = old

        await manager.force_stop_stream("key-X")

        # The new process inserted mid-flight must still be present — only the
        # captured 'old' reference should have been torn down.
        survivor = manager.shared_processes.get("key-X")
        assert survivor is not None, "force_stop_stream destroyed the new process"
        assert survivor is not old
        assert survivor.media_info.get("video_codec") == "h264"

    asyncio.run(_run())
