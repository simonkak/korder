"""Tests for the spotify_play action's dispatch shapes — internal
search vs. direct URI playback. The Spotify Web API itself is mocked;
these tests just exercise the action's op_factory routing and the
new uri parameter introduced alongside the search_spotify tool."""
from __future__ import annotations
from unittest.mock import patch

import korder.actions  # noqa: F401  (register)
from korder.actions.base import get_action


def test_spotify_play_with_uri_skips_internal_search():
    """When params.uri is set, the action must NOT call its own
    SpotifyClient.search_full — the LLM already picked a result via
    search_spotify and we just OpenURI it directly."""
    captured: list[str] = []
    with (
        patch("korder.actions.spotify._open_uri_via_dbus_or_xdg",
              side_effect=lambda u: captured.append(u)),
        patch("korder.actions.spotify._get_client") as get_client,
    ):
        action = get_action("spotify_play")
        op = action.op_factory({
            "uri": "spotify:track:abc123",
            "query": "Numb",  # narration name only
        })
        assert op[0] == "callable"
        op[1]()
    assert captured == ["spotify:track:abc123"]
    # The internal client was never even fetched on this path.
    get_client.assert_not_called()


def test_spotify_play_without_uri_falls_back_to_internal_search():
    """When the LLM dispatches with just a query (no uri), the action
    runs its internal search like before. Backwards-compat with
    pre-tool callers."""
    fake_client = type("F", (), {})()
    captured_searches: list[tuple] = []
    fake_client.search_full = lambda q, kind: (
        captured_searches.append((q, kind))
        or {"uri": "spotify:track:internal", "name": q, "kind": "track"}
    )
    captured_uris: list[str] = []
    with (
        patch("korder.actions.spotify._get_client", return_value=fake_client),
        patch("korder.actions.spotify._open_uri_via_dbus_or_xdg",
              side_effect=lambda u: captured_uris.append(u)),
    ):
        action = get_action("spotify_play")
        op = action.op_factory({"query": "Linkin Park"})
        op[1]()
    assert captured_searches == [("Linkin Park", None)]
    assert captured_uris == ["spotify:track:internal"]


def test_spotify_play_with_uri_and_no_query_still_dispatches():
    """Edge case: LLM emitted only uri (no query) after picking from
    search_spotify. The action should still play that URI; narration
    falls back to the URI string itself."""
    captured: list[str] = []
    with patch(
        "korder.actions.spotify._open_uri_via_dbus_or_xdg",
        side_effect=lambda u: captured.append(u),
    ):
        action = get_action("spotify_play")
        op = action.op_factory({"uri": "spotify:album:xyz"})
        op[1]()
    assert captured == ["spotify:album:xyz"]


def test_spotify_play_declares_search_tool():
    """The action references search_spotify so the LLM is advertised
    the tool whenever spotify_play is in scope. Force-on-skip won't
    fire (parametric tool skip), so the LLM must call deliberately."""
    action = get_action("spotify_play")
    assert "search_spotify" in action.tools
