from fastapi import FastAPI, HTTPException, Request, BackgroundTasks, Depends
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import asyncio
import logging
import psutil
import time
from datetime import datetime
import uuid
from typing import List, Optional
import os

from .models import (
    StreamConfig, StreamInfo, StreamSeekRequest, WebhookConfig, 
    ProxyStats, HealthCheck, EventType
)
from .stream_manager import StreamManager
from .events import EventManager
from .config import config


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Global managers
event_manager = EventManager()
stream_manager = StreamManager(event_manager)
start_time = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Starting M3U Proxy Server...")
    await event_manager.start()
    await stream_manager.start()
    logger.info("M3U Proxy Server started successfully")
    
    yield
    
    # Shutdown
    logger.info("Shutting down M3U Proxy Server...")
    await stream_manager.stop()
    await event_manager.stop()
    logger.info("M3U Proxy Server shut down complete")


app = FastAPI(
    title="M3U Streaming Proxy",
    description="High-performance streaming proxy with failover support and hardware acceleration",
    version="1.0.0",
    lifespan=lifespan
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


async def get_client_ip(request: Request) -> str:
    """Get client IP address from request."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@app.get("/health", response_model=HealthCheck)
async def health_check():
    """Health check endpoint."""
    return HealthCheck(
        status="healthy",
        version="1.0.0",
        dependencies={
            "event_manager": "running" if event_manager._running else "stopped",
            "stream_manager": "running" if stream_manager._running else "stopped"
        }
    )


@app.get("/stats", response_model=ProxyStats)
async def get_stats():
    """Get proxy statistics."""
    streams = await stream_manager.list_streams()
    active_streams = [s for s in streams if s.status.value == "running"]
    clients = await stream_manager.list_clients()
    
    process = psutil.Process()
    cpu_percent = process.cpu_percent()
    memory_info = process.memory_info()
    
    return ProxyStats(
        total_streams=len(streams),
        active_streams=len(active_streams),
        total_clients=len(clients),
        uptime=time.time() - start_time,
        cpu_usage=cpu_percent,
        memory_usage=memory_info.rss,
        network_in=0,  # TODO: Implement network stats tracking
        network_out=0
    )


@app.get("/hardware")
async def get_hardware_info():
    """Get hardware acceleration information."""
    hw_support = stream_manager._detect_hardware_acceleration()
    best_method = stream_manager._get_best_hw_acceleration()
    
    return {
        "hardware_acceleration_available": stream_manager._has_hardware_acceleration(),
        "best_method": best_method,
        "supported_methods": hw_support,
        "details": {
            "vaapi": "Intel/AMD hardware acceleration on Linux via VAAPI",
            "qsv": "Intel Quick Sync Video hardware acceleration", 
            "nvenc": "NVIDIA NVENC hardware acceleration",
            "cuda": "NVIDIA CUDA acceleration for filtering/scaling",
            "videotoolbox": "Apple VideoToolbox hardware acceleration (macOS)",
            "opencl": "OpenCL acceleration for filtering"
        }
    }


@app.post("/streams", response_model=StreamInfo)
async def create_stream(config: StreamConfig):
    """Create a new stream."""
    try:
        stream_info = await stream_manager.create_stream(config)
        return stream_info
    except Exception as e:
        logger.error(f"Failed to create stream: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/streams", response_model=List[StreamInfo])
async def list_streams():
    """List all streams."""
    return await stream_manager.list_streams()


@app.get("/streams/{stream_id}", response_model=StreamInfo)
async def get_stream(stream_id: str):
    """Get stream information."""
    stream_info = await stream_manager.get_stream_info(stream_id)
    if not stream_info:
        raise HTTPException(status_code=404, detail="Stream not found")
    return stream_info


@app.post("/streams/{stream_id}/start", response_model=StreamInfo)
async def start_stream(stream_id: str):
    """Start a stream."""
    try:
        stream_info = await stream_manager.start_stream(stream_id)
        return stream_info
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to start stream {stream_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/streams/{stream_id}/stop")
async def stop_stream(stream_id: str):
    """Stop a stream."""
    success = await stream_manager.stop_stream(stream_id)
    if not success:
        raise HTTPException(status_code=404, detail="Stream not found")
    return {"message": "Stream stopped successfully"}


@app.post("/streams/{stream_id}/seek")
async def seek_stream(stream_id: str, seek_request: StreamSeekRequest):
    """Seek to a position in a VOD stream."""
    success = await stream_manager.seek_stream(stream_id, seek_request.position)
    if not success:
        stream_info = await stream_manager.get_stream_info(stream_id)
        if not stream_info:
            raise HTTPException(status_code=404, detail="Stream not found")
        if stream_info.is_live:
            raise HTTPException(status_code=400, detail="Cannot seek in live streams")
        raise HTTPException(status_code=500, detail="Seek failed")
    
    return {"message": f"Seeking to position {seek_request.position}s"}


@app.get("/streams/{stream_id}/playlist.m3u8")
async def get_hls_playlist(stream_id: str, request: Request):
    """Get HLS playlist for a stream."""
    stream_info = await stream_manager.get_stream_info(stream_id)
    if not stream_info:
        raise HTTPException(status_code=404, detail="Stream not found")
    
    # Use IP + User-Agent as client identifier for HLS sessions
    client_ip = await get_client_ip(request)
    user_agent = request.headers.get("User-Agent", "")
    client_id = f"{client_ip}_{hash(user_agent)}"
    
    # Connect client only if not already connected
    await stream_manager.ensure_client_connected(stream_id, client_id, client_ip, user_agent)
    
    # Serve HLS playlist
    playlist_path = f"{config.temp_dir}/stream_{stream_id}/playlist.m3u8"
    
    if not os.path.exists(playlist_path):
        raise HTTPException(status_code=503, detail="Stream not ready")
    
    try:
        with open(playlist_path, 'r') as f:
            content = f.read()
        
        # Rewrite segment URLs to point to our API endpoints
        lines = content.split('\n')
        rewritten_lines = []
        
        for line in lines:
            # If it's a segment file (ends with .ts), rewrite the URL
            if line.strip().endswith('.ts'):
                segment_name = line.strip()
                # Rewrite to use our segments endpoint
                rewritten_lines.append(f"segments/{segment_name}")
            else:
                rewritten_lines.append(line)
        
        rewritten_content = '\n'.join(rewritten_lines)
        
        return StreamingResponse(
            iter([rewritten_content]),
            media_type="application/vnd.apple.mpegurl",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0"
            }
        )
    except Exception as e:
        logger.error(f"Error serving HLS playlist: {e}")
        raise HTTPException(status_code=500, detail="Error serving playlist")


@app.get("/streams/{stream_id}/segments/{segment_name}")
async def get_hls_segment(stream_id: str, segment_name: str, request: Request):
    """Get HLS segment for a stream."""
    stream_info = await stream_manager.get_stream_info(stream_id)
    if not stream_info:
        raise HTTPException(status_code=404, detail="Stream not found")
    
    # Update client activity (same logic as playlist endpoint)
    client_ip = await get_client_ip(request)
    user_agent = request.headers.get("User-Agent", "")
    client_id = f"{client_ip}_{hash(user_agent)}"
    
    # Ensure client is still connected and update activity
    await stream_manager.ensure_client_connected(stream_id, client_id, client_ip, user_agent)
    
    segment_path = f"{config.temp_dir}/stream_{stream_id}/{segment_name}"
    
    if not os.path.exists(segment_path):
        raise HTTPException(status_code=404, detail="Segment not found")
    
    try:
        def iterfile(file_path: str):
            with open(file_path, 'rb') as f:
                yield from f
        
        return StreamingResponse(
            iterfile(segment_path),
            media_type="video/mp2t",
            headers={
                "Cache-Control": "max-age=3600",
                "Accept-Ranges": "bytes"
            }
        )
    except Exception as e:
        logger.error(f"Error serving HLS segment: {e}")
        raise HTTPException(status_code=500, detail="Error serving segment")


@app.get("/streams/{stream_id}/stream")
async def get_stream_data(stream_id: str, request: Request):
    """Get direct stream data (MPEG-TS, etc.)."""
    stream_info = await stream_manager.get_stream_info(stream_id)
    if not stream_info:
        raise HTTPException(status_code=404, detail="Stream not found")
    
    # Connect client
    client_id = str(uuid.uuid4())
    client_ip = await get_client_ip(request)
    user_agent = request.headers.get("User-Agent")
    
    await stream_manager.connect_client(stream_id, client_id, client_ip, user_agent)
    
    async def stream_generator():
        try:
            # TODO: Implement actual streaming from FFmpeg process
            # This is a placeholder - in real implementation, you'd read from
            # the FFmpeg output pipe or a shared buffer
            while True:
                await asyncio.sleep(0.1)
                yield b"placeholder_stream_data"
        except Exception as e:
            logger.error(f"Error in stream generator: {e}")
        finally:
            await stream_manager.disconnect_client(stream_id, client_id)
    
    return StreamingResponse(
        stream_generator(),
        media_type="video/mp2t",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Connection": "keep-alive"
        }
    )


@app.post("/webhooks")
async def add_webhook(webhook: WebhookConfig):
    """Add a webhook configuration."""
    event_manager.add_webhook(webhook)
    return {"message": "Webhook added successfully"}


@app.delete("/webhooks")
async def remove_webhook(webhook_url: str):
    """Remove a webhook by URL."""
    success = event_manager.remove_webhook(webhook_url)
    if not success:
        raise HTTPException(status_code=404, detail="Webhook not found")
    return {"message": "Webhook removed successfully"}


@app.get("/webhooks")
async def list_webhooks():
    """List all configured webhooks."""
    return [
        {
            "url": str(wh.url),
            "events": wh.events,
            "timeout": wh.timeout,
            "retry_attempts": wh.retry_attempts
        }
        for wh in event_manager.webhooks
    ]


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "src.api:app",
        host="0.0.0.0",
        port=8080,
        reload=True,
        log_level="info"
    )
