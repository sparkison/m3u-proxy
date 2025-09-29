#!/usr/bin/env python3
"""
Performance testing script for m3u-proxy

Tests key scenarios:
1. Multiple concurrent clients on same stream (connection pooling)
2. Immediate cleanup when clients disconnect (channel zapping)
3. Failover speed and transparency
4. Stream creation/destruction performance
"""

import asyncio
import httpx
import time
import json
import logging
from typing import List, Dict, Optional
from datetime import datetime
import argparse

# Set up logging
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class PerformanceTest:
    def __init__(self, proxy_url: str = "http://localhost:8085"):
        self.proxy_url = proxy_url
        self.client = httpx.AsyncClient(timeout=30.0)
        self.results = {}

    async def setup(self):
        """Setup test environment"""
        try:
            response = await self.client.get(f"{self.proxy_url}/health")
            if response.status_code != 200:
                raise Exception(f"Proxy not healthy: {response.status_code}")
            logger.info("Proxy is healthy and ready for testing")
        except Exception as e:
            logger.error(f"Failed to connect to proxy: {e}")
            raise

    async def cleanup(self):
        """Cleanup test environment"""
        await self.client.aclose()

    async def test_stream_creation_performance(self, num_streams: int = 10) -> Dict:
        """Test stream creation performance"""
        logger.info(
            f"Testing stream creation performance with {num_streams} streams")

        test_urls = [
            "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8",
            "https://devstreaming-cdn.apple.com/videos/streaming/examples/img_bipbop_adv_example_fmp4/master.m3u8",
        ]

        start_time = time.time()
        created_streams = []

        for i in range(num_streams):
            try:
                url = test_urls[i % len(test_urls)]
                payload = {
                    "url": url,
                    "failover_urls": [test_urls[(i + 1) % len(test_urls)]],
                    "user_agent": f"PerformanceTest-{i}/1.0"
                }

                response = await self.client.post(f"{self.proxy_url}/streams", json=payload)
                if response.status_code == 200:
                    stream_data = response.json()
                    created_streams.append(stream_data["stream_id"])
                else:
                    logger.error(
                        f"Failed to create stream {i}: {response.status_code}")

            except Exception as e:
                logger.error(f"Error creating stream {i}: {e}")

        end_time = time.time()
        total_time = end_time - start_time

        # Cleanup created streams
        for stream_id in created_streams:
            try:
                await self.client.delete(f"{self.proxy_url}/streams/{stream_id}")
            except Exception as e:
                logger.warning(f"Failed to cleanup stream {stream_id}: {e}")

        result = {
            "test": "stream_creation_performance",
            "requested_streams": num_streams,
            "successful_streams": len(created_streams),
            "total_time_seconds": round(total_time, 2),
            "streams_per_second": round(len(created_streams) / total_time, 2),
            "average_creation_time": round(total_time / max(1, len(created_streams)), 3)
        }

        logger.info(f"Stream creation test completed: {result}")
        return result

    async def test_concurrent_clients_same_stream(self, num_clients: int = 5) -> Dict:
        """Test multiple clients connecting to the same stream (connection pooling)"""
        logger.info(
            f"Testing concurrent clients on same stream with {num_clients} clients")

        # Create a test stream
        payload = {
            "url": "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8",
            "user_agent": "PerformanceTest/1.0"
        }

        response = await self.client.post(f"{self.proxy_url}/streams", json=payload)
        if response.status_code != 200:
            raise Exception(
                f"Failed to create test stream: {response.status_code}")

        stream_data = response.json()
        stream_id = stream_data["stream_id"]
        playlist_url = f"{self.proxy_url}{stream_data['playlist_url']}"

        logger.info(f"Created test stream: {stream_id}")

        async def client_task(client_id: int) -> Dict:
            """Individual client task"""
            client_start = time.time()
            bytes_received = 0
            requests_made = 0

            try:
                # Simulate client behavior: fetch playlist, then segments
                for i in range(3):  # Fetch playlist 3 times
                    response = await self.client.get(f"{playlist_url}?client_id=perf_test_{client_id}_{i}")
                    if response.status_code == 200:
                        requests_made += 1
                        bytes_received += len(response.content)
                        # Small delay between requests
                        await asyncio.sleep(0.1)

            except Exception as e:
                logger.error(f"Client {client_id} error: {e}")

            client_end = time.time()
            return {
                "client_id": client_id,
                "duration": round(client_end - client_start, 2),
                "bytes_received": bytes_received,
                "requests_made": requests_made
            }

        # Start all clients concurrently
        start_time = time.time()

        # Get initial stats
        initial_stats = await self.client.get(f"{self.proxy_url}/stats")
        initial_data = initial_stats.json() if initial_stats.status_code == 200 else {}

        # Run concurrent clients
        client_tasks = [client_task(i) for i in range(num_clients)]
        client_results = await asyncio.gather(*client_tasks, return_exceptions=True)

        end_time = time.time()

        # Get final stats
        final_stats = await self.client.get(f"{self.proxy_url}/stats")
        final_data = final_stats.json() if final_stats.status_code == 200 else {}

        # Cleanup
        try:
            await self.client.delete(f"{self.proxy_url}/streams/{stream_id}")
        except Exception as e:
            logger.warning(f"Failed to cleanup stream {stream_id}: {e}")

        # Process results
        successful_clients = [r for r in client_results if isinstance(r, dict)]
        total_bytes = sum(c["bytes_received"] for c in successful_clients)
        total_requests = sum(c["requests_made"] for c in successful_clients)

        result = {
            "test": "concurrent_clients_same_stream",
            "num_clients": num_clients,
            "successful_clients": len(successful_clients),
            "total_time_seconds": round(end_time - start_time, 2),
            "total_bytes_received": total_bytes,
            "total_requests_made": total_requests,
            "average_client_duration": round(sum(c["duration"] for c in successful_clients) / max(1, len(successful_clients)), 2),
            "requests_per_second": round(total_requests / (end_time - start_time), 2),
            "connection_efficiency": "unknown"  # Would need more detailed monitoring
        }

        logger.info(f"Concurrent clients test completed: {result}")
        return result

    async def test_channel_zapping_cleanup(self, num_zaps: int = 10) -> Dict:
        """Test immediate cleanup when clients disconnect (channel zapping simulation)"""
        logger.info(
            f"Testing channel zapping cleanup with {num_zaps} channel changes")

        test_urls = [
            "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8",
            "https://devstreaming-cdn.apple.com/videos/streaming/examples/img_bipbop_adv_example_fmp4/master.m3u8",
        ]

        start_time = time.time()
        cleanup_times = []
        created_streams = []

        for i in range(num_zaps):
            zap_start = time.time()

            # Create stream
            payload = {
                "url": test_urls[i % len(test_urls)],
                "user_agent": f"ZappingTest-{i}/1.0"
            }

            response = await self.client.post(f"{self.proxy_url}/streams", json=payload)
            if response.status_code != 200:
                logger.error(f"Failed to create stream for zap {i}")
                continue

            stream_data = response.json()
            stream_id = stream_data["stream_id"]
            created_streams.append(stream_id)

            # Make a quick request to activate the stream
            playlist_url = f"{self.proxy_url}{stream_data['playlist_url']}"
            try:
                await self.client.get(f"{playlist_url}?client_id=zap_test_{i}")
            except:
                pass  # Don't fail the test if this doesn't work

            # Small delay to let connection establish
            await asyncio.sleep(0.1)

            # "Zap away" - delete the stream (simulating client disconnect)
            try:
                await self.client.delete(f"{self.proxy_url}/streams/{stream_id}")
            except Exception as e:
                logger.warning(f"Failed to delete stream {stream_id}: {e}")

            zap_end = time.time()
            cleanup_times.append(zap_end - zap_start)

            # Brief pause between zaps
            await asyncio.sleep(0.05)

        end_time = time.time()

        # Cleanup any remaining streams
        for stream_id in created_streams:
            try:
                await self.client.delete(f"{self.proxy_url}/streams/{stream_id}")
            except:
                pass

        result = {
            "test": "channel_zapping_cleanup",
            "num_zaps": num_zaps,
            "successful_zaps": len(cleanup_times),
            "total_time_seconds": round(end_time - start_time, 2),
            "average_zap_time": round(sum(cleanup_times) / max(1, len(cleanup_times)), 3),
            "zaps_per_second": round(len(cleanup_times) / (end_time - start_time), 2),
            "cleanup_times": cleanup_times
        }

        logger.info(f"Channel zapping test completed: {result}")
        return result

    async def test_failover_performance(self) -> Dict:
        """Test failover speed and transparency"""
        logger.info("Testing failover performance")

        # Use URLs where first one might fail and second should work
        payload = {
            "url": "http://invalid-url-that-will-fail.com/stream.m3u8",  # This should fail
            # This should work
            "failover_urls": ["https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8"],
            "user_agent": "FailoverTest/1.0"
        }

        start_time = time.time()

        response = await self.client.post(f"{self.proxy_url}/streams", json=payload)
        if response.status_code != 200:
            return {"test": "failover_performance", "error": "Failed to create stream"}

        stream_data = response.json()
        stream_id = stream_data["stream_id"]

        creation_time = time.time() - start_time

        # Try to access the stream to trigger failover
        playlist_url = f"{self.proxy_url}{stream_data['playlist_url']}"

        access_start = time.time()
        try:
            response = await self.client.get(f"{playlist_url}?client_id=failover_test", timeout=60.0)
            access_success = response.status_code == 200
        except Exception as e:
            logger.error(f"Error accessing stream during failover test: {e}")
            access_success = False

        access_time = time.time() - access_start
        total_time = time.time() - start_time

        # Cleanup
        try:
            await self.client.delete(f"{self.proxy_url}/streams/{stream_id}")
        except Exception as e:
            logger.warning(f"Failed to cleanup stream {stream_id}: {e}")

        result = {
            "test": "failover_performance",
            "creation_time_seconds": round(creation_time, 2),
            "access_time_seconds": round(access_time, 2),
            "total_time_seconds": round(total_time, 2),
            "access_successful": access_success,
            # If successful, failover was transparent
            "transparent_failover": access_success
        }

        logger.info(f"Failover test completed: {result}")
        return result

    async def run_all_tests(self) -> Dict:
        """Run all performance tests"""
        logger.info("Starting comprehensive performance testing")

        all_results = {
            "test_suite": "m3u-proxy_performance",
            "timestamp": datetime.now().isoformat(),
            "proxy_url": self.proxy_url,
            "tests": {}
        }

        try:
            # Test 1: Stream creation performance
            all_results["tests"]["stream_creation"] = await self.test_stream_creation_performance(10)

            # Test 2: Concurrent clients (connection pooling)
            all_results["tests"]["concurrent_clients"] = await self.test_concurrent_clients_same_stream(5)

            # Test 3: Channel zapping cleanup
            all_results["tests"]["channel_zapping"] = await self.test_channel_zapping_cleanup(10)

            # Test 4: Failover performance
            all_results["tests"]["failover"] = await self.test_failover_performance()

            # Get final system stats
            try:
                stats_response = await self.client.get(f"{self.proxy_url}/stats")
                if stats_response.status_code == 200:
                    all_results["final_system_stats"] = stats_response.json()
            except:
                pass

            all_results["status"] = "completed"
            logger.info("All performance tests completed successfully")

        except Exception as e:
            logger.error(f"Error during testing: {e}")
            all_results["status"] = "failed"
            all_results["error"] = str(e)

        return all_results


async def main():
    parser = argparse.ArgumentParser(
        description='m3u-proxy Performance Testing')
    parser.add_argument(
        '--proxy-url', default='http://localhost:8085', help='Proxy URL')
    parser.add_argument('--output', help='Output file for results (JSON)')
    parser.add_argument('--verbose', action='store_true',
                        help='Verbose logging')

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    test = PerformanceTest(args.proxy_url)

    try:
        await test.setup()
        results = await test.run_all_tests()

        # Print results
        print("\n" + "="*50)
        print("PERFORMANCE TEST RESULTS")
        print("="*50)
        print(json.dumps(results, indent=2))

        # Save to file if requested
        if args.output:
            with open(args.output, 'w') as f:
                json.dump(results, f, indent=2)
            print(f"\nResults saved to {args.output}")

    finally:
        await test.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
