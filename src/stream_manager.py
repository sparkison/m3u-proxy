import asyncio
import httpx
import re
import logging
from typing import Dict, Optional, AsyncIterator, List, Set
from urllib.parse import urljoin, urlparse, quote, unquote
from datetime import datetime, timedelta
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
    active_consumers: int = 0  # Count of actively consuming clients (HLS segments + TS streams)
    stream_buffer: Optional[Queue] = field(default_factory=lambda: Queue(maxsize=1000))  # Shared buffer for all clients


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

    async def add_stream_consumer(self, stream_id: str, client_id: str) -> bool:
        """Add a consumer to a stream and start provider connection if needed"""
        if stream_id not in self.streams:
            return False
            
        stream_info = self.streams[stream_id]
        stream_info.active_consumers += 1
        
        logger.info(f"Added consumer {client_id} to stream {stream_id} (total consumers: {stream_info.active_consumers})")
        
        # Start provider connection if this is the first consumer
        if stream_info.active_consumers == 1 and not stream_info.origin_task:
            logger.info(f"Starting provider connection for stream {stream_id} (first consumer)")
            stream_info.origin_task = asyncio.create_task(
                self._fetch_unified_stream(stream_id)
            )
            
        return True

    async def remove_stream_consumer(self, stream_id: str, client_id: str) -> bool:
        """Remove a consumer from a stream and stop provider connection if no consumers remain"""
        if stream_id not in self.streams:
            return False
            
        stream_info = self.streams[stream_id]
        if stream_info.active_consumers > 0:
            stream_info.active_consumers -= 1
            
        logger.info(f"Removed consumer {client_id} from stream {stream_id} (remaining consumers: {stream_info.active_consumers})")
        
        # Stop provider connection if no consumers remain
        if stream_info.active_consumers == 0 and stream_info.origin_task:
            logger.info(f"Stopping provider connection for stream {stream_id} (no consumers)")
            stream_info.origin_task.cancel()
            stream_info.origin_task = None
            
            # Clear the buffer
            try:
                while not stream_info.stream_buffer.empty():
                    stream_info.stream_buffer.get_nowait()
            except:
                pass
                
        return True

    async def _fetch_unified_stream(self, stream_id: str):
        """Unified stream fetcher that works for both HLS and TS streams"""
        if stream_id not in self.streams:
            return
            
        stream_info = self.streams[stream_id]
        current_url = stream_info.current_url or stream_info.original_url
        
        try:
            logger.info(f"Starting unified provider connection for stream {stream_id}: {current_url}")
            
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
                
                logger.info(f"Connected to provider stream {stream_id}: {response.status_code}, Content-Type: {response.headers.get('content-type')}")
                
                chunk_count = 0
                bytes_served = 0
                
                async for chunk in response.aiter_bytes(chunk_size=32768):
                    # Check if we still have consumers
                    if stream_info.active_consumers == 0:
                        logger.info(f"No consumers for stream {stream_id}, stopping provider connection")
                        break
                        
                    bytes_served += len(chunk)
                    chunk_count += 1
                    
                    if chunk_count == 1:
                        logger.info(f"Provider stream {stream_id} first chunk: {len(chunk)} bytes")
                    elif chunk_count % 100 == 0:
                        logger.debug(f"Provider stream {stream_id} chunk {chunk_count}, total: {bytes_served} bytes, consumers: {stream_info.active_consumers}")
                    
                    # Put chunk in shared buffer (non-blocking, drop if buffer full)
                    try:
                        stream_info.stream_buffer.put_nowait(chunk)
                    except asyncio.QueueFull:
                        # Buffer full, drop oldest chunk and add new one
                        try:
                            stream_info.stream_buffer.get_nowait()
                            stream_info.stream_buffer.put_nowait(chunk)
                        except asyncio.QueueEmpty:
                            pass
                    
                    # Update stream stats
                    stream_info.total_bytes_served += len(chunk)
                    stream_info.last_access = datetime.now()
                
                logger.info(f"Provider stream {stream_id} completed: {chunk_count} chunks, {bytes_served} bytes")
                
        except asyncio.CancelledError:
            logger.info(f"Provider stream {stream_id} cancelled (no consumers)")
        except Exception as e:
            logger.error(f"Error in provider stream {stream_id}: {e}")
            stream_info.error_count += 1
        finally:
            # Send end-of-stream signal to any waiting consumers
            try:
                stream_info.stream_buffer.put_nowait(None)
            except asyncio.QueueFull:
                # Clear buffer and send end signal
                try:
                    while not stream_info.stream_buffer.empty():
                        stream_info.stream_buffer.get_nowait()
                    stream_info.stream_buffer.put_nowait(None)
                except asyncio.QueueEmpty:
                    pass
            # Clean up
            stream_info.origin_task = None
            logger.info(f"Provider connection closed for stream {stream_id}")

    async def get_complete_segment_data(self, stream_id: str, client_id: str) -> Optional[bytes]:
        """Get complete segment data for HLS segments (waits for full download)"""
        if stream_id not in self.streams:
            return None
            
        stream_info = self.streams[stream_id]
        
        try:
            logger.debug(f"Waiting for segment data for stream {stream_id}")
            
            # Wait for provider connection to start and complete
            max_wait_for_start = 10.0
            start_time = asyncio.get_event_loop().time()
            
            # Wait for provider connection to start
            while not (hasattr(stream_info, 'origin_task') and stream_info.origin_task):
                if asyncio.get_event_loop().time() - start_time > max_wait_for_start:
                    logger.warning(f"Provider connection did not start for stream {stream_id}")
                    return None
                await asyncio.sleep(0.1)
            
            logger.debug(f"Provider connection started for stream {stream_id}, waiting for completion")
            
            # Wait for the provider task to complete (it will collect the entire segment)
            try:
                await asyncio.wait_for(stream_info.origin_task, timeout=30.0)
                logger.debug(f"Provider task completed for stream {stream_id}")
            except asyncio.TimeoutError:
                logger.warning(f"Timeout waiting for provider task completion for stream {stream_id}")
                return None
            except Exception as e:
                logger.warning(f"Provider task failed for stream {stream_id}: {e}")
                return None
            
            # Now collect all data from the buffer (it should be complete)
            segment_data = b""
            
            # Collect all available data from the buffer
            while True:
                try:
                    chunk = stream_info.stream_buffer.get_nowait()
                    if chunk is None:  # End of stream signal
                        break
                    segment_data += chunk
                except asyncio.QueueEmpty:
                    break
                    
            # Update client stats
            if client_id in self.clients and segment_data:
                client_info = self.clients[client_id]
                client_info.bytes_served += len(segment_data)
                client_info.last_access = datetime.now()
            
            logger.info(f"Completed segment collection for stream {stream_id}: {len(segment_data)} bytes")
            return segment_data if segment_data else None
                    
        except Exception as e:
            logger.error(f"Error getting complete segment data for client {client_id}: {e}")
            return None

    async def get_stream_data(self, stream_id: str, client_id: str, timeout: float = 30.0) -> Optional[bytes]:
        """Get stream data from shared buffer for any type of stream"""
        if stream_id not in self.streams:
            return None
            
        stream_info = self.streams[stream_id]
        
        try:
            # Wait for data from shared buffer
            chunk = await asyncio.wait_for(
                stream_info.stream_buffer.get(), 
                timeout=timeout
            )
            
            # None signals end of stream
            if chunk is None:
                return None
            
            # Update client stats
            if client_id in self.clients:
                client_info = self.clients[client_id]
                client_info.bytes_served += len(chunk)
                client_info.last_access = datetime.now()
            
            return chunk
            
        except asyncio.TimeoutError:
            logger.debug(f"Timeout waiting for stream data for client {client_id}")
            return None
        except Exception as e:
            logger.error(f"Error getting stream data for client {client_id}: {e}")
            return None

    async def _handle_range_request(self, stream_id: str, client_id: str, start: int, end: Optional[int] = None):
        """Handle HTTP range requests for VOD content by proxying directly to origin"""
        if stream_id not in self.streams:
            raise HTTPException(status_code=404, detail="Stream not found")
            
        stream_info = self.streams[stream_id]
        stream_url = stream_info.current_url or stream_info.original_url
        
        # Prepare headers for range request to origin
        headers = {
            'User-Agent': stream_info.user_agent or 'Mozilla/5.0 (compatible)',
            'Accept': '*/*',
            'Range': f'bytes={start}-{end if end is not None else ""}'
        }
        
        logger.info(f"Proxying range request to origin: {stream_url}, Range: bytes={start}-{end if end is not None else ''}")
        
        try:
            # Simple approach: make the range request and stream whatever we get back
            async def proxy_range_generator():
                bytes_served = 0
                try:
                    async with self.http_client.stream('GET', stream_url, headers=headers, follow_redirects=True) as response:
                        logger.info(f"Origin range response: {response.status_code}")
                        logger.info(f"Origin headers: Content-Length={response.headers.get('content-length')}, Content-Range={response.headers.get('content-range')}")
                        
                        # Stream whatever the origin server gives us
                        async for chunk in response.aiter_bytes(chunk_size=32 * 1024):
                            yield chunk
                            bytes_served += len(chunk)
                            
                            if bytes_served % (5 * 1024 * 1024) == 0:  # Log every 5MB
                                logger.info(f"Range proxy streamed {bytes_served} bytes")
                    
                    logger.info(f"Range proxy completed: {bytes_served} bytes total")
                    
                    # Update client's last access time for successful completion
                    if client_id in self.clients:
                        self.clients[client_id].last_access = datetime.now()
                
                except GeneratorExit:
                    logger.info(f"Range proxy generator stopped by client disconnect: {bytes_served} bytes served")
                    # For range requests, don't force cleanup - client might make more range requests
                    # Let the normal timeout-based cleanup handle inactive clients
                    return
                    
                except Exception as e:
                    logger.error(f"Error in range proxy generator: {e}")
                    # Only force cleanup on actual errors, not normal completion
                    if client_id in self.clients:
                        self.clients[client_id].last_access = datetime.now() - timedelta(minutes=10)  # Force cleanup on error
                    return
            
            # Make the actual request to get the real headers
            async with self.http_client.stream('GET', stream_url, headers=headers, follow_redirects=True) as test_response:
                # Get the actual response headers from the real request
                content_type = test_response.headers.get('content-type', 'video/mp4')
                content_length = test_response.headers.get('content-length')
                content_range = test_response.headers.get('content-range')
                
                logger.info(f"Test response: {test_response.status_code}, Content-Type: {content_type}")
                logger.info(f"Test headers: Content-Length={content_length}, Content-Range={content_range}")
                
                # Build response headers based on what the origin server returned
                response_headers = {
                    "Content-Type": content_type,
                    "Accept-Ranges": "bytes",
                    "Cache-Control": "no-cache"
                }
                
                # Copy headers from origin if available
                if content_length:
                    response_headers["Content-Length"] = content_length
                
                if content_range:
                    response_headers["Content-Range"] = content_range
                
                # If origin doesn't support range requests but we got data, simulate the range
                if test_response.status_code == 200 and not content_range:
                    # Calculate range headers manually
                    total_size = content_length
                    if total_size:
                        if end is not None:
                            range_length = str(end - start + 1)
                            end_pos = end
                        else:
                            range_length = str(int(total_size) - start)
                            end_pos = int(total_size) - 1
                        
                        response_headers["Content-Length"] = range_length
                        response_headers["Content-Range"] = f"bytes {start}-{end_pos}/{total_size}"
                
                # Determine status code
                status_code = 206 if (test_response.status_code == 206 or content_range or test_response.status_code == 200) else test_response.status_code
                
                logger.info(f"Range proxy response: {status_code}, headers: {response_headers}")
                
                return StreamingResponse(
                    proxy_range_generator(),
                    status_code=status_code,
                    headers=response_headers
                )
            
        except Exception as e:
            logger.error(f"Error proxying range request for {stream_id}: {e}")
            await self.remove_stream_consumer(stream_id, client_id)
            raise HTTPException(status_code=500, detail=f"Range proxy failed: {str(e)}")

    async def proxy_hls_segment(self, stream_id: str, client_id: str, segment_url: str, range_header: Optional[str] = None) -> StreamingResponse:
        """Proxy an individual HLS segment without creating a separate stream"""
        logger.info(f"Proxying HLS segment for stream {stream_id}, client {client_id}: {segment_url}")
        
        try:
            # Prepare headers for the segment request
            headers = {}
            if range_header:
                headers['Range'] = range_header
                
            # Fetch the segment directly
            async def segment_generator():
                bytes_served = 0
                try:
                    async with self.http_client.stream('GET', segment_url, headers=headers, follow_redirects=True) as response:
                        logger.info(f"HLS segment response: {response.status_code}")
                        
                        # Stream the segment data
                        async for chunk in response.aiter_bytes(chunk_size=32 * 1024):
                            yield chunk
                            bytes_served += len(chunk)
                    
                    logger.info(f"HLS segment completed: {bytes_served} bytes")
                    
                    # Update client's last access time
                    if client_id in self.clients:
                        self.clients[client_id].last_access = datetime.now()
                        self.clients[client_id].bytes_served += bytes_served
                        self.clients[client_id].segments_served += 1
                        
                except Exception as e:
                    logger.error(f"Error streaming HLS segment: {e}")
                    raise
            
            # Make a test request to get headers
            async with self.http_client.stream('GET', segment_url, headers=headers, follow_redirects=True) as test_response:
                content_type = test_response.headers.get('content-type', 'video/MP2T')
                content_length = test_response.headers.get('content-length')
                
                response_headers = {
                    "Content-Type": content_type,
                    "Cache-Control": "no-cache"
                }
                
                if content_length:
                    response_headers["Content-Length"] = content_length
                
                status_code = test_response.status_code
                
                return StreamingResponse(
                    segment_generator(),
                    status_code=status_code,
                    headers=response_headers
                )
                
        except Exception as e:
            logger.error(f"Error proxying HLS segment for {stream_id}: {e}")
            raise HTTPException(status_code=500, detail=f"HLS segment proxy failed: {str(e)}")

    async def stream_unified_response(self, stream_id: str, client_id: str, is_hls_segment: bool = False, range_header: Optional[str] = None) -> StreamingResponse:
        """Unified streaming response for both HLS segments and TS streams"""
        logger.info(f"Creating unified response: stream_id={stream_id}, client_id={client_id}, is_hls_segment={is_hls_segment}, range={range_header}")
        
        if stream_id not in self.streams:
            logger.error(f"Stream not found: {stream_id}")
            raise HTTPException(status_code=404, detail="Stream not found")

        stream_info = self.streams[stream_id]
        
        # Handle range requests for VOD content
        if range_header and not is_hls_segment:
            logger.info(f"Range request received for stream {stream_id}: {range_header}")
            # Parse range header (e.g., "bytes=2153050439-" or "bytes=0-1023")
            try:
                range_match = range_header.replace('bytes=', '').strip()
                if '-' in range_match:
                    start_str, end_str = range_match.split('-', 1)
                    start = int(start_str) if start_str else 0
                    end = int(end_str) if end_str else None
                    
                    logger.info(f"Parsed range request: start={start}, end={end}")
                    
                    # Add client as consumer even for range requests to track them properly
                    await self.add_stream_consumer(stream_id, client_id)
                    
                    # Return range response instead of full stream
                    return await self._handle_range_request(stream_id, client_id, start, end)
                else:
                    logger.warning(f"Invalid range header format: {range_header}")
            except Exception as e:
                logger.error(f"Error parsing range header {range_header}: {e}")
                # Fall through to normal streaming if range parsing fails
        
        # Add this client as a consumer
        consumer_added = await self.add_stream_consumer(stream_id, client_id)
        
        # Wait for provider connection to start for both HLS and continuous streams
        await asyncio.sleep(0.1)  # Give provider connection time to start
        
        # Check if provider connection failed immediately
        if hasattr(stream_info, 'origin_task') and stream_info.origin_task and stream_info.origin_task.done():
            try:
                # Check if the task completed with an exception
                stream_info.origin_task.result()
            except Exception as e:
                logger.error(f"Provider connection failed for stream {stream_id}: {e}")
                await self.remove_stream_consumer(stream_id, client_id)
                raise HTTPException(status_code=503, detail="Provider connection failed")
        
        # For continuous streams (non-HLS), wait for initial data to be available
        if not is_hls_segment:
            logger.info(f"Waiting for continuous stream data to start for {stream_id}")
            # Wait up to 5 seconds for the first chunk to arrive
            wait_start = asyncio.get_event_loop().time()
            max_wait = 5.0
            
            while asyncio.get_event_loop().time() - wait_start < max_wait:
                # Check if we have data in the buffer
                if not stream_info.stream_buffer.empty():
                    logger.info(f"Data available in buffer for stream {stream_id}")
                    break
                
                # Check if provider task completed successfully or failed
                if hasattr(stream_info, 'origin_task') and stream_info.origin_task and stream_info.origin_task.done():
                    try:
                        stream_info.origin_task.result()
                        logger.info(f"Provider task completed for {stream_id}")
                        # Even if completed, check if we got any data
                        if not stream_info.stream_buffer.empty():
                            break
                        else:
                            logger.warning(f"Provider task completed but no data received for {stream_id}")
                            await self.remove_stream_consumer(stream_id, client_id)
                            raise HTTPException(status_code=503, detail="Provider completed but no data received")
                    except Exception as e:
                        logger.error(f"Provider connection failed during wait for stream {stream_id}: {e}")
                        await self.remove_stream_consumer(stream_id, client_id)
                        raise HTTPException(status_code=503, detail=f"Provider connection failed: {e}")
                
                await asyncio.sleep(0.2)  # Check every 200ms
            
            # Final check - if still no data available after waiting
            if stream_info.stream_buffer.empty():
                logger.warning(f"No data available after {max_wait}s wait for stream {stream_id}")
                await self.remove_stream_consumer(stream_id, client_id)
                raise HTTPException(status_code=503, detail="No data available from provider after waiting")
            
            logger.info(f"Stream {stream_id} ready to start - data available in buffer")
        
        async def generate():
            try:
                if is_hls_segment:
                    # For HLS segments, wait for the complete segment data
                    segment_data = await self.get_complete_segment_data(stream_id, client_id)
                    
                    if segment_data and len(segment_data) > 0:
                        logger.info(f"Yielding HLS segment data: {len(segment_data)} bytes")
                        yield segment_data
                    else:
                        # No data available - provider may have failed
                        logger.warning(f"No segment data available for stream {stream_id}, client {client_id}")
                        yield b""
                else:
                    # For continuous streams, keep streaming until client disconnects
                    logger.info(f"Starting continuous stream for {stream_id}")
                    yielded_data = False
                    chunk_count = 0
                    while stream_info.active_consumers > 0:
                        chunk = await self.get_stream_data(stream_id, client_id, timeout=30.0)
                        if chunk is None:
                            logger.info(f"End of stream reached for {stream_id}")
                            break
                        yield chunk
                        yielded_data = True
                        chunk_count += 1
                        if chunk_count % 50 == 0:
                            logger.info(f"Streamed {chunk_count} chunks for {stream_id}")
                    
                    logger.info(f"Continuous stream ended for {stream_id}, yielded {chunk_count} chunks")
                    # Ensure we yield something for continuous streams too
                    if not yielded_data:
                        logger.warning(f"No data yielded for continuous stream {stream_id}")
                        yield b""
                        
            except Exception as e:
                logger.error(f"Error in unified stream generator for client {client_id}: {e}")
                yield b""
            finally:
                # Remove this client as a consumer
                await self.remove_stream_consumer(stream_id, client_id)
                # Clean up client
                if client_id in self.clients:
                    await self.cleanup_client(client_id)
                logger.info(f"Unified stream consumer {client_id} disconnected from stream {stream_id}")
        
        # Determine content type
        stream_url = stream_info.current_url or stream_info.original_url
        
        if stream_url.endswith('.ts') or '/live/' in stream_url:
            content_type = "video/mp2t"
        elif stream_url.endswith('.m3u8'):
            content_type = "application/vnd.apple.mpegurl"
        elif stream_url.endswith('.mp4'):
            content_type = "video/mp4"
        else:
            content_type = "application/octet-stream"
            
        logger.info(f"Starting stream response for {stream_id} with content-type: {content_type}")
        
        # Build headers, ensuring no None values
        headers = {
            "Content-Type": content_type,
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
            "Accept-Ranges": "bytes"  # Enable range request support
        }
        
        # Only add Transfer-Encoding if it's not an HLS segment
        if not is_hls_segment:
            headers["Transfer-Encoding"] = "chunked"
        
        try:
            response = StreamingResponse(
                generate(),
                media_type=content_type,
                headers=headers
            )
            logger.info(f"StreamingResponse created successfully for {stream_id}")
            return response
        except Exception as e:
            logger.error(f"Error creating StreamingResponse for {stream_id}: {e}")
            raise

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
