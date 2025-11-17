from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional

# Application version
VERSION = "0.2.25"


class Settings(BaseSettings):
    """
    Application configuration loaded from environment variables.
    Utilizes pydantic-settings for robust validation and type-casting.
    """

    # Server Configuration
    HOST: str = "0.0.0.0"
    PORT: int = 8085
    # Default public URL for re-writing URLs (HLS and Transcoded streams only)
    PUBLIC_URL: Optional[str] = None
    LOG_LEVEL: str = "error"
    APP_DEBUG: bool = False
    RELOAD: bool = False
    DOCS_URL: str = "/docs"
    REDOC_URL: str = "/redoc"
    OPENAPI_URL: str = "/openapi.json"

    # Route Configuration
    ROOT_PATH: str = "/m3u-proxy"  # Default base path for integration with m3u-editor
    CLIENT_TIMEOUT: int = 10
    STREAM_TIMEOUT: int = 15
    SHARED_STREAM_TIMEOUT: int = 30
    CLEANUP_INTERVAL: int = 30
    # Grace period (seconds) to wait before cleaning up a shared FFmpeg process after
    # the last client disconnects. This helps avoid churn for brief reconnects.
    SHARED_STREAM_GRACE: int = 3
    STREAM_SHARING_STRATEGY: str = "url_profile"  # url_profile, url_only, disabled

    # Default stream properties (can be overridden per stream)
    DEFAULT_USER_AGENT: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    DEFAULT_CONNECTION_TIMEOUT: float = 10.0
    DEFAULT_READ_TIMEOUT: float = 30.0
    DEFAULT_MAX_RETRIES: int = 3
    DEFAULT_BACKOFF_FACTOR: float = 1.5
    DEFAULT_HEALTH_CHECK_INTERVAL: float = 300.0

    # Additional configuration from .env file
    DEFAULT_RETRY_ATTEMPTS: int = 3
    DEFAULT_RETRY_DELAY: int = 5
    TEMP_DIR: str = "/tmp/m3u-proxy-streams"
    LOG_FILE: str = "m3u-proxy.log"

    # Redis Configuration for pooling and multi-worker coordination
    REDIS_HOST: str = "localhost"
    REDIS_SERVER_PORT: int = 6379
    REDIS_DB: int = 0
    REDIS_ENABLED: bool = False
    ENABLE_TRANSCODING_POOLING: bool = True
    MAX_CLIENTS_PER_SHARED_STREAM: int = 10

    # Worker configuration
    WORKER_ID: Optional[str] = None
    HEARTBEAT_INTERVAL: int = 30  # seconds

    # Transcoding configuration
    # HLS garbage collection configuration
    HLS_GC_ENABLED: bool = True
    HLS_GC_INTERVAL: int = 600
    HLS_GC_AGE_THRESHOLD: int = 3600  # seconds (1 hour)
    # Optional base directory for HLS transcoding output. If unset, the
    # system temp dir (tempfile.gettempdir()) will be used. Set this to
    # a directory that all workers can access if running multiple workers.
    HLS_TEMP_DIR: Optional[str] = None
    # How long (seconds) to wait for FFmpeg to produce the initial HLS playlist
    # before considering the transcoder failed and cleaning it up.
    HLS_WAIT_TIME: int = 10

    # API Authentication
    API_TOKEN: Optional[str] = None

    # Strict Live TS Mode Configuration
    # Enable strict mode for live MPEG-TS streams to improve playback stability
    STRICT_LIVE_TS: bool = False
    # Pre-buffer size in bytes (256KB-512KB recommended for 0.5-1s of data)
    STRICT_LIVE_TS_PREBUFFER_SIZE: int = 262144  # 256 KB default
    # Circuit breaker timeout - if no data received for this many seconds, mark upstream as bad
    STRICT_LIVE_TS_CIRCUIT_BREAKER_TIMEOUT: int = 2
    # How long to mark a failed upstream as bad (seconds) before retrying
    STRICT_LIVE_TS_CIRCUIT_BREAKER_COOLDOWN: int = 60
    # Maximum chunk wait time in seconds for pre-buffer (to avoid infinite wait)
    STRICT_LIVE_TS_PREBUFFER_TIMEOUT: int = 10

    # Model configuration
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        env_prefix="",  # No prefix, read directly from .env
        extra="ignore"  # Ignore extra environment variables from container
    )


# Global settings instance
settings = Settings()
