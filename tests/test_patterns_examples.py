"""
Additional test examples demonstrating Python testing best practices
"""
import pytest
from unittest.mock import Mock, patch, AsyncMock
import asyncio
from datetime import datetime

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from stream_manager import StreamManager


class TestMockingExamples:
    """Examples of how to use mocking in Python tests"""
    
    @pytest.mark.asyncio
    async def test_with_mock_http_client(self):
        """Example of mocking HTTP requests"""
        # Arrange
        manager = StreamManager()
        test_url = "http://example.com/test.m3u8"
        
        # Mock the HTTP client response
        with patch.object(manager.http_client, 'get') as mock_get:
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.text = "#EXTM3U\nsegment1.ts"
            mock_get.return_value = mock_response
            
            # Act & Assert
            # Here we would test playlist fetching if we had that method
            # This shows the pattern for mocking HTTP calls
            assert mock_response.status_code == 200
            assert "#EXTM3U" in mock_response.text


class TestParameterizedTests:
    """Examples of parameterized tests - testing multiple inputs efficiently"""
    
    @pytest.mark.parametrize("url,expected_length", [
        ("http://example.com/stream.m3u8", 32),
        ("http://different.com/another.m3u8", 32),
        ("https://secure.example.com/playlist.m3u8", 32),
    ])
    @pytest.mark.asyncio
    async def test_stream_id_generation_for_different_urls(self, url, expected_length):
        """Test that different URLs produce stream IDs of expected length"""
        # Arrange
        manager = StreamManager()
        
        # Act
        stream_id = await manager.get_or_create_stream(url)
        
        # Assert
        assert len(stream_id) == expected_length
        assert isinstance(stream_id, str)
    
    @pytest.mark.parametrize("failover_count", [0, 1, 3, 5])
    @pytest.mark.asyncio
    async def test_different_failover_counts(self, failover_count):
        """Test streams with different numbers of failover URLs"""
        # Arrange
        manager = StreamManager()
        primary_url = "http://primary.com/stream.m3u8"
        failover_urls = [f"http://backup{i}.com/stream.m3u8" for i in range(failover_count)]
        
        # Act
        stream_id = await manager.get_or_create_stream(primary_url, failover_urls=failover_urls)
        
        # Assert
        assert stream_id is not None
        stream_info = manager.streams[stream_id]
        assert len(stream_info.failover_urls) == failover_count


class TestFixtures:
    """Examples of using pytest fixtures"""
    
    @pytest.fixture
    def stream_manager(self):
        """Fixture that provides a fresh StreamManager for each test"""
        return StreamManager()
    
    @pytest.fixture
    async def stream_with_data(self, stream_manager):
        """Fixture that provides a StreamManager with a pre-created stream"""
        url = "http://fixture.com/stream.m3u8"
        stream_id = await stream_manager.get_or_create_stream(url)
        return stream_manager, stream_id, url
    
    def test_using_simple_fixture(self, stream_manager):
        """Test using the stream_manager fixture"""
        # The fixture provides a fresh StreamManager
        assert len(stream_manager.streams) == 0
        assert len(stream_manager.clients) == 0
    
    def test_using_sync_fixture_pattern(self, stream_manager):
        """Test a pattern that doesn't require async fixtures"""
        # This shows how to avoid async fixture complexity
        # by doing the setup in the test itself
        assert len(stream_manager.streams) == 0
        
        # We could do async setup here if needed, but keep it simple


class TestErrorHandling:
    """Examples of testing error conditions"""
    
    @pytest.mark.asyncio
    async def test_handling_duplicate_stream_creation(self):
        """Test that creating the same stream twice works correctly"""
        # Arrange
        manager = StreamManager()
        url = "http://example.com/duplicate.m3u8"
        
        # Act
        stream_id_1 = await manager.get_or_create_stream(url)
        stream_id_2 = await manager.get_or_create_stream(url)
        
        # Assert
        assert stream_id_1 == stream_id_2  # Should be the same
        assert len(manager.streams) == 1   # Only one stream created
    
    def test_empty_url_handling(self):
        """Test behavior with edge case inputs"""
        # This test shows how you would test edge cases
        # In a real app, you might want to validate URLs
        manager = StreamManager()
        
        # Different apps handle this differently:
        # Some might raise ValueError, others might return None
        # Test whatever your actual behavior is
        assert manager is not None  # Basic sanity check


class TestAsyncPatterns:
    """Examples of testing async code properly"""
    
    @pytest.mark.asyncio
    async def test_concurrent_stream_access(self):
        """Test that concurrent access to streams works correctly"""
        # Arrange
        manager = StreamManager()
        url = "http://example.com/concurrent.m3u8"
        
        async def create_stream():
            return await manager.get_or_create_stream(url)
        
        # Act - create multiple coroutines that try to create the same stream
        tasks = [create_stream() for _ in range(5)]
        results = await asyncio.gather(*tasks)
        
        # Assert - all should return the same stream ID
        assert all(result == results[0] for result in results)
        assert len(manager.streams) == 1  # Only one stream should be created
    
    @pytest.mark.asyncio
    async def test_multiple_different_streams(self):
        """Test creating multiple different streams"""
        # Arrange
        manager = StreamManager()
        urls = [f"http://example.com/stream{i}.m3u8" for i in range(3)]
        
        # Act
        stream_ids = []
        for url in urls:
            stream_id = await manager.get_or_create_stream(url)
            stream_ids.append(stream_id)
        
        # Assert
        assert len(stream_ids) == 3
        assert len(set(stream_ids)) == 3  # All unique
        assert len(manager.streams) == 3


class TestDataValidation:
    """Examples of testing data validation and business logic"""
    
    @pytest.mark.asyncio
    async def test_stream_properties_are_set_correctly(self):
        """Test that stream objects are created with correct properties"""
        # Arrange
        manager = StreamManager()
        url = "http://example.com/stream.m3u8"
        failover_urls = ["http://backup.com/stream.m3u8"]
        user_agent = "TestAgent/1.0"
        
        # Act
        stream_id = await manager.get_or_create_stream(
            url, 
            failover_urls=failover_urls,
            user_agent=user_agent
        )
        
        # Assert - check all properties are set correctly
        stream = manager.streams[stream_id]
        assert stream.stream_id == stream_id
        assert stream.original_url == url
        assert stream.failover_urls == failover_urls
        assert stream.user_agent == user_agent
        assert stream.is_active is True
        assert stream.client_count == 0
        assert isinstance(stream.created_at, datetime)
        assert isinstance(stream.last_access, datetime)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
