import asyncio
import httpx
import re
import logging
from typing import Dict, Optional, AsyncIterator, List, Set
from urllib.parse import urljoin, urlparse, quote, unquote
from datetime import datetime
from dataclasses import dataclass, field

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
    user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"


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
    def __init__(self, base_url: str):
        self.base_url = base_url

    async def process_playlist(self, content: str, original_base_url: str) -> str:
        """Process M3U8 playlist content, rewriting URLs appropriately"""
        lines = content.split('\n')
        processed_lines = []

        # Check if this is a master playlist (contains #EXT-X-STREAM-INF)
        is_master_playlist = any('#EXT-X-STREAM-INF' in line for line in lines)

        for line in lines:
            original_line = line
            line = line.strip()

            if line and not line.startswith('#'):
                # This is a direct URL line
                if line.startswith('http'):
                    full_url = line
                else:
                    # Relative URL - resolve it
                    full_url = urljoin(original_base_url, line)

                if is_master_playlist:
                    # This is a variant playlist URL - proxy it as another playlist
                    encoded_url = quote(full_url, safe='')
                    proxy_url = f"{self.base_url}/playlist.m3u8?url={encoded_url}"
                else:
                    # This is a media segment URL - proxy it as a segment
                    encoded_url = quote(full_url, safe='')
                    proxy_url = f"{self.base_url}/segment?url={encoded_url}"

                processed_lines.append(proxy_url)

            elif line.startswith('#EXT-X-MAP:'):
                # Process EXT-X-MAP URI
                uri_match = re.search(r'URI="([^"]+)"', line)
                if uri_match:
                    uri = uri_match.group(1)
                    if uri.startswith('http'):
                        full_url = uri
                    else:
                        full_url = urljoin(original_base_url, uri)

                    encoded_url = quote(full_url, safe='')
                    proxy_url = f"{self.base_url}/segment?url={encoded_url}"

                    # Replace the URI in the line
                    new_line = line.replace(
                        f'URI="{uri}"', f'URI="{proxy_url}"')
                    processed_lines.append(new_line)
                else:
                    processed_lines.append(original_line)

            else:
                processed_lines.append(original_line)

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
            headers={'User-Agent': 'M3U-Proxy/2.0'}
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

            self.streams[stream_id] = StreamInfo(
                stream_id=stream_id,
                original_url=stream_url,
                current_url=stream_url,
                created_at=now,
                last_access=now,
                failover_urls=failover_urls or [],
                user_agent=user_agent
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

        # Update client info
        client_info = self.clients[client_id]
        client_info.last_access = now
        client_info.stream_id = stream_id

        return client_info

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

            # Determine base URL for resolving relative URLs
            parsed_url = urlparse(current_url)
            base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
            if parsed_url.path:
                path_parts = parsed_url.path.rsplit('/', 1)
                if len(path_parts) > 1:
                    base_url += path_parts[0] + '/'
                else:
                    base_url += '/'
            else:
                base_url += '/'

            # Process the playlist
            processor = M3U8Processor(base_proxy_url)
            processed_content = await processor.process_playlist(content, base_url)

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
        range_header: Optional[str] = None
    ) -> AsyncIterator[bytes]:
        """Proxy a segment request directly"""
        try:
            logger.info(
                f"Client {client_id} requesting segment: {segment_url}")

            # Prepare headers with user agent from stream
            # default
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36'}

            # Get user agent from client's stream if available
            if client_id in self.clients:
                client_info = self.clients[client_id]
                if client_info.stream_id and client_info.stream_id in self.streams:
                    stream_info = self.streams[client_info.stream_id]
                    headers['User-Agent'] = stream_info.user_agent

            if range_header:
                headers['Range'] = range_header
                logger.info(f"Range request: {range_header}")

            bytes_served = 0
            async with self.http_client.stream('GET', segment_url, headers=headers) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    bytes_served += len(chunk)
                    yield chunk

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

    def get_stats(self) -> Dict:
        """Get comprehensive stats"""
        return {
            "proxy_stats": {
                "total_streams": self._stats.total_streams,
                "active_streams": self._stats.active_streams,
                "total_clients": self._stats.total_clients,
                "active_clients": self._stats.active_clients,
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
