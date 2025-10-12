from fastapi import FastAPI, HTTPException, Query, Response, Request, Depends, Header
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import logging
import uuid
import hashlib
from urllib.parse import unquote, urlparse
from typing import Optional, List
from pydantic import BaseModel, field_validator, ValidationError
from datetime import datetime

from stream_manager import StreamManager
from events import EventManager
from models import StreamEvent, EventType, WebhookConfig
from config import settings, VERSION

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def get_content_type(url: str) -> str:
    """Determine content type based on URL extension"""
    url_lower = url.lower()
    if url_lower.endswith('.ts'):
        return 'video/mp2t'
    elif url_lower.endswith('.m3u8'):
        return 'application/vnd.apple.mpegurl'
    elif url_lower.endswith('.mp4'):
        return 'video/mp4'
    elif url_lower.endswith('.mkv'):
        return 'video/x-matroska'
    elif url_lower.endswith('.webm'):
        return 'video/webm'
    elif url_lower.endswith('.avi'):
        return 'video/x-msvideo'
    else:
        return 'application/octet-stream'


def is_direct_stream(url: str) -> bool:
    """Check if URL is a direct stream (not HLS playlist)"""
    return url.lower().endswith(('.ts', '.mp4', '.mkv', '.webm', '.avi'))


def validate_url(url: str) -> str:
    """Validate URL format and security"""
    if not url or not url.strip():
        raise ValueError("URL cannot be empty")

    # Basic URL parsing validation
    try:
        parsed = urlparse(url)
    except Exception:
        raise ValueError("Invalid URL format")

    # Ensure scheme is http or https
    if parsed.scheme.lower() not in ['http', 'https']:
        raise ValueError("URL must use HTTP or HTTPS protocol")

    # Ensure there's a valid netloc (domain)
    if not parsed.netloc:
        raise ValueError("URL must have a valid domain")

    # Security checks
    if url.lower().startswith('javascript:'):
        raise ValueError("JavaScript URLs are not allowed")

    if url.lower().startswith('file:'):
        raise ValueError("File URLs are not allowed")

    # Additional security check for malicious URLs
    dangerous_patterns = ['<script', 'javascript:', 'data:', 'vbscript:']
    url_lower = url.lower()
    for pattern in dangerous_patterns:
        if pattern in url_lower:
            raise ValueError(f"URL contains dangerous pattern: {pattern}")

    return url


# Request models
class StreamCreateRequest(BaseModel):
    url: str
    failover_urls: Optional[List[str]] = None
    user_agent: Optional[str] = None
    metadata: Optional[dict] = None

    @field_validator('url')
    @classmethod
    def validate_primary_url(cls, v):
        return validate_url(v)

    @field_validator('failover_urls')
    @classmethod
    def validate_failover_urls(cls, v):
        if v is not None:
            return [validate_url(url) for url in v]
        return v

    @field_validator('metadata')
    @classmethod
    def validate_metadata(cls, v):
        if v is not None:
            # Ensure all keys and values are strings
            if not isinstance(v, dict):
                raise ValueError("metadata must be a dictionary")
            for key, value in v.items():
                if not isinstance(key, str):
                    raise ValueError(
                        f"metadata key must be string, got {type(key)}")
                if not isinstance(value, (str, int, float, bool)):
                    raise ValueError(
                        f"metadata value for '{key}' must be string, int, float, or bool")
            # Convert all values to strings for consistency
            return {str(k): str(v) for k, v in v.items()}
        return v


# Global stream manager and event manager
stream_manager = StreamManager()
event_manager = EventManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup and shutdown events"""
    # Startup
    logger.info("m3u-proxy Enhanced starting up...")
    await event_manager.start()

    # Connect event manager to stream manager
    stream_manager.set_event_manager(event_manager)

    await stream_manager.start()

    # Set up custom event handlers
    def log_event_handler(event: StreamEvent):
        """Simple event handler that logs all events"""
        logger.info(
            f"Event: {event.event_type} for stream {event.stream_id} at {event.timestamp}")

    # Add the handler to the event manager
    event_manager.add_handler(log_event_handler)

    yield

    # Shutdown
    logger.info("m3u-proxy Enhanced shutting down...")
    await stream_manager.stop()
    await event_manager.stop()


app = FastAPI(
    title="m3u-proxy",
    version=VERSION,
    description="Advanced IPTV streaming proxy with client management, stats, and failover support",
    lifespan=lifespan,
    root_path=settings.ROOT_PATH if hasattr(settings, 'ROOT_PATH') else "",
    docs_url=settings.DOCS_URL if hasattr(settings, 'DOCS_URL') else "/docs",
    redoc_url=settings.REDOC_URL if hasattr(
        settings, 'REDOC_URL') else "/redoc",
    openapi_url=settings.OPENAPI_URL if hasattr(
        settings, 'OPENAPI_URL') else "/openapi.json",
)

# Configure CORS to allow all origins for streaming compatibility
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all origins for maximum compatibility
    allow_credentials=True,
    allow_methods=["*"],  # Allow all methods (GET, POST, HEAD, OPTIONS, etc.)
    allow_headers=["*"],  # Allow all headers
    expose_headers=["*"],  # Expose all headers to the client
)


def get_client_info(request: Request):
    """Extract client information from request"""
    return {
        "user_agent": request.headers.get("user-agent") or "unknown",
        "ip_address": request.client.host if request.client else "unknown"
    }


async def verify_token(
    x_api_token: Optional[str] = Header(None, alias="X-API-Token"),
    api_token: Optional[str] = Query(None, description="API token (alternative to X-API-Token header)")
):
    """
    Verify API token if API_TOKEN is configured.
    Token can be provided via:
    - X-API-Token header (recommended)
    - api_token query parameter (for browser access or when headers are difficult)
    
    If API_TOKEN is not set in environment, authentication is disabled.
    """
    # If no API token is configured, skip authentication
    if not settings.API_TOKEN:
        return True
    
    # Check for token in either header or query parameter
    provided_token = x_api_token or api_token
    
    # If API token is configured, require it in the header or query
    if not provided_token:
        raise HTTPException(
            status_code=401,
            detail="API token required. Provide token via X-API-Token header or api_token query parameter.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Verify the token matches
    if provided_token != settings.API_TOKEN:
        raise HTTPException(
            status_code=403,
            detail="Invalid API token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    return True


@app.get("/", dependencies=[Depends(verify_token)])
async def root():
    stats = stream_manager.get_stats()
    proxy_stats = stats["proxy_stats"]
    return {
        "status": "running",
        "message": "m3u-proxy Enhanced is running",
        "version": VERSION,
        "uptime": proxy_stats["uptime_seconds"],
        "stats": proxy_stats
    }


async def resolve_stream_id(
    stream_id: str,
    url: Optional[str] = Query(
        None, description="Stream URL (for direct access, overrides stream_id in path)"),
    parent: Optional[str] = Query(
        None, description="Parent stream ID (for variant playlists)")
) -> str:
    """
    Dependency to get a stream_id. If a URL is provided in the query,
    it will be used to create/retrieve a stream, overriding the path stream_id.
    Also validates that the stream exists.

    If parent is provided, the created stream will be marked as a variant of the parent.
    """
    if url:
        try:
            decoded_url = unquote(url)
            validate_url(decoded_url)
            # If parent is provided, this is a variant stream
            parent_id = parent if parent else None
            return await stream_manager.get_or_create_stream(decoded_url, parent_stream_id=parent_id)
        except ValueError as e:
            raise HTTPException(
                status_code=400, detail=f"Invalid URL provided: {e}")
        except Exception as e:
            logger.error(f"Error creating stream from URL parameter: {e}")
            raise HTTPException(
                status_code=500, detail="Failed to process stream from URL")

    if stream_id not in stream_manager.streams:
        raise HTTPException(status_code=404, detail="Stream not found")

    return stream_id


@app.post("/streams", dependencies=[Depends(verify_token)])
async def create_stream(request: StreamCreateRequest):
    """Create a new stream with optional failover URLs, custom user agent, and metadata"""
    try:
        stream_id = await stream_manager.get_or_create_stream(
            request.url,
            request.failover_urls,
            request.user_agent,
            metadata=request.metadata
        )

        # Emit stream started event
        event = StreamEvent(
            event_type=EventType.STREAM_STARTED,
            stream_id=stream_id,
            data={
                "primary_url": request.url,
                "failover_urls": request.failover_urls or [],
                "user_agent": request.user_agent,
                "stream_type": "direct" if is_direct_stream(request.url) else "hls"
            }
        )
        await event_manager.emit_event(event)

        # Determine the appropriate endpoint based on stream type
        if is_direct_stream(request.url):
            stream_endpoint = f"/stream/{stream_id}"
            stream_type = "direct"
        else:
            stream_endpoint = f"/hls/{stream_id}/playlist.m3u8"
            stream_type = "hls"

        response = {
            "stream_id": stream_id,
            "primary_url": request.url,
            "failover_urls": request.failover_urls or [],
            "user_agent": request.user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
            "stream_type": stream_type,
            "stream_endpoint": stream_endpoint,
            "playlist_url": stream_endpoint,  # For test compatibility
            # For test compatibility
            "direct_url": f"/stream/{stream_id}" if stream_type == "direct" else stream_endpoint,
            "message": f"Stream created successfully ({stream_type})"
        }

        # Include metadata in response if provided
        if request.metadata:
            response["metadata"] = request.metadata

        return response
    except Exception as e:
        logger.error(f"Error creating stream: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/hls/{stream_id}/playlist.m3u8")
async def get_hls_playlist(
    request: Request,
    stream_id: str = Depends(resolve_stream_id),
    client_id: Optional[str] = Query(
        None, description="Client ID (auto-generated if not provided)")
):
    """Get HLS playlist for a stream"""
    try:
        # Generate or reuse client ID based on request characteristics
        # Use IP + User-Agent + Stream ID to create a consistent client ID
        if not client_id:
            client_info_data = get_client_info(request)
            client_hash = hashlib.md5(
                f"{client_info_data['ip_address']}-{client_info_data['user_agent']}-{stream_id}".encode()
            ).hexdigest()[:16]
            client_id = f"client_{client_hash}"

        # Only register client if not already registered for this stream
        if client_id not in stream_manager.clients or stream_manager.clients[client_id].stream_id != stream_id:
            client_info_data = get_client_info(request)
            client_info = await stream_manager.register_client(
                client_id,
                stream_id,
                user_agent=client_info_data["user_agent"],
                ip_address=client_info_data["ip_address"]
            )

            # Emit client connected event
            event = StreamEvent(
                event_type=EventType.CLIENT_CONNECTED,
                stream_id=stream_id,
                data={
                    "client_id": client_id,
                    "user_agent": client_info_data["user_agent"],
                    "ip_address": client_info_data["ip_address"]
                }
            )
            await event_manager.emit_event(event)
        else:
            logger.debug(
                f"Reusing existing client {client_id} for stream {stream_id}")

        # Build base URL for this stream
        base_proxy_url = f"http://localhost:8085/hls/{stream_id}"

        # Get processed playlist content
        content = await stream_manager.get_playlist_content(stream_id, client_id, base_proxy_url)

        if content is None:
            raise HTTPException(
                status_code=503, detail="Playlist not available")

        logger.info(
            f"Serving playlist to client {client_id} for stream {stream_id}")

        response = Response(
            content=content, media_type="application/vnd.apple.mpegurl")
        # Add client ID to response headers for tracking
        response.headers["X-Client-ID"] = client_id
        response.headers["X-Stream-ID"] = stream_id
        # Add CORS headers
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, HEAD, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "*"
        response.headers["Access-Control-Expose-Headers"] = "*"
        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error serving playlist: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/hls/{stream_id}/segment")
async def get_hls_segment(
    stream_id: str,
    request: Request,
    client_id: str = Query(..., description="Client ID"),
    url: str = Query(..., description="The segment URL to proxy")
):
    """Proxy HLS segment for a client"""
    try:
        # Decode the URL
        segment_url = unquote(url)

        # Get range header if present
        range_header = request.headers.get('range')

        # Extract additional headers from query parameters (h_ prefixed)
        additional_headers = {}
        for key, value in request.query_params.items():
            if key.startswith("h_"):
                header_name = key[2:].replace('_', '-').lower()
                # Special handling for common headers
                if header_name == 'user-agent':
                    header_name = 'User-Agent'
                elif header_name == 'referer':
                    header_name = 'Referer'
                elif header_name == 'origin':
                    header_name = 'Origin'
                elif header_name == 'accept':
                    header_name = 'Accept'
                elif header_name == 'accept-encoding':
                    header_name = 'Accept-Encoding'
                elif header_name == 'accept-language':
                    header_name = 'Accept-Language'

                additional_headers[header_name] = value
                logger.info(
                    f"Extracted header from query param: {header_name}={value}")

        # For HLS segments, we don't create separate streams for each segment URL
        # Instead, we use the parent HLS stream_id and handle segment fetching directly
        # This prevents creating many individual streams for each .ts segment

        logger.info(
            f"HLS segment request - Stream: {stream_id}, Client: {client_id}, URL: {segment_url}")

        # Register client for the parent HLS stream (not the segment)
        client_info_data = get_client_info(request)
        await stream_manager.register_client(
            client_id,
            stream_id,  # Use the parent HLS stream ID
            user_agent=client_info_data["user_agent"],
            ip_address=client_info_data["ip_address"]
        )

        # For HLS segments, we need to fetch the segment directly without creating a separate stream
        # Use a special segment proxy function that doesn't create a new stream
        try:
            response = await stream_manager.proxy_hls_segment(
                stream_id,  # Parent HLS stream
                client_id,
                segment_url,  # The actual segment URL to fetch
                range_header
            )
            return response
        except Exception as stream_error:
            logger.error(f"Stream response error: {stream_error}")
            # Fall back to error response
            return Response(
                content=b"Stream unavailable",
                status_code=503,
                headers={"Content-Type": "text/plain"}
            )

    except Exception as e:
        logger.error(f"Error serving segment: {e}")
        logger.error(f"Exception type: {type(e)}")
        logger.error(f"Exception args: {e.args}")
        # Ensure we have a string representation
        error_detail = str(e) if e else "Unknown error"
        raise HTTPException(status_code=500, detail=error_detail)


@app.get("/hls/{stream_id}/segment.ts")
async def get_hls_segment_ts(
    stream_id: str,
    request: Request,
    client_id: str = Query(..., description="Client ID"),
    url: str = Query(..., description="The segment URL to proxy")
):
    """Proxy HLS segment with .ts extension for better ffplay compatibility"""
    return await get_hls_segment(stream_id, request, client_id, url)


@app.get("/stream/{stream_id}")
async def get_direct_stream(
    request: Request,
    stream_id: str = Depends(resolve_stream_id),
    client_id: Optional[str] = Query(
        None, description="Client ID (auto-generated if not provided)")
):
    """Serve direct streams (.ts, .mp4, .mkv, etc.) for IPTV"""
    try:
        # The stream_id is now validated by the resolve_stream_id dependency
        stream_info = stream_manager.streams[stream_id]
        stream_url = stream_info.current_url or stream_info.original_url

        # Generate or reuse client ID based on request characteristics
        # Use IP + User-Agent + Stream ID to create a consistent client ID
        if not client_id:
            client_info_data = get_client_info(request)
            client_hash = hashlib.md5(
                f"{client_info_data['ip_address']}-{client_info_data['user_agent']}-{stream_id}".encode()
            ).hexdigest()[:16]
            client_id = f"client_{client_hash}"

        # Only register client if not already registered for this stream
        if client_id not in stream_manager.clients or stream_manager.clients[client_id].stream_id != stream_id:
            client_info_data = get_client_info(request)
            await stream_manager.register_client(
                client_id,
                stream_id,
                user_agent=client_info_data["user_agent"],
                ip_address=client_info_data["ip_address"]
            )
            logger.info(
                f"Registered client {client_id} for stream {stream_id}")
        else:
            logger.debug(
                f"Reusing existing client {client_id} for stream {stream_id}")

        # Determine content type
        content_type = get_content_type(stream_url)

        # Get range header if present
        range_header = request.headers.get('range')

        logger.info(
            f"Serving direct stream to client {client_id} for stream {stream_id}")
        logger.info(f"Stream URL: {stream_url}")
        logger.info(f"Content-Type: {content_type}")
        if range_header:
            logger.info(f"Range request: {range_header}")

        # Use direct proxy for continuous streams
        # This provides true byte-for-byte proxying with per-client connections
        return await stream_manager.stream_continuous_direct(
            stream_id,
            client_id,
            range_header=range_header
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error serving direct stream: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.head("/stream/{stream_id}")
async def head_direct_stream(
    request: Request,
    stream_id: str = Depends(resolve_stream_id),
    client_id: Optional[str] = Query(
        None, description="Client ID (auto-generated if not provided)")
):
    """Handle HEAD requests for direct streams (needed for MP4 duration/seeking)"""
    try:
        # The stream_id is now validated by the resolve_stream_id dependency
        stream_info = stream_manager.streams[stream_id]
        stream_url = stream_info.current_url or stream_info.original_url

        # Determine content type
        content_type = get_content_type(stream_url)

        # Check for Range header
        range_header = request.headers.get('range')

        # For HEAD requests, we need to make a HEAD request to the origin server
        # to get metadata like Content-Length for MP4 files
        headers = {
            'User-Agent': stream_info.user_agent or 'Mozilla/5.0 (compatible)',
            'Accept': '*/*',
        }

        # If this is a range request, add the Range header
        if range_header:
            headers['Range'] = range_header
            logger.info(
                f"HEAD range request for stream {stream_id}: {stream_url}, Range: {range_header}")
        else:
            logger.info(f"HEAD request for stream {stream_id}: {stream_url}")

        try:
            async with stream_manager.http_client.stream('HEAD', stream_url, headers=headers, follow_redirects=True) as response:
                response.raise_for_status()

                # Build response headers based on origin server response
                response_headers = {
                    "Content-Type": content_type,
                    "Accept-Ranges": "bytes",
                    "Cache-Control": "no-cache, no-store, must-revalidate",
                    "Pragma": "no-cache",
                    "Expires": "0",
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
                    "Access-Control-Allow-Headers": "*",
                    "Access-Control-Expose-Headers": "*"
                }

                # Determine status code
                status_code = 200
                if range_header and response.status_code == 206:
                    status_code = 206
                    # Forward range-related headers
                    if 'content-range' in response.headers:
                        response_headers["Content-Range"] = response.headers['content-range']

                # Forward important headers from origin
                if 'content-length' in response.headers:
                    response_headers["Content-Length"] = response.headers['content-length']
                    logger.info(
                        f"Content-Length for {stream_id}: {response.headers['content-length']}")

                if 'last-modified' in response.headers:
                    response_headers["Last-Modified"] = response.headers['last-modified']

                return Response(
                    content=None,
                    status_code=status_code,
                    headers=response_headers
                )

        except Exception as e:
            logger.warning(f"HEAD request failed for {stream_url}: {e}")
            # Return basic HEAD response even if origin HEAD fails
            return Response(
                content=None,
                status_code=200,
                headers={
                    "Content-Type": content_type,
                    "Accept-Ranges": "bytes",
                    "Cache-Control": "no-cache, no-store, must-revalidate",
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
                    "Access-Control-Allow-Headers": "*",
                    "Access-Control-Expose-Headers": "*"
                }
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error handling HEAD request: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/hls/{stream_id}/clients/{client_id}", dependencies=[Depends(verify_token)])
async def disconnect_client(stream_id: str, client_id: str):
    """Disconnect a specific client"""
    try:
        await stream_manager.cleanup_client(client_id)
        return {"message": f"Client {client_id} disconnected"}
    except Exception as e:
        logger.error(f"Error disconnecting client {client_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/stats", dependencies=[Depends(verify_token)])
async def get_stats():
    """Get comprehensive proxy statistics"""
    try:
        stats = stream_manager.get_stats()
        # Flatten the response for test compatibility
        result = stats["proxy_stats"].copy()
        result["streams"] = stats["streams"]
        result["clients"] = stats["clients"]
        return result
    except Exception as e:
        logger.error(f"Error getting stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/stats/detailed", dependencies=[Depends(verify_token)])
async def get_detailed_stats():
    """Get detailed statistics including performance and monitoring metrics"""
    try:
        stats = stream_manager.get_stats()
        return {
            "proxy_stats": stats["proxy_stats"],
            "connection_pool_stats": stats["proxy_stats"].get("connection_pool_stats", {}),
            "failover_stats": stats["proxy_stats"].get("failover_stats", {}),
            "performance_metrics": stats["proxy_stats"].get("performance_metrics", {}),
            "error_stats": stats["proxy_stats"].get("error_stats", {}),
            "stream_count": len(stats["streams"]),
            "client_count": len(stats["clients"])
        }
    except Exception as e:
        logger.error(f"Error getting detailed stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/stats/performance", dependencies=[Depends(verify_token)])
async def get_performance_stats():
    """Get performance-specific statistics"""
    try:
        stats = stream_manager.get_stats()
        proxy_stats = stats["proxy_stats"]
        return {
            "connection_pool": proxy_stats.get("connection_pool_stats", {}),
            "performance_metrics": proxy_stats.get("performance_metrics", {}),
            "failover_stats": proxy_stats.get("failover_stats", {}),
            "error_stats": proxy_stats.get("error_stats", {}),
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        logger.error(f"Error getting performance stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/stats/streams", dependencies=[Depends(verify_token)])
async def get_stream_stats():
    """Get stream-specific statistics"""
    try:
        stats = stream_manager.get_stats()
        return {
            "total_streams": len(stats["streams"]),
            "streams": stats["streams"]
        }
    except Exception as e:
        logger.error(f"Error getting stream stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/stats/clients", dependencies=[Depends(verify_token)])
async def get_client_stats():
    """Get client-specific statistics"""
    try:
        stats = stream_manager.get_stats()
        return {
            "total_clients": len(stats["clients"]),
            "clients": stats["clients"]
        }
    except Exception as e:
        logger.error(f"Error getting client stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/clients", dependencies=[Depends(verify_token)])
async def list_clients():
    """List all active clients (alias for /stats/clients)"""
    return await get_client_stats()


@app.get("/streams", dependencies=[Depends(verify_token)])
async def list_streams():
    """List all active streams"""
    try:
        stats = stream_manager.get_stats()
        return {
            "streams": stats["streams"],
            "total": len(stats["streams"])
        }
    except Exception as e:
        logger.error(f"Error listing streams: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/streams/{stream_id}", dependencies=[Depends(verify_token)])
async def get_stream_info(stream_id: str):
    """Get information about a specific stream"""
    try:
        if stream_id not in stream_manager.streams:
            raise HTTPException(status_code=404, detail="Stream not found")

        stats = stream_manager.get_stats()
        stream_stats = next(
            (s for s in stats["streams"] if s["stream_id"] == stream_id), None)

        if not stream_stats:
            raise HTTPException(status_code=404, detail="Stream not found")

        # Get clients for this stream
        stream_clients = [c for c in stats["clients"]
                          if c["stream_id"] == stream_id]

        return {
            "stream": stream_stats,
            "clients": stream_clients,
            "client_count": len(stream_clients)
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting stream info: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/streams/{stream_id}", dependencies=[Depends(verify_token)])
async def delete_stream(stream_id: str):
    """Delete a stream and disconnect all its clients"""
    try:
        if stream_id not in stream_manager.streams:
            raise HTTPException(status_code=404, detail="Stream not found")

        # Get all clients for this stream
        if stream_id in stream_manager.stream_clients:
            client_ids = list(stream_manager.stream_clients[stream_id])
            for client_id in client_ids:
                await stream_manager.cleanup_client(client_id)

        # Remove stream
        if stream_id in stream_manager.streams:
            del stream_manager.streams[stream_id]
        if stream_id in stream_manager.stream_clients:
            del stream_manager.stream_clients[stream_id]

        stream_manager._stats.active_streams -= 1

        return {"message": f"Stream {stream_id} deleted"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting stream: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/streams/{stream_id}/failover", dependencies=[Depends(verify_token)])
async def trigger_failover(stream_id: str):
    """Manually trigger failover for a stream"""
    try:
        if stream_id not in stream_manager.streams:
            raise HTTPException(status_code=404, detail="Stream not found")

        # Update to next failover URL
        success = await stream_manager._try_update_failover_url(stream_id)

        if success:
            stream_info = stream_manager.streams[stream_id]
            return {
                "message": "Failover successful",
                "new_url": stream_info.current_url,
                "failover_index": stream_info.current_failover_index
            }
        else:
            return {
                "message": "Failover failed - no working failover URLs available"
            }, 500
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error triggering failover: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health", dependencies=[Depends(verify_token)])
async def health_check():
    """Health check endpoint with detailed status"""
    try:
        stats = stream_manager.get_stats()
        proxy_stats = stats["proxy_stats"]
        return {
            "status": "healthy",
            "version": VERSION,
            "uptime_seconds": proxy_stats["uptime_seconds"],
            "active_streams": proxy_stats["active_streams"],
            "active_clients": proxy_stats["active_clients"],
            "total_bytes_served": proxy_stats["total_bytes_served"],
            "stats": proxy_stats
        }
    except Exception as e:
        logger.error(f"Error in health check: {e}")
        return {
            "status": "error",
            "error": str(e)
        }, 500


# Webhook Management Endpoints
@app.post("/webhooks", dependencies=[Depends(verify_token)])
async def add_webhook(webhook: WebhookConfig):
    """Add a new webhook configuration"""
    try:
        event_manager.add_webhook(webhook)
        return {
            "message": "Webhook added successfully",
            "webhook_url": str(webhook.url),
            "events": [event.value for event in webhook.events]
        }
    except Exception as e:
        logger.error(f"Error adding webhook: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/webhooks", dependencies=[Depends(verify_token)])
async def list_webhooks():
    """List all configured webhooks"""
    try:
        webhooks = [
            {
                "url": str(wh.url),
                "events": [event.value for event in wh.events],
                "timeout": wh.timeout,
                "retry_attempts": wh.retry_attempts
            }
            for wh in event_manager.webhooks
        ]
        return {"webhooks": webhooks}
    except Exception as e:
        logger.error(f"Error listing webhooks: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/webhooks", dependencies=[Depends(verify_token)])
async def remove_webhook(webhook_url: str = Query(..., description="Webhook URL to remove")):
    """Remove a webhook configuration"""
    try:
        removed = event_manager.remove_webhook(webhook_url)
        if removed:
            return {"message": f"Webhook {webhook_url} removed successfully"}
        else:
            raise HTTPException(status_code=404, detail="Webhook not found")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error removing webhook: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/webhooks/test", dependencies=[Depends(verify_token)])
async def test_webhook(webhook_url: str = Query(..., description="Webhook URL to test")):
    """Send a test event to a webhook"""
    try:
        # Create test event
        test_event = StreamEvent(
            event_type=EventType.STREAM_STARTED,
            stream_id="test_stream_123",
            data={
                "test": True,
                "message": "This is a test webhook event",
                "primary_url": "http://example.com/test.m3u8"
            }
        )

        # Find webhook and send test
        webhook_found = False
        for webhook in event_manager.webhooks:
            if str(webhook.url) == webhook_url:
                webhook_found = True
                await event_manager._send_webhook(webhook, test_event)
                break

        if not webhook_found:
            raise HTTPException(status_code=404, detail="Webhook not found")

        return {
            "message": f"Test event sent to {webhook_url}",
            "event_id": test_event.event_id
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error testing webhook: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Event Handler Examples
# Custom event handlers are now set up in the lifespan context manager above
