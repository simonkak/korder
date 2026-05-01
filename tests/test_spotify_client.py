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


def test_search_kind_album_returns_album_uri():
    seen_types: list[str] = []

    def handler(req, *args, **kwargs):
        url = req.full_url
        if url.startswith("https://accounts.spotify.com/api/token"):
            return _mock_response({"access_token": "tok", "expires_in": 3600})
        # Capture the type= parameter Spotify was queried with
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(url).query)
        seen_types.append(q.get("type", [""])[0])
        return _mock_response({
            "albums": {"items": [{"uri": "spotify:album:meteora"}]},
            "tracks": {"items": [{"uri": "spotify:track:numb"}]},
        })

    with _patched_urlopen(handler):
        c = SpotifyClient("cid", "secret")
        uri = c.search("Meteora", kind="album")
    assert uri == "spotify:album:meteora"
    assert seen_types == ["album"]


def test_search_kind_track_returns_track_uri():
    def handler(req, *args, **kwargs):
        url = req.full_url
        if url.startswith("https://accounts.spotify.com/api/token"):
            return _mock_response({"access_token": "tok", "expires_in": 3600})
        return _mock_response({"tracks": {"items": [{"uri": "spotify:track:numb"}]}})

    with _patched_urlopen(handler):
        c = SpotifyClient("cid", "secret")
        uri = c.search("Numb", kind="track")
    assert uri == "spotify:track:numb"


def test_search_kind_artist_returns_artist_uri():
    def handler(req, *args, **kwargs):
        url = req.full_url
        if url.startswith("https://accounts.spotify.com/api/token"):
            return _mock_response({"access_token": "tok", "expires_in": 3600})
        return _mock_response({
            "artists": {"items": [{"name": "Pink Floyd", "uri": "spotify:artist:pf"}]}
        })

    with _patched_urlopen(handler):
        c = SpotifyClient("cid", "secret")
        assert c.search("Pink Floyd", kind="artist") == "spotify:artist:pf"


def test_search_kind_playlist_returns_playlist_uri():
    def handler(req, *args, **kwargs):
        url = req.full_url
        if url.startswith("https://accounts.spotify.com/api/token"):
            return _mock_response({"access_token": "tok", "expires_in": 3600})
        return _mock_response({
            "playlists": {"items": [{"name": "Workout", "uri": "spotify:playlist:wo"}]}
        })

    with _patched_urlopen(handler):
        c = SpotifyClient("cid", "secret")
        assert c.search("Workout", kind="playlist") == "spotify:playlist:wo"


# ---- Unspecified-kind picker (kind=None) --------------------------------

def _all_types_handler(payload: dict, seen_types: list[str] | None = None):
    """Build a urlopen handler that returns `payload` for any /v1/search
    call, capturing the type= query param into seen_types if provided."""
    def handler(req, *args, **kwargs):
        url = req.full_url
        if url.startswith("https://accounts.spotify.com/api/token"):
            return _mock_response({"access_token": "tok", "expires_in": 3600})
        if seen_types is not None:
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(url).query)
            seen_types.append(q.get("type", [""])[0])
        return _mock_response(payload)
    return handler


def test_unspecified_kind_uses_single_multi_type_request():
    """kind=None → one call with type=artist,album,track,playlist."""
    seen_types: list[str] = []
    handler = _all_types_handler({
        "artists": {"items": []},
        "albums": {"items": []},
        "tracks": {"items": [{"name": "X", "uri": "spotify:track:x"}]},
        "playlists": {"items": []},
    }, seen_types=seen_types)

    with _patched_urlopen(handler):
        c = SpotifyClient("cid", "secret")
        c.search("X", kind=None)
    assert seen_types == ["artist,album,track,playlist"], \
        f"Expected one combined search call; got {seen_types!r}"


def test_unspecified_kind_picks_artist_on_exact_name_match():
    """Artist 'Linkin Park' exact-matches the query → pick artist, even
    if albums/tracks substring-match it."""
    handler = _all_types_handler({
        "artists": {"items": [{"name": "Linkin Park", "uri": "spotify:artist:lp"}]},
        "albums": {"items": [{"name": "Linkin Park: The Best", "uri": "spotify:album:lpb"}]},
        "tracks": {"items": [{"name": "Linkin Park Tribute", "uri": "spotify:track:lpt"}]},
        "playlists": {"items": []},
    })
    with _patched_urlopen(handler):
        c = SpotifyClient("cid", "secret")
        assert c.search("Linkin Park") == "spotify:artist:lp"


def test_unspecified_kind_picks_album_when_album_exact_matches():
    """No artist with that name; album exact-matches → album wins over track."""
    handler = _all_types_handler({
        "artists": {"items": [{"name": "Some Artist", "uri": "spotify:artist:sa"}]},
        "albums": {"items": [{"name": "Meteora", "uri": "spotify:album:m"}]},
        "tracks": {"items": [{"name": "Meteora", "uri": "spotify:track:m"}]},
        "playlists": {"items": []},
    })
    with _patched_urlopen(handler):
        c = SpotifyClient("cid", "secret")
        assert c.search("Meteora") == "spotify:album:m"


def test_unspecified_kind_picks_track_when_only_track_matches():
    """No artist/album with the name; only track contains it → track."""
    handler = _all_types_handler({
        "artists": {"items": [{"name": "Dawid Podsiadło", "uri": "spotify:artist:dp"}]},
        "albums": {"items": [{"name": "Małomiasteczkowy Tour Live", "uri": "spotify:album:mt"}]},
        "tracks": {"items": [{"name": "Małomiasteczkowy", "uri": "spotify:track:m"}]},
        "playlists": {"items": []},
    })
    with _patched_urlopen(handler):
        c = SpotifyClient("cid", "secret")
        # Tier 1 (exact): track "Małomiasteczkowy" wins; album name is
        # longer ("Małomiasteczkowy Tour Live") so it only substring-matches.
        assert c.search("Małomiasteczkowy") == "spotify:track:m"


def test_unspecified_kind_picks_playlist_as_last_resort():
    """Only playlist matches → playlist."""
    handler = _all_types_handler({
        "artists": {"items": []},
        "albums": {"items": []},
        "tracks": {"items": []},
        "playlists": {"items": [{"name": "Today's Top Hits", "uri": "spotify:playlist:tth"}]},
    })
    with _patched_urlopen(handler):
        c = SpotifyClient("cid", "secret")
        assert c.search("Today's Top Hits") == "spotify:playlist:tth"


def test_unspecified_kind_diacritic_insensitive_match():
    """Query without diacritics matches name with diacritics, and vice versa."""
    handler = _all_types_handler({
        "artists": {"items": []},
        "albums": {"items": []},
        "tracks": {"items": [{"name": "Małomiasteczkowy", "uri": "spotify:track:m"}]},
        "playlists": {"items": []},
    })
    with _patched_urlopen(handler):
        c = SpotifyClient("cid", "secret")
        # Whisper sometimes drops diacritics. Picker should still match.
        assert c.search("Malomiasteczkowy") == "spotify:track:m"


def test_unspecified_kind_substring_tier_priority():
    """No exact matches anywhere; substring matches in album AND track →
    album wins by priority order (artist > album > track > playlist)."""
    handler = _all_types_handler({
        "artists": {"items": [{"name": "Wholly Unrelated Band", "uri": "spotify:artist:x"}]},
        "albums": {"items": [{"name": "Wish You Were Here Live 1975", "uri": "spotify:album:wywh"}]},
        "tracks": {"items": [{"name": "Wish You Were Here (Live)", "uri": "spotify:track:wywh"}]},
        "playlists": {"items": []},
    })
    with _patched_urlopen(handler):
        c = SpotifyClient("cid", "secret")
        assert c.search("Wish You Were Here") == "spotify:album:wywh"


def test_unspecified_kind_returns_none_when_no_name_matches():
    """Nothing's name contains the query → None (caller falls back to
    open-search-UI behavior)."""
    handler = _all_types_handler({
        "artists": {"items": [{"name": "Wholly Unrelated", "uri": "spotify:artist:x"}]},
        "albums": {"items": [{"name": "Different Title", "uri": "spotify:album:d"}]},
        "tracks": {"items": [{"name": "Another Song", "uri": "spotify:track:a"}]},
        "playlists": {"items": [{"name": "Mismatched Playlist", "uri": "spotify:playlist:m"}]},
    })
    with _patched_urlopen(handler):
        c = SpotifyClient("cid", "secret")
        assert c.search("xyz random unmatchable") is None


def test_unspecified_kind_ignores_null_playlist_items():
    """Spotify occasionally returns null playlist items (deactivated owners)
    — picker should skip them and pick the next valid candidate."""
    handler = _all_types_handler({
        "artists": {"items": []},
        "albums": {"items": []},
        "tracks": {"items": [{"name": "Hello", "uri": "spotify:track:h"}]},
        "playlists": {"items": [None, {"name": "Hello", "uri": "spotify:playlist:h"}]},
    })
    with _patched_urlopen(handler):
        c = SpotifyClient("cid", "secret")
        # Track "Hello" exact-matches and beats playlist by priority.
        assert c.search("Hello") == "spotify:track:h"


def test_invalid_kind_falls_through_to_unspecified():
    """An unrecognized kind string is treated as unset → multi-type search."""
    seen_types: list[str] = []
    handler = _all_types_handler({
        "artists": {"items": []},
        "albums": {"items": [{"name": "hello", "uri": "spotify:album:h"}]},
        "tracks": {"items": []},
        "playlists": {"items": []},
    }, seen_types=seen_types)

    with _patched_urlopen(handler):
        c = SpotifyClient("cid", "secret")
        assert c.search("hello", kind="garbage") == "spotify:album:h"
    assert seen_types == ["artist,album,track,playlist"]
