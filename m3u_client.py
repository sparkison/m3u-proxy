#!/usr/bin/env python3

import requests
import json
import argparse
import time
from urllib.parse import quote

class M3UProxyClient:
    def __init__(self, base_url="http://localhost:8001"):
        self.base_url = base_url
    
    def create_stream(self, url, failover_urls=None, user_agent=None):
        """Create a new stream"""
        data = {"url": url}
        if failover_urls:
            data["failover_urls"] = failover_urls
        if user_agent:
            data["user_agent"] = user_agent
        
        response = requests.post(f"{self.base_url}/streams", json=data)
        return response.json()
    
    def list_streams(self):
        """List all streams"""
        response = requests.get(f"{self.base_url}/streams")
        return response.json()
    
    def get_stream_info(self, stream_id):
        """Get detailed stream information"""
        response = requests.get(f"{self.base_url}/streams/{stream_id}")
        return response.json()
    
    def delete_stream(self, stream_id):
        """Delete a stream"""
        response = requests.delete(f"{self.base_url}/streams/{stream_id}")
        return response.json()
    
    def trigger_failover(self, stream_id):
        """Trigger manual failover"""
        response = requests.post(f"{self.base_url}/streams/{stream_id}/failover")
        return response.json()
    
    def get_stats(self):
        """Get comprehensive statistics"""
        response = requests.get(f"{self.base_url}/stats")
        return response.json()
    
    def get_health(self):
        """Get health status"""
        response = requests.get(f"{self.base_url}/health")
        return response.json()
    
    def format_bytes(self, bytes_count):
        """Format bytes in human readable format"""
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if bytes_count < 1024:
                return f"{bytes_count:.1f} {unit}"
            bytes_count /= 1024
        return f"{bytes_count:.1f} PB"
    
    def print_stats(self):
        """Print formatted statistics"""
        stats = self.get_stats()
        proxy_stats = stats["proxy_stats"]
        
        print("=" * 60)
        print("M3U PROXY - STATISTICS")
        print("=" * 60)
        print(f"Uptime: {proxy_stats['uptime_seconds']} seconds")
        print(f"Total Streams: {proxy_stats['total_streams']}")
        print(f"Active Streams: {proxy_stats['active_streams']}")
        print(f"Total Clients: {proxy_stats['total_clients']}")
        print(f"Active Clients: {proxy_stats['active_clients']}")
        print(f"Total Data Served: {self.format_bytes(proxy_stats['total_bytes_served'])}")
        print(f"Total Segments Served: {proxy_stats['total_segments_served']}")
        print()
        
        if stats["streams"]:
            print("ACTIVE STREAMS:")
            print("-" * 60)
            for stream in stats["streams"]:
                print(f"Stream ID: {stream['stream_id'][:16]}...")
                print(f"  URL: {stream['original_url'][:50]}...")
                print(f"  User Agent: {stream['user_agent'][:50]}...")
                print(f"  Clients: {stream['client_count']}")
                print(f"  Data Served: {self.format_bytes(stream['total_bytes_served'])}")
                print(f"  Segments: {stream['total_segments_served']}")
                print(f"  Errors: {stream['error_count']}")
                print(f"  Has Failover: {stream['has_failover']}")
                print(f"  Last Access: {stream['last_access']}")
                print()
        
        if stats["clients"]:
            print("ACTIVE CLIENTS:")
            print("-" * 60)
            for client in stats["clients"]:
                print(f"Client ID: {client['client_id']}")
                print(f"  Stream: {client['stream_id'][:16] if client['stream_id'] else 'None'}...")
                print(f"  User Agent: {client['user_agent']}")
                print(f"  IP: {client['ip_address']}")
                print(f"  Data: {self.format_bytes(client['bytes_served']) if client['bytes_served'] else '0 B'}")
                print(f"  Segments: {client['segments_served'] or 0}")
                print()

def main():
    parser = argparse.ArgumentParser(description="m3u-proxy Client")
    parser.add_argument("--base-url", default="http://localhost:8001", 
                       help="Base URL of the proxy server")
    
    subparsers = parser.add_subparsers(dest="command", help="Available commands")
    
    # Create stream
    create_parser = subparsers.add_parser("create", help="Create a new stream")
    create_parser.add_argument("url", help="Primary stream URL")
    create_parser.add_argument("--failover", nargs="*", help="Failover URLs")
    create_parser.add_argument("--user-agent", help="Custom user agent string")
    
    # List streams
    subparsers.add_parser("list", help="List all streams")
    
    # Stream info
    info_parser = subparsers.add_parser("info", help="Get stream information")
    info_parser.add_argument("stream_id", help="Stream ID")
    
    # Delete stream
    delete_parser = subparsers.add_parser("delete", help="Delete a stream")
    delete_parser.add_argument("stream_id", help="Stream ID")
    
    # Failover
    failover_parser = subparsers.add_parser("failover", help="Trigger failover")
    failover_parser.add_argument("stream_id", help="Stream ID")
    
    # Stats
    subparsers.add_parser("stats", help="Show statistics")
    
    # Health
    subparsers.add_parser("health", help="Check health")
    
    # Monitor
    subparsers.add_parser("monitor", help="Monitor in real-time")
    
    args = parser.parse_args()
    
    client = M3UProxyClient(args.base_url)
    
    try:
        if args.command == "create":
            result = client.create_stream(args.url, args.failover, args.user_agent)
            print(json.dumps(result, indent=2))
            
        elif args.command == "list":
            result = client.list_streams()
            if result["streams"]:
                for stream in result["streams"]:
                    stream_type = "HLS" if stream['original_url'].lower().endswith('.m3u8') else "Direct"
                    print(f"{stream['stream_id']}: {stream['original_url']} ({stream_type})")
            else:
                print("No active streams")
                
        elif args.command == "info":
            result = client.get_stream_info(args.stream_id)
            print(json.dumps(result, indent=2))
            
        elif args.command == "delete":
            result = client.delete_stream(args.stream_id)
            print(result["message"])
            
        elif args.command == "failover":
            result = client.trigger_failover(args.stream_id)
            print(json.dumps(result, indent=2))
            
        elif args.command == "stats":
            client.print_stats()
            
        elif args.command == "health":
            result = client.get_health()
            print(json.dumps(result, indent=2))
            
        elif args.command == "monitor":
            print("Monitoring m3u-proxy (Press Ctrl+C to stop)...")
            try:
                while True:
                    client.print_stats()
                    time.sleep(5)
                    print("\n" + "="*60 + "\n")
            except KeyboardInterrupt:
                print("\nMonitoring stopped.")
                
        else:
            parser.print_help()
            
    except requests.RequestException as e:
        print(f"Error: {e}")
    except Exception as e:
        print(f"Unexpected error: {e}")

if __name__ == "__main__":
    main()
