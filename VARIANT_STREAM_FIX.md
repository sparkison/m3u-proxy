# HLS Variant Stream Fix

## Problem
When playing an HLS master playlist (e.g., `blender.m3u8`), multiple stream entries were being created:
- 1 for the master playlist
- 1 for each variant/quality level (rate_0_28_us.m3u8, rate_2_28_us.m3u8, etc.)

This resulted in showing 5+ streams when there was actually only 1 HLS stream with multiple quality variants.

## Root Cause
When the HLS master playlist was processed:
1. The master playlist URLs were rewritten to point to the proxy with `?url=...` parameters
2. When the player requested variant playlists (different quality levels), the `resolve_stream_id()` dependency was creating **new streams** for each variant URL
3. Each variant URL got a unique `stream_id` (MD5 hash of the URL), appearing as separate streams

## Solution
Added parent-child relationship tracking for HLS variant streams:

### 1. **StreamInfo Model** (`src/stream_manager.py`)
Added fields to track variant relationships:
```python
parent_stream_id: Optional[str] = None
is_variant_stream: bool = False
```

### 2. **Stream Creation** (`get_or_create_stream()`)
- Added `parent_stream_id` parameter
- Variant streams inherit user agent from parent
- Variant streams are NOT counted in global stats
- Logs indicate when a stream is a variant

### 3. **URL Rewriting** (`M3U8Processor`)
- Added `parent_stream_id` to constructor
- Variant playlist URLs now include `&parent={stream_id}` parameter
- This allows proper parent-child association when variants are requested

### 4. **API Resolution** (`resolve_stream_id()`)
- Added `parent` query parameter
- When a URL with `parent` parameter is requested, the stream is marked as a variant
- Variant streams are associated with their parent

### 5. **Client Registration** (`register_client()`)
- Clients connecting to variant streams are associated with the parent stream
- This ensures client counts are accurate on the master playlist

### 6. **Stats Aggregation** (`get_stats()`)
- Variant streams are excluded from the main streams list
- Client counts and byte/segment stats are aggregated from variants to parent
- Only master playlists appear in `/streams` endpoint

## Changes Made

### Files Modified:
1. **`src/stream_manager.py`**
   - Updated `StreamInfo` dataclass
   - Updated `M3U8Processor` class
   - Updated `get_or_create_stream()` method
   - Updated `register_client()` method
   - Updated `get_stats()` method

2. **`src/api.py`**
   - Updated `resolve_stream_id()` dependency

## Testing
After the fix:
- Creating a stream with `blender.m3u8` (HLS master playlist)
- Results in showing **1 stream** in `/streams` endpoint
- All clients are properly associated with the master stream
- Variant playlists still work correctly but don't appear as separate streams

## Verification
```bash
# Create stream
curl -X POST "http://localhost:8085/streams" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://ireplay.tv/test/blender.m3u8"}'

# Check streams (should show only 1)
curl "http://localhost:8085/streams" | jq

# Request master playlist (creates variants internally)
curl "http://localhost:8085/hls/{stream_id}/playlist.m3u8"

# Check streams again (should still show only 1)
curl "http://localhost:8085/streams" | jq

# Check clients (should be associated with parent stream)
curl "http://localhost:8085/clients" | jq
```

## Benefits
✅ Clean stream list showing only actual streams (not variants)  
✅ Accurate client counts on parent streams  
✅ Proper stat aggregation from variants to parents  
✅ Maintains backward compatibility  
✅ No changes needed to client players  

## Notes
- Variant streams still exist internally for proper HLS handling
- They're just hidden from the public API endpoints
- Stats from variants are aggregated to the parent stream
- The `parent` parameter in URLs enables proper tracking
