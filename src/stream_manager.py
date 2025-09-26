import asyncio
import httpx
import re
import logging
from typing import Dict, Optional, AsyncIterator, List, Set
from urllib.parse import urljoin, urlparse, quote, unquote
from datetime import datetime
from dataclasses import dataclass, field
import weakref
from asyncio import Queue
import sys
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

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
    is_connected: bool = True  # Track actual connection state
    data_queue: Optional[Queue] = field(default_factory=lambda: Queue(maxsize=100))  # Buffer for stream data


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
    final_playlist_url: Optional[str] = None  # Track final URL after redirects
    user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    # Stream connection management
    origin_task: Optional[asyncio.Task] = None  # Task fetching from origin
    is_live_stream: bool = False  # Whether this is a continuous live stream
    connected_clients: Set[str] = field(default_factory=set)  # Active client IDs


@dataclass
class ProxyStats:
    total_streams: int = 0
    active_streams: int = 0
    total_clients: int = 0
    active_clients: int = 0
    total_bytes_served: int = 0
    total_segments_served: int = 0
    uptime_start: datetime = field(default_factory=datetime.now)


class M3U8Processor:
    def __init__(self, base_url: str, client_id: str, user_agent: Optional[str] = None, original_url: Optional[str] = None):
        self.base_url = base_url
        self.client_id = client_id
        self.user_agent = user_agent or 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36'
        self.original_url = original_url or base_url

    def process_playlist(self, content: str, base_proxy_url: str, original_base_url: Optional[str] = None) -> str:
        """Process M3U8 content and rewrite segment URLs"""
        lines = content.split('\n')
        processed_lines = []

        for line in lines:
            stripped_line = line.strip()
            if stripped_line and not stripped_line.startswith('#'):
                # This is a segment URL
                if not stripped_line.startswith('http'):
                    # Convert relative URL to absolute using original base URL
                    from urllib.parse import urljoin, urlparse
                    resolve_base = original_base_url or self.original_url
                    stripped_line = urljoin(resolve_base, stripped_line)
                
                # Encode the segment URL and add auth headers
                encoded_url = quote(stripped_line, safe='')
                segment_url = f"{base_proxy_url}/segment?url={encoded_url}&client_id={self.client_id}"
                
                # Add authentication headers to the segment URL based on original playlist domain
                from urllib.parse import urlparse
                original_parsed = urlparse(self.original_url)
                
                # Add headers as query parameters (following MediaFlow pattern)
                auth_headers = {
                    'referer': f"{original_parsed.scheme}://{original_parsed.netloc}/",
                    'origin': f"{original_parsed.scheme}://{original_parsed.netloc}",
                    'user-agent': self.user_agent,
                    'accept': '*/*',
                    'accept-encoding': 'gzip, deflate, br',
                    'accept-language': 'en-US,en;q=0.9'
                }
                
                # Add header parameters to segment URL
                for header_name, header_value in auth_headers.items():
                    encoded_header_value = quote(header_value, safe='')
                    segment_url += f"&h_{header_name}={encoded_header_value}"
                
                processed_lines.append(segment_url)
                logger.debug(f"Processed segment URL with auth headers: {stripped_line} -> {segment_url}")
            else:
                processed_lines.append(line)

        return '\n'.join(processed_lines)


class StreamManager:
    def __init__(self):
        self.streams: Dict[str, StreamInfo] = {}
        self.clients: Dict[str, ClientInfo] = {}
        # stream_id -> client_ids
        self.stream_clients: Dict[str, Set[str]] = {}
        self.client_timeout = 30  # seconds
        self.stream_timeout = 300  # 5 minutes
        self.http_client = httpx.AsyncClient(
            timeout=30.0,
            follow_redirects=True,  # Enable redirect following
            max_redirects=10        # Limit redirects to prevent loops
        )
        # Separate client for live streams with longer timeout
        self.live_stream_client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=300.0, write=10.0, pool=10.0),  # 5 min read timeout for live streams
            follow_redirects=True,
            max_redirects=10
        )
        self._stats = ProxyStats()
        self._cleanup_task: Optional[asyncio.Task] = None
        self._running = False
        self.event_manager = None  # Will be set by API

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
        logger.info("Stream manager started")

    async def stop(self):
        """Stop the stream manager"""
        self._running = False
        if self._cleanup_task:
            self._cleanup_task.cancel()
        await self.http_client.aclose()
        await self.live_stream_client.aclose()
        logger.info("Stream manager stopped")

    async def get_or_create_stream(
        self,
        stream_url: str,
        failover_urls: Optional[List[str]] = None,
        user_agent: Optional[str] = None
    ) -> str:
        """Get or create a stream and return its ID"""
        # Create a stream ID based on the URL
        import hashlib
        stream_id = hashlib.md5(stream_url.encode()).hexdigest()

        if stream_id not in self.streams:
            now = datetime.now()
            # Use provided user agent or default
            if user_agent is None:
                user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"

            # Detect if this is a live stream
            is_live = stream_url.endswith('.ts') or '/live/' in stream_url or stream_url.endswith('.m3u8')
            
            self.streams[stream_id] = StreamInfo(
                stream_id=stream_id,
                original_url=stream_url,
                current_url=stream_url,
                created_at=now,
                last_access=now,
                failover_urls=failover_urls or [],
                user_agent=user_agent,
                is_live_stream=is_live
            )
            self.stream_clients[stream_id] = set()
            self._stats.total_streams += 1
            self._stats.active_streams += 1
            logger.info(
                f"Created new stream: {stream_id} with user agent: {user_agent}")

        # Update last access
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

        # Add client to stream
        if stream_id in self.stream_clients:
            self.stream_clients[stream_id].add(client_id)
            self.streams[stream_id].client_count = len(
                self.stream_clients[stream_id])
            # Add to connected clients set
            self.streams[stream_id].connected_clients.add(client_id)

        # Update client info
        client_info = self.clients[client_id]
        client_info.last_access = now
        client_info.stream_id = stream_id
        client_info.is_connected = True

        return client_info

    async def start_live_stream(self, stream_id: str) -> bool:
        """Start the origin connection for a live stream"""
        if stream_id not in self.streams:
            return False
            
        stream_info = self.streams[stream_id]
        
        # Only start if it's a live stream and not already running
        if not stream_info.is_live_stream or stream_info.origin_task:
            return False
            
        logger.info(f"Starting live stream origin connection for {stream_id}")
        
        # Start the origin fetching task
        stream_info.origin_task = asyncio.create_task(
            self._fetch_live_stream(stream_id)
        )
        
        return True

    async def _fetch_live_stream(self, stream_id: str):
        """Fetch live stream from origin and distribute to clients"""
        if stream_id not in self.streams:
            return
            
        stream_info = self.streams[stream_id]
        current_url = stream_info.current_url or stream_info.original_url
        
        try:
            logger.info(f"Fetching live stream from: {current_url}")
            
            # Prepare headers
            headers = {
                'User-Agent': stream_info.user_agent,
                'Referer': f"{urlparse(current_url).scheme}://{urlparse(current_url).netloc}/",
                'Origin': f"{urlparse(current_url).scheme}://{urlparse(current_url).netloc}",
                'Accept': '*/*',
                'Connection': 'keep-alive'
            }
            
            # Use appropriate client
            is_live_stream = current_url.endswith('.ts') or '/live/' in current_url
            client_to_use = self.live_stream_client if is_live_stream else self.http_client
            
            async with client_to_use.stream('GET', current_url, headers=headers, follow_redirects=True) as response:
                response.raise_for_status()
                
                logger.info(f"Connected to live stream: {response.status_code}, Content-Type: {response.headers.get('content-type')}")
                
                chunk_count = 0
                bytes_served = 0
                
                async for chunk in response.aiter_bytes(chunk_size=32768):
                    if not stream_info.is_active or not stream_info.connected_clients:
                        logger.info(f"Stopping live stream {stream_id} - no active clients")
                        break
                        
                    bytes_served += len(chunk)
                    chunk_count += 1
                    
                    if chunk_count == 1:
                        logger.info(f"Live stream {stream_id} first chunk: {len(chunk)} bytes")
                    elif chunk_count % 100 == 0:
                        logger.debug(f"Live stream {stream_id} chunk {chunk_count}, total: {bytes_served} bytes, clients: {len(stream_info.connected_clients)}")
                    
                    # Distribute chunk to all connected clients
                    disconnected_clients = []
                    for client_id in list(stream_info.connected_clients):
                        if client_id in self.clients:
                            client_info = self.clients[client_id]
                            try:
                                # Try to put chunk in client queue (non-blocking)
                                client_info.data_queue.put_nowait(chunk)
                                client_info.bytes_served += len(chunk)
                                client_info.last_access = datetime.now()
                            except asyncio.QueueFull:
                                # Client not consuming fast enough, disconnect them
                                logger.warning(f"Client {client_id} queue full, disconnecting")
                                disconnected_clients.append(client_id)
                        else:
                            disconnected_clients.append(client_id)
                    
                    # Clean up disconnected clients
                    for client_id in disconnected_clients:
                        stream_info.connected_clients.discard(client_id)
                        if client_id in self.clients:
                            self.clients[client_id].is_connected = False
                
                logger.info(f"Live stream {stream_id} completed: {chunk_count} chunks, {bytes_served} bytes")
                
        except Exception as e:
            logger.error(f"Error in live stream {stream_id}: {e}")
            stream_info.error_count += 1
        finally:
            # Clean up
            stream_info.origin_task = None
            for client_id in list(stream_info.connected_clients):
                if client_id in self.clients:
                    # Signal end of stream
                    try:
                        self.clients[client_id].data_queue.put_nowait(None)
                    except asyncio.QueueFull:
                        pass
            stream_info.connected_clients.clear()
            logger.info(f"Live stream {stream_id} origin connection closed")

    async def get_playlist_content(
        self,
        stream_id: str,
        client_id: str,
        base_proxy_url: str
    ) -> Optional[str]:
        """Get and process playlist content for a stream"""
        if stream_id not in self.streams:
            return None

        stream_info = self.streams[stream_id]
        current_url = stream_info.current_url or stream_info.original_url

        try:
            logger.info(f"Fetching playlist from: {current_url}")
            # Use stream-specific user agent
            headers = {'User-Agent': stream_info.user_agent}
            response = await self.http_client.get(current_url, headers=headers)
            response.raise_for_status()

            content = response.text
            logger.info(f"Received playlist content ({len(content)} chars)")

            # Use the final URL after redirects for authentication
            final_url = str(response.url)
            logger.info(f"Final playlist URL after redirects: {final_url}")
            
            # Store the final URL in stream info for segment authentication
            stream_info.final_playlist_url = final_url

            # Determine base URL for resolving relative URLs
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

            # Process the playlist - use final URL for authentication headers
            processor = M3U8Processor(base_proxy_url, client_id, stream_info.user_agent, final_url)
            processed_content = processor.process_playlist(content, base_proxy_url, base_url)

            # Update stats
            stream_info.last_access = datetime.now()
            if client_id in self.clients:
                self.clients[client_id].last_access = datetime.now()

            return processed_content

        except Exception as e:
            logger.error(
                f"Error fetching playlist for stream {stream_id}: {e}")

            # Try failover if available
            if await self._try_failover(stream_id):
                return await self.get_playlist_content(stream_id, client_id, base_proxy_url)

            stream_info.error_count += 1
            return None

    async def proxy_segment(
        self,
        segment_url: str,
        client_id: str,
        range_header: Optional[str] = None,
        additional_headers: Optional[dict] = None
    ) -> AsyncIterator[bytes]:
        """Proxy a segment request directly"""
        try:
            logger.info(
                f"Client {client_id} requesting segment: {segment_url}")

            # Prepare headers with proper authentication
            # Start with minimal headers - User-Agent will be set from stream info or additional headers
            headers = {}

            # Set default User-Agent as fallback
            headers['User-Agent'] = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36'

            # Add any additional headers passed from the API request (h_ query parameters take precedence over defaults)
            if additional_headers:
                headers.update(additional_headers)
                logger.info(f"Added additional headers: {additional_headers}")

            # Get headers from client's stream if available - stream user agent takes highest precedence
            if client_id in self.clients:
                client_info = self.clients[client_id]
                if client_info.stream_id and client_info.stream_id in self.streams:
                    stream_info = self.streams[client_info.stream_id]
                    # Stream's user agent takes precedence over all other user agents
                    headers['User-Agent'] = stream_info.user_agent
                    
                    # Add authentication headers based on final playlist URL (after redirects)
                    from urllib.parse import urlparse
                    # Use final playlist URL if available, otherwise fall back to original URL
                    auth_url = stream_info.final_playlist_url or stream_info.original_url
                    auth_parsed = urlparse(auth_url)
                    segment_parsed = urlparse(segment_url)
                    
                    # Set Referer to the actual playlist domain for authentication
                    headers['Referer'] = f"{auth_parsed.scheme}://{auth_parsed.netloc}/"
                    headers['Origin'] = f"{auth_parsed.scheme}://{auth_parsed.netloc}"
                    
                    # Add Accept header for TS segments
                    headers['Accept'] = '*/*'
                    headers['Accept-Encoding'] = 'gzip, deflate, br'
                    headers['Accept-Language'] = 'en-US,en;q=0.9'
                    headers['Connection'] = 'keep-alive'
                    
                    # Log authentication headers being used
                    logger.info(f"Using stream user agent: {stream_info.user_agent}")
                    logger.info(f"Using authentication URL: {auth_url}")
                    logger.info(f"Using authentication headers for segment request: Referer={headers['Referer']}, Origin={headers['Origin']}")

            if range_header:
                headers['Range'] = range_header
                logger.info(f"Range request: {range_header}")

            # Handle redirects manually for streaming
            current_url = segment_url
            max_redirects = 10
            redirect_count = 0
            bytes_served = 0
            
            # Use appropriate HTTP client based on URL/stream type
            # Live MPEG-TS streams (.ts URLs) need longer timeouts
            is_live_stream = segment_url.endswith('.ts') or '/live/' in segment_url
            client_to_use = self.live_stream_client if is_live_stream else self.http_client
            
            if is_live_stream:
                logger.debug("Using live stream client with extended timeouts")
            
            while redirect_count < max_redirects:
                try:
                    async with client_to_use.stream('GET', current_url, headers=headers, follow_redirects=False) as response:
                        # Handle redirects manually
                        if response.status_code in (301, 302, 303, 307, 308):
                            location = response.headers.get('location')
                            if not location:
                                logger.error(f"Redirect response without location header for {current_url}")
                                response.raise_for_status()
                                return
                                
                            # Handle relative redirects
                            if location.startswith('/'):
                                from urllib.parse import urlparse, urljoin
                                parsed_url = urlparse(current_url)
                                base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
                                current_url = urljoin(base_url, location)
                            elif not location.startswith('http'):
                                from urllib.parse import urljoin
                                current_url = urljoin(current_url, location)
                            else:
                                current_url = location
                                
                            redirect_count += 1
                            logger.info(f"Following redirect {redirect_count}/{max_redirects}: {segment_url} -> {current_url}")
                            continue
                        
                        # Not a redirect, process the response
                        logger.info(f"Streaming segment from final URL: {current_url} (after {redirect_count} redirects)")
                        logger.info(f"Response status: {response.status_code}, Content-Type: {response.headers.get('content-type', 'unknown')}")
                        
                        response.raise_for_status()
                        
                        # Log the content length if available
                        content_length = response.headers.get('content-length')
                        if content_length:
                            logger.info(f"Expected content length: {content_length} bytes")
                        else:
                            logger.info("Content length unknown (likely live stream)")
                        
                        # Use larger chunk size for MPEG-TS streams (188 bytes * 32 = 6016, rounded to 8KB)
                        # This improves performance for continuous streams
                        chunk_size = 32768  # 32KB chunks for better MPEG-TS performance
                        
                        chunk_count = 0
                        last_log_count = 0
                        async for chunk in response.aiter_bytes(chunk_size=chunk_size):
                            bytes_served += len(chunk)
                            chunk_count += 1
                            if chunk_count == 1:
                                logger.info(f"First chunk received: {len(chunk)} bytes (using {chunk_size} byte chunks)")
                            elif chunk_count - last_log_count >= 50:  # Log every 50 chunks instead of 100
                                logger.debug(f"Streaming chunk {chunk_count}, total bytes: {bytes_served}")
                                last_log_count = chunk_count
                            yield chunk
                            
                        logger.info(f"Finished streaming {chunk_count} chunks, {bytes_served} total bytes")
                        break  # Successfully streamed, exit the redirect loop
                        
                except Exception as e:
                    logger.error(f"Error during redirect {redirect_count} for {current_url}: {e}")
                    if redirect_count == 0:
                        # If first request fails, re-raise the error
                        raise
                    else:
                        # If a redirect fails, try to continue with what we have
                        break
            
            if redirect_count >= max_redirects:
                logger.error(f"Too many redirects ({max_redirects}) for segment: {segment_url}")
                raise Exception(f"Too many redirects for segment: {segment_url}")

            # Update client stats
            if client_id in self.clients:
                client_info = self.clients[client_id]
                client_info.bytes_served += bytes_served
                client_info.segments_served += 1
                client_info.last_access = datetime.now()

                # Update stream stats
                if client_info.stream_id and client_info.stream_id in self.streams:
                    stream_info = self.streams[client_info.stream_id]
                    stream_info.total_bytes_served += bytes_served
                    stream_info.total_segments_served += 1
                    stream_info.last_access = datetime.now()

            # Update global stats
            self._stats.total_bytes_served += bytes_served
            self._stats.total_segments_served += 1

        except Exception as e:
            logger.error(f"Error proxying segment for client {client_id}: {e}")
            raise

    async def _try_failover(self, stream_id: str) -> bool:
        """Try to failover to next available URL"""
        if stream_id not in self.streams:
            return False

        stream_info = self.streams[stream_id]

        if not stream_info.failover_urls:
            logger.warning(
                f"No failover URLs available for stream {stream_id}")
            return False

        # Try next failover URL
        next_index = (stream_info.current_failover_index +
                      1) % len(stream_info.failover_urls)
        next_url = stream_info.failover_urls[next_index]

        logger.info(
            f"Attempting failover for stream {stream_id} to: {next_url}")

        try:
            # Test the failover URL with stream's user agent
            headers = {'User-Agent': stream_info.user_agent}
            response = await self.http_client.head(next_url, headers=headers, timeout=10.0)
            response.raise_for_status()

            # Failover successful
            old_url = stream_info.current_url
            stream_info.current_url = next_url
            stream_info.current_failover_index = next_index
            logger.info(f"Failover successful for stream {stream_id}")

            # Emit failover event
            await self._emit_event(
                "FAILOVER_TRIGGERED",
                stream_id,
                {
                    "old_url": old_url,
                    "new_url": next_url,
                    "failover_index": next_index
                }
            )

            return True

        except Exception as e:
            logger.error(f"Failover failed for stream {stream_id}: {e}")
            return False

    async def cleanup_client(self, client_id: str):
        """Clean up a client"""
        if client_id in self.clients:
            client_info = self.clients[client_id]

            # Emit client disconnected event
            await self._emit_event(
                "CLIENT_DISCONNECTED",
                client_info.stream_id or "unknown",
                {
                    "client_id": client_id,
                    "bytes_served": client_info.bytes_served,
                    "segments_served": client_info.segments_served
                }
            )

            # Remove from stream
            if client_info.stream_id and client_info.stream_id in self.stream_clients:
                self.stream_clients[client_info.stream_id].discard(client_id)
                if client_info.stream_id in self.streams:
                    self.streams[client_info.stream_id].client_count = len(
                        self.stream_clients[client_info.stream_id]
                    )

            del self.clients[client_id]
            self._stats.active_clients -= 1
            logger.info(f"Cleaned up client: {client_id}")

    async def _periodic_cleanup(self):
        """Periodic cleanup of inactive clients and streams"""
        while self._running:
            try:
                await self._cleanup_inactive_clients()
                await self._cleanup_inactive_streams()
                await asyncio.sleep(30)  # Run every 30 seconds
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
            # Check if stream has any active clients
            has_active_clients = stream_id in self.stream_clients and len(
                self.stream_clients[stream_id]) > 0

            # Check if stream is old and unused
            is_old = (current_time -
                      stream_info.last_access).seconds > self.stream_timeout

            if not has_active_clients and is_old:
                inactive_streams.append(stream_id)

        for stream_id in inactive_streams:
            if stream_id in self.streams:
                del self.streams[stream_id]
                if stream_id in self.stream_clients:
                    del self.stream_clients[stream_id]
                self._stats.active_streams -= 1
                logger.info(f"Cleaned up inactive stream: {stream_id}")

    async def stream_to_client(self, stream_id: str, client_id: str):
        """Stream data from client queue for live streams"""
        if stream_id not in self.streams or client_id not in self.clients:
            logger.error(f"Stream {stream_id} or client {client_id} not found")
            raise HTTPException(status_code=404, detail="Stream or client not found")

        stream_info = self.streams[stream_id]
        client_info = self.clients[client_id]
        
        # For live streams, use queue-based streaming
        if stream_info.is_live_stream:
            async def generate():
                try:
                    while client_info.is_connected and stream_info.is_active:
                        try:
                            # Wait for data with timeout using asyncio.Queue's get method
                            chunk = await asyncio.wait_for(
                                client_info.data_queue.get(), 
                                timeout=30.0
                            )
                            
                            # None signals end of stream
                            if chunk is None:
                                logger.info(f"End of stream signal for client {client_id}")
                                break
                                
                            yield chunk
                            
                        except asyncio.TimeoutError:
                            logger.debug(f"Timeout waiting for data for client {client_id}")
                            # Check if stream is still active
                            if not stream_info.is_active:
                                break
                                
                        except Exception as e:
                            logger.error(f"Error streaming to client {client_id}: {e}")
                            break
                            
                except Exception as e:
                    logger.error(f"Error in stream generator for client {client_id}: {e}")
                finally:
                    # Mark client as disconnected
                    client_info.is_connected = False
                    stream_info.connected_clients.discard(client_id)
                    logger.info(f"Client {client_id} disconnected from stream {stream_id}")
                    
                    # For live streams, clean up client immediately instead of waiting for periodic cleanup
                    if stream_info.is_live_stream:
                        try:
                            await self.cleanup_client(client_id)
                        except Exception as e:
                            logger.error(f"Error during immediate client cleanup: {e}")
            
            return StreamingResponse(
                generate(),
                media_type="video/mp2t",
                headers={
                    "Content-Type": "video/mp2t",
                    "Cache-Control": "no-cache, no-store, must-revalidate",
                    "Pragma": "no-cache",
                    "Expires": "0",
                    "Transfer-Encoding": "chunked"
                }
            )
        else:
            # For non-live streams, fall back to direct proxying
            return await self.proxy_direct_stream(stream_id, client_id)

    async def proxy_direct_stream(self, stream_id: str, client_id: str = None):
        """Direct proxy for non-live streams"""
        if stream_id not in self.streams:
            logger.error(f"Stream {stream_id} not found")
            raise HTTPException(status_code=404, detail="Stream not found")

        stream_info = self.streams[stream_id]
        current_url = stream_info.current_url or stream_info.original_url
        
        try:
            # Prepare headers
            headers = {
                'User-Agent': stream_info.user_agent,
                'Referer': f"{urlparse(current_url).scheme}://{urlparse(current_url).netloc}/",
                'Origin': f"{urlparse(current_url).scheme}://{urlparse(current_url).netloc}",
                'Accept': '*/*',
                'Connection': 'keep-alive'
            }
            
            # Use appropriate client
            client_to_use = self.http_client
            
            async def generate():
                async with client_to_use.stream('GET', current_url, headers=headers, follow_redirects=True) as response:
                    response.raise_for_status()
                    
                    logger.info(f"Connected to stream: {response.status_code}, Content-Type: {response.headers.get('content-type')}")
                    
                    chunk_count = 0
                    bytes_served = 0
                    
                    async for chunk in response.aiter_bytes(chunk_size=32768):
                        bytes_served += len(chunk)
                        chunk_count += 1
                        
                        if chunk_count == 1:
                            logger.info(f"Stream {stream_id} first chunk: {len(chunk)} bytes")
                        elif chunk_count % 100 == 0:
                            logger.debug(f"Stream {stream_id} chunk {chunk_count}, total: {bytes_served} bytes")
                        
                        # Update stats
                        stream_info.total_bytes_served += len(chunk)
                        stream_info.last_access = datetime.now()
                        
                        if client_id and client_id in self.clients:
                            self.clients[client_id].bytes_served += len(chunk)
                            self.clients[client_id].last_access = datetime.now()
                        
                        yield chunk
                    
                    logger.info(f"Stream {stream_id} completed: {chunk_count} chunks, {bytes_served} bytes")
            
            return StreamingResponse(
                generate(),
                media_type="video/mp2t",
                headers={
                    "Content-Type": "video/mp2t",
                    "Cache-Control": "no-cache, no-store, must-revalidate",
                    "Pragma": "no-cache",
                    "Expires": "0",
                    "Transfer-Encoding": "chunked"
                }
            )

        except Exception as e:
            logger.error(f"Error proxying direct stream {current_url}: {e}")
            stream_info.error_count += 1
            raise HTTPException(status_code=500, detail=str(e))

    def get_stats(self) -> Dict:
        """Get comprehensive stats"""
        # Calculate active streams (streams with active clients)
        active_stream_count = sum(1 for stream in self.streams.values() 
                                if stream.client_count > 0 and stream.is_active)
        
        # Calculate active clients (clients that accessed content recently)  
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
