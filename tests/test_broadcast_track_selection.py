"""Tests for preferred_audio_language and subtitles_enabled in broadcast FFmpeg commands."""

from src.broadcast_manager import BroadcastConfig, NetworkBroadcastProcess


def _build_cmd(**overrides) -> list:
    """Build an FFmpeg command from a BroadcastConfig with the given overrides."""
    defaults = dict(
        network_id="test-net",
        stream_url="http://example.com/stream.ts",
    )
    defaults.update(overrides)
    config = BroadcastConfig(**defaults)
    proc = NetworkBroadcastProcess.__new__(NetworkBroadcastProcess)
    proc.config = config
    proc.hls_dir = "/tmp/hls"
    return proc._build_ffmpeg_command()


def _audio_map(cmd):
    """Extract the audio -map value from an FFmpeg argv."""
    for i, v in enumerate(cmd):
        if v == "-map" and i + 1 < len(cmd) and cmd[i + 1].startswith("0:a"):
            return cmd[i + 1]
    return None


def test_default_maps_first_audio():
    """Without preferred_audio_language the proxy maps 0:a:0? (first audio)."""
    cmd = _build_cmd()
    assert _audio_map(cmd) == "0:a:0?"


def test_preferred_audio_language_maps_by_language():
    """With preferred_audio_language the proxy maps 0:a:m:language:XX?."""
    cmd = _build_cmd(preferred_audio_language="jpn")
    assert _audio_map(cmd) == "0:a:m:language:jpn?"


def test_preferred_audio_language_eng():
    """English audio language selection."""
    cmd = _build_cmd(preferred_audio_language="eng")
    assert "0:a:m:language:eng?" in cmd


def test_no_subtitle_map_when_disabled():
    """Subtitles are not mapped when subtitles_enabled is False (default)."""
    cmd = _build_cmd()
    assert "0:s?" not in cmd


def test_subtitle_map_when_enabled():
    """Subtitles are mapped via 0:s? when subtitles_enabled is True."""
    cmd = _build_cmd(subtitles_enabled=True)
    assert "0:s?" in cmd


def test_subtitle_codec_copy_when_enabled_and_not_transcoding():
    """When not transcoding, subtitles_enabled adds -c:s copy for passthrough."""
    cmd = _build_cmd(subtitles_enabled=True, transcode=False)
    assert "-c:s" in cmd
    copy_idx = cmd.index("-c:s") + 1
    assert cmd[copy_idx] == "copy"


def test_no_subtitle_codec_when_disabled():
    """When subtitles_enabled is False, -c:s is not present."""
    cmd = _build_cmd(subtitles_enabled=False, transcode=False)
    assert "-c:s" not in cmd


def test_audio_language_and_subtitles_combined():
    """Both preferred_audio_language and subtitles_enabled can be active together."""
    cmd = _build_cmd(preferred_audio_language="fra", subtitles_enabled=True)
    assert "0:a:m:language:fra?" in cmd
    assert "0:s?" in cmd
    assert "-c:s" in cmd


def test_video_map_always_present():
    """Video is always mapped (0:v:0?) regardless of audio/subtitle prefs."""
    cmd = _build_cmd(preferred_audio_language="eng", subtitles_enabled=True)
    assert "0:v:0?" in cmd
