#!/bin/bash
echo "🔍 Checking for GPU acceleration devices..."

# Helper function for device access checks
check_dev() {
    local dev=$1
    if [ -e "$dev" ]; then
        if [ -r "$dev" ] && [ -w "$dev" ]; then
            echo "✅ Device $dev is accessible."
        else
            echo "⚠️ Device $dev exists but is not accessible. Check permissions or container runtime options."
        fi
    else
        echo "ℹ️ Device $dev does not exist."
    fi
}

# Initialize device detection flags
ANY_GPU_DEVICES_FOUND=false
DRI_DEVICES_FOUND=false
NVIDIA_FOUND=false
NVIDIA_GPU_IN_LSPCI=false
INTEL_GPU_IN_LSPCI=false
AMD_GPU_IN_LSPCI=false

# Check for all GPU types in hardware via lspci
if command -v lspci >/dev/null 2>&1; then
    # Check for NVIDIA GPUs
    if lspci | grep -i "NVIDIA" | grep -i "VGA\|3D\|Display" >/dev/null; then
        NVIDIA_GPU_IN_LSPCI=true
        NVIDIA_MODEL=$(lspci | grep -i "NVIDIA" | grep -i "VGA\|3D\|Display" | head -1 | sed -E 's/.*: (.*) \[.*/\1/' | sed 's/Corporation //')
    fi
    
    # Check for Intel GPUs
    if lspci | grep -i "Intel" | grep -v "NVIDIA" | grep -i "VGA\|3D\|Display" >/dev/null; then
        INTEL_GPU_IN_LSPCI=true
        INTEL_MODEL=$(lspci | grep -i "Intel" | grep -v "NVIDIA" | grep -i "VGA\|3D\|Display" | head -1 | sed -E 's/.*: (.*) \[.*/\1/' | sed 's/Corporation //')
    fi
    
    # Check for AMD GPUs
    if lspci | grep -i "AMD\|ATI\|Advanced Micro Devices" | grep -v "NVIDIA\|Intel" | grep -i "VGA\|3D\|Display" >/dev/null; then
        AMD_GPU_IN_LSPCI=true
        AMD_MODEL=$(lspci | grep -i "AMD\|ATI\|Advanced Micro Devices" | grep -v "NVIDIA\|Intel" | grep -i "VGA\|3D\|Display" | head -1 | sed -E 's/.*: (.*) \[.*/\1/' | sed 's/Corporation //' | sed 's/Technologies //')
    fi
    
    # Display detected GPU hardware
    if [ "$NVIDIA_GPU_IN_LSPCI" = true ]; then
        echo "🔍 Hardware detection: NVIDIA GPU ($NVIDIA_MODEL)"
    fi
    if [ "$INTEL_GPU_IN_LSPCI" = true ]; then
        echo "🔍 Hardware detection: Intel GPU ($INTEL_MODEL)"
    fi
    if [ "$AMD_GPU_IN_LSPCI" = true ]; then
        echo "🔍 Hardware detection: AMD GPU ($AMD_MODEL)"
    fi
fi

# Check for any GPU devices first
for dev in /dev/dri/renderD* /dev/dri/card* /dev/nvidia*; do
    if [ -e "$dev" ]; then
        ANY_GPU_DEVICES_FOUND=true
        break
    fi
done

if [ "$ANY_GPU_DEVICES_FOUND" = true ]; then
    echo "🔍 Checking GPU device access..."
    
    # Check NVIDIA devices
    for dev in /dev/nvidia*; do
        if [ -e "$dev" ]; then
            NVIDIA_FOUND=true
            check_dev "$dev"
        fi
    done
    
    # Check DRI devices (Intel/AMD)
    for dev in /dev/dri/renderD* /dev/dri/card*; do
        if [ -e "$dev" ]; then
            DRI_DEVICES_FOUND=true
            check_dev "$dev"
        fi
    done
else
    echo "❌ No GPU acceleration devices detected in this container."
    echo "ℹ️ Checking for potential configuration issues..."
    
    if command -v lspci >/dev/null 2>&1; then
        if lspci | grep -i "VGA\|3D\|Display" | grep -i "NVIDIA\|Intel\|AMD" >/dev/null; then
            echo "⚠️ Host system appears to have GPU hardware, but no devices are accessible to the container."
            echo "   - For NVIDIA GPUs: Ensure NVIDIA Container Runtime is configured properly"
            echo "   - For Intel/AMD GPUs: Verify that /dev/dri/ devices are passed to the container"
        else
            echo "ℹ️ No GPU hardware detected on the host system. CPU-only transcoding will be used."
        fi
    fi
    
    echo "📋 =================================================="
    echo "✅ GPU detection script complete. No GPUs available for hardware acceleration."
    return 0 2>/dev/null || true
fi

# Check FFmpeg hardware acceleration support
echo "🔍 Checking FFmpeg hardware acceleration capabilities..."
if command -v ffmpeg >/dev/null 2>&1; then
    HWACCEL=$(ffmpeg -hide_banner -hwaccels 2>/dev/null | grep -v "Hardware acceleration methods:" || echo "None found")
    
    # Initialize variables to store compatible methods
    COMPATIBLE_METHODS=""
    
    echo "🔍 Available FFmpeg hardware acceleration methods:"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    
    if [ -n "$HWACCEL" ] && [ "$HWACCEL" != "None found" ]; then
        echo "  📌 Compatible with your hardware:"
        COMPATIBLE_FOUND=false
        
        for method in $HWACCEL; do
            if [ "$method" = "Hardware" ] || [ -z "$method" ]; then
                continue
            fi
            
            IS_COMPATIBLE=false
            DESCRIPTION=""
            
            # Check NVIDIA methods
            if [ "$NVIDIA_FOUND" = true ] && [[ "$method" =~ ^(cuda|cuvid|nvenc|nvdec)$ ]]; then
                IS_COMPATIBLE=true
                DESCRIPTION="NVIDIA GPU acceleration"
            # Check Intel methods
            elif [ "$INTEL_GPU_IN_LSPCI" = true ] && [ "$method" = "qsv" ]; then
                IS_COMPATIBLE=true
                DESCRIPTION="Intel QuickSync acceleration"
            # Check VAAPI methods
            elif [ "$method" = "vaapi" ] && (([ "$INTEL_GPU_IN_LSPCI" = true ] || [ "$AMD_GPU_IN_LSPCI" = true ]) && [ "$DRI_DEVICES_FOUND" = true ]); then
                IS_COMPATIBLE=true
                if [ "$INTEL_GPU_IN_LSPCI" = true ]; then
                    DESCRIPTION="Intel VAAPI acceleration"
                else
                    DESCRIPTION="AMD VAAPI acceleration"
                fi
            fi
            
            if [ "$IS_COMPATIBLE" = true ]; then
                COMPATIBLE_FOUND=true
                COMPATIBLE_METHODS="$COMPATIBLE_METHODS $method"
                echo "    ✅ $method - $DESCRIPTION"
            fi
        done
        
        if [ "$COMPATIBLE_FOUND" = false ]; then
            echo "    ❌ No compatible acceleration methods found for your hardware"
        fi
    else
        echo "  ❌ No hardware acceleration methods found"
    fi
    
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
else
    echo "⚠️ FFmpeg not found in PATH."
fi

# Set environment variables for detected hardware acceleration
export HW_ACCEL_AVAILABLE=false
export HW_ACCEL_TYPE=""
export HW_ACCEL_DEVICE=""

# Provide a final summary and set environment variables
echo "📋 ===================== SUMMARY ====================="

if [ "$NVIDIA_FOUND" = true ] && echo "$COMPATIBLE_METHODS" | grep -q "cuda\|nvenc"; then
    if [ -n "$NVIDIA_MODEL" ]; then
        echo "🔰 NVIDIA GPU: $NVIDIA_MODEL"
    else
        echo "🔰 NVIDIA GPU: ACTIVE"
    fi
    echo "✅ FFmpeg NVIDIA acceleration: AVAILABLE"
    export HW_ACCEL_AVAILABLE=true
    export HW_ACCEL_TYPE="nvidia"
    export HW_ACCEL_DEVICE="cuda"
    echo "   Recommended FFmpeg args: -hwaccel cuda -hwaccel_output_format cuda"
elif [ "$DRI_DEVICES_FOUND" = true ] && echo "$COMPATIBLE_METHODS" | grep -q "vaapi"; then
    if [ "$INTEL_GPU_IN_LSPCI" = true ] && [ -n "$INTEL_MODEL" ]; then
        echo "🔰 Intel GPU: $INTEL_MODEL"
        export HW_ACCEL_TYPE="intel"
    elif [ "$AMD_GPU_IN_LSPCI" = true ] && [ -n "$AMD_MODEL" ]; then
        echo "🔰 AMD GPU: $AMD_MODEL"
        export HW_ACCEL_TYPE="amd"
    else
        echo "🔰 GPU: VAAPI COMPATIBLE"
        export HW_ACCEL_TYPE="vaapi"
    fi
    echo "✅ FFmpeg VAAPI acceleration: AVAILABLE"
    export HW_ACCEL_AVAILABLE=true
    export HW_ACCEL_DEVICE="vaapi"
    echo "   Recommended FFmpeg args: -hwaccel vaapi -hwaccel_output_format vaapi"
else
    echo "❌ NO GPU ACCELERATION DETECTED"
    echo "⚠️ Hardware acceleration is unavailable - using CPU-only transcoding"
    export HW_ACCEL_AVAILABLE=false
    export HW_ACCEL_TYPE="cpu"
    export HW_ACCEL_DEVICE=""
fi

echo "📋 =================================================="
echo "✅ GPU detection script complete."

# Export variables to a file for the Python application to read
cat > /tmp/hwaccel.env << EOF
HW_ACCEL_AVAILABLE=$HW_ACCEL_AVAILABLE
HW_ACCEL_TYPE=$HW_ACCEL_TYPE
HW_ACCEL_DEVICE=$HW_ACCEL_DEVICE
EOF

echo "💾 Hardware acceleration settings saved to /tmp/hwaccel.env"