#!/usr/bin/env python3
"""
m3u-proxy - Main Entry Point
A high-performance IPTV streaming proxy with client management and failover support.
"""

import uvicorn
import logging
import sys
import os
import asyncio

# Add the src directory to Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

# Import config after setting up the path
from config import settings


def main():
    """Main function to start the m3u-proxy server."""

    # Try to use uvloop for better async performance (2-4x faster)
    try:
        import uvloop
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
        use_uvloop = True
    except ImportError:
        use_uvloop = False

    # Configure logging
    logging.basicConfig(
        level=settings.LOG_LEVEL.upper(),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    logger = logging.getLogger(__name__)
    logger.info("="*60)
    logger.info(f"Starting m3u-proxy v0.2.2 on {settings.HOST}:{settings.PORT}")
    logger.info("="*60)
    logger.info(f"Log level set to: {settings.LOG_LEVEL}")
    if use_uvloop:
        logger.info("✓ Using uvloop for optimized async I/O performance")
    else:
        logger.info("✓ Using standard asyncio (install uvloop for better performance)")
    logger.info("✓ Direct proxy architecture (per-client connections)")
    logger.info("✓ Connection pooling enabled")
    logger.info("✓ Seamless failover support")
    if settings.DEBUG:
        logger.warning("⚠ Debug mode is enabled. Do not use in production.")
    if settings.RELOAD:
        logger.info("✓ Auto-reload is enabled.")

    # Start the server using settings from the config object
    uvicorn.run(
        "api:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.RELOAD,
        log_level=settings.LOG_LEVEL.lower(),
        loop="uvloop" if use_uvloop and not settings.RELOAD else "asyncio"
    )


if __name__ == "__main__":
    main()
