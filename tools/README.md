# Tools

This directory contains utility scripts and tools for the m3u-proxy application.

## Scripts

### `performance_test.py`
Performance testing utility for benchmarking the m3u-proxy server.

**Usage:**
```bash
python tools/performance_test.py --proxy-url http://localhost:8085 --output results.json --verbose
```

**Features:**
- Stream creation performance testing
- Concurrent client connection testing
- Failover speed testing
- Channel zapping (client disconnect/reconnect) testing
- JSON output for results analysis

### `m3u_client.py`
Command-line client for interacting with the m3u-proxy API.

**Usage:**
```bash
python tools/m3u_client.py create "http://example.com/stream.m3u8"
python tools/m3u_client.py list
python tools/m3u_client.py stats
```

**Features:**
- Create and manage streams
- List active streams and clients
- View server statistics
- Interactive CLI interface

### `demo_events.py`
Demonstration script showing the event system functionality.

**Usage:**
```bash
python tools/demo_events.py
```

**Features:**
- Shows webhook configuration
- Demonstrates event emission
- Example of custom event handlers

### `run_tests.py`
Enhanced test runner with additional options beyond basic pytest.

**Usage:**
```bash
python tools/run_tests.py              # Run all tests
python tools/run_tests.py --unit       # Run only unit tests
python tools/run_tests.py --integration # Run only integration tests
python tools/run_tests.py --coverage   # Run with coverage report
```

**Features:**
- Selective test running
- Coverage reporting
- Integration test isolation

## Requirements

All tools use the same dependencies as the main application. Make sure you have the virtual environment activated and dependencies installed:

```bash
source .venv/bin/activate
pip install -r requirements.txt
```
