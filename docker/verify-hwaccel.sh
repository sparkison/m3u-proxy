#!/bin/bash
echo "🔧 Hardware Acceleration Verification Script"
echo "=============================================="

# Function to run commands and show output
run_cmd() {
    local cmd="$1"
    local description="$2"
    echo ""
    echo "📋 $description"
    echo "Command: $cmd"
    echo "----------------------------------------"
    if eval "$cmd" 2>&1; then
        echo "✅ Success"
    else
        echo "❌ Failed (exit code: $?)"
    fi
    echo "----------------------------------------"
}

# Check basic system info
echo ""
echo "🖥️  SYSTEM INFORMATION"
echo "===================="
run_cmd "uname -a" "System Information"
run_cmd "cat /etc/os-release | head -5" "OS Release"

# Check for GPU hardware
echo ""
echo "🎮 GPU HARDWARE DETECTION"
echo "========================"
run_cmd "lspci | grep -i 'VGA\|3D\|Display'" "PCI Graphics Devices"
run_cmd "lspci -nn | grep -i intel" "Intel Devices (with IDs)"

# Check device files
echo ""
echo "💾 DEVICE FILES"
echo "==============="
run_cmd "ls -la /dev/dri/ 2>/dev/null || echo 'No /dev/dri devices found'" "DRI Devices"
run_cmd "ls -la /dev/nvidia* 2>/dev/null || echo 'No NVIDIA devices found'" "NVIDIA Devices"

# Check permissions
if [ -e "/dev/dri" ]; then
    run_cmd "stat /dev/dri/" "DRI Directory Permissions"
    for dev in /dev/dri/*; do
        if [ -e "$dev" ]; then
            run_cmd "stat $dev" "Device: $dev"
        fi
    done
fi

# Check VAAPI support
echo ""
echo "🔧 VAAPI VERIFICATION"
echo "===================="
run_cmd "vainfo 2>&1 || echo 'vainfo not available'" "VAAPI Info"

# Check libva drivers
echo ""
echo "📚 LIBVA DRIVERS"
echo "==============="
run_cmd "find /usr/lib* -name '*va*' -type f 2>/dev/null | grep -E '(i965|iHD)' | head -10" "LIBVA Driver Files"
run_cmd "pkg-config --modversion libva 2>/dev/null || echo 'libva pkg-config not found'" "LIBVA Version"

# Check FFmpeg capabilities in detail
echo ""
echo "🎬 FFMPEG HARDWARE ACCELERATION"
echo "==============================="
run_cmd "ffmpeg -version | head -3" "FFmpeg Version"
run_cmd "ffmpeg -hide_banner -hwaccels" "Available Hardware Accelerators"
run_cmd "ffmpeg -hide_banner -encoders | grep vaapi" "VAAPI Encoders"
run_cmd "ffmpeg -hide_banner -decoders | grep vaapi" "VAAPI Decoders"

# Test VAAPI functionality if devices exist
if [ -e "/dev/dri/renderD128" ]; then
    echo ""
    echo "🧪 VAAPI FUNCTIONALITY TEST"
    echo "==========================="
    
    # Test basic VAAPI init
    run_cmd "timeout 10 ffmpeg -f lavfi -i testsrc=duration=1:size=320x240:rate=1 -vaapi_device /dev/dri/renderD128 -f null - 2>&1 | head -20" "VAAPI Device Init Test"
    
    # Test VAAPI encoding (if supported)
    if ffmpeg -hide_banner -encoders | grep -q h264_vaapi; then
        run_cmd "timeout 10 ffmpeg -f lavfi -i testsrc=duration=1:size=320x240:rate=1 -vaapi_device /dev/dri/renderD128 -vf 'format=nv12,hwupload' -c:v h264_vaapi -f null - 2>&1 | head -20" "VAAPI H.264 Encoding Test"
    fi
fi

# Check environment variables
echo ""
echo "🌍 ENVIRONMENT VARIABLES"
echo "======================="
run_cmd "env | grep -E '(LIBVA|VAAPI|DRI)' || echo 'No relevant environment variables set'" "Hardware-related Environment"

# Check Intel GPU specific info
echo ""
echo "💻 INTEL GPU SPECIFIC"
echo "===================="
run_cmd "cat /sys/class/drm/card*/device/vendor 2>/dev/null | head -5" "GPU Vendor IDs"
run_cmd "cat /sys/class/drm/card*/device/device 2>/dev/null | head -5" "GPU Device IDs"

# Check for Intel graphics driver
run_cmd "lsmod | grep -E '(i915|intel)'" "Intel Graphics Kernel Modules"

# Check GPU utilization (if available)
if command -v intel_gpu_top >/dev/null 2>&1; then
    run_cmd "timeout 3 intel_gpu_top -l 1 2>/dev/null || echo 'intel_gpu_top failed'" "Intel GPU Status"
fi

# Final summary with recommendations
echo ""
echo "💡 TROUBLESHOOTING RECOMMENDATIONS"
echo "=================================="

if ! lspci | grep -qi intel; then
    echo "⚠️  No Intel GPU detected in lspci output"
    echo "   - This might not be an Intel system"
    echo "   - Check if you're running on the right hardware"
elif [ ! -e "/dev/dri" ]; then
    echo "⚠️  No /dev/dri devices found"
    echo "   - DRI devices are not available in this container"
    echo "   - Add '--device /dev/dri:/dev/dri' to your docker run command"
    echo "   - Or add 'devices: - /dev/dri:/dev/dri' to docker-compose.yml"
elif ! command -v vainfo >/dev/null 2>&1; then
    echo "⚠️  vainfo command not available"
    echo "   - VAAPI tools might not be installed in the container"
    echo "   - This is expected in the linuxserver/ffmpeg image"
    echo "   - FFmpeg should still work with VAAPI if drivers are present"
elif ! ffmpeg -hide_banner -hwaccels | grep -q vaapi; then
    echo "⚠️  FFmpeg doesn't show VAAPI acceleration support"
    echo "   - FFmpeg might not be compiled with VAAPI support"
    echo "   - Check if libva-dev libraries are available"
else
    echo "✅ Hardware acceleration setup looks good!"
    echo "   - Try setting LIBVA_DRIVER_NAME=i965 environment variable"
    echo "   - For older Intel GPUs, i965 driver is often required"
fi

echo ""
echo "🔧 To enable hardware acceleration for older Intel GPUs:"
echo "   export LIBVA_DRIVER_NAME=i965"
echo "   Or add 'LIBVA_DRIVER_NAME=i965' to your docker environment"
echo ""
echo "✅ Verification complete!"