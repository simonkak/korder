"""Tests for the targeted MPRIS pause/resume actions. Mock the
qdbus + status helpers in korder.audio._mpris so the tests don't
depend on a live D-Bus session."""
from __future__ import annotations
from unittest.mock import patch

import korder.actions  # noqa: F401  (self-register)
from korder.audio import _mpris
from korder.actions.base import get_action
from korder.actions.playback_target import _resolve_target


SPOTIFY = "org.mpris.MediaPlayer2.spotify"
FIREFOX_1 = "org.mpris.MediaPlayer2.firefox.instance_1"
FIREFOX_2 = "org.mpris.MediaPlayer2.firefox.instance_2"
VLC = "org.mpris.MediaPlayer2.vlc"


def _mock_mpris(players, statuses, titles):
    """Returns context-manager patches that fake the MPRIS read API."""
    return (
        patch.object(_mpris, "list_players", return_value=players),
        patch.object(
            _mpris,
            "player_status",
            side_effect=lambda s: statuses.get(s, ""),
        ),
        patch.object(
            _mpris,
            "player_metadata",
            side_effect=lambda s: {"title": titles.get(s, "")},
        ),
    )


# ---- _resolve_target -----------------------------------------------------


def test_resolve_target_matches_player_name():
    """'Spotify' as the target picks the spotify service even when
    Firefox is also running."""
    p1, p2, p3 = _mock_mpris(
        players=[SPOTIFY, FIREFOX_1],
        statuses={SPOTIFY: "Playing", FIREFOX_1: "Playing"},
        titles={SPOTIFY: "Bohemian Rhapsody", FIREFOX_1: "Some YouTube Video"},
    )
    with p1, p2, p3:
        assert _resolve_target("Spotify", prefer_status="Playing") == SPOTIFY


def test_resolve_target_matches_player_among_two_firefoxes_by_title():
    """Two Firefox tabs both score 1 on the player name; the one whose
    title overlaps the rest of the target wins."""
    p1, p2, p3 = _mock_mpris(
        players=[FIREFOX_1, FIREFOX_2],
        statuses={FIREFOX_1: "Playing", FIREFOX_2: "Playing"},
        titles={
            FIREFOX_1: "How I Chose a Linux Distro - YouTube",
            FIREFOX_2: "Cat Compilation - YouTube",
        },
    )
    with p1, p2, p3:
        got = _resolve_target(
            "Firefox How I Chose a Linux Distro", prefer_status="Playing"
        )
        assert got == FIREFOX_1


def test_resolve_target_with_no_target_picks_preferred_status():
    """No target supplied → fall back to status. For pause the
    preferred status is Playing; for resume it's Paused."""
    p1, p2, p3 = _mock_mpris(
        players=[SPOTIFY, FIREFOX_1],
        statuses={SPOTIFY: "Paused", FIREFOX_1: "Playing"},
        titles={SPOTIFY: "Track A", FIREFOX_1: "Tab B"},
    )
    with p1, p2, p3:
        # For pause: pick the Playing one
        assert _resolve_target("", prefer_status="Playing") == FIREFOX_1
        # For resume: pick the Paused one
        assert _resolve_target("", prefer_status="Paused") == SPOTIFY


def test_resolve_target_returns_none_when_no_overlap():
    """Don't pick a random player when the user's named target shares
    no tokens with anything we see — better to surface 'no match' than
    to pause something the user didn't intend."""
    p1, p2, p3 = _mock_mpris(
        players=[SPOTIFY],
        statuses={SPOTIFY: "Playing"},
        titles={SPOTIFY: "Bohemian Rhapsody"},
    )
    with p1, p2, p3:
        assert _resolve_target("VLC", prefer_status="Playing") is None


def test_resolve_target_returns_none_when_no_players():
    with patch.object(_mpris, "list_players", return_value=[]):
        assert _resolve_target("Spotify", prefer_status="Playing") is None


def test_resolve_target_tie_breaks_on_status():
    """Spotify (Paused) and Firefox (Playing) both score 0 against
    'something'. With prefer_status='Playing' the tie-break would pick
    Firefox — but we require a non-zero overlap, so this returns None."""
    p1, p2, p3 = _mock_mpris(
        players=[SPOTIFY, FIREFOX_1],
        statuses={SPOTIFY: "Paused", FIREFOX_1: "Playing"},
        titles={SPOTIFY: "Track A", FIREFOX_1: "Tab B"},
    )
    with p1, p2, p3:
        assert _resolve_target("something", prefer_status="Playing") is None


def test_resolve_target_tie_breaks_on_status_when_overlap_present():
    """Both services match the target token equally; prefer_status
    decides."""
    p1, p2, p3 = _mock_mpris(
        players=[SPOTIFY, FIREFOX_1],
        statuses={SPOTIFY: "Paused", FIREFOX_1: "Playing"},
        # both titles contain 'song' so target='song' ties on overlap
        titles={SPOTIFY: "Best song ever", FIREFOX_1: "Cool song video"},
    )
    with p1, p2, p3:
        # Pause path → prefer Playing
        assert _resolve_target("song", prefer_status="Playing") == FIREFOX_1
        # Resume path → prefer Paused
        assert _resolve_target("song", prefer_status="Paused") == SPOTIFY


# ---- Registered actions --------------------------------------------------


def test_pause_player_action_registered():
    action = get_action("pause_player")
    assert action is not None
    assert "target" in action.parameters


def test_resume_player_action_registered():
    action = get_action("resume_player")
    assert action is not None
    assert "target" in action.parameters


def test_pause_player_op_routes_to_correct_service():
    """Action factory builds a callable that, when invoked, calls
    Pause on the resolved service."""
    pause_calls: list[str] = []
    p1, p2, p3 = _mock_mpris(
        players=[SPOTIFY, FIREFOX_1],
        statuses={SPOTIFY: "Playing", FIREFOX_1: "Playing"},
        titles={SPOTIFY: "Bohemian Rhapsody", FIREFOX_1: "YouTube Video"},
    )
    with (
        p1, p2, p3,
        patch.object(_mpris, "pause_player", side_effect=lambda s: pause_calls.append(s) or True),
    ):
        action = get_action("pause_player")
        kind, fn = action.op_factory({"target": "Spotify"})
        assert kind == "callable"
        fn()
    assert pause_calls == [SPOTIFY]


def test_resume_player_op_routes_to_correct_service():
    play_calls: list[str] = []
    p1, p2, p3 = _mock_mpris(
        players=[SPOTIFY, FIREFOX_1],
        statuses={SPOTIFY: "Paused", FIREFOX_1: "Playing"},
        titles={SPOTIFY: "Track A", FIREFOX_1: "Tab B"},
    )
    with (
        p1, p2, p3,
        patch.object(_mpris, "play_player", side_effect=lambda s: play_calls.append(s) or True),
    ):
        action = get_action("resume_player")
        kind, fn = action.op_factory({"target": "Spotify"})
        kind == "callable"
        fn()
    assert play_calls == [SPOTIFY]


def test_pause_player_op_with_empty_target_picks_playing():
    """No target → pause whatever is currently Playing. This makes
    the bare 'pause' utterance still useful via this action — though
    the LLM should normally route bare 'pause' to play_pause."""
    pause_calls: list[str] = []
    p1, p2, p3 = _mock_mpris(
        players=[SPOTIFY, FIREFOX_1],
        statuses={SPOTIFY: "Paused", FIREFOX_1: "Playing"},
        titles={SPOTIFY: "Track A", FIREFOX_1: "Tab B"},
    )
    with (
        p1, p2, p3,
        patch.object(_mpris, "pause_player", side_effect=lambda s: pause_calls.append(s) or True),
    ):
        action = get_action("pause_player")
        _, fn = action.op_factory({})
        fn()
    assert pause_calls == [FIREFOX_1]


def test_pause_player_op_no_match_does_not_call_dbus():
    pause_calls: list[str] = []
    p1, p2, p3 = _mock_mpris(
        players=[SPOTIFY],
        statuses={SPOTIFY: "Playing"},
        titles={SPOTIFY: "Track"},
    )
    with (
        p1, p2, p3,
        patch.object(_mpris, "pause_player", side_effect=lambda s: pause_calls.append(s) or True),
    ):
        action = get_action("pause_player")
        _, fn = action.op_factory({"target": "VLC"})
        fn()
    assert pause_calls == []
