"""
Configuration for Redis pooling and multi-worker coordination.
"""

import os
from typing import Optional
from config import settings


def get_redis_config() -> dict:
    """Get Redis configuration"""
    # Build Redis URL from components or use explicit URL if provided
    redis_url = os.getenv(
        "REDIS_URL", f"redis://{settings.REDIS_HOST}:{settings.REDIS_SERVER_PORT}/{settings.REDIS_DB}")

    return {
        "host": settings.REDIS_HOST,
        "port": settings.REDIS_SERVER_PORT,
        "db": settings.REDIS_DB,
        "redis_url": redis_url,
        "enabled": settings.REDIS_ENABLED,
        "pooling_enabled": settings.ENABLE_TRANSCODING_POOLING,
        "max_clients_per_stream": settings.MAX_CLIENTS_PER_SHARED_STREAM,
        "stream_timeout": settings.SHARED_STREAM_TIMEOUT,
        "worker_id": settings.WORKER_ID,
        "heartbeat_interval": settings.HEARTBEAT_INTERVAL,
        "cleanup_interval": settings.CLEANUP_INTERVAL,
        "sharing_strategy": settings.STREAM_SHARING_STRATEGY
    }


def should_use_pooling() -> bool:
    """Check if pooling should be used"""
    return settings.REDIS_ENABLED and settings.ENABLE_TRANSCODING_POOLING
