#!/usr/bin/env python3
"""
Example: Using Hardware Acceleration in m3u-proxy

This example demonstrates how to use the hardware acceleration detection
in your streaming proxy for transcoding operations.
"""

import asyncio
import subprocess
import logging
from typing import List, Optional
from pathlib import Path

# Import the hardware acceleration detector
from hwaccel import hw_accel, get_ffmpeg_hwaccel_args, is_hwaccel_available

logger = logging.getLogger(__name__)


async def transcode_stream(input_url: str, output_path: str, target_codec: str = "h264") -> bool:
    """
    Transcode a stream using hardware acceleration if available.
    
    Args:
        input_url: Input stream URL or file path
        output_path: Output file path
        target_codec: Target video codec (h264, h265, etc.)
    
    Returns:
        True if transcoding succeeded, False otherwise
    """
    try:
        # Get hardware acceleration arguments
        hwaccel_args = get_ffmpeg_hwaccel_args(output_codec=target_codec)
        
        # Build FFmpeg command
        cmd = ["ffmpeg", "-y"]  # -y to overwrite output file
        
        # Add hardware acceleration args first
        cmd.extend(hwaccel_args)
        
        # Add input
        cmd.extend(["-i", input_url])
        
        # Add output settings
        if is_hwaccel_available():
            hw_type = hw_accel.get_type()
            if hw_type == "nvidia":
                # Use NVIDIA hardware encoders
                if target_codec == "h264":
                    cmd.extend(["-c:v", "h264_nvenc"])
                elif target_codec == "h265":
                    cmd.extend(["-c:v", "hevc_nvenc"])
                cmd.extend(["-preset", "fast", "-b:v", "2M"])
            elif hw_type in ["intel", "amd", "vaapi"]:
                # Use VAAPI hardware encoders
                if target_codec == "h264":
                    cmd.extend(["-c:v", "h264_vaapi"])
                elif target_codec == "h265":
                    cmd.extend(["-c:v", "hevc_vaapi"])
                cmd.extend(["-b:v", "2M"])
        else:
            # CPU-only encoding
            logger.info("Using CPU-only encoding")
            if target_codec == "h264":
                cmd.extend(["-c:v", "libx264", "-preset", "faster"])
            elif target_codec == "h265":
                cmd.extend(["-c:v", "libx265", "-preset", "faster"])
            cmd.extend(["-b:v", "2M"])
        
        # Add audio codec and output file
        cmd.extend(["-c:a", "aac", "-b:a", "128k", output_path])
        
        logger.info(f"Running FFmpeg command: {' '.join(cmd)}")
        
        # Run the command
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        stdout, stderr = await process.communicate()
        
        if process.returncode == 0:
            logger.info(f"Transcoding completed successfully: {output_path}")
            return True
        else:
            logger.error(f"Transcoding failed: {stderr.decode()}")
            return False
            
    except Exception as e:
        logger.error(f"Error during transcoding: {e}")
        return False


async def probe_stream_hwaccel(input_url: str) -> Optional[dict]:
    """
    Probe a stream using hardware-accelerated FFprobe if available.
    
    Args:
        input_url: Input stream URL or file path
    
    Returns:
        Stream information dict or None if failed
    """
    try:
        # Get basic hardware acceleration args (for decoding)
        hwaccel_args = hw_accel.get_basic_args()
        
        cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams"]
        
        # Add hardware acceleration for decoding if available
        if hwaccel_args:
            cmd.extend(hwaccel_args)
        
        cmd.append(input_url)
        
        logger.info(f"Probing stream: {input_url}")
        
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        stdout, stderr = await process.communicate()
        
        if process.returncode == 0:
            import json
            return json.loads(stdout.decode())
        else:
            logger.error(f"Stream probe failed: {stderr.decode()}")
            return None
            
    except Exception as e:
        logger.error(f"Error probing stream: {e}")
        return None


def get_optimal_transcode_settings() -> dict:
    """
    Get optimal transcoding settings based on available hardware.
    
    Returns:
        Dictionary with recommended settings
    """
    settings = {
        "hwaccel_available": hw_accel.is_available(),
        "type": hw_accel.get_type(),
        "recommended_presets": [],
        "max_concurrent_streams": 1,
    }
    
    if hw_accel.is_available():
        hw_type = hw_accel.get_type()
        
        if hw_type == "nvidia":
            settings["recommended_presets"] = ["fast", "medium", "slow"]
            settings["max_concurrent_streams"] = 4  # NVIDIA can handle more
            settings["supported_codecs"] = ["h264_nvenc", "hevc_nvenc"]
        elif hw_type in ["intel", "amd", "vaapi"]:
            settings["recommended_presets"] = ["medium", "slow"]
            settings["max_concurrent_streams"] = 2  # VAAPI more limited
            settings["supported_codecs"] = ["h264_vaapi", "hevc_vaapi"]
    else:
        settings["recommended_presets"] = ["ultrafast", "faster", "fast"]
        settings["max_concurrent_streams"] = 1  # CPU limited
        settings["supported_codecs"] = ["libx264", "libx265"]
    
    return settings


async def main():
    """Example usage of hardware acceleration features."""
    # Log the detected hardware capabilities
    hw_accel.log_capabilities()
    
    # Get optimal settings
    settings = get_optimal_transcode_settings()
    logger.info(f"Optimal transcode settings: {settings}")
    
    # Example: Transcode a stream (replace with actual stream URL)
    # await transcode_stream(
    #     "https://example.com/stream.m3u8",
    #     "/tmp/output.mp4",
    #     target_codec="h264"
    # )
    
    logger.info("Hardware acceleration example completed")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())