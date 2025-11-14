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
    """Create a fake HLS temp dir older than threshold and ensure GC removes it.
    
    Updated to match new GC behavior: only empty directories are removed.
    FFmpeg handles segment deletion via -hls_delete_threshold.
    """
    from pooled_stream_manager import PooledStreamManager

    # Instantiate manager first so we know which base dir GC will scan
    manager = PooledStreamManager(enable_sharing=False)

    tmpdir = getattr(manager, 'hls_base_dir', tempfile.gettempdir())
    # Create a fake HLS dir with the expected prefix inside the manager's base dir
    fake_name = f"m3u_proxy_hls_test_{int(time.time())}_{os.getpid()}"
    fake_path = os.path.join(tmpdir, fake_name)
    os.makedirs(fake_path, exist_ok=True)

    # Don't create any files inside - the new GC only removes EMPTY directories
    # This simulates a directory that was left behind after FFmpeg cleaned up its segments

    # Set mtime to an older time (2 hours ago)
    old_time = time.time() - (2 * 60 * 60)
    os.utime(fake_path, (old_time, old_time))

    # Ensure directory exists before GC
    assert os.path.isdir(fake_path)

    # Force threshold to 1 hour (3600s) which is less than age we set (2h)
    manager.hls_gc_age_threshold = 60 * 60

    # Run GC
    await manager._gc_hls_temp_dirs()

    # Directory should be removed (since it's empty and old)
    assert not os.path.exists(fake_path), "GC should remove empty old directories"

    # Test that non-empty directories are NOT removed
    # Create another old directory with content
    fake_name_nonempty = f"m3u_proxy_hls_test_{int(time.time())}_{os.getpid()}_nonempty"
    fake_path_nonempty = os.path.join(tmpdir, fake_name_nonempty)
    os.makedirs(fake_path_nonempty, exist_ok=True)
    
    # Add a file
    with open(os.path.join(fake_path_nonempty, 'segment.ts'), 'w') as fh:
        fh.write('fake segment data')
    
    # Make it old
    os.utime(fake_path_nonempty, (old_time, old_time))
    
    # Run GC again
    await manager._gc_hls_temp_dirs()
    
    # Non-empty directory should still exist
    assert os.path.exists(fake_path_nonempty), "GC should NOT remove non-empty directories"
    
    # Clean up
    shutil.rmtree(fake_path_nonempty, ignore_errors=True)
