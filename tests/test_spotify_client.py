"""Spotify API client tests with mocked HTTP. Covers the auth-token
caching, search response parsing, and graceful failure paths so we don't
hit the real Spotify API in CI."""
from __future__ import annotations
import io
import json
import time
from unittest.mock import patch

from korder.spotify_client import SpotifyClient


def _mock_response(body: dict):
    """Build a urllib-like response object whose .read() returns JSON."""
    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def read(self): return json.dumps(body).encode("utf-8")
    return _Resp()


def _patched_urlopen(handler):
    """Decorator that monkey-patches urllib.request.urlopen for the test."""
    return patch("korder.spotify_client.urllib.request.urlopen", side_effect=handler)


def test_search_returns_first_track_uri():
    calls: list[str] = []

    def handler(req, *args, **kwargs):
        url = req.full_url
        calls.append(url)
        if url.startswith("https://accounts.spotify.com/api/token"):
            return _mock_response({"access_token": "tok-123", "expires_in": 3600})
        if url.startswith("https://api.spotify.com/v1/search"):
            return _mock_response({
                "tracks": {"items": [{"uri": "spotify:track:abc"}]}
            })
        raise AssertionError(f"unexpected URL {url!r}")

    with _patched_urlopen(handler):
        c = SpotifyClient("cid", "secret")
        uri = c.search_track("Despacito")
    assert uri == "spotify:track:abc"
    # Should have called token endpoint once + search once
    assert any("/api/token" in u for u in calls)
    assert any("/v1/search" in u for u in calls)


def test_token_is_cached_across_calls():
    token_calls = []

    def handler(req, *args, **kwargs):
        url = req.full_url
        if url.startswith("https://accounts.spotify.com/api/token"):
            token_calls.append(url)
            return _mock_response({"access_token": "tok-cached", "expires_in": 3600})
        return _mock_response({"tracks": {"items": [{"uri": "spotify:track:x"}]}})

    with _patched_urlopen(handler):
        c = SpotifyClient("cid", "secret")
        c.search_track("a")
        c.search_track("b")
        c.search_track("c")
    # Token endpoint hit only once; subsequent searches reuse cached token
    assert len(token_calls) == 1


def test_no_results_returns_none():
    def handler(req, *args, **kwargs):
        url = req.full_url
        if url.startswith("https://accounts.spotify.com/api/token"):
            return _mock_response({"access_token": "tok", "expires_in": 3600})
        return _mock_response({"tracks": {"items": []}})

    with _patched_urlopen(handler):
        c = SpotifyClient("cid", "secret")
        assert c.search_track("absolutely-nothing") is None


def test_empty_query_returns_none_without_api_call():
    """Empty query short-circuits before any HTTP."""
    with patch("korder.spotify_client.urllib.request.urlopen") as mock_open:
        c = SpotifyClient("cid", "secret")
        assert c.search_track("") is None
        assert c.search_track("   ") is None
    mock_open.assert_not_called()


def test_missing_credentials_returns_none():
    """No client_id/secret → no token, no search, no exception."""
    with patch("korder.spotify_client.urllib.request.urlopen") as mock_open:
        c = SpotifyClient("", "")
        assert c.search_track("hello") is None
    mock_open.assert_not_called()


def test_token_fetch_failure_returns_none():
    """API down or wrong creds → graceful None, not an exception."""
    def handler(req, *args, **kwargs):
        raise OSError("network down")

    with _patched_urlopen(handler):
        c = SpotifyClient("cid", "secret")
        assert c.search_track("hello") is None
