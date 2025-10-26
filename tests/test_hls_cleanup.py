import sys
import asyncio
import types
import shutil
import os
import tempfile
import pytest

# Ensure src is on path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


@pytest.mark.asyncio
async def test_hls_shared_process_cleanup(monkeypatch, tmp_path):
    """Simulate an HLS shared transcoder: client connects, then disconnects, and
    the pooled manager should schedule and execute cleanup of the shared process.
    """
    # Import the module under test
    import pooled_stream_manager as psm

    # Stub out start_process to avoid launching real ffmpeg and to create an hls_dir
    async def fake_start(self):
        # Create a per-stream hls_dir under the pytest tmp_path
        base_dir = str(tmp_path)
        os.makedirs(base_dir, exist_ok=True)
        self.hls_dir = tempfile.mkdtemp(prefix=f"m3u_proxy_hls_{self.stream_id}_", dir=base_dir)
        # Touch an index.m3u8 file to simulate ffmpeg producing a playlist
        with open(os.path.join(self.hls_dir, 'index.m3u8'), 'w') as fh:
            fh.write('#EXTM3U\n')

        # Simulate a running process
        self.process = types.SimpleNamespace(pid=99999, returncode=None)
        self.status = 'running'
        return True

    # Stub out cleanup to avoid killing real processes, but simulate directory removal
    async def fake_cleanup(self):
        try:
            if self.hls_dir and os.path.isdir(self.hls_dir):
                shutil.rmtree(self.hls_dir)
        except Exception:
            pass
        self.status = 'stopped'

    monkeypatch.setattr(psm.SharedTranscodingProcess, 'start_process', fake_start)
    monkeypatch.setattr(psm.SharedTranscodingProcess, 'cleanup', fake_cleanup)

    manager = psm.PooledStreamManager(enable_sharing=False)

    # Create an HLS-style ffmpeg args list so the SharedTranscodingProcess will enter hls mode
    ffmpeg_args = ['-i', 'http://example.com/playlist.m3u8', '-c:v', 'libx264', '-f', 'hls']

    # Create shared stream and register one client
    stream_key, proc = await manager.get_or_create_shared_stream(
        url='http://example.com/playlist.m3u8',
        profile='custom',
        ffmpeg_args=ffmpeg_args,
        client_id='client_test_1',
        user_agent='pytest',
        headers=None,
    )

    assert stream_key in manager.shared_processes
    shared = manager.shared_processes[stream_key]
    assert shared.status == 'running'
    assert os.path.isdir(shared.hls_dir)

    # Now remove the client. This should schedule a delayed cleanup (grace period)
    await manager.remove_client_from_stream('client_test_1')

    # Wait longer than the grace period used by manager._delayed_cleanup_if_empty (default 1s)
    await asyncio.sleep(2.0)

    # After the grace period and cleanup, the shared process should be gone
    assert stream_key not in manager.shared_processes
