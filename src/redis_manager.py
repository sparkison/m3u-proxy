"""
Redis-based stream management for shared transcoding processes.
Implements connection pooling and multi-worker coordination.
"""

import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple
import redis.asyncio as redis
import logging

logger = logging.getLogger(__name__)

class RedisStreamManager:
    """Redis-backed stream manager for shared transcoding processes"""
    
    def __init__(self, redis_url: str = "redis://localhost:6379/0"):
        self.redis_url = redis_url
        self.redis_client: Optional[redis.Redis] = None
        self.worker_id = str(uuid.uuid4())
        self.heartbeat_interval = 30  # seconds
        self.client_timeout = 300     # 5 minutes
        self.stream_timeout = 600     # 10 minutes
        self._heartbeat_task: Optional[asyncio.Task] = None
        
    async def connect(self):
        """Initialize Redis connection"""
        self.redis_client = redis.from_url(self.redis_url, decode_responses=True)
        await self.redis_client.ping()
        logger.info(f"Redis connected for worker {self.worker_id}")
        
        # Start heartbeat
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        
    async def disconnect(self):
        """Clean up Redis connection"""
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        
        if self.redis_client:
            # Clean up our worker's streams
            await self._cleanup_worker_streams()
            await self.redis_client.close()
        
    async def _heartbeat_loop(self):
        """Send periodic heartbeat to indicate worker is alive"""
        while True:
            try:
                await asyncio.sleep(self.heartbeat_interval)
                await self.redis_client.hset(
                    "workers", 
                    self.worker_id, 
                    json.dumps({
                        "last_seen": time.time(),
                        "streams": await self._get_worker_streams()
                    })
                )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Heartbeat error: {e}")
                
    async def _get_worker_streams(self) -> List[str]:
        """Get list of streams owned by this worker"""
        streams = []
        pattern = f"stream:*"
        async for key in self.redis_client.scan_iter(match=pattern):
            stream_data = await self.redis_client.hgetall(key)
            if stream_data.get("owner") == self.worker_id:
                streams.append(key.split(":", 1)[1])
        return streams
        
    async def _cleanup_worker_streams(self):
        """Clean up streams owned by this worker"""
        worker_streams = await self._get_worker_streams()
        for stream_id in worker_streams:
            await self.cleanup_stream(stream_id)
            
    # Stream Management
    
    async def create_shared_stream(
        self, 
        stream_id: str,
        url: str,
        profile: str,
        ffmpeg_args: List[str],
        user_agent: str = None
    ) -> bool:
        """Create a shared transcoding stream that multiple clients can connect to"""
        
        # Check if stream already exists
        stream_key = f"stream:{stream_id}"
        if await self.redis_client.exists(stream_key):
            # Stream exists, check if it's healthy
            stream_data = await self.redis_client.hgetall(stream_key)
            if await self._is_stream_healthy(stream_id, stream_data):
                logger.info(f"Reusing existing healthy stream {stream_id}")
                return True
            else:
                # Clean up unhealthy stream
                await self.cleanup_stream(stream_id)
        
        # Create new stream
        stream_data = {
            "url": url,
            "profile": profile,
            "ffmpeg_args": json.dumps(ffmpeg_args),
            "user_agent": user_agent or "",
            "owner": self.worker_id,
            "created_at": time.time(),
            "last_access": time.time(),
            "status": "starting",
            "client_count": 0,
            "total_bytes": 0,
            "ffmpeg_pid": 0
        }
        
        await self.redis_client.hset(stream_key, mapping=stream_data)
        logger.info(f"Created shared stream {stream_id} owned by worker {self.worker_id}")
        return True
        
    async def _is_stream_healthy(self, stream_id: str, stream_data: Dict) -> bool:
        """Check if a stream is healthy and accessible"""
        
        # Check if owner worker is still alive
        owner = stream_data.get("owner")
        if not owner:
            return False
            
        worker_data = await self.redis_client.hget("workers", owner)
        if not worker_data:
            logger.warning(f"Stream {stream_id} owner {owner} not found")
            return False
            
        try:
            worker_info = json.loads(worker_data)
            last_seen = worker_info.get("last_seen", 0)
            if time.time() - last_seen > self.heartbeat_interval * 2:
                logger.warning(f"Stream {stream_id} owner {owner} last seen {time.time() - last_seen}s ago")
                return False
        except json.JSONDecodeError:
            return False
            
        # Check stream status
        status = stream_data.get("status")
        if status in ["failed", "stopped"]:
            return False
            
        # Check if stream is too old without activity
        last_access = float(stream_data.get("last_access", 0))
        if time.time() - last_access > self.stream_timeout:
            logger.warning(f"Stream {stream_id} inactive for {time.time() - last_access}s")
            return False
            
        return True
        
    async def register_client(self, stream_id: str, client_id: str, client_info: Dict = None) -> bool:
        """Register a client connection to a shared stream"""
        
        stream_key = f"stream:{stream_id}"
        client_key = f"client:{stream_id}:{client_id}"
        
        # Verify stream exists
        if not await self.redis_client.exists(stream_key):
            logger.error(f"Cannot register client {client_id} - stream {stream_id} does not exist")
            return False
            
        # Register client
        client_data = {
            "stream_id": stream_id,
            "connected_at": time.time(),
            "last_seen": time.time(),
            "bytes_served": 0,
            **(client_info or {})
        }
        
        await self.redis_client.hset(client_key, mapping=client_data)
        await self.redis_client.expire(client_key, self.client_timeout)
        
        # Update stream client count
        await self.redis_client.hincrby(stream_key, "client_count", 1)
        await self.redis_client.hset(stream_key, "last_access", time.time())
        
        logger.info(f"Registered client {client_id} to stream {stream_id}")
        return True
        
    async def unregister_client(self, stream_id: str, client_id: str):
        """Unregister a client from a shared stream"""
        
        client_key = f"client:{stream_id}:{client_id}"
        stream_key = f"stream:{stream_id}"
        
        # Get client data for stats
        client_data = await self.redis_client.hgetall(client_key)
        bytes_served = int(client_data.get("bytes_served", 0))
        
        # Remove client
        await self.redis_client.delete(client_key)
        
        # Update stream stats
        if await self.redis_client.exists(stream_key):
            await self.redis_client.hincrby(stream_key, "client_count", -1)
            await self.redis_client.hincrby(stream_key, "total_bytes", bytes_served)
            
            # Check if stream should be cleaned up
            client_count = int(await self.redis_client.hget(stream_key, "client_count") or 0)
            if client_count <= 0:
                # Schedule cleanup after grace period
                await self.redis_client.expire(stream_key, 60)  # 1 minute grace period
                
        logger.info(f"Unregistered client {client_id} from stream {stream_id}")
        
    async def get_stream_clients(self, stream_id: str) -> List[Dict]:
        """Get all active clients for a stream"""
        clients = []
        pattern = f"client:{stream_id}:*"
        
        async for key in self.redis_client.scan_iter(match=pattern):
            client_data = await self.redis_client.hgetall(key)
            if client_data:
                client_id = key.split(":", 2)[2]
                clients.append({
                    "client_id": client_id,
                    **client_data
                })
                
        return clients
        
    async def update_client_stats(self, stream_id: str, client_id: str, bytes_served: int):
        """Update client serving statistics"""
        client_key = f"client:{stream_id}:{client_id}"
        await self.redis_client.hset(client_key, mapping={
            "last_seen": time.time(),
            "bytes_served": bytes_served
        })
        await self.redis_client.expire(client_key, self.client_timeout)
        
    async def update_stream_status(self, stream_id: str, status: str, ffmpeg_pid: int = None, extra_data: Dict = None):
        """Update stream status and metadata"""
        stream_key = f"stream:{stream_id}"
        
        update_data = {
            "status": status,
            "last_access": time.time()
        }
        
        if ffmpeg_pid is not None:
            update_data["ffmpeg_pid"] = ffmpeg_pid
            
        if extra_data:
            update_data.update(extra_data)
            
        await self.redis_client.hset(stream_key, mapping=update_data)
        
    async def get_stream_info(self, stream_id: str) -> Optional[Dict]:
        """Get stream information and metadata"""
        stream_key = f"stream:{stream_id}"
        stream_data = await self.redis_client.hgetall(stream_key)
        
        if not stream_data:
            return None
            
        return stream_data
        
    async def cleanup_stream(self, stream_id: str):
        """Clean up a stream and all its clients"""
        stream_key = f"stream:{stream_id}"
        
        # Get all clients for this stream
        clients = await self.get_stream_clients(stream_id)
        
        # Remove all clients
        for client in clients:
            client_key = f"client:{stream_id}:{client['client_id']}"
            await self.redis_client.delete(client_key)
            
        # Remove stream
        await self.redis_client.delete(stream_key)
        
        logger.info(f"Cleaned up stream {stream_id} and {len(clients)} clients")
        
    # Process Management
    
    async def find_available_stream(self, url: str, profile: str) -> Optional[str]:
        """Find an existing stream that can be reused for the same URL/profile"""
        
        pattern = "stream:*"
        async for key in self.redis_client.scan_iter(match=pattern):
            stream_data = await self.redis_client.hgetall(key)
            
            # Check if stream matches our requirements
            if (stream_data.get("url") == url and 
                stream_data.get("profile") == profile and
                await self._is_stream_healthy(key.split(":", 1)[1], stream_data)):
                
                stream_id = key.split(":", 1)[1]
                logger.info(f"Found reusable stream {stream_id} for {url} with profile {profile}")
                return stream_id
                
        return None
        
    async def get_active_streams(self) -> List[Dict]:
        """Get all active streams across all workers"""
        streams = []
        pattern = "stream:*"
        
        async for key in self.redis_client.scan_iter(match=pattern):
            stream_data = await self.redis_client.hgetall(key)
            if stream_data:
                stream_id = key.split(":", 1)[1]
                streams.append({
                    "stream_id": stream_id,
                    **stream_data
                })
                
        return streams
        
    async def cleanup_stale_streams(self):
        """Clean up stale streams from dead workers"""
        
        # Get all workers and their last seen times
        workers = await self.redis_client.hgetall("workers")
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
            logger.info(f"Cleaning up streams from {len(stale_workers)} stale workers")
            
            pattern = "stream:*"
            async for key in self.redis_client.scan_iter(match=pattern):
                stream_data = await self.redis_client.hgetall(key)
                owner = stream_data.get("owner")
                
                if owner in stale_workers:
                    stream_id = key.split(":", 1)[1]
                    logger.info(f"Cleaning up stream {stream_id} from stale worker {owner}")
                    await self.cleanup_stream(stream_id)
                    
            # Remove stale workers
            for worker_id in stale_workers:
                await self.redis_client.hdel("workers", worker_id)


class RedisConnectionPool:
    """Manages Redis connections with connection pooling"""
    
    def __init__(self, redis_url: str = "redis://localhost:6379/0", max_connections: int = 10):
        self.redis_url = redis_url
        self.max_connections = max_connections
        self.pool: Optional[redis.ConnectionPool] = None
        
    async def create_pool(self):
        """Create Redis connection pool"""
        self.pool = redis.ConnectionPool.from_url(
            self.redis_url,
            max_connections=self.max_connections,
            decode_responses=True
        )
        
        # Test connection
        redis_client = redis.Redis(connection_pool=self.pool)
        await redis_client.ping()
        await redis_client.close()
        
        logger.info(f"Redis connection pool created with {self.max_connections} max connections")
        
    async def get_client(self) -> redis.Redis:
        """Get a Redis client from the pool"""
        if not self.pool:
            await self.create_pool()
        return redis.Redis(connection_pool=self.pool)
        
    async def close_pool(self):
        """Close the connection pool"""
        if self.pool:
            await self.pool.disconnect()