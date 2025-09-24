#!/usr/bin/env python3
"""
M3U Streaming Proxy Server
Main entry point for the application
"""

import asyncio
import uvicorn
import argparse
import os
import logging
from pathlib import Path

from src.config import config


def setup_logging(debug: bool = None):
    """Setup logging configuration."""
    if debug is None:
        debug = config.debug
    
    level = logging.DEBUG if debug else getattr(logging, config.log_level, logging.INFO)
    
    # Ensure temp directory exists
    os.makedirs(os.path.dirname(config.log_file), exist_ok=True)
    
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(config.log_file)
        ]
    )


def main():
    parser = argparse.ArgumentParser(description='M3U Streaming Proxy Server')
    parser.add_argument('--host', default=config.host, help='Host to bind to')
    parser.add_argument('--port', type=int, default=config.port, help='Port to bind to')
    parser.add_argument('--reload', action='store_true', help='Enable auto-reload')
    parser.add_argument('--debug', action='store_true', help='Enable debug logging')
    parser.add_argument('--workers', type=int, default=1, help='Number of workers')
    
    args = parser.parse_args()
    
    setup_logging(args.debug)
    
    # Create temp directory for streams
    os.makedirs(config.temp_dir, exist_ok=True)
    
    logger = logging.getLogger(__name__)
    logger.info(f"Starting M3U Proxy Server on {args.host}:{args.port}")
    logger.info(f"Configuration: {config.to_dict()}")
    
    try:
        uvicorn.run(
            "src.api:app",
            host=args.host,
            port=args.port,
            reload=args.reload,
            workers=args.workers,
            log_level="debug" if args.debug else config.log_level.lower(),
            access_log=True
        )
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
    except Exception as e:
        logger.error(f"Server error: {e}")
        raise

if __name__ == "__main__":
    main()
