# API Token Authentication - Implementation Summary

## Changes Made

### 1. Configuration (`src/config.py`)
- Added `API_TOKEN: Optional[str] = None` to the Settings class
- This allows the token to be set via environment variable
- When not set (default), authentication is disabled

### 2. API Module (`src/api.py`)
- Added `Header` import from FastAPI
- Created `verify_token()` dependency function that:
  - Returns `True` if `API_TOKEN` is not configured (auth disabled)
  - Returns `401` error if token is required but not provided
  - Returns `403` error if token is provided but invalid
  - Returns `True` if token matches the configured value

### 3. Protected Endpoints
Added `dependencies=[Depends(verify_token)]` to the following endpoints:

**Management:**
- `GET /` - Root/status
- `POST /streams` - Create stream
- `GET /streams` - List streams
- `GET /streams/{stream_id}` - Get stream info
- `DELETE /streams/{stream_id}` - Delete stream
- `POST /streams/{stream_id}/failover` - Trigger failover

**Statistics:**
- `GET /stats` - Main statistics
- `GET /stats/detailed` - Detailed stats
- `GET /stats/performance` - Performance stats
- `GET /stats/streams` - Stream stats
- `GET /stats/clients` - Client stats
- `GET /clients` - List clients

**Monitoring:**
- `GET /health` - Health check

**Webhooks:**
- `POST /webhooks` - Add webhook
- `GET /webhooks` - List webhooks
- `DELETE /webhooks` - Remove webhook
- `POST /webhooks/test` - Test webhook

**Client Management:**
- `DELETE /hls/{stream_id}/clients/{client_id}` - Disconnect client

### 4. Unprotected Endpoints (By Design)
These endpoints do NOT require authentication as they are used by media players:
- `GET /hls/{stream_id}/playlist.m3u8` - HLS playlist
- `GET /hls/{stream_id}/segment` - HLS segment
- `GET /hls/{stream_id}/segment.ts` - HLS segment alt
- `GET /stream/{stream_id}` - Direct stream

Security is handled by the `stream_id` - only users who know the ID can access the stream.

### 5. Documentation
- **docs/AUTHENTICATION.md**: Comprehensive guide on using API authentication
- **README.md**: Updated with authentication section and examples
- **.env**: Added example configuration with `API_TOKEN` commented out

### 6. Examples and Tools
- **tools/auth_example.py**: Complete example script demonstrating all auth operations
- **tests/test_auth.py**: Unit tests for authentication functionality

## Usage

### Enable Authentication
```bash
# Set in environment
export API_TOKEN="your_secret_token_here"

# Or in .env file
API_TOKEN=your_secret_token_here

# Restart the server
python main.py
```

### Disable Authentication
```bash
# Unset the variable
unset API_TOKEN

# Or leave it commented in .env
# API_TOKEN=

# Restart the server
python main.py
```

### Making Authenticated Requests
```bash
# With cURL
curl -H "X-API-Token: your_secret_token_here" http://localhost:8085/stats

# With Python
import requests
headers = {"X-API-Token": "your_secret_token_here"}
response = requests.get("http://localhost:8085/stats", headers=headers)
```

## Security Features

1. **Optional by Default**: Authentication is disabled unless explicitly configured
2. **Header-Based**: Uses standard HTTP header (`X-API-Token`)
3. **Simple Token Comparison**: Uses secure string comparison
4. **Streaming Unaffected**: Media playback endpoints remain unauthenticated
5. **Clear Error Messages**: Returns appropriate HTTP status codes (401/403)

## Testing

Run the authentication tests:
```bash
pytest tests/test_auth.py -v
```

Run the example script:
```bash
# Without authentication
python tools/auth_example.py

# With authentication
export API_TOKEN="test_token"
python tools/auth_example.py
```

## Backward Compatibility

This implementation is **fully backward compatible**:
- Existing deployments without `API_TOKEN` set will continue to work unchanged
- No breaking changes to existing API contracts
- Stream URLs remain accessible (by design)
- All existing client code will continue to work

## Future Enhancements

Potential improvements for future versions:
- Multiple API tokens with different permissions
- JWT-based authentication
- Rate limiting per token
- Token expiration
- Audit logging of authenticated requests
- IP whitelisting alongside token auth
