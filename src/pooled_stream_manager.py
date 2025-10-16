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

logger = logging.getLogger(__name__)

try:
    import redis.asyncio as redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    logger.warning("Redis not available - falling back to single-worker mode")

class SharedTranscodingProcess:
    """Represents a shared FFmpeg transcoding process"""
    
    def __init__(self, stream_id: str, url: str, profile: str, ffmpeg_args: List[str]):
        self.stream_id = stream_id
        self.url = url
        self.profile = profile
        self.ffmpeg_args = ffmpeg_args
        self.process: Optional[asyncio.subprocess.Process] = None
        self.clients: Set[str] = set()
        self.created_at = time.time()
        self.last_access = time.time()
        self.total_bytes_served = 0
        self.status = "starting"
        
    async def start_process(self):
        """Start the FFmpeg process"""
        try:
            logger.info(f"Starting shared FFmpeg process for stream {self.stream_id}")
            
            # Build FFmpeg command - ensure output to stdout
            ffmpeg_cmd = ["ffmpeg"] + self.ffmpeg_args
            
            # Ensure we're outputting to stdout in MPEGTS format
            if "-f" not in ffmpeg_cmd:
                ffmpeg_cmd.extend(["-f", "mpegts"])
            if "pipe:1" not in ffmpeg_cmd and "-" not in ffmpeg_cmd:
                ffmpeg_cmd.append("pipe:1")
                
            logger.info(f"FFmpeg command: {' '.join(ffmpeg_cmd)}")
            
            self.process = await asyncio.create_subprocess_exec(
                *ffmpeg_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            self.status = "running"
            logger.info(f"Shared FFmpeg process started with PID: {self.process.pid}")
            
            # Start stderr logging task
            asyncio.create_task(self._log_stderr())
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to start shared FFmpeg process: {e}")
            self.status = "failed"
            return False
            
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
            logger.error(f"Error reading FFmpeg stderr for {self.stream_id}: {e}")
            
    def add_client(self, client_id: str):
        """Add a client to this shared process"""
        self.clients.add(client_id)
        self.last_access = time.time()
        logger.info(f"Client {client_id} joined shared stream {self.stream_id} ({len(self.clients)} total)")
        
    def remove_client(self, client_id: str):
        """Remove a client from this shared process"""
        self.clients.discard(client_id)
        self.last_access = time.time()
        logger.info(f"Client {client_id} left shared stream {self.stream_id} ({len(self.clients)} remaining)")
        
    def should_cleanup(self, timeout: int = 300) -> bool:
        """Check if this process should be cleaned up (no clients for timeout seconds)"""
        if len(self.clients) == 0:
            return time.time() - self.last_access > timeout
        return False
        
    async def cleanup(self):
        """Clean up the FFmpeg process"""
        if self.process and self.process.returncode is None:
            logger.info(f"Terminating shared FFmpeg process for stream {self.stream_id}")
            try:
                self.process.terminate()
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning(f"FFmpeg process didn't terminate cleanly, killing it")
                self.process.kill()
                await self.process.wait()
            except Exception as e:
                logger.error(f"Error cleaning up FFmpeg process: {e}")
                
        self.status = "stopped"
        self.clients.clear()


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
        self.redis_client: Optional[Any] = None  # Use Any to avoid type issues when Redis not available
        
        # Local process management
        self.shared_processes: Dict[str, SharedTranscodingProcess] = {}
        self.client_streams: Dict[str, str] = {}  # client_id -> stream_id mapping
        
        # Configuration
        self.cleanup_interval = 60      # seconds
        self.heartbeat_interval = 30    # seconds  
        self.stream_timeout = 300       # 5 minutes without clients
        self.client_timeout = 600       # 10 minutes
        
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
                self.redis_client = redis_async.from_url(self.redis_url, decode_responses=True)
                await self.redis_client.ping()
                logger.info(f"Redis connected for worker {self.worker_id}")
                
                # Start heartbeat task
                self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
                
            except Exception as e:
                logger.warning(f"Failed to connect to Redis: {e}. Running in single-worker mode")
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
                    worker_data = {
                        "last_seen": time.time(),
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
            if process.should_cleanup(self.stream_timeout):
                to_cleanup.append(stream_id)
                
        for stream_id in to_cleanup:
            logger.info(f"Cleaning up stale process for stream {stream_id}")
            await self._cleanup_local_process(stream_id)
            
    async def _cleanup_local_process(self, stream_id: str):
        """Clean up a specific local process"""
        if stream_id in self.shared_processes:
            process = self.shared_processes[stream_id]
            await process.cleanup()
            del self.shared_processes[stream_id]
            
            # Update Redis
            if self.redis_client:
                await self.redis_client.delete(f"stream:{stream_id}")
                
    async def _cleanup_stale_redis_streams(self):
        """Clean up stale streams from Redis (dead workers)"""
        if not self.redis_client:
            return
            
        try:
            # Get all workers
            workers = await self.redis_client.hgetall("workers") or {}
            current_time = time.time()
            stale_workers = set()
            
            for worker_id, worker_data_str in workers.items():
                try:
                    worker_data = json.loads(worker_data_str)
                    last_seen = worker_data.get("last_seen", 0)
                    if current_time - last_seen > self.heartbeat_interval * 3:
                        stale_workers.add(worker_id)
                except json.JSONDecodeError:
                    stale_workers.add(worker_id)
                    
            # Clean up streams from stale workers
            if stale_workers:
                pattern = "stream:*"
                async for key in self.redis_client.scan_iter(match=pattern):
                    stream_data = await self.redis_client.hgetall(key) or {}
                    owner = stream_data.get("owner")
                    
                    if owner in stale_workers:
                        await self.redis_client.delete(key)
                        logger.info(f"Cleaned up stale Redis stream {key}")
                        
                # Remove stale workers
                for worker_id in stale_workers:
                    await self.redis_client.hdel("workers", worker_id)
                    
        except Exception as e:
            logger.error(f"Error cleaning up stale Redis streams: {e}")
            
    async def _cleanup_worker_streams(self):
        """Clean up streams owned by this worker"""
        if not self.redis_client:
            return
            
        try:
            pattern = "stream:*"
            async for key in self.redis_client.scan_iter(match=pattern):
                stream_data = await self.redis_client.hgetall(key) or {}
                if stream_data.get("owner") == self.worker_id:
                    await self.redis_client.delete(key)
        except Exception as e:
            logger.error(f"Error cleaning up worker streams: {e}")
            
    def _generate_stream_key(self, url: str, profile: str, profile_variables: Dict = None) -> str:
        """Generate a consistent key for stream sharing"""
        # Create a hash of URL + profile + variables for consistent stream sharing
        data = f"{url}|{profile}|{json.dumps(profile_variables or {}, sort_keys=True)}"
        return hashlib.sha256(data.encode()).hexdigest()[:16]
        
    async def get_or_create_shared_stream(
        self,
        url: str, 
        profile: str,
        ffmpeg_args: List[str],
        client_id: str,
        profile_variables: Dict = None
    ) -> Tuple[str, SharedTranscodingProcess]:
        """Get existing shared stream or create new one"""
        
        stream_key = self._generate_stream_key(url, profile, profile_variables)
        
        # First check if we have it locally
        if stream_key in self.shared_processes:
            process = self.shared_processes[stream_key]
            process.add_client(client_id)
            self.client_streams[client_id] = stream_key
            return stream_key, process
            
        # If sharing enabled, check Redis for existing streams
        if self.enable_sharing and self.redis_client:
            redis_key = f"stream:{stream_key}"
            stream_data = await self.redis_client.hgetall(redis_key) or {}
            
            if stream_data and await self._is_redis_stream_healthy(stream_data):
                # Stream exists and is healthy, but not in our worker
                # We could implement cross-worker streaming here, but for now create local copy
                logger.info(f"Found healthy stream {stream_key} on another worker, creating local copy")
        
        # Create new local process
        process = SharedTranscodingProcess(stream_key, url, profile, ffmpeg_args)
        
        if await process.start_process():
            self.shared_processes[stream_key] = process
            process.add_client(client_id)
            self.client_streams[client_id] = stream_key
            
            # Register in Redis
            if self.redis_client:
                stream_data = {
                    "url": url,
                    "profile": profile,
                    "owner": self.worker_id,
                    "created_at": time.time(),
                    "last_access": time.time(),
                    "status": "running",
                    "ffmpeg_pid": process.process.pid if process.process else 0
                }
                await self.redis_client.hset(f"stream:{stream_key}", mapping=stream_data)
                
            logger.info(f"Created new shared stream {stream_key} for {len(process.clients)} clients")
            return stream_key, process
        else:
            raise Exception(f"Failed to start transcoding process for stream {stream_key}")
            
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
            
        stream_key = self.client_streams[client_id]
        del self.client_streams[client_id]
        
        if stream_key in self.shared_processes:
            process = self.shared_processes[stream_key]
            process.remove_client(client_id)
            
            # If no clients remain, schedule cleanup
            if len(process.clients) == 0:
                process.last_access = time.time()
                
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