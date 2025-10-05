# Add src to path first
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import pytest
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient
from datetime import datetime
from api import app, StreamCreateRequest
from stream_manager import StreamInfo


class TestMetadataFeature:
    """Test custom metadata functionality for streams"""

    @pytest.fixture
    def client(self):
        return TestClient(app)

    @pytest.fixture
    def mock_stream_manager(self):
        with patch('api.stream_manager') as mock:
            mock.streams = {}
            mock.get_or_create_stream = AsyncMock(return_value="test_stream_metadata_123")
            mock.get_stats = Mock(return_value={
                "proxy_stats": {
                    "total_streams": 1,
                    "active_streams": 1,
                    "total_clients": 0,
                    "active_clients": 0,
                    "total_bytes_served": 0,
                    "total_segments_served": 0,
                    "uptime_seconds": 0
                },
                "streams": [],
                "clients": []
            })
            yield mock

    def test_stream_create_request_with_metadata(self):
        """Test that StreamCreateRequest accepts metadata"""
        request = StreamCreateRequest(
            url="https://example.com/stream.m3u8",
            user_agent="Test/1.0",
            metadata={
                "local_id": "test_123",
                "channel_name": "Test Channel"
            }
        )
        
        assert request.metadata is not None
        assert request.metadata["local_id"] == "test_123"
        assert request.metadata["channel_name"] == "Test Channel"

    def test_stream_create_request_without_metadata(self):
        """Test that StreamCreateRequest works without metadata (backward compatibility)"""
        request = StreamCreateRequest(
            url="https://example.com/stream.m3u8"
        )
        
        assert request.metadata is None

    def test_metadata_validation_strings(self):
        """Test metadata validation with string values"""
        request = StreamCreateRequest(
            url="https://example.com/stream.m3u8",
            metadata={
                "key1": "value1",
                "key2": "value2"
            }
        )
        
        assert request.metadata["key1"] == "value1"
        assert request.metadata["key2"] == "value2"

    def test_metadata_validation_integers(self):
        """Test metadata validation converts integers to strings"""
        request = StreamCreateRequest(
            url="https://example.com/stream.m3u8",
            metadata={
                "count": 42,
                "number": 100
            }
        )
        
        # Values should be converted to strings
        assert request.metadata["count"] == "42"
        assert request.metadata["number"] == "100"
        assert isinstance(request.metadata["count"], str)

    def test_metadata_validation_booleans(self):
        """Test metadata validation converts booleans to strings"""
        request = StreamCreateRequest(
            url="https://example.com/stream.m3u8",
            metadata={
                "active": True,
                "featured": False
            }
        )
        
        assert request.metadata["active"] == "True"
        assert request.metadata["featured"] == "False"
        assert isinstance(request.metadata["active"], str)

    def test_metadata_validation_floats(self):
        """Test metadata validation converts floats to strings"""
        request = StreamCreateRequest(
            url="https://example.com/stream.m3u8",
            metadata={
                "rating": 4.5,
                "score": 9.8
            }
        )
        
        assert request.metadata["rating"] == "4.5"
        assert request.metadata["score"] == "9.8"
        assert isinstance(request.metadata["rating"], str)

    def test_metadata_validation_mixed_types(self):
        """Test metadata validation with mixed types"""
        request = StreamCreateRequest(
            url="https://example.com/stream.m3u8",
            metadata={
                "name": "Test",
                "count": 10,
                "active": True,
                "rating": 3.14
            }
        )
        
        # All values should be strings
        assert all(isinstance(v, str) for v in request.metadata.values())
        assert request.metadata["name"] == "Test"
        assert request.metadata["count"] == "10"
        assert request.metadata["active"] == "True"
        assert request.metadata["rating"] == "3.14"

    def test_metadata_validation_rejects_nested_objects(self):
        """Test that nested objects in metadata are rejected"""
        with pytest.raises(Exception):  # Should raise validation error
            StreamCreateRequest(
                url="https://example.com/stream.m3u8",
                metadata={
                    "nested": {"key": "value"}
                }
            )

    def test_metadata_validation_rejects_arrays(self):
        """Test that arrays in metadata are rejected"""
        with pytest.raises(Exception):  # Should raise validation error
            StreamCreateRequest(
                url="https://example.com/stream.m3u8",
                metadata={
                    "list": [1, 2, 3]
                }
            )

    def test_create_stream_endpoint_with_metadata(self, client, mock_stream_manager):
        """Test POST /streams endpoint with metadata"""
        response = client.post(
            "/streams",
            json={
                "url": "https://example.com/stream.m3u8",
                "metadata": {
                    "local_id": "test_123",
                    "channel_name": "Test Channel"
                }
            }
        )
        
        assert response.status_code == 200
        data = response.json()
        assert "stream_id" in data
        assert "metadata" in data
        assert data["metadata"]["local_id"] == "test_123"
        assert data["metadata"]["channel_name"] == "Test Channel"
        
        # Verify get_or_create_stream was called with metadata
        mock_stream_manager.get_or_create_stream.assert_called_once()
        call_kwargs = mock_stream_manager.get_or_create_stream.call_args.kwargs
        assert "metadata" in call_kwargs
        assert call_kwargs["metadata"]["local_id"] == "test_123"

    def test_create_stream_endpoint_without_metadata(self, client, mock_stream_manager):
        """Test POST /streams endpoint without metadata (backward compatibility)"""
        response = client.post(
            "/streams",
            json={
                "url": "https://example.com/stream.m3u8"
            }
        )
        
        assert response.status_code == 200
        data = response.json()
        assert "stream_id" in data
        # Metadata should not be in response if not provided
        assert "metadata" not in data

    def test_stream_info_metadata_storage(self):
        """Test that StreamInfo properly stores metadata"""
        now = datetime.now()
        stream_info = StreamInfo(
            stream_id="test_123",
            original_url="https://example.com/stream.m3u8",
            created_at=now,
            last_access=now,
            metadata={
                "local_id": "channel_123",
                "name": "Test Channel"
            }
        )
        
        assert stream_info.metadata is not None
        assert stream_info.metadata["local_id"] == "channel_123"
        assert stream_info.metadata["name"] == "Test Channel"

    def test_stream_info_empty_metadata_default(self):
        """Test that StreamInfo has empty metadata by default"""
        now = datetime.now()
        stream_info = StreamInfo(
            stream_id="test_123",
            original_url="https://example.com/stream.m3u8",
            created_at=now,
            last_access=now
        )
        
        assert stream_info.metadata is not None
        assert stream_info.metadata == {}
        assert len(stream_info.metadata) == 0

    def test_get_stats_includes_metadata(self, client, mock_stream_manager):
        """Test that GET /streams includes metadata in response"""
        # Mock a stream with metadata
        now = datetime.now()
        mock_stream = StreamInfo(
            stream_id="test_123",
            original_url="https://example.com/stream.m3u8",
            created_at=now,
            last_access=now,
            current_url="https://example.com/stream.m3u8",
            metadata={
                "local_id": "channel_123",
                "channel_name": "Test Channel"
            }
        )
        
        mock_stream_manager.get_stats.return_value = {
            "proxy_stats": {
                "total_streams": 1,
                "active_streams": 1,
                "total_clients": 0,
                "active_clients": 0,
                "total_bytes_served": 0,
                "total_segments_served": 0,
                "uptime_seconds": 0
            },
            "streams": [{
                "stream_id": "test_123",
                "original_url": "https://example.com/stream.m3u8",
                "current_url": "https://example.com/stream.m3u8",
                "user_agent": "Test/1.0",
                "client_count": 0,
                "total_bytes_served": 0,
                "total_segments_served": 0,
                "error_count": 0,
                "is_active": True,
                "has_failover": False,
                "stream_type": "HLS",
                "created_at": now.isoformat(),
                "last_access": now.isoformat(),
                "metadata": {
                    "local_id": "channel_123",
                    "channel_name": "Test Channel"
                }
            }],
            "clients": []
        }
        
        response = client.get("/streams")
        
        assert response.status_code == 200
        data = response.json()
        assert "streams" in data
        assert len(data["streams"]) == 1
        
        stream = data["streams"][0]
        assert "metadata" in stream
        assert stream["metadata"]["local_id"] == "channel_123"
        assert stream["metadata"]["channel_name"] == "Test Channel"

    def test_metadata_channel_identification_pattern(self):
        """Test common pattern: channel identification"""
        request = StreamCreateRequest(
            url="https://example.com/hbo.m3u8",
            metadata={
                "local_id": "channel_hbo_hd",
                "channel_name": "HBO HD",
                "channel_number": "201"
            }
        )
        
        assert request.metadata["local_id"] == "channel_hbo_hd"
        assert request.metadata["channel_name"] == "HBO HD"
        assert request.metadata["channel_number"] == "201"

    def test_metadata_content_classification_pattern(self):
        """Test common pattern: content classification"""
        request = StreamCreateRequest(
            url="https://example.com/sports.m3u8",
            metadata={
                "category": "sports",
                "subcategory": "soccer",
                "league": "premier_league",
                "quality": "1080p",
                "language": "en"
            }
        )
        
        assert request.metadata["category"] == "sports"
        assert request.metadata["quality"] == "1080p"

    def test_metadata_provider_tracking_pattern(self):
        """Test common pattern: provider tracking"""
        request = StreamCreateRequest(
            url="https://example.com/stream.m3u8",
            metadata={
                "provider": "provider_a",
                "provider_stream_id": "ext_12345",
                "region": "us-east"
            }
        )
        
        assert request.metadata["provider"] == "provider_a"
        assert request.metadata["provider_stream_id"] == "ext_12345"
        assert request.metadata["region"] == "us-east"


class TestMetadataIntegration:
    """Integration tests for metadata feature"""
    
    @pytest.fixture
    def client(self):
        return TestClient(app)

    def test_full_workflow_with_metadata(self, client):
        """Test complete workflow: create stream with metadata and retrieve it"""
        # Create stream with metadata
        create_response = client.post(
            "/streams",
            json={
                "url": "https://example.com/test-workflow.m3u8",
                "user_agent": "TestClient/1.0",
                "metadata": {
                    "local_id": "workflow_test",
                    "test_name": "Full Workflow Test",
                    "count": 42,
                    "active": True
                }
            }
        )
        
        assert create_response.status_code == 200
        create_data = create_response.json()
        stream_id = create_data["stream_id"]
        
        # Verify metadata in creation response
        assert "metadata" in create_data
        assert create_data["metadata"]["local_id"] == "workflow_test"
        assert create_data["metadata"]["count"] == "42"  # Converted to string
        assert create_data["metadata"]["active"] == "True"  # Converted to string
        
        # Retrieve streams and verify metadata persisted
        list_response = client.get("/streams")
        assert list_response.status_code == 200
        list_data = list_response.json()
        
        # Find our stream
        our_stream = next(
            (s for s in list_data["streams"] if s["stream_id"] == stream_id),
            None
        )
        
        assert our_stream is not None
        assert "metadata" in our_stream
        assert our_stream["metadata"]["local_id"] == "workflow_test"
        assert our_stream["metadata"]["test_name"] == "Full Workflow Test"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
