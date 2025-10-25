import asyncio
import os
import time
import sys

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from pooled_stream_manager import PooledStreamManager

async def main():
    print('Starting smoke HLS debug test')
    pm = PooledStreamManager(enable_sharing=False)
    await pm.start()

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
        print('hls_dir=', getattr(proc, 'hls_dir', None))
        # Wait and print stderr lines for debugging
        if proc.process and proc.process.stderr:
            print('Reading stderr from ffmpeg for 5s...')
            end = time.time() + 5
            while time.time() < end:
                line = await proc.process.stderr.readline()
                if not line:
                    break
                try:
                    print('FFMPEG:', line.decode('utf-8', errors='ignore').strip())
                except Exception as e:
                    print('FFMPEG (raw):', line)
            print('Done reading stderr')

        playlist = await proc.read_playlist()
        print('Playlist read:', bool(playlist))
        if playlist:
            print('Playlist contents:\n', playlist)

        files = []
        if proc.hls_dir and os.path.isdir(proc.hls_dir):
            files = os.listdir(proc.hls_dir)
        print('Files in hls_dir:', files)

        await proc.cleanup()
        await pm.stop()
    except Exception as e:
        print('Error during debug smoke test:', e)
        try:
            await pm.stop()
        except:
            pass

if __name__ == '__main__':
    asyncio.run(main())
