# Add src to path first
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import pytest
from unittest.mock import Mock, MagicMock
from fastapi import Request
from api import get_client_info


class TestClientIPDetection:
    """Test X-Forwarded-For header detection for client IP"""

    def test_x_forwarded_for_single_ip(self):
        """Test X-Forwarded-For with single IP"""
        request = Mock(spec=Request)
        request.headers = {"x-forwarded-for": "192.168.1.100"}
        request.client = Mock(host="10.0.0.1")
        
        client_info = get_client_info(request)
        
        assert client_info["ip_address"] == "192.168.1.100"

    def test_x_forwarded_for_multiple_ips(self):
        """Test X-Forwarded-For with multiple IPs (proxy chain)"""
        request = Mock(spec=Request)
        # Format: "client, proxy1, proxy2"
        request.headers = {"x-forwarded-for": "192.168.1.100, 10.0.0.5, 10.0.0.6"}
        request.client = Mock(host="10.0.0.1")
        
        client_info = get_client_info(request)
        
        # Should return the first IP (original client)
        assert client_info["ip_address"] == "192.168.1.100"

    def test_x_forwarded_for_with_spaces(self):
        """Test X-Forwarded-For with extra spaces"""
        request = Mock(spec=Request)
        request.headers = {"x-forwarded-for": "  192.168.1.100  ,  10.0.0.5  "}
        request.client = Mock(host="10.0.0.1")
        
        client_info = get_client_info(request)
        
        # Should strip spaces correctly
        assert client_info["ip_address"] == "192.168.1.100"

    def test_fallback_to_client_host(self):
        """Test fallback to request.client.host when X-Forwarded-For not present"""
        request = Mock(spec=Request)
        request.headers = {}
        request.client = Mock(host="10.0.0.1")
        
        client_info = get_client_info(request)
        
        assert client_info["ip_address"] == "10.0.0.1"

    def test_no_client_and_no_header(self):
        """Test when neither X-Forwarded-For nor client.host available"""
        request = Mock(spec=Request)
        request.headers = {}
        request.client = None
        
        client_info = get_client_info(request)
        
        assert client_info["ip_address"] == "unknown"

    def test_empty_x_forwarded_for(self):
        """Test empty X-Forwarded-For header"""
        request = Mock(spec=Request)
        request.headers = {"x-forwarded-for": ""}
        request.client = Mock(host="10.0.0.1")
        
        client_info = get_client_info(request)
        
        # Should fallback to client.host when X-Forwarded-For is empty
        assert client_info["ip_address"] == "10.0.0.1"

    def test_user_agent_extraction(self):
        """Test user agent extraction works alongside IP detection"""
        request = Mock(spec=Request)
        request.headers = {
            "x-forwarded-for": "192.168.1.100",
            "user-agent": "TestClient/1.0"
        }
        request.client = Mock(host="10.0.0.1")
        
        client_info = get_client_info(request)
        
        assert client_info["ip_address"] == "192.168.1.100"
        assert client_info["user_agent"] == "TestClient/1.0"

    def test_ipv6_address_in_x_forwarded_for(self):
        """Test IPv6 address in X-Forwarded-For"""
        request = Mock(spec=Request)
        request.headers = {"x-forwarded-for": "2001:0db8:85a3::8a2e:0370:7334"}
        request.client = Mock(host="10.0.0.1")
        
        client_info = get_client_info(request)
        
        assert client_info["ip_address"] == "2001:0db8:85a3::8a2e:0370:7334"

    def test_case_insensitive_header(self):
        """Test that header lookup is case-insensitive"""
        request = Mock(spec=Request)
        # FastAPI normalizes headers to lowercase, so we test with lowercase
        request.headers = MagicMock()
        request.headers.get = MagicMock(side_effect=lambda key, default=None: 
            "192.168.1.100" if key.lower() == "x-forwarded-for" else default)
        request.client = Mock(host="10.0.0.1")
        
        client_info = get_client_info(request)
        
        assert client_info["ip_address"] == "192.168.1.100"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
