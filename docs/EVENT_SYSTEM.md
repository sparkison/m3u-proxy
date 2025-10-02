# Event System Guide

The m3u-proxy now has a fully integrated event system that allows you to listen for stream events and send notifications via webhooks.

## 📨 Event Types

The system fires the following events:

- `STREAM_STARTED` - When a new stream is created
- `STREAM_STOPPED` - When a stream is stopped/cleaned up  
- `STREAM_FAILED` - When a stream fails to start or encounters errors
- `CLIENT_CONNECTED` - When a client connects to a stream
- `CLIENT_DISCONNECTED` - When a client disconnects 
- `FAILOVER_TRIGGERED` - When failover to backup URL occurs

## 🔧 API Endpoints

### Webhook Management

**Add Webhook:**
```bash
POST /webhooks
Content-Type: application/json

{
  "url": "https://your-server.com/webhook",
  "events": ["stream_started", "client_connected", "failover_triggered"],
  "timeout": 10,
  "retry_attempts": 3
}
```

**List Webhooks:**
```bash
GET /webhooks
```

**Remove Webhook:**
```bash
DELETE /webhooks?webhook_url=https://your-server.com/webhook
```

**Test Webhook:**
```bash
POST /webhooks/test?webhook_url=https://your-server.com/webhook
```

## 📡 Webhook Payload Format

When events occur, webhooks receive POST requests with this structure:

```json
{
  "event_id": "uuid-string",
  "event_type": "stream_started",
  "stream_id": "abc123def456", 
  "timestamp": "2025-09-25T22:38:34.392830",
  "data": {
    "primary_url": "http://example.com/stream.m3u8",
    "user_agent": "VLC/3.0.20",
    "stream_type": "hls"
  }
}
```

## 🎯 Event Data Examples

### Stream Started
```json
{
  "event_type": "stream_started",
  "stream_id": "abc123",
  "data": {
    "primary_url": "http://example.com/stream.m3u8",
    "failover_urls": ["http://backup.com/stream.m3u8"],
    "user_agent": "CustomApp/1.0",
    "stream_type": "hls"
  }
}
```

### Client Connected
```json
{
  "event_type": "client_connected", 
  "stream_id": "abc123",
  "data": {
    "client_id": "client_456",
    "user_agent": "VLC media player",
    "ip_address": "192.168.1.100"
  }
}
```

### Failover Triggered
```json
{
  "event_type": "failover_triggered",
  "stream_id": "abc123", 
  "data": {
    "old_url": "http://primary.com/stream.m3u8",
    "new_url": "http://backup.com/stream.m3u8",
    "failover_index": 1
  }
}
```

### Client Disconnected
```json
{
  "event_type": "client_disconnected",
  "stream_id": "abc123",
  "data": {
    "client_id": "client_456",
    "bytes_served": 1048576,
    "segments_served": 42
  }
}
```

## 🛠️ Example Usage

### Simple Monitoring Webhook

Set up a webhook to monitor stream health:

```bash
curl -X POST http://localhost:8085/webhooks \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://monitor.yoursite.com/iptv-alerts",
    "events": ["stream_failed", "failover_triggered"],
    "timeout": 5,
    "retry_attempts": 2
  }'
```

### Analytics Webhook

Track all stream lifecycle events:

```bash
curl -X POST http://localhost:8085/webhooks \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://analytics.yoursite.com/iptv-events", 
    "events": [
      "stream_started",
      "stream_stopped", 
      "client_connected",
      "client_disconnected"
    ],
    "timeout": 10
  }'
```

### Dashboard Updates

Real-time dashboard updates:

```bash
curl -X POST http://localhost:8085/webhooks \
  -H "Content-Type: application/json" \
  -d '{
    "url": "wss://dashboard.yoursite.com/iptv-updates",
    "events": [
      "stream_started",
      "stream_stopped",
      "stream_failed", 
      "client_connected",
      "client_disconnected",
      "failover_triggered"
    ],
    "timeout": 15,
    "retry_attempts": 1
  }'
```

## 🖥️ Server-Side Handler Examples

### Node.js/Express Example
```javascript
app.post('/iptv-webhook', (req, res) => {
  const { event_type, stream_id, data } = req.body;
  
  switch(event_type) {
    case 'stream_started':
      console.log(`New stream: ${data.primary_url}`);
      break;
      
    case 'failover_triggered':
      console.log(`Failover: ${stream_id} switched to ${data.new_url}`);
      // Send alert to admin
      break;
      
    case 'client_connected':
      console.log(`Client connected: ${data.client_id} from ${data.ip_address}`);
      // Update user count in database
      break;
  }
  
  res.json({ received: true });
});
```

### Python/Flask Example
```python
from flask import Flask, request, jsonify

@app.route('/iptv-webhook', methods=['POST'])
def handle_webhook():
    event = request.json
    event_type = event['event_type']
    stream_id = event['stream_id'] 
    data = event['data']
    
    if event_type == 'stream_failed':
        # Send alert
        send_alert(f"Stream {stream_id} failed!")
        
    elif event_type == 'client_disconnected':
        # Log analytics
        log_session(data['client_id'], data['bytes_served'])
    
    return jsonify({"status": "received"})
```

## 🧪 Testing Events

Use the demo script to see events in action:

```bash
python demo_events.py
```

Or test webhooks with a real endpoint:

```bash
# Test with webhook.site for debugging
curl -X POST http://localhost:8085/webhooks \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://webhook.site/your-unique-id",
    "events": ["stream_started"],
    "timeout": 5
  }'

# Create a stream to trigger the event
curl -X POST http://localhost:8085/streams \
  -H "Content-Type: application/json" \
  -d '{
    "url": "http://example.com/test.m3u8"
  }'
```

## 📊 Built-in Event Handler

The system includes a built-in logging handler that logs all events:

```
INFO: Event: stream_started for stream abc123def456 at 2025-09-25 22:38:34
INFO: Event: client_connected for stream abc123def456 at 2025-09-25 22:38:35
```

This helps with debugging and monitoring without needing external webhooks.

## 🔒 Security Considerations

- **Webhook URLs should use HTTPS** for production
- **Validate webhook payloads** on your server
- **Implement authentication** if needed (custom headers)
- **Set reasonable timeouts** to avoid blocking the event system
- **Monitor webhook failures** and adjust retry settings

The event system runs asynchronously and won't block stream operations even if webhooks fail.
