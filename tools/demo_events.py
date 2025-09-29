#!/usr/bin/env python3
"""
Demo script showing the event system working
"""
import logging
import asyncio
import sys
import os

# Add src to path (now we're in tools/, so go up one level to reach src/)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from models import StreamEvent, EventType, WebhookConfig
from events import EventManager


# Set up logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


async def demo_event_system():
    """Demonstrate the event system functionality"""
    print("🚀 Starting Event System Demo\n")

    # Create event manager
    event_manager = EventManager()

    # Add a simple event handler
    events_received = []

    def log_handler(event: StreamEvent):
        events_received.append(event)
        print(
            f"📨 Event Received: {event.event_type} for stream {event.stream_id}")
        print(f"   Data: {event.data}")
        print(f"   Time: {event.timestamp}")
        print()

    event_manager.add_handler(log_handler)

    # Add a webhook (won't actually send anywhere, just for demo)
    webhook_config = WebhookConfig(
        url="http://example.com/webhook",
        events=[EventType.STREAM_STARTED, EventType.CLIENT_CONNECTED],
        timeout=5
    )
    event_manager.add_webhook(webhook_config)
    print(f"📡 Added webhook: {webhook_config.url}")
    print(f"   Listening for: {[e.value for e in webhook_config.events]}\n")

    # Start the event manager
    await event_manager.start()
    print("✅ Event Manager Started\n")

    # Simulate some events
    print("📤 Simulating Events...\n")

    # 1. Stream started event
    stream_event = StreamEvent(
        event_type=EventType.STREAM_STARTED,
        stream_id="demo_stream_123",
        data={
            "primary_url": "http://example.com/demo.m3u8",
            "user_agent": "DemoApp/1.0",
            "stream_type": "hls"
        }
    )
    await event_manager.emit_event(stream_event)

    # 2. Client connected event
    client_event = StreamEvent(
        event_type=EventType.CLIENT_CONNECTED,
        stream_id="demo_stream_123",
        data={
            "client_id": "demo_client_456",
            "ip_address": "192.168.1.100",
            "user_agent": "VLC Media Player"
        }
    )
    await event_manager.emit_event(client_event)

    # 3. Failover event
    failover_event = StreamEvent(
        event_type=EventType.FAILOVER_TRIGGERED,
        stream_id="demo_stream_123",
        data={
            "old_url": "http://primary.com/stream.m3u8",
            "new_url": "http://backup.com/stream.m3u8",
            "failover_index": 1
        }
    )
    await event_manager.emit_event(failover_event)

    # 4. Client disconnected event
    disconnect_event = StreamEvent(
        event_type=EventType.CLIENT_DISCONNECTED,
        stream_id="demo_stream_123",
        data={
            "client_id": "demo_client_456",
            "bytes_served": 1024000,
            "segments_served": 42
        }
    )
    await event_manager.emit_event(disconnect_event)

    # 5. Stream stopped event
    stop_event = StreamEvent(
        event_type=EventType.STREAM_STOPPED,
        stream_id="demo_stream_123",
        data={
            "reason": "no_active_clients",
            "duration": 300  # 5 minutes
        }
    )
    await event_manager.emit_event(stop_event)

    # Wait a bit for event processing
    await asyncio.sleep(1)

    # Show summary
    print("📊 Event Summary:")
    print(f"   Total events processed: {len(events_received)}")
    print(f"   Event types: {[e.event_type.value for e in events_received]}")
    print(f"   Webhooks configured: {len(event_manager.webhooks)}")

    # Stop the event manager
    await event_manager.stop()
    print("\n🛑 Event Manager Stopped")


async def demo_webhook_management():
    """Demonstrate webhook management"""
    print("\n🔧 Webhook Management Demo\n")

    event_manager = EventManager()

    # Add multiple webhooks
    webhooks = [
        WebhookConfig(
            url="http://monitoring.example.com/alerts",
            events=[EventType.STREAM_FAILED, EventType.FAILOVER_TRIGGERED],
            timeout=10,
            retry_attempts=3
        ),
        WebhookConfig(
            url="http://analytics.example.com/events",
            events=[EventType.STREAM_STARTED, EventType.STREAM_STOPPED],
            timeout=5,
            retry_attempts=1
        ),
        WebhookConfig(
            url="http://dashboard.example.com/updates",
            events=list(EventType),  # All events
            timeout=15
        )
    ]

    for webhook in webhooks:
        event_manager.add_webhook(webhook)
        print(f"➕ Added webhook: {webhook.url}")
        print(
            f"   Events: {[e.value for e in webhook.events][:3]}{'...' if len(webhook.events) > 3 else ''}")
        print(
            f"   Timeout: {webhook.timeout}s, Retries: {webhook.retry_attempts}")
        print()

    print(f"📡 Total webhooks configured: {len(event_manager.webhooks)}")

    # Remove a specific webhook
    removed = event_manager.remove_webhook(
        "http://analytics.example.com/events")
    print(f"🗑️  Removed webhook: {removed}")
    print(f"📡 Remaining webhooks: {len(event_manager.webhooks)}")

if __name__ == "__main__":
    async def main():
        await demo_event_system()
        await demo_webhook_management()
        print("\n✨ Demo completed!")

    # Run the demo
    asyncio.run(main())
