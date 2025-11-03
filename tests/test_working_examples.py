from stream_manager import StreamManager, ClientInfo, StreamInfo, M3U8Processor
import pytest
from datetime import datetime, timezone

# Add src to path
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


class TestBasicFunctionality:
    """Simple tests to demonstrate Python testing concepts"""

    def test_client_info_creation(self):
        """Test that ClientInfo dataclass works correctly"""
        # Arrange
        client_id = "test_client_123"
        created_time = datetime.now(timezone.utc)
        last_access_time = datetime.now(timezone.utc)

        # Act
        client = ClientInfo(
            client_id=client_id,
            created_at=created_time,
            last_access=last_access_time
        )

        # Assert
        assert client.client_id == client_id
        assert client.created_at == created_time
        assert client.last_access == last_access_time
        assert client.bytes_served == 0  # Default value
        assert client.segments_served == 0  # Default value
        assert client.user_agent is None  # Default value

    def test_stream_info_creation(self):
        """Test that StreamInfo dataclass works correctly"""
        # Arrange
        stream_id = "abc12345"
        url = "http://example.com/stream.m3u8"
        created_time = datetime.now(timezone.utc)
        last_access_time = datetime.now(timezone.utc)

        # Act
        stream = StreamInfo(
            stream_id=stream_id,
            original_url=url,
            created_at=created_time,
            last_access=last_access_time
        )

        # Assert
        assert stream.stream_id == stream_id
        assert stream.original_url == url
        assert stream.is_active is True  # Default
        assert stream.client_count == 0  # Default
        assert len(stream.failover_urls) == 0  # Default empty list
        assert "Mozilla" in stream.user_agent  # Has default user agent

    @pytest.mark.asyncio
    async def test_stream_manager_basic_creation(self):
        """Test StreamManager basic functionality"""
        # Arrange
        manager = StreamManager()
        test_url = "http://example.com/test.m3u8"

        # Act
        stream_id = await manager.get_or_create_stream(test_url)

        # Assert
        assert stream_id is not None
        assert isinstance(stream_id, str)
        assert len(stream_id) == 32  # MD5 hash length
        assert stream_id in manager.streams

        # Check the stream was created with correct properties
        stream_info = manager.streams[stream_id]
        assert stream_info.original_url == test_url
        assert stream_info.is_active is True

    @pytest.mark.asyncio
    async def test_stream_deduplication(self):
        """Test that same URL gets same stream ID (deduplication)"""
        # Arrange
        manager = StreamManager()
        test_url = "http://example.com/same.m3u8"

        # Act - create stream twice with same URL
        stream_id_1 = await manager.get_or_create_stream(test_url)
        stream_id_2 = await manager.get_or_create_stream(test_url)

        # Assert - should be the same stream
        assert stream_id_1 == stream_id_2
        assert len(manager.streams) == 1  # Only one stream created

    def test_m3u8_processor_creation(self):
        """Test M3U8Processor can be created"""
        # Arrange & Act
        base_url = "http://proxy.com"
        client_id = "test_client"
        processor = M3U8Processor(base_url, client_id)

        # Assert
        assert processor.base_url == base_url
        assert processor.client_id == client_id

    def test_stream_manager_stats(self):
        """Test getting stats from StreamManager"""
        # Arrange
        manager = StreamManager()

        # Act
        stats = manager.get_stats()

        # Assert
        assert isinstance(stats, dict)  # Based on the actual implementation
        # We can add more specific assertions once we know the exact structure

    @pytest.mark.asyncio
    async def test_stream_with_failover_urls(self):
        """Test creating stream with failover URLs"""
        # Arrange
        manager = StreamManager()
        primary_url = "http://primary.com/stream.m3u8"
        backup_urls = ["http://backup1.com/stream.m3u8",
                       "http://backup2.com/stream.m3u8"]

        # Act
        stream_id = await manager.get_or_create_stream(
            primary_url,
            failover_urls=backup_urls
        )

        # Assert
        assert stream_id is not None
        stream_info = manager.streams[stream_id]
        assert stream_info.failover_urls == backup_urls

    @pytest.mark.asyncio
    async def test_stream_with_custom_user_agent(self):
        """Test creating stream with custom user agent"""
        # Arrange
        manager = StreamManager()
        test_url = "http://example.com/stream.m3u8"
        custom_ua = "MyTestApp/1.0"

        # Act
        stream_id = await manager.get_or_create_stream(
            test_url,
            user_agent=custom_ua
        )

        # Assert
        assert stream_id is not None
        stream_info = manager.streams[stream_id]
        assert stream_info.user_agent == custom_ua


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
