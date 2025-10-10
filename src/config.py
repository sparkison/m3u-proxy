from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional

# Application version
VERSION = "0.2.5"


class Settings(BaseSettings):
    """
    Application configuration loaded from environment variables.
    Utilizes pydantic-settings for robust validation and type-casting.
    """

    # Server Configuration
    HOST: str = "0.0.0.0"
    PORT: int = 8085
    LOG_LEVEL: str = "error"
    APP_DEBUG: bool = False
    RELOAD: bool = False
    DOCS_URL: str = "/docs"
    REDOC_URL: str = "/redoc"
    OPENAPI_URL: str = "/openapi.json"

    # Stream Configuration
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

    # Optional Redis Configuration for distributed systems (not yet implemented)
    REDIS_HOST: Optional[str] = None
    REDIS_SERVER_PORT: int = 6379

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
