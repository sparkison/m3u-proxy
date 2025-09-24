from pydantic import BaseModel, Field, HttpUrl
from typing import List, Optional, Dict, Any
from enum import Enum
from datetime import datetime
import uuid

from .config import config


class StreamFormat(str, Enum):
    MPEG_TS = "mpeg-ts"
    HLS = "hls"
    MKV = "mkv"
    MP4 = "mp4"
    WEBM = "webm"
    AVI = "avi"


class StreamStatus(str, Enum):
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    FAILED = "failed"
    STOPPED = "stopped"


class EventType(str, Enum):
    STREAM_STARTED = "stream_started"
    STREAM_STOPPED = "stream_stopped"
    STREAM_FAILED = "stream_failed"
    CLIENT_CONNECTED = "client_connected"
    CLIENT_DISCONNECTED = "client_disconnected"
    FAILOVER_TRIGGERED = "failover_triggered"


class StreamConfig(BaseModel):
    stream_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    primary_url: HttpUrl
    failover_urls: List[HttpUrl] = Field(default_factory=list)
    format: Optional[StreamFormat] = None
    auto_detect_format: bool = True
    enable_hardware_acceleration: bool = Field(default_factory=lambda: config.enable_hardware_acceleration)
    buffer_size: int = Field(default_factory=lambda: config.default_buffer_size, ge=64 * 1024)
    timeout: int = Field(default_factory=lambda: config.default_timeout, ge=5)
    retry_attempts: int = Field(default_factory=lambda: config.default_retry_attempts, ge=1)
    retry_delay: int = Field(default_factory=lambda: config.default_retry_delay, ge=1)


class StreamInfo(BaseModel):
    stream_id: str
    status: StreamStatus
    current_url: Optional[HttpUrl] = None
    current_url_index: int = 0
    format: Optional[StreamFormat] = None
    duration: Optional[float] = None  # seconds for VOD content
    position: Optional[float] = None  # current position in seconds
    is_live: bool = True
    client_count: int = 0
    started_at: Optional[datetime] = None
    last_error: Optional[str] = None
    bitrate: Optional[int] = None
    resolution: Optional[str] = None
    fps: Optional[float] = None


class ClientInfo(BaseModel):
    client_id: str
    stream_id: str
    connected_at: datetime
    ip_address: str
    user_agent: Optional[str] = None
    bytes_sent: int = 0


class StreamEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    event_type: EventType
    stream_id: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    data: Dict[str, Any] = Field(default_factory=dict)


class WebhookConfig(BaseModel):
    url: HttpUrl
    events: List[EventType] = Field(default_factory=lambda: list(EventType))
    headers: Dict[str, str] = Field(default_factory=dict)
    timeout: int = Field(default=10, ge=1)
    retry_attempts: int = Field(default=3, ge=0)


class StreamSeekRequest(BaseModel):
    position: float = Field(ge=0)  # seconds


class ProxyStats(BaseModel):
    total_streams: int
    active_streams: int
    total_clients: int
    uptime: float  # seconds
    cpu_usage: float
    memory_usage: float
    network_in: int  # bytes
    network_out: int  # bytes


class HealthCheck(BaseModel):
    status: str
    version: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    dependencies: Dict[str, str] = Field(default_factory=dict)
