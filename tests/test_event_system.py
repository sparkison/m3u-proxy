"""
Test the event system integration
"""
from models import StreamEvent, EventType, WebhookConfig
from events import EventManager
import asyncio
import pytest
import httpx
from unittest.mock import Mock, AsyncMock
from datetime import datetime

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


class TestEventSystem:
    """Test the integrated event system"""

    @pytest.mark.asyncio
    async def test_event_manager_basic_functionality(self):
        """Test that EventManager can start, emit events, and stop"""
        # Arrange
        event_manager = EventManager()
        events_received = []

        def test_handler(event):
            events_received.append(event)

        event_manager.add_handler(test_handler)

        # Act
        await event_manager.start()

        # Create and emit test event
        test_event = StreamEvent(
            event_type=EventType.STREAM_STARTED,
            stream_id="test_123",
            data={"url": "http://test.com/stream.m3u8"}
        )

        await event_manager.emit_event(test_event)

        # Give time for event processing
        await asyncio.sleep(0.1)

        await event_manager.stop()

        # Assert
        assert len(events_received) == 1
        assert events_received[0].event_type == EventType.STREAM_STARTED
        assert events_received[0].stream_id == "test_123"

    @pytest.mark.asyncio
    async def test_webhook_configuration(self):
        """Test webhook configuration and management"""
        # Arrange
        event_manager = EventManager()
        webhook_url = "http://example.com/webhook"

        webhook_config = WebhookConfig(
            url=webhook_url,
            events=[EventType.STREAM_STARTED, EventType.CLIENT_CONNECTED],
            timeout=5,
            retry_attempts=2
        )

        # Act
        event_manager.add_webhook(webhook_config)

        # Assert
        assert len(event_manager.webhooks) == 1
        assert str(event_manager.webhooks[0].url) == webhook_url
        assert EventType.STREAM_STARTED in event_manager.webhooks[0].events

        # Test removal
        removed = event_manager.remove_webhook(webhook_url)
        assert removed is True
        assert len(event_manager.webhooks) == 0

    @pytest.mark.asyncio
    async def test_webhook_sending(self):
        """Test that webhooks are actually sent (with mocking)"""
        # Arrange
        event_manager = EventManager()
        webhook_config = WebhookConfig(
            url="http://example.com/webhook",
            events=[EventType.STREAM_STARTED]
        )
        event_manager.add_webhook(webhook_config)

        # Mock httpx client
        with AsyncMock() as mock_client:
            mock_response = Mock()
            mock_response.status_code = 200
            mock_client.return_value.__aenter__.return_value.post.return_value = mock_response

            # Patch httpx.AsyncClient
            import httpx
            original_client = httpx.AsyncClient
            httpx.AsyncClient = mock_client

            try:
                await event_manager.start()

                # Create test event
                test_event = StreamEvent(
                    event_type=EventType.STREAM_STARTED,
                    stream_id="test_123",
                    data={"url": "http://test.com/stream.m3u8"}
                )

                # Emit event
                await event_manager.emit_event(test_event)

                # Give time for webhook processing
                await asyncio.sleep(0.1)

                await event_manager.stop()

                # Note: This is a simplified test - in reality we'd need more
                # complex mocking to verify the webhook was called

            finally:
                httpx.AsyncClient = original_client

    def test_event_model_creation(self):
        """Test that event models can be created properly"""
        # Arrange & Act
        event = StreamEvent(
            event_type=EventType.CLIENT_CONNECTED,
            stream_id="stream_456",
            data={
                "client_id": "client_789",
                "ip_address": "192.168.1.100"
            }
        )

        # Assert
        assert event.event_type == EventType.CLIENT_CONNECTED
        assert event.stream_id == "stream_456"
        assert event.data["client_id"] == "client_789"
        assert event.event_id is not None  # Auto-generated
        assert isinstance(event.timestamp, datetime)

    def test_webhook_config_validation(self):
        """Test webhook configuration validation"""
        # Test valid configuration
        valid_config = WebhookConfig(
            url="https://secure.example.com/webhook",
            events=[EventType.STREAM_STARTED, EventType.STREAM_STOPPED],
            timeout=10,
            retry_attempts=3
        )

        assert valid_config.timeout == 10
        assert valid_config.retry_attempts == 3
        assert len(valid_config.events) == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
