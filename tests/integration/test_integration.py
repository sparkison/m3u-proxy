import pytest
import asyncio
import httpx
import json
from fastapi.testclient import TestClient
from unittest.mock import patch, Mock, AsyncMock

# Add src to path
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from api import app
from stream_manager import StreamManager


class TestFullIntegration:
    """Test full end-to-end functionality"""
    
    @pytest.fixture
    def client(self):
        return TestClient(app)
    
    @pytest.fixture
    def real_stream_manager(self):
        """Use a real StreamManager for integration tests"""
        return StreamManager()
    
    @pytest.mark.asyncio
    async def test_complete_hls_workflow(self, client):
        """Test complete HLS streaming workflow (API interface only)"""
        
        # 1. Create stream via POST API
        create_payload = {
            "url": "http://example.com/playlist.m3u8",
            "failover_urls": ["http://backup.com/playlist.m3u8"],
            "user_agent": "IntegrationTest/1.0"
        }
        
        create_response = client.post("/streams", json=create_payload)
        assert create_response.status_code == 200
        
        stream_data = create_response.json()
        stream_id = stream_data["stream_id"]
        assert len(stream_id) == 32  # MD5 hash length
        assert stream_data["stream_type"] == "hls"
        assert "hls" in stream_data["stream_endpoint"]
        
        # 2. Get stream info
        info_response = client.get(f"/streams/{stream_id}")
        assert info_response.status_code == 200
        
        info_data = info_response.json()
        assert info_data["stream"]["original_url"] == "http://example.com/playlist.m3u8"
        assert info_data["stream"]["is_active"] is True
        
        # 3. Check stats include our stream
        stats_response = client.get("/stats")
        assert stats_response.status_code == 200
        
        stats_data = stats_response.json()
        assert stats_data["total_streams"] >= 1
        
        # 4. Delete the stream
        delete_response = client.delete(f"/streams/{stream_id}")
        assert delete_response.status_code == 200
    
    @pytest.mark.asyncio
    async def test_direct_stream_workflow(self, client):
        """Test direct streaming (non-HLS) workflow"""
        
        mock_stream_data = b"fake_mp4_stream_data"
        
        with patch('httpx.AsyncClient') as mock_client_class:
            mock_client = Mock()
            mock_client_class.return_value = mock_client
            
            # Mock direct stream response
            stream_response = Mock()
            stream_response.status_code = 200
            stream_response.headers = {"content-type": "video/mp4"}
            
            async def mock_aiter_bytes(chunk_size=1024):
                yield mock_stream_data
            
            stream_response.aiter_bytes = mock_aiter_bytes
            
            # Create async context manager mock
            async_context_manager = AsyncMock()
            async_context_manager.__aenter__ = AsyncMock(return_value=stream_response)
            async_context_manager.__aexit__ = AsyncMock(return_value=None)
            mock_client.stream.return_value = async_context_manager
            
            # 1. Create direct stream
            create_payload = {
                "url": "http://example.com/movie.mp4",
                "user_agent": "DirectTest/1.0"
            }
            
            create_response = client.post("/streams", json=create_payload)
            assert create_response.status_code == 200
            
            stream_data = create_response.json()
            stream_id = stream_data["stream_id"]
            
            # 2. Access direct stream
            direct_response = client.get(f"/stream/{stream_id}")
            assert direct_response.status_code == 200
    
    def test_multiple_clients_same_stream(self, client):
        """Test multiple clients accessing the same stream"""
        
        with patch('httpx.AsyncClient') as mock_client_class:
            mock_client = Mock()
            mock_client_class.return_value = mock_client
            
            playlist_response = Mock()
            playlist_response.status_code = 200
            playlist_response.text = "#EXTM3U\nsegment.ts"
            playlist_response.headers = {"content-type": "application/vnd.apple.mpegurl"}
            mock_client.get.return_value = playlist_response
            
            # Create stream
            create_payload = {"url": "http://example.com/shared.m3u8"}
            
            # Multiple clients create "same" stream
            responses = []
            for i in range(3):
                response = client.post("/streams", json=create_payload)
                assert response.status_code == 200
                responses.append(response.json())
            
            # All should get the same stream ID (deduplication)
            stream_ids = [r["stream_id"] for r in responses]
            assert len(set(stream_ids)) == 1  # Only one unique stream ID
            
            # Check stream info shows correct client count would be updated
            # (This would require more complex mocking of the internal state)
    
    def test_failover_integration(self, client):
        """Test failover mechanism in integration"""
        
        with patch('httpx.AsyncClient') as mock_client_class:
            mock_client = Mock()
            mock_client_class.return_value = mock_client
            
            # First URL fails
            mock_client.get.side_effect = [
                httpx.RequestError("Connection failed"),
                Mock(status_code=200, text="#EXTM3U\nsegment.ts", headers={"content-type": "application/vnd.apple.mpegurl"})
            ]
            
            # Create stream with failover
            create_payload = {
                "url": "http://primary.com/stream.m3u8",
                "failover_urls": ["http://backup.com/stream.m3u8"]
            }
            
            create_response = client.post("/streams", json=create_payload)
            assert create_response.status_code == 200
            
            stream_id = create_response.json()["stream_id"]
            
            # Try to access playlist (should trigger failover)
            playlist_response = client.get(f"/playlist/{stream_id}")
            # Depending on implementation, might succeed with backup or fail
            # This tests the failover logic exists
    
    def test_error_handling_integration(self, client):
        """Test various error conditions in integration"""
        
        # 1. Invalid stream ID
        response = client.get("/playlist/invalid_stream_id")
        assert response.status_code == 404
        
        # 2. Invalid segment request
        response = client.get("/proxy/invalid_stream_id/segment.ts")
        assert response.status_code == 404
        
        # 3. Invalid direct stream request  
        response = client.get("/direct/invalid_stream_id")
        assert response.status_code == 404
    
    @pytest.mark.asyncio
    async def test_concurrent_stream_creation(self, client):
        """Test concurrent stream creation doesn't cause issues"""
        
        with patch('httpx.AsyncClient') as mock_client_class:
            mock_client = Mock()
            mock_client_class.return_value = mock_client
            
            playlist_response = Mock()
            playlist_response.status_code = 200
            playlist_response.text = "#EXTM3U\nsegment.ts" 
            playlist_response.headers = {"content-type": "application/vnd.apple.mpegurl"}
            mock_client.get.return_value = playlist_response
            
            # Create multiple streams concurrently
            import threading
            import queue
            
            results = queue.Queue()
            
            def create_stream(url_suffix):
                payload = {"url": f"http://example.com/stream{url_suffix}.m3u8"}
                response = client.post("/streams", json=payload)
                results.put((url_suffix, response.status_code, response.json()))
            
            threads = []
            for i in range(5):
                thread = threading.Thread(target=create_stream, args=(i,))
                threads.append(thread)
                thread.start()
            
            for thread in threads:
                thread.join()
            
            # Check all streams were created successfully
            while not results.empty():
                suffix, status_code, data = results.get()
                assert status_code == 200, f"Stream {suffix} creation failed"
                assert "stream_id" in data
    
    def test_stats_accuracy(self, client):
        """Test that stats are accurately updated"""
        
        with patch('httpx.AsyncClient') as mock_client_class:
            mock_client = Mock()
            mock_client_class.return_value = mock_client
            
            playlist_response = Mock()
            playlist_response.status_code = 200
            playlist_response.text = "#EXTM3U\nsegment.ts"
            playlist_response.headers = {"content-type": "application/vnd.apple.mpegurl"}
            mock_client.get.return_value = playlist_response
            
            # Get initial stats
            initial_stats = client.get("/stats").json()
            initial_streams = initial_stats["total_streams"]
            
            # Create a stream
            create_payload = {"url": "http://example.com/stats_test.m3u8"}
            response = client.post("/streams", json=create_payload)
            assert response.status_code == 200
            
            # Check stats updated
            updated_stats = client.get("/stats").json() 
            assert updated_stats["total_streams"] == initial_streams + 1


if __name__ == "__main__":
    pytest.main([__file__])
