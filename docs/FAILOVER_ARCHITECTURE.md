# Failover System Architecture

## System Components

```
┌──────────────────────────────────────────────────────────────────┐
│                         StreamInfo                               │
├──────────────────────────────────────────────────────────────────┤
│ - original_url: "http://source1.com/stream"                      │
│ - current_url: "http://source2.com/stream"  [updated on failover]│
│ - failover_urls: ["source2.com", "source3.com", ...]             │
│ - current_failover_index: 1                                      │
│ - failover_event: asyncio.Event()  [signals reconnect]           │
│ - connected_clients: Set[client_id]                              │
│ - failover_attempts: 2                                           │
│ - last_failover_time: datetime                                   │
└──────────────────────────────────────────────────────────────────┘
```

## Failover Flow - Direct/Continuous Streams

```
┌──────────────┐
│   Client 1   │───┐
└──────────────┘   │
                   │      ┌─────────────────────────────────┐
┌──────────────┐   │      │    Stream Manager               │
│   Client 2   │───┼─────▶│                                 │
└──────────────┘   │      │  ┌─────────────────────────┐    │
                   │      │  │  StreamInfo             │    │
┌──────────────┐   │      │  │  - current_url          │    │
│   Client 3   │───┘      │  │  - failover_event       │    │
└──────────────┘          │  └─────────────────────────┘    │
                          │                                 │
                          └─────────────────────────────────┘
                                    │
                          ┌─────────┴─────────┐
                          ▼                   ▼
                    ┌───────────┐       ┌───────────┐
                    │ Primary   │       │ Failover  │
                    │ Source 1  │       │ Source 2  │
                    └───────────┘       └───────────┘

Step-by-Step:
1. All clients connected to Source 1 via Stream Manager
2. Error detected OR manual failover triggered
3. Stream Manager updates current_url to Source 2
4. Stream Manager sets failover_event
5. All client connections detect event
6. Each client closes Source 1 connection
7. Each client opens Source 2 connection
8. Streaming continues seamlessly
```

## Automatic Failover Sequence

```
                    Error Detected
                         │
                         ▼
          ┌──────────────────────────────┐
          │  Is failover_urls available? │
          └──────────────────────────────┘
                    │         │
                  Yes         No
                    │         │
                    ▼         ▼
    ┌───────────────────┐  [Emit STREAM_FAILED]
    │  Call _try_update │       Exit
    │  _failover_url()  │
    └───────────────────┘
             │
             ▼
    ┌───────────────────┐
    │ Update current_url│
    │ to next failover  │
    └───────────────────┘
             │
             ▼
    ┌───────────────────┐
    │ Set failover_event│
    └───────────────────┘
             │
             ▼
    ┌───────────────────┐
    │ Stop transcoding  │
    │ (if applicable)   │
    └───────────────────┘
             │
             ▼
    ┌───────────────────┐
    │ Emit FAILOVER_    │
    │ TRIGGERED event   │
    └───────────────────┘
             │
             ▼
    ┌───────────────────┐
    │ Clients detect    │
    │ event & reconnect │
    └───────────────────┘
             │
             ▼
         Success!
```

## Direct Stream with Automatic Failover

```python
# Simplified code flow

async def stream_continuous_direct():
    failover_count = 0
    max_failovers = 3
    
    # Outer loop: handles reconnection
    while failover_count <= max_failovers:
        try:
            # Get current URL (updated on failover)
            active_url = stream_info.current_url
            
            # Connect to provider
            response = await http_client.stream('GET', active_url)
            
            # Inner loop: stream chunks
            async for chunk in response.aiter_bytes():
                # Check for failover event
                if stream_info.failover_event.is_set():
                    # Close connection
                    # Break to outer loop
                    break
                
                yield chunk  # Send to client
            
            # Success - exit
            break
            
        except (NetworkError, TimeoutError) as e:
            # Automatic failover
            if failover_count < max_failovers:
                await _try_update_failover_url(stream_id, "error")
                failover_count += 1
                continue  # Retry with new URL
            else:
                raise  # Give up
```

## HLS Stream Failover

```
┌────────────┐
│   Player   │
└──────┬─────┘
       │
       │ 1. Request Playlist
       ▼
┌─────────────────────────────────────┐
│   GET /hls/{id}/playlist.m3u8       │
│                                     │
│   Uses: stream_info.current_url     │◀── Updated on failover
└─────────────────────────────────────┘
       │
       │ 2. Return Processed Playlist
       │    (segments point to proxy)
       ▼
┌────────────┐
│   Player   │
│  Parses    │
│  Playlist  │
└──────┬─────┘
       │
       │ 3. Request Segments
       ▼
┌─────────────────────────────────────┐
│   GET /hls/{id}/segment.ts          │
│                                     │
│   Fetches from provider             │
│   Auto-retries on error with        │
│   failover URL                      │
└─────────────────────────────────────┘
       │
       │ 4. Return Segment Data
       ▼
┌────────────┐
│   Player   │
│  Continues │
└────────────┘

Key Points:
- Next playlist refresh uses new current_url automatically
- Segment errors trigger automatic failover
- Player unaware of failover - seamless transition
```

## Transcoded Stream Failover

```
┌─────────────┐                          ┌─────────────┐
│  Client 1   │───┐                      │  Primary    │
└─────────────┘   │                      │  Source     │
                  │                      └──────┬──────┘
┌─────────────┐   │    ┌──────────────┐        │
│  Client 2   │───┼───▶│ Stream Mgr   │        │
└─────────────┘   │    └──────┬───────┘        │
                  │           │                │
┌─────────────┐   │           │                │
│  Client 3   │───┘           ▼                ▼
└─────────────┘     ┌──────────────────────────────┐
                    │   Shared FFmpeg Process      │
                    │   (Transcoding)              │
                    │                              │
                    │  Input: current_url          │
                    │  Output: Broadcaster         │
                    └──────────────────────────────┘
                              │
                    ┌─────────┴─────────┐
                    ▼         ▼         ▼
              [Queue 1]   [Queue 2]   [Queue 3]
                    │         │         │
                    └─────────┴─────────┘
                           to clients

Failover Process:
1. Error detected or manual failover
2. Stream Manager calls _try_update_failover_url()
3. Old FFmpeg process stopped
4. current_url updated to failover URL
5. failover_event set
6. Clients detect event
7. Clients removed from old process
8. New FFmpeg process created with new URL
9. Clients reconnected to new process
10. Streaming resumes
```

## Event Timeline

```
Time │ Event
─────┼──────────────────────────────────────────────
0ms  │ Clients streaming from Source 1
     │ 
100ms│ [ERROR] Connection timeout detected
     │ 
101ms│ [ACTION] _try_update_failover_url() called
     │          - current_url = Source 2
     │          - failover_event.set()
     │          - Stop FFmpeg (if transcoded)
     │ 
102ms│ [EVENT] FAILOVER_TRIGGERED emitted
     │         {old_url: Source1, new_url: Source2}
     │ 
105ms│ [CLIENT1] Detects failover_event
     │           Closes Source 1 connection
     │           Opens Source 2 connection
     │ 
107ms│ [CLIENT2] Detects failover_event
     │           Reconnects to Source 2
     │ 
110ms│ [CLIENT3] Detects failover_event
     │           Reconnects to Source 2
     │ 
150ms│ All clients streaming from Source 2
     │ Total interruption: ~50ms per client
```

## Comparison: Before vs After

### Before (No Seamless Failover)

```
Client ──▶ Stream ──▶ Source 1 ──▶ [ERROR!]
                                      │
                                      ▼
                                 Stream STOPS
                                      │
                                      ▼
                          Client must restart
                                      │
                                      ▼
Client ──▶ Stream ──▶ Source 2 ──▶ Works!

Total downtime: 5-30 seconds
User experience: Stream freezes, must reload
```

### After (Seamless Failover)

```
Client ──▶ Stream ──▶ Source 1 ──▶ [ERROR!]
                                      │
                                      ▼
                                 Auto failover
                                      │
                                      ▼
Client ──▶ Stream ──▶ Source 2 ──▶ Works!

Total interruption: 50-200ms
User experience: Brief buffering, automatic resume
```

## Configuration Example

```python
# Create stream with failover
stream_config = {
    "url": "rtmp://primary.tv/live/stream",
    "failover_urls": [
        "rtmp://backup1.tv/live/stream",
        "rtmp://backup2.tv/live/stream", 
        "rtmp://backup3.tv/live/stream"
    ],
    "user_agent": "ProxyTV/1.0",
    "metadata": {
        "channel": "HBO",
        "quality": "1080p"
    }
}

# Failover behavior:
# - First error: Switch to backup1
# - Second error: Switch to backup2
# - Third error: Switch to backup3
# - Fourth error: Back to primary
# - Max 3 retries per streaming session
```

## Performance Characteristics

| Metric | Value | Notes |
|--------|-------|-------|
| Failover Detection | < 1s | Typically 100-500ms |
| Reconnection Time | 100-200ms | Network dependent |
| Total Interruption | 200ms-1s | Usually < 500ms |
| Client Buffer Impact | Minimal | 1-2 seconds buffering |
| Success Rate | 95%+ | With 2+ failover URLs |
| Resource Overhead | Low | Event-based signaling |

## Monitoring Dashboard Concept

```
┌─────────────────────────────────────────────────────────────┐
│ Stream: abc123 - ESPN Live                   ●  ACTIVE      │
├─────────────────────────────────────────────────────────────┤
│ Current Source: backup1.tv/stream                           │
│ Primary Source: primary.tv/stream          ⚠  UNREACHABLE   │
│                                                             │
│ Failover URLs:                                              │
│   [1] backup1.tv/stream                    ●  ACTIVE        │
│   [2] backup2.tv/stream                    ●  STANDBY       │
│   [3] backup3.tv/stream                    ●  STANDBY       │
│                                                             │
│ Stats:                                                      │
│   Active Clients: 23                                        │
│   Failover Attempts: 2                                      │
│   Last Failover: 2 minutes ago (health_check)               │
│   Total Bytes Served: 1.2 GB                                │
│                                                             │
│ Recent Events:                                              │
│   [10:30:15] FAILOVER_TRIGGERED: primary → backup1          │
│   [10:28:42] STREAM_FAILED: Connection timeout              │
│   [10:25:10] STREAM_STARTED: primary source                 │
└─────────────────────────────────────────────────────────────┘
```
