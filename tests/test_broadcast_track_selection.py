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


def test_embedded_subtitle_maps_by_language_when_set():
    """With subtitles_enabled + subtitle_language, the proxy maps 0:s:m:language:XX?
    instead of the generic 0:s? — this lets a per-item override pick a specific
    embedded subtitle track out of several rather than always getting the first."""
    cmd = _build_cmd(subtitles_enabled=True, subtitle_language="jpn")
    assert "0:s:m:language:jpn?" in cmd
    assert "0:s?" not in cmd


def test_embedded_subtitle_falls_back_to_generic_map_without_language():
    """Without subtitle_language, embedded subtitles still map via the generic
    0:s? (any subtitle) — unchanged behavior for callers that don't know the language."""
    cmd = _build_cmd(subtitles_enabled=True, subtitle_language=None)
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


# ---------------------------------------------------------------------------
# External subtitle (Emby sidecar) tests
# ---------------------------------------------------------------------------

SUBTITLE_URL = "http://emby.local/Videos/1/ms_1/Subtitles/2/Stream.srt?api_key=abc"


def _subtitle_map(cmd):
    """Extract the subtitle -map value from an FFmpeg argv."""
    for i, v in enumerate(cmd):
        if (
            v == "-map"
            and i + 1 < len(cmd)
            and cmd[i + 1].endswith(":s?")
            or (v == "-map" and i + 1 < len(cmd) and ":s:" in cmd[i + 1])
        ):
            return cmd[i + 1]
    return None


def test_external_subtitle_added_as_second_input():
    """subtitle_url is added as a second -i after the video input."""
    cmd = _build_cmd(subtitle_url=SUBTITLE_URL)
    # Two -i flags: video (input 0) + subtitle (input 1)
    input_indices = [i for i, v in enumerate(cmd) if v == "-i"]
    assert len(input_indices) == 2
    assert cmd[input_indices[1] + 1] == SUBTITLE_URL


def test_external_subtitle_maps_1s_not_0s():
    """External subtitle maps 1:s:0? (input 1), NOT 0:s? (embedded)."""
    cmd = _build_cmd(subtitle_url=SUBTITLE_URL)
    assert "1:s:0?" in cmd
    assert "0:s?" not in cmd


def test_external_subtitle_takes_precedence_over_embedded():
    """When both subtitle_url and subtitles_enabled are set, external wins."""
    cmd = _build_cmd(subtitle_url=SUBTITLE_URL, subtitles_enabled=True)
    assert "1:s:0?" in cmd
    assert "0:s?" not in cmd


def test_external_subtitle_no_seek_when_server_rebased():
    """subtitle_seek_seconds=0 means the server already rebased cues; no -ss on sub input."""
    cmd = _build_cmd(subtitle_url=SUBTITLE_URL, subtitle_seek_seconds=0.0)
    # The only -ss should be the video seek (none when seek_seconds=0 default).
    # There must NOT be a -ss immediately before the subtitle -i.
    sub_input_idx = (
        cmd.index("-i", cmd.index(SUBTITLE_URL) - 1) if SUBTITLE_URL in cmd else -1
    )
    assert sub_input_idx > 0
    assert cmd[sub_input_idx - 1] != "-ss"


def test_external_subtitle_seeks_locally_when_needed():
    """subtitle_seek_seconds>0 means the proxy must -ss the subtitle input locally."""
    cmd = _build_cmd(subtitle_url=SUBTITLE_URL, subtitle_seek_seconds=2715.0)
    # Find the subtitle -i and verify -ss precedes it
    sub_i_idx = [i for i, v in enumerate(cmd) if v == "-i"][1]
    assert cmd[sub_i_idx - 2] == "-ss"
    assert cmd[sub_i_idx - 1] == "2715.0"


def test_external_subtitle_adds_cs_copy_when_not_transcoding():
    """External subtitle adds -c:s copy in passthrough (non-transcode) mode."""
    cmd = _build_cmd(subtitle_url=SUBTITLE_URL, transcode=False)
    assert "-c:s" in cmd
    assert cmd[cmd.index("-c:s") + 1] == "copy"


def test_external_subtitle_metadata_language_tag():
    """subtitle_language is passed as FFmpeg metadata for the HLS variant."""
    cmd = _build_cmd(subtitle_url=SUBTITLE_URL, subtitle_language="eng")
    assert "-metadata:s:s:0" in cmd
    meta_idx = cmd.index("-metadata:s:s:0") + 1
    assert "language=eng" in cmd[meta_idx]


def test_external_subtitle_no_metadata_when_language_absent():
    """No -metadata:s:s:0 when subtitle_language is not set."""
    cmd = _build_cmd(subtitle_url=SUBTITLE_URL)
    assert "-metadata:s:s:0" not in cmd


def test_external_subtitle_with_audio_language_combined():
    """External subtitle + preferred audio language work together."""
    cmd = _build_cmd(
        subtitle_url=SUBTITLE_URL,
        subtitle_language="eng",
        preferred_audio_language="jpn",
    )
    assert "0:a:m:language:jpn?" in cmd
    assert "1:s:0?" in cmd
    assert "-c:s" in cmd
    assert "language=eng" in cmd


def test_no_subtitle_input_when_url_absent():
    """No second -i and no 1:s map when subtitle_url is not set."""
    cmd = _build_cmd()
    input_count = cmd.count("-i")
    assert input_count == 1
    assert "1:s:0?" not in cmd


def test_empty_subtitle_url_ignored():
    """Empty/whitespace subtitle_url is treated as no subtitle."""
    cmd = _build_cmd(subtitle_url="   ")
    input_count = cmd.count("-i")
    assert input_count == 1
    assert "1:s:0?" not in cmd
