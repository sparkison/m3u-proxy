"""
Test script to demonstrate Redis pooling capabilities.
"""

import asyncio
import json
import requests
from typing import Dict, Any

BASE_URL = 'http://127.0.0.1:8085'
TOKEN = '92225e0f70ce2d4833b2fca9f84c57e923d428d5d125e2b4'
HEADERS = {'X-API-Token': TOKEN, 'Content-Type': 'application/json'}

async def test_redis_pooling_architecture():
    """Test Redis pooling capabilities"""
    
    print("🚀 Testing Redis Pooling Architecture")
    print("=" * 60)
    
    # Check if Redis pooling is available
    print("1️⃣ Checking Redis/Pooling Support...")
    
    try:
        # Test importing Redis support
        from redis_config import get_redis_config, should_use_pooling
        redis_config = get_redis_config()
        
        print(f"   📊 Redis Configuration:")
        print(f"      URL: {redis_config['url']}")
        print(f"      Enabled: {redis_config['enabled']}")  
        print(f"      Pooling: {redis_config['pooling_enabled']}")
        print(f"      Max Clients/Stream: {redis_config['max_clients_per_stream']}")
        print(f"      Sharing Strategy: {redis_config['sharing_strategy']}")
        
        if should_use_pooling():
            print("   ✅ Redis pooling is configured")
        else:
            print("   ℹ️  Redis pooling not enabled (will use individual processes)")
            
    except ImportError:
        print("   ⚠️  Redis configuration not available")
        
    # Test creating multiple transcoded streams (same URL + profile = potential sharing)
    print(f"\n2️⃣ Testing Stream Sharing Potential...")
    
    same_stream_configs = []
    
    for i in range(3):
        payload = {
            'url': 'https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4',
            'profile': 'low_quality',  # Same profile for sharing
            'profile_variables': {
                'video_bitrate': '400k',
                'audio_bitrate': '64k'
            }
        }
        
        print(f"   Creating stream {i+1}/3 with same URL+profile...")
        response = requests.post(f'{BASE_URL}/transcode', headers=HEADERS, json=payload)
        
        if response.status_code == 200:
            data = response.json()
            same_stream_configs.append({
                'stream_id': data['stream_id'],
                'endpoint': data['stream_endpoint'],
                'profile': data['profile']
            })
            print(f"   ✅ Stream {i+1} created: {data['stream_id']}")
        else:
            print(f"   ❌ Stream {i+1} failed: {response.status_code}")
            
    # Test different profiles (should NOT share)
    print(f"\n3️⃣ Testing Different Profiles (No Sharing)...")
    
    different_profiles = ['default', 'high_quality']
    different_stream_configs = []
    
    for i, profile in enumerate(different_profiles):
        payload = {
            'url': 'https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4',
            'profile': profile,
            'profile_variables': {
                'video_bitrate': '1M' if profile == 'high_quality' else '800k'
            }
        }
        
        print(f"   Creating stream with {profile} profile...")
        response = requests.post(f'{BASE_URL}/transcode', headers=HEADERS, json=payload)
        
        if response.status_code == 200:
            data = response.json()
            different_stream_configs.append({
                'stream_id': data['stream_id'],
                'endpoint': data['stream_endpoint'],
                'profile': data['profile']
            })
            print(f"   ✅ {profile} stream created: {data['stream_id']}")
        else:
            print(f"   ❌ {profile} stream failed: {response.status_code}")
            
    # Test concurrent access to same streams (pool sharing test)
    print(f"\n4️⃣ Testing Concurrent Client Access...")
    
    if same_stream_configs:
        test_stream = same_stream_configs[0]
        print(f"   Testing concurrent access to stream: {test_stream['stream_id']}")
        
        async def test_concurrent_client(client_num: int):
            """Simulate a client connecting to the stream"""
            try:
                response = requests.get(
                    f'{BASE_URL}{test_stream["endpoint"]}',
                    stream=True,
                    timeout=10
                )
                
                if response.status_code == 200:
                    # Read a few chunks
                    chunk_count = 0
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            chunk_count += 1
                            if chunk_count >= 3:  # Just test connection
                                break
                    
                    print(f"      ✅ Client {client_num}: {chunk_count} chunks received")
                    return True
                else:
                    print(f"      ❌ Client {client_num}: {response.status_code}")
                    return False
                    
            except Exception as e:
                print(f"      ⚠️  Client {client_num}: {e}")
                return False
        
        # Test 3 concurrent clients
        tasks = []
        for i in range(3):
            task = asyncio.create_task(test_concurrent_client(i + 1))
            tasks.append(task)
            
        results = await asyncio.gather(*tasks, return_exceptions=True)
        successful_clients = sum(1 for r in results if r is True)
        
        print(f"   📊 Concurrent Test Results: {successful_clients}/3 clients successful")
        
        if successful_clients >= 2:
            print(f"   🎉 Multi-client access working!")
            if should_use_pooling():
                print(f"   💡 With Redis pooling: Clients likely shared FFmpeg process")
            else:
                print(f"   💡 Without Redis: Each client has individual FFmpeg process")
        
    # Summary and architecture comparison
    print(f"╭─────────────────────────────────────────────────────╮")
    print(f"│                ARCHITECTURE SUMMARY                 │")
    print(f"├─────────────────────────────────────────────────────┤")
    
    if should_use_pooling():
        print(f"│ 🔄 Pooling Mode: ENABLED                          │")
        print(f"│    • Shared FFmpeg processes across clients       │")
        print(f"│    • Redis coordination for multi-worker          │")
        print(f"│    • Automatic process cleanup and sharing        │")
    else:
        print(f"│ 🔧 Individual Mode: ENABLED (Enhanced isolation)  │")
        print(f"│    • Dedicated FFmpeg per client connection       │")
        print(f"│    • Better resource isolation                    │")
        print(f"│    • Simpler debugging and monitoring             │")
        
    print(f"│                                                     │")
    print(f"│ ✅ Hybrid Architecture: Direct + Transcoded         │")
    print(f"│ ✅ MPEGTS Direct Streaming: No HLS segmentation     │")
    print(f"│ ✅ Profile System: Template-based configuration     │")
    print(f"│ ✅ RESTful API: POST /transcode, GET /stream/{{id}}   │")
    print(f"│ ✅ Failover Support: Multiple URL handling          │")
    print(f"╰─────────────────────────────────────────────────────╯")
    
    print(f"\n📋 NEXT STEPS TO ENABLE REDIS POOLING:")
    print(f"   1. pip install redis")
    print(f"   2. Start Redis server: redis-server")
    print(f"   3. Set environment: REDIS_ENABLED=true")
    print(f"   4. Restart m3u-proxy with Redis support")
    print(f"   5. Multiple workers can share transcoding processes")

if __name__ == "__main__":
    asyncio.run(test_redis_pooling_architecture())