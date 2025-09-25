from fastapi import FastAPI, HTTPException, Query, Response, Request, Depends
from fastapi.responses import StreamingResponse, JSONResponse
import asyncio
import logging
import uuid
from urllib.parse import unquote
from typing import Optional, List
import json

from stream_manager import EnhancedStreamManager

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

app = FastAPI(
    title="M3U Proxy", 
    version="2.0.0",
    description="Advanced HLS streaming proxy with client management, stats, and failover support"
)

# Global stream manager
stream_manager = EnhancedStreamManager()

@app.on_event("startup")
async def startup_event():
    logger.info("M3U Proxy Enhanced starting up...")
    await stream_manager.start()

@app.on_event("shutdown")
async def shutdown_event():
    logger.info("M3U Proxy Enhanced shutting down...")
    await stream_manager.stop()

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
        "message": "M3U Proxy Enhanced is running", 
        "version": "2.0.0",
        "stats": stats["proxy_stats"]
    }

@app.post("/streams")
async def create_stream(
    url: str = Query(..., description="Stream URL (HLS .m3u8 or direct .ts/.mp4/.mkv)"),
    failover_urls: Optional[str] = Query(None, description="Comma-separated failover URLs"),
):
    """Create a new stream with optional failover URLs"""
    try:
        failover_list = []
        if failover_urls:
            failover_list = [u.strip() for u in failover_urls.split(',') if u.strip()]
        
        stream_id = await stream_manager.get_or_create_stream(url, failover_list)
        
        # Determine the appropriate endpoint based on stream type
        if is_direct_stream(url):
            stream_endpoint = f"/stream/{stream_id}"
            stream_type = "direct"
        else:
            stream_endpoint = f"/hls/{stream_id}/playlist.m3u8"
            stream_type = "hls"
        
        return {
            "stream_id": stream_id,
            "primary_url": url,
            "failover_urls": failover_list,
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
    client_id: Optional[str] = Query(None, description="Client ID (auto-generated if not provided)"),
    url: Optional[str] = Query(None, description="Stream URL (for direct access)")
):
    """Get HLS playlist for a stream"""
    try:
        # Generate client ID if not provided
        if not client_id:
            client_id = str(uuid.uuid4())
        
        # If URL is provided, create/get stream first
        if url:
            decoded_url = unquote(url)
            stream_id = await stream_manager.get_or_create_stream(decoded_url)
        
        # Register client
        client_info_data = get_client_info(request)
        await stream_manager.register_client(
            client_id, 
            stream_id,
            user_agent=client_info_data["user_agent"],
            ip_address=client_info_data["ip_address"]
        )
        
        # Build base URL for this stream
        base_proxy_url = f"http://localhost:8001/hls/{stream_id}"
        
        # Get processed playlist content
        content = await stream_manager.get_playlist_content(stream_id, client_id, base_proxy_url)
        
        if content is None:
            raise HTTPException(status_code=503, detail="Playlist not available")
        
        logger.info(f"Serving playlist to client {client_id} for stream {stream_id}")
        
        response = Response(content=content, media_type="application/vnd.apple.mpegurl")
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
        
        # Determine content type from the segment URL
        content_type = get_content_type(segment_url)
        
        # Prepare response headers
        response_headers = {"content-type": content_type}
        if range_header:
            response_headers["accept-ranges"] = "bytes"
        
        # Stream the segment
        return StreamingResponse(
            stream_manager.proxy_segment(segment_url, client_id, range_header),
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
    client_id: Optional[str] = Query(None, description="Client ID (auto-generated if not provided)"),
    url: Optional[str] = Query(None, description="Stream URL (for direct access)")
):
    """Serve direct streams (.ts, .mp4, .mkv, etc.) for IPTV"""
    try:
        # Generate client ID if not provided
        if not client_id:
            client_id = str(uuid.uuid4())
        
        # If URL is provided, create/get stream first
        if url:
            decoded_url = unquote(url)
            stream_id = await stream_manager.get_or_create_stream(decoded_url)
        
        # Register client
        client_info_data = get_client_info(request)
        await stream_manager.register_client(
            client_id, 
            stream_id,
            user_agent=client_info_data["user_agent"],
            ip_address=client_info_data["ip_address"]
        )
        
        # Get stream info
        if stream_id not in stream_manager.streams:
            raise HTTPException(status_code=404, detail="Stream not found")
        
        stream_info = stream_manager.streams[stream_id]
        stream_url = stream_info.current_url or stream_info.original_url
        
        # Determine content type
        content_type = get_content_type(stream_url)
        
        # Get range header if present
        range_header = request.headers.get('range')
        
        # Prepare response headers
        response_headers = {
            "content-type": content_type,
            "X-Client-ID": client_id,
            "X-Stream-ID": stream_id
        }
        if range_header:
            response_headers["accept-ranges"] = "bytes"
        
        logger.info(f"Serving direct stream to client {client_id} for stream {stream_id}")
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
        stream_stats = next((s for s in stats["streams"] if s["stream_id"] == stream_id), None)
        
        if not stream_stats:
            raise HTTPException(status_code=404, detail="Stream not found")
        
        # Get clients for this stream
        stream_clients = [c for c in stats["clients"] if c["stream_id"] == stream_id]
        
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
