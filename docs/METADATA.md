# Custom Stream Metadata Feature

## Overview
The m3u-proxy now supports custom metadata for streams, allowing external players and applications to attach arbitrary key/value pairs to streams. This metadata is preserved and returned in all stream queries, making it easy to track and identify streams with your own internal identifiers and attributes.

## Use Cases

### 1. **Channel Identification**
Associate your local channel IDs with proxy streams:
```json
{
  "metadata": {
    "local_id": "channel_123",
    "channel_name": "HBO HD",
    "channel_number": "201"
  }
}
```

### 2. **Content Classification**
Tag streams with categories and attributes:
```json
{
  "metadata": {
    "category": "sports",
    "subcategory": "soccer",
    "league": "premier_league",
    "quality": "1080p",
    "language": "en"
  }
}
```

### 3. **Business Logic**
Add custom fields for your application logic:
```json
{
  "metadata": {
    "subscription_tier": "premium",
    "region": "us-east",
    "content_rating": "pg-13",
    "expiry_date": "2025-12-31"
  }
}
```

### 4. **Multi-Source Tracking**
Identify which external source provided the stream:
```json
{
  "metadata": {
    "provider": "provider_a",
    "provider_stream_id": "ext_12345",
    "last_updated": "2025-10-05"
  }
}
```

## API Usage

### Creating a Stream with Metadata

**Endpoint:** `POST /streams`

**Request Body:**
```json
{
  "url": "https://example.com/stream.m3u8",
  "user_agent": "MyPlayer/1.0",
  "failover_urls": ["https://backup.example.com/stream.m3u8"],
  "metadata": {
    "local_id": "stream_123",
    "channel_name": "HBO HD",
    "channel_number": "201",
    "category": "movies",
    "quality": "1080p",
    "language": "en"
  }
}
```

**Response:**
```json
{
  "stream_id": "a1b2c3d4e5f6...",
  "primary_url": "https://example.com/stream.m3u8",
  "failover_urls": ["https://backup.example.com/stream.m3u8"],
  "user_agent": "MyPlayer/1.0",
  "stream_type": "hls",
  "stream_endpoint": "/hls/a1b2c3d4e5f6.../playlist.m3u8",
  "playlist_url": "/hls/a1b2c3d4e5f6.../playlist.m3u8",
  "direct_url": "/hls/a1b2c3d4e5f6.../playlist.m3u8",
  "message": "Stream created successfully (hls)",
  "metadata": {
    "local_id": "stream_123",
    "channel_name": "HBO HD",
    "channel_number": "201",
    "category": "movies",
    "quality": "1080p",
    "language": "en"
  }
}
```

### Retrieving Streams with Metadata

**Endpoint:** `GET /streams`

**Response:**
```json
{
  "streams": [
    {
      "stream_id": "a1b2c3d4e5f6...",
      "original_url": "https://example.com/stream.m3u8",
      "current_url": "https://example.com/stream.m3u8",
      "user_agent": "MyPlayer/1.0",
      "client_count": 2,
      "total_bytes_served": 1048576,
      "total_segments_served": 100,
      "error_count": 0,
      "is_active": true,
      "has_failover": true,
      "stream_type": "HLS",
      "created_at": "2025-10-05T14:30:00.000000",
      "last_access": "2025-10-05T14:35:00.000000",
      "metadata": {
        "local_id": "stream_123",
        "channel_name": "HBO HD",
        "channel_number": "201",
        "category": "movies",
        "quality": "1080p",
        "language": "en"
      }
    }
  ],
  "total": 1
}
```

### Filtering Streams by Metadata

#### Server-Side Filtering (Recommended)

**Endpoint:** `GET /streams/by-metadata`

The most efficient way to filter streams is using the server-side filtering endpoint:

**Parameters:**
- `field` (required): The metadata field to filter by
- `value` (required): The value to match
- `active_only` (optional, default: true): Only return streams with active clients

**Example Requests:**

```bash
# Get all streams with local_id = "stream_123"
curl "http://localhost:8085/streams/by-metadata?field=local_id&value=stream_123"

# Get all sports streams (active only)
curl "http://localhost:8085/streams/by-metadata?field=category&value=sports&active_only=true"

# Get all streams from a specific provider (including inactive)
curl "http://localhost:8085/streams/by-metadata?field=provider&value=provider_a&active_only=false"
```

**Response Format:**
```json
{
  "filter": {
    "field": "category",
    "value": "sports"
  },
  "active_only": true,
  "matching_streams": [
    {
      "stream_id": "a1b2c3d4e5f6...",
      "client_count": 2,
      "metadata": {
        "local_id": "espn_hd",
        "category": "sports",
        "channel_name": "ESPN HD"
      },
      "last_access": "2025-10-05T14:35:00.000000",
      "is_active": true,
      "url": "https://example.com/espn.m3u8",
      "stream_type": "HLS"
    }
  ],
  "total_matching": 1,
  "total_clients": 2
}
```

#### Client-Side Filtering (Alternative)

You can also retrieve all streams and filter by metadata in your application:

```python
import requests

# Get all streams
response = requests.get("http://localhost:8085/streams")
streams = response.json()["streams"]

# Filter by local_id
my_stream = next(
    (s for s in streams if s.get("metadata", {}).get("local_id") == "stream_123"),
    None
)

# Filter by category
sports_streams = [
    s for s in streams 
    if s.get("metadata", {}).get("category") == "sports"
]

# Filter by channel number
channel_201 = next(
    (s for s in streams if s.get("metadata", {}).get("channel_number") == "201"),
    None
)
```

#### Python Examples Using Server-Side Filtering

```python
import requests

def get_streams_by_metadata(field, value, active_only=True, base_url="http://localhost:8085"):
    """Get streams filtered by metadata field/value"""
    response = requests.get(
        f"{base_url}/streams/by-metadata",
        params={
            "field": field,
            "value": value,
            "active_only": active_only
        }
    )
    
    if response.status_code == 200:
        return response.json()
    else:
        raise Exception(f"Failed to get streams: {response.text}")

# Usage examples
# Get specific channel by local ID
hbo_data = get_streams_by_metadata("local_id", "hbo_hd")
hbo_streams = hbo_data["matching_streams"]

# Get all active sports streams
sports_data = get_streams_by_metadata("category", "sports", active_only=True)
print(f"Found {sports_data['total_matching']} sports streams with {sports_data['total_clients']} total clients")

# Get all streams from a provider (including inactive)
provider_data = get_streams_by_metadata("provider", "provider_a", active_only=False)
```

## Metadata Specification

### Field Requirements
- **Keys:** Must be strings
- **Values:** Can be strings, integers, floats, or booleans
- **Storage:** All values are automatically converted to strings for consistency
- **Size:** Recommended to keep metadata reasonably small (< 1KB per stream)

### Data Type Conversion
All metadata values are converted to strings for consistency:

```python
# Input
{
  "count": 42,              # integer
  "active": True,           # boolean
  "quality": "1080p",       # string
  "rating": 4.5             # float
}

# Stored/Returned as
{
  "count": "42",
  "active": "True",
  "quality": "1080p",
  "rating": "4.5"
}
```

### Validation
The API validates metadata before accepting it:

✅ **Valid:**
```json
{
  "metadata": {
    "key1": "string value",
    "key2": 123,
    "key3": true,
    "key4": 45.67
  }
}
```

❌ **Invalid:**
```json
{
  "metadata": {
    "key1": ["array", "not", "allowed"],
    "key2": {"nested": "objects not allowed"}
  }
}
```

## Example: Integration with IPTV Player

```python
import requests

class IPTVProxy:
    def __init__(self, proxy_url):
        self.proxy_url = proxy_url
        self.channel_map = {}  # Maps local IDs to proxy stream IDs
    
    def add_channel(self, channel_id, channel_name, stream_url, channel_number=None):
        """Add a channel to the proxy with metadata"""
        metadata = {
            "local_id": channel_id,
            "channel_name": channel_name,
            "added_at": datetime.now().isoformat()
        }
        
        if channel_number:
            metadata["channel_number"] = str(channel_number)
        
        response = requests.post(
            f"{self.proxy_url}/streams",
            json={
                "url": stream_url,
                "metadata": metadata
            }
        )
        
        if response.status_code == 200:
            data = response.json()
            stream_id = data["stream_id"]
            self.channel_map[channel_id] = stream_id
            return data["stream_endpoint"]
        else:
            raise Exception(f"Failed to add channel: {response.text}")
    
    def get_channel_by_local_id(self, channel_id):
        """Get channel info by your local ID using server-side filtering"""
        response = requests.get(
            f"{self.proxy_url}/streams/by-metadata",
            params={
                "field": "local_id",
                "value": channel_id,
                "active_only": False
            }
        )
        
        if response.status_code == 200:
            data = response.json()
            streams = data.get("matching_streams", [])
            return streams[0] if streams else None
        
        return None
    
    def get_channels_by_category(self, category, active_only=True):
        """Get all channels in a category using server-side filtering"""
        response = requests.get(
            f"{self.proxy_url}/streams/by-metadata",
            params={
                "field": "category", 
                "value": category,
                "active_only": active_only
            }
        )
        
        if response.status_code == 200:
            data = response.json()
            return data.get("matching_streams", [])
        
        return []
    
    def get_active_client_count_by_category(self, category):
        """Get total active clients for a category"""
        response = requests.get(
            f"{self.proxy_url}/streams/by-metadata",
            params={
                "field": "category",
                "value": category,
                "active_only": True
            }
        )
        
        if response.status_code == 200:
            data = response.json()
            return data.get("total_clients", 0)
        
        return 0

# Usage
proxy = IPTVProxy("http://localhost:8085")

# Add channels with metadata
hbo_endpoint = proxy.add_channel(
    channel_id="ch_hbo",
    channel_name="HBO HD",
    stream_url="https://provider.com/hbo.m3u8",
    channel_number=201
)

# Later, retrieve by your ID
hbo_info = proxy.get_channel_by_local_id("ch_hbo")
print(f"HBO has {hbo_info['client_count']} viewers")
```

## cURL Examples

### Create with metadata:
```bash
curl -X POST "http://localhost:8085/streams" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com/hbo.m3u8",
    "metadata": {
      "local_id": "hbo_hd",
      "channel_name": "HBO HD",
      "channel_number": "201"
    }
  }'
```

### Server-side filtering by metadata:
```bash
# Get specific stream by local_id
curl "http://localhost:8085/streams/by-metadata?field=local_id&value=hbo_hd"

# Get all sports streams (active only)
curl "http://localhost:8085/streams/by-metadata?field=category&value=sports&active_only=true"

# Get all streams by provider (including inactive)
curl "http://localhost:8085/streams/by-metadata?field=provider&value=provider_a&active_only=false"

# Get total client count for a category
curl -s "http://localhost:8085/streams/by-metadata?field=category&value=sports" | jq '.total_clients'
```

### Get all streams with metadata:
```bash
curl "http://localhost:8085/streams" | jq '.streams[] | {stream_id, metadata}'
```

### Client-side filtering using jq:
```bash
# Get stream with specific local_id (client-side filtering)
curl -s "http://localhost:8085/streams" | \
  jq '.streams[] | select(.metadata.local_id == "hbo_hd")'

# Get all sports channels (client-side filtering)
curl -s "http://localhost:8085/streams" | \
  jq '.streams[] | select(.metadata.category == "sports")'
```

## Best Practices

1. **Use Server-Side Filtering:** Use the `/streams/by-metadata` endpoint instead of client-side filtering for better performance
2. **Use Consistent Keys:** Define a standard set of metadata keys across your application
3. **Include Identifiers:** Always include a unique identifier like `local_id`
4. **Keep It Simple:** Store only what you need; avoid large nested structures
5. **Document Your Schema:** Maintain documentation of your metadata structure
6. **Version Your Metadata:** Consider including a `metadata_version` field for future changes
7. **Filter Efficiently:** Use `active_only=true` when you only need streams with connected clients

## Migration Guide

If you have existing code that creates streams without metadata, it will continue to work:

```python
# Old code - still works
requests.post("/streams", json={"url": "..."})

# New code - with metadata
requests.post("/streams", json={
    "url": "...",
    "metadata": {"local_id": "..."}
})
```

Streams without metadata will have an empty `metadata` object `{}` in responses.

## Limitations

- Metadata is stored in memory and will be lost on restart (like all stream data)
- Maximum recommended size per metadata object: 1KB
- Values are converted to strings automatically
- Nested objects and arrays are not supported
- Metadata is not indexed for searching (filter in your application)

## Future Enhancements

Potential future features:
- Persistent metadata storage
- Server-side metadata filtering
- Metadata update endpoint
- Bulk metadata queries
- Metadata validation schemas
