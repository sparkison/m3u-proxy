from fastapi import FastAPI, HTTPException, Query, Response, Request, Depends, Header
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import logging
import uuid
import hashlib
from urllib.parse import unquote, urlparse
from typing import Optional, List, Dict
from pydantic import BaseModel, field_validator, ValidationError
from datetime import datetime, timezone

from stream_manager import StreamManager
from events import EventManager
from models import StreamEvent, EventType, WebhookConfig
from config import settings, VERSION
from transcoding import get_profile_manager, TranscodingProfileManager
from redis_config import get_redis_config, should_use_pooling
from hwaccel import hw_accel

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


def detect_https_from_headers(request: Request) -> bool:
    """
    Universal HTTPS detection from reverse proxy headers.

    Works with all major reverse proxies:
    - NGINX, Caddy, Traefik, Apache (X-Forwarded-Proto)
    - Cloudflare, AWS ELB (X-Forwarded-Ssl)
    - Microsoft IIS, Azure (Front-End-Https)
    - RFC 7239 compliant proxies (Forwarded header)
    - NGINX Proxy Manager, Tailscale, Headscale, Netbird, etc.

    Returns True if HTTPS is detected, False otherwise.
    """
    # Debug logging (disabled by default - enable if needed for troubleshooting)
    # logger.debug("=" * 80)
    # logger.debug("🔍 DEBUG: ALL HEADERS RECEIVED BY m3u-proxy:")
    # for header_name, header_value in request.headers.items():
    #     logger.debug(f"  {header_name}: {header_value}")
    # logger.debug("=" * 80)

    # Check X-Forwarded-Proto (most common - NGINX, Caddy, Traefik, NPM)
    forwarded_proto = request.headers.get("x-forwarded-proto")
    if forwarded_proto and forwarded_proto.lower() == "https":
        logger.debug("✅ Detected HTTPS via X-Forwarded-Proto: https")
        return True

    # Check X-Forwarded-Scheme (NGINX Proxy Manager, some reverse proxies)
    # NPM sets this correctly even when X-Forwarded-Proto is wrong
    forwarded_scheme = request.headers.get("x-forwarded-scheme")
    if forwarded_scheme and forwarded_scheme.lower() == "https":
        logger.debug("✅ Detected HTTPS via X-Forwarded-Scheme: https")
        return True

    # Check X-Forwarded-Ssl (Cloudflare, some load balancers)
    forwarded_ssl = request.headers.get("x-forwarded-ssl")
    if forwarded_ssl == "on":
        logger.debug("✅ Detected HTTPS via X-Forwarded-Ssl: on")
        return True

    # Check Front-End-Https (Microsoft IIS, Azure)
    frontend_https = request.headers.get("front-end-https")
    if frontend_https == "on":
        logger.debug("✅ Detected HTTPS via Front-End-Https: on")
        return True

    # Check Forwarded header (RFC 7239 standard)
    forwarded = request.headers.get("forwarded")
    if forwarded and "proto=https" in forwarded.lower():
        logger.debug("✅ Detected HTTPS via Forwarded header (RFC 7239)")
        return True

    # Check X-Forwarded-Port (if 443, assume HTTPS)
    forwarded_port = request.headers.get("x-forwarded-port")
    if forwarded_port == "443":
        logger.debug("✅ Detected HTTPS via X-Forwarded-Port: 443")
        return True

    # No HTTPS detected
    logger.debug("No HTTPS headers detected - using HTTP")
    return False


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
    headers: Optional[Dict[str, str]] = None
    strict_live_ts: Optional[bool] = None  # Enable Strict Live TS Mode for this stream

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


class TranscodeCreateRequest(BaseModel):
    url: str
    failover_urls: Optional[List[str]] = None
    user_agent: Optional[str] = None
    metadata: Optional[dict] = None
    profile: Optional[str] = None  # Profile name or custom template
    profile_variables: Optional[Dict[str, str]] = None
    output_format: Optional[str] = None  # mp4, mkv, ts, etc.

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

    @field_validator('profile_variables')
    @classmethod
    def validate_profile_variables(cls, v):
        if v is not None:
            if not isinstance(v, dict):
                raise ValueError("profile_variables must be a dictionary")
            # Ensure all keys and values are strings
            return {str(k): str(val) for k, val in v.items()}
        return v


# Global stream manager and event manager
redis_config = get_redis_config()
redis_url = redis_config.get('redis_url') if should_use_pooling() else None
enable_pooling = should_use_pooling()

stream_manager = StreamManager(
    redis_url=redis_url, enable_pooling=enable_pooling)
event_manager = EventManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup and shutdown events"""
    # Startup
    logger.info("m3u-proxy starting up...")
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
    logger.info("m3u-proxy shutting down...")
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
    # Try to get IP from X-Forwarded-For header first (for proxied requests)
    # X-Forwarded-For can contain multiple IPs: "client, proxy1, proxy2"
    # We want the first one (the original client IP)
    ip_address = "unknown"
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        # Take the first IP in the chain (original client)
        ip_address = forwarded_for.split(",")[0].strip()
    elif request.client:
        # Fallback to direct connection IP
        ip_address = request.client.host
    
    return {
        "user_agent": request.headers.get("user-agent") or "unknown",
        "ip_address": ip_address
    }


async def verify_token(
    x_api_token: Optional[str] = Header(None, alias="X-API-Token"),
    api_token: Optional[str] = Query(
        None, description="API token (alternative to X-API-Token header)")
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
        "message": "m3u-proxy is running",
        "version": VERSION,
        "uptime": proxy_stats["uptime_seconds"],
        "stats": proxy_stats
    }


@app.get("/info", dependencies=[Depends(verify_token)])
async def get_info():
    """
    Get comprehensive information about the m3u-proxy server configuration and capabilities.
    Includes hardware acceleration status, Redis pooling, transcoding profiles, and other details.
    """
    redis_config = get_redis_config()
    use_pooling = should_use_pooling()
    profile_manager = get_profile_manager()

    # Build the info response
    info = {
        "version": VERSION,
        "hardware_acceleration": {
            "enabled": hw_accel.is_available(),
            "type": hw_accel.get_type(),
            "device": hw_accel.config.device if hw_accel.is_available() else None,
            "ffmpeg_args": hw_accel.config.ffmpeg_args if hw_accel.is_available() else []
        },
        "redis": {
            "enabled": settings.REDIS_ENABLED,
            "pooling_enabled": use_pooling,
            "host": redis_config.get("host") if settings.REDIS_ENABLED else None,
            "port": redis_config.get("port") if settings.REDIS_ENABLED else None,
            "db": redis_config.get("db") if settings.REDIS_ENABLED else None,
            "max_clients_per_stream": settings.MAX_CLIENTS_PER_SHARED_STREAM if use_pooling else None,
            "stream_timeout": settings.SHARED_STREAM_TIMEOUT if use_pooling else None,
            "sharing_strategy": settings.STREAM_SHARING_STRATEGY if use_pooling else None
        },
        "transcoding": {
            "enabled": True,
            "profiles": profile_manager.list_profiles()
        },
        "configuration": {
            "default_user_agent": settings.DEFAULT_USER_AGENT,
            "client_timeout": settings.CLIENT_TIMEOUT,
            "stream_timeout": settings.STREAM_TIMEOUT,
            "cleanup_interval": settings.CLEANUP_INTERVAL,
            "max_retries": settings.DEFAULT_MAX_RETRIES
        },
        "worker": {
            "worker_id": settings.WORKER_ID if settings.WORKER_ID else "single",
            "heartbeat_interval": settings.HEARTBEAT_INTERVAL,
            "multi_worker_mode": bool(settings.WORKER_ID)
        }
    }

    return info


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
            metadata=request.metadata,
            headers=request.headers,
            strict_live_ts=request.strict_live_ts
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


@app.post("/transcode", dependencies=[Depends(verify_token)])
async def create_transcode_stream(request: TranscodeCreateRequest):
    """Create a new transcoded stream with optional failover URLs and custom profile"""
    try:
        profile_manager = get_profile_manager()

        # Determine if profile is a predefined name or custom template
        profile_identifier = request.profile or "default"
        profile = None
        profile_name = profile_identifier

        # Check if it looks like custom FFmpeg args (starts with -)
        if profile_identifier.strip().startswith('-'):
            logger.info(
                f"Using custom profile template: {profile_identifier[:50]}...")
            profile = profile_manager.create_profile_from_template(
                name="custom",
                parameters=profile_identifier,
                description="Custom FFmpeg profile from API"
            )
            profile_name = "custom"
        else:
            # Try to get as predefined profile
            profile = profile_manager.get_profile(profile_identifier)

            # If not found, error with available profiles
            if not profile:
                available_profiles = ', '.join(
                    profile_manager.list_profiles().keys())
                raise HTTPException(
                    status_code=400,
                    detail=f"Profile '{profile_identifier}' not found. Available profiles: {available_profiles}"
                )

        # Prepare template variables by merging required vars with user's custom variables
        template_vars = {
            "input_url": request.url,
            "output_args": "pipe:1",  # Output to stdout for streaming
            "format": request.output_format or "mpegts",  # Default to MPEGTS for streaming
        }
        # Merge with user-provided variables (user vars take precedence for overrides)
        if request.profile_variables:
            template_vars.update(request.profile_variables)

        # Generate FFmpeg args from profile and variables
        ffmpeg_args = profile.render(template_vars)

        # Prepare comprehensive metadata including transcoding info
        transcoding_metadata = {
            "transcoding": "true",
            "profile": profile_name,
            "profile_variables": str(request.profile_variables or {}),
            "ffmpeg_args": " ".join(ffmpeg_args)
        }
        if request.metadata:
            transcoding_metadata.update(request.metadata)

        # Create the stream with transcoding parameters and complete metadata
        stream_id = await stream_manager.get_or_create_stream(
            request.url,
            request.failover_urls,
            request.user_agent,
            metadata=transcoding_metadata,
            is_transcoded=True,
            transcode_profile=profile_name,
            transcode_ffmpeg_args=ffmpeg_args
        )

        # Emit stream started event with transcoding info
        event = StreamEvent(
            event_type=EventType.STREAM_STARTED,
            stream_id=stream_id,
            data={
                "primary_url": request.url,
                "failover_urls": request.failover_urls or [],
                "user_agent": request.user_agent,
                "stream_type": "transcoded",
                "profile": profile_name,
                "profile_variables": request.profile_variables or {},
                "ffmpeg_args": ffmpeg_args
            }
        )
        await event_manager.emit_event(event)

        # Determine whether this transcoded profile produces HLS output
        def _detect_hls_from_args(args: List[str]) -> bool:
            try:
                lowered = [str(a).lower() for a in (args or [])]
                for i, a in enumerate(lowered):
                    if a == '-f' and i + 1 < len(lowered) and lowered[i + 1] == 'hls':
                        return True
                    if a.startswith('-f') and 'hls' in a:
                        return True
                    if a.startswith('-hls_time') or 'hls_time' in a:
                        return True
                    if a.endswith('.m3u8'):
                        return True
                # also check joined string as fallback
                if ' -f hls' in ' '.join(lowered) or '-hls_time' in ' '.join(lowered):
                    return True
            except Exception:
                pass
            return False

        is_hls_output = _detect_hls_from_args(ffmpeg_args)

        # Choose endpoint based on output type
        if is_hls_output:
            stream_endpoint = f"/hls/{stream_id}/playlist.m3u8"
            direct_url = stream_endpoint
            out_format = 'hls'
            message = "Transcoded stream created successfully (HLS output)"
        else:
            stream_endpoint = f"/stream/{stream_id}"
            direct_url = stream_endpoint
            out_format = template_vars.get("format", "mpegts")
            message = "Transcoded stream created successfully (direct MPEGTS pipe)"

        response = {
            "stream_id": stream_id,
            "primary_url": request.url,
            "failover_urls": request.failover_urls or [],
            "user_agent": request.user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
            "stream_type": "transcoded",
            "stream_endpoint": stream_endpoint,
            "playlist_url": stream_endpoint if is_hls_output else None,
            "direct_url": direct_url,
            "format": out_format,
            "profile": profile.name,
            "profile_variables": template_vars,  # Show the actual variables used
            "ffmpeg_args": ffmpeg_args,
            "message": message
        }

        # Include metadata in response if provided
        if request.metadata:
            response["metadata"] = request.metadata

        return response
    except HTTPException:
        raise  # Re-raise HTTP exceptions as-is
    except Exception as e:
        logger.error(f"Error creating transcoded stream: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/transcode/profiles", dependencies=[Depends(verify_token)])
async def list_transcode_profiles():
    """List available transcoding profiles"""
    try:
        profile_manager = get_profile_manager()
        profiles_dict = profile_manager.list_profiles()

        # Get individual profile objects for more details
        profile_details = []
        for name in profiles_dict.keys():
            profile = profile_manager.get_profile(name)
            if profile:
                profile_details.append({
                    "name": profile.name,
                    "description": profile.description or profile.name,
                    "parameters": profile.parameters
                })

        # Check hardware acceleration availability
        from hwaccel import HardwareAccelDetector, is_hwaccel_available
        hw_detector = HardwareAccelDetector()
        hw_available = is_hwaccel_available()
        hw_type = hw_detector.config.type if hw_detector.config else None

        return {
            "profiles": profile_details,
            "hardware_acceleration": {
                "available": hw_available,
                "type": hw_type
            },
            "default_variables": profile_manager.get_default_variables()
        }
    except Exception as e:
        logger.error(f"Error listing transcoding profiles: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/hls/{stream_id}/playlist.m3u8")
async def get_hls_playlist(
    request: Request,
    stream_id: str = Depends(resolve_stream_id),
    client_id: Optional[str] = Query(
        None, description="Client ID (auto-generated if not provided)")
):
    """Get HLS playlist for a stream (supports both direct proxy and transcoded HLS streams)"""
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

        # Build base URL for this stream using settings.PUBLIC_URL (optional) and settings.PORT
        # PUBLIC_URL may be an IP, domain, or include a scheme (http/https) and/or port.
        public_url = getattr(settings, 'PUBLIC_URL', None)
        port = getattr(settings, 'PORT', None) or 8085
        root_path = getattr(settings, 'ROOT_PATH', '')

        if public_url:
            # If PUBLIC_URL includes a scheme, respect it. Otherwise assume http.
            public_with_scheme = public_url if public_url.startswith(('http://', 'https://')) else f"http://{public_url}"
            parsed = urlparse(public_with_scheme)
            scheme = parsed.scheme or 'http'

            # ✅ UNIVERSAL FIX: Auto-detect HTTPS from reverse proxy headers
            # This works with ALL major reverse proxies without requiring user configuration
            https_detected = detect_https_from_headers(request)
            if https_detected:
                scheme = "https"

            # Preserve hostname and path. If PUBLIC_URL provided an explicit port, use it;
            # otherwise fall back to settings.PORT when available.
            host = parsed.hostname or ''
            url_port = parsed.port
            path = parsed.path or ''

            # If ROOT_PATH is already included in the PUBLIC_URL path, remove it to prevent duplication
            # We'll add it back later to ensure it's always present in the final URL
            if root_path and path.startswith(root_path):
                path = path[len(root_path):]

            # ✅ FIX: When HTTPS is detected via reverse proxy headers, don't add internal port
            # The reverse proxy handles the external port (443 for HTTPS, 80 for HTTP)
            # Only use the internal port (8085) when:
            # 1. PUBLIC_URL explicitly includes a port, OR
            # 2. No reverse proxy is detected (direct access)
            if url_port:
                # PUBLIC_URL explicitly includes a port - respect it
                netloc = f"{host}:{url_port}"
            elif https_detected:
                # HTTPS detected via reverse proxy - use hostname only (standard port 443)
                netloc = host
            elif port and port != 80:
                # HTTP direct access with non-standard port - include port
                netloc = f"{host}:{port}"
            else:
                # HTTP with standard port 80 or no port specified
                netloc = host

            # Combine scheme, netloc, and any path from PUBLIC_URL (preserve sub-paths)
            base = f"{scheme}://{netloc}{path.rstrip('/')}"

            # Add ROOT_PATH back to ensure segment URLs include the correct prefix for NGINX routing
            if root_path:
                base = f"{base}{root_path}"
        else:
            # Default to localhost with configured port (or 8085)
            base = f"http://localhost:{port}"
            # Add ROOT_PATH if configured
            if root_path:
                base = f"{base}{root_path}"

        base_proxy_url = f"{base.rstrip('/')}/hls/{stream_id}"

        # Get processed playlist content (works for both direct HLS and transcoded HLS)
        content = await stream_manager.get_playlist_content(stream_id, client_id, base_proxy_url)

        if content is None:
            raise HTTPException(
                status_code=503, detail="Playlist not available")

        # Check if this is a transcoded stream for logging purposes
        stream_info = stream_manager.streams.get(stream_id)
        stream_type = "transcoded HLS" if stream_info and stream_info.is_transcoded else "direct HLS"
        
        logger.info(
            f"Serving {stream_type} playlist to client {client_id} for stream {stream_id}")

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

        # Check if this is a transcoded stream
        if stream_info.is_transcoded:
            logger.info(
                f"Using transcoded stream for {stream_id} with profile: {stream_info.transcode_profile}")

            # For transcoded streams outputting to pipe:1 or other non-HLS formats,
            # use streamed transcoding path
            return await stream_manager.stream_transcoded(
                stream_id,
                client_id,
                range_header=range_header
            )
        else:
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
    """Handle HEAD requests for direct streams (needed for MP4 duration/seeking)
    
    In Strict Live TS Mode, this returns quickly without upstream hits for live TS streams.
    """
    try:
        # The stream_id is now validated by the resolve_stream_id dependency
        stream_info = stream_manager.streams[stream_id]
        stream_url = stream_info.current_url or stream_info.original_url

        # Determine content type
        content_type = get_content_type(stream_url)

        # Check for Range header
        range_header = request.headers.get('range')

        # Determine if strict mode is enabled (global or per-stream)
        strict_mode_enabled = settings.STRICT_LIVE_TS or stream_info.strict_live_ts

        # STRICT MODE OPTIMIZATION: For live TS streams in strict mode, return immediately
        # without hitting upstream to prevent redundant requests and connection issues
        if strict_mode_enabled and stream_info.is_live_continuous:
            logger.info(
                f"STRICT MODE: HEAD request for live TS stream {stream_id} - returning quick response without upstream hit")
            
            response_headers = {
                "Content-Type": content_type,
                "Accept-Ranges": "none",  # Live streams don't support ranges
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
                "Access-Control-Allow-Headers": "*",
                "Access-Control-Expose-Headers": "*",
                "Connection": "keep-alive"
            }
            
            # Do NOT include Content-Length for live streams
            return Response(
                content=None,
                status_code=200,  # Always 200 for live streams, never 206
                headers=response_headers
            )

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
            "timestamp": datetime.now(timezone.utc).isoformat()
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


@app.get("/streams/by-metadata", dependencies=[Depends(verify_token)])
async def get_streams_by_metadata(
    field: str = Query(..., description="Metadata field to filter by"),
    value: str = Query(..., description="Value to match"),
    active_only: bool = Query(
        True, description="Only return streams with active clients")
):
    """Get active streams filtered by any metadata field with real-time status"""
    matching_streams = []

    for stream_id, stream_info in stream_manager.streams.items():
        # Skip variant streams - they don't have independent client counts
        if stream_info.is_variant_stream:
            continue

        # Check metadata field match
        if stream_info.metadata.get(field) != value:
            continue

        # Count only ACTIVE clients, not all clients in the set
        active_client_count = 0
        if stream_id in stream_manager.stream_clients:
            for client_id in stream_manager.stream_clients[stream_id]:
                if (client_id in stream_manager.clients and
                        stream_manager.clients[client_id].is_connected):
                    active_client_count += 1

        # Check if stream has active clients (if requested)
        if active_only and active_client_count == 0:
            continue

        # Check if stream is still considered active
        if not stream_info.is_active:
            continue

        matching_streams.append({
            'stream_id': stream_id,
            'client_count': active_client_count,  # Use active count, not total count
            'metadata': stream_info.metadata,
            'last_access': stream_info.last_access.isoformat(),
            'is_active': stream_info.is_active,
            'url': stream_info.current_url or stream_info.original_url,
            'stream_type': 'Transcoding' if stream_info.metadata.get('transcoding') else ('HLS' if stream_info.is_hls else ('VOD' if stream_info.is_vod else 'Live Continuous'))
        })

    return {
        'filter': {'field': field, 'value': value},
        'active_only': active_only,
        'matching_streams': matching_streams,
        'total_matching': len(matching_streams),
        'total_clients': sum(stream['client_count'] for stream in matching_streams)
    }


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

        # Get only ACTIVE clients for this stream
        active_stream_clients = []
        if stream_id in stream_manager.stream_clients:
            for client_id in stream_manager.stream_clients[stream_id]:
                if (client_id in stream_manager.clients and
                        stream_manager.clients[client_id].is_connected):
                    # Find the client in stats
                    client_stats = next(
                        (c for c in stats["clients"]
                         if c["client_id"] == client_id),
                        None
                    )
                    if client_stats:
                        active_stream_clients.append(client_stats)

        return {
            "stream": stream_stats,
            "clients": active_stream_clients,
            "client_count": len(active_stream_clients)
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

        stream_info = stream_manager.streams[stream_id]

        # For transcoded streams, force stop the FFmpeg process immediately
        if stream_info.is_transcoded and stream_manager.pooled_manager:
            logger.info(f"Force stopping transcoded stream {stream_id}")
            # Get the stream key used by pooled manager
            # This is typically based on URL and profile
            from pooled_stream_manager import PooledStreamManager
            stream_key = stream_manager.pooled_manager._generate_stream_key(
                stream_info.current_url or stream_info.original_url,
                stream_info.transcode_profile or "default"
            )
            await stream_manager.pooled_manager.force_stop_stream(stream_key)

        # Get all clients for this stream and clean them up
        if stream_id in stream_manager.stream_clients:
            client_ids = list(stream_manager.stream_clients[stream_id])
            for client_id in client_ids:
                await stream_manager.cleanup_client(client_id)

        # Emit stream_stopped event before removing the stream
        await stream_manager._emit_event("STREAM_STOPPED", stream_id, {
            "reason": "manual_deletion",
            "was_transcoded": stream_info.is_transcoded
        })

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
    """Manually trigger failover for a stream - will seamlessly switch all active clients to the next failover URL"""
    try:
        if stream_id not in stream_manager.streams:
            raise HTTPException(status_code=404, detail="Stream not found")

        stream_info = stream_manager.streams[stream_id]
        
        if not stream_info.failover_urls:
            raise HTTPException(
                status_code=400, 
                detail="No failover URLs configured for this stream"
            )

        # Trigger failover which will:
        # 1. Update current_url to next failover URL
        # 2. Signal all active clients via failover_event
        # 3. For transcoded streams, restart FFmpeg with new URL
        # 4. Emit FAILOVER_TRIGGERED event
        success = await stream_manager._try_update_failover_url(stream_id, "manual")

        if success:
            return {
                "message": "Failover triggered successfully - all clients will seamlessly reconnect",
                "new_url": stream_info.current_url,
                "failover_index": stream_info.current_failover_index,
                "failover_attempts": stream_info.failover_attempts,
                "active_clients": len(stream_info.connected_clients),
                "stream_type": "Transcoded" if stream_info.is_transcoded else (
                    "HLS" if stream_info.is_hls else (
                        "VOD" if stream_info.is_vod else "Live Continuous"
                    )
                )
            }
        else:
            raise HTTPException(
                status_code=500,
                detail="Failover failed - no working failover URLs available"
            )
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
            "public_url": settings.PUBLIC_URL,
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
