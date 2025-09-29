#!/usr/bin/env python3
"""
m3u-proxy - Main Entry Point
A high-performance IPTV streaming proxy with client management and failover support.
"""

import uvicorn
import logging
import sys
import os

# Add the src directory to Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

# Import config after setting up the path
from config import settings


def main():
    """Main function to start the m3u-proxy server."""

    # Configure logging
    logging.basicConfig(
        level=settings.LOG_LEVEL.upper(),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    logger = logging.getLogger(__name__)
    logger.info(f"Starting m3u-proxy on {settings.HOST}:{settings.PORT}")
    logger.info(f"Log level set to: {settings.LOG_LEVEL}")
    if settings.DEBUG:
        logger.warning("Debug mode is enabled. Do not use in production.")
    if settings.RELOAD:
        logger.info("Auto-reload is enabled.")

    # Start the server using settings from the config object
    uvicorn.run(
        "api:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.RELOAD,
        log_level=settings.LOG_LEVEL.lower()
    )


if __name__ == "__main__":
    main()
