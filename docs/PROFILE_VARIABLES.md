# Profile Variables Guide

This document explains the `profile_variables` feature in m3u-proxy, which enables dynamic customization of transcoding profiles through template variable substitution.

## Overview

Profile variables allow you to create flexible, reusable transcoding profiles by using placeholders in FFmpeg arguments. These placeholders can be substituted with custom values at runtime, enabling fine-tuned control over transcoding parameters without creating multiple profile variants.

## How It Works

The transcoding profile system uses a template-based approach with the following components:

1. **Profile Templates**: FFmpeg argument strings with placeholders
2. **Variable Substitution**: Placeholders replaced with actual values at runtime
3. **Default Values**: Fallback values when variables aren't provided

### Template Syntax

Variables in profile templates use curly brace syntax:

- `{variable}` - Simple placeholder (must be provided or left as-is)
- `{variable|default}` - Placeholder with default value

When a profile is rendered, the system:
1. Replaces `{variable}` with the provided value from `profile_variables`
2. Uses the default value if no value is provided
3. Keeps the placeholder unchanged if neither value nor default exists

## Built-in Default Variables

The following variables are automatically provided by the system:

| Variable | Default Value | Description |
|----------|--------------|-------------|
| `input_url` | (auto-filled) | Source stream URL |
| `output_args` | `pipe:1` | Output destination (stdout for streaming) |
| `video_bitrate` | `2M` | Target video bitrate |
| `audio_bitrate` | `128k` | Target audio bitrate |
| `format` | `mp4` | Output container format |
| `crf` | `23` | Constant Rate Factor for quality control |

## Using Profile Variables

### API Request Format

When creating a transcoded stream, include the `profile_variables` field:

```json
{
  "url": "http://example.com/stream.m3u8",
  "profile": "default",
  "profile_variables": {
    "video_bitrate": "5000k",
    "audio_bitrate": "192k",
    "crf": "18"
  }
}
```

### Example 1: Basic Quality Adjustment

Override the default CRF and bitrate values:

```bash
curl -X POST http://localhost:8000/transcode \
  -H "X-API-Token: your-token-here" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "http://source.example.com/stream.m3u8",
    "profile": "default",
    "profile_variables": {
      "crf": "20",
      "video_bitrate": "3500k",
      "audio_bitrate": "160k"
    }
  }'
```

**Result**: Creates a higher quality stream with CRF 20, 3500k video bitrate, and 160k audio bitrate.

### Example 2: Custom Resolution Profile

Using the 720p profile with custom bitrate:

```json
{
  "url": "http://example.com/1080p-stream.m3u8",
  "profile": "720p",
  "profile_variables": {
    "video_bitrate": "3000k",
    "audio_bitrate": "192k"
  }
}
```

### Example 3: Low Latency Streaming

Customize the low latency profile:

```json
{
  "url": "http://example.com/live-event.m3u8",
  "profile": "lowlatency",
  "profile_variables": {
    "video_bitrate": "1500k",
    "audio_bitrate": "128k"
  }
}
```

### Example 4: Custom Profile Template

Create a completely custom profile using FFmpeg arguments as the profile:

```json
{
  "url": "http://example.com/stream.m3u8",
  "profile": "-i {input_url} -c:v libx264 -preset {preset|faster} -crf {crf|23} -maxrate {maxrate|2500k} -bufsize {bufsize|5000k} -c:a aac -b:a {audio_bitrate|192k} -f mpegts {output_args}",
  "profile_variables": {
    "preset": "medium",
    "crf": "20",
    "maxrate": "4000k",
    "bufsize": "8000k",
    "audio_bitrate": "256k"
  }
}
```

**Note**: Custom profiles must start with `-` to be recognized as FFmpeg arguments rather than predefined profile names.

### Example 5: Format Conversion

Override output format for specific use cases:

```json
{
  "url": "http://example.com/stream.m3u8",
  "profile": "hq",
  "output_format": "mkv",
  "profile_variables": {
    "video_bitrate": "8000k",
    "audio_bitrate": "320k"
  }
}
```

## Default Profiles and Their Variables

### default
```
-i {input_url} -c:v h264_nvenc -preset fast -b:v {video_bitrate|2M} 
-c:a aac -b:a {audio_bitrate|128k} -f {format|mp4} {output_args|}
```

**Customizable variables**: `video_bitrate`, `audio_bitrate`, `format`, `output_args`

### hq (High Quality)
```
-i {input_url} -c:v h264_nvenc -preset fast -crf {crf|23} 
-maxrate {maxrate|2500k} -bufsize {bufsize|5000k} 
-c:a aac -b:a {audio_bitrate|192k} -f {format|mp4} {output_args|}
```

**Customizable variables**: `crf`, `maxrate`, `bufsize`, `audio_bitrate`, `format`, `output_args`

### lowlatency
```
-i {input_url} -c:v h264_nvenc -preset fast -tune zerolatency 
-b:v {video_bitrate|1M} -c:a aac -b:a {audio_bitrate|96k} 
-f {format|mpegts} {output_args|}
```

**Customizable variables**: `video_bitrate`, `audio_bitrate`, `format`, `output_args`

### 720p
```
-i {input_url} -c:v h264_nvenc -preset fast -vf scale=1280:720 
-b:v {video_bitrate|2500k} -c:a aac -b:a {audio_bitrate|128k} 
-f {format|mp4} {output_args|}
```

**Customizable variables**: `video_bitrate`, `audio_bitrate`, `format`, `output_args`

### 1080p
```
-i {input_url} -c:v h264_nvenc -preset fast -vf scale=1920:1080 
-b:v {video_bitrate|5000k} -c:a aac -b:a {audio_bitrate|192k} 
-f {format|mp4} {output_args|}
```

**Customizable variables**: `video_bitrate`, `audio_bitrate`, `format`, `output_args`

### hevc (H.265)
```
-i {input_url} -c:v hevc_nvenc -preset fast -b:v {video_bitrate|2M} 
-c:a aac -b:a {audio_bitrate|128k} -f {format|mp4} {output_args|}
```

**Customizable variables**: `video_bitrate`, `audio_bitrate`, `format`, `output_args`

### audio (Audio Only)
```
-i {input_url} -vn -c:a aac -b:a {audio_bitrate|192k} 
-f {format|adts} {output_args|}
```

**Customizable variables**: `audio_bitrate`, `format`, `output_args`

## Python API Usage

### Using profile_manager Directly

```python
from transcoding import get_profile_manager

profile_manager = get_profile_manager()

# Get a profile
profile = profile_manager.get_profile("hq")

# Define custom variables
variables = {
    "input_url": "http://example.com/stream.m3u8",
    "output_args": "pipe:1",
    "crf": "18",
    "maxrate": "5000k",
    "bufsize": "10000k",
    "audio_bitrate": "256k"
}

# Render the profile to FFmpeg arguments
ffmpeg_args = profile.render(variables)
print(" ".join(ffmpeg_args))
```

### Creating Custom Profiles

```python
profile_manager = get_profile_manager()

# Create a custom profile from template
custom_profile = profile_manager.create_profile_from_template(
    name="my_custom_profile",
    parameters="-i {input_url} -c:v libx264 -preset {preset|medium} -crf {crf|22} -c:a aac -b:a {audio_bitrate|192k} -f mpegts {output_args}",
    description="My custom transcoding profile"
)

# Use it
variables = {
    "input_url": "http://example.com/stream.m3u8",
    "output_args": "pipe:1",
    "preset": "slow",
    "crf": "18"
}

ffmpeg_args = custom_profile.render(variables)
```

## Best Practices

### 1. Use Defaults Wisely
Provide sensible defaults in your templates:
```
{crf|23}  # Good: has a reasonable default
{crf}     # Less ideal: requires explicit value
```

### 2. Document Custom Variables
When creating custom profiles, document which variables are expected:
```json
{
  "profile": "-i {input_url} -c:v libx264 -preset {preset|faster} ...",
  "profile_variables": {
    "preset": "medium"  // Comment: Available values: ultrafast, superfast, veryfast, faster, fast, medium, slow, slower, veryslow
  }
}
```

### 3. Validate Variable Values
The API validates that `profile_variables` is a dictionary with string keys and values. Invalid types will be rejected.

### 4. Hardware Acceleration Considerations
Default profiles automatically use hardware acceleration when available:
- NVIDIA: `h264_nvenc`, `hevc_nvenc`
- Intel/AMD/VAAPI: `h264_vaapi`, `hevc_vaapi`
- Software fallback: `libx264`, `libx265`

Custom profiles can override these by specifying the codec explicitly in `profile_variables` or the template.

### 5. Bitrate Suffixes
Remember to include proper suffixes:
- `k` for kilobits: `2500k`
- `M` for megabits: `2M`
- No suffix = bits per second

## Common Use Cases

### Adaptive Bitrate by Network
```python
# Mobile network
profile_variables = {
    "video_bitrate": "800k",
    "audio_bitrate": "64k"
}

# WiFi
profile_variables = {
    "video_bitrate": "3000k",
    "audio_bitrate": "192k"
}

# High-speed connection
profile_variables = {
    "video_bitrate": "8000k",
    "audio_bitrate": "320k"
}
```

### Quality Tiers
```python
# Low quality
low_quality = {"crf": "28", "video_bitrate": "1000k"}

# Medium quality  
medium_quality = {"crf": "23", "video_bitrate": "2500k"}

# High quality
high_quality = {"crf": "18", "video_bitrate": "5000k"}
```

### Format-Specific Optimization
```python
# HLS streaming
hls_vars = {
    "format": "mpegts",
    "video_bitrate": "2500k",
    "audio_bitrate": "128k"
}

# Download/archive
archive_vars = {
    "format": "mp4",
    "video_bitrate": "5000k",
    "audio_bitrate": "256k",
    "crf": "18"
}
```

## API Response

When you create a transcoded stream with profile variables, the response includes:

```json
{
  "stream_id": "abc123def456",
  "url": "http://proxy.example.com/stream/abc123def456",
  "profile": "hq",
  "profile_variables": {
    "crf": "18",
    "maxrate": "5000k",
    "bufsize": "10000k",
    "audio_bitrate": "256k"
  },
  "ffmpeg_args": "-i http://source.com/stream.m3u8 -c:v h264_nvenc -preset fast -crf 18 -maxrate 5000k -bufsize 10000k -c:a aac -b:a 256k -f mpegts pipe:1",
  "message": "Transcoded stream created successfully (direct MPEGTS pipe)"
}
```

## Troubleshooting

### Variables Not Being Substituted
- **Issue**: Placeholder remains in output
- **Solution**: Check variable name spelling and ensure it's included in `profile_variables`

### Invalid Template Error
- **Issue**: Profile template validation fails
- **Solution**: Ensure template includes `{input_url}` and has balanced braces

### Type Errors
- **Issue**: "profile_variables must be a dictionary"
- **Solution**: Ensure you're passing an object/dict, not an array or string

### Missing Required Variables
- **Issue**: FFmpeg fails with invalid arguments
- **Solution**: Either provide the variable in `profile_variables` or add a default value in the template: `{variable|default}`

## Security Considerations

The template validation system checks for:
- Balanced braces
- Required `{input_url}` placeholder
- Dangerous command patterns (rm, wget, curl, shell commands, etc.)

Custom templates are validated before use. Malicious patterns will be rejected.

## See Also

- [HARDWARE_ACCELERATION.md](HARDWARE_ACCELERATION.md) - Hardware acceleration setup
- [TESTING.md](TESTING.md) - Testing transcoding profiles
- [REDIS_POOLING.md](REDIS_POOLING.md) - Stream pooling with transcoding
