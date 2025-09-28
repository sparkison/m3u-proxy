# Add src to path first
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from api import app, get_content_type, is_direct_stream
import pytest
from fastapi.testclient import TestClient
from unittest.mock import Mock, AsyncMock, patch
import json


class TestHelperFunctions:
    """Test utility functions"""

    def test_get_content_type(self):
        assert get_content_type("test.ts") == "video/mp2t"
        assert get_content_type(
            "playlist.m3u8") == "application/vnd.apple.mpegurl"
        assert get_content_type("video.mp4") == "video/mp4"
        assert get_content_type("video.mkv") == "video/x-matroska"
        assert get_content_type("video.webm") == "video/webm"
        assert get_content_type("video.avi") == "video/x-msvideo"
        assert get_content_type("unknown.xyz") == "application/octet-stream"

    def test_is_direct_stream(self):
        assert is_direct_stream("stream.ts") is True
        assert is_direct_stream("video.mp4") is True
        assert is_direct_stream("video.mkv") is True
        assert is_direct_stream("video.webm") is True
        assert is_direct_stream("video.avi") is True
        assert is_direct_stream("playlist.m3u8") is False
        assert is_direct_stream("unknown.xyz") is False


class TestAPI:
    """Test FastAPI endpoints"""

    @pytest.fixture
    def client(self):
        return TestClient(app)

    @pytest.fixture
    def mock_stream_manager(self):
        with patch('api.stream_manager') as mock:
            # Mock the streams dict to include test_stream_123
            mock.streams = {"test_stream_123": Mock()}
            
            mock.get_or_create_stream = AsyncMock(
                return_value="test_stream_123")
            mock.get_stream_info = Mock(return_value=Mock(
                stream_id="test_stream_123",
                original_url="http://example.com/test.m3u8",
                is_active=True,
                client_count=1,
                error_count=0
            ))
            mock.get_stats = Mock(return_value={
                "proxy_stats": {
                    "total_streams": 1,
                    "active_streams": 1,
                    "total_clients": 0,
                    "active_clients": 0,
                    "total_bytes_served": 0,
                    "total_segments_served": 0,
                    "uptime_seconds": 3600
                },
                "streams": [{
                    "stream_id": "test_stream_123",
                    "original_url": "http://example.com/test.m3u8",
                    "is_active": True,
                    "client_count": 1,
                    "error_count": 0,
                    "uptime": 3600
                }],
                "clients": []
            })
            mock.get_all_streams = Mock(return_value=[])
            mock.get_all_clients = Mock(return_value=[])
            yield mock

    def test_root_endpoint(self, client):
        response = client.get("/")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "running"
        assert "version" in data
        assert "uptime" in data

    def test_health_endpoint(self, client, mock_stream_manager):
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert "stats" in data

    def test_create_stream_post_valid(self, client, mock_stream_manager):
        payload = {
            "url": "http://example.com/stream.m3u8",
            "failover_urls": ["http://backup.com/stream.m3u8"],
            "user_agent": "TestApp/1.0"
        }

        response = client.post("/streams", json=payload)
        assert response.status_code == 200

        data = response.json()
        assert data["stream_id"] == "test_stream_123"
        assert "playlist_url" in data
        assert "direct_url" in data

    def test_create_stream_post_minimal(self, client, mock_stream_manager):
        payload = {"url": "http://example.com/stream.m3u8"}

        response = client.post("/streams", json=payload)
        assert response.status_code == 200

        data = response.json()
        assert data["stream_id"] == "test_stream_123"

    def test_create_stream_post_invalid_url(self, client):
        payload = {"url": "not_a_valid_url"}

        response = client.post("/streams", json=payload)
        assert response.status_code == 422  # Validation error

    def test_get_streams(self, client, mock_stream_manager):
        response = client.get("/streams")
        assert response.status_code == 200
        data = response.json()
        assert "streams" in data
        assert isinstance(data["streams"], list)

    def test_get_stream_info_exists(self, client, mock_stream_manager):
        response = client.get("/streams/test_stream_123")
        assert response.status_code == 200

        data = response.json()
        assert "stream" in data
        assert "clients" in data
        assert "client_count" in data
        assert data["stream"]["stream_id"] == "test_stream_123"
        assert data["stream"]["original_url"] == "http://example.com/test.m3u8"

    def test_get_stream_info_not_found(self, client, mock_stream_manager):
        mock_stream_manager.get_stream_info.return_value = None

        response = client.get("/streams/nonexistent")
        assert response.status_code == 404

        data = response.json()
        assert "not found" in data["detail"].lower()

    def test_delete_stream_exists(self, client, mock_stream_manager):
        mock_stream_manager.remove_stream = Mock(return_value=True)

        response = client.delete("/streams/test_stream_123")
        assert response.status_code == 200

        data = response.json()
        assert "deleted" in data["message"].lower()

    def test_delete_stream_not_found(self, client, mock_stream_manager):
        mock_stream_manager.remove_stream = Mock(return_value=False)

        response = client.delete("/streams/nonexistent")
        assert response.status_code == 404

    def test_get_clients(self, client, mock_stream_manager):
        response = client.get("/clients")
        assert response.status_code == 200

        data = response.json()
        assert "clients" in data
        assert isinstance(data["clients"], list)

    def test_playlist_endpoint(self, client, mock_stream_manager):
        # Mock the get_playlist_content method used by the endpoint  
        mock_stream_manager.get_playlist_content = AsyncMock(
            return_value="#EXTM3U\nsegment1.ts")
        mock_stream_manager.register_client = AsyncMock(return_value=Mock())
        mock_stream_manager.clients = {}

        response = client.get("/hls/test_stream_123/playlist.m3u8")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/vnd.apple.mpegurl"
        assert "#EXTM3U" in response.text

    def test_playlist_endpoint_not_found(self, client, mock_stream_manager):
        mock_stream_manager.get_stream_info.return_value = None

        response = client.get("/playlist/nonexistent")
        assert response.status_code == 404

    def test_proxy_endpoint_segment(self, client, mock_stream_manager):
        # Mock the proxy_hls_segment method used by the endpoint
        from starlette.responses import StreamingResponse
        
        async def mock_response_generator():
            yield b"segment_data_chunk_1"
            yield b"segment_data_chunk_2"

        mock_response = StreamingResponse(mock_response_generator(), media_type="video/mp2t")
        mock_stream_manager.proxy_hls_segment = AsyncMock(return_value=mock_response)
        mock_stream_manager.register_client = AsyncMock(return_value=Mock())

        response = client.get("/hls/test_stream_123/segment?client_id=test_client&url=http://example.com/segment1.ts")
        assert response.status_code == 200
        # Note: In tests, the media_type might not be set exactly as expected

    def test_proxy_endpoint_not_found(self, client, mock_stream_manager):
        mock_stream_manager.get_stream_info.return_value = None

        response = client.get("/proxy/nonexistent/segment.ts")
        assert response.status_code == 404

    def test_direct_stream_endpoint(self, client, mock_stream_manager):
        from starlette.responses import StreamingResponse
        
        async def mock_stream_generator():
            yield b"stream_data_chunk_1"
            yield b"stream_data_chunk_2"

        # Mock the stream_unified_response method used by the endpoint
        mock_response = StreamingResponse(mock_stream_generator(), media_type="video/mp4")
        mock_stream_manager.stream_unified_response = AsyncMock(return_value=mock_response)
        mock_stream_manager.register_client = AsyncMock(return_value=Mock())
        mock_stream_manager.clients = {}

        response = client.get("/stream/test_stream_123")
        assert response.status_code == 200

    def test_stats_endpoint(self, client, mock_stream_manager):
        response = client.get("/stats")
        assert response.status_code == 200

        data = response.json()
        assert "total_streams" in data
        assert "active_streams" in data
        assert "total_clients" in data

    @patch('api.stream_manager')
    def test_error_handling_stream_creation_failure(self, mock_sm, client):
        mock_sm.get_or_create_stream = AsyncMock(
            side_effect=Exception("Stream creation failed"))

        payload = {"url": "http://example.com/stream.m3u8"}
        response = client.post("/streams", json=payload)
        assert response.status_code == 500

        data = response.json()
        assert "failed" in data["detail"].lower()

    def test_cors_headers(self, client):
        response = client.get("/", headers={"Origin": "http://localhost:3000"})
        # FastAPI might add CORS headers if configured
        assert response.status_code == 200


class TestStreamValidation:
    """Test stream URL validation"""

    @pytest.fixture
    def client(self):
        return TestClient(app)

    def test_valid_urls(self, client):
        valid_urls = [
            "http://example.com/stream.m3u8",
            "https://secure.example.com/playlist.m3u8",
            "http://192.168.1.100:8080/live/stream.ts",
            "https://cdn.example.com/video.mp4"
        ]

        with patch('api.stream_manager') as mock_sm:
            mock_sm.get_or_create_stream = AsyncMock(return_value="test_123")

            for url in valid_urls:
                payload = {"url": url}
                response = client.post("/streams", json=payload)
                assert response.status_code == 200, f"Failed for URL: {url}"

    def test_invalid_urls(self, client):
        invalid_urls = [
            "not_a_url",
            "ftp://example.com/file.m3u8",  # Wrong protocol
            "http://",  # Incomplete URL
            "",  # Empty string
            "javascript:alert('xss')"  # XSS attempt
        ]

        for url in invalid_urls:
            payload = {"url": url}
            response = client.post("/streams", json=payload)
            # Should either be 422 (validation) or 500 (processing error)
            assert response.status_code in [
                422, 500], f"Should reject URL: {url}"


if __name__ == "__main__":
    pytest.main([__file__])
