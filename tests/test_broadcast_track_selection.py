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
    """With preferred_audio_language the proxy maps 0:a:m:language:XX (no trailing '?' — FFmpeg's metadata stream specifier doesn't support it)."""
    cmd = _build_cmd(preferred_audio_language="jpn")
    assert _audio_map(cmd) == "0:a:m:language:jpn"


def test_preferred_audio_language_eng():
    """English audio language selection."""
    cmd = _build_cmd(preferred_audio_language="eng")
    assert "0:a:m:language:eng" in cmd


def test_numeric_preferred_audio_maps_by_type_relative_position():
    """A per-item override resolves to a numeric type-relative stream position
    (e.g. "1" = the 2nd audio stream) rather than a language — the proxy must map
    it with a plain index specifier (0:a:N?), not the metadata form, since a plain
    index degrades gracefully via '?' while the metadata form does not."""
    cmd = _build_cmd(preferred_audio_language="1")
    assert _audio_map(cmd) == "0:a:1?"
    assert "0:a:m:language:1" not in cmd


def test_no_subtitle_map_when_disabled():
    """Subtitles are not mapped when subtitles_enabled is False (default)."""
    cmd = _build_cmd()
    assert "0:s?" not in cmd


def test_subtitle_map_when_enabled():
    """Embedded subtitles are mapped via 1:s? (their own second, un-throttled
    input — see add_source_input's realtime note) when subtitles_enabled is True."""
    cmd = _build_cmd(subtitles_enabled=True)
    assert "1:s?" in cmd


def test_embedded_subtitle_maps_by_language_when_set():
    """With subtitles_enabled + subtitle_language, the proxy maps 1:s:m:language:XX
    (no trailing '?') instead of the generic 1:s? — this lets a per-item override
    pick a specific embedded subtitle track out of several rather than always
    getting the first."""
    cmd = _build_cmd(subtitles_enabled=True, subtitle_language="jpn")
    assert "1:s:m:language:jpn" in cmd
    assert "1:s?" not in cmd


def test_embedded_subtitle_falls_back_to_generic_map_without_language():
    """Without subtitle_language, embedded subtitles still map via the generic
    1:s? (any subtitle) — unchanged behavior for callers that don't know the language."""
    cmd = _build_cmd(subtitles_enabled=True, subtitle_language=None)
    assert "1:s?" in cmd


def test_numeric_subtitle_language_maps_by_type_relative_position():
    """A per-item override resolves to a numeric type-relative subtitle stream
    position rather than a language — mapped with a plain (gracefully optional)
    index specifier, mirroring the audio case."""
    cmd = _build_cmd(subtitles_enabled=True, subtitle_language="0")
    assert "1:s:0?" in cmd
    assert "1:s:m:language:0" not in cmd


def test_embedded_subtitle_reads_from_its_own_untethered_second_input():
    """The real field bug: FFmpeg's -re real-time governor paces its shared
    demux loop using every mapped stream's packets, and a container's embedded
    subtitle packets are often extremely sparse/bursty enough to send the
    governor's reported lag climbing without bound — dragging video/audio
    segment output down to a crawl too (confirmed against a real broadcast:
    "Resumed reading ... after a lag of Ns" climbing from 0.6s to 50s+ within a
    minute). Reading the embedded subtitle track from its own second input,
    with NO `-re` in front of it, sidesteps the governor entirely — verified
    against the same broadcast, segment cadence returned to normal immediately."""
    cmd = _build_cmd(subtitles_enabled=True)

    input_indices = [i for i, v in enumerate(cmd) if v == "-i"]
    assert len(input_indices) == 2
    # Both inputs read the same stream_url (the subtitle track lives in the
    # same container as the video/audio).
    assert cmd[input_indices[0] + 1] == "http://example.com/stream.ts"
    assert cmd[input_indices[1] + 1] == "http://example.com/stream.ts"

    # Exactly one -re, and it must precede the FIRST -i, not the second.
    assert cmd.count("-re") == 1
    re_idx = cmd.index("-re")
    assert re_idx < input_indices[0]
    assert not (input_indices[0] < re_idx < input_indices[1])


def test_no_subtitle_codec_override_when_enabled_and_not_transcoding():
    """Even when not transcoding video/audio, subtitles must NOT get `-c:s copy`:
    HLS's .vtt segments must be real WebVTT, and a source in SRT/ASS copied
    as-is only superficially resembles WebVTT (comma decimal separators, no
    "WEBVTT" header) — some players render the first cue and silently drop
    every cue after it. Leaving -c:s unset lets FFmpeg's HLS muxer apply its
    own default (real webvtt transcoding) regardless of the copy mode."""
    cmd = _build_cmd(subtitles_enabled=True, transcode=False)
    assert "-c:s" not in cmd


def test_no_subtitle_codec_when_disabled():
    """When subtitles_enabled is False, -c:s is not present."""
    cmd = _build_cmd(subtitles_enabled=False, transcode=False)
    assert "-c:s" not in cmd


def test_audio_language_and_subtitles_combined():
    """Both preferred_audio_language and subtitles_enabled can be active together."""
    cmd = _build_cmd(preferred_audio_language="fra", subtitles_enabled=True)
    assert "0:a:m:language:fra" in cmd
    assert "1:s?" in cmd
    # No explicit -c:s override — see test_no_subtitle_codec_override_when_enabled_and_not_transcoding.
    assert "-c:s" not in cmd


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


def test_external_subtitle_has_no_codec_override_when_not_transcoding():
    """External subtitle must NOT get `-c:s copy` either, for the same reason as
    the embedded-subtitle case: the sidecar file is typically SRT, and HLS's
    .vtt segments need real WebVTT, not an as-is copy."""
    cmd = _build_cmd(subtitle_url=SUBTITLE_URL, transcode=False)
    assert "-c:s" not in cmd


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
    assert "0:a:m:language:jpn" in cmd
    assert "1:s:0?" in cmd
    assert "-c:s" not in cmd
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


def test_itsoffset_compensates_embedded_subtitle_second_input():
    """Confirmed live (Plex, via Safari): the embedded-subtitle second input's
    cues consistently show up ~1s early relative to the dialogue they belong
    to, since it's a separate, independently-zeroed input from the primary
    (see add_source_input's realtime note). A positive -itsoffset delays this
    input's local zero point by the same amount, cancelling the lead — tuned
    empirically (1.5 overcorrected to late, 1.0 lines up correctly). An
    earlier attempt at this used -copyts + -avoid_negative_ts instead, but
    -copyts also disables FFmpeg's timestamp-discontinuity sanitization, which
    broke live playback entirely against a real Plex source."""
    cmd = _build_cmd(subtitles_enabled=True)
    assert "-copyts" not in cmd
    assert "-itsoffset" in cmd
    assert cmd[cmd.index("-itsoffset") + 1] == "1.0"
    input_indices = [i for i, v in enumerate(cmd) if v == "-i"]
    itsoffset_idx = cmd.index("-itsoffset")
    # -itsoffset must precede the second (subtitle) input, not the primary one.
    assert input_indices[0] < itsoffset_idx < input_indices[1]
    assert "-avoid_negative_ts" in cmd
    assert cmd[cmd.index("-avoid_negative_ts") + 1] == "make_zero"
    assert cmd.index("-avoid_negative_ts") > input_indices[1]


def test_no_itsoffset_for_external_subtitle():
    """External subtitle sidecars are a full-file fetch (byte-precise local
    seek), not a live re-connect, so they aren't subject to the same landing
    gap and get no compensating -itsoffset."""
    cmd = _build_cmd(subtitle_url="http://example.com/subs.srt")
    assert "-itsoffset" not in cmd
    assert "-avoid_negative_ts" not in cmd


def test_no_itsoffset_when_subtitles_disabled():
    cmd = _build_cmd()
    assert "-itsoffset" not in cmd
    assert "-avoid_negative_ts" not in cmd
    assert "-copyts" not in cmd
