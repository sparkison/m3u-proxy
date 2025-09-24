#!/usr/bin/env python3
"""
Test script to verify M3U Proxy setup and basic functionality
"""

import asyncio
import aiohttp
import json
from typing import Dict, Any


class M3UProxyTest:
    def __init__(self, base_url: str = "http://localhost:8080"):
        self.base_url = base_url
        
    async def test_health(self) -> bool:
        """Test health endpoint"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{self.base_url}/health") as response:
                    if response.status == 200:
                        data = await response.json()
                        print(f"✅ Health check passed: {data}")
                        return True
                    else:
                        print(f"❌ Health check failed: {response.status}")
                        return False
        except Exception as e:
            print(f"❌ Health check error: {e}")
            return False
    
    async def test_stats(self) -> bool:
        """Test stats endpoint"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{self.base_url}/stats") as response:
                    if response.status == 200:
                        data = await response.json()
                        print(f"✅ Stats endpoint working: {data}")
                        return True
                    else:
                        print(f"❌ Stats endpoint failed: {response.status}")
                        return False
        except Exception as e:
            print(f"❌ Stats endpoint error: {e}")
            return False
    
    async def test_create_stream(self) -> str:
        """Test stream creation"""
        stream_config = {
            "primary_url": "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4",
            "failover_urls": [],
            "auto_detect_format": True,
            "enable_hardware_acceleration": True
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/streams",
                    json=stream_config
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        stream_id = data["stream_id"]
                        print(f"✅ Stream created: {stream_id}")
                        return stream_id
                    else:
                        text = await response.text()
                        print(f"❌ Stream creation failed: {response.status} - {text}")
                        return None
        except Exception as e:
            print(f"❌ Stream creation error: {e}")
            return None
    
    async def test_list_streams(self) -> bool:
        """Test stream listing"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{self.base_url}/streams") as response:
                    if response.status == 200:
                        data = await response.json()
                        print(f"✅ Stream listing works: {len(data)} streams")
                        return True
                    else:
                        print(f"❌ Stream listing failed: {response.status}")
                        return False
        except Exception as e:
            print(f"❌ Stream listing error: {e}")
            return False
    
    async def test_webhook(self) -> bool:
        """Test webhook management"""
        webhook_config = {
            "url": "https://httpbin.org/post",
            "events": ["stream_started", "stream_stopped"],
            "timeout": 10,
            "retry_attempts": 2
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                # Add webhook
                async with session.post(
                    f"{self.base_url}/webhooks",
                    json=webhook_config
                ) as response:
                    if response.status == 200:
                        print("✅ Webhook added successfully")
                        
                        # List webhooks
                        async with session.get(f"{self.base_url}/webhooks") as list_response:
                            if list_response.status == 200:
                                data = await list_response.json()
                                print(f"✅ Webhook listing works: {len(data)} webhooks")
                                return True
                    else:
                        print(f"❌ Webhook test failed: {response.status}")
                        return False
        except Exception as e:
            print(f"❌ Webhook test error: {e}")
            return False
    
    async def run_all_tests(self) -> bool:
        """Run all tests"""
        print("🚀 Starting M3U Proxy tests...")
        print("=" * 50)
        
        tests = [
            ("Health Check", self.test_health()),
            ("Stats Endpoint", self.test_stats()),
            ("Stream Creation", self.test_create_stream()),
            ("Stream Listing", self.test_list_streams()),
            ("Webhook Management", self.test_webhook())
        ]
        
        results = []
        for name, test_coro in tests:
            print(f"\n🧪 Testing {name}...")
            try:
                result = await test_coro
                if isinstance(result, str):  # Stream ID returned
                    result = result is not None
                results.append(result if result is not None else False)
            except Exception as e:
                print(f"❌ {name} failed with exception: {e}")
                results.append(False)
        
        print("\n" + "=" * 50)
        print("📊 Test Results:")
        for i, (name, _) in enumerate(tests):
            status = "✅ PASS" if results[i] else "❌ FAIL"
            print(f"  {name}: {status}")
        
        passed = sum(results)
        total = len(results)
        print(f"\nOverall: {passed}/{total} tests passed")
        
        return passed == total


async def main():
    """Main test function"""
    import sys
    
    # Check if server URL was provided
    base_url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080"
    
    tester = M3UProxyTest(base_url)
    success = await tester.run_all_tests()
    
    if success:
        print("\n🎉 All tests passed! M3U Proxy is working correctly.")
        sys.exit(0)
    else:
        print("\n💥 Some tests failed. Check the server setup.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
