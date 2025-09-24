import asyncio
import aiohttp
import logging
import time
import psutil
from typing import Dict, List, Optional, Set
from datetime import datetime
import uuid
import json
import ffmpeg
import m3u8
import os
import signal
import subprocess
from pathlib import Path

from .models import (
    StreamConfig, StreamInfo, StreamStatus, StreamFormat, 
    ClientInfo, StreamEvent, EventType, StreamSeekRequest
)
from .events import EventManager
from .config import config


logger = logging.getLogger(__name__)


class StreamManager:
    def __init__(self, event_manager: EventManager):
        self.event_manager = event_manager
        self.streams: Dict[str, StreamInfo] = {}
        self.stream_processes: Dict[str, subprocess.Popen] = {}
        self.stream_configs: Dict[str, StreamConfig] = {}
        self.clients: Dict[str, Set[str]] = {}  # stream_id -> set of client_ids
        self.client_info: Dict[str, ClientInfo] = {}
        self._running = False
        self._monitor_task: Optional[asyncio.Task] = None

    async def start(self):
        """Start the stream manager and monitoring task."""
        self._running = True
        self._monitor_task = asyncio.create_task(self._monitor_streams())
        logger.info("Stream manager started")

    async def stop(self):
        """Stop the stream manager and all active streams."""
        self._running = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

        # Stop all active streams
        for stream_id in list(self.streams.keys()):
            await self.stop_stream(stream_id)

        logger.info("Stream manager stopped")

    async def create_stream(self, config: StreamConfig) -> StreamInfo:
        """Create a new stream with the given configuration."""
        stream_info = StreamInfo(
            stream_id=config.stream_id,
            status=StreamStatus.IDLE,
            current_url=config.primary_url,
            current_url_index=0,
            is_live=True
        )

        self.streams[config.stream_id] = stream_info
        self.stream_configs[config.stream_id] = config
        self.clients[config.stream_id] = set()

        logger.info(f"Created stream {config.stream_id}")
        return stream_info

    async def start_stream(self, stream_id: str) -> StreamInfo:
        """Start a stream if it's not already running."""
        if stream_id not in self.streams:
            raise ValueError(f"Stream {stream_id} not found")

        stream_info = self.streams[stream_id]
        config = self.stream_configs[stream_id]

        # If already running, return current info
        if stream_info.status == StreamStatus.RUNNING:
            logger.info(f"Stream {stream_id} already running")
            return stream_info

        stream_info.status = StreamStatus.STARTING
        stream_info.started_at = datetime.utcnow()

        try:
            # Detect format if not specified
            if config.auto_detect_format and not config.format:
                detected_format = await self._detect_format(config.primary_url)
                config.format = detected_format
                stream_info.format = detected_format

            # Get stream metadata
            metadata = await self._get_stream_metadata(config.primary_url)
            if metadata:
                stream_info.duration = metadata.get('duration')
                stream_info.bitrate = metadata.get('bit_rate')
                stream_info.resolution = metadata.get('resolution')
                stream_info.fps = metadata.get('fps')
                stream_info.is_live = metadata.get('duration') is None

            # Start FFmpeg process
            process = await self._start_ffmpeg_process(config, stream_info)
            self.stream_processes[stream_id] = process

            stream_info.status = StreamStatus.RUNNING
            stream_info.last_error = None

            # Emit event
            await self.event_manager.emit_event(StreamEvent(
                event_type=EventType.STREAM_STARTED,
                stream_id=stream_id,
                data={"url": str(config.primary_url), "format": config.format}
            ))

            logger.info(f"Started stream {stream_id}")
            return stream_info

        except Exception as e:
            stream_info.status = StreamStatus.FAILED
            stream_info.last_error = str(e)
            
            await self.event_manager.emit_event(StreamEvent(
                event_type=EventType.STREAM_FAILED,
                stream_id=stream_id,
                data={"error": str(e), "url": str(config.primary_url)}
            ))
            
            logger.error(f"Failed to start stream {stream_id}: {e}")
            raise

    async def stop_stream(self, stream_id: str) -> bool:
        """Stop a running stream."""
        if stream_id not in self.streams:
            return False

        stream_info = self.streams[stream_id]
        
        # Stop FFmpeg process
        if stream_id in self.stream_processes:
            process = self.stream_processes[stream_id]
            try:
                process.terminate()
                await asyncio.sleep(1)
                if process.poll() is None:
                    process.kill()
            except Exception as e:
                logger.error(f"Error stopping FFmpeg process for stream {stream_id}: {e}")
            finally:
                del self.stream_processes[stream_id]

        # Disconnect all clients
        for client_id in list(self.clients[stream_id]):
            await self.disconnect_client(stream_id, client_id)

        stream_info.status = StreamStatus.STOPPED
        
        await self.event_manager.emit_event(StreamEvent(
            event_type=EventType.STREAM_STOPPED,
            stream_id=stream_id
        ))

        logger.info(f"Stopped stream {stream_id}")
        return True

    async def connect_client(self, stream_id: str, client_id: str, ip_address: str, user_agent: str = None) -> bool:
        """Connect a client to a stream."""
        if stream_id not in self.streams:
            return False

        # Start stream if not running
        stream_info = self.streams[stream_id]
        if stream_info.status != StreamStatus.RUNNING:
            await self.start_stream(stream_id)

        # Add client
        self.clients[stream_id].add(client_id)
        self.client_info[client_id] = ClientInfo(
            client_id=client_id,
            stream_id=stream_id,
            connected_at=datetime.utcnow(),
            ip_address=ip_address,
            user_agent=user_agent
        )

        stream_info.client_count = len(self.clients[stream_id])

        await self.event_manager.emit_event(StreamEvent(
            event_type=EventType.CLIENT_CONNECTED,
            stream_id=stream_id,
            data={"client_id": client_id, "ip_address": ip_address}
        ))

        logger.info(f"Connected client {client_id} to stream {stream_id}")
        return True

    async def disconnect_client(self, stream_id: str, client_id: str) -> bool:
        """Disconnect a client from a stream."""
        if stream_id not in self.clients or client_id not in self.clients[stream_id]:
            return False

        self.clients[stream_id].remove(client_id)
        if client_id in self.client_info:
            del self.client_info[client_id]

        if stream_id in self.streams:
            self.streams[stream_id].client_count = len(self.clients[stream_id])

        await self.event_manager.emit_event(StreamEvent(
            event_type=EventType.CLIENT_DISCONNECTED,
            stream_id=stream_id,
            data={"client_id": client_id}
        ))

        logger.info(f"Disconnected client {client_id} from stream {stream_id}")
        return True

    async def seek_stream(self, stream_id: str, position: float) -> bool:
        """Seek to a specific position in a VOD stream."""
        if stream_id not in self.streams:
            return False

        stream_info = self.streams[stream_id]
        if stream_info.is_live:
            return False  # Cannot seek in live streams

        # Update position
        stream_info.position = position
        
        # TODO: Implement actual seeking in FFmpeg process
        # This would require restarting the FFmpeg process with -ss parameter
        
        logger.info(f"Seeking stream {stream_id} to position {position}s")
        return True

    async def get_stream_info(self, stream_id: str) -> Optional[StreamInfo]:
        """Get information about a stream."""
        return self.streams.get(stream_id)

    async def list_streams(self) -> List[StreamInfo]:
        """List all streams."""
        return list(self.streams.values())

    async def get_client_info(self, client_id: str) -> Optional[ClientInfo]:
        """Get information about a client."""
        return self.client_info.get(client_id)

    async def list_clients(self, stream_id: str = None) -> List[ClientInfo]:
        """List clients, optionally filtered by stream."""
        if stream_id:
            client_ids = self.clients.get(stream_id, set())
            return [self.client_info[cid] for cid in client_ids if cid in self.client_info]
        return list(self.client_info.values())

    async def _monitor_streams(self):
        """Monitor stream processes and handle failover."""
        while self._running:
            try:
                for stream_id, process in list(self.stream_processes.items()):
                    if process.poll() is not None:  # Process has terminated
                        logger.warning(f"Stream {stream_id} process terminated")
                        await self._handle_stream_failure(stream_id)
                
                await asyncio.sleep(5)  # Check every 5 seconds
            except Exception as e:
                logger.error(f"Error in stream monitor: {e}")
                await asyncio.sleep(5)

    async def _handle_stream_failure(self, stream_id: str):
        """Handle stream failure and attempt failover."""
        if stream_id not in self.stream_configs:
            return

        config = self.stream_configs[stream_id]
        stream_info = self.streams[stream_id]

        # Try failover URLs
        if config.failover_urls and stream_info.current_url_index < len(config.failover_urls):
            next_index = stream_info.current_url_index + 1
            if next_index == 1:  # First failover (from primary)
                next_url = config.failover_urls[0]
            else:
                next_url = config.failover_urls[next_index - 1]

            logger.info(f"Attempting failover for stream {stream_id} to URL {next_index}")
            
            stream_info.current_url = next_url
            stream_info.current_url_index = next_index

            try:
                # Restart with new URL
                process = await self._start_ffmpeg_process(config, stream_info)
                self.stream_processes[stream_id] = process
                stream_info.status = StreamStatus.RUNNING
                
                await self.event_manager.emit_event(StreamEvent(
                    event_type=EventType.FAILOVER_TRIGGERED,
                    stream_id=stream_id,
                    data={"url": str(next_url), "url_index": next_index}
                ))
                
                logger.info(f"Failover successful for stream {stream_id}")
                return
            except Exception as e:
                logger.error(f"Failover failed for stream {stream_id}: {e}")

        # No more failover URLs or all failed
        stream_info.status = StreamStatus.FAILED
        stream_info.last_error = "All URLs failed"
        
        await self.event_manager.emit_event(StreamEvent(
            event_type=EventType.STREAM_FAILED,
            stream_id=stream_id,
            data={"error": "All URLs failed"}
        ))

    async def _detect_format(self, url: str) -> Optional[StreamFormat]:
        """Auto-detect stream format from URL or content."""
        url_str = str(url).lower()
        
        # Check URL extension
        if url_str.endswith('.m3u8'):
            return StreamFormat.HLS
        elif url_str.endswith('.ts'):
            return StreamFormat.MPEG_TS
        elif url_str.endswith('.mkv'):
            return StreamFormat.MKV
        elif url_str.endswith('.mp4'):
            return StreamFormat.MP4
        elif url_str.endswith('.webm'):
            return StreamFormat.WEBM
        elif url_str.endswith('.avi'):
            return StreamFormat.AVI

        # Try to probe with FFmpeg
        try:
            probe = ffmpeg.probe(str(url), timeout=10)
            if probe and 'streams' in probe:
                # Analyze first video stream
                for stream in probe['streams']:
                    if stream.get('codec_type') == 'video':
                        codec_name = stream.get('codec_name', '').lower()
                        if 'h264' in codec_name or 'h265' in codec_name:
                            return StreamFormat.MPEG_TS  # Default for H.264/H.265
                        break
        except Exception as e:
            logger.warning(f"Could not probe URL {url}: {e}")

        return StreamFormat.MPEG_TS  # Default fallback

    async def _get_stream_metadata(self, url: str) -> Optional[Dict]:
        """Get stream metadata using FFprobe."""
        try:
            probe = ffmpeg.probe(str(url), timeout=10)
            metadata = {}
            
            if 'format' in probe:
                metadata['duration'] = probe['format'].get('duration')
                if metadata['duration']:
                    metadata['duration'] = float(metadata['duration'])
                metadata['bit_rate'] = probe['format'].get('bit_rate')
                if metadata['bit_rate']:
                    metadata['bit_rate'] = int(metadata['bit_rate'])

            if 'streams' in probe:
                for stream in probe['streams']:
                    if stream.get('codec_type') == 'video':
                        width = stream.get('width')
                        height = stream.get('height')
                        if width and height:
                            metadata['resolution'] = f"{width}x{height}"
                        
                        fps = stream.get('r_frame_rate', '0/1')
                        if '/' in fps:
                            num, den = fps.split('/')
                            if den != '0':
                                metadata['fps'] = float(num) / float(den)
                        break

            return metadata
        except Exception as e:
            logger.warning(f"Could not get metadata for URL {url}: {e}")
            return None

    async def _start_ffmpeg_process(self, config: StreamConfig, stream_info: StreamInfo) -> subprocess.Popen:
        """Start FFmpeg process for streaming."""
        input_url = str(stream_info.current_url)
        output_port = 8000 + hash(config.stream_id) % 10000  # Simple port assignment
        
        # Build FFmpeg command
        cmd = ['ffmpeg', '-y']
        
        # Hardware acceleration
        if config.enable_hardware_acceleration and self._has_hardware_acceleration():
            cmd.extend(['-hwaccel', 'auto'])
            
        # Input options
        cmd.extend(['-i', input_url])
        
        # Seeking support for VOD
        if stream_info.position and not stream_info.is_live:
            cmd.extend(['-ss', str(stream_info.position)])
            
        # Output options
        if config.format == StreamFormat.HLS:
            cmd.extend([
                '-c', 'copy',
                '-f', 'hls',
                '-hls_time', '6',
                '-hls_list_size', '10',
                '-hls_flags', 'delete_segments',
                f'{config.temp_dir}/stream_{config.stream_id}/playlist.m3u8'
            ])
        else:
            cmd.extend([
                '-c', 'copy',
                '-f', 'mpegts',
                f'http://localhost:{output_port}/stream'
            ])
        
        # Create output directory if needed
        if config.format == StreamFormat.HLS:
            os.makedirs(f'{config.temp_dir}/stream_{config.stream_id}', exist_ok=True)
        
        logger.info(f"Starting FFmpeg with command: {' '.join(cmd)}")
        
        # Start process
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=os.setsid
        )
        
        return process

    def _has_hardware_acceleration(self) -> bool:
        """Check if hardware acceleration is available."""
        # Check for NVIDIA GPU
        try:
            result = subprocess.run(['nvidia-smi'], capture_output=True, timeout=5)
            if result.returncode == 0:
                return True
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
            
        # Check for Intel Quick Sync (Linux)
        if os.path.exists('/dev/dri'):
            return True
            
        # Check for macOS VideoToolbox
        try:
            result = subprocess.run(['ffmpeg', '-hwaccels'], capture_output=True, timeout=5)
            if b'videotoolbox' in result.stdout:
                return True
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
            
        return False
