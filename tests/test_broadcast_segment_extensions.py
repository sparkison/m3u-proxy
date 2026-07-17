"""
Tests for serving .vtt/.m3u8 sub-playlist files through the broadcast segment
endpoint, not just .ts video segments.

get_segment_path() (both NetworkBroadcastProcess's and BroadcastManager's) used to
hard-code `.ts`-only, silently 404ing every request for the video/subtitle
sub-playlists the editor's hls-variant route fetches through this same endpoint —
this had zero test coverage, which is exactly how it went unnoticed through the
whole subtitle-selection feature build.
"""

from fastapi.testclient import TestClient

from src.api import app, broadcast_manager
from src.broadcast_manager import BroadcastConfig, NetworkBroadcastProcess


def _make_process(tmp_path) -> NetworkBroadcastProcess:
    config = BroadcastConfig(
        network_id="test-net", stream_url="http://example.com/stream.ts"
    )
    proc = NetworkBroadcastProcess.__new__(NetworkBroadcastProcess)
    proc.config = config
    proc.hls_dir = str(tmp_path)
    return proc


def test_process_get_segment_path_accepts_ts_vtt_and_m3u8(tmp_path):
    proc = _make_process(tmp_path)
    for name in ["live000001.ts", "live0.vtt", "live_vtt.m3u8", "live.m3u8"]:
        (tmp_path / name).write_text("data")
        assert proc.get_segment_path(name) == str(tmp_path / name)


def test_process_get_segment_path_rejects_other_extensions(tmp_path):
    proc = _make_process(tmp_path)
    (tmp_path / "live.txt").write_text("data")
    assert proc.get_segment_path("live.txt") is None


def test_process_get_segment_path_returns_none_for_missing_file(tmp_path):
    proc = _make_process(tmp_path)
    assert proc.get_segment_path("live000099.ts") is None


def test_process_get_segment_path_sanitizes_directory_traversal(tmp_path):
    proc = _make_process(tmp_path)
    outside = tmp_path.parent / "live000001.ts"
    outside.write_text("data")
    try:
        assert proc.get_segment_path("../live000001.ts") is None
    finally:
        outside.unlink()


def test_manager_get_segment_path_accepts_vtt_and_m3u8(tmp_path):
    manager_hls_dir = tmp_path / "hls"
    broadcast_dir = manager_hls_dir / "broadcast_test-net-2"
    broadcast_dir.mkdir(parents=True)
    (broadcast_dir / "live_vtt.m3u8").write_text("data")

    from src.broadcast_manager import BroadcastManager

    manager = BroadcastManager(hls_base_dir=str(manager_hls_dir))
    result = manager.get_segment_path("test-net-2", "live_vtt.m3u8")
    assert result == str(broadcast_dir / "live_vtt.m3u8")


def test_manager_get_segment_path_rejects_non_media_extension(tmp_path):
    manager_hls_dir = tmp_path / "hls"
    broadcast_dir = manager_hls_dir / "broadcast_test-net-3"
    broadcast_dir.mkdir(parents=True)
    (broadcast_dir / "config.json").write_text("{}")

    from src.broadcast_manager import BroadcastManager

    manager = BroadcastManager(hls_base_dir=str(manager_hls_dir))
    assert manager.get_segment_path("test-net-3", "config.json") is None


def test_segment_endpoint_serves_m3u8_with_no_cache_headers(tmp_path, monkeypatch):
    broadcast_dir = tmp_path / "broadcast_test-net-4"
    broadcast_dir.mkdir(parents=True)
    (broadcast_dir / "live_vtt.m3u8").write_text("#EXTM3U\n#EXT-X-ENDLIST\n")
    monkeypatch.setattr(broadcast_manager, "hls_base_dir", str(tmp_path))

    client = TestClient(app)
    response = client.get("/broadcast/test-net-4/segment/live_vtt.m3u8")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/vnd.apple.mpegurl")
    assert response.headers["cache-control"] == "no-cache, no-store, must-revalidate"


def test_segment_endpoint_serves_vtt_with_long_cache_headers(tmp_path, monkeypatch):
    broadcast_dir = tmp_path / "broadcast_test-net-5"
    broadcast_dir.mkdir(parents=True)
    (broadcast_dir / "live0.vtt").write_text("WEBVTT\n")
    monkeypatch.setattr(broadcast_manager, "hls_base_dir", str(tmp_path))

    client = TestClient(app)
    response = client.get("/broadcast/test-net-5/segment/live0.vtt")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/vtt")
    assert response.headers["cache-control"] == "max-age=86400"


def test_segment_endpoint_still_serves_ts_with_video_mp2t(tmp_path, monkeypatch):
    broadcast_dir = tmp_path / "broadcast_test-net-6"
    broadcast_dir.mkdir(parents=True)
    (broadcast_dir / "live000001.ts").write_bytes(b"\x47" * 188)
    monkeypatch.setattr(broadcast_manager, "hls_base_dir", str(tmp_path))

    client = TestClient(app)
    response = client.get("/broadcast/test-net-6/segment/live000001.ts")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(
        "video/MP2T"
    ) or response.headers["content-type"].startswith("video/mp2t")
    assert response.headers["cache-control"] == "max-age=86400"


def test_segment_endpoint_404_for_unknown_file(tmp_path, monkeypatch):
    monkeypatch.setattr(broadcast_manager, "hls_base_dir", str(tmp_path))

    client = TestClient(app)
    response = client.get("/broadcast/test-net-7/segment/live_vtt.m3u8")

    assert response.status_code == 404
