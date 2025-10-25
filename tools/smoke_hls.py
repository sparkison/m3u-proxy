import asyncio
import os
import time
import sys
import tempfile

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from pooled_stream_manager import PooledStreamManager

async def main():
    print('Starting smoke HLS test')
    pm = PooledStreamManager(enable_sharing=False)
    await pm.start()

    # Use a lavfi testsrc as input and ask ffmpeg to produce HLS
    ffmpeg_args = [
        '-f', 'lavfi',
        '-i', 'testsrc=size=320x240:rate=10',
        '-c:v', 'libx264',
        '-preset', 'veryfast',
        '-pix_fmt', 'yuv420p',
        '-profile:v', 'baseline',
        '-level', '3.0',
        '-g', '25',
        '-hls_time', '1',
        '-hls_list_size', '0',
        '-f', 'hls',
        'index.m3u8'
    ]

    try:
        stream_key, proc = await pm.get_or_create_shared_stream(
            url='testsrc',
            profile='hls_test',
            ffmpeg_args=ffmpeg_args,
            client_id='smoke-client',
            user_agent='smoke-test'
        )

        print('Started shared process, stream_key=', stream_key, 'mode=', getattr(proc, 'mode', None))

        # Wait for ffmpeg to produce files
        for i in range(15):
            playlist = await proc.read_playlist()
            if playlist:
                print('Playlist found (len=%d)' % len(playlist))
                print(playlist.splitlines()[:10])
                break
            print('Waiting for playlist... (%d)' % i)
            await asyncio.sleep(0.5)

        if not playlist:
            print('No playlist produced after wait; check ffmpeg availability and args')
            return 2

        # List files
        print('HLS dir:', proc.hls_dir)
        files = os.listdir(proc.hls_dir)
        print('Files in hls dir:', files)

        # Check for at least one .ts or .m4s segment
        segs = [f for f in files if f.endswith('.ts') or f.endswith('.m4s') or f.endswith('.mp4')]
        print('Segments found:', segs)

        # Cleanup
        await proc.cleanup()
        await pm.stop()
        return 0

    except Exception as e:
        print('Error during smoke test:', e)
        try:
            await pm.stop()
        except:
            pass
        return 3

if __name__ == '__main__':
    rc = asyncio.run(main())
    print('Exit code', rc)
    sys.exit(rc)
