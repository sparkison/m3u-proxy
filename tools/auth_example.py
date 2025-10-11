#!/usr/bin/env python3
"""
Example script demonstrating API token authentication usage with m3u-proxy.

This script shows how to:
1. Create streams with authentication
2. List streams
3. Get statistics
4. Delete streams

Usage:
    # Set your API token
    export API_TOKEN="your_secret_token"
    
    # Run the script
    python auth_example.py
"""

import os
import requests
from typing import Optional

# Configuration
BASE_URL = os.getenv("PROXY_URL", "http://localhost:8085")
API_TOKEN = os.getenv("API_TOKEN")


def get_headers() -> dict:
    """Get headers with API token if configured"""
    headers = {"Content-Type": "application/json"}
    if API_TOKEN:
        headers["X-API-Token"] = API_TOKEN
    return headers


def create_stream(url: str, failover_urls: Optional[list] = None) -> dict:
    """Create a new stream"""
    print(f"\n📡 Creating stream: {url}")
    
    payload = {"url": url}
    if failover_urls:
        payload["failover_urls"] = failover_urls
    
    response = requests.post(
        f"{BASE_URL}/streams",
        headers=get_headers(),
        json=payload
    )
    
    if response.status_code == 401:
        print("❌ Authentication required! Set API_TOKEN environment variable.")
        return None
    elif response.status_code == 403:
        print("❌ Invalid API token!")
        return None
    elif response.status_code >= 400:
        print(f"❌ Error: {response.json().get('detail', 'Unknown error')}")
        return None
    
    data = response.json()
    print(f"✅ Stream created: {data['stream_id']}")
    print(f"   HLS URL: {data.get('hls_url', 'N/A')}")
    print(f"   Direct URL: {data.get('stream_url', 'N/A')}")
    return data


def list_streams() -> dict:
    """List all active streams"""
    print("\n📋 Listing all streams...")
    
    response = requests.get(
        f"{BASE_URL}/streams",
        headers=get_headers()
    )
    
    if response.status_code == 401:
        print("❌ Authentication required! Set API_TOKEN environment variable.")
        return None
    elif response.status_code == 403:
        print("❌ Invalid API token!")
        return None
    
    data = response.json()
    streams = data.get("streams", [])
    
    print(f"✅ Found {len(streams)} active stream(s)")
    for stream in streams:
        print(f"   - {stream['stream_id']}: {stream.get('status', 'unknown')} "
              f"({stream.get('active_clients', 0)} clients)")
    
    return data


def get_stats() -> dict:
    """Get proxy statistics"""
    print("\n📊 Getting proxy statistics...")
    
    response = requests.get(
        f"{BASE_URL}/stats",
        headers=get_headers()
    )
    
    if response.status_code == 401:
        print("❌ Authentication required! Set API_TOKEN environment variable.")
        return None
    elif response.status_code == 403:
        print("❌ Invalid API token!")
        return None
    
    data = response.json()
    print(f"✅ Statistics:")
    print(f"   Active streams: {data.get('active_streams', 0)}")
    print(f"   Active clients: {data.get('active_clients', 0)}")
    print(f"   Total bytes served: {data.get('total_bytes_served', 0):,} bytes")
    print(f"   Uptime: {data.get('uptime_seconds', 0):.0f} seconds")
    
    return data


def delete_stream(stream_id: str) -> bool:
    """Delete a stream"""
    print(f"\n🗑️  Deleting stream: {stream_id}")
    
    response = requests.delete(
        f"{BASE_URL}/streams/{stream_id}",
        headers=get_headers()
    )
    
    if response.status_code == 401:
        print("❌ Authentication required! Set API_TOKEN environment variable.")
        return False
    elif response.status_code == 403:
        print("❌ Invalid API token!")
        return False
    elif response.status_code == 404:
        print("❌ Stream not found!")
        return False
    
    print(f"✅ Stream deleted successfully")
    return True


def check_health() -> dict:
    """Check proxy health (requires auth)"""
    print("\n🏥 Checking proxy health...")
    
    response = requests.get(
        f"{BASE_URL}/health",
        headers=get_headers()
    )
    
    if response.status_code == 401:
        print("❌ Authentication required! Set API_TOKEN environment variable.")
        return None
    elif response.status_code == 403:
        print("❌ Invalid API token!")
        return None
    
    data = response.json()
    print(f"✅ Status: {data.get('status', 'unknown')}")
    print(f"   Version: {data.get('version', 'unknown')}")
    print(f"   Uptime: {data.get('uptime_seconds', 0):.0f} seconds")
    
    return data


def main():
    """Main example flow"""
    print("=" * 60)
    print("m3u-proxy API Token Authentication Example")
    print("=" * 60)
    
    if not API_TOKEN:
        print("\n⚠️  WARNING: API_TOKEN not set!")
        print("If authentication is enabled on the server, requests will fail.")
        print("Set it with: export API_TOKEN='your_token_here'")
    else:
        print(f"\n🔑 Using API token: {API_TOKEN[:8]}...{API_TOKEN[-4:]}")
    
    # Check health
    health = check_health()
    if not health:
        print("\n❌ Cannot connect to proxy or authentication failed. Exiting.")
        return
    
    # Get current stats
    get_stats()
    
    # Create a test stream
    stream_data = create_stream(
        url="https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8",
        failover_urls=["https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8"]
    )
    
    if stream_data:
        stream_id = stream_data.get("stream_id")
        
        # List all streams
        list_streams()
        
        # Get updated stats
        get_stats()
        
        # Note: Stream playback URLs don't require authentication
        print("\n🎬 Stream playback URLs (no auth required):")
        if stream_data.get("hls_url"):
            print(f"   HLS: {stream_data['hls_url']}")
        if stream_data.get("stream_url"):
            print(f"   Direct: {stream_data['stream_url']}")
        
        # Clean up - delete the stream
        if stream_id:
            delete_stream(stream_id)
    
    print("\n" + "=" * 60)
    print("Example completed!")
    print("=" * 60)


if __name__ == "__main__":
    main()
