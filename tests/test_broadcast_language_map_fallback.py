"""
Tests for the reactive language-map fallback in NetworkBroadcastProcess.start().

FFmpeg's metadata-based `-map 0:X:m:language:YY` stream specifier does not
support an optional-map '?' suffix at all (confirmed against a real FFmpeg build:
appending '?' fails "Invalid argument" even when a stream DOES match). So
_build_ffmpeg_command() never appends '?' to it — it fails "Failed to set value
'...' for option 'map': Invalid argument" and FFmpeg exits immediately only when
the language genuinely matches zero streams. Rather
than probing the source upfront, _retry_without_broken_language_maps() gives the
spawned process a brief window to hit that failure and retries without whichever
language caused it, reading which map failed from FFmpeg's own error message
rather than assuming it's always the audio one — a source can fail on the audio
map, the subtitle map, or both (e.g. an audio-only source with both
preferred_audio_language and a subtitle language configured), and each needs its
own fallback.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.broadcast_manager import BroadcastConfig, NetworkBroadcastProcess


def _make_process(**config_overrides) -> NetworkBroadcastProcess:
    defaults = dict(
        network_id="test-net",
        stream_url="http://example.com/stream.ts",
    )
    defaults.update(config_overrides)
    config = BroadcastConfig(**defaults)
    proc = NetworkBroadcastProcess.__new__(NetworkBroadcastProcess)
    proc.config = config
    proc.network_id = config.network_id
    proc.hls_dir = "/tmp/hls"
    return proc


def _fake_process(stderr: bytes = b"", returncode: int = 1):
    """A fake asyncio.subprocess.Process double that has already exited.

    Uses a plain MagicMock as the container (not AsyncMock) — only .wait() and
    .stderr.read() need to be awaitable, and they're explicitly given AsyncMocks
    below. An AsyncMock container makes every unconfigured attribute access
    awaitable too, which left a dangling "coroutine was never awaited" warning
    from something touching an attribute this test never actually uses.
    """
    process = MagicMock()
    process.returncode = returncode
    process.wait = AsyncMock(return_value=returncode)
    process.stderr = MagicMock()
    process.stderr.read = AsyncMock(return_value=stderr)
    return process


async def _timeout_without_awaiting(coro, timeout):
    """Stand-in for asyncio.wait_for() that times out immediately without ever
    awaiting the coroutine it was given — matching a process that's still
    running when the grace window ends. Must close() the unawaited coroutine
    itself (the real wait_for would eventually consume it); otherwise it's
    garbage-collected later with a "coroutine was never awaited" warning
    attributed to whatever unrelated test happens to be running at GC time."""
    coro.close()
    raise asyncio.TimeoutError()


@pytest.mark.asyncio
async def test_returns_same_process_when_still_running_after_grace_window():
    """The healthy/common case: FFmpeg is still running after the grace window
    (both maps succeeded) — no retry, no second process spawned. Simulated by
    making asyncio.wait_for time out, exactly as it would for a process that
    outlives the grace window (the fake process itself doesn't need to actually
    hang for 1.5s)."""
    proc = _make_process(preferred_audio_language="jpn")
    proc.process = _fake_process()

    with (
        patch(
            "src.broadcast_manager.asyncio.wait_for",
            AsyncMock(side_effect=_timeout_without_awaiting),
        ),
        patch(
            "src.broadcast_manager.asyncio.create_subprocess_exec", AsyncMock()
        ) as mock_exec,
    ):
        result = await proc._retry_without_broken_language_maps()

    assert result is proc.process
    mock_exec.assert_not_called()
    assert proc.config.preferred_audio_language == "jpn"


@pytest.mark.asyncio
async def test_returns_same_process_when_exit_is_unrelated_to_map():
    """FFmpeg exited quickly, but for a reason unrelated to -map (e.g. bad input
    URL) — leave it alone for the normal failure-handling path to deal with."""
    proc = _make_process(preferred_audio_language="jpn")
    proc.process = _fake_process(
        stderr=b"Error opening input: No such file or directory"
    )

    with patch(
        "src.broadcast_manager.asyncio.create_subprocess_exec", AsyncMock()
    ) as mock_exec:
        result = await proc._retry_without_broken_language_maps()

    assert result is proc.process
    mock_exec.assert_not_called()
    assert proc.config.preferred_audio_language == "jpn"


@pytest.mark.asyncio
async def test_retries_without_audio_language_on_audio_map_failure():
    """FFmpeg rejects '0:a:m:language:afr' because no audio stream matches —
    clears preferred_audio_language and spawns a replacement process."""
    proc = _make_process(preferred_audio_language="afr")
    proc.process = _fake_process(
        stderr=(
            b"Failed to set value '0:a:m:language:afr' for option 'map': Invalid argument\n"
            b"Error parsing options for output file live.m3u8.\n"
        ),
    )

    replacement = _fake_process(returncode=0)
    with patch(
        "src.broadcast_manager.asyncio.create_subprocess_exec",
        AsyncMock(return_value=replacement),
    ) as mock_exec:
        result = await proc._retry_without_broken_language_maps()

    assert result is replacement
    mock_exec.assert_called_once()
    assert proc.config.preferred_audio_language is None

    retried_cmd = mock_exec.call_args.args
    assert "0:a:m:language:afr" not in retried_cmd
    assert "0:a:0?" in retried_cmd


@pytest.mark.asyncio
async def test_retries_without_subtitle_language_on_subtitle_map_failure():
    """The exact field bug: an audio-only source (no subtitle tracks at all) with
    BOTH preferred_audio_language and a subtitle language set to 'eng'. The audio
    map succeeds (it really is English) but the subtitle map fails since there's no
    subtitle stream to match at all. Must clear subtitle_language, NOT
    preferred_audio_language — an earlier version of this always blamed audio
    regardless of which map actually failed, so the retry kept the exact same
    broken subtitle map and crashed again identically."""
    proc = _make_process(
        preferred_audio_language="eng",
        subtitles_enabled=True,
        subtitle_language="eng",
    )
    proc.process = _fake_process(
        stderr=(
            b"Failed to set value '0:s:m:language:eng' for option 'map': Invalid argument\n"
            b"Error parsing options for output file live.m3u8.\n"
        ),
    )

    replacement = _fake_process(returncode=0)
    with patch(
        "src.broadcast_manager.asyncio.create_subprocess_exec",
        AsyncMock(return_value=replacement),
    ) as mock_exec:
        result = await proc._retry_without_broken_language_maps()

    assert result is replacement
    mock_exec.assert_called_once()
    assert proc.config.subtitle_language is None
    assert proc.config.preferred_audio_language == "eng"  # untouched — it was fine

    retried_cmd = mock_exec.call_args.args
    assert "0:s:m:language:eng" not in retried_cmd
    assert "0:s?" in retried_cmd
    # Audio map preference is preserved across the subtitle-only retry.
    assert "0:a:m:language:eng" in retried_cmd


@pytest.mark.asyncio
async def test_retries_twice_when_both_audio_and_subtitle_maps_fail():
    """Both preferred_audio_language and subtitle_language match zero streams —
    each failure only reveals one bad map at a time, so this must loop: first
    clears whichever map failed first, respawns, hits the second failure, clears
    that one too, and finally succeeds."""
    proc = _make_process(
        preferred_audio_language="afr",
        subtitles_enabled=True,
        subtitle_language="afr",
    )
    first_failure = _fake_process(
        stderr=b"Failed to set value '0:a:m:language:afr' for option 'map': Invalid argument\n",
    )
    second_failure = _fake_process(
        stderr=b"Failed to set value '0:s:m:language:afr' for option 'map': Invalid argument\n",
    )
    success = _fake_process(returncode=0)
    proc.process = first_failure

    with patch(
        "src.broadcast_manager.asyncio.create_subprocess_exec",
        AsyncMock(side_effect=[second_failure, success]),
    ) as mock_exec:
        result = await proc._retry_without_broken_language_maps()

    assert result is success
    assert mock_exec.call_count == 2
    assert proc.config.preferred_audio_language is None
    assert proc.config.subtitle_language is None
