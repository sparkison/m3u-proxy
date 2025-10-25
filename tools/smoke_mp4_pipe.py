"""
Smoke test to run ffmpeg with mp4 -> pipe:1 and observe stdout/stderr throughput.
Usage: python tools/smoke_mp4_pipe.py <input_url> [duration_seconds]

This script will run ffmpeg with recommended low-latency flags for fragmented MP4
and print timestamps and sizes of stdout chunks as they arrive. It's tolerant of
short gaps (it keeps running across 1s timeouts) so we can observe initial
fragmentation delays (e.g., waiting for first keyframe).
"""

import asyncio
import sys
import time

FFMPEG_TEMPLATE = [
    "-hide_banner",
    "-loglevel",
    "info",
    "-i",
    "{input}",
    # encoding: copy video/audio to avoid heavy CPU in smoke tests; change to libx264/aac
    # if you want to exercise re-encoding
    "-c:v",
    "copy",
    "-c:a",
    "copy",
    # fragmented mp4 flags
    "-movflags",
    "+frag_keyframe+empty_moov+separate_moof+omit_tfhd_offset+default_base_moof",
    "-f",
    "mp4",
    "pipe:1",
]


async def reader(name, stream, on_chunk):
    """Read from stream and call on_chunk(bytes) for each chunk.
    Keeps running until EOF. On short (1s) timeouts it prints a notice but continues.
    """
    total = 0
    while True:
        try:
            chunk = await asyncio.wait_for(stream.read(65536), timeout=1.0)
        except asyncio.TimeoutError:
            print(f"[{name}] no data for 1s... continuing")
            continue
        if not chunk:
            print(f"[{name}] EOF")
            break
        total += len(chunk)
        on_chunk(chunk)
    return total


async def run(input_url, duration=20):
    args = ["ffmpeg"] + [a.format(input=input_url) for a in FFMPEG_TEMPLATE]
    print("Running:", " ".join(args))

    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )

    start = time.time()

    last_stdout_time = None

    def on_stdout(chunk):
        nonlocal last_stdout_time
        now = time.time()
        print(f"[stdout] +{now-start:0.3f}s chunk={len(chunk)} bytes")
        last_stdout_time = now

    def on_stderr(chunk):
        # print stderr lines with timestamp (decode best-effort)
        try:
            s = chunk.decode("utf-8", errors="replace")
        except Exception:
            s = repr(chunk)
        for line in s.splitlines():
            print(f"[stderr] +{time.time()-start:0.3f}s {line}")

    # start readers
    stdout_task = asyncio.create_task(reader("stdout", proc.stdout, on_stdout))
    stderr_task = asyncio.create_task(reader("stderr", proc.stderr, on_stderr))

    try:
        while True:
            now = time.time()
            if now - start > duration:
                print("Duration elapsed, terminating ffmpeg")
                proc.terminate()
                break
            # if ffmpeg exited early
            if proc.returncode is not None:
                break
            await asyncio.sleep(0.2)
    finally:
        # wait for process to finish
        try:
            await proc.wait()
        except Exception:
            pass
        # cancel readers if still running
        for t in (stdout_task, stderr_task):
            if not t.done():
                t.cancel()
        # gather results
        stdout_total = 0
        stderr_total = 0
        try:
            stdout_total = await stdout_task
        except asyncio.CancelledError:
            pass
        try:
            stderr_total = await stderr_task
        except asyncio.CancelledError:
            pass
        print(f"Finished: returncode={proc.returncode}, stdout_total={stdout_total}, stderr_total={stderr_total}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python tools/smoke_mp4_pipe.py <input_url> [duration_seconds]")
        sys.exit(2)
    url = sys.argv[1]
    duration = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    asyncio.run(run(url, duration))
