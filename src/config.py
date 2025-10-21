from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional

# Application version
VERSION = "0.2.14"


class Settings(BaseSettings):
    """
    Application configuration loaded from environment variables.
    Utilizes pydantic-settings for robust validation and type-casting.
    """

    # Server Configuration
    HOST: str = "0.0.0.0"
    PORT: int = 8085
    PUBLIC_URL: Optional[str] = None  # Default public URL for re-writing URLs (HLS streams only)
    LOG_LEVEL: str = "error"
    APP_DEBUG: bool = False
    RELOAD: bool = False
    DOCS_URL: str = "/docs"
    REDOC_URL: str = "/redoc"
    OPENAPI_URL: str = "/openapi.json"

    # Route Configuration
    ROOT_PATH: str = "/m3u-proxy"  # Default base path for integration with m3u-editor
    CLIENT_TIMEOUT: int = 30
    STREAM_TIMEOUT: int = 300
    CLEANUP_INTERVAL: int = 60

    # Default stream properties (can be overridden per stream)
    DEFAULT_USER_AGENT: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    DEFAULT_CONNECTION_TIMEOUT: float = 10.0
    DEFAULT_READ_TIMEOUT: float = 30.0
    DEFAULT_MAX_RETRIES: int = 3
    DEFAULT_BACKOFF_FACTOR: float = 1.5
    DEFAULT_HEALTH_CHECK_INTERVAL: float = 300.0

    # Additional configuration from .env file
    DEFAULT_BUFFER_SIZE: int = 1048576
    DEFAULT_TIMEOUT: int = 30
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
    SHARED_STREAM_TIMEOUT: int = 300  # 5 minutes

    # Worker configuration
    WORKER_ID: Optional[str] = None
    HEARTBEAT_INTERVAL: int = 30  # seconds
    CLEANUP_INTERVAL: int = 60    # seconds
    STREAM_SHARING_STRATEGY: str = "url_profile"  # url_profile, url_only, disabled

    # API Authentication
    API_TOKEN: Optional[str] = None

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
