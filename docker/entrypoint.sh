#!/bin/bash
set -e  # Exit immediately if a command exits with a non-zero status

# Function to handle cleanup
cleanup() {
    echo "🔥 Cleanup triggered! Stopping services..."
    for pid in "${pids[@]}"; do
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            echo "⛔ Stopping process (PID: $pid)..."
            kill -TERM "$pid" 2>/dev/null
        fi
    done
    wait
}

# Catch termination signals
trap cleanup TERM INT

# Array to track process IDs
pids=()

# Display version information
echo "⚡️ m3u-proxy starting up..."
echo "🐍 Python version: $(python3 --version)"
echo "🎬 FFmpeg version: $(ffmpeg -version 2>/dev/null | head -1 | cut -d' ' -f3 || echo 'Not found')"

# Set working directory
cd /app

# Run hardware acceleration check
echo "🔍 Running hardware acceleration check..."
chmod +x /app/docker/check-hwaccel.sh
# Run the hardware check in a subshell so any 'exit' in the script doesn't kill this entrypoint
/bin/bash /app/docker/check-hwaccel.sh || {
    echo "⚠️ Hardware check script exited with code $? — continuing startup using CPU settings"
}

# Load hardware acceleration environment variables
if [ -f /tmp/hwaccel.env ]; then
    source /tmp/hwaccel.env
    echo "✅ Hardware acceleration configuration loaded"
else
    echo "⚠️ No hardware acceleration configuration found"
fi

# Start the Python application
echo "🚀 Starting m3u-proxy application..."
python3 main.py &
app_pid=$!
echo "✅ m3u-proxy started with PID $app_pid"
pids+=("$app_pid")

# Wait for the application to exit
echo "⏳ Waiting for application processes..."
wait "${pids[@]}"
echo "✅ All processes have exited. Container shutting down."