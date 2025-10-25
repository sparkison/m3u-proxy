"""
Test for HLS temp-dir garbage collection in PooledStreamManager
"""
import sys
import os
import tempfile
import time
import shutil

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import pytest


@pytest.mark.asyncio
async def test_hls_gc_removes_old_dirs():
    """Create a fake HLS temp dir older than threshold and ensure GC removes it."""
    from pooled_stream_manager import PooledStreamManager

    tmpdir = tempfile.gettempdir()
    # Create a fake HLS dir with the expected prefix
    fake_name = f"m3u_proxy_hls_test_{int(time.time())}_{os.getpid()}"
    fake_path = os.path.join(tmpdir, fake_name)
    os.makedirs(fake_path, exist_ok=True)

    # Create a dummy file inside
    with open(os.path.join(fake_path, 'index.m3u8'), 'w') as fh:
        fh.write('#EXTM3U\n')

    # Set mtime to an older time (2 hours ago)
    old_time = time.time() - (2 * 60 * 60)
    os.utime(fake_path, (old_time, old_time))

    # Ensure directory exists before GC
    assert os.path.isdir(fake_path)

    # Instantiate manager and set threshold low enough to remove
    manager = PooledStreamManager(enable_sharing=False)
    # Force threshold to 1 hour (3600s) which is less than age we set (2h)
    manager.hls_gc_age_threshold = 60 * 60

    # Run GC
    await manager._gc_hls_temp_dirs()

    # Directory should be removed
    assert not os.path.exists(fake_path)
