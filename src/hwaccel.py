"""
Hardware Acceleration Detection Utilities

This module provides utilities to detect and configure hardware acceleration
for FFmpeg operations based on the container's GPU capabilities.
"""

import os
import logging
from typing import Dict, Optional, List
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class HardwareAccelConfig:
    """Configuration for hardware acceleration."""
    available: bool
    type: str  # 'nvidia', 'intel', 'amd', 'vaapi', 'cpu'
    device: str  # 'cuda', 'vaapi', ''
    ffmpeg_args: List[str]


class HardwareAccelDetector:
    """Detects and configures hardware acceleration for FFmpeg."""

    def __init__(self):
        self.config = self._load_config()

    def _load_config(self) -> HardwareAccelConfig:
        """Load hardware acceleration configuration from environment or file."""
        # Try to load from the file created by the init script
        hwaccel_file = "/tmp/hwaccel.env"
        env_vars = {}

        if os.path.exists(hwaccel_file):
            try:
                with open(hwaccel_file, 'r') as f:
                    for line in f:
                        if '=' in line and not line.strip().startswith('#'):
                            key, value = line.strip().split('=', 1)
                            env_vars[key] = value
                logger.info(
                    f"Loaded hardware acceleration config from {hwaccel_file}")
            except Exception as e:
                logger.warning(
                    f"Failed to load hardware config from file: {e}")

        # Fallback to environment variables
        available = env_vars.get('HW_ACCEL_AVAILABLE', os.getenv(
            'HW_ACCEL_AVAILABLE', 'false')).lower() == 'true'
        accel_type = env_vars.get(
            'HW_ACCEL_TYPE', os.getenv('HW_ACCEL_TYPE', 'cpu'))
        device = env_vars.get(
            'HW_ACCEL_DEVICE', os.getenv('HW_ACCEL_DEVICE', ''))

        # Generate FFmpeg arguments based on detected hardware
        ffmpeg_args = self._generate_ffmpeg_args(available, accel_type, device)

        config = HardwareAccelConfig(
            available=available,
            type=accel_type,
            device=device,
            ffmpeg_args=ffmpeg_args
        )

        logger.info(f"Hardware acceleration config: {config}")
        return config

    def _generate_ffmpeg_args(self, available: bool, accel_type: str, device: str) -> List[str]:
        """Generate appropriate FFmpeg arguments for the detected hardware."""
        if not available or accel_type == 'cpu':
            return []

        args = []

        if accel_type == 'nvidia' and device == 'cuda':
            args.extend(['-hwaccel', 'cuda', '-hwaccel_output_format', 'cuda'])
        elif device == 'vaapi':
            args.extend(
                ['-hwaccel', 'vaapi', '-hwaccel_output_format', 'vaapi'])
            # Add VAAPI device if needed
            if os.path.exists('/dev/dri/renderD128'):
                args.extend(['-vaapi_device', '/dev/dri/renderD128'])
        elif accel_type == 'intel' and device == 'vaapi':
            args.extend(
                ['-hwaccel', 'vaapi', '-hwaccel_output_format', 'vaapi'])
            if os.path.exists('/dev/dri/renderD128'):
                args.extend(['-vaapi_device', '/dev/dri/renderD128'])

        return args

    def get_transcode_args(self, input_codec: Optional[str] = None, output_codec: Optional[str] = None) -> List[str]:
        """Get FFmpeg arguments optimized for transcoding with available hardware."""
        args = self.config.ffmpeg_args.copy()

        if not self.config.available:
            logger.info("Using CPU-only transcoding")
            return args

        # Add codec-specific optimizations
        if self.config.type == 'nvidia':
            if output_codec and 'h264' in output_codec.lower():
                args.extend(['-c:v', 'h264_nvenc'])
            elif output_codec and 'h265' in output_codec.lower():
                args.extend(['-c:v', 'hevc_nvenc'])
        elif self.config.type in ['intel', 'vaapi']:
            if output_codec and 'h264' in output_codec.lower():
                args.extend(['-c:v', 'h264_vaapi'])
            elif output_codec and 'h265' in output_codec.lower():
                args.extend(['-c:v', 'hevc_vaapi'])

        logger.info(f"Generated transcode args: {args}")
        return args

    def get_basic_args(self) -> List[str]:
        """Get basic hardware acceleration arguments without codec specification."""
        return self.config.ffmpeg_args.copy()

    def is_available(self) -> bool:
        """Check if hardware acceleration is available."""
        return self.config.available

    def get_type(self) -> str:
        """Get the type of hardware acceleration."""
        return self.config.type

    def log_capabilities(self):
        """Log the detected hardware acceleration capabilities."""
        if self.config.available:
            logger.info(
                f"🚀 Hardware acceleration ENABLED: {self.config.type} ({self.config.device})")
            logger.info(f"📋 FFmpeg args: {' '.join(self.config.ffmpeg_args)}")
        else:
            logger.info(
                "💻 Using CPU-only transcoding (no hardware acceleration)")


# Global instance for easy access
hw_accel = HardwareAccelDetector()


def get_ffmpeg_hwaccel_args(output_codec: Optional[str] = None) -> List[str]:
    """
    Convenience function to get hardware acceleration arguments for FFmpeg.

    Args:
        output_codec: Target output codec (e.g., 'h264', 'h265')

    Returns:
        List of FFmpeg arguments for hardware acceleration
    """
    return hw_accel.get_transcode_args(output_codec=output_codec)


def is_hwaccel_available() -> bool:
    """
    Convenience function to check if hardware acceleration is available.

    Returns:
        True if hardware acceleration is available, False otherwise
    """
    return hw_accel.is_available()
