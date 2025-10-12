# m3u-proxy

A high-performance HTTP proxy server for IPTV content with **true live proxying**, per-client connection management, and seamless failover support. Built with FastAPI and optimized for efficiency.

## Features

### Core Streaming
- 🚀 **Pure HTTP Proxy**: Zero transcoding, direct byte-for-byte streaming
- 🎯 **Per-Client Connections**: Each client gets independent provider connection
- ⚡ **Truly Ephemeral**: Provider connections open only when client consuming
- 📺 **HLS Support**: Optimized playlist and segment handling (.m3u8)
- 📡 **Continuous Streams**: Direct proxy for .ts, .mp4, .mkv, .webm, .avi files
- 🔄 **Real-time URL Rewriting**: Automatic playlist modification for proxied content
- 📱 **Full VOD Support**: Byte-range requests, seeking, multiple positions

### Performance & Reliability
- ⚡ **uvloop Integration**: 2-4x faster async I/O operations
- 🔄 **Seamless Failover**: <100ms transparent URL switching per client
- 🎯 **Immediate Cleanup**: Connections close instantly when client stops

### Management & Monitoring
- 👥 **Client Tracking**: Individual client sessions and bandwidth monitoring
- 📊 **Real-time Statistics**: Live metrics on streams, clients, and data usage
- 🔎 **Stream Type Detection**: Automatic HLS/VOD/Live detection
- 🧹 **Automatic Cleanup**: Inactive streams and clients auto-removed
- 📣 **Event System**: Real-time events and webhook notifications
- 🩺 **Health Checks**: Built-in health endpoints for monitoring
- 🏷️ **Custom Metadata**: Attach arbitrary key/value pairs to streams for identification

## Quick Start

#### 🐳 Docker compose example

Use the below example to run using the precompiled Dockerhub image.
You can also replace `latest` with `dev` or `experimental` to try another branch.

```yaml
services:
  m3u-proxy:
    image: sparkison/m3u-proxy:latest
    container_name: m3u-proxy
    ports:
      - "8085:8085"
    environment:
      # Server Configuration
      - M3U_PROXY_HOST=0.0.0.0
      - M3U_PROXY_PORT=8085
      - LOG_LEVEL=INFO
      
      # Timeouts (optional)
      - CLIENT_TIMEOUT=300
      - CLEANUP_INTERVAL=60
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8085/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 10s
```

## Building from source

### 1. Clone Repo & Install Dependencies

#### Prerequisits

- `python` installed on your system: `>=3.10`
- `pip` installed on your system: `>=23`

```bash
git clone https://github.com/sparkison/m3u-proxy.git && cd m3u-proxy
pip install -r requirements.txt
```

### 2. Start the Server

```bash
python main.py --debug
```

Server will start on `http://localhost:8085`

### 3. Create a Stream

```bash
# HLS stream with custom user agent
curl -X POST "http://localhost:8085/streams" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://your-stream.m3u8", "user_agent": "MyApp/1.0"}'

# Direct IPTV stream with failover
curl -X POST "http://localhost:8085/streams" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "http://server.com/stream.ts",
    "failover_urls": ["http://backup.com/stream.ts"],
    "user_agent": "VLC/3.0.18"
  }'

# Using the CLI client
python m3u_client.py create "https://your-stream.m3u8" --user-agent "MyApp/1.0"
python m3u_client.py create "http://server.com/movie.mkv" --failover "http://backup.com/movie.mkv"
```

## API Documentation

### Stream Management

#### Create Stream
```bash
POST /streams
Content-Type: application/json

{
  "url": "stream_url",
  "failover_urls": ["backup_url1", "backup_url2"],
  "user_agent": "Custom User Agent String"  
}
```

#### List Streams
```bash
GET /streams
```

#### Stream Information
```bash
GET /streams/{stream_id}
```

#### Delete Stream
```bash
DELETE /streams/{stream_id}
```

#### Trigger Failover
```bash
POST /streams/{stream_id}/failover
```

### Statistics & Monitoring

#### Comprehensive Stats
```bash
GET /stats
```

#### Health Check
```bash
GET /health
```

#### Client Information
```bash
GET /clients
GET /clients/{client_id}
```

## CLI Client Usage

The included CLI client (`m3u_client.py`) provides easy access to all proxy features:

```bash
# Create a stream with failover
python m3u_client.py create "https://primary.m3u8" --failover "https://backup1.m3u8" "https://backup2.m3u8"

# List all active streams
python m3u_client.py list

# View comprehensive statistics
python m3u_client.py stats

# Monitor in real-time (updates every 5 seconds)
python m3u_client.py monitor

# Check health status
python m3u_client.py health

# Get detailed stream information
python m3u_client.py info <stream_id>

# Trigger manual failover
python m3u_client.py failover <stream_id>

# Delete a stream
python m3u_client.py delete <stream_id>
```
## Configuration

### Environment Variables

```bash
# Server configuration
M3U_PROXY_HOST=0.0.0.0
M3U_PROXY_PORT=8085

# API Authentication (optional)
# Set API_TOKEN to require authentication for management endpoints
# Leave unset or empty to disable authentication
API_TOKEN=your_secret_token_here

# Client timeout (seconds)
CLIENT_TIMEOUT=300

# Cleanup interval (seconds)
CLEANUP_INTERVAL=60
```

### API Authentication

When `API_TOKEN` is set in the environment, all management endpoints require authentication via the `X-API-Token` header. This includes:

- `/` - Root endpoint
- `/streams` - Create, list, get, delete streams
- `/stats/*` - All statistics endpoints
- `/clients` - Client management
- `/health` - Health check endpoint
- `/webhooks` - Webhook management
- `/streams/{stream_id}/failover` - Failover control
- `/hls/{stream_id}/clients/{client_id}` - Client disconnect

**Stream endpoints (the actual streaming URLs) do NOT require authentication** since they are accessed by media players that identify streams via `stream_id`.

Example with authentication:

```bash
# Set your API token
export API_TOKEN="my_secret_token"

# Method 1: Using header (recommended for API calls)
curl -X POST "http://localhost:8085/streams" \
  -H "Content-Type: application/json" \
  -H "X-API-Token: my_secret_token" \
  -d '{"url": "https://your-stream.m3u8"}'

# Method 2: Using query parameter (useful for browser access)
curl -X POST "http://localhost:8085/streams?api_token=my_secret_token" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://your-stream.m3u8"}'

# Browser access example
# Visit: http://localhost:8085/stats?api_token=my_secret_token

# Without token - will get 401 error
curl -X POST "http://localhost:8085/streams" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://your-stream.m3u8"}'
```

To disable authentication, simply leave `API_TOKEN` unset or set it to an empty string.

### Server Startup Options

```python
# Main server with all features
python main.py

# With custom options
python main.py --port 8002 --debug --reload
```

## Architecture

### Core Design Philosophy

**Direct Per-Client Proxy** - Each client gets independent provider connections. No shared buffers, no buffering at all. True ephemeral architecture where provider connections exist only when actively serving a client.

### Key Components

1. **Stream Manager** (`src/stream_manager.py`)
   - **Per-Client Direct Proxy**: Independent provider connection per client
   - **Stream Type Detection**: Automatic HLS vs continuous stream identification
   - **Seamless Failover**: <100ms transparent URL switching with connection handoff
   - **Connection Pooling**: httpx client with optimized keepalive (20 connections)
   - **Automatic Cleanup**: Instant connection closure on client disconnect

2. **Stream Handling Approaches**

   **Continuous Streams** (.ts, .mp4, .mkv direct files):
   - Each client → Separate provider connection
   - Direct byte-for-byte streaming (StreamingResponse)
   - Zero buffering, zero shared state
   - Failover per-client without affecting others
   - Connection closes immediately when client stops

   **HLS Streams** (playlists and segments):
   - Playlist parsing and URL rewriting
   - Segment proxying with connection pooling
   - Efficient small-file handling
   - Real-time playlist modification

3. **FastAPI Application** (`src/api.py`)
   - RESTful endpoints for all operations
   - Client tracking and bandwidth monitoring
   - Statistics aggregation and reporting
   - Event emission for external monitoring

### Data Models

```python
# Client tracking
ClientInfo(
    client_id: str,
    stream_id: Optional[str],
    user_agent: str,
    ip_address: str,
    first_seen: datetime,
    last_seen: datetime,
    bytes_served: int,
    segments_served: int
)

# Stream information
StreamInfo(
    stream_id: str,
    original_url: str,
    current_url: str,
    failover_urls: List[str],
    client_count: int,
    total_bytes_served: int,
    total_segments_served: int,
    error_count: int,
    created_at: datetime,
    last_access: datetime
)
```

## Troubleshooting

### Common Issues

1. **Stream Won't Load**
   - Check original URL accessibility
   - Verify CORS headers if accessing from browser
   - Check server logs for detailed errors

2. **High Memory Usage**
   - Reduce `CLIENT_TIMEOUT` for faster cleanup
   - Monitor client connections and cleanup inactive ones
   - Consider horizontal scaling for high loads

3. **Failover Not Working**
   - Verify failover URLs are accessible
   - Check failover trigger conditions in logs
   - Test manual failover via API

### Debug Mode

```bash
# Enable detailed logging
export LOG_LEVEL=DEBUG
python main.py --debug
```

## Integration Examples

### HTML5 Video Player
```html
<video controls>
  <source src="http://localhost:8085/hls/{stream_id}/playlist.m3u8" type="application/x-mpegURL">
</video>
```

### FFmpeg
```bash
ffplay "http://localhost:8085/hls/{stream_id}/playlist.m3u8"
```

### VLC
```bash
vlc "http://localhost:8085/hls/{stream_id}/playlist.m3u8"
```

## 📡 Event System & Webhooks

The proxy includes a comprehensive event system for monitoring and integration:

### Webhook Configuration
```bash
# Add webhook to receive events
curl -X POST "http://localhost:8085/webhooks" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://your-server.com/webhook",
    "events": ["stream_started", "client_connected", "failover_triggered"],
    "timeout": 10,
    "retry_attempts": 3
  }'
```

### Available Events
- `stream_started` - New stream created
- `stream_stopped` - Stream ended
- `client_connected` - Client joined stream  
- `client_disconnected` - Client left stream
- `failover_triggered` - Switched to backup URL

### Webhook Payload Example
```json
{
  "event_id": "uuid",
  "event_type": "stream_started", 
  "stream_id": "abc123",
  "timestamp": "2025-09-25T22:38:34.392830",
  "data": {
    "primary_url": "http://example.com/stream.m3u8",
    "user_agent": "MyApp/1.0"
  }
}
```

### Demo Events
```bash
# Try the event system demo
python demo_events.py
```

📖 **Full Documentation**: See [EVENT_SYSTEM.md](docs/EVENT_SYSTEM.md) for complete webhook integration guide.

## Development

### Project Structure
```
├── src/
│   ├── stream_manager.py  # v2.0 Core: Per-client direct proxy
│   ├── api.py             # FastAPI server application
│   ├── models.py          # Data models and schemas
│   ├── config.py          # Configuration management
│   └── events.py          # Event system with webhooks
├── docs/
│   ├── ARCHITECTURE.md           # Architecture design overview
│   ├── EVENT_SYSTEM.md           # Webhook integration guide
│   └── TESTING.md                # Testing documentation
├── tests/                 # Test suite
│   ├── integration/       # Integration tests
│   └── test_*.py          # Unit tests
├── tools/                 # Utility scripts and tools
│   ├── performance_test.py # Performance testing
│   ├── m3u_client.py      # CLI client
│   ├── demo_events.py     # Event system demo
│   └── run_tests.py       # Enhanced test runner
├── main.py                # Server entry point (uvloop support)
└── README.md              # This file
```

### Contributing

1. Fork the repository
2. Create a feature branch
3. Add tests for new functionality
4. Submit a pull request

## License

MIT License - see LICENSE file for details.

## Credits

Built with FastAPI and inspired by MediaFlow Proxy. Designed for production IPTV streaming with emphasis on efficiency, correctness, and zero transcoding.

## Support

For issues, feature requests, or questions, please open a GitHub issue.
