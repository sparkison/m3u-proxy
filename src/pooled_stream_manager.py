"""
Enhanced stream manager with Redis support for shared transcoding processes.
Implements connection pooling and multi-worker coordination.
"""

import asyncio
import json
import time
import uuid
import hashlib
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple, Any
from urllib.parse import urlparse
import logging
from config import settings
import os
import tempfile
import shutil

logger = logging.getLogger(__name__)

try:
    import redis.asyncio as redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    logger.warning("Redis not available - falling back to single-worker mode")


class SharedTranscodingProcess:
    """Represents a shared FFmpeg transcoding process with broadcasting to multiple clients"""

    def __init__(self, stream_id: str, url: str, profile: str, ffmpeg_args: List[str], user_agent: Optional[str] = None, headers: Optional[Dict[str, str]] = None, hls_base_dir: Optional[str] = None):
        self.stream_id = stream_id
        self.url = url
        self.profile = profile
        self.ffmpeg_args = ffmpeg_args
        self.user_agent = user_agent
        self.headers = headers or {}
        # Base directory to create HLS per-stream directories in. If None,
        # the process will fall back to the system tempdir.
        self.hls_base_dir = hls_base_dir
        self.process: Optional[asyncio.subprocess.Process] = None
        self.clients: Dict[str, float] = {}  # client_id -> last_access_time
        self.created_at = time.time()
        self.last_access = time.time()
        self.total_bytes_served = 0
        self.status = "starting"

        # Broadcasting support - each client gets its own queue
        self.client_queues: Dict[str, asyncio.Queue] = {}
        self._broadcaster_task: Optional[asyncio.Task] = None
        self._broadcaster_lock = asyncio.Lock()

        self.last_chunk_time = time.time()  # Track when last chunk was produced
        self.output_timeout = 30  # Seconds without output before considering failed
        # Detect output mode (stdout stream vs HLS files)
        self.mode = "stdout"
        self.hls_dir: Optional[str] = None
        # If ffmpeg_args suggest HLS output, switch to hls mode
        joined_args = ' '.join(self.ffmpeg_args).lower()
        if '-hls_time' in joined_args or '-hls_list_size' in joined_args or '-f hls' in joined_args:
            self.mode = 'hls'
            # Determine base dir for HLS output
            base_dir = None
            if self.hls_base_dir:
                base_dir = self.hls_base_dir
            else:
                try:
                    base_dir = tempfile.gettempdir()
                except Exception:
                    base_dir = None

            # Ensure base dir exists if provided
            if base_dir:
                try:
                    os.makedirs(base_dir, exist_ok=True)
                except Exception:
                    pass

            # Create a per-stream directory for HLS segments
            try:
                self.hls_dir = tempfile.mkdtemp(prefix=f"m3u_proxy_hls_{self.stream_id}_", dir=base_dir)
            except Exception:
                # Fallback to system tempdir without dir param
                self.hls_dir = tempfile.mkdtemp(prefix=f"m3u_proxy_hls_{self.stream_id}_")

            logger.info(f"SharedTranscodingProcess {self.stream_id} will run in HLS mode, hls_dir={self.hls_dir}")

    async def start_process(self):
        """Start the FFmpeg process"""
        try:
            logger.info(
                f"Starting shared FFmpeg process for stream {self.stream_id}")

            # Build FFmpeg command - ensure output to stdout
            ffmpeg_cmd = ["ffmpeg"]

            # Add user agent / headers only for network inputs (http/rtsp/etc.)
            if self.user_agent and isinstance(self.url, str) and ('://' in self.url and not self.url.startswith('file://')):
                ffmpeg_cmd.extend(["-user_agent", self.user_agent])

            # Add headers if provided, ensuring proper format and only for network inputs
            if self.headers and isinstance(self.url, str) and ('://' in self.url and not self.url.startswith('file://')):
                header_str = "".join(
                    [f"{k}: {v}\r\n" for k, v in self.headers.items()])
                ffmpeg_cmd.extend(["-headers", header_str])

            ffmpeg_cmd.extend(self.ffmpeg_args)

            # If HLS mode, ensure we write to the hls_dir index.m3u8
            if self.mode == 'hls':
                # If the ffmpeg args already include an output filename, respect it
                # Otherwise append the playlist target into the hls dir
                playlist_path = os.path.join(self.hls_dir if self.hls_dir else tempfile.gettempdir(), 'index.m3u8')
                # If ffmpeg_args already specify an output playlist, replace any m3u8 token with absolute path
                replaced = False
                for i, token in enumerate(ffmpeg_cmd):
                    try:
                        if not isinstance(token, str):
                            continue
                        t_lower = token.lower()
                        # Skip tokens that look like input specs ("-i <url>" or "-i<url>")
                        if t_lower.endswith('.m3u8'):
                            prev = ffmpeg_cmd[i-1] if i > 0 else None
                            if isinstance(prev, str) and prev == '-i':
                                # This is an input URL; do NOT replace it with our output path
                                continue
                            if t_lower.startswith('-i') and t_lower[2:].endswith('.m3u8'):
                                # Token like '-ihttp://.../playlist.m3u8' - treat as input, skip
                                continue
                            # Otherwise this looks like an output playlist token, replace it
                            ffmpeg_cmd[i] = playlist_path
                            replaced = True
                    except Exception:
                        continue

                if not replaced:
                    # Remove any pipe outputs which are inappropriate for file-based HLS output
                    ffmpeg_cmd = [t for t in ffmpeg_cmd if not (isinstance(t, str) and t.startswith('pipe:'))]
                    # Append absolute playlist path as the intended HLS output
                    ffmpeg_cmd.append(playlist_path)

            else:
                # Ensure we're outputting to stdout in MPEGTS format only if no -f specified
                if "-f" not in [a.lower() for a in ffmpeg_cmd]:
                    ffmpeg_cmd.extend(["-f", "mpegts"])
                # Use pipe:1 for stdout-based broadcasting unless an output file is specified
                if "pipe:1" not in ffmpeg_cmd and "-" not in ffmpeg_cmd and self.mode == 'stdout':
                    ffmpeg_cmd.append("pipe:1")

            logger.info(f"FFmpeg command: {' '.join(ffmpeg_cmd)}")

            self.process = await asyncio.create_subprocess_exec(
                *ffmpeg_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            self.status = "running"
            logger.info(
                f"Shared FFmpeg process started with PID: {self.process.pid}")

            # Start stderr logging task
            asyncio.create_task(self._log_stderr())

            # Start broadcaster task to read from FFmpeg and send to all clients
            if self.mode == 'stdout':
                self._broadcaster_task = asyncio.create_task(
                    self._broadcast_loop())
            else:
                # In HLS mode, start a small watcher task to update last_chunk_time
                self._broadcaster_task = asyncio.create_task(self._hls_watch_loop())

            return True

        except Exception as e:
            logger.error(f"Failed to start shared FFmpeg process: {e}")
            self.status = "failed"
            return False

    async def _broadcast_loop(self):
        """Read from FFmpeg stdout and broadcast to all client queues"""
        if not self.process or not self.process.stdout:
            logger.error(
                f"Cannot start broadcaster - no process or stdout for {self.stream_id}")
            return

        logger.info(f"Starting broadcaster for stream {self.stream_id}")

        try:
            while self.process and self.process.returncode is None:
                # Read chunk from FFmpeg
                chunk = await self.process.stdout.read(32768)
                if not chunk:
                    logger.info(
                        f"FFmpeg stdout closed for stream {self.stream_id}")
                    break

                # Update last chunk time
                self.last_chunk_time = time.time()

                # Broadcast to all client queues
                async with self._broadcaster_lock:
                    dead_clients = []
                    for client_id, queue in self.client_queues.items():
                        try:
                            # Use put_nowait to avoid blocking if a client's queue is full
                            queue.put_nowait(chunk)
                        except asyncio.QueueFull:
                            # When the queue is full, remove the oldest chunk to make space
                            try:
                                queue.get_nowait()
                            except asyncio.QueueEmpty:
                                # Should not happen if QueueFull was just raised, but defensive
                                pass
                            # Retry putting the new chunk
                            try:
                                queue.put_nowait(chunk)
                            except asyncio.QueueFull:
                                logger.warning(
                                    f"Client {client_id} queue full after dropping chunk")
                        except Exception as e:
                            logger.error(
                                f"Error sending to client {client_id}: {e}")
                            dead_clients.append(client_id)

                    # Remove dead clients
                    for client_id in dead_clients:
                        self.client_queues.pop(client_id, None)

                # Update stats
                self.total_bytes_served += len(chunk)
                self.last_access = time.time()

        except Exception as e:
            logger.error(f"Broadcaster error for stream {self.stream_id}: {e}")
        finally:
            logger.info(f"Broadcaster stopped for stream {self.stream_id}")
            # Signal all clients that the stream has ended
            async with self._broadcaster_lock:
                for queue in self.client_queues.values():
                    try:
                        queue.put_nowait(None)  # None signals end of stream
                    except:
                        pass

    async def _hls_watch_loop(self):
        """Watch HLS output directory and update last_chunk_time when new segments appear"""
        if not self.hls_dir:
            return
        try:
            known = set()
            while self.process and self.process.returncode is None:
                # list files
                try:
                    files = os.listdir(self.hls_dir)
                except FileNotFoundError:
                    files = []

                new_files = [f for f in files if f not in known]
                if new_files:
                    self.last_chunk_time = time.time()
                    for f in new_files:
                        known.add(f)
                await asyncio.sleep(0.5)
        except Exception as e:
            logger.debug(f"HLS watch loop ended for {self.stream_id}: {e}")

    async def _log_stderr(self):
        """Log FFmpeg stderr output"""
        if not self.process or not self.process.stderr:
            return

        try:
            while self.process and self.process.returncode is None:
                line = await self.process.stderr.readline()
                if not line:
                    break

                line_str = line.decode('utf-8', errors='ignore').strip()
                if line_str:
                    # Log FFmpeg output (you could parse stats here)
                    logger.debug(f"FFmpeg [{self.stream_id}]: {line_str}")

        except Exception as e:
            logger.error(
                f"Error reading FFmpeg stderr for {self.stream_id}: {e}")

    async def read_playlist(self) -> Optional[str]:
        """Read the generated HLS playlist (index.m3u8) if available"""
        if not self.hls_dir:
            return None
        playlist_path = os.path.join(self.hls_dir, 'index.m3u8')
        try:
            if os.path.exists(playlist_path):
                with open(playlist_path, 'r', encoding='utf-8', errors='ignore') as fh:
                    return fh.read()
        except Exception as e:
            logger.error(f"Error reading playlist {playlist_path}: {e}")
        return None

    def get_segment_path(self, segment_name: str) -> Optional[str]:
        if not self.hls_dir:
            return None
        candidate = os.path.join(self.hls_dir, segment_name)
        if os.path.exists(candidate):
            return candidate
        return None

    async def add_client(self, client_id: str) -> asyncio.Queue:
        """Add a client to this shared process and return their queue"""
        async with self._broadcaster_lock:
            # Create a queue for this client (max 100 chunks buffered)
            client_queue = asyncio.Queue(maxsize=100)
            self.client_queues[client_id] = client_queue
            self.clients[client_id] = time.time()
            self.last_access = time.time()
            logger.info(
                f"Client {client_id} joined shared stream {self.stream_id} ({len(self.clients)} total)")
            return client_queue

    async def remove_client(self, client_id: str):
        """Remove a client from this shared process"""
        async with self._broadcaster_lock:
            if client_id in self.clients:
                del self.clients[client_id]
                self.last_access = time.time()
                logger.info(
                    f"Client {client_id} left shared stream {self.stream_id} ({len(self.clients)} remaining)")

            # Remove client's queue
            if client_id in self.client_queues:
                del self.client_queues[client_id]

    async def prune_stale_clients(self, timeout: int):
        """Remove clients that have been inactive for a while"""
        stale_clients = [
            cid for cid, last_seen in self.clients.items()
            if time.time() - last_seen > timeout
        ]
        for client_id in stale_clients:
            await self.remove_client(client_id)

    def should_cleanup(self, timeout: int = 300) -> bool:
        """Check if this process should be cleaned up (no clients for timeout seconds)"""
        return not self.clients and (time.time() - self.last_access > timeout)

    def health_check(self):
        """Check the health of the FFmpeg process."""
        if self.process and self.process.returncode is not None:
            if self.status != "failed":
                logger.warning(
                    f"FFmpeg process for stream {self.stream_id} has exited with code {self.process.returncode}.")
                self.status = "failed"
                return False

        # Also check if process exists but is not responding
        if self.process is None and self.status == "running":
            logger.warning(
                f"FFmpeg process for stream {self.stream_id} is None but status is running")
            self.status = "failed"
            return False
        
        # Check if no output for too long (indicates stuck process)
        if time.time() - self.last_chunk_time > self.output_timeout:
            logger.warning(
                f"FFmpeg process for stream {self.stream_id} has produced no output for {self.output_timeout}s, marking as failed")
            self.status = "failed"
            return False

        return self.status == "running" and self.process is not None

    async def cleanup(self):
        """Clean up the FFmpeg process"""
        if self.process and self.process.returncode is None:
            logger.info(
                f"Terminating shared FFmpeg process for stream {self.stream_id}")
            try:
                self.process.terminate()
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning(
                    f"FFmpeg process didn't terminate cleanly, killing it")
                self.process.kill()
                await self.process.wait()
            except Exception as e:
                logger.error(f"Error cleaning up FFmpeg process: {e}")

        self.status = "stopped"
        self.clients.clear()
        # Remove HLS directory if present and empty
        try:
            if self.hls_dir and os.path.isdir(self.hls_dir):
                # Attempt to remove files then dir
                for fname in os.listdir(self.hls_dir):
                    try:
                        os.remove(os.path.join(self.hls_dir, fname))
                    except Exception:
                        pass
                try:
                    os.rmdir(self.hls_dir)
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"Error cleaning up hls_dir for {self.stream_id}: {e}")


class PooledStreamManager:
    """Stream manager with Redis support and connection pooling"""

    def __init__(self,
                 redis_url: Optional[str] = None,
                 worker_id: Optional[str] = None,
                 enable_sharing: bool = True):

        self.redis_url = redis_url or "redis://localhost:6379/0"
        self.worker_id = worker_id or str(uuid.uuid4())[:8]
        self.enable_sharing = enable_sharing and REDIS_AVAILABLE

        # Redis client
        # Use Any to avoid type issues when Redis not available
        self.redis_client: Optional[Any] = None

        # Local process management
        self.shared_processes: Dict[str, SharedTranscodingProcess] = {}
        # client_id -> stream_id mapping
        self.client_streams: Dict[str, str] = {}

        # Configuration
        self.cleanup_interval = int(getattr(settings, 'CLEANUP_INTERVAL', 60))      # seconds - how often to run cleanup loop
        self.heartbeat_interval = int(getattr(settings, 'HEARTBEAT_INTERVAL', 30))  # seconds - Redis worker heartbeat
        # seconds - fallback timeout for streams with no clients
        self.stream_timeout = int(getattr(settings, 'STREAM_TIMEOUT', 300))
        # Default 30 seconds - timeout for inactive clients
        self.client_timeout = int(getattr(settings, 'CLIENT_TIMEOUT', 30))
        # HLS GC configuration (defaults from config.settings)
        self.hls_gc_enabled = bool(getattr(settings, 'HLS_GC_ENABLED', True))
        # How often to scan filesystem for stale HLS dirs (seconds)
        self.hls_gc_interval = int(getattr(settings, 'HLS_GC_INTERVAL', 600))
        # Age threshold for removing HLS dirs (seconds)
        self.hls_gc_age_threshold = int(getattr(settings, 'HLS_GC_AGE_THRESHOLD', 60 * 60))
        # Track last time GC ran so we can respect hls_gc_interval cadence
        self._last_hls_gc_run = 0
        # Base directory for HLS per-stream output. If settings.HLS_TEMP_DIR is not set,
        # fall back to the system tempdir used by tempfile.gettempdir()
        self.hls_base_dir = getattr(settings, 'HLS_TEMP_DIR', None) or tempfile.gettempdir()

        # Tasks
        self._cleanup_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self):
        """Start the pooled stream manager"""
        self._running = True

        if self.enable_sharing and REDIS_AVAILABLE:
            try:
                # Import here to avoid issues if redis not installed
                import redis.asyncio as redis_async
                self.redis_client = redis_async.from_url(
                    self.redis_url, decode_responses=True)
                await self.redis_client.ping()
                logger.info(f"Redis connected for worker {self.worker_id}")

                # Start heartbeat task
                self._heartbeat_task = asyncio.create_task(
                    self._heartbeat_loop())

            except Exception as e:
                logger.warning(
                    f"Failed to connect to Redis: {e}. Running in single-worker mode")
                self.enable_sharing = False
                self.redis_client = None

        # Start cleanup task
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

        mode = "multi-worker with Redis" if self.enable_sharing else "single-worker"
        logger.info(f"Pooled stream manager started in {mode} mode")

    async def stop(self):
        """Stop the pooled stream manager"""
        self._running = False

        # Cancel tasks
        if self._cleanup_task:
            self._cleanup_task.cancel()
        if self._heartbeat_task:
            self._heartbeat_task.cancel()

        # Clean up all local processes
        for process in list(self.shared_processes.values()):
            await process.cleanup()
        self.shared_processes.clear()

        # Close Redis connection
        if self.redis_client:
            await self._cleanup_worker_streams()
            await self.redis_client.close()

        logger.info("Pooled stream manager stopped")

    async def _heartbeat_loop(self):
        """Send periodic heartbeat to Redis"""
        while self._running:
            try:
                await asyncio.sleep(self.heartbeat_interval)
                if self.redis_client:
                    now = time.time()
                    # Update heartbeat score
                    await self.redis_client.zadd("worker_heartbeats", {self.worker_id: now})

                    # Update worker data
                    worker_data = {
                        "last_seen": now,
                        "streams": list(self.shared_processes.keys()),
                        "worker_id": self.worker_id
                    }
                    await self.redis_client.hset("workers", self.worker_id, json.dumps(worker_data))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Heartbeat error: {e}")

    async def _cleanup_loop(self):
        """Periodic cleanup of stale streams and processes"""
        while self._running:
            try:
                await asyncio.sleep(self.cleanup_interval)
                await self._cleanup_stale_processes()

                # Optionally run HLS temp-dir GC (scans system temp dir for leftover m3u_proxy_hls_*)
                if self.hls_gc_enabled:
                    now = time.time()
                    if now - getattr(self, '_last_hls_gc_run', 0) >= self.hls_gc_interval:
                        await self._gc_hls_temp_dirs()
                        self._last_hls_gc_run = now

                if self.enable_sharing and self.redis_client:
                    await self._cleanup_stale_redis_streams()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Cleanup error: {e}")

    async def _cleanup_stale_processes(self):
        """Clean up local processes with no clients"""
        to_cleanup = []

        for stream_id, process in self.shared_processes.items():
            process.health_check()
            await process.prune_stale_clients(self.client_timeout)
            if process.should_cleanup(self.stream_timeout) or process.status == "failed":
                to_cleanup.append(stream_id)

        for stream_id in to_cleanup:
            logger.info(f"Cleaning up stale process for stream {stream_id}")
            await self._cleanup_local_process(stream_id)

    async def _cleanup_local_process(self, stream_id: str):
        """Clean up a specific local process"""
        if stream_id in self.shared_processes:
            process = self.shared_processes.pop(stream_id)
            await process.cleanup()

            # Update Redis
            if self.redis_client:
                redis_key = f"stream:{stream_id}"
                await self.redis_client.delete(redis_key)
                await self.redis_client.srem(f"worker:{self.worker_id}:streams", redis_key)

    async def _cleanup_stale_redis_streams(self):
        """Clean up stale streams from Redis (dead workers)"""
        if not self.redis_client:
            return

        try:
            # Find stale workers
            stale_threshold = time.time() - (self.heartbeat_interval * 3)
            stale_workers = await self.redis_client.zrangebyscore("worker_heartbeats", -1, stale_threshold)

            if not stale_workers:
                return

            # Clean up streams from stale workers
            for worker_id in stale_workers:
                worker_streams_key = f"worker:{worker_id}:streams"
                stream_keys = await self.redis_client.smembers(worker_streams_key)
                if stream_keys:
                    await self.redis_client.delete(*stream_keys)
                    logger.info(
                        f"Cleaned up {len(stream_keys)} streams for stale worker {worker_id}")
                await self.redis_client.delete(worker_streams_key)

            # Remove stale workers from heartbeats and data
            await self.redis_client.zremrangebyscore("worker_heartbeats", -1, stale_threshold)
            await self.redis_client.hdel("workers", *stale_workers)
            logger.info(f"Removed stale workers: {stale_workers}")

        except Exception as e:
            logger.error(f"Error cleaning up stale Redis streams: {e}")

    async def _cleanup_worker_streams(self):
        """Clean up streams owned by this worker from Redis"""
        if not self.redis_client:
            return

        try:
            worker_streams_key = f"worker:{self.worker_id}:streams"
            stream_keys = await self.redis_client.smembers(worker_streams_key)
            if stream_keys:
                await self.redis_client.delete(*stream_keys)
            await self.redis_client.delete(worker_streams_key)
        except Exception as e:
            logger.error(f"Error cleaning up worker streams from Redis: {e}")

    async def _gc_hls_temp_dirs(self):
        """Scan the system temp dir for leftover HLS directories and remove ones older than threshold.

        We look for directories created with the prefix 'm3u_proxy_hls_' and remove them if they
        are not currently in use by any active SharedTranscodingProcess and they have a
        modification time older than hls_gc_age_threshold.
        """
        try:
            # Use configured HLS base dir (may be system tempdir by default)
            tmpdir = getattr(self, 'hls_base_dir', None) or tempfile.gettempdir()
            prefix = "m3u_proxy_hls_"
            now = time.time()

            # Build a set of active hls dirs to avoid deleting currently-used ones
            active_dirs = set()
            for p in self.shared_processes.values():
                if p.hls_dir:
                    active_dirs.add(os.path.abspath(p.hls_dir))

            # Iterate entries in tmpdir
            try:
                entries = os.listdir(tmpdir)
            except Exception:
                entries = []

            removed = 0
            for entry in entries:
                if not entry.startswith(prefix):
                    continue

                full_path = os.path.abspath(os.path.join(tmpdir, entry))
                if not os.path.isdir(full_path):
                    continue

                # Skip any dir that's currently active
                if full_path in active_dirs:
                    continue

                # Determine age by mtime
                try:
                    mtime = os.path.getmtime(full_path)
                except Exception:
                    continue

                age = now - mtime
                if age > self.hls_gc_age_threshold:
                    try:
                        shutil.rmtree(full_path)
                        removed += 1
                        logger.info(f"Removed stale HLS dir: {full_path}")
                    except Exception as e:
                        logger.error(f"Failed to remove HLS dir {full_path}: {e}")

            if removed:
                logger.info(f"HLS GC removed {removed} stale directories from {tmpdir}")

        except Exception as e:
            logger.error(f"Error while running HLS GC: {e}")

    def _generate_stream_key(self, url: str, profile: str) -> str:
        """Generate a consistent key for stream sharing"""
        # Create a hash of URL + profile for consistent stream sharing
        data = f"{url}|{profile}"
        return hashlib.sha256(data.encode()).hexdigest()[:16]

    async def get_or_create_shared_stream(
        self,
        url: str,
        profile: str,
        ffmpeg_args: List[str],
        client_id: str,
        user_agent: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Tuple[str, SharedTranscodingProcess]:
        """Get existing shared stream or create new one"""

        stream_key = self._generate_stream_key(url, profile)

        # First check if we have it locally
        if stream_key in self.shared_processes:
            process = self.shared_processes[stream_key]

            # Check if the process is still healthy before reusing it
            process.health_check()

            # If process has failed or exited, clean it up and create a new one
            if process.status == "failed" or (process.process and process.process.returncode is not None):
                logger.warning(
                    f"Existing process for stream {stream_key} is unhealthy, recreating...")
                await self._cleanup_local_process(stream_key)
                # Process will be recreated below
            else:
                # Process is healthy, reuse it
                await process.add_client(client_id)
                self.client_streams[client_id] = stream_key
                return stream_key, process

        # If sharing enabled, check Redis for existing streams
        if self.enable_sharing and self.redis_client:
            redis_key = f"stream:{stream_key}"
            stream_data = await self.redis_client.hgetall(redis_key) or {}

            if stream_data and await self._is_redis_stream_healthy(stream_data):
                owner = stream_data.get("owner")
                if owner != self.worker_id:
                    logger.info(
                        f"Stream {stream_key} is managed by another worker ({owner}). This worker will not create a local copy.")
                    raise ConnectionAbortedError(
                        f"Stream is on another worker {owner}")

        # Create new local process
        process = SharedTranscodingProcess(
            stream_key, url, profile, ffmpeg_args, user_agent=user_agent, headers=headers, hls_base_dir=self.hls_base_dir)

        if await process.start_process():
            self.shared_processes[stream_key] = process
            await process.add_client(client_id)
            self.client_streams[client_id] = stream_key

            # Register in Redis
            if self.redis_client:
                redis_key = f"stream:{stream_key}"
                stream_data = {
                    "url": url,
                    "profile": profile,
                    "owner": self.worker_id,
                    "created_at": time.time(),
                    "last_access": time.time(),
                    "status": "running",
                    "ffmpeg_pid": process.process.pid if process.process else 0
                }
                await self.redis_client.hset(redis_key, mapping=stream_data)
                await self.redis_client.sadd(f"worker:{self.worker_id}:streams", redis_key)

            logger.info(
                f"Created new shared stream {stream_key} for {len(process.clients)} clients")
            return stream_key, process
        else:
            raise Exception(
                f"Failed to start transcoding process for stream {stream_key}")

    async def _is_redis_stream_healthy(self, stream_data: Dict) -> bool:
        """Check if a Redis stream entry represents a healthy stream"""

        # Check if owner worker is alive
        owner = stream_data.get("owner")
        if not owner or not self.redis_client:
            return False

        worker_data = await self.redis_client.hget("workers", owner)
        if not worker_data:
            return False

        try:
            worker_info = json.loads(worker_data)
            last_seen = worker_info.get("last_seen", 0)
            return time.time() - last_seen < self.heartbeat_interval * 2
        except json.JSONDecodeError:
            return False

    async def remove_client_from_stream(self, client_id: str):
        """Remove a client from its stream"""
        if client_id not in self.client_streams:
            return

        stream_key = self.client_streams.pop(client_id, None)

        if stream_key and stream_key in self.shared_processes:
            process = self.shared_processes[stream_key]
            await process.remove_client(client_id)

            # If no more clients, immediately schedule cleanup
            if not process.clients:
                logger.info(
                    f"No more clients for stream {stream_key}, scheduling immediate cleanup")
                # Use configured grace period (SHARED_STREAM_GRACE) so short client
                # reconnects (e.g. range-based reconnects from players) don't
                # immediately kill the transcoding process. Fall back to 1s if
                # the setting isn't available.
                grace = int(getattr(settings, 'SHARED_STREAM_GRACE', 1))
                asyncio.create_task(self._delayed_cleanup_if_empty(
                    stream_key, grace_period=grace))

    async def force_stop_stream(self, stream_key: str):
        """
        Immediately stop a stream and its FFmpeg process without grace period.
        Used when explicitly deleting a stream via API.
        """
        if stream_key not in self.shared_processes:
            logger.info(f"Stream {stream_key} not found in local processes")
            return False

        logger.info(
            f"Force stopping stream {stream_key} and terminating FFmpeg process")
        process = self.shared_processes[stream_key]

        # Remove all clients from this stream immediately
        clients_to_remove = list(process.clients.keys())
        for client_id in clients_to_remove:
            await process.remove_client(client_id)
            if client_id in self.client_streams:
                del self.client_streams[client_id]

        # Immediately cleanup the FFmpeg process
        await self._cleanup_local_process(stream_key)

        logger.info(
            f"Stream {stream_key} force stopped, FFmpeg process terminated")
        return True

    async def _delayed_cleanup_if_empty(self, stream_key: str, grace_period: int = 10):
        """
        Clean up a stream after a grace period if it still has no clients.
        This prevents immediate termination on brief disconnects while ensuring
        resources are freed quickly when streaming actually stops.
        """
        await asyncio.sleep(grace_period)

        if stream_key not in self.shared_processes:
            return  # Already cleaned up

        process = self.shared_processes[stream_key]

        # Check if clients reconnected during grace period
        if not process.clients:
            logger.info(
                f"Grace period expired for stream {stream_key} with no clients, cleaning up FFmpeg process")
            await self._cleanup_local_process(stream_key)
        else:
            logger.info(
                f"Clients reconnected to stream {stream_key} during grace period, keeping process alive")

    def update_client_activity(self, client_id: str):
        """Update the last access time for a client."""
        if client_id in self.client_streams:
            stream_key = self.client_streams[client_id]
            if stream_key in self.shared_processes:
                process = self.shared_processes[stream_key]
                if client_id in process.clients:
                    process.clients[client_id] = time.time()

    async def stream_shared_process(self, client_id: str) -> Optional[asyncio.subprocess.Process]:
        """Get the FFmpeg process for a client's stream"""

        if client_id not in self.client_streams:
            return None

        stream_key = self.client_streams[client_id]
        if stream_key not in self.shared_processes:
            return None

        process = self.shared_processes[stream_key]
        return process.process

    async def get_stream_stats(self) -> Dict[str, Any]:
        """Get statistics about active streams"""

        stats = {
            "worker_id": self.worker_id,
            "sharing_enabled": self.enable_sharing,
            "local_streams": len(self.shared_processes),
            "total_clients": len(self.client_streams),
            "streams": []
        }

        for stream_id, process in self.shared_processes.items():
            stream_stats = {
                "stream_id": stream_id,
                "url": process.url,
                "profile": process.profile,
                "client_count": len(process.clients),
                "created_at": process.created_at,
                "last_access": process.last_access,
                "status": process.status,
                "total_bytes_served": process.total_bytes_served
            }
            stats["streams"].append(stream_stats)

        return stats
