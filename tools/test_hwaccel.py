#!/usr/bin/env python3
"""
Test Hardware Acceleration Integration

This script tests the hardware acceleration detection and integration.
"""

import os
import sys
import logging

# Add src to path for testing
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from hwaccel import hw_accel, get_ffmpeg_hwaccel_args, is_hwaccel_available

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def test_hardware_detection():
    """Test hardware acceleration detection."""
    logger.info("🧪 Testing hardware acceleration detection...")
    
    # Test basic detection
    available = is_hwaccel_available()
    hw_type = hw_accel.get_type()
    
    logger.info(f"Hardware acceleration available: {available}")
    logger.info(f"Hardware type: {hw_type}")
    
    # Test getting FFmpeg arguments
    basic_args = hw_accel.get_basic_args()
    h264_args = get_ffmpeg_hwaccel_args("h264")
    h265_args = get_ffmpeg_hwaccel_args("h265")
    
    logger.info(f"Basic FFmpeg args: {basic_args}")
    logger.info(f"H.264 transcode args: {h264_args}")
    logger.info(f"H.265 transcode args: {h265_args}")
    
    # Log full capabilities
    hw_accel.log_capabilities()
    
    return available


def test_ffmpeg_availability():
    """Test if FFmpeg is available and working."""
    logger.info("🧪 Testing FFmpeg availability...")
    
    import subprocess
    try:
        result = subprocess.run(['ffmpeg', '-version'], 
                              capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            version_line = result.stdout.split('\n')[0]
            logger.info(f"✅ FFmpeg available: {version_line}")
            return True
        else:
            logger.error("❌ FFmpeg command failed")
            return False
    except Exception as e:
        logger.error(f"❌ FFmpeg not available: {e}")
        return False


def test_environment_file():
    """Test if the hardware acceleration environment file exists."""
    logger.info("🧪 Testing environment file...")
    
    hwaccel_file = "/tmp/hwaccel.env"
    if os.path.exists(hwaccel_file):
        logger.info(f"✅ Hardware acceleration config file exists: {hwaccel_file}")
        with open(hwaccel_file, 'r') as f:
            content = f.read()
            logger.info(f"📄 Config content:\n{content}")
        return True
    else:
        logger.warning(f"⚠️ Hardware acceleration config file not found: {hwaccel_file}")
        return False


def main():
    """Run all tests."""
    logger.info("🚀 Starting hardware acceleration integration tests...")
    
    results = []
    
    # Test hardware detection
    results.append(("Hardware Detection", test_hardware_detection()))
    
    # Test FFmpeg availability
    results.append(("FFmpeg Availability", test_ffmpeg_availability()))
    
    # Test environment file
    results.append(("Environment File", test_environment_file()))
    
    # Print summary
    logger.info("\n📋 Test Results Summary:")
    logger.info("=" * 50)
    
    all_passed = True
    for test_name, passed in results:
        status = "✅ PASS" if passed else "❌ FAIL"
        logger.info(f"{test_name:<20}: {status}")
        if not passed:
            all_passed = False
    
    logger.info("=" * 50)
    
    if all_passed:
        logger.info("🎉 All tests passed! Hardware acceleration integration is working.")
    else:
        logger.warning("⚠️ Some tests failed. Check the logs above for details.")
    
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())