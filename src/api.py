from fastapi import FastAPI, HTTPException, Query, Response, Request
from fastapi.responses import StreamingResponse
import logging
import uuid
import hashlib
from urllib.parse import unquote
from typing import Optional, List
from pydantic import BaseModel

from stream_manager import StreamManager
from events import EventManager
from models import StreamEvent, EventType, WebhookConfig

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

# Request models


class StreamCreateRequest(BaseModel):
    url: str
    failover_urls: Optional[List[str]] = None
    user_agent: Optional[str] = None


app = FastAPI(
    title="m3u-proxy",
    version="2.0.0",
    description="Advanced IPTV streaming proxy with client management, stats, and failover support"
)

# Global stream manager and event manager
stream_manager = StreamManager()
event_manager = EventManager()


@app.on_event("startup")
async def startup_event():
    logger.info("m3u-proxy Enhanced starting up...")
    await event_manager.start()

    # Connect event manager to stream manager
    stream_manager.set_event_manager(event_manager)

    await stream_manager.start()


@app.on_event("shutdown")
async def shutdown_event():
    logger.info("m3u-proxy Enhanced shutting down...")
    await stream_manager.stop()
    await event_manager.stop()


def get_client_info(request: Request):
    """Extract client information from request"""
    return {
        "user_agent": request.headers.get("user-agent"),
        "ip_address": request.client.host if request.client else None
    }


@app.get("/")
async def root():
    stats = stream_manager.get_stats()
    return {
        "message": "m3u-proxy Enhanced is running",
        "version": "2.0.0",
        "stats": stats["proxy_stats"]
    }


@app.post("/streams")
async def create_stream(request: StreamCreateRequest):
    """Create a new stream with optional failover URLs and custom user agent"""
    try:
        stream_id = await stream_manager.get_or_create_stream(
            request.url,
            request.failover_urls,
            request.user_agent
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

        return {
            "stream_id": stream_id,
            "primary_url": request.url,
            "failover_urls": request.failover_urls or [],
            "user_agent": request.user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
            "stream_type": stream_type,
            "stream_endpoint": stream_endpoint,
            "message": f"Stream created successfully ({stream_type})"
        }
    except Exception as e:
        logger.error(f"Error creating stream: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/hls/{stream_id}/playlist.m3u8")
async def get_hls_playlist(
    stream_id: str,
    request: Request,
    client_id: Optional[str] = Query(
        None, description="Client ID (auto-generated if not provided)"),
    url: Optional[str] = Query(
        None, description="Stream URL (for direct access)")
):
    """Get HLS playlist for a stream"""
    try:
        # If URL is provided, create/get stream first
        if url:
            decoded_url = unquote(url)
            stream_id = await stream_manager.get_or_create_stream(decoded_url)

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
        base_proxy_url = f"http://localhost:8001/hls/{stream_id}"

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

        # Determine content type from the segment URL
        content_type = get_content_type(segment_url)

        # Prepare response headers
        response_headers = {"content-type": content_type}
        if range_header:
            response_headers["accept-ranges"] = "bytes"

        # Stream the segment with additional headers
        return StreamingResponse(
            stream_manager.proxy_segment(
                segment_url, client_id, range_header, additional_headers),
            headers=response_headers,
            status_code=206 if range_header else 200
        )

    except Exception as e:
        logger.error(f"Error serving segment: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/stream/{stream_id}")
async def get_direct_stream(
    stream_id: str,
    request: Request,
    client_id: Optional[str] = Query(
        None, description="Client ID (auto-generated if not provided)"),
    url: Optional[str] = Query(
        None, description="Stream URL (for direct access)")
):
    """Serve direct streams (.ts, .mp4, .mkv, etc.) for IPTV"""
    try:
        # If URL is provided, create/get stream first
        if url:
            decoded_url = unquote(url)
            stream_id = await stream_manager.get_or_create_stream(decoded_url)

        # Get stream info first
        if stream_id not in stream_manager.streams:
            raise HTTPException(status_code=404, detail="Stream not found")

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

        # Prepare response headers
        response_headers = {
            "content-type": content_type,
            "X-Client-ID": client_id,
            "X-Stream-ID": stream_id,
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0"
        }
        if range_header:
            response_headers["accept-ranges"] = "bytes"

        logger.info(
            f"Serving direct stream to client {client_id} for stream {stream_id}")
        logger.info(f"Stream URL: {stream_url}")
        logger.info(f"Content-Type: {content_type}")

        # Stream the content
        return StreamingResponse(
            stream_manager.proxy_segment(stream_url, client_id, range_header),
            headers=response_headers,
            status_code=206 if range_header else 200
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error serving direct stream: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/hls/{stream_id}/clients/{client_id}")
async def disconnect_client(stream_id: str, client_id: str):
    """Disconnect a specific client"""
    try:
        await stream_manager.cleanup_client(client_id)
        return {"message": f"Client {client_id} disconnected"}
    except Exception as e:
        logger.error(f"Error disconnecting client {client_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/stats")
async def get_stats():
    """Get comprehensive proxy statistics"""
    try:
        return stream_manager.get_stats()
    except Exception as e:
        logger.error(f"Error getting stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/stats/streams")
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


@app.get("/stats/clients")
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


@app.get("/streams")
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


@app.get("/streams/{stream_id}")
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


@app.delete("/streams/{stream_id}")
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


@app.post("/streams/{stream_id}/failover")
async def trigger_failover(stream_id: str):
    """Manually trigger failover for a stream"""
    try:
        if stream_id not in stream_manager.streams:
            raise HTTPException(status_code=404, detail="Stream not found")

        success = await stream_manager._try_failover(stream_id)

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


@app.get("/health")
async def health_check():
    """Health check endpoint with detailed status"""
    try:
        stats = stream_manager.get_stats()
        return {
            "status": "healthy",
            "version": "2.0.0",
            "uptime_seconds": stats["proxy_stats"]["uptime_seconds"],
            "active_streams": stats["proxy_stats"]["active_streams"],
            "active_clients": stats["proxy_stats"]["active_clients"],
            "total_bytes_served": stats["proxy_stats"]["total_bytes_served"]
        }
    except Exception as e:
        logger.error(f"Error in health check: {e}")
        return {
            "status": "error",
            "error": str(e)
        }, 500


# Webhook Management Endpoints
@app.post("/webhooks")
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


@app.get("/webhooks")
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


@app.delete("/webhooks")
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


@app.post("/webhooks/test")
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


@app.on_event("startup")
async def setup_event_handlers():
    """Set up custom event handlers"""

    def log_event_handler(event: StreamEvent):
        """Simple event handler that logs all events"""
        logger.info(
            f"Event: {event.event_type} for stream {event.stream_id} at {event.timestamp}")

    # Add the handler to the event manager
    event_manager.add_handler(log_event_handler)
