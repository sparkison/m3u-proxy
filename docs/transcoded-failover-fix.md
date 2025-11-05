# Transcoded Stream Failover Fix

## Problem

When triggering manual failover for a transcoded stream via the API endpoint `POST /streams/{stream_id}/failover`:

1. The old FFmpeg process was correctly stopped
2. The `failover_event` was set to notify clients
3. **BUT** the client connection was being cleaned up in a `finally` block before it could reconnect
4. This caused the stream to stop completely instead of seamlessly failing over to the new URL

## Root Cause

The issue was in the `stream_transcoded()` generator function. The code structure was:

```python
while failover_count <= max_failovers:
    try:
        # streaming logic
    except Exception:
        # error handling
    finally:
        # This was running on EVERY iteration
        await self.pooled_manager.remove_client_from_stream(client_id)

# This cleanup was never reached during failover
await self.cleanup_client(client_id)
```

The `finally` block was executing after every loop iteration, including when we wanted to retry with a failover URL. This removed the client from the stream pool before it could reconnect.

## Solution

Moved the cleanup logic outside the `while` loop so it only executes after all retries are exhausted or the stream completes normally:

```python
while failover_count <= max_failovers:
    try:
        # streaming logic with failover detection
        if stream_info.failover_event.is_set():
            # Clean up current connection
            await self.pooled_manager.remove_client_from_stream(client_id)
            stream_key = None  # Prevent double cleanup
            failover_count += 1
            break  # Reconnect with new URL
    except Exception:
        # error handling
        break  # Don't retry on unexpected exceptions

# Final cleanup after loop completes
if client_id and stream_key and self.pooled_manager:
    await self.pooled_manager.remove_client_from_stream(client_id)
await self.cleanup_client(client_id)
```

## Changes Made

### 1. Removed `finally` Block
- Moved cleanup code outside the `while` loop
- Prevents premature client disconnection during failover attempts

### 2. Added Selective Cleanup in Failover Branch
- When `failover_event` is detected, we explicitly clean up the current stream
- Set `stream_key = None` to prevent double cleanup later
- This ensures the old FFmpeg process is released before creating a new one

### 3. Enhanced Logging
- Added log when starting a failover attempt with the new URL
- Added log when removing client from old stream
- Added log when detecting failover event with new URL
- This makes debugging much easier

### 4. Better Exception Handling
- Unexpected exceptions now break the loop instead of raising
- Prevents stack traces from propagating while still stopping retries

## Expected Behavior Now

When manual failover is triggered:

```
1. API receives: POST /streams/{stream_id}/failover
2. _try_update_failover_url() is called:
   - Updates current_url to next failover URL
   - Stops old FFmpeg process
   - Sets failover_event
3. Client's streaming generator detects failover_event
4. Client removes itself from old stream pool
5. Inner loop breaks, outer loop continues
6. New iteration starts with updated current_url
7. New FFmpeg process created with failover URL
8. Client reconnects to new process
9. Streaming resumes seamlessly
```

## Testing

Test with the following command sequence:

```bash
# 1. Start a transcoded stream with failover URLs
curl -X POST "http://localhost:8085/transcode" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "http://source1.com/stream.ts",
    "failover_urls": [
      "http://source2.com/stream.ts",
      "http://source3.com/stream.ts"
    ],
    "profile": "720p"
  }'

# 2. Start streaming (in another terminal)
ffplay "http://localhost:8085/stream/{stream_id}"

# 3. Trigger manual failover (while streaming)
curl -X POST "http://localhost:8085/streams/{stream_id}/failover" \
  -H "Authorization: Bearer {token}"

# Expected: Stream continues playing from new source
# Log should show:
# - "Failover detected for transcoded stream"
# - "Removed client from old stream"
# - "Starting failover attempt 1/3 for client, new URL: ..."
# - "Starting shared FFmpeg process"
# - "Streaming from FFmpeg process PID..."
```

## Log Output Example

**Before fix:**
```
INFO: Failover triggered for stream xxx
INFO: Stopping transcoding process for failover
INFO: Client removed from shared stream (finally block)
INFO: Transcoded streaming cancelled for client
INFO: Cleaned up client
# Stream stops - no reconnection
```

**After fix:**
```
INFO: Failover triggered for stream xxx
INFO: Stopping transcoding process for failover
INFO: Failover detected for transcoded stream xxx, will reconnect client to new URL
INFO: Removed client from old stream
INFO: Starting failover attempt 1/3 for client, new URL: http://source2.com/...
INFO: Starting shared FFmpeg process
INFO: Streaming from FFmpeg process PID 12345
# Stream continues from new source
```

## Files Modified

- `/Users/shaunparkison/Sites/m3ue/m3u-proxy/src/stream_manager.py`
  - `stream_transcoded()` method - Fixed cleanup logic to allow reconnection
  - Added enhanced logging for failover process

## Related

This fix complements the earlier failover improvements for:
- Direct/Continuous streams (already working)
- HLS streams (already working)
- Transcoded streams (NOW FIXED)

All three streaming methods now support seamless failover!
