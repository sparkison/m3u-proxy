#!/usr/bin/env python3
"""
Test the new POST API and user agent features
"""
import requests
import json

BASE_URL = "http://localhost:8001"

def test_post_api():
    """Test the new POST JSON API"""
    print("🧪 Testing POST JSON API with User Agents")
    print("=" * 50)
    
    test_cases = [
        {
            "name": "HLS with custom user agent",
            "data": {
                "url": "http://example.com/live.m3u8",
                "user_agent": "MyIPTVApp/2.0"
            }
        },
        {
            "name": "Direct TS with failover and user agent",
            "data": {
                "url": "http://example.com/live.ts", 
                "failover_urls": ["http://backup.com/live.ts"],
                "user_agent": "VLC/3.0.20"
            }
        },
        {
            "name": "MKV with multiple failovers",
            "data": {
                "url": "http://example.com/movie.mkv",
                "failover_urls": [
                    "http://backup1.com/movie.mkv",
                    "http://backup2.com/movie.mkv"
                ],
                "user_agent": "Kodi/20.0"
            }
        },
        {
            "name": "Stream without user agent (should use default)",
            "data": {
                "url": "http://example.com/default.mp4"
            }
        }
    ]
    
    created_streams = []
    
    for test_case in test_cases:
        print(f"\n📡 {test_case['name']}")
        print(f"   URL: {test_case['data']['url']}")
        
        try:
            response = requests.post(f"{BASE_URL}/streams", json=test_case['data'])
            if response.status_code == 200:
                result = response.json()
                print(f"   ✅ Success! Stream ID: {result['stream_id'][:16]}...")
                print(f"   📱 User Agent: {result['user_agent'][:50]}...")
                print(f"   🎯 Type: {result['stream_type']}")
                print(f"   🔗 Endpoint: {result['stream_endpoint']}")
                created_streams.append(result['stream_id'])
            else:
                print(f"   ❌ Failed: {response.status_code}")
        except Exception as e:
            print(f"   ❌ Error: {e}")
    
    return created_streams

def test_cli_compatibility():
    """Test CLI client works with new features"""
    print(f"\n🖥️  CLI Client Test")
    print("=" * 50)
    
    import subprocess
    import sys
    
    # Test CLI create with user agent
    cmd = [
        sys.executable, "m3u_client.py", "create", 
        "http://example.com/cli-test.ts",
        "--user-agent", "CLI-Test/1.0",
        "--failover", "http://backup.com/cli-test.ts"
    ]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            data = json.loads(result.stdout)
            print(f"   ✅ CLI Success! Stream ID: {data['stream_id'][:16]}...")
            print(f"   📱 User Agent: {data['user_agent']}")
            print(f"   🔄 Failover URLs: {len(data['failover_urls'])} configured")
        else:
            print(f"   ❌ CLI Failed: {result.stderr}")
    except Exception as e:
        print(f"   ❌ CLI Error: {e}")

def main():
    print("🚀 M3U PROXY - POST API & USER AGENT TESTS")
    print("=" * 60)
    
    # Test POST API
    streams = test_post_api()
    
    # Test CLI compatibility  
    test_cli_compatibility()
    
    print(f"\n📊 Summary:")
    print(f"   Created {len(streams)} streams via POST API")
    print(f"   All tests completed!")
    
    # Show final stats
    try:
        stats_response = requests.get(f"{BASE_URL}/stats")
        if stats_response.status_code == 200:
            stats = stats_response.json()
            print(f"   Total active streams: {stats['proxy_stats']['active_streams']}")
            print(f"   Different user agents in use:")
            for stream in stats['streams'][-3:]:  # Show last 3
                ua = stream['user_agent'][:30]
                print(f"     • {ua}...")
    except:
        pass

if __name__ == "__main__":
    main()
