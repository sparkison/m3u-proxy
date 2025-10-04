# m3u-proxy Architecture Review & Analysis
**Date:** October 4, 2025  
**Reviewer:** GitHub Copilot  
**Version:** Current (dev branch)

## Executive Summary

This document provides a comprehensive review of the m3u-proxy application against the 8 key requirements specified by the project owner. The analysis includes validation of current implementation, identification of issues, and recommendations for optimization.

### Overall Assessment: ✅ **MOSTLY COMPLIANT** with **CRITICAL ISSUES IDENTIFIED**

The application correctly implements most features but has **critical architectural issues** that prevent it from being a true "live proxy" as specified.

---

## 1. Requirement Analysis

### ✅ Requirement 1: Provider Connections are Ephemeral and Proxied
**Status:** COMPLIANT

**Implementation:**
- Provider connections are managed through `StreamInfo.origin_task` 
- Connections only opened when `active_consumers > 0`
- See: `add_stream_consumer()` and `_fetch_unified_stream()` in `stream_manager.py`

```python
# Lines 302-322: Provider connection only starts with first consumer
async def add_stream_consumer(self, stream_id: str, client_id: str) -> bool:
    stream_info.active_consumers += 1
    
    # Start provider connection if this is the first consumer
    if stream_info.active_consumers == 1 and not stream_info.origin_task:
        logger.info(f"Starting provider connection for stream {stream_id} (first consumer)")
        stream_info.origin_task = asyncio.create_task(
            self._fetch_unified_stream(stream_id)
        )
```

**Evidence:** ✅ Confirmed working via testing

---

### ✅ Requirement 2: Multiple Clients Can Share a Single Provider Connection
**Status:** COMPLIANT

**Implementation:**
- Shared buffer architecture: `StreamInfo.stream_buffer` (Queue with maxsize=1000)
- Single `origin_task` serves multiple consumers via shared queue
- See: Lines 59, 302-322, 426-448 in `stream_manager.py`

```python
# Lines 426-448: Single provider connection feeds shared buffer
stream_info.stream_buffer.put_nowait(chunk)  # All consumers read from this buffer
```

**Connection Pooling Stats:**
```python
# Lines 1576-1603: Connection pooling metrics
active_connections = sum(1 for stream in self.streams.values()
                        if stream.origin_task and not stream.origin_task.done())
connection_efficiency = total_consumers / max(1, active_connections)
```

**Evidence:** ✅ Architecture supports this correctly

---

### ✅ Requirement 3: Immediate Connection Cleanup When No Active Clients
**Status:** COMPLIANT

**Implementation:**
- `remove_stream_consumer()` immediately cancels provider task when consumers reach 0
- See: Lines 323-351 in `stream_manager.py`

```python
# Lines 323-351: Immediate cleanup on last consumer disconnect
async def remove_stream_consumer(self, stream_id: str, client_id: str) -> bool:
    stream_info.active_consumers -= 1
    
    # Stop provider connection if no consumers remain
    if stream_info.active_consumers == 0 and stream_info.origin_task:
        logger.info(f"Stopping provider connection for stream {stream_id} (no consumers)")
        stream_info.origin_task.cancel()
        stream_info.origin_task = None
        # Clear the buffer
```

**Evidence:** ✅ Confirmed - enables "channel zapping"

---

### ⚠️ Requirement 4: Seamless Failover Support
**Status:** PARTIALLY COMPLIANT - Issues Identified

**Implementation:**
- Failover logic exists in `_try_failover()` with exponential backoff
- Automatic failover triggered on timeout/network errors
- See: Lines 1429-1496 in `stream_manager.py`

**Issues Identified:**

1. **❌ NOT Seamless for Active Clients:**
   ```python
   # Lines 489-500: Failover triggers restart, not seamless handoff
   failover_success = await self._try_failover(stream_id, reason)
   if failover_success:
       await asyncio.sleep(2.0)  # Delay before retry
       await self._fetch_unified_stream(stream_id)  # RESTARTS from scratch
   ```
   
   **Problem:** When failover occurs, the entire `_fetch_unified_stream` task restarts. Active clients consuming from the buffer will experience interruption (None signal sent to buffer), then need to wait for new connection to establish.

2. **Buffer Cleared on Failover:**
   ```python
   # Lines 560-569: Buffer cleared, clients get None signal
   stream_info.stream_buffer.put_nowait(None)  # End-of-stream signal
   ```

3. **No Pre-buffering During Failover:**
   - No mechanism to pre-connect to failover URL while primary is still working
   - No smooth transition between connections

**Recommendation:** See "Optimization Recommendations" section below.

---

### ✅ Requirement 5: Robust Event System
**Status:** COMPLIANT

**Implementation:**
- Complete event system in `events.py` with EventManager
- All required events implemented:
  - ✅ `STREAM_STARTED` (Line 368)
  - ✅ `STREAM_STOPPED` (Lines 460, 474)
  - ✅ `STREAM_FAILED` (Lines 481, 521, 543)
  - ✅ `CLIENT_CONNECTED` (Line 291)
  - ✅ `CLIENT_DISCONNECTED` (via cleanup)
  - ✅ `FAILOVER_TRIGGERED` (Line 1471)

**Webhook Support:**
- Async webhook delivery with retry logic
- Configurable per-event subscriptions
- Exponential backoff on failures

**Evidence:** ✅ Full-featured event system

---

### ❌ Requirement 6: True "Live" Proxy with User Agent Support
**Status:** **CRITICAL ISSUE - NOT COMPLIANT**

**Problem:** The application is **NOT** acting as a true live proxy for continuous streams.

**Current Implementation Issues:**

#### Issue 6.1: HLS Segments are Proxied Individually (Correct)
```python
# Lines 795-876: Each HLS segment is proxied on-demand
async def proxy_hls_segment(self, stream_id: str, client_id: str, 
                            segment_url: str, range_header: Optional[str] = None):
    """Proxy an individual HLS segment without creating a separate stream"""
    # This is CORRECT - segments are fetched on-demand as client requests them
```

✅ **This is correct behavior** - HLS segments are fetched as clients request them via playlist.

#### Issue 6.2: **Continuous Streams (.ts, .mp4, etc.) Have Critical Problems**

**Problem 1: Provider Connection Opens BEFORE Client Request**

```python
# Lines 302-322: Provider connection starts when stream is CREATED, not consumed
async def get_or_create_stream(self, stream_url: str, ...):
    # Stream is created here
    stream_id = hashlib.md5(stream_url.encode()).hexdigest()
    
    if stream_id not in self.streams:
        self.streams[stream_id] = StreamInfo(...)  # Stream exists
        
# Later in API layer (api.py lines 160-220):
# Stream creation endpoint creates stream immediately
# Provider connection starts on first CLIENT CONNECTION to playlist/stream
# NOT when first byte is actually requested
```

**Problem 2: Shared Buffer Architecture Doesn't Work for Continuous Streams**

```python
# Lines 853-1004: Stream response for continuous .ts streams
async def stream_unified_response(...):
    # Add this client as a consumer
    await self.add_stream_consumer(stream_id, client_id)  # Starts provider!
    
    # Wait for data
    await asyncio.sleep(0.1)  # Provider connection time to start
    
    async def generate():
        if is_hls_segment:
            # Get complete segment data (works for HLS)
            segment_data = await self.get_complete_segment_data(...)
        else:
            # For continuous streams - STREAMS FROM SHARED BUFFER
            while stream_info.active_consumers > 0:
                chunk = await self.get_stream_data(stream_id, client_id, timeout=30.0)
                yield chunk  # Yields chunks from shared buffer
```

**Why This is Wrong for Continuous Streams:**

1. **Timing Issues:** 
   - Client A starts watching at time T
   - Client B joins at T+30s
   - Both read from SAME shared buffer
   - Client B will get data from T+30s onwards, but may also get stale data from buffer that was meant for Client A
   - Buffer can have up to 1000 chunks (maxsize=1000) of mixed/stale data

2. **Seek/Position Issues:**
   - Continuous .ts streams are LIVE - there's no "seek" support
   - But the shared buffer treats all clients equally
   - Client B cannot start from "current live position" - it gets whatever is in buffer

3. **VOD Streams (.mp4, .mkv) Completely Broken:**
   - Multiple clients cannot share a single provider connection for VOD
   - Each client needs independent read position
   - Current architecture would make all clients watch in sync

**Problem 3: Range Requests Work Around The Issue (But Prove the Architecture is Wrong)**

```python
# Lines 680-805: Range requests BYPASS the shared buffer entirely
async def _handle_range_request(self, stream_id: str, client_id: str, 
                                start: int, end: Optional[int] = None):
    # Makes DIRECT connection to origin for this client
    async def proxy_range_generator():
        async with self.http_client.stream('GET', stream_url, 
                                          headers=headers, ...) as response:
            async for chunk in response.aiter_bytes(chunk_size=32 * 1024):
                yield chunk  # Direct proxy - NO shared buffer
```

**This proves the architecture is wrong** - if you need to bypass your core architecture for it to work correctly, the architecture is flawed.

**Correct Behavior for True Live Proxy:**

For **continuous streams** (.ts URLs, MPEG-TS, etc.):
- ✅ Should proxy byte-for-byte as client consumes
- ✅ Each client gets independent connection to provider (or smart pooling with position tracking)
- ✅ Provider connection opens ONLY when client starts consuming bytes
- ✅ Provider connection closes IMMEDIATELY when client stops

For **VOD streams** (.mp4, .mkv, etc.):
- ✅ Should support range requests
- ✅ Each client needs independent position tracking
- ✅ Cannot share single provider connection without position awareness

For **HLS streams**:
- ✅ Current implementation is CORRECT
- ✅ Playlist proxying works perfectly
- ✅ Segments fetched on-demand

**Evidence:** ❌ **CRITICAL ARCHITECTURE FLAW** - Shared buffer approach doesn't work for continuous streams

---

### ✅ Requirement 7: User IP Protection
**Status:** COMPLIANT

**Implementation:**
- All provider connections use stream-specific or default user agent
- Client headers properly isolated
- Stream-specific authentication headers in segment requests

```python
# Lines 1181-1305: Proxy segment with proper header isolation
async def proxy_segment(self, segment_url: str, client_id: str, ...):
    headers = {'User-Agent': stream_info.user_agent}  # Stream's UA, not client's
    headers['Referer'] = f"{auth_parsed.scheme}://{auth_parsed.netloc}/"
    headers['Origin'] = f"{auth_parsed.scheme}://{auth_parsed.netloc}"
    # Client IP never sent to provider
```

**Evidence:** ✅ Confirmed - proper header isolation

---

### ✅ Requirement 8: Multiple Format Support
**Status:** COMPLIANT

**Implementation:**
- HLS (.m3u8) - Full support with playlist processing
- MPEG-TS (.ts) - Supported (but with architecture issues noted in Req 6)
- MP4, MKV, WebM, AVI - Supported via content-type detection

```python
# Lines 18-38 in api.py: Content type detection
def get_content_type(url: str) -> str:
    if url_lower.endswith('.ts'): return 'video/mp2t'
    elif url_lower.endswith('.m3u8'): return 'application/vnd.apple.mpegurl'
    elif url_lower.endswith('.mp4'): return 'video/mp4'
    elif url_lower.endswith('.mkv'): return 'video/x-matroska'
    # ... etc
```

**Evidence:** ✅ Multiple formats supported

---

## 2. Critical Issues Summary

### 🔴 CRITICAL: Architecture Flaw in Continuous Stream Handling

**Problem:** Shared buffer architecture (`stream_buffer`) doesn't work correctly for continuous streams (.ts, .mp4, .mkv).

**Impact:**
- Multiple clients cannot properly share a single provider connection for non-HLS streams
- Timing/synchronization issues between clients
- VOD streams (.mp4, .mkv) completely broken for multiple clients
- Not a true "live proxy" as specified

**Root Cause:** The application tries to use ONE architecture (shared buffer) for TWO fundamentally different streaming patterns:
1. **HLS** - Discrete segments, request-based, works fine
2. **Continuous** - Byte streams, need position tracking, DOESN'T work

### 🟡 MEDIUM: Failover Not Seamless

**Problem:** Failover restarts the entire provider connection, causing interruption for active clients.

**Impact:**
- Clients experience buffering/interruption during failover
- Not "transparent" as specified in requirements

---

## 3. Optimization Recommendations

### Priority 1: Fix Continuous Stream Architecture ⚠️ CRITICAL

**Current Broken Flow:**
```
Client A → Shared Buffer ← Provider Stream
Client B → Shared Buffer
```

**Recommended Architecture:**

#### Option A: Separate Proxy Paths (Simpler, Recommended)

```python
class StreamManager:
    async def stream_response(self, stream_id: str, client_id: str):
        stream_info = self.streams[stream_id]
        
        # Determine streaming strategy based on stream type
        if stream_info.original_url.endswith('.m3u8'):
            # HLS: Use current shared buffer approach (IT WORKS!)
            return await self._stream_hls_response(stream_id, client_id)
        elif self._is_vod_stream(stream_info):
            # VOD: Direct proxy with range support (per-client)
            return await self._stream_vod_response(stream_id, client_id)
        else:
            # Live continuous (.ts): Direct proxy (per-client)
            return await self._stream_live_continuous_response(stream_id, client_id)
    
    async def _stream_live_continuous_response(self, stream_id: str, client_id: str):
        """Direct proxy for live continuous streams - NO shared buffer"""
        stream_info = self.streams[stream_id]
        current_url = stream_info.current_url or stream_info.original_url
        
        # Track that this client is consuming (for stats/cleanup)
        await self.add_stream_consumer(stream_id, client_id)
        
        async def generate():
            try:
                # OPEN provider connection for THIS client only
                headers = {'User-Agent': stream_info.user_agent, ...}
                
                async with self.live_stream_client.stream('GET', current_url, 
                                                          headers=headers) as response:
                    response.raise_for_status()
                    
                    # Direct byte-for-byte proxy - NO BUFFERING
                    async for chunk in response.aiter_bytes(chunk_size=32768):
                        yield chunk
                        
                        # Update stats
                        stream_info.total_bytes_served += len(chunk)
                        if client_id in self.clients:
                            self.clients[client_id].bytes_served += len(chunk)
                            
            except Exception as e:
                logger.error(f"Stream error for client {client_id}: {e}")
                # Try failover for THIS client
                if stream_info.failover_urls:
                    await self._try_failover(stream_id, "stream_error")
                    # Recursively retry with new URL
                    async for chunk in self._stream_live_continuous_response(stream_id, client_id):
                        yield chunk
            finally:
                # Remove consumer tracking
                await self.remove_stream_consumer(stream_id, client_id)
        
        return StreamingResponse(generate(), media_type="video/mp2t", ...)
```

**Benefits:**
- ✅ True live proxy - provider connection only open while client consumes
- ✅ Each client gets independent stream at their own pace
- ✅ Provider connection closes immediately when client disconnects
- ✅ Failover works per-client without affecting others
- ✅ Simpler reasoning about state

**Connection Pooling Note:** For multiple clients on same continuous stream, you COULD implement smart pooling, but it requires:
- Position tracking per client
- Read-ahead buffering
- Synchronization logic
- Much more complexity

For most use cases, **direct proxy per client is better** - it's simple, correct, and performant.

#### Option B: Smart Position-Aware Buffer (Complex, Not Recommended)

Only implement if you MUST have connection pooling for continuous streams:
- Track read position per client
- Implement sliding window buffer with position metadata
- Handle clients at different positions
- Much more complex, more failure modes

**Recommendation:** Use Option A (separate proxy paths)

---

### Priority 2: Implement Seamless Failover

**Current Problem:**
```python
# Failover RESTARTS entire connection
await self._try_failover(stream_id, reason)
await self._fetch_unified_stream(stream_id)  # Restart from beginning
```

**Recommended Solution:**

```python
async def _seamless_failover_for_continuous_stream(self, stream_id: str, 
                                                   old_response, reason: str):
    """Seamless failover without interrupting client stream"""
    stream_info = self.streams[stream_id]
    
    # Try next failover URL
    success = await self._try_failover(stream_id, reason)
    if not success:
        return None  # No more failover URLs
    
    new_url = stream_info.current_url
    headers = {'User-Agent': stream_info.user_agent, ...}
    
    try:
        # Open new connection to failover URL
        new_response = await self.live_stream_client.stream(
            'GET', new_url, headers=headers
        ).__aenter__()
        
        new_response.raise_for_status()
        logger.info(f"Seamless failover successful for {stream_id}")
        return new_response
        
    except Exception as e:
        logger.error(f"Failover connection failed: {e}")
        # Try next failover URL recursively
        return await self._seamless_failover_for_continuous_stream(
            stream_id, old_response, "failover_failed"
        )

# In the streaming generator:
async def generate():
    response = await self._open_stream_connection(current_url, headers)
    
    try:
        async for chunk in response.aiter_bytes(chunk_size=32768):
            yield chunk
    except (httpx.TimeoutException, httpx.NetworkError) as e:
        # Try seamless failover
        response = await self._seamless_failover_for_continuous_stream(
            stream_id, response, "timeout"
        )
        
        if response:
            # Continue with new connection - client doesn't notice
            async for chunk in response.aiter_bytes(chunk_size=32768):
                yield chunk
        else:
            # No more failover URLs available
            raise
```

---

### Priority 3: Implement Connection Pooling for HLS Segments

**Current:** Each segment creates new HTTP connection
**Recommended:** Use persistent connections with `httpx.AsyncClient` connection pooling

```python
# Already done! Just verify limits are set:
self.http_client = httpx.AsyncClient(
    timeout=settings.DEFAULT_CONNECTION_TIMEOUT,
    follow_redirects=True,
    max_redirects=10,
    limits=httpx.Limits(max_keepalive_connections=20, max_connections=100)  # Add this
)
```

---

### Priority 4: Add Proactive Health Checks

**Current:** Health checks exist but could be improved
**Recommendation:** Add proactive failover before failure occurs

```python
async def _periodic_health_check(self):
    """Enhanced health checking with proactive failover"""
    while self._running:
        for stream_id, stream_info in list(self.streams.items()):
            if stream_info.active_consumers == 0:
                continue
            
            # Check primary URL health
            is_healthy = await self._health_check_stream(stream_id)
            
            if not is_healthy and stream_info.failover_urls:
                # Proactively fail over BEFORE clients experience issues
                logger.warning(f"Proactive failover for {stream_id}")
                
                # For continuous streams: gracefully transition active clients
                if not stream_info.original_url.endswith('.m3u8'):
                    await self._trigger_proactive_failover(stream_id)
                else:
                    # For HLS: update current_url, next playlist request uses it
                    await self._try_failover(stream_id, "health_check_failed")
        
        await asyncio.sleep(60)
```

---

### Priority 5: Optimize Buffer Sizes

**Current:** Fixed buffer sizes
**Recommendation:** Dynamic buffer sizing based on stream type

```python
# For HLS segments (discrete, bounded size):
segment_buffer = Queue(maxsize=100)  # Smaller is fine

# For continuous streams (if using buffering):
# Calculate based on bitrate
estimated_bitrate = 5_000_000  # 5 Mbps
chunk_size = 32768
chunks_per_second = estimated_bitrate / 8 / chunk_size  # ~19 chunks/sec
buffer_duration_seconds = 10  # Buffer 10 seconds
buffer_size = int(chunks_per_second * buffer_duration_seconds)  # ~190 chunks
```

---

## 4. Performance Optimizations

### Use `uvloop` for Better Async Performance

```bash
pip install uvloop
```

```python
# In main.py:
import uvloop
asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
```

**Expected Improvement:** 2-4x faster async I/O operations

---

### Implement Connection Pooling Limits

```python
# In stream_manager.py __init__:
self.http_client = httpx.AsyncClient(
    timeout=settings.DEFAULT_CONNECTION_TIMEOUT,
    follow_redirects=True,
    max_redirects=10,
    limits=httpx.Limits(
        max_keepalive_connections=20,  # Keep 20 connections alive
        max_connections=100,  # Max 100 total connections
        keepalive_expiry=30.0  # Keep alive for 30 seconds
    )
)
```

---

### Add Response Compression

```python
# In api.py, add middleware:
from fastapi.middleware.gzip import GZIPMiddleware

app.add_middleware(GZIPMiddleware, minimum_size=1000)
```

**Note:** Only compresses non-streaming responses (stats, etc.)

---

## 5. Testing Recommendations

### Test Cases to Add

1. **Multiple Clients on Same Continuous Stream**
   ```python
   # Should work correctly with recommended architecture fix
   # Currently broken with shared buffer
   ```

2. **Rapid Channel Zapping**
   ```python
   # Test provider connections close immediately
   # Verify no connection leaks
   ```

3. **Seamless Failover During Active Streaming**
   ```python
   # Client should not notice failover
   # No buffering/interruption
   ```

4. **VOD Stream with Range Requests**
   ```python
   # Multiple clients at different positions
   # Verify each gets correct byte ranges
   ```

---

## 6. Code Quality Improvements

### Add Type Hints Consistently

```python
# Current:
async def proxy_segment(self, segment_url, client_id, ...):

# Better:
async def proxy_segment(
    self, 
    segment_url: str, 
    client_id: str,
    range_header: Optional[str] = None
) -> AsyncIterator[bytes]:
```

### Extract Magic Numbers to Configuration

```python
# Current:
Queue(maxsize=1000)
chunk_size=32768

# Better (in config.py):
STREAM_BUFFER_SIZE: int = 1000
CHUNK_SIZE: int = 32768
```

### Add Comprehensive Docstrings

```python
async def _fetch_unified_stream(self, stream_id: str):
    """
    Fetch stream data from provider and distribute to consumers.
    
    This method manages the provider connection lifecycle:
    - Opens connection on first consumer
    - Feeds data into shared buffer
    - Closes connection when no consumers remain
    
    Args:
        stream_id: Unique stream identifier
        
    Raises:
        httpx.TimeoutException: If provider connection times out
        httpx.NetworkError: If network error occurs
        
    Notes:
        - Automatically attempts failover on errors
        - Emits stream_started, stream_stopped, stream_failed events
    """
```

---

## 7. Summary & Action Items

### ✅ What's Working Well

1. ✅ HLS playlist and segment proxying
2. ✅ Event system with webhooks
3. ✅ Client tracking and statistics
4. ✅ Immediate cleanup on client disconnect
5. ✅ User agent and IP protection
6. ✅ Multiple format support

### ❌ Critical Issues to Fix

1. 🔴 **CRITICAL:** Shared buffer architecture doesn't work for continuous streams
2. 🟡 **MEDIUM:** Failover is not seamless (causes interruption)

### 📋 Action Items (Prioritized)

**Priority 1 (CRITICAL):**
- [ ] Implement separate proxy paths for HLS vs continuous streams
- [ ] Remove shared buffer for continuous streams
- [ ] Use direct proxy per-client for .ts/.mp4/.mkv streams

**Priority 2 (HIGH):**
- [ ] Implement seamless failover for continuous streams
- [ ] Add failover testing suite
- [ ] Document failover behavior

**Priority 3 (MEDIUM):**
- [ ] Add `uvloop` for performance
- [ ] Implement connection pooling limits
- [ ] Add proactive health checks

**Priority 4 (LOW):**
- [ ] Add comprehensive type hints
- [ ] Extract magic numbers to config
- [ ] Add detailed docstrings
- [ ] Add compression middleware

---

## 8. Conclusion

The m3u-proxy application has a **solid foundation** with excellent HLS support, robust event system, and good client management. However, it has a **critical architectural flaw** in how it handles continuous streams (non-HLS).

**The shared buffer approach does not work for continuous byte streams** where clients need independent position tracking. The current architecture would cause synchronization issues, timing problems, and completely break VOD streaming for multiple clients.

**Recommended Path Forward:**
1. Implement separate streaming strategies per stream type (HLS vs continuous)
2. Use direct proxy for continuous streams (simpler and correct)
3. Enhance failover to be truly seamless
4. Add comprehensive testing for the fixed architecture

With these changes, m3u-proxy will be a **true live proxy** that correctly handles all streaming scenarios as specified in the requirements.

---

## Appendix: Testing the Current Implementation

### Test URLs Provided

1. **Live Stream:** `https://ireplay.tv/test/blender.m3u8` ✅ Works correctly
2. **VOD Stream:** `https://devstreaming-cdn.apple.com/videos/streaming/examples/img_bipbop_adv_example_fmp4/master.m3u8` ✅ Works correctly

**Note:** Both are HLS streams, which use the architecture that WORKS. To test the broken continuous stream architecture, use a direct .ts URL or .mp4 URL with multiple simultaneous clients.

### Test Commands

```bash
# Create HLS stream (works correctly)
curl -X POST "http://localhost:8085/streams" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://ireplay.tv/test/blender.m3u8", "user_agent": "TestClient/1.0"}'

# Test with multiple clients (would reveal continuous stream issues if using .ts URL)
# Client 1:
ffplay "http://localhost:8085/hls/{stream_id}/playlist.m3u8"

# Client 2 (simultaneous):
ffplay "http://localhost:8085/hls/{stream_id}/playlist.m3u8"

# Check stats:
curl "http://localhost:8085/stats" | python3 -m json.tool
```

---

**Document Version:** 1.0  
**Last Updated:** October 4, 2025  
**Status:** Ready for Review
