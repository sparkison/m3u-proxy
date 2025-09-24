# M3U Streaming Proxy

A high-performance streaming proxy service built with Python that supports multiple formats, failover URLs, hardware acceleration, and comprehensive API management.

## Features

- **Multi-format Support**: MPEG-TS, HLS, MKV, MP4, WebM, AVI
- **Failover Support**: Automatic failover to backup URLs if primary fails
- **Hardware Acceleration**: Support for GPU acceleration via `/dev/dri` on Docker
- **VOD Seeking**: Scrubbing support for video-on-demand content
- **Stream Sharing**: Connect multiple clients to existing streams
- **Event System**: Webhooks for stream events (started, stopped, failed, etc.)
- **API Management**: Full REST API for stream control and monitoring
- **Docker Support**: Ready-to-run Docker containers
- **Real-time Stats**: Monitor active streams, clients, and system resources

## Quick Start

### Local Development (macOS)

1. **Configure the Environment**:
   ```bash
   # Copy the example environment file
   cp .env.example .env
   
   # Edit .env to customize settings (especially PORT if needed)
   # PORT=8085  # Change to avoid conflicts
   ```

2. **Install Dependencies**:
   ```bash
   # Install FFmpeg (required)
   brew install ffmpeg
   
   # Install Python dependencies
   pip install -r requirements.txt
   ```

3. **Run the Server**:
   ```bash
   python main.py --debug --reload
   ```

4. **Access the API**:
   - API docs: http://localhost:PORT/docs (where PORT is from .env, default 8085)
   - Health check: http://localhost:PORT/health

### Docker Deployment

1. **Configure Environment** (optional):
   ```bash
   # Create or edit .env file to customize settings
   echo "PORT=8090" >> .env
   echo "LOG_LEVEL=DEBUG" >> .env
   ```

2. **Build and Run**:
   ```bash
   docker-compose up -d
   ```

3. **With Custom Port**:
   ```bash
   PORT=8090 docker-compose up -d
   ```

4. **With Hardware Acceleration** (Linux with GPU):
   ```bash
   # Make sure /dev/dri is available
   ls -la /dev/dri/
   docker-compose up -d
   ```

## API Usage

### Monitor Streams

```bash
# Get all streams
curl "http://localhost:8085/streams"

# Get specific stream
curl "http://localhost:8085/streams/{stream_id}"

# Get system stats
curl "http://localhost:8085/stats"

# Get hardware acceleration info
curl "http://localhost:8085/hardware"
```

### Create a Stream

```bash
curl -X POST "http://localhost:8085/streams" \
     -H "Content-Type: application/json" \
     -d '{
       "primary_url": "https://example.com/stream.m3u8",
       "failover_urls": [
         "https://backup1.com/stream.m3u8",
         "https://backup2.com/stream.m3u8"
       ],
       "enable_hardware_acceleration": true
     }'
```

### Start a Stream

```bash
curl -X POST "http://localhost:8085/streams/{stream_id}/start"
```

### Connect to Stream

- **HLS**: `http://localhost:8085/streams/{stream_id}/playlist.m3u8`
- **Direct**: `http://localhost:8085/streams/{stream_id}/stream`

### Monitor Streams

```bash
# Get all streams
curl "http://localhost:8085/streams"

# Get specific stream
curl "http://localhost:8085/streams/{stream_id}"

# Get system stats
curl "http://localhost:8085/stats"
```

### Seek in VOD Content

```bash
curl -X POST "http://localhost:8085/streams/{stream_id}/seek" \
     -H "Content-Type: application/json" \
     -d '{"position": 120.5}'
```

### Configure Webhooks

```bash
curl -X POST "http://localhost:8085/webhooks" \
     -H "Content-Type: application/json" \
     -d '{
       "url": "https://your-webhook-url.com/events",
       "events": ["stream_started", "stream_failed", "client_connected"],
       "headers": {"Authorization": "Bearer your-token"}
     }'
```

## Configuration

### Environment Variables

The application can be configured using environment variables or a `.env` file:

```bash
# Server Configuration
HOST=0.0.0.0              # Server host
PORT=8085                 # Server port (change to avoid conflicts)
LOG_LEVEL=INFO            # Logging level (DEBUG, INFO, WARNING, ERROR)
DEBUG=false               # Enable debug mode

# Stream Configuration
DEFAULT_BUFFER_SIZE=1048576    # Default buffer size (1MB)
DEFAULT_TIMEOUT=30            # Default stream timeout (seconds)
DEFAULT_RETRY_ATTEMPTS=3      # Default retry attempts
DEFAULT_RETRY_DELAY=5         # Default retry delay (seconds)

# Hardware Acceleration
ENABLE_HARDWARE_ACCELERATION=true    # Enable hardware acceleration

# Paths
TEMP_DIR=/tmp/m3u-proxy-streams     # Temporary directory for streams
LOG_FILE=m3u-proxy.log              # Log file path
```

### Using Different Ports

To avoid port conflicts with other applications:

1. **Local Development**:
   ```bash
   echo "PORT=8085" > .env
   python main.py
   ```

2. **Docker**:
   ```bash
   PORT=8085 docker-compose up -d
   ```

3. **Command Line**:
   ```bash
   python main.py --port 8085
   ```

### Hardware Acceleration

The service automatically detects and uses available hardware acceleration:

- **VAAPI**: Intel/AMD hardware acceleration on Linux (requires `/dev/dri` access)
- **Intel Quick Sync (QSV)**: Intel CPU hardware acceleration
- **NVIDIA NVENC**: NVIDIA GPU hardware acceleration
- **NVIDIA CUDA**: GPU acceleration for filtering and scaling
- **Apple VideoToolbox**: Hardware acceleration on macOS
- **OpenCL**: Cross-platform acceleration for filtering

**Priority Order**: NVENC > QSV > VAAPI > VideoToolbox > CUDA > OpenCL

**Check Available Methods**:
```bash
curl http://localhost:8085/hardware
```

**Docker with Hardware Acceleration**:
```bash
# For Intel/AMD (VAAPI)
docker run --device /dev/dri:/dev/dri m3u-proxy

# For NVIDIA (add nvidia runtime)
docker run --gpus all --device /dev/dri:/dev/dri m3u-proxy
```

## Event System

The service emits events for various stream activities:

- `stream_started`: When a stream begins
- `stream_stopped`: When a stream ends
- `stream_failed`: When a stream encounters an error
- `client_connected`: When a client connects
- `client_disconnected`: When a client disconnects
- `failover_triggered`: When failover to backup URL occurs

## Development

### Project Structure

```
m3u-proxy/
├── src/
│   ├── __init__.py
│   ├── models.py          # Data models and schemas
│   ├── stream_manager.py  # Core streaming logic
│   ├── events.py          # Event management and webhooks
│   └── api.py            # FastAPI application
├── main.py               # Application entry point
├── requirements.txt      # Python dependencies
├── Dockerfile           # Docker container definition
├── docker-compose.yml   # Docker Compose configuration
└── README.md           # This file
```

### Running Tests

```bash
# TODO: Add test suite
python -m pytest tests/
```

## NGINX Reverse Proxy (Optional)

If using Laravel Herd or need NGINX reverse proxy:

```nginx
server {
    listen 80;
    server_name your-domain.com;
    
    location / {
        proxy_pass http://127.0.0.1:8085;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        
        # For streaming
        proxy_buffering off;
        proxy_cache off;
    }
}
```

## Monitoring

### Health Checks

- **Endpoint**: `/health`
- **Docker**: Built-in healthcheck every 30s
- **Returns**: Service status and dependency states

### Metrics

- **Endpoint**: `/stats`
- **Includes**: Stream counts, client counts, CPU/memory usage
- **Prometheus**: Optional Prometheus metrics (see docker-compose.yml)

## Troubleshooting

### Common Issues

1. **FFmpeg not found**: Install FFmpeg system-wide
2. **Permission denied on /dev/dri**: Add user to video group (Linux)
3. **Streams not starting**: Check URL accessibility and format support
4. **High CPU usage**: Enable hardware acceleration if available

### Logs

- **Local**: Check `m3u-proxy.log` file
- **Docker**: `docker-compose logs -f m3u-proxy`

## License

This project is open source. Please ensure compliance with FFmpeg licensing if distributing.

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests if applicable
5. Submit a pull request

## Support

For issues, feature requests, or questions, please open a GitHub issue.
