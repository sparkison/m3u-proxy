"""
Tests for API token authentication
"""
import pytest
import sys
import os
from unittest.mock import patch
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


@pytest.fixture
def client_with_auth():
    """Create a test client with authentication enabled"""
    with patch('config.settings') as mock_settings:
        mock_settings.API_TOKEN = "test_token_123"
        mock_settings.ROOT_PATH = ""
        mock_settings.DOCS_URL = "/docs"
        mock_settings.REDOC_URL = "/redoc"
        mock_settings.OPENAPI_URL = "/openapi.json"
        
        # Import after patching settings
        from api import app
        return TestClient(app)


@pytest.fixture
def client_without_auth():
    """Create a test client with authentication disabled"""
    with patch('config.settings') as mock_settings:
        mock_settings.API_TOKEN = None
        mock_settings.ROOT_PATH = ""
        mock_settings.DOCS_URL = "/docs"
        mock_settings.REDOC_URL = "/redoc"
        mock_settings.OPENAPI_URL = "/openapi.json"
        
        # Import after patching settings
        from api import app
        return TestClient(app)


class TestAuthentication:
    """Test API token authentication"""
    
    def test_protected_endpoint_without_token_when_auth_enabled(self, client_with_auth):
        """Protected endpoints should return 401 when token is missing"""
        response = client_with_auth.get("/health")
        assert response.status_code == 401
        assert "API token required" in response.json()["detail"]
    
    def test_protected_endpoint_with_invalid_token(self, client_with_auth):
        """Protected endpoints should return 403 with invalid token"""
        response = client_with_auth.get(
            "/health",
            headers={"X-API-Token": "wrong_token"}
        )
        assert response.status_code == 403
        assert "Invalid API token" in response.json()["detail"]
    
    def test_protected_endpoint_with_valid_token(self, client_with_auth):
        """Protected endpoints should work with valid token"""
        response = client_with_auth.get(
            "/health",
            headers={"X-API-Token": "test_token_123"}
        )
        # Should get a successful response (200) or error unrelated to auth
        assert response.status_code != 401
        assert response.status_code != 403
    
    def test_all_protected_endpoints_require_token(self, client_with_auth):
        """All management endpoints should require authentication"""
        protected_endpoints = [
            ("/", "GET"),
            ("/stats", "GET"),
            ("/stats/detailed", "GET"),
            ("/stats/performance", "GET"),
            ("/stats/streams", "GET"),
            ("/stats/clients", "GET"),
            ("/clients", "GET"),
            ("/streams", "GET"),
            ("/health", "GET"),
            ("/webhooks", "GET"),
        ]
        
        for endpoint, method in protected_endpoints:
            if method == "GET":
                response = client_with_auth.get(endpoint)
            elif method == "POST":
                response = client_with_auth.post(endpoint)
            elif method == "DELETE":
                response = client_with_auth.delete(endpoint)
            
            # Should get 401 without token
            assert response.status_code in [401, 403], f"{endpoint} should require authentication"
    
    def test_no_auth_required_when_token_not_configured(self, client_without_auth):
        """When API_TOKEN is not set, endpoints should be accessible"""
        response = client_without_auth.get("/health")
        # Should not get auth errors
        assert response.status_code not in [401, 403]
    
    def test_stream_endpoints_not_protected(self, client_with_auth):
        """Stream playback endpoints should not require authentication"""
        # Note: These will return 404 if stream doesn't exist, but not 401/403
        stream_endpoints = [
            "/hls/test_stream/playlist.m3u8",
            "/stream/test_stream",
        ]
        
        for endpoint in stream_endpoints:
            response = client_with_auth.get(endpoint)
            # Should not get auth errors (might get 404 though)
            assert response.status_code not in [401, 403], \
                f"{endpoint} should not require authentication"
    
    def test_case_insensitive_header(self, client_with_auth):
        """API token header should be case-insensitive"""
        # FastAPI/Starlette normalizes headers
        response = client_with_auth.get(
            "/health",
            headers={"x-api-token": "test_token_123"}
        )
        # Should work regardless of case
        assert response.status_code != 401
        assert response.status_code != 403


class TestAuthenticationIntegration:
    """Integration tests for authentication with actual operations"""
    
    def test_create_stream_with_auth(self, client_with_auth):
        """Creating a stream should work with valid token"""
        response = client_with_auth.post(
            "/streams",
            headers={"X-API-Token": "test_token_123"},
            json={"url": "http://example.com/stream.m3u8"}
        )
        # Should not get auth errors
        assert response.status_code not in [401, 403]
    
    def test_create_stream_without_auth_fails(self, client_with_auth):
        """Creating a stream should fail without token"""
        response = client_with_auth.post(
            "/streams",
            json={"url": "http://example.com/stream.m3u8"}
        )
        assert response.status_code == 401
