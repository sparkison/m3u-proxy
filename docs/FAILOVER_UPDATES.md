# Failover System Improvements

## Overview

The failover system has been completely redesigned to support **seamless automatic failover** for all streaming methods (HLS, Direct/Continuous, and Transcoded) without requiring clients to stop and restart their streams.

## Key Changes

### 1. Stream-Wide Failover Event System

**Added to `StreamInfo` dataclass:**
- `failover_event: asyncio.Event` - Signals all connected clients when failover occurs

**How it works:**
- When failover is triggered (manually or automatically), the event is set
- All active streaming connections check this event periodically
- When detected, connections close gracefully and reconnect using the new URL
- Seamless transition without interrupting playback

### 2. Enhanced `_try_update_failover_url()` Method

**Previous behavior:**
- Only updated `current_url` to next failover URL
- Did NOT notify active connections
- Required stream restart to use new URL

**New behavior:**
```python
async def _try_update_failover_url(self, stream_id: str, reason: str = "manual") -> bool:
    # 1. Update current_url to next failover URL
    # 2. Set failover_event to signal all clients
    # 3. For transcoded streams, stop old FFmpeg process
    # 4. Emit FAILOVER_TRIGGERED event with comprehensive data
    # 5. Clear event after brief delay
```

**Triggers failover for:**
- Manual API calls (`/streams/{stream_id}/failover`)
- Health check failures
- Connection errors during streaming
- Stream errors and timeouts

### 3. Direct/Continuous Stream Failover

**Location:** `stream_continuous_direct()` method

**Implementation:**
- Wrapped streaming logic in `while failover_count <= max_failovers:` loop
- Checks `failover_event.is_set()` during streaming
- On event detection: closes current connection, breaks inner loop
- Outer loop reconnects using updated `current_url`
- Automatic failover on:
  - `httpx.ReadError` (connection dropped)
  - `httpx.TimeoutException` (timeout)
  - `httpx.NetworkError` (network issues)
  - `httpx.HTTPError` (HTTP errors)
  - Generic exceptions

**Example flow:**
1. Client streaming from URL A
2. Connection error occurs OR manual failover triggered
3. System updates to URL B and sets failover_event
4. Streaming loop detects event
5. Closes connection to URL A
6. Opens connection to URL B
7. Continues streaming seamlessly

### 4. HLS Stream Failover

**Playlist Fetching:**
- Automatically uses `current_url` which updates on failover
- Next playlist refresh automatically uses new URL
- No changes needed - works automatically!

**Segment Fetching Enhancement:**
- Added retry logic with automatic failover in `proxy_hls_segment()`
- On segment fetch error:
  - Triggers failover to next URL
  - Retries segment fetch
  - Falls back to error after max retries (3)

**Implementation:**
```python
retry_count = 0
max_retries = 3

while retry_count <= max_retries:
    try:
        # Fetch and stream segment
    except (TimeoutException, NetworkError, HTTPError) as e:
        if stream_info.failover_urls and retry_count < max_retries:
            await self._try_update_failover_url(stream_id, "segment_fetch_error")
            retry_count += 1
            continue
        else:
            raise
```

### 5. Transcoded Stream Failover

**Location:** `stream_transcoded()` method

**Implementation:**
- Wrapped streaming logic in `while failover_count <= max_failovers:` loop
- Checks `failover_event.is_set()` during streaming
- On event detection:
  1. Removes client from old shared FFmpeg process
  2. Breaks inner loop
  3. Outer loop creates new FFmpeg process with new URL
  4. Reconnects client to new process
  5. Continues streaming seamlessly

**Automatic failover triggers:**
- FFmpeg process creation failure
- FFmpeg process exits unexpectedly
- Connection errors during streaming
- HTTPException, ConnectionError, BrokenPipeError

**Note:** When failover is triggered via `_try_update_failover_url()`, it automatically stops the old FFmpeg process:
```python
if stream_info.is_transcoded and self.pooled_manager:
    if stream_info.transcode_stream_key:
        await self.pooled_manager.force_stop_stream(stream_info.transcode_stream_key)
```

### 6. Enhanced Manual Failover API

**Endpoint:** `POST /streams/{stream_id}/failover`

**Previous Response:**
```json
{
  "message": "Failover successful",
  "new_url": "...",
  "failover_index": 1
}
```

**New Response:**
```json
{
  "message": "Failover triggered successfully - all clients will seamlessly reconnect",
  "new_url": "http://failover-url.com/stream",
  "failover_index": 1,
  "failover_attempts": 1,
  "active_clients": 3,
  "stream_type": "Live Continuous"
}
```

**Behavior:**
- Validates failover URLs exist
- Calls `_try_update_failover_url(stream_id, "manual")`
- Signals ALL active clients to reconnect
- Returns comprehensive status
- Works for all stream types (HLS, Direct, Transcoded)

### 7. Automatic Failover on Errors

All streaming methods now automatically attempt failover when errors occur:

**Direct/Continuous Streams:**
- Network errors → automatic failover
- Timeouts → automatic failover
- Connection drops → automatic failover
- Up to 3 failover attempts before giving up

**HLS Streams:**
- Segment fetch errors → automatic failover
- Playlist fetch uses updated URL automatically
- Up to 3 retry attempts per segment

**Transcoded Streams:**
- FFmpeg startup failure → automatic failover
- FFmpeg process crash → automatic failover
- Connection errors → automatic failover
- Up to 3 failover attempts before giving up

## Testing Recommendations

### 1. Manual Failover Test
```bash
# Start streaming
curl "http://proxy:8085/stream/{stream_id}?client_id=test1" > /dev/null &

# Trigger manual failover
curl -X POST "http://proxy:8085/streams/{stream_id}/failover" \
  -H "Authorization: Bearer {token}"

# Verify stream continues without restart
# Check logs for "Failover detected" and "reconnecting"
```

### 2. Automatic Failover Test (Network Error)
```bash
# Create stream with failover URLs
curl -X POST "http://proxy:8085/streams" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "http://bad-url.com/stream.m3u8",
    "failover_urls": [
      "http://good-url.com/stream.m3u8",
      "http://backup-url.com/stream.m3u8"
    ]
  }'

# Start streaming - should automatically failover to good URL
curl "http://proxy:8085/stream/{stream_id}" > /dev/null
```

### 3. HLS Failover Test
```bash
# Create HLS stream with failovers
curl -X POST "http://proxy:8085/streams" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "http://source1.com/playlist.m3u8",
    "failover_urls": ["http://source2.com/playlist.m3u8"]
  }'

# Start playing HLS stream
ffplay "http://proxy:8085/hls/{stream_id}/playlist.m3u8?client_id=test1"

# Trigger failover while playing
curl -X POST "http://proxy:8085/streams/{stream_id}/failover"

# Stream should continue without interruption
```

### 4. Transcoded Stream Failover Test
```bash
# Create transcoded stream with failovers
curl -X POST "http://proxy:8085/transcode" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "http://source1.com/stream.ts",
    "failover_urls": ["http://source2.com/stream.ts"],
    "profile": "720p"
  }'

# Start transcoded streaming
ffplay "http://proxy:8085/stream/{stream_id}"

# Trigger failover while transcoding
curl -X POST "http://proxy:8085/streams/{stream_id}/failover"

# Verify: old FFmpeg stopped, new FFmpeg started, client reconnected
```

### 5. Continuous Failover Cycling Test
```bash
# Create stream with multiple failovers
curl -X POST "http://proxy:8085/streams" \
  -d '{
    "url": "http://source1.com/stream",
    "failover_urls": [
      "http://source2.com/stream",
      "http://source3.com/stream",
      "http://source4.com/stream"
    ]
  }'

# Trigger multiple failovers in sequence
for i in {1..3}; do
  sleep 5
  curl -X POST "http://proxy:8085/streams/{stream_id}/failover"
  echo "Failover $i triggered"
done

# Should cycle through all failover URLs
```

## Event Logging

All failover events are logged with comprehensive information:

```
INFO: Failover triggered for stream abc123 (reason: manual): http://old.com -> http://new.com
INFO: Stopping transcoding process for failover: transcode_key_xyz
INFO: Failover detected for stream abc123, reconnecting client client1
INFO: Reconnecting client client1 to failover URL: http://new.com
```

Webhook events are also sent with type `FAILOVER_TRIGGERED`:
```json
{
  "event_type": "FAILOVER_TRIGGERED",
  "stream_id": "abc123",
  "data": {
    "old_url": "http://source1.com/stream",
    "new_url": "http://source2.com/stream",
    "failover_index": 1,
    "attempt_number": 1,
    "reason": "manual",
    "client_count": 3
  }
}
```

## Benefits

1. **Zero Downtime:** Clients never need to stop/restart - failover is seamless
2. **Automatic Recovery:** Streams automatically failover on errors without manual intervention
3. **Manual Control:** Can force failover via API when needed (e.g., freezing stream)
4. **Universal:** Works for all streaming types (HLS, Direct, Transcoded)
5. **Resilient:** Up to 3 automatic retry attempts before giving up
6. **Transparent:** Comprehensive logging and webhook events for monitoring

## Limitations

1. **Brief Interruption:** Small delay (typically < 1 second) during reconnection
2. **Retry Limit:** Max 3 failover attempts per streaming session
3. **HLS Segments:** Old segments may fail but player will request new ones
4. **Buffer Discontinuity:** Some players may show brief buffering during transition

## Future Enhancements

1. **Health-based Selection:** Automatically select best failover based on latency/health
2. **Smart Caching:** Cache last N seconds before failover for seamless transition
3. **Client-side Fallback:** Return failover URLs to clients for client-side failover
4. **Load Balancing:** Distribute clients across multiple failover URLs
5. **Predictive Failover:** Failover before complete failure based on degrading metrics
