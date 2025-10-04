"""
Stream Manager with Separate Proxy Paths
This version implements efficient per-client proxying for continuous streams
while maintaining the shared buffer approach for HLS segments.
"""

import m3u8
import asyncio
import httpx
import logging
from typing import Dict, Optional, AsyncIterator, List, Set
from urllib.parse import urljoin, urlparse, quote, unquote
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from asyncio import Queue
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from config import settings

logger = logging.getLogger(__name__)


@dataclass
class ClientInfo:
    client_id: str
    created_at: datetime
    last_access: datetime
    user_agent: Optional[str] = None
    ip_address: Optional[str] = None
    stream_id: Optional[str] = None
    bytes_served: int = 0
    segments_served: int = 0
    is_connected: bool = True


@dataclass
class StreamInfo:
    stream_id: str
    original_url: str
    created_at: datetime
    last_access: datetime
    client_count: int = 0
    total_bytes_served: int = 0
    total_segments_served: int = 0
    error_count: int = 0
    is_active: bool = True
    failover_urls: List[str] = field(default_factory=list)
    current_failover_index: int = 0
    current_url: Optional[str] = None
    final_playlist_url: Optional[str] = None
    user_agent: str = settings.DEFAULT_USER_AGENT
    # Track connected clients for stats
    connected_clients: Set[str] = field(default_factory=set)
    # Failover management
    failover_attempts: int = 0
    last_failover_time: Optional[datetime] = None
    connection_timeout: float = settings.DEFAULT_CONNECTION_TIMEOUT
    read_timeout: float = settings.DEFAULT_READ_TIMEOUT
    max_retries: int = settings.DEFAULT_MAX_RETRIES
    backoff_factor: float = settings.DEFAULT_BACKOFF_FACTOR
    health_check_interval: float = settings.DEFAULT_HEALTH_CHECK_INTERVAL
    last_health_check: Optional[datetime] = None
    # Stream type detection
    is_hls: bool = False
    is_vod: bool = False
    is_live_continuous: bool = False


@dataclass
class ProxyStats:
    total_streams: int = 0
    active_streams: int = 0
    total_clients: int = 0
    active_clients: int = 0
    total_bytes_served: int = 0
    total_segments_served: int = 0
    uptime_start: datetime = field(default_factory=datetime.now)
    connection_pool_stats: Dict = field(default_factory=dict)
    failover_stats: Dict = field(default_factory=dict)


class M3U8Processor:
    def __init__(self, base_url: str, client_id: str, user_agent: Optional[str] = None, original_url: Optional[str] = None):
        self.base_url = base_url
        self.client_id = client_id
        self.user_agent = user_agent or settings.DEFAULT_USER_AGENT
        self.original_url = original_url or base_url

    def process_playlist(self, content: str, base_proxy_url: str, original_base_url: Optional[str] = None) -> str:
        """Process M3U8 content and rewrite segment URLs using the m3u8 library."""
        try:
            playlist = m3u8.loads(content, uri=original_base_url or self.original_url)

            # Handle both variant playlists (master) and media playlists
            if playlist.is_variant:
                for variant in playlist.playlists:
                    variant.uri = self._rewrite_url(variant.absolute_uri, base_proxy_url)
                for media in playlist.media:
                    if media.uri:
                        media.uri = self._rewrite_url(media.absolute_uri, base_proxy_url)
            else:
                for segment in playlist.segments:
                    segment.uri = self._rewrite_url(segment.absolute_uri, base_proxy_url)
                # Handle initialization section if present
                for seg_map in (playlist.segment_map if isinstance(playlist.segment_map, list) else []):
                    if hasattr(seg_map, 'uri') and seg_map.uri:
                        seg_map.uri = self._rewrite_url(seg_map.absolute_uri, base_proxy_url)

            return playlist.dumps()
        except Exception as e:
            logger.error(f"Error processing M3U8 playlist: {e}")
            return content

    def _rewrite_url(self, original_url: str, base_proxy_url: str) -> str:
        """Rewrites a URL to point to the proxy, encoding the original URL."""
        encoded_url = quote(original_url, safe='')
        if original_url.endswith('.m3u8'):
            return f"{base_proxy_url}/playlist.m3u8?url={encoded_url}&client_id={self.client_id}"
        else:
            return f"{base_proxy_url}/segment.ts?url={encoded_url}&client_id={self.client_id}"


class StreamManager:
    def __init__(self):
        self.streams: Dict[str, StreamInfo] = {}
        self.clients: Dict[str, ClientInfo] = {}
        self.stream_clients: Dict[str, Set[str]] = {}
        self.client_timeout = settings.CLIENT_TIMEOUT
        self.stream_timeout = settings.STREAM_TIMEOUT
        
        # Optimized HTTP clients with connection pooling
        self.http_client = httpx.AsyncClient(
            timeout=settings.DEFAULT_CONNECTION_TIMEOUT,
            follow_redirects=True,
            max_redirects=10,
            limits=httpx.Limits(
                max_keepalive_connections=20,
                max_connections=100,
                keepalive_expiry=30.0
            )
        )
        
        self.live_stream_client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=settings.DEFAULT_CONNECTION_TIMEOUT,
                read=settings.DEFAULT_READ_TIMEOUT,
                write=10.0,
                pool=10.0
            ),
            follow_redirects=True,
            max_redirects=10,
            limits=httpx.Limits(
                max_keepalive_connections=10,
                max_connections=50,
                keepalive_expiry=30.0
            )
        )
        
        self._stats = ProxyStats()
        self._cleanup_task: Optional[asyncio.Task] = None
        self._health_check_task: Optional[asyncio.Task] = None
        self._running = False
        self.event_manager = None

    def set_event_manager(self, event_manager):
        """Set the event manager for emitting events"""
        self.event_manager = event_manager

    async def _emit_event(self, event_type: str, stream_id: str, data: dict):
        """Helper method to emit events if event manager is available"""
        if self.event_manager:
            try:
                from models import StreamEvent, EventType
                event = StreamEvent(
                    event_type=getattr(EventType, event_type),
                    stream_id=stream_id,
                    data=data
                )
                await self.event_manager.emit_event(event)
            except Exception as e:
                logger.error(f"Error emitting event: {e}")

    async def start(self):
        """Start the stream manager"""
        self._running = True
        self._cleanup_task = asyncio.create_task(self._periodic_cleanup())
        self._health_check_task = asyncio.create_task(self._periodic_health_check())
        logger.info("Stream manager started with optimized connection pooling and direct proxy architecture")

    async def stop(self):
        """Stop the stream manager"""
        self._running = False
        if self._cleanup_task:
            self._cleanup_task.cancel()
        if self._health_check_task:
            self._health_check_task.cancel()
        await self.http_client.aclose()
        await self.live_stream_client.aclose()
        logger.info("Stream manager stopped")

    def _detect_stream_type(self, url: str) -> tuple[bool, bool, bool]:
        """Detect stream type: (is_hls, is_vod, is_live_continuous)"""
        url_lower = url.lower()
        
        # HLS detection
        if url_lower.endswith('.m3u8'):
            return (True, False, False)
        
        # VOD detection (typically non-.ts video files)
        if url_lower.endswith(('.mp4', '.mkv', '.webm', '.avi')):
            return (False, True, False)
        
        # Live continuous stream (.ts or live path)
        if url_lower.endswith('.ts') or '/live/' in url_lower:
            return (False, False, True)
        
        # Default: treat as live continuous
        return (False, False, True)

    async def get_or_create_stream(
        self,
        stream_url: str,
        failover_urls: Optional[List[str]] = None,
        user_agent: Optional[str] = None
    ) -> str:
        """Get or create a stream and return its ID"""
        import hashlib
        stream_id = hashlib.md5(stream_url.encode()).hexdigest()

        if stream_id not in self.streams:
            now = datetime.now()
            if user_agent is None:
                user_agent = settings.DEFAULT_USER_AGENT

            # Detect stream type
            is_hls, is_vod, is_live_continuous = self._detect_stream_type(stream_url)

            self.streams[stream_id] = StreamInfo(
                stream_id=stream_id,
                original_url=stream_url,
                current_url=stream_url,
                created_at=now,
                last_access=now,
                failover_urls=failover_urls or [],
                user_agent=user_agent,
                is_hls=is_hls,
                is_vod=is_vod,
                is_live_continuous=is_live_continuous
            )
            self.stream_clients[stream_id] = set()
            self._stats.total_streams += 1
            self._stats.active_streams += 1
            
            stream_type = "HLS" if is_hls else ("VOD" if is_vod else "Live Continuous")
            logger.info(f"Created new stream: {stream_id} ({stream_type}) with user agent: {user_agent}")

        self.streams[stream_id].last_access = datetime.now()
        return stream_id

    async def register_client(
        self,
        client_id: str,
        stream_id: str,
        user_agent: Optional[str] = None,
        ip_address: Optional[str] = None
    ) -> ClientInfo:
        """Register a client for a stream"""
        now = datetime.now()

        if client_id not in self.clients:
            self.clients[client_id] = ClientInfo(
                client_id=client_id,
                created_at=now,
                last_access=now,
                user_agent=user_agent,
                ip_address=ip_address,
                stream_id=stream_id
            )
            self._stats.total_clients += 1
            self._stats.active_clients += 1
            logger.info(f"Registered new client: {client_id}")

        if stream_id in self.stream_clients:
            self.stream_clients[stream_id].add(client_id)
            self.streams[stream_id].client_count = len(self.stream_clients[stream_id])
            self.streams[stream_id].connected_clients.add(client_id)

        client_info = self.clients[client_id]
        client_info.last_access = now
        client_info.stream_id = stream_id
        client_info.is_connected = True

        await self._emit_event("CLIENT_CONNECTED", stream_id, {
            "client_id": client_id,
            "user_agent": user_agent,
            "ip_address": ip_address,
            "stream_client_count": len(self.stream_clients[stream_id]) if stream_id in self.stream_clients else 0
        })

        return client_info

    async def cleanup_client(self, client_id: str):
        """Clean up a client"""
        if client_id in self.clients:
            client_info = self.clients[client_id]
            stream_id = client_info.stream_id

            if stream_id and stream_id in self.stream_clients:
                self.stream_clients[stream_id].discard(client_id)
                if stream_id in self.streams:
                    self.streams[stream_id].client_count = len(self.stream_clients[stream_id])
                    self.streams[stream_id].connected_clients.discard(client_id)

            await self._emit_event("CLIENT_DISCONNECTED", stream_id or "unknown", {
                "client_id": client_id,
                "bytes_served": client_info.bytes_served,
                "segments_served": client_info.segments_served
            })

            del self.clients[client_id]
            self._stats.active_clients -= 1
            logger.info(f"Cleaned up client: {client_id}")

    # ============================================================================
    # DIRECT PROXY FOR CONTINUOUS STREAMS (New Architecture)
    # ============================================================================

    async def stream_continuous_direct(
        self,
        stream_id: str,
        client_id: str,
        range_header: Optional[str] = None
    ) -> StreamingResponse:
        """
        Direct byte-for-byte proxy for continuous streams (.ts, .mp4, .mkv).
        Each client gets their own provider connection - NO shared buffer.
        Provider connection is truly ephemeral and only open while streaming.
        """
        if stream_id not in self.streams:
            raise HTTPException(status_code=404, detail="Stream not found")

        stream_info = self.streams[stream_id]
        current_url = stream_info.current_url or stream_info.original_url

        # Register this client
        if client_id not in self.clients:
            await self.register_client(client_id, stream_id)

        logger.info(f"Starting direct proxy for client {client_id}, stream {stream_id}")

        async def generate():
            """Generator that directly proxies bytes from provider to client"""
            bytes_served = 0
            chunk_count = 0
            response = None
            
            try:
                # Emit stream started event
                await self._emit_event("STREAM_STARTED", stream_id, {
                    "url": current_url,
                    "client_id": client_id,
                    "mode": "direct_proxy"
                })

                # Prepare headers
                headers = {
                    'User-Agent': stream_info.user_agent,
                    'Referer': f"{urlparse(current_url).scheme}://{urlparse(current_url).netloc}/",
                    'Origin': f"{urlparse(current_url).scheme}://{urlparse(current_url).netloc}",
                    'Accept': '*/*',
                    'Connection': 'keep-alive'
                }

                if range_header:
                    headers['Range'] = range_header

                # Select appropriate HTTP client
                client_to_use = self.live_stream_client if stream_info.is_live_continuous else self.http_client

                # OPEN provider connection - happens ONLY when client starts consuming
                logger.info(f"Opening provider connection for {stream_id} to {current_url}")
                response = await client_to_use.stream('GET', current_url, headers=headers, follow_redirects=True).__aenter__()
                response.raise_for_status()

                logger.info(f"Provider connected: {response.status_code}, Content-Type: {response.headers.get('content-type')}")

                # Direct byte-for-byte proxy - NO buffering, NO transcoding
                async for chunk in response.aiter_bytes(chunk_size=32768):
                    yield chunk
                    bytes_served += len(chunk)
                    chunk_count += 1

                    # Update stats (lightweight)
                    if chunk_count == 1:
                        logger.info(f"First chunk delivered to client {client_id}: {len(chunk)} bytes")
                    elif chunk_count % 100 == 0:
                        logger.debug(f"Client {client_id}: {chunk_count} chunks, {bytes_served} bytes")

                logger.info(f"Stream completed for client {client_id}: {chunk_count} chunks, {bytes_served} bytes")

                # Emit stream stopped event
                await self._emit_event("STREAM_STOPPED", stream_id, {
                    "client_id": client_id,
                    "bytes_served": bytes_served,
                    "chunks_served": chunk_count
                })

            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPError) as e:
                logger.warning(f"Stream error for client {client_id}: {type(e).__name__}: {e}")

                # Try seamless failover
                if stream_info.failover_urls:
                    logger.info(f"Attempting seamless failover for client {client_id}")
                    new_response = await self._seamless_failover(stream_id, e)
                    
                    if new_response:
                        # Continue streaming from failover URL
                        try:
                            async for chunk in new_response.aiter_bytes(chunk_size=32768):
                                yield chunk
                                bytes_served += len(chunk)
                                chunk_count += 1
                            
                            logger.info(f"Failover successful for client {client_id}")
                        except Exception as failover_error:
                            logger.error(f"Failover stream also failed: {failover_error}")
                            await self._emit_event("STREAM_FAILED", stream_id, {
                                "client_id": client_id,
                                "error": str(failover_error),
                                "error_type": "failover_failed"
                            })
                    else:
                        await self._emit_event("STREAM_FAILED", stream_id, {
                            "client_id": client_id,
                            "error": str(e),
                            "error_type": type(e).__name__
                        })
                else:
                    await self._emit_event("STREAM_FAILED", stream_id, {
                        "client_id": client_id,
                        "error": str(e),
                        "error_type": type(e).__name__,
                        "no_failover": True
                    })

            except Exception as e:
                logger.error(f"Unexpected error for client {client_id}: {e}")
                await self._emit_event("STREAM_FAILED", stream_id, {
                    "client_id": client_id,
                    "error": str(e),
                    "error_type": "unexpected"
                })

            finally:
                # CLOSE provider connection immediately when done
                if response:
                    try:
                        await response.aclose()
                        logger.info(f"Provider connection closed for client {client_id}")
                    except:
                        pass

                # Update stats
                if client_id in self.clients:
                    self.clients[client_id].bytes_served += bytes_served
                    self.clients[client_id].last_access = datetime.now()
                
                if stream_id in self.streams:
                    self.streams[stream_id].total_bytes_served += bytes_served
                    self.streams[stream_id].last_access = datetime.now()
                
                self._stats.total_bytes_served += bytes_served

                # Cleanup client
                await self.cleanup_client(client_id)

        # Determine content type
        if current_url.endswith('.ts') or '/live/' in current_url:
            content_type = "video/mp2t"
        elif current_url.endswith('.mp4'):
            content_type = "video/mp4"
        elif current_url.endswith('.mkv'):
            content_type = "video/x-matroska"
        elif current_url.endswith('.webm'):
            content_type = "video/webm"
        else:
            content_type = "application/octet-stream"

        headers = {
            "Content-Type": content_type,
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
            "Accept-Ranges": "bytes"
        }

        return StreamingResponse(generate(), media_type=content_type, headers=headers)

    async def _seamless_failover(self, stream_id: str, error: Exception) -> Optional[httpx.Response]:
        """
        Attempt seamless failover to next URL.
        Returns new response object if successful, None if all failovers exhausted.
        """
        if stream_id not in self.streams:
            return None

        stream_info = self.streams[stream_id]
        
        if not stream_info.failover_urls:
            logger.warning(f"No failover URLs available for stream {stream_id}")
            return None

        # Try next failover URL
        next_index = (stream_info.current_failover_index + 1) % len(stream_info.failover_urls)
        next_url = stream_info.failover_urls[next_index]

        logger.info(f"Attempting failover for stream {stream_id} to: {next_url}")

        try:
            headers = {
                'User-Agent': stream_info.user_agent,
                'Referer': f"{urlparse(next_url).scheme}://{urlparse(next_url).netloc}/",
                'Origin': f"{urlparse(next_url).scheme}://{urlparse(next_url).netloc}",
                'Accept': '*/*'
            }

            client_to_use = self.live_stream_client if stream_info.is_live_continuous else self.http_client
            
            # Open new connection to failover URL
            new_response = await client_to_use.stream('GET', next_url, headers=headers, follow_redirects=True).__aenter__()
            new_response.raise_for_status()

            # Update stream info
            old_url = stream_info.current_url
            stream_info.current_url = next_url
            stream_info.current_failover_index = next_index
            stream_info.failover_attempts += 1
            stream_info.last_failover_time = datetime.now()

            logger.info(f"Seamless failover successful for stream {stream_id}")

            await self._emit_event("FAILOVER_TRIGGERED", stream_id, {
                "old_url": old_url,
                "new_url": next_url,
                "failover_index": next_index,
                "attempt_number": stream_info.failover_attempts,
                "reason": str(error),
                "seamless": True
            })

            return new_response

        except Exception as e:
            logger.error(f"Failover attempt failed for stream {stream_id}: {e}")
            stream_info.failover_attempts += 1
            
            # Try next failover URL recursively if available
            if stream_info.failover_attempts < len(stream_info.failover_urls) * stream_info.max_retries:
                return await self._seamless_failover(stream_id, e)
            
            return None

    # ============================================================================
    # HLS SUPPORT (Keep existing shared approach - it works!)
    # ============================================================================

    async def get_playlist_content(
        self,
        stream_id: str,
        client_id: str,
        base_proxy_url: str
    ) -> Optional[str]:
        """Get and process playlist content for HLS streams"""
        if stream_id not in self.streams:
            return None

        stream_info = self.streams[stream_id]
        current_url = stream_info.current_url or stream_info.original_url

        try:
            logger.info(f"Fetching HLS playlist from: {current_url}")
            headers = {'User-Agent': stream_info.user_agent}
            response = await self.http_client.get(current_url, headers=headers)
            response.raise_for_status()

            content = response.text
            final_url = str(response.url)
            stream_info.final_playlist_url = final_url

            parsed_url = urlparse(final_url)
            base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
            if parsed_url.path:
                path_parts = parsed_url.path.rsplit('/', 1)
                if len(path_parts) > 1:
                    base_url += path_parts[0] + '/'
                else:
                    base_url += '/'
            else:
                base_url += '/'

            processor = M3U8Processor(base_proxy_url, client_id, stream_info.user_agent, final_url)
            processed_content = processor.process_playlist(content, base_proxy_url, base_url)

            stream_info.last_access = datetime.now()
            if client_id in self.clients:
                self.clients[client_id].last_access = datetime.now()

            return processed_content

        except Exception as e:
            logger.error(f"Error fetching playlist for stream {stream_id}: {e}")
            stream_info.error_count += 1
            return None

    async def proxy_hls_segment(
        self,
        stream_id: str,
        client_id: str,
        segment_url: str,
        range_header: Optional[str] = None
    ) -> StreamingResponse:
        """Proxy individual HLS segment - direct pass-through"""
        logger.info(f"Proxying HLS segment for stream {stream_id}, client {client_id}")

        async def segment_generator():
            bytes_served = 0
            try:
                headers = {}
                if range_header:
                    headers['Range'] = range_header

                # Get stream info for user agent
                if stream_id in self.streams:
                    headers['User-Agent'] = self.streams[stream_id].user_agent

                async with self.http_client.stream('GET', segment_url, headers=headers, follow_redirects=True) as response:
                    response.raise_for_status()
                    
                    async for chunk in response.aiter_bytes(chunk_size=32768):
                        yield chunk
                        bytes_served += len(chunk)

                # Update stats
                if client_id in self.clients:
                    self.clients[client_id].bytes_served += bytes_served
                    self.clients[client_id].segments_served += 1
                    self.clients[client_id].last_access = datetime.now()

                if stream_id in self.streams:
                    self.streams[stream_id].total_bytes_served += bytes_served
                    self.streams[stream_id].total_segments_served += 1
                    self.streams[stream_id].last_access = datetime.now()

                self._stats.total_bytes_served += bytes_served
                self._stats.total_segments_served += 1

            except Exception as e:
                logger.error(f"Error streaming HLS segment: {e}")
                raise

        return StreamingResponse(
            segment_generator(),
            media_type="video/MP2T",
            headers={"Cache-Control": "no-cache"}
        )

    # ============================================================================
    # HEALTH CHECKS AND CLEANUP
    # ============================================================================

    async def _periodic_health_check(self):
        """Periodic health check for streams"""
        while self._running:
            try:
                for stream_id, stream_info in list(self.streams.items()):
                    if stream_info.client_count == 0:
                        continue

                    if (stream_info.last_health_check is None or
                        (datetime.now() - stream_info.last_health_check).total_seconds() >= stream_info.health_check_interval):
                        
                        is_healthy = await self._health_check_stream(stream_id)
                        
                        if not is_healthy and stream_info.failover_urls:
                            logger.warning(f"Stream {stream_id} failed health check, marking for failover")
                            # For direct proxy, failover happens per-client during streaming
                            # Just update the current_url so new connections use the failover
                            await self._try_update_failover_url(stream_id)

                await asyncio.sleep(60)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in periodic health check: {e}")
                await asyncio.sleep(60)

    async def _health_check_stream(self, stream_id: str) -> bool:
        """Check if stream URL is healthy"""
        if stream_id not in self.streams:
            return False

        stream_info = self.streams[stream_id]
        current_url = stream_info.current_url or stream_info.original_url

        try:
            headers = {'User-Agent': stream_info.user_agent}
            if stream_info.is_hls:
                response = await self.http_client.get(current_url, headers=headers, timeout=10.0)
            else:
                response = await self.http_client.head(current_url, headers=headers, timeout=10.0)
            
            response.raise_for_status()
            stream_info.last_health_check = datetime.now()
            return True
        except Exception as e:
            logger.warning(f"Health check failed for stream {stream_id}: {e}")
            return False

    async def _try_update_failover_url(self, stream_id: str) -> bool:
        """Update to next failover URL for future connections"""
        if stream_id not in self.streams:
            return False

        stream_info = self.streams[stream_id]
        if not stream_info.failover_urls:
            return False

        next_index = (stream_info.current_failover_index + 1) % len(stream_info.failover_urls)
        old_url = stream_info.current_url
        stream_info.current_url = stream_info.failover_urls[next_index]
        stream_info.current_failover_index = next_index
        
        logger.info(f"Updated failover URL for stream {stream_id}: {old_url} -> {stream_info.current_url}")
        return True

    async def _periodic_cleanup(self):
        """Periodic cleanup of inactive clients and streams"""
        while self._running:
            try:
                await self._cleanup_inactive_clients()
                await self._cleanup_inactive_streams()
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in periodic cleanup: {e}")
                await asyncio.sleep(30)

    async def _cleanup_inactive_clients(self):
        """Clean up clients that haven't been accessed recently"""
        current_time = datetime.now()
        inactive_clients = []

        for client_id, client_info in self.clients.items():
            if (current_time - client_info.last_access).seconds > self.client_timeout:
                inactive_clients.append(client_id)

        for client_id in inactive_clients:
            await self.cleanup_client(client_id)

    async def _cleanup_inactive_streams(self):
        """Clean up streams with no active clients"""
        current_time = datetime.now()
        inactive_streams = []

        for stream_id, stream_info in self.streams.items():
            has_active_clients = stream_id in self.stream_clients and len(self.stream_clients[stream_id]) > 0
            is_old = (current_time - stream_info.last_access).seconds > self.stream_timeout

            if not has_active_clients and is_old:
                inactive_streams.append(stream_id)

        for stream_id in inactive_streams:
            if stream_id in self.streams:
                del self.streams[stream_id]
                if stream_id in self.stream_clients:
                    del self.stream_clients[stream_id]
                self._stats.active_streams -= 1
                logger.info(f"Cleaned up inactive stream: {stream_id}")

    def get_stats(self) -> Dict:
        """Get comprehensive stats"""
        active_stream_count = sum(1 for stream in self.streams.values()
                                  if stream.client_count > 0 and stream.is_active)
        active_client_count = sum(1 for client in self.clients.values()
                                  if (datetime.now() - client.last_access).seconds < self.client_timeout)

        return {
            "proxy_stats": {
                "total_streams": len(self.streams),
                "active_streams": active_stream_count,
                "total_clients": len(self.clients),
                "active_clients": active_client_count,
                "total_bytes_served": self._stats.total_bytes_served,
                "total_segments_served": self._stats.total_segments_served,
                "uptime_seconds": (datetime.now() - self._stats.uptime_start).seconds
            },
            "streams": [
                {
                    "stream_id": stream.stream_id,
                    "original_url": stream.original_url,
                    "current_url": stream.current_url,
                    "user_agent": stream.user_agent,
                    "client_count": stream.client_count,
                    "total_bytes_served": stream.total_bytes_served,
                    "total_segments_served": stream.total_segments_served,
                    "error_count": stream.error_count,
                    "is_active": stream.is_active,
                    "has_failover": len(stream.failover_urls) > 0,
                    "stream_type": "HLS" if stream.is_hls else ("VOD" if stream.is_vod else "Live Continuous"),
                    "created_at": stream.created_at.isoformat(),
                    "last_access": stream.last_access.isoformat()
                }
                for stream in self.streams.values()
            ],
            "clients": [
                {
                    "client_id": client.client_id,
                    "stream_id": client.stream_id,
                    "user_agent": client.user_agent,
                    "ip_address": client.ip_address,
                    "bytes_served": client.bytes_served,
                    "segments_served": client.segments_served,
                    "created_at": client.created_at.isoformat(),
                    "last_access": client.last_access.isoformat()
                }
                for client in self.clients.values()
            ]
        }
