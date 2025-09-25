import pytest
import asyncio
from datetime import datetime
from unittest.mock import Mock, AsyncMock, patch
import httpx

# Add src to path so we can import our modules
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from stream_manager import (
    StreamManager, 
    ClientInfo, 
    StreamInfo, 
    ProxyStats,
    M3U8Processor
)


class TestClientInfo:
    """Test ClientInfo dataclass"""
    
    def test_client_info_creation(self):
        client = ClientInfo(
            client_id="test_client",
            created_at=datetime.now(),
            last_access=datetime.now()
        )
        assert client.client_id == "test_client"
        assert client.bytes_served == 0
        assert client.segments_served == 0
        assert client.user_agent is None


class TestStreamInfo:
    """Test StreamInfo dataclass"""
    
    def test_stream_info_creation(self):
        stream = StreamInfo(
            stream_id="test_stream",
            original_url="http://example.com/stream.m3u8",
            created_at=datetime.now(),
            last_access=datetime.now()
        )
        assert stream.stream_id == "test_stream"
        assert stream.original_url == "http://example.com/stream.m3u8"
        assert stream.client_count == 0
        assert stream.is_active is True
        assert len(stream.failover_urls) == 0
        assert "Mozilla" in stream.user_agent  # Default user agent


class TestM3U8Processor:
    """Test M3U8 URL processing"""
    
    def test_rewrite_playlist_urls(self):
        processor = M3U8Processor("http://original.com/", "stream123", "http://proxy.com")
        
        playlist = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXTINF:10.0,
segment1.ts
#EXTINF:10.0,
segment2.ts
#EXT-X-ENDLIST"""
        
        result = processor.rewrite_playlist_urls(playlist)
        
        assert "http://proxy.com/proxy/stream123/segment1.ts" in result
        assert "http://proxy.com/proxy/stream123/segment2.ts" in result
        assert "#EXTM3U" in result
        assert "#EXT-X-VERSION:3" in result
    
    def test_rewrite_absolute_urls(self):
        processor = M3U8Processor("http://original.com/", "stream123", "http://proxy.com")
        
        playlist = """#EXTM3U
http://original.com/segment1.ts
http://original.com/segment2.ts"""
        
        result = processor.rewrite_playlist_urls(playlist)
        
        assert "http://proxy.com/proxy/stream123/segment1.ts" in result
        assert "http://proxy.com/proxy/stream123/segment2.ts" in result


class TestStreamManager:
    """Test StreamManager functionality"""
    
    @pytest.fixture
    def stream_manager(self):
        return StreamManager()
    
    def test_generate_stream_id(self, stream_manager):
        stream_id = stream_manager.generate_stream_id("http://example.com/test.m3u8")
        assert len(stream_id) == 8
        assert isinstance(stream_id, str)
    
    def test_get_stream_info_nonexistent(self, stream_manager):
        result = stream_manager.get_stream_info("nonexistent")
        assert result is None
    
    @pytest.mark.asyncio
    async def test_create_stream(self, stream_manager):
        url = "http://example.com/test.m3u8"
        failover_urls = ["http://backup1.com/test.m3u8", "http://backup2.com/test.m3u8"]
        user_agent = "TestAgent/1.0"
        
        stream_id = await stream_manager.get_or_create_stream(
            url, 
            failover_urls=failover_urls,
            user_agent=user_agent
        )
        
        assert stream_id is not None
        assert len(stream_id) == 8
        
        # Check stream was created properly
        stream_info = stream_manager.get_stream_info(stream_id)
        assert stream_info is not None
        assert stream_info.original_url == url
        assert stream_info.failover_urls == failover_urls
        assert stream_info.user_agent == user_agent
    
    @pytest.mark.asyncio
    async def test_get_or_create_stream_existing(self, stream_manager):
        url = "http://example.com/test.m3u8"
        
        # Create stream first time
        stream_id1 = await stream_manager.get_or_create_stream(url)
        
        # Should return same stream ID for same URL
        stream_id2 = await stream_manager.get_or_create_stream(url)
        
        assert stream_id1 == stream_id2
    
    def test_get_all_streams(self, stream_manager):
        stats = stream_manager.get_stats()
        assert stats.total_streams == 0
        assert stats.active_streams == 0
        assert stats.total_clients == 0
    
    def test_register_client(self, stream_manager):
        client_id = stream_manager.register_client(
            user_agent="TestClient/1.0",
            ip_address="127.0.0.1"
        )
        
        assert client_id is not None
        assert len(client_id) == 36  # UUID4 length
        
        # Check client was registered
        client_info = stream_manager.clients.get(client_id)
        assert client_info is not None
        assert client_info.user_agent == "TestClient/1.0"
        assert client_info.ip_address == "127.0.0.1"
    
    @pytest.mark.asyncio 
    async def test_proxy_segment_success(self, stream_manager):
        # Create a stream first
        url = "http://example.com/test.m3u8"
        stream_id = await stream_manager.get_or_create_stream(url)
        
        # Mock httpx client
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "video/mp2t"}
        mock_response.aiter_bytes = AsyncMock(return_value=[b"test_segment_data"])
        
        with patch.object(stream_manager, 'client') as mock_client:
            mock_client.stream.return_value.__aenter__.return_value = mock_response
            
            segments = []
            async for segment in stream_manager.proxy_segment(stream_id, "segment1.ts"):
                segments.append(segment)
            
            assert len(segments) == 1
            assert segments[0] == b"test_segment_data"
    
    @pytest.mark.asyncio
    async def test_proxy_playlist_success(self, stream_manager):
        url = "http://example.com/playlist.m3u8"
        stream_id = await stream_manager.get_or_create_stream(url)
        
        playlist_content = """#EXTM3U
#EXT-X-VERSION:3
#EXTINF:10.0,
segment1.ts"""
        
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.text = playlist_content
        mock_response.headers = {"content-type": "application/vnd.apple.mpegurl"}
        
        with patch.object(stream_manager, 'client') as mock_client:
            mock_client.get.return_value = mock_response
            
            result = await stream_manager.proxy_playlist(stream_id, "http://proxy.com")
            
            assert "#EXTM3U" in result
            assert "http://proxy.com/proxy/" in result
            assert stream_id in result
    
    @pytest.mark.asyncio
    async def test_failover_mechanism(self, stream_manager):
        url = "http://example.com/test.m3u8"
        failover_urls = ["http://backup.com/test.m3u8"]
        
        stream_id = await stream_manager.get_or_create_stream(
            url, 
            failover_urls=failover_urls
        )
        
        # Simulate first URL failing
        with patch.object(stream_manager, 'client') as mock_client:
            # First call fails
            mock_client.get.side_effect = httpx.RequestError("Connection failed")
            
            with pytest.raises(httpx.RequestError):
                await stream_manager.proxy_playlist(stream_id, "http://proxy.com")
        
        # Check that failover was triggered
        stream_info = stream_manager.get_stream_info(stream_id)
        assert stream_info.error_count > 0


@pytest.mark.asyncio
async def test_concurrent_access(stream_manager):
    """Test concurrent access to stream manager"""
    url = "http://example.com/concurrent.m3u8"
    
    async def create_stream():
        return await stream_manager.get_or_create_stream(url)
    
    # Create multiple tasks that try to create the same stream
    tasks = [create_stream() for _ in range(10)]
    results = await asyncio.gather(*tasks)
    
    # All should return the same stream ID
    assert all(result == results[0] for result in results)
    
    # Should only have created one stream
    stats = stream_manager.get_stats()
    assert stats.total_streams == 1


if __name__ == "__main__":
    pytest.main([__file__])
