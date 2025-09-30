# m3u-proxy

A high-performance HTTP proxy server for IPTV content with client management, statistics tracking, and failover support. Built with FastAPI and inspired by MediaFlow Proxy.

## Features

### Core Streaming
- 🚀 **Pure HTTP Proxy**: No transcoding, direct byte-range streaming
- 📺 **HLS Support**: Handles master playlists, media playlists, and segments (.m3u8)
- 📡 **IPTV Support**: Direct streaming of .ts, .mp4, .mkv, .webm, .avi files
- 🔄 **Real-time URL Rewriting**: Automatic playlist modification for proxied content
- 📱 **Byte-range Support**: Full support for VOD streams with byte-range requests

### Enterprise Features
- 👥 **Client Management**: Track and manage individual client sessions
- 📊 **Comprehensive Statistics**: Real-time metrics on streams, clients, and data usage
- 🔄 **Failover Support**: Automatic and manual failover between multiple stream URLs
- 🎯 **Stream Isolation**: Each stream gets a unique ID and isolated statistics
- 🧹 **Automatic Cleanup**: Inactive streams and clients are automatically cleaned up
- 🎯 **Event System**: Real-time events and webhook notifications for stream lifecycle

### API Features
- 🌐 **RESTful API**: Complete REST API for stream and client management
- 📈 **Real-time Stats**: Live statistics endpoints for monitoring
- 🎛️ **Manual Controls**: Trigger failover, manage streams, and view detailed info
- 💚 **Health Checks**: Built-in health endpoints for monitoring
- 📡 **Webhook Integration**: Send events to external systems via webhooks

## Quick Start

### 1. Install Dependencies

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

### 4. Access Your Stream

For **HLS streams** (.m3u8):
```
http://localhost:8085/hls/{stream_id}/playlist.m3u8
```

For **Direct streams** (.ts, .mp4, .mkv, etc.):
```
http://localhost:8085/stream/{stream_id}
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

# Client timeout (seconds)
CLIENT_TIMEOUT=300

# Cleanup interval (seconds)
CLEANUP_INTERVAL=60
```

### Server Startup Options

```python
# Main server with all features
python main.py

# With custom options
python main.py --port 8002 --debug --reload
```

## Architecture

### Components

1. **Stream Manager** (`src/stream_manager.py`)
   - Client session tracking
   - Stream statistics and management
   - Failover logic and URL management
   - Automatic cleanup tasks

2. **M3U8 Processor**
   - Real-time playlist parsing and modification
   - URL rewriting for segments and initialization maps
   - Master/media playlist detection

3. **FastAPI Application** (`src/api.py`)
   - RESTful endpoints for all operations
   - Client registration and management
   - Statistics aggregation and reporting

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

## Use Cases

### Development & Testing
- Test HLS streams without complex server setup
- Debug playlist issues with real-time URL rewriting
- Monitor client behavior and stream performance

### Production Streaming
- Serve HLS content with failover protection
- Track viewer statistics and engagement
- Load balance across multiple stream sources

### Content Delivery
- Proxy remote HLS streams for local delivery
- Add analytics to existing streaming infrastructure
- Implement custom authentication and access control

## Performance

### Benchmarks (Single Process)
- **Throughput**: ~100 concurrent clients per process
- **Latency**: <10ms proxy overhead
- **Memory**: ~50MB base + ~1KB per active client
- **CPU**: Minimal overhead, I/O bound operations

### Scaling
- Horizontal scaling with multiple processes/containers
- Shared statistics through external storage (Redis/DB)
- Load balancer friendly with health checks

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

📖 **Full Documentation**: See [EVENT_SYSTEM.md](EVENT_SYSTEM.md) for complete webhook integration guide.

## Development

### Project Structure
```
├── src/
│   ├── stream_manager.py  # Core stream and client management
│   ├── api.py             # FastAPI server application
│   ├── models.py          # Data models and schemas
│   ├── config.py          # Configuration management
│   └── events.py          # Event system
├── tests/                 # Test suite
│   ├── integration/       # Integration tests
│   └── test_*.py          # Unit tests
├── tools/                 # Utility scripts and tools
│   ├── performance_test.py # Performance testing
│   ├── m3u_client.py      # CLI client
│   ├── demo_events.py     # Event system demo
│   └── run_tests.py       # Enhanced test runner
├── main.py                # Server entry point
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

Inspired by MediaFlow Proxy and designed for production HLS streaming scenarios.

## Support

For issues, feature requests, or questions, please open a GitHub issue.
