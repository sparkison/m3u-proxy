"""
Stream Manager with Separate Proxy Paths
This version implements efficient per-client proxying for continuous streams
while maintaining the shared buffer approach for HLS segments.
"""

import m3u8
import asyncio
import httpx
import logging
import subprocess
import signal
import os
from typing import Dict, Optional, AsyncIterator, List, Set, Any
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
    # HLS variant tracking - for variant playlists that are part of a master playlist
    parent_stream_id: Optional[str] = None
    is_variant_stream: bool = False
    # Custom metadata - arbitrary key/value pairs for external identification
    metadata: Dict[str, str] = field(default_factory=dict)
    # Transcoding configuration
    is_transcoded: bool = False
    transcode_profile: Optional[str] = None
    transcode_ffmpeg_args: List[str] = field(default_factory=list)
    transcode_process: Optional[asyncio.subprocess.Process] = None


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
    def __init__(self, base_url: str, client_id: str, user_agent: Optional[str] = None, original_url: Optional[str] = None, parent_stream_id: Optional[str] = None):
        self.base_url = base_url
        self.client_id = client_id
        self.user_agent = user_agent or settings.DEFAULT_USER_AGENT
        self.original_url = original_url or base_url
        self.parent_stream_id = parent_stream_id

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
            # For variant playlists, include parent stream ID
            parent_param = f"&parent={self.parent_stream_id}" if self.parent_stream_id else ""
            return f"{base_proxy_url}/playlist.m3u8?url={encoded_url}&client_id={self.client_id}{parent_param}"
        else:
            return f"{base_proxy_url}/segment.ts?url={encoded_url}&client_id={self.client_id}"


class StreamManager:
    def __init__(self, redis_url: Optional[str] = None, enable_pooling: bool = True):
        self.streams: Dict[str, StreamInfo] = {}
        self.clients: Dict[str, ClientInfo] = {}
        self.stream_clients: Dict[str, Set[str]] = {}
        self.client_timeout = settings.CLIENT_TIMEOUT
        self.stream_timeout = settings.STREAM_TIMEOUT
        
        # Pooling configuration
        self.enable_pooling = enable_pooling
        self.pooled_manager: Optional[Any] = None  # Will be PooledStreamManager if available
        
        # Redis configuration
        if redis_url and enable_pooling:
            try:
                from pooled_stream_manager import PooledStreamManager
                self.pooled_manager = PooledStreamManager(redis_url=redis_url)
                logger.info("Redis pooling enabled")
            except ImportError:
                logger.warning("Redis pooling requested but pooled_stream_manager not available")
            except Exception as e:
                logger.warning(f"Failed to initialize Redis pooling: {e}")
        elif enable_pooling:
            logger.info("Pooling enabled in single-worker mode (no Redis)")
            try:
                from pooled_stream_manager import PooledStreamManager
                self.pooled_manager = PooledStreamManager(enable_sharing=False)
            except ImportError:
                logger.warning("pooled_stream_manager not available")
        else:
            logger.info("Connection pooling disabled")
        
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
        
        # Start pooled manager if available
        if self.pooled_manager:
            await self.pooled_manager.start()
            
        self._cleanup_task = asyncio.create_task(self._periodic_cleanup())
        self._health_check_task = asyncio.create_task(self._periodic_health_check())
        
        mode = "with Redis pooling" if (self.pooled_manager and self.pooled_manager.enable_sharing) else "single-worker"
        logger.info(f"Stream manager started {mode} and optimized connection pooling")

    async def stop(self):
        """Stop the stream manager"""
        self._running = False
        
        # Stop pooled manager
        if self.pooled_manager:
            await self.pooled_manager.stop()
            
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
        user_agent: Optional[str] = None,
        parent_stream_id: Optional[str] = None,
        metadata: Optional[Dict[str, str]] = None,
        is_transcoded: bool = False,
        transcode_profile: Optional[str] = None,
        transcode_ffmpeg_args: Optional[List[str]] = None
    ) -> str:
        """Get or create a stream and return its ID
        
        Args:
            stream_url: The URL of the stream
            failover_urls: Optional list of failover URLs
            user_agent: Optional user agent string
            parent_stream_id: Optional parent stream ID for variant playlists
            metadata: Optional custom key/value pairs for external identification
            is_transcoded: Whether this stream should be transcoded
            transcode_profile: Name of the transcoding profile to use
            transcode_ffmpeg_args: FFmpeg arguments for transcoding
        """
        import hashlib
        stream_id = hashlib.md5(stream_url.encode()).hexdigest()

        if stream_id not in self.streams:
            now = datetime.now()
            if user_agent is None:
                user_agent = settings.DEFAULT_USER_AGENT

            # Detect stream type
            is_hls, is_vod, is_live_continuous = self._detect_stream_type(stream_url)
            
            # If this is a variant stream, inherit user agent from parent
            is_variant = parent_stream_id is not None
            if is_variant and parent_stream_id in self.streams:
                user_agent = self.streams[parent_stream_id].user_agent

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
                is_live_continuous=is_live_continuous,
                parent_stream_id=parent_stream_id,
                is_variant_stream=is_variant,
                metadata=metadata or {},
                is_transcoded=is_transcoded,
                transcode_profile=transcode_profile,
                transcode_ffmpeg_args=transcode_ffmpeg_args or []
            )
            self.stream_clients[stream_id] = set()
            
            # Only count non-variant streams in stats
            if not is_variant:
                self._stats.total_streams += 1
                self._stats.active_streams += 1
            
            stream_type = "HLS" if is_hls else ("VOD" if is_vod else "Live Continuous")
            variant_info = f" (variant of {parent_stream_id})" if is_variant else ""
            logger.info(f"Created new stream: {stream_id} ({stream_type}){variant_info} with user agent: {user_agent}")

        self.streams[stream_id].last_access = datetime.now()
        return stream_id

    async def register_client(
        self,
        client_id: str,
        stream_id: str,
        user_agent: Optional[str] = None,
        ip_address: Optional[str] = None
    ) -> ClientInfo:
        """Register a client for a stream
        
        If the stream is a variant, the client is registered with the parent stream instead.
        """
        now = datetime.now()
        
        # If this is a variant stream, register client with the parent instead
        effective_stream_id = stream_id
        if stream_id in self.streams:
            stream = self.streams[stream_id]
            if stream.is_variant_stream and stream.parent_stream_id:
                effective_stream_id = stream.parent_stream_id
                logger.debug(f"Redirecting client registration from variant {stream_id} to parent {effective_stream_id}")

        if client_id not in self.clients:
            self.clients[client_id] = ClientInfo(
                client_id=client_id,
                created_at=now,
                last_access=now,
                user_agent=user_agent,
                ip_address=ip_address,
                stream_id=effective_stream_id
            )
            self._stats.total_clients += 1
            self._stats.active_clients += 1
            logger.info(f"Registered new client: {client_id}")

        if effective_stream_id in self.stream_clients:
            self.stream_clients[effective_stream_id].add(client_id)
            self.streams[effective_stream_id].client_count = len(self.stream_clients[effective_stream_id])
            self.streams[effective_stream_id].connected_clients.add(client_id)

        client_info = self.clients[client_id]
        client_info.last_access = now
        client_info.stream_id = effective_stream_id
        client_info.is_connected = True

        await self._emit_event("CLIENT_CONNECTED", effective_stream_id, {
            "client_id": client_id,
            "user_agent": user_agent,
            "ip_address": ip_address,
            "stream_client_count": len(self.stream_clients[effective_stream_id]) if effective_stream_id in self.stream_clients else 0
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
            stream_context = None
            last_stats_update = 0  # Track bytes at last stats update
            
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

                # IMPORTANT: Do NOT send Range headers for live continuous streams
                # Live IPTV streams (.ts) are infinite and don't support range requests
                # Range requests can cause providers to immediately close the connection
                if range_header and not stream_info.is_live_continuous:
                    headers['Range'] = range_header
                    logger.info(f"Including Range header for VOD stream: {range_header}")
                elif range_header and stream_info.is_live_continuous:
                    logger.info(f"Ignoring Range header for live stream (not supported)")

                # Select appropriate HTTP client
                client_to_use = self.live_stream_client if stream_info.is_live_continuous else self.http_client

                # OPEN provider connection - happens ONLY when client starts consuming
                logger.info(f"Opening provider connection for {stream_id} to {current_url}")
                
                # Get the stream context manager
                stream_context = client_to_use.stream('GET', current_url, headers=headers, follow_redirects=True)
                # Enter the context to get the response object
                response = await stream_context.__aenter__()
                
                # Now we can call methods on the actual response object
                response.raise_for_status()

                logger.info(f"Provider connected: {response.status_code}, Content-Type: {response.headers.get('content-type')}")
                
                # Direct byte-for-byte proxy - NO buffering, NO transcoding
                async for chunk in response.aiter_bytes(chunk_size=32768):
                    yield chunk
                    bytes_served += len(chunk)
                    chunk_count += 1

                    # Update stats periodically (every 10 chunks = ~320KB)
                    if chunk_count % 10 == 0:
                        # Calculate delta since last update
                        bytes_delta = bytes_served - last_stats_update
                        
                        if client_id in self.clients:
                            self.clients[client_id].last_access = datetime.now()
                            self.clients[client_id].bytes_served += bytes_delta
                        if stream_id in self.streams:
                            self.streams[stream_id].last_access = datetime.now()
                            self.streams[stream_id].total_bytes_served += bytes_delta
                        self._stats.total_bytes_served += bytes_delta
                        
                        # Update last stats checkpoint
                        last_stats_update = bytes_served

                    # Update stats (lightweight) - log more frequently to debug
                    if chunk_count == 1:
                        logger.info(f"First chunk delivered to client {client_id}: {len(chunk)} bytes")
                    elif chunk_count <= 10:
                        logger.info(f"Chunk {chunk_count} delivered to client {client_id}: {len(chunk)} bytes")
                    elif chunk_count % 100 == 0:
                        logger.info(f"Client {client_id}: {chunk_count} chunks, {bytes_served:,} bytes served")

                logger.info(f"Stream completed for client {client_id}: {chunk_count} chunks, {bytes_served} bytes")

                # Emit stream stopped event
                await self._emit_event("STREAM_STOPPED", stream_id, {
                    "client_id": client_id,
                    "bytes_served": bytes_served,
                    "chunks_served": chunk_count
                })

            except httpx.ReadError as e:
                # ReadError often means client disconnected or provider closed connection
                # This is especially common with Range requests on live streams
                # MUST be caught before NetworkError since ReadError is a subclass
                error_str = str(e) if str(e) else "<empty ReadError>"
                logger.info(f"ReadError for client {client_id}: {error_str} (bytes_served: {bytes_served}, chunk_count: {chunk_count})")
                
                if bytes_served == 0:
                    # No data was sent - provider likely rejected the request
                    logger.warning(f"Provider closed connection immediately for {client_id} - possibly due to Range header on live stream")
                    await self._emit_event("STREAM_FAILED", stream_id, {
                        "client_id": client_id,
                        "error": "Provider closed connection immediately (may not support Range requests)",
                        "error_type": "ReadError",
                        "bytes_served": 0
                    })
                else:
                    # Some data was sent - likely client disconnection during streaming
                    logger.info(f"Client {client_id} likely disconnected during streaming")
                    await self._emit_event("CLIENT_DISCONNECTED", stream_id, {
                        "client_id": client_id,
                        "bytes_served": bytes_served,
                        "chunks_served": chunk_count,
                        "reason": "read_error_during_stream"
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

            except (ConnectionResetError, ConnectionError, BrokenPipeError) as e:
                # Client disconnected - this is normal, not an error
                logger.info(f"Client {client_id} disconnected: {type(e).__name__}")
                await self._emit_event("CLIENT_DISCONNECTED", stream_id, {
                    "client_id": client_id,
                    "bytes_served": bytes_served,
                    "chunks_served": chunk_count,
                    "reason": "client_disconnected"
                })

            except Exception as e:
                # Log with more detail for debugging
                error_str = str(e) if str(e) else f"<empty {type(e).__name__}>"
                logger.warning(f"Stream error for client {client_id}: {type(e).__name__}: {error_str}")
                logger.warning(f"Exception details - bytes_served: {bytes_served}, chunks: {chunk_count}")
                
                # Check if this looks like a client disconnection (empty error message often indicates this)
                if not str(e) and bytes_served > 0:
                    logger.info(f"Likely client disconnection for {client_id} (empty error message, data was streaming)")
                    await self._emit_event("CLIENT_DISCONNECTED", stream_id, {
                        "client_id": client_id,
                        "bytes_served": bytes_served,
                        "chunks_served": chunk_count,
                        "reason": "possible_client_disconnection"
                    })
                else:
                    await self._emit_event("STREAM_FAILED", stream_id, {
                        "client_id": client_id,
                        "error": error_str,
                        "error_type": type(e).__name__,
                        "bytes_served": bytes_served
                    })

            finally:
                # Manually exit the context manager
                if stream_context is not None:
                    try:
                        await stream_context.__aexit__(None, None, None)
                        logger.info(f"Provider connection closed for client {client_id}")
                    except Exception as close_error:
                        logger.warning(f"Error closing response: {close_error}")

                # Update final stats (add any remaining bytes not yet counted)
                bytes_remaining = bytes_served - last_stats_update
                if bytes_remaining > 0:
                    if client_id in self.clients:
                        self.clients[client_id].bytes_served += bytes_remaining
                        self.clients[client_id].last_access = datetime.now()
                    
                    if stream_id in self.streams:
                        self.streams[stream_id].total_bytes_served += bytes_remaining
                        self.streams[stream_id].last_access = datetime.now()
                    
                    self._stats.total_bytes_served += bytes_remaining

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
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
            "Access-Control-Allow-Headers": "*",
            "Access-Control-Expose-Headers": "*"
        }

        # For live streams, explicitly state we don't support range requests
        if stream_info.is_live_continuous:
            headers["Accept-Ranges"] = "none"
        else:
            headers["Accept-Ranges"] = "bytes"

        return StreamingResponse(generate(), media_type=content_type, headers=headers)

    async def stream_transcoded(
        self,
        stream_id: str,
        client_id: str,
        range_header: Optional[str] = None
    ) -> StreamingResponse:
        """
        Real-time transcoding stream using FFmpeg.
        Fetches the original stream and transcodes it on-the-fly using the configured profile.
        """
        if stream_id not in self.streams:
            raise HTTPException(status_code=404, detail="Stream not found")

        stream_info = self.streams[stream_id]
        
        if not stream_info.is_transcoded:
            raise HTTPException(status_code=400, detail="Stream is not configured for transcoding")
            
        if not stream_info.transcode_ffmpeg_args:
            raise HTTPException(status_code=500, detail="No transcoding configuration available")

        current_url = stream_info.current_url or stream_info.original_url

        # Register this client
        if client_id not in self.clients:
            await self.register_client(client_id, stream_id)

        logger.info(f"Starting transcoded stream for client {client_id}, stream {stream_id}, profile: {stream_info.transcode_profile}")

        async def generate():
            """Generator that transcodes the stream using FFmpeg and yields the output"""
            bytes_served = 0
            chunk_count = 0
            ffmpeg_process = None
            
            try:
                # Emit stream started event
                await self._emit_event("STREAM_STARTED", stream_id, {
                    "url": current_url,
                    "client_id": client_id,
                    "mode": "transcoded",
                    "profile": stream_info.transcode_profile,
                    "ffmpeg_args": stream_info.transcode_ffmpeg_args
                })

                # Build FFmpeg command
                ffmpeg_cmd = ["ffmpeg"]
                
                # Add input arguments and URL
                cmd_args = stream_info.transcode_ffmpeg_args.copy()
                
                # Replace template variables in the FFmpeg args for streaming
                streaming_variables = {
                    "input_url": current_url,
                    "output_args": "-",  # Just stdout, format will be handled separately
                    "format": "mpegts"   # Force MPEGTS format for streaming
                }
                
                # Re-render the profile with streaming-specific variables
                from transcoding import get_profile_manager
                profile_manager = get_profile_manager()
                profile = None
                if stream_info.transcode_profile:
                    profile = profile_manager.get_profile(stream_info.transcode_profile)
                if profile:
                    cmd_args = profile.render(streaming_variables)
                else:
                    # Fallback to original args if profile not found
                    cmd_args = stream_info.transcode_ffmpeg_args.copy()
                    for i, arg in enumerate(cmd_args):
                        for var, value in streaming_variables.items():
                            placeholder = f"{{{var}}}"
                            if placeholder in arg:
                                cmd_args[i] = arg.replace(placeholder, value)
                
                ffmpeg_cmd.extend(cmd_args)

                logger.info(f"Starting FFmpeg transcoding: {' '.join(ffmpeg_cmd)}")
                
                # Start FFmpeg process with stderr capture for debugging
                ffmpeg_process = await asyncio.create_subprocess_exec(
                    *ffmpeg_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    stdin=asyncio.subprocess.DEVNULL
                )
                
                # Start a task to read stderr for debugging
                async def log_ffmpeg_stderr():
                    if ffmpeg_process.stderr:
                        while True:
                            try:
                                line = await ffmpeg_process.stderr.readline()
                                if not line:
                                    break
                                line_str = line.decode('utf-8', errors='ignore').strip()
                                if line_str:
                                    logger.info(f"FFmpeg stderr: {line_str}")
                            except Exception as e:
                                logger.error(f"Error reading FFmpeg stderr: {e}")
                                break
                
                # Start stderr logging task
                stderr_task = asyncio.create_task(log_ffmpeg_stderr())
                
                # Store the process in stream info for cleanup
                stream_info.transcode_process = ffmpeg_process

                logger.info(f"FFmpeg process started with PID: {ffmpeg_process.pid}")
                
                # Ensure stdout is available
                if not ffmpeg_process.stdout:
                    raise Exception("FFmpeg process stdout is not available")
                
                # Read and yield transcoded data
                while True:
                    try:
                        # Read chunk from FFmpeg stdout
                        chunk = await asyncio.wait_for(
                            ffmpeg_process.stdout.read(32768), 
                            timeout=30.0
                        )
                        
                        if not chunk:
                            # FFmpeg finished or stopped outputting
                            break
                            
                        yield chunk
                        bytes_served += len(chunk)
                        chunk_count += 1

                        # Update stats periodically
                        if chunk_count % 10 == 0:
                            if client_id in self.clients:
                                self.clients[client_id].last_access = datetime.now()
                                self.clients[client_id].bytes_served += len(chunk)
                            if stream_id in self.streams:
                                self.streams[stream_id].last_access = datetime.now()
                                self.streams[stream_id].total_bytes_served += len(chunk)
                            self._stats.total_bytes_served += len(chunk)

                        # Log progress
                        if chunk_count == 1:
                            logger.info(f"First transcoded chunk delivered to client {client_id}: {len(chunk)} bytes")
                        elif chunk_count % 100 == 0:
                            logger.info(f"Transcoding progress - Client {client_id}: {chunk_count} chunks, {bytes_served:,} bytes")

                    except asyncio.TimeoutError:
                        # Check if FFmpeg process is still alive
                        if ffmpeg_process.returncode is not None:
                            logger.error(f"FFmpeg process died unexpectedly (exit code: {ffmpeg_process.returncode})")
                            break
                        else:
                            logger.warning(f"FFmpeg output timeout for client {client_id}, continuing...")
                            continue

                logger.info(f"Transcoding completed for client {client_id}: {chunk_count} chunks, {bytes_served} bytes")

                # Emit stream stopped event
                await self._emit_event("STREAM_STOPPED", stream_id, {
                    "client_id": client_id,
                    "bytes_served": bytes_served,
                    "chunks_served": chunk_count,
                    "mode": "transcoded"
                })

            except Exception as e:
                logger.error(f"Error during transcoding for client {client_id}: {e}")
                
                # Emit failure event
                await self._emit_event("STREAM_FAILED", stream_id, {
                    "client_id": client_id,
                    "error": str(e),
                    "error_type": type(e).__name__,
                    "bytes_served": bytes_served,
                    "mode": "transcoded"
                })
                
                raise HTTPException(status_code=500, detail=f"Transcoding error: {str(e)}")
                
            finally:
                # Clean up stderr logging task
                try:
                    stderr_task.cancel()
                except NameError:
                    pass  # stderr_task wasn't created
                    
                # Clean up FFmpeg process
                if ffmpeg_process:
                    try:
                        if ffmpeg_process.returncode is None:  # Process is still running
                            logger.info(f"Terminating FFmpeg process {ffmpeg_process.pid}")
                            ffmpeg_process.terminate()
                            try:
                                await asyncio.wait_for(ffmpeg_process.wait(), timeout=5.0)
                            except asyncio.TimeoutError:
                                logger.warning(f"FFmpeg process {ffmpeg_process.pid} didn't terminate gracefully, killing it")
                                ffmpeg_process.kill()
                                await ffmpeg_process.wait()
                        
                        # Clear the process from stream info
                        if stream_info.transcode_process == ffmpeg_process:
                            stream_info.transcode_process = None
                            
                    except Exception as cleanup_error:
                        logger.error(f"Error cleaning up FFmpeg process: {cleanup_error}")

        # Set appropriate headers for transcoded stream
        headers = {
            "Content-Type": "video/mp2t",  # MPEG-TS format
            "Cache-Control": "no-cache, no-store, must-revalidate", 
            "Pragma": "no-cache",
            "Expires": "0",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
            "Access-Control-Allow-Headers": "*",
            "Access-Control-Expose-Headers": "*",
            "Accept-Ranges": "none"  # Transcoded streams don't support ranges
        }

        return StreamingResponse(generate(), media_type="video/mp2t", headers=headers)

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

            # Pass stream_id as parent for variant playlists, unless this is already a variant
            parent_id = stream_id if not stream_info.is_variant_stream else stream_info.parent_stream_id
            processor = M3U8Processor(base_proxy_url, client_id, stream_info.user_agent, final_url, parent_stream_id=parent_id)
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
            headers={
                "Cache-Control": "no-cache",
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
                "Access-Control-Allow-Headers": "*",
                "Access-Control-Expose-Headers": "*"
            }
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
            if (current_time - client_info.last_access).total_seconds() > self.client_timeout:
                inactive_clients.append(client_id)

        for client_id in inactive_clients:
            await self.cleanup_client(client_id)

    async def _cleanup_inactive_streams(self):
        """Clean up streams with no active clients"""
        current_time = datetime.now()
        inactive_streams = []

        for stream_id, stream_info in self.streams.items():
            # Count only ACTIVE clients for this stream
            active_client_count = 0
            if stream_id in self.stream_clients:
                for client_id in self.stream_clients[stream_id]:
                    if (client_id in self.clients and 
                        self.clients[client_id].is_connected):
                        active_client_count += 1

            has_active_clients = active_client_count > 0
            time_diff_seconds = (current_time - stream_info.last_access).total_seconds()
            is_old = time_diff_seconds > self.stream_timeout

            if not has_active_clients and is_old:
                logger.info(f"Marking stream {stream_id} for cleanup: no_active_clients={not has_active_clients}, time_diff={time_diff_seconds}s, timeout={self.stream_timeout}s")
                inactive_streams.append(stream_id)
            elif has_active_clients and is_old:
                logger.debug(f"Stream {stream_id} is old ({time_diff_seconds}s) but has {active_client_count} active clients - keeping alive")
            elif not has_active_clients and not is_old:
                logger.debug(f"Stream {stream_id} has no active clients but is recent ({time_diff_seconds}s < {self.stream_timeout}s) - keeping alive")

        for stream_id in inactive_streams:
            if stream_id in self.streams:
                logger.info(f"Cleaning up inactive stream: {stream_id}")
                del self.streams[stream_id]
                if stream_id in self.stream_clients:
                    del self.stream_clients[stream_id]
                self._stats.active_streams -= 1

    def get_stats(self) -> Dict:
        """Get comprehensive stats - aggregates variant stream stats into parent streams"""
        # Only count non-variant streams
        non_variant_streams = [s for s in self.streams.values() if not s.is_variant_stream]
        
        # Build a map of aggregated stats for parent streams
        stream_stats_map = {}
        for stream in self.streams.values():
            # If this is a variant, aggregate its stats into the parent
            if stream.is_variant_stream and stream.parent_stream_id:
                parent_id = stream.parent_stream_id
                if parent_id not in stream_stats_map:
                    # Initialize with parent stream data if it exists
                    if parent_id in self.streams:
                        parent = self.streams[parent_id]
                        # Count only ACTIVE clients for parent stream
                        active_parent_clients = 0
                        if parent_id in self.stream_clients:
                            for client_id in self.stream_clients[parent_id]:
                                if (client_id in self.clients and 
                                    self.clients[client_id].is_connected):
                                    active_parent_clients += 1
                        
                        stream_stats_map[parent_id] = {
                            "bytes": parent.total_bytes_served,
                            "segments": parent.total_segments_served,
                            "errors": parent.error_count,
                            "clients": active_parent_clients
                        }
                    else:
                        stream_stats_map[parent_id] = {"bytes": 0, "segments": 0, "errors": 0, "clients": 0}
                
                # Add variant's stats to parent
                stream_stats_map[parent_id]["bytes"] += stream.total_bytes_served
                stream_stats_map[parent_id]["segments"] += stream.total_segments_served
                stream_stats_map[parent_id]["errors"] += stream.error_count
                # Don't double-count clients - they're tracked at parent level
            elif not stream.is_variant_stream:
                # Non-variant stream - use its own stats with active client count
                stream_id = stream.stream_id
                if stream_id not in stream_stats_map:
                    # Count only ACTIVE clients for this stream
                    active_stream_clients = 0
                    if stream_id in self.stream_clients:
                        for client_id in self.stream_clients[stream_id]:
                            if (client_id in self.clients and 
                                self.clients[client_id].is_connected):
                                active_stream_clients += 1
                    
                    stream_stats_map[stream_id] = {
                        "bytes": stream.total_bytes_served,
                        "segments": stream.total_segments_served,
                        "errors": stream.error_count,
                        "clients": active_stream_clients
                    }
        
        # Count active streams (streams with at least one active client)
        active_stream_count = sum(1 for stream in non_variant_streams
                                  if stream_stats_map.get(stream.stream_id, {}).get("clients", 0) > 0 and stream.is_active)
        
        # Count only connected clients
        active_client_count = sum(1 for client in self.clients.values()
                                  if client.is_connected)

        return {
            "proxy_stats": {
                "total_streams": len(non_variant_streams),
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
                    "client_count": stream_stats_map.get(stream.stream_id, {}).get("clients", 0),
                    "total_bytes_served": stream_stats_map.get(stream.stream_id, {}).get("bytes", stream.total_bytes_served),
                    "total_segments_served": stream_stats_map.get(stream.stream_id, {}).get("segments", stream.total_segments_served),
                    "error_count": stream_stats_map.get(stream.stream_id, {}).get("errors", stream.error_count),
                    "is_active": stream.is_active,
                    "has_failover": len(stream.failover_urls) > 0,
                    "stream_type": "HLS" if stream.is_hls else ("VOD" if stream.is_vod else "Live Continuous"),
                    "created_at": stream.created_at.isoformat(),
                    "last_access": stream.last_access.isoformat(),
                    "metadata": stream.metadata
                }
                for stream in non_variant_streams
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
                    "last_access": client.last_access.isoformat(),
                    "is_connected": client.is_connected
                }
                for client in self.clients.values()
                if client.is_connected  # Only include connected clients
            ]
        }
