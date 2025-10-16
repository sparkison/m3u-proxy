"""
Example of initializing StreamManager with Redis support
"""

import asyncio
from src.stream_manager import StreamManager

async def main():
    print("🚀 Stream Manager Initialization Examples")
    print("=" * 50)
    
    # Example 1: Current behavior (individual processes)
    print("1️⃣ Individual Process Mode (Current)")
    stream_manager_individual = StreamManager(enable_pooling=False)
    await stream_manager_individual.start()
    
    print("   ✅ Individual mode: Each client gets dedicated FFmpeg process")
    print("   💡 Best for: Development, debugging, maximum isolation")
    await stream_manager_individual.stop()
    
    # Example 2: Local pooling (no Redis)  
    print("\n2️⃣ Local Pooling Mode (Single Worker)")
    try:
        stream_manager_pooled = StreamManager(enable_pooling=True)
        await stream_manager_pooled.start()
        
        print("   ✅ Local pooling: Clients share FFmpeg processes locally")
        print("   💡 Best for: Single server deployment, resource efficiency")
        await stream_manager_pooled.stop()
    except Exception as e:
        print(f"   ⚠️  Local pooling not available: {e}")
    
    # Example 3: Redis pooling (multi-worker)
    print("\n3️⃣ Redis Pooling Mode (Multi-Worker)")
    try:
        stream_manager_redis = StreamManager(
            redis_url="redis://localhost:6379/0",
            enable_pooling=True
        )
        await stream_manager_redis.start()
        
        print("   ✅ Redis pooling: Multi-worker coordination with shared processes")  
        print("   💡 Best for: Production, multiple API servers, horizontal scaling")
        await stream_manager_redis.stop()
    except Exception as e:
        print(f"   ⚠️  Redis pooling not available: {e}")
        print(f"      To enable: pip install redis && redis-server")
    
    print("\n📊 Architecture Comparison:")
    print("   Individual: 100 clients = 100 FFmpeg processes")
    print("   Pooled:     100 clients = ~10 FFmpeg processes (same URL+profile)")
    print("   Redis:      100 clients across 5 workers = ~10 shared processes")

if __name__ == "__main__":
    asyncio.run(main())