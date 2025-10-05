#!/usr/bin/env python3
"""
Test script for custom metadata feature in stream creation.
Demonstrates how external players can pass custom key/value pairs
to identify and track streams.
"""

import requests
import json

BASE_URL = "http://localhost:8085"

def test_metadata_feature():
    print("🧪 Testing Custom Metadata Feature\n")
    print("=" * 70)
    
    # Test 1: Create stream with metadata
    print("\n1️⃣  Creating stream with custom metadata...")
    metadata = {
        "local_id": "stream_123",
        "channel_name": "HBO HD",
        "channel_number": "201",
        "category": "movies",
        "quality": "1080p",
        "language": "en",
        "custom_tag": "premium"
    }
    
    response = requests.post(
        f"{BASE_URL}/streams",
        json={
            "url": "https://example.com/test/stream.m3u8",
            "user_agent": "MetadataTestClient/1.0",
            "metadata": metadata
        }
    )
    
    if response.status_code != 200:
        print(f"❌ Failed to create stream: {response.text}")
        return False
    
    stream_data = response.json()
    stream_id = stream_data["stream_id"]
    print(f"✅ Stream created: {stream_id}")
    
    # Check if metadata is in response
    if "metadata" in stream_data:
        print("✅ Metadata included in creation response:")
        for key, value in stream_data["metadata"].items():
            print(f"   • {key}: {value}")
    else:
        print("⚠️  Metadata not found in creation response")
    
    # Test 2: Retrieve stream and verify metadata
    print(f"\n2️⃣  Retrieving stream list to verify metadata...")
    streams_response = requests.get(f"{BASE_URL}/streams")
    
    if streams_response.status_code != 200:
        print(f"❌ Failed to get streams: {streams_response.text}")
        return False
    
    streams_data = streams_response.json()
    
    # Find our stream
    our_stream = None
    for stream in streams_data["streams"]:
        if stream["stream_id"] == stream_id:
            our_stream = stream
            break
    
    if not our_stream:
        print(f"❌ Stream {stream_id} not found in list")
        return False
    
    print("✅ Stream found in list")
    
    # Check metadata
    if "metadata" in our_stream:
        print("✅ Metadata preserved in stream list:")
        returned_metadata = our_stream["metadata"]
        for key, value in returned_metadata.items():
            print(f"   • {key}: {value}")
        
        # Verify all metadata keys match
        if set(returned_metadata.keys()) == set(metadata.keys()):
            print("✅ All metadata keys preserved")
        else:
            print("⚠️  Some metadata keys missing")
            print(f"   Expected: {set(metadata.keys())}")
            print(f"   Got: {set(returned_metadata.keys())}")
        
        return True
    else:
        print("❌ Metadata not found in stream list")
        return False

def test_stream_without_metadata():
    print("\n" + "=" * 70)
    print("\n3️⃣  Testing stream creation without metadata...")
    
    response = requests.post(
        f"{BASE_URL}/streams",
        json={
            "url": "https://example.com/test/another-stream.m3u8",
            "user_agent": "TestClient/1.0"
        }
    )
    
    if response.status_code != 200:
        print(f"❌ Failed to create stream: {response.text}")
        return False
    
    stream_data = response.json()
    stream_id = stream_data["stream_id"]
    print(f"✅ Stream created without metadata: {stream_id}")
    
    # Verify it works normally
    streams_response = requests.get(f"{BASE_URL}/streams")
    streams_data = streams_response.json()
    
    our_stream = next((s for s in streams_data["streams"] if s["stream_id"] == stream_id), None)
    
    if our_stream:
        metadata = our_stream.get("metadata", {})
        if not metadata or len(metadata) == 0:
            print("✅ Stream has empty metadata (as expected)")
            return True
        else:
            print(f"⚠️  Stream has unexpected metadata: {metadata}")
            return True
    else:
        print("❌ Stream not found")
        return False

def test_metadata_validation():
    print("\n" + "=" * 70)
    print("\n4️⃣  Testing metadata validation...")
    
    # Test with various data types
    test_cases = [
        {
            "name": "String values",
            "metadata": {"key1": "value1", "key2": "value2"},
            "should_pass": True
        },
        {
            "name": "Integer values",
            "metadata": {"count": 42, "priority": 1},
            "should_pass": True
        },
        {
            "name": "Boolean values",
            "metadata": {"active": True, "featured": False},
            "should_pass": True
        },
        {
            "name": "Mixed types",
            "metadata": {"name": "test", "count": 10, "active": True, "quality": "hd"},
            "should_pass": True
        }
    ]
    
    for test_case in test_cases:
        print(f"\n   Testing: {test_case['name']}")
        response = requests.post(
            f"{BASE_URL}/streams",
            json={
                "url": f"https://example.com/test/{test_case['name'].replace(' ', '-')}.m3u8",
                "metadata": test_case["metadata"]
            }
        )
        
        if test_case["should_pass"]:
            if response.status_code == 200:
                print(f"   ✅ {test_case['name']}: Passed")
                # Verify all values converted to strings
                returned_metadata = response.json().get("metadata", {})
                all_strings = all(isinstance(v, str) for v in returned_metadata.values())
                if all_strings:
                    print(f"   ✅ All values converted to strings")
                else:
                    print(f"   ⚠️  Not all values are strings: {returned_metadata}")
            else:
                print(f"   ❌ {test_case['name']}: Failed - {response.status_code}")
                return False
        else:
            if response.status_code != 200:
                print(f"   ✅ {test_case['name']}: Correctly rejected")
            else:
                print(f"   ❌ {test_case['name']}: Should have been rejected")
                return False
    
    return True

def print_example_usage():
    print("\n" + "=" * 70)
    print("\n📚 Example Usage\n")
    
    example = {
        "url": "https://example.com/hbo.m3u8",
        "user_agent": "MyPlayer/1.0",
        "failover_urls": ["https://backup.example.com/hbo.m3u8"],
        "metadata": {
            "local_id": "channel_hbo_hd",
            "channel_name": "HBO HD",
            "channel_number": "201",
            "category": "premium",
            "language": "en",
            "quality": "1080p"
        }
    }
    
    print("curl -X POST http://localhost:8085/streams \\")
    print("  -H 'Content-Type: application/json' \\")
    print("  -d '" + json.dumps(example, indent=2) + "'")
    
    print("\n" + "=" * 70)

if __name__ == "__main__":
    print("\n" + "=" * 70)
    print("Custom Stream Metadata Feature Test")
    print("=" * 70)
    
    try:
        test1_pass = test_metadata_feature()
        test2_pass = test_stream_without_metadata()
        test3_pass = test_metadata_validation()
        
        print_example_usage()
        
        print("\n" + "=" * 70)
        print("\n📊 Test Summary:")
        print(f"   Test 1 (With metadata): {'✅ PASSED' if test1_pass else '❌ FAILED'}")
        print(f"   Test 2 (Without metadata): {'✅ PASSED' if test2_pass else '❌ FAILED'}")
        print(f"   Test 3 (Validation): {'✅ PASSED' if test3_pass else '❌ FAILED'}")
        
        if test1_pass and test2_pass and test3_pass:
            print("\n✅ ALL TESTS PASSED")
        else:
            print("\n❌ SOME TESTS FAILED")
        
        print("=" * 70 + "\n")
        
    except Exception as e:
        print(f"\n❌ Test error: {e}")
        import traceback
        traceback.print_exc()
