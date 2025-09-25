import os
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv


class Config:
    """Application configuration loaded from environment variables and .env file."""

    def __init__(self):
        # Load .env file if it exists
        env_file = Path(__file__).parent.parent / ".env"
        if env_file.exists():
            load_dotenv(env_file)

    # Server Configuration
    @property
    def host(self) -> str:
        return os.getenv("HOST", "0.0.0.0")

    @property
    def port(self) -> int:
        return int(os.getenv("PORT", "8080"))

    @property
    def log_level(self) -> str:
        return os.getenv("LOG_LEVEL", "INFO").upper()

    @property
    def debug(self) -> bool:
        return os.getenv("DEBUG", "false").lower() in ("true", "1", "yes", "on")

    # Stream Configuration
    @property
    def default_buffer_size(self) -> int:
        return int(os.getenv("DEFAULT_BUFFER_SIZE", "1048576"))  # 1MB

    @property
    def default_timeout(self) -> int:
        return int(os.getenv("DEFAULT_TIMEOUT", "30"))

    @property
    def default_retry_attempts(self) -> int:
        return int(os.getenv("DEFAULT_RETRY_ATTEMPTS", "3"))

    @property
    def default_retry_delay(self) -> int:
        return int(os.getenv("DEFAULT_RETRY_DELAY", "5"))

    # Hardware Acceleration
    @property
    def enable_hardware_acceleration(self) -> bool:
        return os.getenv("ENABLE_HARDWARE_ACCELERATION", "true").lower() in ("true", "1", "yes", "on")

    # Paths
    @property
    def temp_dir(self) -> str:
        return os.getenv("TEMP_DIR", "/tmp/m3u-proxy-streams")

    @property
    def log_file(self) -> str:
        return os.getenv("LOG_FILE", "m3u-proxy.log")

    # Optional Redis Configuration
    @property
    def redis_host(self) -> Optional[str]:
        return os.getenv("REDIS_HOST")

    @property
    def redis_port(self) -> int:
        return int(os.getenv("REDIS_PORT", "6379"))

    @property
    def redis_db(self) -> int:
        return int(os.getenv("REDIS_DB", "0"))

    # Optional Metrics Configuration
    @property
    def enable_metrics(self) -> bool:
        return os.getenv("ENABLE_METRICS", "false").lower() in ("true", "1", "yes", "on")

    @property
    def metrics_port(self) -> int:
        return int(os.getenv("METRICS_PORT", "9090"))

    def to_dict(self) -> dict:
        """Return configuration as dictionary for debugging."""
        return {
            "host": self.host,
            "port": self.port,
            "log_level": self.log_level,
            "debug": self.debug,
            "default_buffer_size": self.default_buffer_size,
            "default_timeout": self.default_timeout,
            "default_retry_attempts": self.default_retry_attempts,
            "default_retry_delay": self.default_retry_delay,
            "enable_hardware_acceleration": self.enable_hardware_acceleration,
            "temp_dir": self.temp_dir,
            "log_file": self.log_file,
            "redis_host": self.redis_host,
            "redis_port": self.redis_port,
            "redis_db": self.redis_db,
            "enable_metrics": self.enable_metrics,
            "metrics_port": self.metrics_port,
        }


# Global configuration instance
config = Config()
