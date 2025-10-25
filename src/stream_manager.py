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
import time
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
    headers: Dict[str, str] = field(default_factory=dict)
    # Transcoding configuration
    is_transcoded: bool = False
    transcode_profile: Optional[str] = None
    transcode_ffmpeg_args: List[str] = field(default_factory=list)
    transcode_process: Optional[asyncio.subprocess.Process] = None
    # Key used by the pooled manager to identify shared transcoding processes
    transcode_stream_key: Optional[str] = None


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
            playlist = m3u8.loads(
                content, uri=original_base_url or self.original_url)

            # Handle both variant playlists (master) and media playlists
            if playlist.is_variant:
                for variant in playlist.playlists:
                    variant.uri = self._rewrite_url(
                        variant.absolute_uri, base_proxy_url)
                for media in playlist.media:
                    if media.uri:
                        media.uri = self._rewrite_url(
                            media.absolute_uri, base_proxy_url)
            else:
                for segment in playlist.segments:
                    segment.uri = self._rewrite_url(
                        segment.absolute_uri, base_proxy_url)
                # Handle initialization section if present
                for seg_map in (playlist.segment_map if isinstance(playlist.segment_map, list) else []):
                    if hasattr(seg_map, 'uri') and seg_map.uri:
                        seg_map.uri = self._rewrite_url(
                            seg_map.absolute_uri, base_proxy_url)

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
        
        # Track cancellation flags for active streaming generators
        # Key: client_id, Value: asyncio.Event that gets set when stream should stop
        self.client_cancel_events: Dict[str, asyncio.Event] = {}

        # Pooling configuration
        self.enable_pooling = enable_pooling
        # Will be PooledStreamManager if available
        self.pooled_manager: Optional[Any] = None

        # Redis configuration
        if redis_url and enable_pooling:
            try:
                from pooled_stream_manager import PooledStreamManager
                self.pooled_manager = PooledStreamManager(redis_url=redis_url)
                logger.info("Redis pooling enabled")
            except ImportError:
                logger.warning(
                    "Redis pooling requested but pooled_stream_manager not available")
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

        # Disable health checks for now until we can come up with a better approach
        # Using get/head requests can interfere with live streams, or 502 errors
        # Need to instead check the health during actual streaming requests
        # self._health_check_task = asyncio.create_task(
        #     self._periodic_health_check())

        mode = "with Redis pooling" if (
            self.pooled_manager and self.pooled_manager.enable_sharing) else "single-worker"
        logger.info(
            f"Stream manager started {mode} and optimized connection pooling")

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
        headers: Optional[Dict[str, str]] = None,
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
            headers: Optional dictionary of custom headers
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
            is_hls, is_vod, is_live_continuous = self._detect_stream_type(
                stream_url)

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
                headers=headers or {},
                is_transcoded=is_transcoded,
                transcode_profile=transcode_profile,
                transcode_ffmpeg_args=transcode_ffmpeg_args or []
            )
            self.stream_clients[stream_id] = set()

            # Only count non-variant streams in stats
            if not is_variant:
                self._stats.total_streams += 1
                self._stats.active_streams += 1

            stream_type = "HLS" if is_hls else (
                "VOD" if is_vod else "Live Continuous")
            variant_info = f" (variant of {parent_stream_id})" if is_variant else ""
            logger.info(
                f"Created new stream: {stream_id} ({stream_type}){variant_info} with user agent: {user_agent}")

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
                logger.debug(
                    f"Redirecting client registration from variant {stream_id} to parent {effective_stream_id}")

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
            self.streams[effective_stream_id].client_count = len(
                self.stream_clients[effective_stream_id])
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
        """Clean up a client and signal its streaming generator to stop"""
        if client_id in self.clients:
            client_info = self.clients[client_id]
            stream_id = client_info.stream_id
            
            # Signal the streaming generator to stop
            if client_id in self.client_cancel_events:
                self.client_cancel_events[client_id].set()
                logger.info(f"Signaled streaming generator to stop for client: {client_id}")

            if stream_id and stream_id in self.stream_clients:
                self.stream_clients[stream_id].discard(client_id)
                if stream_id in self.streams:
                    self.streams[stream_id].client_count = len(
                        self.stream_clients[stream_id])
                    self.streams[stream_id].connected_clients.discard(
                        client_id)

            await self._emit_event("CLIENT_DISCONNECTED", stream_id or "unknown", {
                "client_id": client_id,
                "bytes_served": client_info.bytes_served,
                "segments_served": client_info.segments_served
            })

            del self.clients[client_id]
            self._stats.active_clients -= 1
            
            # Clean up cancel event
            if client_id in self.client_cancel_events:
                del self.client_cancel_events[client_id]
            
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

        # Create cancellation event for this client
        cancel_event = asyncio.Event()
        self.client_cancel_events[client_id] = cancel_event

        logger.info(
            f"Starting direct proxy for client {client_id}, stream {stream_id}")

        # Variables to capture from the generator for response headers
        provider_status_code = None
        provider_content_range = None
        provider_content_length = None

        async def generate():
            """Generator that directly proxies bytes from provider to client"""
            nonlocal provider_status_code, provider_content_range, provider_content_length
            
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
                headers.update(stream_info.headers)

                # IMPORTANT: Do NOT send Range headers for live continuous streams
                # Live IPTV streams (.ts) are infinite and don't support range requests
                # Range requests can cause providers to immediately close the connection
                if range_header and not stream_info.is_live_continuous:
                    headers['Range'] = range_header
                    logger.info(
                        f"Including Range header for VOD stream: {range_header}")
                elif range_header and stream_info.is_live_continuous:
                    logger.info(
                        f"Ignoring Range header for live stream (not supported)")

                # Select appropriate HTTP client
                client_to_use = self.live_stream_client if stream_info.is_live_continuous else self.http_client

                # OPEN provider connection - happens ONLY when client starts consuming
                logger.info(
                    f"Opening provider connection for {stream_id} to {current_url}")

                # Get the stream context manager
                stream_context = client_to_use.stream(
                    'GET', current_url, headers=headers, follow_redirects=True)
                # Enter the context to get the response object
                response = await stream_context.__aenter__()

                # Now we can call methods on the actual response object
                response.raise_for_status()

                # Capture provider response details for proper HTTP 206 handling
                provider_status_code = response.status_code
                provider_content_range = response.headers.get('content-range')
                provider_content_length = response.headers.get('content-length')

                logger.info(
                    f"Provider connected: {response.status_code}, Content-Type: {response.headers.get('content-type')}")
                if provider_content_range:
                    logger.info(f"Provider Content-Range: {provider_content_range}")

                # Direct byte-for-byte proxy - NO buffering, NO transcoding
                async for chunk in response.aiter_bytes(chunk_size=32768):
                    # Check if streaming should be cancelled
                    if cancel_event.is_set():
                        logger.info(f"Streaming cancelled for client {client_id} by external request")
                        break
                    
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
                        logger.info(
                            f"First chunk delivered to client {client_id}: {len(chunk)} bytes")
                    elif chunk_count <= 10:
                        logger.info(
                            f"Chunk {chunk_count} delivered to client {client_id}: {len(chunk)} bytes")
                    elif chunk_count % 100 == 0:
                        logger.info(
                            f"Client {client_id}: {chunk_count} chunks, {bytes_served:,} bytes served")

                logger.info(
                    f"Stream completed for client {client_id}: {chunk_count} chunks, {bytes_served} bytes")

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
                logger.info(
                    f"ReadError for client {client_id}: {error_str} (bytes_served: {bytes_served}, chunk_count: {chunk_count})")

                if bytes_served == 0:
                    # No data was sent - provider likely rejected the request
                    logger.warning(
                        f"Provider closed connection immediately for {client_id} - possibly due to Range header on live stream")
                    await self._emit_event("STREAM_FAILED", stream_id, {
                        "client_id": client_id,
                        "error": "Provider closed connection immediately (may not support Range requests)",
                        "error_type": "ReadError",
                        "bytes_served": 0
                    })
                else:
                    # Some data was sent - likely client disconnection during streaming
                    logger.info(
                        f"Client {client_id} likely disconnected during streaming")
                    await self._emit_event("CLIENT_DISCONNECTED", stream_id, {
                        "client_id": client_id,
                        "bytes_served": bytes_served,
                        "chunks_served": chunk_count,
                        "reason": "read_error_during_stream"
                    })

            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPError) as e:
                logger.warning(
                    f"Stream error for client {client_id}: {type(e).__name__}: {e}")

                # Try seamless failover
                if stream_info.failover_urls:
                    logger.info(
                        f"Attempting seamless failover for client {client_id}")
                    new_response = await self._seamless_failover(stream_id, e)

                    if new_response:
                        # Continue streaming from failover URL
                        try:
                            async for chunk in new_response.aiter_bytes(chunk_size=32768):
                                yield chunk
                                bytes_served += len(chunk)
                                chunk_count += 1

                            logger.info(
                                f"Failover successful for client {client_id}")
                        except Exception as failover_error:
                            logger.error(
                                f"Failover stream also failed: {failover_error}")
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
                logger.info(
                    f"Client {client_id} disconnected: {type(e).__name__}")
                await self._emit_event("CLIENT_DISCONNECTED", stream_id, {
                    "client_id": client_id,
                    "bytes_served": bytes_served,
                    "chunks_served": chunk_count,
                    "reason": "client_disconnected"
                })

            except Exception as e:
                # Log with more detail for debugging
                error_str = str(e) if str(e) else f"<empty {type(e).__name__}>"
                logger.warning(
                    f"Stream error for client {client_id}: {type(e).__name__}: {error_str}")
                logger.warning(
                    f"Exception details - bytes_served: {bytes_served}, chunks: {chunk_count}")

                # Check if this looks like a client disconnection (empty error message often indicates this)
                if not str(e) and bytes_served > 0:
                    logger.info(
                        f"Likely client disconnection for {client_id} (empty error message, data was streaming)")
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
                        logger.info(
                            f"Provider connection closed for client {client_id}")
                    except Exception as close_error:
                        logger.warning(
                            f"Error closing response: {close_error}")

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

        # Create generator to start streaming
        gen = generate()
        
        # Consume first iteration to capture provider response headers
        # This is necessary to determine if we should return 206 or 200
        try:
            first_chunk = await gen.__anext__()
        except StopAsyncIteration:
            # Empty stream
            return StreamingResponse(iter([]), media_type=content_type, headers=headers)
        
        # Now we have provider_status_code, provider_content_range, provider_content_length
        # Determine proper response status and headers
        status_code = 200
        if range_header and provider_status_code == 206 and provider_content_range:
            # Provider returned 206, we should also return 206
            status_code = 206
            headers["Content-Range"] = provider_content_range
            logger.info(f"Returning 206 Partial Content with range: {provider_content_range}")
        
        if provider_content_length:
            headers["Content-Length"] = provider_content_length

        # Create new generator that yields the first chunk then continues with the rest
        async def generate_with_first_chunk():
            yield first_chunk
            async for chunk in gen:
                yield chunk

        return StreamingResponse(
            generate_with_first_chunk(), 
            status_code=status_code,
            media_type=content_type, 
            headers=headers
        )

    async def stream_transcoded(
        self,
        stream_id: str,
        client_id: str,
        range_header: Optional[str] = None
    ) -> StreamingResponse:
        """
        Stream transcoded content using the PooledStreamManager.
        """
        if not self.pooled_manager:
            raise HTTPException(
                status_code=501, detail="Transcoding pooling is not enabled")

        if stream_id not in self.streams:
            raise HTTPException(status_code=404, detail="Stream not found")

        stream_info = self.streams[stream_id]
        if not stream_info.is_transcoded:
            raise HTTPException(
                status_code=400, detail="Stream is not configured for transcoding")

        # Register client
        if client_id not in self.clients:
            await self.register_client(client_id, stream_id)

        # Create cancellation event for this client
        cancel_event = asyncio.Event()
        self.client_cancel_events[client_id] = cancel_event

        logger.info(
            f"Requesting pooled transcoded stream for client {client_id}, stream {stream_id}")

        async def generate():
            shared_process = None
            stream_key = None
            bytes_served = 0

            try:
                # Get or create a shared transcoding process
                stream_key, shared_process = await self.pooled_manager.get_or_create_shared_stream(
                    url=stream_info.current_url or stream_info.original_url,
                    profile=stream_info.transcode_profile,
                    ffmpeg_args=stream_info.transcode_ffmpeg_args,
                    client_id=client_id,
                    user_agent=stream_info.user_agent,
                    headers=stream_info.headers,
                )

                if not shared_process or not shared_process.process or not shared_process.process.stdout:
                    raise HTTPException(
                        status_code=500, detail="Failed to get a valid transcoding process")

                # Verify the process is actually running
                if shared_process.process.returncode is not None:
                    raise HTTPException(
                        status_code=500,
                        detail=f"Transcoding process has exited with code {shared_process.process.returncode}"
                    )

                logger.info(
                    f"Streaming from FFmpeg process PID {shared_process.process.pid} for client {client_id}")

                # Get the client's queue - the broadcaster will feed chunks into it
                client_queue = shared_process.client_queues.get(client_id)
                if not client_queue:
                    raise HTTPException(
                        status_code=500, detail="Client queue not found")

                # Stream data from the client's queue (fed by broadcaster)
                while True:
                    # Check if streaming should be cancelled
                    if cancel_event.is_set():
                        logger.info(f"Transcoded streaming cancelled for client {client_id} by external request")
                        break
                    
                    # Get chunk from queue (broadcaster puts chunks here) with timeout
                    # to allow checking cancellation event periodically
                    try:
                        chunk = await asyncio.wait_for(client_queue.get(), timeout=0.5)
                    except asyncio.TimeoutError:
                        # No chunk available, loop back to check cancellation
                        continue
                    
                    if chunk is None:  # None signals end of stream
                        logger.info(f"Transcoded streaming ended for client {client_id}")
                        break
                    
                    yield chunk
                    bytes_served += len(chunk)

                    # Update client activity
                    if self.pooled_manager:
                        self.pooled_manager.update_client_activity(client_id)
                    if client_id in self.clients:
                        self.clients[client_id].last_access = datetime.now()
                        self.clients[client_id].bytes_served += len(chunk)

                    # Update stream-level stats (for bandwidth tracking)
                    if stream_id in self.streams:
                        self.streams[stream_id].total_bytes_served += len(chunk)
                        self.streams[stream_id].last_access = datetime.now()

                    # Update global stats
                    self._stats.total_bytes_served += len(chunk)

            except Exception as e:
                logger.error(
                    f"Error during pooled transcoding for client {client_id}: {e}")
            finally:
                # Clean up: remove client from the shared stream
                if client_id and stream_key and self.pooled_manager:
                    await self.pooled_manager.remove_client_from_stream(client_id)

                # Final client cleanup
                await self.cleanup_client(client_id)
                logger.info(
                    f"Finished pooled stream for client {client_id}, served {bytes_served} bytes")

        headers = {
            "Content-Type": None,
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
            "Access-Control-Allow-Origin": "*",
        }
        # Determine content type from transcode args
        def _detect_content_type_from_ffmpeg_args(ffmpeg_args: List[str]) -> str:
            # Map common ffmpeg format names/extensions to Content-Type
            fmt_map = {
                'mp4': 'video/mp4',
                'mov': 'video/mp4',
                'matroska': 'video/x-matroska',
                'mkv': 'video/x-matroska',
                'webm': 'video/webm',
                'mpegts': 'video/mp2t',
                'mpeg': 'video/mpeg',
                'hls': 'application/vnd.apple.mpegurl',
                'hls_native': 'application/vnd.apple.mpegurl',
                'flv': 'video/x-flv',
                'ogg': 'video/ogg',
                'mp3': 'audio/mpeg'
            }

            try:
                args = ffmpeg_args or []
                # Look for explicit -f <format>
                fmt = None
                for i, a in enumerate(args):
                    if a == '-f' and i + 1 < len(args):
                        fmt = args[i + 1].lower()
                        break
                    # handle combined -fmatroska (rare)
                    if a.startswith('-f') and len(a) > 2:
                        fmt = a[2:].lower()
                        break

                # If we found a format token, map it
                if fmt:
                    if fmt in fmt_map:
                        return fmt_map[fmt]
                    # Some formats may include codec lists like "mov,mp4,m4a"
                    if ',' in fmt:
                        for part in fmt.split(','):
                            if part in fmt_map:
                                return fmt_map[part]

                # Fallback: inspect any output filenames in args for known extensions
                for a in args:
                    if isinstance(a, str):
                        la = a.lower()
                        if la.endswith('.mp4'):
                            return 'video/mp4'
                        if la.endswith('.mkv') or la.endswith('.mk3d'):
                            return 'video/x-matroska'
                        if la.endswith('.webm'):
                            return 'video/webm'
                        if la.endswith('.ts'):
                            return 'video/mp2t'
                        if la.endswith('.m3u8'):
                            return 'application/vnd.apple.mpegurl'

                # If output is a pipe (pipe:1) and no explicit fmt, assume streaming MPEG-TS
                joined = ' '.join(args).lower()
                if 'pipe:1' in joined or 'pipe:' in joined:
                    return 'video/mp2t'

            except Exception:
                pass

            return 'application/octet-stream'

        content_type = _detect_content_type_from_ffmpeg_args(stream_info.transcode_ffmpeg_args)

        # Set header content-type
        headers['Content-Type'] = content_type

        # Transcoded streams are live/progressive streams; disallow range requests to
        # avoid client players issuing range-based reconnects which can cause
        # duplicate client registrations and premature cleanup.
        headers['Accept-Ranges'] = 'none'

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
            logger.warning(
                f"No failover URLs available for stream {stream_id}")
            return None

        # Try next failover URL
        next_index = (stream_info.current_failover_index +
                      1) % len(stream_info.failover_urls)
        next_url = stream_info.failover_urls[next_index]

        logger.info(
            f"Attempting failover for stream {stream_id} to: {next_url}")

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
            logger.error(
                f"Failover attempt failed for stream {stream_id}: {e}")
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

        # If this stream is a transcoded HLS, try to get playlist from the pooled manager
        if stream_info.is_transcoded and self.pooled_manager:
            try:
                # Ensure a shared transcoding process exists for this stream (this will create one if necessary)
                stream_key, shared_process = await self.pooled_manager.get_or_create_shared_stream(
                    url=stream_info.current_url or stream_info.original_url,
                    profile=stream_info.transcode_profile or "",
                    ffmpeg_args=stream_info.transcode_ffmpeg_args or [],
                    client_id=client_id,
                    user_agent=stream_info.user_agent,
                    headers=stream_info.headers,
                )
                # Record the stream key for later mapping
                stream_info.transcode_stream_key = stream_key

                # If the shared process is HLS-mode, read its playlist file directly
                if hasattr(shared_process, 'mode') and getattr(shared_process, 'mode') == 'hls':
                    # Wait briefly for FFmpeg to produce the initial playlist if it's not yet present.
                    playlist_text = await shared_process.read_playlist()
                    waited = 0.0
                    poll_interval = 0.5
                    # Allow configurable wait time via settings.HLS_WAIT_TIME (seconds)
                    max_wait = float(getattr(settings, 'HLS_WAIT_TIME', 10))
                    while not playlist_text and waited < max_wait and shared_process.process and shared_process.process.returncode is None:
                        await asyncio.sleep(poll_interval)
                        waited += poll_interval
                        playlist_text = await shared_process.read_playlist()

                    # If playlist still not available after waiting, consider the transcoder failed
                    if not playlist_text:
                        logger.warning(f"HLS playlist not produced within {max_wait}s for stream {stream_id}; cleaning up transcoder")
                        try:
                            # Attempt to stop and remove the shared process
                            if self.pooled_manager:
                                await self.pooled_manager.force_stop_stream(stream_key)
                        except Exception as e:
                            logger.error(f"Error force-stopping failed HLS transcoder for {stream_key}: {e}")
                        # Return None so caller will treat playlist as unavailable
                        return None

                    if playlist_text:
                        # Construct pseudo-final URL based on local file path so M3U8Processor can compute bases
                        final_url = f"file://{shared_process.hls_dir}/index.m3u8"
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

                        parent_id = stream_id if not stream_info.is_variant_stream else stream_info.parent_stream_id
                        processor = M3U8Processor(
                            base_proxy_url, client_id, stream_info.user_agent, final_url, parent_stream_id=parent_id)
                        processed_content = processor.process_playlist(
                            playlist_text, base_proxy_url, base_url)

                        stream_info.last_access = datetime.now()
                        if client_id in self.clients:
                            self.clients[client_id].last_access = datetime.now()

                        return processed_content

            except ConnectionAbortedError:
                # Stream is being managed by another worker. Fall back to fetching via HTTP from that worker if possible.
                logger.debug("Transcoded HLS is managed by another worker; falling back to HTTP fetch of playlist if available")
            except Exception as e:
                logger.error(f"Error retrieving transcoded playlist from pooled manager: {e}")

        try:
            logger.info(f"Fetching HLS playlist from: {current_url}")
            headers = {'User-Agent': stream_info.user_agent}
            headers.update(stream_info.headers)
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
            processor = M3U8Processor(
                base_proxy_url, client_id, stream_info.user_agent, final_url, parent_stream_id=parent_id)
            processed_content = processor.process_playlist(
                content, base_proxy_url, base_url)

            stream_info.last_access = datetime.now()
            if client_id in self.clients:
                self.clients[client_id].last_access = datetime.now()

            return processed_content

        except Exception as e:
            logger.error(
                f"Error fetching playlist for stream {stream_id}: {e}")
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
        logger.info(
            f"Proxying HLS segment for stream {stream_id}, client {client_id}")

        async def segment_generator():
            bytes_served = 0
            try:
                headers = {}
                if range_header:
                    headers['Range'] = range_header

                # Get stream info for user agent and custom headers
                if stream_id in self.streams:
                    stream_info = self.streams[stream_id]
                    headers['User-Agent'] = stream_info.user_agent
                    headers.update(stream_info.headers)

                # If the segment is a local file generated by an HLS transcoder, read from disk
                if segment_url.startswith('file://') or os.path.exists(segment_url):
                    # Strip file:// prefix if present
                    path = segment_url[7:] if segment_url.startswith('file://') else segment_url
                    # Stream the file contents
                    with open(path, 'rb') as fh:
                        while True:
                            chunk = fh.read(32768)
                            if not chunk:
                                break
                            yield chunk
                            bytes_served += len(chunk)
                else:
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
                            logger.warning(
                                f"Stream {stream_id} failed health check, marking for failover")
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

        next_index = (stream_info.current_failover_index +
                      1) % len(stream_info.failover_urls)
        old_url = stream_info.current_url
        stream_info.current_url = stream_info.failover_urls[next_index]
        stream_info.current_failover_index = next_index

        logger.info(
            f"Updated failover URL for stream {stream_id}: {old_url} -> {stream_info.current_url}")
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
            time_diff_seconds = (
                current_time - stream_info.last_access).total_seconds()
            is_old = time_diff_seconds > self.stream_timeout

            if not has_active_clients and is_old:
                logger.info(
                    f"Marking stream {stream_id} for cleanup: no_active_clients={not has_active_clients}, time_diff={time_diff_seconds}s, timeout={self.stream_timeout}s")
                inactive_streams.append(stream_id)
            elif has_active_clients and is_old:
                logger.debug(
                    f"Stream {stream_id} is old ({time_diff_seconds}s) but has {active_client_count} active clients - keeping alive")
            elif not has_active_clients and not is_old:
                logger.debug(
                    f"Stream {stream_id} has no active clients but is recent ({time_diff_seconds}s < {self.stream_timeout}s) - keeping alive")

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
        non_variant_streams = [
            s for s in self.streams.values() if not s.is_variant_stream]

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
                        stream_stats_map[parent_id] = {
                            "bytes": 0, "segments": 0, "errors": 0, "clients": 0}

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
                    "metadata": stream.metadata,
                    "headers": stream.headers
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
