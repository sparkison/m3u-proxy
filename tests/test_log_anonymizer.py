"""
Tests for src/log_anonymizer.py.

Prompted by a real leak: Plex's X-Plex-Token is deliberately pulled out of the
stream URL into a raw FFmpeg -headers argument (so the URL itself carries no
token), which meant the anonymizer's whole-URL redaction never saw it and it
was logged in plaintext in the broadcast start command line.
"""

from src.log_anonymizer import _scrub


def test_redacts_full_urls():
    text = "Fetching http://192.168.1.5:32400/library/parts/1/2/file.mkv?extra=1"
    assert "192.168.1.5" not in _scrub(text)
    assert "****" in _scrub(text)


def test_redacts_plex_token_header_outside_url():
    """The exact field bug: X-Plex-Token as a raw FFmpeg -headers argument, not
    part of any URL, so whole-URL redaction alone can't catch it."""
    text = "-headers X-Plex-Token: ad7iQDh2xvJ1vBThYy54\n -i ****"
    result = _scrub(text)
    assert "ad7iQDh2xvJ1vBThYy54" not in result
    assert "X-Plex-Token: ****" in result


def test_redacts_emby_token_header():
    text = "-headers X-Emby-Token: deadbeef1234"
    result = _scrub(text)
    assert "deadbeef1234" not in result
    assert "X-Emby-Token: ****" in result


def test_redacts_emby_api_key_in_url():
    """Emby/Jellyfin's api_key travels as a URL query param — covered by the
    whole-URL redaction, but verified explicitly since it's the other integration
    the user asked to confirm."""
    text = "http://emby.local:8096/Videos/1/stream.ts?api_key=abcdef1234&static=true"
    result = _scrub(text)
    assert "abcdef1234" not in result


def test_redacts_bearer_authorization_header_including_token():
    """Authorization headers carry a scheme prefix + token separated by a space
    ('Bearer <token>') — a naive whitespace-terminated match would only swallow
    the word 'Bearer' and leave the actual token exposed."""
    text = "-headers Authorization: Bearer sometoken.jwt.here"
    result = _scrub(text)
    assert "sometoken.jwt.here" not in result
    assert "Bearer" not in result


def test_redacts_emby_structured_authorization_header():
    """Emby/Jellyfin's X-Emby-Authorization is a structured
    'MediaBrowser Client="...", Token="..."' value — must not leak the
    embedded Token= value even though it's behind an internal quote."""
    text = '-headers X-Emby-Authorization: MediaBrowser Client="Emby Theater", Token="abc123"'
    result = _scrub(text)
    assert "abc123" not in result
    assert "Emby Theater" not in result


def test_redacts_password_and_username():
    text = "WebDAV auth username=admin password=hunter2"
    result = _scrub(text)
    assert "admin" not in result
    assert "hunter2" not in result


def test_redacts_uuids():
    text = "network_id=0756b386-fbe0-42ab-a4b5-2898a7fa9552"
    result = _scrub(text)
    assert "0756b386-fbe0-42ab-a4b5-2898a7fa9552" not in result


def test_leaves_unrelated_text_untouched():
    text = "Broadcast started with PID 12345, exit code 0"
    assert _scrub(text) == text
